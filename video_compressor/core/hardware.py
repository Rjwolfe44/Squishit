"""
Hardware detection and optimization for video compression.
Detects CPU, GPU, and hardware encoder capabilities.
"""

import json
import platform
import subprocess
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple, Any
from enum import Enum
import multiprocessing

from .governor import recommend_thread_share, resolve_governor_mode
from .quality_ladder import ordered_hw_vendors

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

logger = logging.getLogger(__name__)

# Vendor token stored on HardwareInfo.preferred_hw_encoder. The family name
# is the encoder API users see (NVENC / QSV / AMF).
_HW_ENCODER_FAMILY = {
    "nvidia": "NVENC",
    "intel": "QSV",
    "amd": "AMF",
    "apple": "VideoToolbox",
}

# Windows PowerShell 5.1 writes UTF-16 to a pipe unless this is set first.
# WMIC is gone by default on Windows 11 24H2/25H2, so CIM is the primary query.
_CIM_VIDEO_COMMAND = (
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false; "
    "$OutputEncoding = [Console]::OutputEncoding; "
    "Get-CimInstance Win32_VideoController | "
    "Select-Object Name,AdapterRAM,PNPDeviceID | "
    "ConvertTo-Json -Compress"
)


def _hidden_window_flag() -> int:
    """CREATE_NO_WINDOW on Windows. Missing on other interpreters."""

    if platform.system() != "Windows":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _decode_command_output(payload: Any) -> str:
    """Decode a Windows console capture.

    ``powershell.exe`` and ``wmic.exe`` often write UTF-16 LE to a pipe.
    ``text=True`` then decodes that as the ANSI code page, JSON parsing
    fails, and the adapter list comes back empty.
    """

    if payload is None:
        return ""
    if isinstance(payload, str):
        if "\x00" not in payload:
            return payload
        try:
            payload = payload.encode("latin-1")
        except UnicodeEncodeError:
            return payload.replace("\x00", "")
    if not payload:
        return ""
    if payload.startswith(b"\xff\xfe") or payload.startswith(b"\xfe\xff"):
        text = payload.decode("utf-16", errors="replace")
    elif len(payload) >= 4 and payload[1] == 0 and payload[3] == 0:
        text = payload.decode("utf-16-le", errors="replace")
    else:
        text = payload.decode("utf-8-sig", errors="replace")
    return text.replace("\x00", "").lstrip("\ufeff").strip()


def _adapter_ram_mb(value: Any) -> int:
    """Megabytes from Win32_VideoController.AdapterRAM.

    The property is a uint32. Values above 2 GiB often arrive as negative
    signed integers, and nothing above 4 GiB can be represented. Callers
    use this for display only; encoder detection does not depend on it.
    """

    if value is None or value is False:
        return 0
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "null":
            return 0
        value = text
    try:
        ram = int(value)
    except (TypeError, ValueError):
        try:
            ram = int(float(value))
        except (TypeError, ValueError):
            return 0
    if ram < 0:
        ram += 1 << 32
    if ram <= 0:
        return 0
    return ram // (1024 * 1024)


def _is_amd_display(name: str, pnp: str = "") -> bool:
    """True for an AMD display adapter, including RX 9070 XT / Navi 48.

    Friendly names usually contain AMD or Radeon. Some driver strings are
    only the chip name (``Navi 48``); those still carry PCI vendor 1002.
    The matching AMD HDMI/DP audio function is not a GPU.
    """

    upper = (name or "").upper()
    if "AUDIO" in upper or "SOUND" in upper:
        return False
    if "AMD" in upper or "RADEON" in upper:
        return True
    pnp_upper = (pnp or "").upper().replace("\\\\", "\\")
    return "PCI\\VEN_1002" in pnp_upper


def _amd_display_rank(name: str) -> int:
    """Put a discrete Radeon ahead of an integrated one.

    CIM often enumerates the iGPU first. The Qt card shows the first GPU
    name for a vendor, so an RX 9070 XT should come before "Radeon Graphics".
    """

    upper = (name or "").upper()
    if re.search(r"\bRX\b", upper) or "RADEON PRO" in upper or "FIREPRO" in upper:
        return 0
    if re.search(r"\bGRAPHICS\b", upper):
        return 2
    return 1


