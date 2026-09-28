"""Fast hardware AV1 versus Max / Archival SVT-AV1.

Max / Archival stays on libsvtav1. On Windows that encode may decode with
D3D11VA and, on Resource Max with one job, sets lp to the logical CPU count.
VideoCodec.AV1 with hardware on picks av1_nvenc, then av1_qsv, then av1_amf.
"""

from __future__ import annotations

import platform
from pathlib import Path

from video_compressor.core.codecs import AudioCodec, VideoCodec
from video_compressor.core.compressor import (
    VideoCompressor,
    VideoInfo,
    decode_hwaccel_failed,
    software_svt_decode_hwaccel,
)
from video_compressor.core.hardware import GPUInfo, GPUVendor, HardwareInfo
from video_compressor.core.profiles import (
    ARCHIVAL_PROFILE_NAME,
    CompressionProfile,
    ProfileManager,
    ProfileType,
)
from video_compressor.core.quality_ladder import EncoderChoice, QualityRung, get_step
from video_compressor.gui.copy import PROFILE_FRIENDLY
from video_compressor.gui.hw_status import FoundHardware, HardwareProbe, describe_hw_status
from video_compressor.gui.qt_shell.encode_form import CODEC_LABELS, EncodeForm


LOGICAL_CPUS = 16


def _video_info(path: Path) -> VideoInfo:
    return VideoInfo(
        filepath=path,
        duration=10.0,
        size=5_000_000,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=3_000_000,
        audio_bitrate=128_000,
        total_bitrate=3_128_000,
        frame_count=300,
    )


def _compressor(monkeypatch, *, gpus, encoders, ram_gb=8.0) -> VideoCompressor:
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = set(encoders)
    compressor.hw_detector._info = HardwareInfo(
        os_name="Windows",
        os_version="11",
        cpu_name="cpu",
        cpu_cores=8,
        cpu_threads=LOGICAL_CPUS,
        total_ram_gb=ram_gb,
        gpus=list(gpus),
        recommended_threads=LOGICAL_CPUS,
    )
    return compressor


def _capture(compressor, profile, source: Path, output: Path, runs):
    """Run compress() and return each FFmpeg argv the stub saw."""

    captured = []

    def fake_run(cmd, *, job, job_id, media_info, start_time, **kwargs):
        captured.append(list(cmd))
        outcome = runs[len(captured) - 1]
        if outcome == "ok":
            job.output_file.parent.mkdir(parents=True, exist_ok=True)
            job.output_file.write_bytes(b"encoded")
            return 0, []
        return 1, list(outcome)

    compressor._run_ffmpeg_process = fake_run
    compressor.analyze_media = lambda path: _video_info(path)
    result = compressor.compress(source, output, profile, job_id=output.stem)
    return captured, result


def _encoder(cmd):
    return cmd[cmd.index("-c:v") + 1]


def _lp(cmd) -> int:
    params = cmd[cmd.index("-svtav1-params") + 1]
    values = dict(part.split("=", 1) for part in params.split(":") if "=" in part)
    return int(values["lp"])


def _archival(tmp_path: Path) -> CompressionProfile:
    stored = ProfileManager(config_dir=tmp_path / "profiles").get_profile(ARCHIVAL_PROFILE_NAME)
    profile = CompressionProfile.from_dict(stored.to_dict())
    profile.resource_governor = "max"
    return profile


def _amd_9070() -> GPUInfo:
    return GPUInfo(
        "AMD Radeon RX 9070 XT",
        GPUVendor.AMD,
        encoder_support={"h264": True, "hevc": True, "av1": True},
    )


_HW_AV1 = {"av1_nvenc", "av1_qsv", "av1_amf", "libsvtav1", "libaom-av1", "libopus", "aac"}


