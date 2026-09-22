import pytest

from video_compressor.core.codecs import (
    AudioCodec,
    CodecManager,
    CodecSettings,
    ENCODER_REGISTRY,
    RateControl,
    VideoCodec,
    VideoContainer,
)
from video_compressor.core.compressor import CompressionJob, VideoCompressor, VideoInfo
from video_compressor.core.profiles import CompressionProfile, ProfileType


def test_container_compatibility_matrix_matches_expected_pairs():
    manager = CodecManager()

    assert manager.is_video_container_supported(VideoCodec.H264, VideoContainer.MP4)
    assert manager.is_video_container_supported(VideoCodec.HEVC, VideoContainer.MKV)
    assert manager.is_video_container_supported(VideoCodec.VP9, VideoContainer.WEBM)
    assert not manager.is_video_container_supported(VideoCodec.VP9, VideoContainer.AVI)
    assert not manager.is_video_container_supported(VideoCodec.HEVC, VideoContainer.WEBM)


def test_default_container_falls_back_to_codec_safe_option():
    manager = CodecManager()

    assert manager.get_compatible_container(VideoCodec.H264, "avi") == VideoContainer.AVI
    assert manager.get_compatible_container(VideoCodec.HEVC, "webm") == VideoContainer.MP4
    assert manager.get_compatible_container(VideoCodec.VP9, "mov") == VideoContainer.WEBM


def test_supported_codecs_include_av1_when_encoders_available():
    manager = CodecManager()
    manager._available_encoders = {
        "libx264",
        "libx265",
        "libvpx-vp9",
        "libaom-av1",
        "libsvtav1",
    }

    supported = manager.get_supported_codecs()
    assert VideoCodec.H264 in supported
    assert VideoCodec.HEVC in supported
    assert VideoCodec.VP9 in supported
    assert VideoCodec.SVT_AV1 in supported
    assert VideoCodec.AV1 in supported


def test_target_size_prefers_bitrate_rate_control_for_all_encoders(monkeypatch):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()

    for encoder_name, encoder_info in ENCODER_REGISTRY.items():
        selected = compressor._select_target_rate_control(encoder_name)
        assert selected in encoder_info["rate_controls"]
        assert selected in {RateControl.CBR, RateControl.ABR, RateControl.VBR}


@pytest.mark.parametrize("encoder_name", sorted(ENCODER_REGISTRY.keys()))
def test_bitrate_driven_codec_settings_emit_bitrate_args_for_all_encoders(encoder_name):
    encoder_info = ENCODER_REGISTRY[encoder_name]
    supported = encoder_info["rate_controls"]
    preferred = next(
        rate_control
        for rate_control in (RateControl.VBR, RateControl.ABR, RateControl.CBR)
        if rate_control in supported
    )

    settings = CodecSettings(
        video_codec=encoder_info["codec"],
        audio_codec=AudioCodec.OPUS,
        video_bitrate=600_000,
        audio_bitrate=128_000,
        rate_control=preferred,
        preset="medium",
        threads=4,
    )
    args = settings.to_ffmpeg_args(
        hw_encoder=encoder_name if encoder_info["type"] == "hardware" else None,
    )

    assert "-b:v" in args
    if preferred in {RateControl.CBR, RateControl.VBR}:
        assert args[args.index("-b:v") + 1] in {"600000", "600k"}


def test_libx265_command_clamps_threads_and_avoids_duplicate_thread_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    profile = CompressionProfile(
        name="HEVC Threads",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.HEVC,
        audio_codec=AudioCodec.OPUS,
        audio_bitrate=128_000,
        threads=32,
        use_hw_accel=False,
    )
    input_file = tmp_path / "input.mp4"
    input_file.write_bytes(b"0")
    job = CompressionJob(
        id="job-threads",
        input_file=input_file,
        output_file=tmp_path / "output.mkv",
        profile=profile,
        parallel_jobs=1,
    )
    job.video_info = VideoInfo(
        filepath=input_file,
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

    cmd = compressor._build_video_ffmpeg_command(job, profile)

    assert cmd.count("-threads") == 1
    assert cmd[cmd.index("-threads") + 1] == "16"
    assert job.threads_used == 16