def _parse_cim_video_controllers(payload: Any) -> List[Dict[str, Any]]:
    """Parse ``Get-CimInstance Win32_VideoController`` JSON."""

    text = _decode_command_output(payload)
    if not text:
        return []
    start_obj = text.find("{")
    start_arr = text.find("[")
    starts = [index for index in (start_obj, start_arr) if index >= 0]
    if not starts:
        return []
    try:
        raw = json.loads(text[min(starts):])
    except json.JSONDecodeError:
        logger.debug("Could not parse CIM video-controller JSON")
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    controllers: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or item.get("Caption") or "").strip()
        pnp = str(item.get("PNPDeviceID") or "").strip()
        if not name and not pnp:
            continue
        controllers.append(
            {
                "name": name,
                "memory_mb": _adapter_ram_mb(item.get("AdapterRAM")),
                "pnp": pnp,
            }
        )
    return controllers


def _parse_wmic_video_controllers(payload: Any) -> List[Dict[str, Any]]:
    """Parse ``wmic path Win32_VideoController get name,AdapterRAM``."""

    text = _decode_command_output(payload)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = lines[0].lower()
    if "adapterram" in header or header == "name":
        lines = lines[1:]

    controllers: List[Dict[str, Any]] = []
    for line in lines:
        if line.lower().startswith("no instance"):
            continue
        ram_first = re.match(r"^(?P<ram>-?\d+)\s+(?P<name>.+)$", line)
        if ram_first:
            controllers.append(
                {
                    "name": ram_first.group("name").strip(),
                    "memory_mb": _adapter_ram_mb(ram_first.group("ram")),
                    "pnp": "",
                }
            )
            continue
        name_first = re.match(r"^(?P<name>.+?)\s+(?P<ram>-?\d+)$", line)
        if name_first:
            controllers.append(
                {
                    "name": name_first.group("name").strip(),
                    "memory_mb": _adapter_ram_mb(name_first.group("ram")),
                    "pnp": "",
                }
            )
            continue
        controllers.append({"name": line, "memory_mb": 0, "pnp": ""})
    return controllers


class GPUVendor(Enum):
    """GPU vendor types."""
    NVIDIA = "nvidia"
    AMD = "amd"
    INTEL = "intel"
    APPLE = "apple"
    UNKNOWN = "unknown"


@dataclass
class GPUInfo:
    """Information about a GPU."""
    name: str
    vendor: GPUVendor
    memory_mb: int = 0
    encoder_support: Dict[str, bool] = field(default_factory=dict)
    
    def __str__(self) -> str:
        return f"{self.name} ({self.vendor.value})"


@dataclass
class HardwareInfo:
    """Complete hardware information."""
    os_name: str
    os_version: str
    cpu_name: str
    cpu_cores: int
    cpu_threads: int
    total_ram_gb: float
    gpus: List[GPUInfo] = field(default_factory=list)
    recommended_threads: int = 4
    has_hw_encoder: bool = False
    preferred_hw_encoder: Optional[str] = None

    def encoding_label(self) -> str:
        """Status text for ``cli.py --hardware`` and the hardware summary.

        The vendor token stays ``nvidia`` / ``intel`` / ``amd`` so encoder
        selection is unchanged. The family name is what the machine can run.
        """

        if not self.has_hw_encoder or not self.preferred_hw_encoder:
            return "Not Available"
        family = _HW_ENCODER_FAMILY.get(self.preferred_hw_encoder, "")
        if family:
            return f"Available ({self.preferred_hw_encoder} / {family})"
        return f"Available ({self.preferred_hw_encoder})"

    def __str__(self) -> str:
        lines = [
            f"OS: {self.os_name} {self.os_version}",
            f"CPU: {self.cpu_name} ({self.cpu_threads} threads)",
            f"RAM: {self.total_ram_gb:.1f} GB",
            f"GPUs: {len(self.gpus)}",
        ]
        for gpu in self.gpus:
            lines.append(f"  - {gpu}")
        if self.has_hw_encoder:
            lines.append(f"Hardware Encoding: {self.encoding_label()}")
        return "\n".join(lines)


