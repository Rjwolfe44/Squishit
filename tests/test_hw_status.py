"""Plain-language hardware status. The encoder still comes from the real picker."""

from __future__ import annotations

from types import SimpleNamespace

from video_compressor.core.codecs import VideoCodec
from video_compressor.core.compressor import VideoCompressor
from video_compressor.core.hardware import GPUInfo, GPUVendor, HardwareInfo
from video_compressor.gui.hw_status import (
    FoundHardware,
    HardwareProbe,
    describe_hw_status,
    findings_from_info,
    probe_from_compressor,
)


def _info(*gpus):
    return HardwareInfo(
        os_name="Linux",
        os_version="test",
        cpu_name="cpu",
        cpu_cores=4,
        cpu_threads=8,
        total_ram_gb=16,
        gpus=list(gpus),
        preferred_hw_encoder="amd",
    )


def test_findings_follow_nvenc_then_qsv_then_amf_not_probe_order():
    info = _info(
        GPUInfo("AMD Radeon", GPUVendor.AMD, encoder_support={"h264": True}),
        GPUInfo("Intel UHD 770", GPUVendor.INTEL, encoder_support={"h264": True}),
        GPUInfo(
            "NVIDIA GeForce RTX 4070", GPUVendor.NVIDIA, encoder_support={"h264": True}
        ),
    )
    assert [item.family for item in findings_from_info(info)] == ["NVENC", "QSV", "AMF"]
    assert findings_from_info(info)[0].gpu_name == "NVIDIA GeForce RTX 4070"


def test_probe_reports_the_picker_result_without_reordering_it():
    """A stub picker that returns QSV must stay QSV even when NVENC was found."""

    seen = []

    class Stub:
        hw_detector = SimpleNamespace(
            info=_info(
                GPUInfo("NVIDIA", GPUVendor.NVIDIA, encoder_support={"h264": True}),
                GPUInfo("Intel", GPUVendor.INTEL, encoder_support={"h264": True}),
            )
        )

        def _select_hw_encoder(self, codec):
            seen.append(codec)
            if codec is VideoCodec.H264:
                return "h264_qsv"
            return None

    probe = probe_from_compressor(Stub())
    assert probe.failed is False
    assert probe.encoder_for(VideoCodec.H264) == "h264_qsv"
    assert [item.family for item in probe.found] == ["NVENC", "QSV"]
    assert VideoCodec.H264 in seen
    assert VideoCodec.SVT_AV1 in seen


def test_real_picker_still_prefers_nvenc_for_the_home_probe(monkeypatch):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = {
        "h264_nvenc",
        "h264_qsv",
        "h264_amf",
        "hevc_nvenc",
        "libx264",
        "libx265",
    }
    compressor.hw_detector._info = _info(
        GPUInfo("AMD", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True}),
        GPUInfo("Intel", GPUVendor.INTEL, encoder_support={"h264": True, "hevc": True}),
        GPUInfo(
            "NVIDIA GeForce RTX 4070",
            GPUVendor.NVIDIA,
            encoder_support={"h264": True, "hevc": True},
        ),
    )

    probe = probe_from_compressor(compressor)
    status = describe_hw_status(
        probe,
        codec=VideoCodec.H264,
        use_hw=True,
        force_software=False,
        action="Quick Compress",
        software_name="libx264",
    )
    assert probe.encoder_for(VideoCodec.H264) == "h264_nvenc"
    assert status.badge == "NVENC"
    assert status.state == "ready"
    assert "RTX 4070" in status.found
    assert "(QSV)" in status.found
    assert status.using == "Quick Compress will use NVIDIA hardware (NVENC)."
    assert status.tooltip == "h264_nvenc"

    compressor.codec_manager._available_encoders.discard("h264_nvenc")
    probe = probe_from_compressor(compressor)
    status = describe_hw_status(
        probe,
        codec=VideoCodec.H264,
        use_hw=True,
        force_software=False,
    )
    assert probe.encoder_for(VideoCodec.H264) == "h264_qsv"
    assert status.badge == "QSV"
    assert "instead of NVIDIA hardware (NVENC)" in status.using


def test_status_copy_covers_pending_failure_and_software_fallback():
    pending = describe_hw_status(
        None,
        codec=VideoCodec.H264,
        use_hw=True,
        force_software=False,
    )
    assert pending.badge == "Checking…"
    assert pending.state == "pending"
    assert "Checking this PC" in pending.found
    assert "software" not in pending.using.lower()

    failed = describe_hw_status(
        HardwareProbe(failed=True),
        codec=VideoCodec.H264,
        use_hw=True,
        force_software=False,
        software_name="libx264",
    )
    assert failed.state == "failed"
    assert failed.found == "Hardware detection failed."
    assert failed.using == "Quick Compress will use software encoding."

    nothing = describe_hw_status(
        HardwareProbe(),
        codec=VideoCodec.H264,
        use_hw=True,
        force_software=False,
    )
    assert nothing.badge == "Software"
    assert nothing.found == "No hardware encoder found."
    assert "will use software encoding" in nothing.using

    found = (
        FoundHardware("NVENC", "NVIDIA GeForce RTX 4070"),
        FoundHardware("AMF", "AMD Radeon RX 9070 XT"),
    )
    unusable = describe_hw_status(
        HardwareProbe(found=found, encoders={"h264": None, "hevc": None}),
        codec=VideoCodec.HEVC,
        use_hw=True,
        force_software=False,
    )
    assert unusable.state == "software"
    assert "RTX 4070" in unusable.found
    assert "not available for this preset" in unusable.using
    assert "software encoding" in unusable.using

    forced = describe_hw_status(
        HardwareProbe(found=found, encoders={"svt-av1": None}),
        codec=VideoCodec.SVT_AV1,
        use_hw=False,
        force_software=True,
        action="This preset",
        software_name="libsvtav1",
    )
    assert forced.badge == "Software"
    assert "RTX 4070" in forced.found
    assert "keeps hardware off" in forced.using
    assert "software encoding" in forced.using
    assert forced.tooltip == "libsvtav1"

    off = describe_hw_status(
        HardwareProbe(found=found, encoders={"h264": "h264_nvenc"}),
        codec=VideoCodec.H264,
        use_hw=False,
        force_software=False,
    )
    assert "turned off" in off.using
    assert "NVENC" in off.found


def test_missing_detector_is_a_visible_failure():
    probe = probe_from_compressor(SimpleNamespace())
    assert probe.failed is True
    status = describe_hw_status(
        probe,
        codec=VideoCodec.H264,
        use_hw=True,
        force_software=False,
    )
    assert status.state == "failed"
    assert "failed" in status.found.lower()
