"""
Hardware detection and optimization for video compression.
Detects CPU, GPU, and hardware encoder capabilities.
"""

import platform
import subprocess
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from enum import Enum
import multiprocessing

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

logger = logging.getLogger(__name__)


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
            lines.append(f"Hardware Encoding: {self.preferred_hw_encoder}")
        return "\n".join(lines)


class HardwareDetector:
    """Detects hardware capabilities for optimal encoding settings."""
    
    def __init__(self):
        self._info: Optional[HardwareInfo] = None
    
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
        """Detect available GPUs."""
        gpus = []
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
                for controller in self._get_windows_video_controllers():
                    name = controller.get("name", "").strip()
                    if not name:
                        continue
                    if "AMD" in name.upper() or "RADEON" in name.upper():
                        gpus.append(GPUInfo(
                            name=name,
                            vendor=GPUVendor.AMD,
                            memory_mb=controller.get("memory_mb", 0),
                            encoder_support={
                                "h264": True,
                                "hevc": True,
                                "av1": self._amd_supports_av1(name),
                            }
                        ))
            except Exception as e:
                logger.debug(f"Error detecting AMD GPU: {e}")
        
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
        """Check for available hardware encoders."""
        for gpu in gpus:
            if gpu.vendor == GPUVendor.NVIDIA:
                return True, "nvidia"
            elif gpu.vendor == GPUVendor.AMD:
                return True, "amd"
            elif gpu.vendor == GPUVendor.INTEL:
                return True, "intel"
            elif gpu.vendor == GPUVendor.APPLE:
                return True, "apple"
        
        return False, None

    def _get_windows_video_controllers(self) -> List[Dict[str, int | str]]:
        """Get Windows display controllers with names and memory where possible."""
        controllers: List[Dict[str, int | str]] = []

        wmic_result = subprocess.run(
            ["wmic", "path", "win32_VideoController", "get", "name,AdapterRAM"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        if wmic_result.returncode == 0 and wmic_result.stdout.strip():
            for line in wmic_result.stdout.strip().splitlines()[1:]:
                line = line.strip()
                if not line:
                    continue
                match = re.match(r"^(?P<name>.+?)\s+(?P<ram>\d+)$", line)
                if match:
                    ram_bytes = int(match.group("ram"))
                    controllers.append({
                        "name": match.group("name").strip(),
                        "memory_mb": ram_bytes // (1024 * 1024),
                    })
                    continue

                reversed_match = re.match(r"^(?P<ram>\d+)\s+(?P<name>.+)$", line)
                if reversed_match:
                    ram_bytes = int(reversed_match.group("ram"))
                    controllers.append({
                        "name": reversed_match.group("name").strip(),
                        "memory_mb": ram_bytes // (1024 * 1024),
                    })
                else:
                    controllers.append({"name": line, "memory_mb": 0})

        if controllers:
            return controllers

        powershell_result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        if powershell_result.returncode != 0 or not powershell_result.stdout.strip():
            return []

        import json

        raw = json.loads(powershell_result.stdout)
        if isinstance(raw, dict):
            raw = [raw]

        for item in raw:
            ram_bytes = int(item.get("AdapterRAM") or 0)
            controllers.append({
                "name": str(item.get("Name") or "").strip(),
                "memory_mb": ram_bytes // (1024 * 1024),
            })

        return controllers

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
        """Resolve the effective resource governor for the current workload."""

        normalized = (governor or "auto").lower()
        if normalized in {"low", "balanced", "max"}:
            return normalized

        info = self.info
        codec_name = (codec or "").lower()
        heavy_cpu_codec = codec_name in {"av1", "svt-av1"} and not hw_encoder
        high_resolution = frame_height >= 2160

        if n_parallel >= 4:
            return "low" if info.cpu_threads <= 12 else "balanced"

        if hw_encoder:
            if n_parallel >= 3:
                return "low"
            if high_resolution or info.total_ram_gb < 16:
                return "balanced"
            return "low" if codec_name == "h264" else "balanced"

        if heavy_cpu_codec:
            if n_parallel == 1 and info.cpu_threads >= 12 and info.total_ram_gb >= 16:
                return "max"
            return "balanced"

        if codec_name == "hevc":
            return "balanced" if n_parallel <= 2 else "low"

        return "balanced"

    def recommend_threads_for_job(
        self,
        n_parallel: int = 1,
        governor: str = "auto",
        codec: Optional[str] = None,
        hw_encoder: Optional[str] = None,
        frame_height: int = 0,
    ) -> int:
        """Return a fair thread count when *n_parallel* jobs run simultaneously."""

        resolved_governor = self.resolve_resource_governor(
            governor=governor,
            n_parallel=n_parallel,
            codec=codec,
            hw_encoder=hw_encoder,
            frame_height=frame_height,
        )
        governor_scale = {
            "low": 0.45,
            "balanced": 0.7,
            "max": 1.0,
        }[resolved_governor]

        if resolved_governor == "max" and not hw_encoder and self.info.total_ram_gb < 16:
            governor_scale = 0.85

        total_budget = max(1, int(self.info.cpu_threads * governor_scale))
        return max(1, int(total_budget / max(1, n_parallel)))

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