class HardwareDetector:
    """Detects hardware capabilities for optimal encoding settings."""
    
    def __init__(self):
        self._info: Optional[HardwareInfo] = None
        self._windows_controllers: Optional[List[Dict[str, Any]]] = None
    
    @property
    def info(self) -> HardwareInfo:
        """Get cached hardware information."""
        if self._info is None:
            self._info = self._detect_hardware()
        return self._info
    
    def refresh(self) -> HardwareInfo:
        """Refresh hardware detection."""
        self._info = self._detect_hardware()
        return self._info
    
    def _detect_hardware(self) -> HardwareInfo:
        """Detect all hardware information."""
        # Basic system info
        os_name = platform.system()
        os_version = platform.version()
        
        # CPU info
        cpu_name = self._get_cpu_name()
        cpu_cores = multiprocessing.cpu_count()
        
        # Try to get thread count (logical processors)
        if HAS_PSUTIL:
            cpu_cores = psutil.cpu_count(logical=False) or cpu_cores
            cpu_threads = psutil.cpu_count(logical=True) or cpu_cores
            total_ram = psutil.virtual_memory().total / (1024 ** 3)
        else:
            cpu_threads = cpu_cores
            total_ram = self._get_ram_size()
        
        # Calculate recommended threads for encoding
        # Use most CPU threads by default while leaving a small amount of headroom.
        recommended_threads = max(2, int(cpu_threads * 0.9))
        
        # Detect GPUs
        gpus = self._detect_gpus()
        
        # Check for hardware encoders
        has_hw_encoder, preferred_encoder = self._check_hw_encoders(gpus)
        
        return HardwareInfo(
            os_name=os_name,
            os_version=os_version,
            cpu_name=cpu_name,
            cpu_cores=cpu_cores,
            cpu_threads=cpu_threads,
            total_ram_gb=round(total_ram, 1),
            gpus=gpus,
            recommended_threads=recommended_threads,
            has_hw_encoder=has_hw_encoder,
            preferred_hw_encoder=preferred_encoder
        )
    
    def _get_cpu_name(self) -> str:
        """Get CPU name/model."""
        system = platform.system()
        
        try:
            if system == "Windows":
                import wmi
                c = wmi.WMI()
                return c.Win32_Processor()[0].Name.strip()
            elif system == "Darwin":
                result = subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    capture_output=True, text=True
                )
                return result.stdout.strip()
            elif system == "Linux":
                with open("/proc/cpuinfo", "r") as f:
                    for line in f:
                        if "model name" in line:
                            return line.split(":")[1].strip()
        except Exception as e:
            logger.debug(f"Could not get CPU name: {e}")
        
        return platform.processor() or "Unknown CPU"
    
    def _get_ram_size(self) -> float:
        """Get total RAM in GB."""
        try:
            if platform.system() == "Windows":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                c_ulonglong = ctypes.c_ulonglong
                
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ('dwLength', ctypes.c_ulong),
                        ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', c_ulonglong),
                        ('ullAvailPhys', c_ulonglong),
                        ('ullTotalPageFile', c_ulonglong),
                        ('ullAvailPageFile', c_ulonglong),
                        ('ullTotalVirtual', c_ulonglong),
                        ('ullAvailVirtual', c_ulonglong),
                        ('ullAvailExtendedVirtual', c_ulonglong),
                    ]
                
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(stat)
                kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
                return stat.ullTotalPhys / (1024 ** 3)
            
            elif platform.system() == "Linux":
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        if "MemTotal" in line:
                            # Value is in kB
                            kb = int(line.split()[1])
                            return kb / (1024 ** 2)
        except Exception as e:
            logger.debug(f"Could not get RAM size: {e}")
        
        return 8.0  # Default assumption
    
    def _detect_gpus(self) -> List[GPUInfo]:
        """Detect available GPUs.

        FFmpeg's ``-encoders`` list is not a GPU inventory. A Windows build
        can list ``h264_nvenc``, ``h264_qsv``, and ``h264_amf`` together when
        only one vendor is installed; the others are compile-time stubs.
        Vendor comes from the display-adapter query. Encoder selection checks
        FFmpeg afterwards and still prefers NVENC, then QSV, then AMF.
        """

        gpus = []
        self._windows_controllers = None
        system = platform.system()
        
        # Check for NVIDIA GPUs
        nvidia_gpus = self._detect_nvidia_gpus()
        gpus.extend(nvidia_gpus)
        
        # Check for AMD GPUs
        amd_gpus = self._detect_amd_gpus(system)
        gpus.extend(amd_gpus)
        
        # Check for Intel GPUs
        intel_gpus = self._detect_intel_gpus(system)
        gpus.extend(intel_gpus)
        
        # Check for Apple Silicon
        if system == "Darwin":
            apple_gpu = self._detect_apple_gpu()
            if apple_gpu:
                gpus.append(apple_gpu)
        
        return gpus
    
    def _detect_nvidia_gpus(self) -> List[GPUInfo]:
        """Detect NVIDIA GPUs."""
        gpus = []
        
        try:
            # Try nvidia-smi
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
            
            if result.returncode == 0:
                for line in result.stdout.strip().split('\n'):
                    parts = [p.strip() for p in line.split(',')]
                    if len(parts) >= 2:
                        name = parts[0]
                        try:
                            memory = int(float(parts[1]))
                        except:
                            memory = 0
                        
                        gpus.append(GPUInfo(
                            name=name,
                            vendor=GPUVendor.NVIDIA,
                            memory_mb=memory,
                            encoder_support={
                                "h264": True,  # NVENC supports all recent GPUs
                                "hevc": True,
                                "av1": "RTX 40" in name or "RTX 40" in name,
                            }
                        ))
        except FileNotFoundError:
            logger.debug("nvidia-smi not found, no NVIDIA GPU detected")
        except Exception as e:
            logger.debug(f"Error detecting NVIDIA GPU: {e}")
        
        return gpus
    
    def _detect_amd_gpus(self, system: str) -> List[GPUInfo]:
        """Detect AMD GPUs."""
        gpus = []
        
        if system == "Windows":
            try:
                seen = set()
                for controller in self._get_windows_video_controllers():
                    name = str(controller.get("name") or "").strip()
                    pnp = str(controller.get("pnp") or "")
                    if not _is_amd_display(name, pnp):
                        continue
                    if not name:
                        name = "AMD Radeon"
                    if name in seen:
                        continue
                    seen.add(name)
                    gpus.append(GPUInfo(
                        name=name,
                        vendor=GPUVendor.AMD,
                        memory_mb=int(controller.get("memory_mb") or 0),
                        encoder_support={
                            "h264": True,
                            "hevc": True,
                            "av1": self._amd_supports_av1(name),
                        }
                    ))
            except Exception as e:
                logger.debug(f"Error detecting AMD GPU: {e}")
            gpus.sort(key=lambda gpu: _amd_display_rank(gpu.name))
        
        elif system == "Linux":
            try:
                # Check for AMD GPU in lspci
                result = subprocess.run(
                    ["lspci"],
                    capture_output=True, text=True
                )
                
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if "AMD" in line and "VGA" in line:
                            name = line.split(":")[-1].strip()
                            gpus.append(GPUInfo(
                                name=name,
                                vendor=GPUVendor.AMD,
                                encoder_support={"h264": True, "hevc": True}
                            ))
            except Exception as e:
                logger.debug(f"Error detecting AMD GPU on Linux: {e}")
        
        return gpus
    
    def _detect_intel_gpus(self, system: str) -> List[GPUInfo]:
        """Detect Intel integrated/discrete GPUs."""
        gpus = []
        
        if system == "Windows":
            try:
                for controller in self._get_windows_video_controllers():
                    name = controller.get("name", "").strip()
                    if "Intel" in name and ("UHD" in name or "Iris" in name or "Arc" in name):
                        has_av1 = "Arc" in name or "Ultra" in name
                        gpus.append(GPUInfo(
                            name=name,
                            vendor=GPUVendor.INTEL,
                            memory_mb=controller.get("memory_mb", 0),
                            encoder_support={
                                "h264": True,
                                "hevc": True,
                                "av1": has_av1,
                            }
                        ))
            except Exception as e:
                logger.debug(f"Error detecting Intel GPU: {e}")
        
        return gpus
    
    def _detect_apple_gpu(self) -> Optional[GPUInfo]:
        """Detect Apple Silicon GPU."""
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True
            )
            
            cpu_name = result.stdout.strip()
            
            # Check for Apple Silicon
            if "Apple" in cpu_name or "M1" in cpu_name or "M2" in cpu_name or "M3" in cpu_name:
                # Determine chip variant
                if "M3" in cpu_name:
                    gpu_name = "Apple M3 GPU"
                    has_av1 = True
                elif "M2" in cpu_name:
                    gpu_name = "Apple M2 GPU"
                    has_av1 = "M2 Pro" in cpu_name or "M2 Max" in cpu_name or "M2 Ultra" in cpu_name
                else:
                    gpu_name = "Apple M1 GPU"
                    has_av1 = False
                
                return GPUInfo(
                    name=gpu_name,
                    vendor=GPUVendor.APPLE,
                    encoder_support={
                        "h264": True,
                        "hevc": True,
                        "av1": has_av1,
                    }
                )
        except Exception as e:
            logger.debug(f"Error detecting Apple GPU: {e}")
        
        return None
    
    def _check_hw_encoders(self, gpus: List[GPUInfo]) -> Tuple[bool, Optional[str]]:
        """Pick the preferred vendor in NVENC → QSV → AMF order.

        GPU probes append NVIDIA, then AMD, then Intel. That detection
        order must not choose the winner: Intel QSV outranks AMD AMF when
        both are present, and NVIDIA outranks both.
        """
        vendors: List[str] = []
        for gpu in gpus:
            vendor_name = gpu.vendor.value
            if vendor_name in vendors:
                continue
            if gpu.encoder_support.get("h264") or gpu.encoder_support.get("hevc"):
                vendors.append(vendor_name)
        ordered = ordered_hw_vendors(vendors)
        if not ordered:
            return False, None
        return True, ordered[0]

    def _get_windows_video_controllers(self) -> List[Dict[str, Any]]:
        """Display controllers from CIM, then WMIC if CIM returns nothing.

        Windows 11 24H2 and 25H2 do not install ``wmic.exe`` by default.
        The old query called WMIC first and treated ``FileNotFoundError`` as
        "no AMD GPU", so the CIM fallback never ran. CIM is the query that
        sees an RX 9070 XT on those installs.
        """

        if self._windows_controllers is not None:
            return self._windows_controllers

        controllers = self._controllers_from_cim()
        if not controllers:
            controllers = self._controllers_from_wmic()
        self._windows_controllers = controllers
        return controllers

    def _run_command(self, args: List[str]) -> Optional[subprocess.CompletedProcess]:
        """Run a detector helper. A missing executable is an empty result."""

        try:
            return subprocess.run(
                args,
                capture_output=True,
                check=False,
                creationflags=_hidden_window_flag(),
            )
        except OSError as exc:
            logger.debug("Command %s unavailable: %s", args[0], exc)
            return None

    def _controllers_from_cim(self) -> List[Dict[str, Any]]:
        """``Get-CimInstance Win32_VideoController`` via Windows PowerShell."""

        for executable in ("powershell", "pwsh"):
            result = self._run_command(
                [
                    executable,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    _CIM_VIDEO_COMMAND,
                ]
            )
            if result is None:
                continue
            parsed = _parse_cim_video_controllers(result.stdout)
            if parsed:
                return parsed
            stdout = result.stdout or b""
            stderr = result.stderr or b""
            if result.returncode == 0 and not stdout.strip() and not stderr.strip():
                return []
        return []

    def _controllers_from_wmic(self) -> List[Dict[str, Any]]:
        """Legacy WMIC listing, used only when CIM returned no controllers."""

        result = self._run_command(
            ["wmic", "path", "win32_VideoController", "get", "name,AdapterRAM"]
        )
        if result is None or result.returncode != 0:
            return []
        return _parse_wmic_video_controllers(result.stdout)

    def _amd_supports_av1(self, gpu_name: str) -> bool:
        """Detect AV1 encode support for modern AMD GPUs."""
        normalized = gpu_name.upper()
        av1_patterns = [
            r"RX\s*76\d{2}",
            r"RX\s*77\d{2}",
            r"RX\s*78\d{2}",
            r"RX\s*79\d{2}",
            r"RX\s*8\d{3}",
            r"RX\s*9\d{3}",
            r"RADEON\s+PRO\s+W7",
            r"RADEON\s+AI\s+PRO",
        ]
        return any(re.search(pattern, normalized) for pattern in av1_patterns)
    
    def resolve_resource_governor(
        self,
        governor: str = "auto",
        n_parallel: int = 1,
        codec: Optional[str] = None,
        hw_encoder: Optional[str] = None,
        frame_height: int = 0,
    ) -> str:
        """Resolve the effective resource governor for the current workload.

        Delegates to the encode resource governor so detection and budgeting
        share one mode table.
        """

        info = self.info
        return resolve_governor_mode(
            governor=governor,
            n_parallel=n_parallel,
            codec=codec,
            hw_encoder=hw_encoder,
            frame_height=frame_height,
            cpu_threads=info.cpu_threads,
            total_ram_gb=info.total_ram_gb,
        )

    def recommend_threads_for_job(
        self,
        n_parallel: int = 1,
        governor: str = "auto",
        codec: Optional[str] = None,
        hw_encoder: Optional[str] = None,
        frame_height: int = 0,
    ) -> int:
        """Return a fair thread count when *n_parallel* jobs run simultaneously."""

        info = self.info
        return recommend_thread_share(
            cpu_threads=info.cpu_threads,
            total_ram_gb=info.total_ram_gb,
            n_parallel=n_parallel,
            governor=governor,
            codec=codec,
            hw_encoder=hw_encoder,
            frame_height=frame_height,
        )

    def get_optimal_settings(self, target_codec: str = "hevc") -> Dict:
        """
        Get optimal encoding settings based on hardware.
        
        Args:
            target_codec: Target video codec
            
        Returns:
            Dictionary with optimal settings
        """
        info = self.info
        
        settings = {
            "threads": info.recommended_threads,
            "hw_accel": info.has_hw_encoder,
            "hw_vendor": info.preferred_hw_encoder,
            "preset": "medium",
            "resolution_scale": 1.0,
        }
        
        # Adjust based on RAM
        if info.total_ram_gb >= 32:
            settings["preset"] = "slow"  # Can afford slower encoding
        elif info.total_ram_gb >= 16:
            settings["preset"] = "medium"
        else:
            settings["preset"] = "fast"  # Conserve memory
        
        # GPU-specific optimizations
        if info.has_hw_encoder:
            vendor = info.preferred_hw_encoder
            
            # Find matching GPU
            for gpu in info.gpus:
                if (vendor == "nvidia" and gpu.vendor == GPUVendor.NVIDIA) or \
                   (vendor == "amd" and gpu.vendor == GPUVendor.AMD) or \
                   (vendor == "intel" and gpu.vendor == GPUVendor.INTEL) or \
                   (vendor == "apple" and gpu.vendor == GPUVendor.APPLE):
                    
                    # Check codec support
                    codec_key = target_codec.lower()
                    if codec_key in gpu.encoder_support:
                        if gpu.encoder_support[codec_key]:
                            settings["gpu_codec_support"] = True
                        else:
                            settings["gpu_codec_support"] = False
                            settings["hw_accel"] = False
                    break
        
        return settings
    
    def estimate_encoding_speed(self, codec: str, resolution: Tuple[int, int], 
                                  duration: float, hw_accel: bool = False) -> float:
        """
        Estimate encoding time based on hardware.
        
        Args:
            codec: Video codec
            resolution: Video resolution (width, height)
            duration: Video duration in seconds
            hw_accel: Whether to use hardware acceleration
            
        Returns:
            Estimated encoding time in seconds
        """
        info = self.info
        
        # Base encoding speeds (relative to real-time)
        # These are rough estimates for a modern CPU
        codec_speeds = {
            "h264": 1.5,   # ~1.5x real-time
            "hevc": 0.4,   # ~0.4x real-time (slower)
            "av1": 0.15,   # ~0.15x real-time (very slow)
            "vp9": 0.3,    # ~0.3x real-time
        }
        
        base_speed = codec_speeds.get(codec.lower(), 1.0)
        
        # Adjust for CPU performance (assume baseline is 8 threads at 3GHz)
        thread_factor = min(info.cpu_threads / 8, 2.0)  # Cap at 2x improvement
        
        # Resolution factor (relative to 1080p)
        pixels = resolution[0] * resolution[1]
        res_factor = pixels / (1920 * 1080)
        
        # Hardware acceleration provides ~5-10x speedup
        hw_factor = 0.15 if hw_accel else 1.0
        
        # Calculate estimate
        encoding_time = duration / (base_speed * thread_factor * hw_factor) * res_factor
        
        return encoding_time


# Global instance for convenience
_hardware_detector: Optional[HardwareDetector] = None


def get_hardware_detector() -> HardwareDetector:
    """Get the global hardware detector instance."""
    global _hardware_detector
    if _hardware_detector is None:
        _hardware_detector = HardwareDetector()
    return _hardware_detector