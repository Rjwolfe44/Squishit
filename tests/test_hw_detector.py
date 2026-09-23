"""GPU detection for AMF. No physical RX 9070 XT required.

Windows 11 24H2/25H2 does not ship ``wmic.exe``. These fixtures are the
Deus-Ex-Machina case: CIM lists a Radeon RX 9070 XT (and an AMD iGPU), and
FFmpeg lists ``h264_amf`` / ``hevc_amf`` next to NVENC and QSV stubs.
"""

from __future__ import annotations

import json
import platform
import subprocess

import pytest

from video_compressor.core.codecs import VideoCodec
from video_compressor.core.compressor import VideoCompressor
from video_compressor.core.hardware import (
    GPUVendor,
    HardwareDetector,
    _decode_command_output,
    _parse_cim_video_controllers,
)
from video_compressor.gui.hw_status import describe_hw_status, probe_from_compressor

_DEUS_ADAPTERS = [
    {
        "Name": "AMD Radeon(TM) Graphics",
        "AdapterRAM": 512 * 1024 * 1024,
        "PNPDeviceID": r"PCI\VEN_1002&DEV_13C0&SUBSYS_00000000&REV_C1",
    },
    {
        "Name": "AMD High Definition Audio Device",
        "AdapterRAM": None,
        "PNPDeviceID": r"HDAUDIO\FUNC_01&VEN_1002&DEV_AA01",
    },
    {
        "Name": "AMD Radeon RX 9070 XT",
        "AdapterRAM": -2147483648,
        "PNPDeviceID": r"PCI\VEN_1002&DEV_7550&SUBSYS_00000000&REV_C0",
    },
]

_FFMPEG_STUBS = {
    "h264_nvenc",
    "hevc_nvenc",
    "h264_qsv",
    "hevc_qsv",
    "h264_amf",
    "hevc_amf",
    "libx264",
    "libx265",
}


def _cim_bytes(adapters, *, encoding="utf-16") -> bytes:
    """PowerShell's pipe default is UTF-16, not the ANSI code page."""

    return json.dumps(adapters).encode(encoding)


def _install_queries(
    monkeypatch,
    *,
    cim: bytes | None = None,
    wmic: bytes | None = None,
    powershell_missing: bool = False,
):
    """Pretend this process is Windows and stub the adapter queries."""

    real_run = subprocess.run
    seen: list[list[str]] = []

    def fake_run(args, **kwargs):
        exe = str(args[0])
        seen.append([str(part) for part in args])
        if exe == "nvidia-smi":
            raise FileNotFoundError(exe)
        if exe == "lspci":
            raise FileNotFoundError(exe)
        if exe in {"powershell", "pwsh"}:
            if powershell_missing or exe == "pwsh":
                raise FileNotFoundError(exe)
            return subprocess.CompletedProcess(args, 0, stdout=cim or b"", stderr=b"")
        if exe == "wmic":
            if wmic is None:
                raise FileNotFoundError(exe)
            return subprocess.CompletedProcess(args, 0, stdout=wmic, stderr=b"")
        return real_run(args, **kwargs)

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def _compressor(detector: HardwareDetector, encoders: set[str]) -> VideoCompressor:
    compressor = VideoCompressor(hw_detector=detector)
    compressor.codec_manager._available_encoders = set(encoders)
    return compressor


@pytest.fixture
def quiet_tools(monkeypatch):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)


def test_utf16_cim_json_decodes_to_the_9070_xt():
    payload = _cim_bytes(_DEUS_ADAPTERS, encoding="utf-16")
    assert payload.startswith(b"\xff\xfe")
    text = _decode_command_output(payload)
    assert "AMD Radeon RX 9070 XT" in text
    # latin-1 of this pipe is what text=True used to feed json.loads.
    assert "\x00" in payload.decode("latin-1")
    parsed = _parse_cim_video_controllers(payload)
    names = [item["name"] for item in parsed]
    assert "AMD Radeon RX 9070 XT" in names
    xt = next(item for item in parsed if "9070 XT" in item["name"])
    assert xt["memory_mb"] == 2048
    assert xt["memory_mb"] > 0


def test_missing_wmic_reports_rx_9070_xt_and_prefers_amf(monkeypatch, quiet_tools):
    """The failure on Deus: wmic is absent, CIM is UTF-16, FFmpeg has stubs."""

    seen = _install_queries(
        monkeypatch,
        cim=_cim_bytes(_DEUS_ADAPTERS),
        wmic=None,
    )
    detector = HardwareDetector()
    info = detector.refresh()

    names = [gpu.name for gpu in info.gpus]
    assert names[0] == "AMD Radeon RX 9070 XT"
    assert "AMD Radeon(TM) Graphics" in names
    assert all("Audio" not in name for name in names)
    assert info.has_hw_encoder is True
    assert info.preferred_hw_encoder == "amd"
    assert info.encoding_label() == "Available (amd / AMF)"

    discrete = info.gpus[0]
    assert discrete.vendor is GPUVendor.AMD
    assert discrete.encoder_support["h264"] is True
    assert discrete.encoder_support["hevc"] is True
    assert discrete.encoder_support["av1"] is True
    integrated = next(gpu for gpu in info.gpus if "Graphics" in gpu.name)
    assert integrated.encoder_support["av1"] is False

    commands = [args[0] for args in seen]
    assert commands.count("powershell") == 1
    assert "wmic" not in commands
    assert "ffmpeg" not in commands

    compressor = _compressor(detector, _FFMPEG_STUBS)
    assert compressor._select_hw_encoder(VideoCodec.H264) == "h264_amf"
    assert compressor._select_hw_encoder(VideoCodec.HEVC) == "hevc_amf"

    probe = probe_from_compressor(compressor)
    status = describe_hw_status(
        probe,
        codec=VideoCodec.H264,
        use_hw=True,
        force_software=False,
        action="Quick Compress",
        software_name="libx264",
    )
    assert status.badge == "AMF"
    assert status.state == "ready"
    assert "AMD Radeon RX 9070 XT (AMF)" in status.found
    assert status.using == "Quick Compress will use AMD hardware (AMF)."
    assert status.tooltip == "h264_amf"
    assert "software" not in status.using.lower()