def test_decode_hwaccel_is_windows_svt_only():
    assert (
        software_svt_decode_hwaccel(
            system="Windows",
            codec=VideoCodec.SVT_AV1,
            hw_encoder=None,
        )
        == "d3d11va"
    )
    assert (
        software_svt_decode_hwaccel(
            system="Linux",
            codec=VideoCodec.SVT_AV1,
            hw_encoder=None,
        )
        is None
    )
    assert (
        software_svt_decode_hwaccel(
            system="Windows",
            codec=VideoCodec.AV1,
            hw_encoder="av1_amf",
        )
        is None
    )
    assert (
        software_svt_decode_hwaccel(
            system="Windows",
            codec=VideoCodec.SVT_AV1,
            hw_encoder="av1_amf",
        )
        is None
    )
    failed = ["ffmpeg", "-hwaccel", "d3d11va", "-i", "in.mp4", "-c:v", "libsvtav1"]
    assert decode_hwaccel_failed(failed, "Failed to create Direct3D 11 device")
    assert decode_hwaccel_failed(failed, "[d3d11va @ 0] hwaccel init failed")
    # The flag name in a later SVT error is not a decode-device failure.
    assert not decode_hwaccel_failed(
        failed,
        "Error setting option crf to value\nffmpeg -hwaccel d3d11va -i in.mp4",
    )
    assert not decode_hwaccel_failed(
        ["ffmpeg", "-i", "in.mp4", "-c:v", "av1_amf"],
        "d3d11 device creation failed",
    )


def test_hw_av1_picker_is_nvenc_then_qsv_then_amf(monkeypatch):
    gpus = [
        GPUInfo("AMD Radeon(TM) Graphics", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True, "av1": False}),
        _amd_9070(),
        GPUInfo("Intel Arc", GPUVendor.INTEL, encoder_support={"h264": True, "hevc": True, "av1": True}),
        GPUInfo("RTX 4070", GPUVendor.NVIDIA, encoder_support={"h264": True, "hevc": True, "av1": True}),
    ]
    compressor = _compressor(monkeypatch, gpus=gpus, encoders=_HW_AV1)
    assert compressor._select_hw_encoder(VideoCodec.AV1) == "av1_nvenc"
    assert compressor._select_hw_encoder(VideoCodec.SVT_AV1) is None

    gpus[3].encoder_support["av1"] = False
    assert compressor._select_hw_encoder(VideoCodec.AV1) == "av1_qsv"

    gpus[2].encoder_support["av1"] = False
    assert compressor._select_hw_encoder(VideoCodec.AV1) == "av1_amf"

    compressor.codec_manager._available_encoders.discard("av1_amf")
    assert compressor._select_hw_encoder(VideoCodec.AV1) is None


def test_max_svt_job_uses_d3d11va_decode_and_full_lp(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    compressor = _compressor(monkeypatch, gpus=[_amd_9070()], encoders=_HW_AV1, ram_gb=8.0)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    cmds, result = _capture(
        compressor,
        _archival(tmp_path),
        source,
        tmp_path / "archival.mkv",
        ["ok"],
    )

    assert result.success, result.error_message
    assert result.encoder_name == "libsvtav1"
    assert result.video_codec == "svt-av1"
    assert result.threads_used == LOGICAL_CPUS
    cmd = cmds[0]
    assert cmd[:6] == ["ffmpeg", "-y", "-hide_banner", "-hwaccel", "d3d11va", "-i"]
    assert _encoder(cmd) == "libsvtav1"
    assert _lp(cmd) == LOGICAL_CPUS
    assert cmd.count("-threads") == 0
    assert "-hwaccel_output_format" not in cmd
    assert not any(token in cmd for token in ("av1_amf", "av1_nvenc", "av1_qsv", "libaom-av1"))


def test_d3d11va_failure_retries_software_decode_on_libsvtav1(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    compressor = _compressor(monkeypatch, gpus=[_amd_9070()], encoders=_HW_AV1)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    cmds, result = _capture(
        compressor,
        _archival(tmp_path),
        source,
        tmp_path / "archival.mkv",
        [["[d3d11va @ 0] Failed to create Direct3D 11 device", "hwaccel init failed"], "ok"],
    )

    assert result.success, result.error_message
    assert result.attempt_count == 2
    assert result.encoder_name == "libsvtav1"
    assert len(cmds) == 2
    assert cmds[0][cmds[0].index("-hwaccel") + 1] == "d3d11va"
    assert "-hwaccel" not in cmds[1]
    assert _encoder(cmds[0]) == "libsvtav1"
    assert _encoder(cmds[1]) == "libsvtav1"
    assert _lp(cmds[1]) == LOGICAL_CPUS
    assert "av1_amf" not in cmds[0]
    assert "av1_amf" not in cmds[1]


def test_unrelated_svt_failure_does_not_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    compressor = _compressor(monkeypatch, gpus=[_amd_9070()], encoders=_HW_AV1)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    cmds, result = _capture(
        compressor,
        _archival(tmp_path),
        source,
        tmp_path / "archival.mkv",
        [["Error setting option crf to value"]],
    )

    assert result.success is False
    assert result.attempt_count == 1
    assert len(cmds) == 1
    assert _encoder(cmds[0]) == "libsvtav1"


def test_av1_with_hw_on_amd_selects_av1_amf_not_svt(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    compressor = _compressor(monkeypatch, gpus=[_amd_9070()], encoders=_HW_AV1, ram_gb=32.0)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    profile = CompressionProfile(
        name="Fast HW AV1",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.AV1,
        crf=get_step(VideoCodec.AV1, QualityRung.QUICK).crf,
        preset=get_step(VideoCodec.AV1, QualityRung.QUICK).preset,
        audio_codec=AudioCodec.AAC,
        audio_bitrate=128_000,
        video_container="mkv",
        use_hw_accel=True,
        resource_governor="max",
    )
    cmds, result = _capture(compressor, profile, source, tmp_path / "fast.mkv", ["ok"])

    assert result.success, result.error_message
    assert result.encoder_name == "av1_amf"
    assert result.video_codec == "av1"
    cmd = cmds[0]
    assert _encoder(cmd) == "av1_amf"
    assert "-hwaccel" not in cmd
    assert "libsvtav1" not in cmd
    assert "libaom-av1" not in cmd
    assert cmd[cmd.index("-quality") + 1] == "speed"


def test_copy_separates_fast_hardware_av1_from_archival_svt():
    assert CODEC_LABELS[VideoCodec.AV1] == "Fast hardware AV1"
    assert "best compression" in CODEC_LABELS[VideoCodec.SVT_AV1]
    assert "CPU" in CODEC_LABELS[VideoCodec.SVT_AV1]
    assert VideoCodec.AV1.display_name == "Fast hardware AV1"
    assert "best compression" in VideoCodec.SVT_AV1.display_name

    archival = get_step(VideoCodec.SVT_AV1, QualityRung.MAX)
    assert "best compression" in archival.hint
    assert "CPU" in archival.hint
    assert "fast hardware AV1" in archival.hint
    assert "Hardware stays off" in archival.hint
    assert archival.force_software is True

    fast = get_step(VideoCodec.AV1, QualityRung.QUICK)
    assert "Fast hardware AV1" in fast.hint
    assert "NVENC" in fast.hint
    assert "best compression" in fast.hint
    assert fast.force_software is False

    form = EncodeForm()
    form.set_codec(VideoCodec.SVT_AV1)
    assert "best compression" in form.hw_hint()
    assert "Fast hardware AV1" in form.hw_hint()
    assert form.effective_hw() is False
    form.set_codec(VideoCodec.AV1)
    form.use_hw_accel = True
    assert form.hw_hint().startswith("Fast hardware AV1")
    assert "av1_amf" in form.hw_hint()

    form.set_codec(VideoCodec.HEVC)
    hevc = form.ladder_choice()
    form.ladder_choice = lambda: EncoderChoice(  # type: ignore[method-assign]
        codec=hevc.codec,
        rung=hevc.rung,
        crf=hevc.crf,
        preset=hevc.preset,
        force_software=True,
        allow_hw_accel=False,
        hint=hevc.hint,
    )
    forced = form.hw_hint()
    assert "stays off" in forced
    assert "SVT-AV1" not in forced
    assert "Fast hardware AV1" not in forced
    assert "best compression" in PROFILE_FRIENDLY["Max / Archival"]

    status = describe_hw_status(
        HardwareProbe(
            found=(FoundHardware("AMF", "AMD Radeon RX 9070 XT"),),
            encoders={"av1": "av1_amf", "svt-av1": None},
        ),
        codec=VideoCodec.AV1,
        use_hw=True,
        force_software=False,
        action="This preset",
        software_name="libaom-av1",
    )
    assert status.badge == "AMF"
    assert status.using.startswith("Fast hardware AV1.")
    assert "AMD hardware (AMF)" in status.using
    assert status.tooltip == "av1_amf"