def test_no_display_adapter_stays_on_software_despite_ffmpeg_stubs(
    monkeypatch, quiet_tools
):
    basic = [
        {
            "Name": "Microsoft Basic Display Adapter",
            "AdapterRAM": 0,
            "PNPDeviceID": r"PCI\VEN_1414&DEV_008C",
        }
    ]
    _install_queries(monkeypatch, cim=_cim_bytes(basic), wmic=None)
    detector = HardwareDetector()
    info = detector.refresh()

    assert info.gpus == []
    assert info.has_hw_encoder is False
    assert info.preferred_hw_encoder is None
    assert info.encoding_label() == "Not Available"

    compressor = _compressor(detector, _FFMPEG_STUBS)
    assert compressor._select_hw_encoder(VideoCodec.H264) is None
    assert compressor._select_hw_encoder(VideoCodec.HEVC) is None

    status = describe_hw_status(
        probe_from_compressor(compressor),
        codec=VideoCodec.H264,
        use_hw=True,
        force_software=False,
        action="Quick Compress",
        software_name="libx264",
    )
    assert status.badge == "Software"
    assert status.state == "software"
    assert status.found == "No hardware encoder found."
    assert "software encoding" in status.using


def test_qsv_still_outranks_amf_when_both_adapters_exist(monkeypatch, quiet_tools):
    adapters = [
        {
            "Name": "AMD Radeon RX 9070 XT",
            "AdapterRAM": 0,
            "PNPDeviceID": r"PCI\VEN_1002&DEV_7550",
        },
        {
            "Name": "Intel Arc A770",
            "AdapterRAM": 1024,
            "PNPDeviceID": r"PCI\VEN_8086&DEV_56A0",
        },
    ]
    _install_queries(monkeypatch, cim=json.dumps(adapters).encode("utf-8"))
    detector = HardwareDetector()
    info = detector.refresh()
    assert info.preferred_hw_encoder == "intel"
    assert info.encoding_label() == "Available (intel / QSV)"
    compressor = _compressor(detector, _FFMPEG_STUBS)
    assert compressor._select_hw_encoder(VideoCodec.H264) == "h264_qsv"


def test_wmic_fallback_keeps_a_clean_9070_name_when_powershell_is_missing(
    monkeypatch,
):
    wmic = (
        "AdapterRAM  Name\r\n-2147483648  AMD Radeon RX 9070 XT\r\n"
    ).encode("utf-16")
    seen = _install_queries(monkeypatch, cim=None, wmic=wmic, powershell_missing=True)
    info = HardwareDetector().refresh()
    assert [gpu.name for gpu in info.gpus] == ["AMD Radeon RX 9070 XT"]
    assert info.gpus[0].memory_mb == 2048
    assert info.preferred_hw_encoder == "amd"
    commands = [args[0] for args in seen]
    assert "powershell" in commands
    assert "wmic" in commands


def test_pci_vendor_1002_counts_when_the_friendly_name_omits_radeon(monkeypatch):
    # One adapter is a JSON object, not an array, and PowerShell may print
    # a warning before it.
    adapter = {
        "Name": "Navi 48",
        "AdapterRAM": 0,
        "PNPDeviceID": r"PCI\VEN_1002&DEV_7550&SUBSYS_00000000&REV_C0",
    }
    payload = ("WARNING: sample\n" + json.dumps(adapter)).encode("utf-8")
    _install_queries(monkeypatch, cim=payload)
    info = HardwareDetector().refresh()
    assert [gpu.name for gpu in info.gpus] == ["Navi 48"]
    assert info.gpus[0].vendor is GPUVendor.AMD
    assert info.has_hw_encoder is True
    assert info.preferred_hw_encoder == "amd"


def test_linux_lspci_vga_line_still_detects_amd(monkeypatch):
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if args[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi")
        if args[0] == "lspci":
            text = (
                "03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. "
                "[AMD/ATI] Navi 48 [Radeon RX 9070/9070 XT]\n"
            )
            return subprocess.CompletedProcess(args, 0, stdout=text, stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", fake_run)
    gpus = HardwareDetector()._detect_gpus()
    assert len(gpus) == 1
    assert gpus[0].vendor is GPUVendor.AMD
    assert "9070" in gpus[0].name


def test_cli_hardware_lists_amd_amf(monkeypatch, capsys):
    import video_compressor.core.hardware as hardware
    from cli import show_hardware

    _install_queries(monkeypatch, cim=_cim_bytes(_DEUS_ADAPTERS))
    monkeypatch.setattr(hardware, "_hardware_detector", None)
    show_hardware()
    out = capsys.readouterr().out
    assert "AMD Radeon RX 9070 XT" in out
    assert "AMD Radeon(TM) Graphics" in out
    assert "Hardware Encoding: Available (amd / AMF)" in out
    assert "Not Available" not in out
    assert "Audio" not in out
