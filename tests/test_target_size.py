import pytest

from video_compressor.core.codecs import AudioCodec, VideoCodec, VideoContainer
from video_compressor.core.compressor import (
    VideoCompressor,
    VideoInfo,
    CompressionJob,
    SoftwareFallbackReason,
    TargetSizeSearchState,
)
from video_compressor.core.hardware import GPUInfo, GPUVendor, HardwareDetector, HardwareInfo
from video_compressor.core.profiles import (
    CompressionProfile,
    ProfileType,
    TargetSizeMode,
    build_quick_compress_profiles,
    build_target_size_plan,
    describe_target_size_plan,
    refine_target_size_bitrate,
    resolve_target_size_mode,
    resolve_requested_target_size_mb,
)


def test_target_size_plan_modes_change_tolerance_and_attempts():
    fast = build_target_size_plan(
        target_size_mb=100,
        duration_seconds=300,
        audio_bitrate=128_000,
        container="mp4",
        codec=VideoCodec.HEVC,
        mode="fast",
    )
    strict = build_target_size_plan(
        target_size_mb=100,
        duration_seconds=300,
        audio_bitrate=128_000,
        container="mp4",
        codec=VideoCodec.HEVC,
        mode="strict",
    )

    assert fast.mode == TargetSizeMode.FAST
    assert fast.max_attempts == 1
    assert fast.tolerance_ratio > strict.tolerance_ratio
    assert strict.max_attempts > fast.max_attempts


def test_exact_target_size_plan_uses_harsher_settings():
    exact = build_target_size_plan(
        target_size_mb=100,
        duration_seconds=300,
        audio_bitrate=128_000,
        container="mp4",
        codec=VideoCodec.HEVC,
        mode="exact",
    )
    strict = build_target_size_plan(
        target_size_mb=100,
        duration_seconds=300,
        audio_bitrate=128_000,
        container="mp4",
        codec=VideoCodec.HEVC,
        mode="strict",
    )

    assert exact.mode == TargetSizeMode.EXACT
    assert exact.max_attempts > strict.max_attempts
    assert exact.min_video_bitrate < strict.min_video_bitrate
    assert "pads exact bytes" in describe_target_size_plan(exact, 150_000_000).lower()


def test_refine_target_size_bitrate_moves_toward_target():
    plan = build_target_size_plan(
        target_size_mb=100,
        duration_seconds=600,
        audio_bitrate=128_000,
        container="mkv",
        codec=VideoCodec.HEVC,
        mode="balanced",
    )

    smaller = refine_target_size_bitrate(
        current_video_bitrate=2_000_000,
        actual_size_bytes=130_000_000,
        plan=plan,
    )
    larger = refine_target_size_bitrate(
        current_video_bitrate=2_000_000,
        actual_size_bytes=75_000_000,
        plan=plan,
    )

    assert smaller < 2_000_000
    assert larger > 2_000_000
    assert smaller >= plan.min_video_bitrate


def test_target_size_search_brackets_and_interpolates_bitrate(monkeypatch):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    plan = build_target_size_plan(
        target_size_mb=1,
        duration_seconds=30,
        audio_bitrate=64_000,
        container="mkv",
        codec=VideoCodec.HEVC,
        mode="exact",
    )
    search_state = TargetSizeSearchState()

    first_retry = compressor._choose_next_target_video_bitrate(
        attempted_bitrate=1_200_000,
        actual_size_bytes=1_350_000,
        target_plan=plan,
        search_state=search_state,
    )
    second_retry = compressor._choose_next_target_video_bitrate(
        attempted_bitrate=750_000,
        actual_size_bytes=840_000,
        target_plan=plan,
        search_state=search_state,
    )

    assert first_retry < 1_200_000
    assert search_state.upper_bitrate == 1_200_000
    assert search_state.lower_bitrate == 750_000
    assert 750_000 < second_retry < 1_200_000


def test_auto_target_size_mode_prefers_fast_for_short_mild_jobs():
    resolved = resolve_target_size_mode(
        "auto",
        target_size_mb=80,
        source_size_bytes=100_000_000,
        duration_seconds=120,
        codec=VideoCodec.HEVC,
        use_hw_accel=True,
        profile_type=ProfileType.FAST,
    )

    assert resolved == TargetSizeMode.FAST


def test_auto_target_size_mode_prefers_strict_for_aggressive_jobs():
    resolved = resolve_target_size_mode(
        "auto",
        target_size_mb=40,
        source_size_bytes=100_000_000,
        duration_seconds=1_800,
        codec=VideoCodec.HEVC,
        use_hw_accel=False,
        profile_type=ProfileType.BALANCED,
    )

    assert resolved == TargetSizeMode.STRICT


def test_auto_target_size_mode_escalates_to_exact_when_below_floor(monkeypatch, tmp_path):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = {"libx265", "libx264", "aac", "libopus"}
    media_info = VideoInfo(
        filepath=tmp_path / "clip.mp4",
        duration=180.0,
        size=155_700_000,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=6_000_000,
        audio_bitrate=128_000,
        total_bitrate=6_128_000,
        frame_count=10_800,
    )
    profile = CompressionProfile(
        name="Auto Target",
        profile_type=ProfileType.BALANCED,
        video_codec=VideoCodec.HEVC,
        audio_codec=AudioCodec.AAC,
        audio_bitrate=128_000,
        use_hw_accel=True,
        target_size_mb=3,
        target_size_mode="auto",
    )

    assert compressor._resolve_effective_target_mode(profile, media_info) == TargetSizeMode.EXACT

    prepared = compressor._prepare_target_size_profile(profile, media_info)

    assert prepared.target_size_mode == TargetSizeMode.EXACT.value


def test_target_size_plan_warns_when_requested_target_is_below_floor():
    plan = build_target_size_plan(
        target_size_mb=10,
        duration_seconds=1_200,
        audio_bitrate=128_000,
        container="mp4",
        codec=VideoCodec.HEVC,
        mode="strict",
    )

    assert plan.minimum_size_bytes > plan.target_size_bytes
    assert "estimated floor" in plan.warning.lower()
    assert "floor ~" in describe_target_size_plan(plan, 900_000_000).lower()


def test_resolve_requested_target_size_from_percent_uses_source_size():
    resolved = resolve_requested_target_size_mb(
        explicit_target_mb=None,
        reduction_percent=25,
        original_size_bytes=200_000_000,
    )

    assert resolved == 150


def test_auto_resource_governor_resolves_from_workload():
    detector = HardwareDetector()
    detector._info = HardwareInfo(
        os_name="Windows",
        os_version="11",
        cpu_name="Test CPU",
        cpu_cores=8,
        cpu_threads=16,
        total_ram_gb=32.0,
        recommended_threads=14,
        has_hw_encoder=True,
        preferred_hw_encoder="amd",
    )

    assert detector.resolve_resource_governor(
        governor="auto",
        n_parallel=1,
        codec="svt-av1",
        hw_encoder=None,
        frame_height=1080,
    ) == "max"
    assert detector.resolve_resource_governor(
        governor="auto",
        n_parallel=3,
        codec="hevc",
        hw_encoder="hevc_amf",
        frame_height=1080,
    ) == "low"
    assert detector.recommend_threads_for_job(
        n_parallel=1,
        governor="auto",
        codec="svt-av1",
        hw_encoder=None,
        frame_height=1080,
    ) > detector.recommend_threads_for_job(
        n_parallel=3,
        governor="auto",
        codec="hevc",
        hw_encoder="hevc_amf",
        frame_height=1080,
    )


def test_exact_target_profile_uses_software_when_no_hardware_encoder(monkeypatch, tmp_path):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = {"libx265", "libx264", "aac", "libopus"}
    media_info = VideoInfo(
        filepath=tmp_path / "clip.mp4",
        duration=600.0,
        size=400_000_000,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=4_000_000,
        audio_bitrate=192_000,
        total_bitrate=4_192_000,
        frame_count=36_000,
    )
    profile = CompressionProfile(
        name="Exact",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.HEVC,
        audio_codec=AudioCodec.AAC,
        audio_bitrate=192_000,
        use_hw_accel=True,
        target_size_mb=10,
        target_size_mode="exact",
    )

    prepared = compressor._prepare_target_size_profile(profile, media_info)

    assert prepared.use_hw_accel is False
    assert prepared.video_codec == VideoCodec.HEVC
    assert prepared.audio_codec == AudioCodec.OPUS
    assert prepared.audio_bitrate < profile.audio_bitrate
    assert prepared.frame_rate is not None and prepared.frame_rate < media_info.fps
    assert prepared.max_resolution is not None and prepared.max_resolution < media_info.height


def test_exact_two_pass_prefers_hevc_path(monkeypatch, tmp_path):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = {"libx265", "aac", "libopus"}
    media_info = VideoInfo(
        filepath=tmp_path / "clip.mp4",
        duration=600.0,
        size=400_000_000,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=4_000_000,
        audio_bitrate=192_000,
        total_bitrate=4_192_000,
        frame_count=36_000,
    )
    profile = CompressionProfile(
        name="Exact Two Pass",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.HEVC,
        audio_codec=AudioCodec.AAC,
        audio_bitrate=192_000,
        use_hw_accel=True,
        target_size_mb=10,
        target_size_mode="exact",
        exact_two_pass=True,
    )

    prepared = compressor._prepare_target_size_profile(profile, media_info)

    assert prepared.video_codec == VideoCodec.HEVC
    assert prepared.use_hw_accel is False


def test_exact_target_drop_audio_policy_can_remove_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    media_info = VideoInfo(
        filepath=tmp_path / "clip.mp4",
        duration=600.0,
        size=400_000_000,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=4_000_000,
        audio_bitrate=192_000,
        total_bitrate=4_192_000,
        frame_count=36_000,
    )
    profile = CompressionProfile(
        name="Exact Drop Audio",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.HEVC,
        audio_codec=AudioCodec.OPUS,
        audio_bitrate=8_000,
        use_hw_accel=False,
        target_size_mb=10,
        target_size_mode="exact",
        exact_audio_policy="drop",
    )

    change = compressor._apply_exact_target_fallback(profile, media_info)

    assert change == "audio removed"
    assert profile.disable_audio is True


def test_exact_target_retries_after_large_undershoot_before_padding(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0" * 5_000_000)
    output = tmp_path / "output.mp4"

    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = {"libx265", "aac", "libopus"}

    monkeypatch.setattr("video_compressor.core.compressor.detect_media_type", lambda _: "video")
    monkeypatch.setattr(
        compressor,
        "analyze_media",
        lambda _: VideoInfo(
            filepath=source,
            duration=20.0,
            size=5_000_000,
            width=1280,
            height=720,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
            video_bitrate=1_800_000,
            audio_bitrate=96_000,
            total_bitrate=1_896_000,
            frame_count=600,
        ),
    )
    monkeypatch.setattr(
        compressor.codec_manager,
        "get_compatible_container",
        lambda codec, container: VideoContainer.MKV,
    )

    bitrate_attempts = []

    def fake_build_video_ffmpeg_command(job, profile, target_plan=None, target_video_bitrate=None):
        bitrate_attempts.append(target_video_bitrate or (target_plan.video_bitrate if target_plan else None))
        job.encoder_name = "libx265"
        job.threads_used = 8
        return ["ffmpeg", "-i", str(job.input_file), str(job.output_file)]

    attempt_sizes = iter([800_000, 1_000_000])

    def fake_run_ffmpeg_process(
        cmd,
        *,
        job,
        job_id,
        media_info,
        start_time,
        progress_start=0.0,
        progress_span=99.0,
        track_output=True,
    ):
        job.output_file.write_bytes(b"0" * next(attempt_sizes))
        return 0, []

    monkeypatch.setattr(compressor, "_build_video_ffmpeg_command", fake_build_video_ffmpeg_command)
    monkeypatch.setattr(compressor, "_run_ffmpeg_process", fake_run_ffmpeg_process)

    profile = CompressionProfile(
        name="Exact Retry",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.HEVC,
        audio_codec=AudioCodec.AAC,
        audio_bitrate=96_000,
        target_size_mb=1,
        target_size_mode="exact",
        use_hw_accel=False,
    )

    result = compressor.compress(source, output, profile, job_id="exact-retry")

    assert result.success is True
    assert result.attempt_count == 2
    assert len(bitrate_attempts) == 2
    assert bitrate_attempts[1] > bitrate_attempts[0]
    assert result.compressed_size == 1_000_000


@pytest.mark.parametrize(
    ("codec", "encoder_name"),
    [
        (VideoCodec.H264, "h264_amf"),
        (VideoCodec.HEVC, "hevc_amf"),
        (VideoCodec.AV1, "av1_amf"),
    ],
)
def test_target_size_command_forces_bitrate_rate_control_for_all_amf_encoders(
    monkeypatch,
    tmp_path,
    codec,
    encoder_name,
):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    monkeypatch.setattr(compressor, "_select_hw_encoder", lambda selected_codec: encoder_name)

    profile = CompressionProfile(
        name="Strict Target",
        profile_type=ProfileType.CUSTOM,
        video_codec=codec,
        audio_codec=AudioCodec.AAC,
        audio_bitrate=128_000,
        use_hw_accel=True,
        target_size_mb=10,
        target_size_mode="strict",
    )
    input_file = tmp_path / "input.mp4"
    input_file.write_bytes(b"0")
    job = CompressionJob(
        id="job-1",
        input_file=input_file,
        output_file=tmp_path / "output.mp4",
        profile=profile,
        parallel_jobs=1,
    )
    job.video_info = VideoInfo(
        filepath=input_file,
        duration=34.0,
        size=148_450_000,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=30_000_000,
        audio_bitrate=128_000,
        total_bitrate=30_128_000,
        frame_count=2040,
    )

    cmd = compressor._build_video_ffmpeg_command(
        job,
        profile,
        target_video_bitrate=600_000,
    )

    assert encoder_name in cmd
    assert "-b:v" in cmd
    assert "600000" in cmd
    assert "-rc" in cmd
    rc_value = cmd[cmd.index("-rc") + 1]
    assert rc_value in {"cbr", "vbr_peak"}
    assert "cqp" not in cmd


def test_pad_output_to_target_size_hits_exact_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    output = tmp_path / "out.mkv"
    output.write_bytes(b"1234567890")

    padding = compressor._pad_output_to_target_size(output, 25)

    assert padding == 15
    assert output.stat().st_size == 25


def test_compress_skips_when_target_is_not_smaller(tmp_path, monkeypatch):
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0" * 2_000_000)
    output = tmp_path / "output.mp4"

    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    monkeypatch.setattr("video_compressor.core.compressor.detect_media_type", lambda _: "video")
    monkeypatch.setattr(
        compressor,
        "analyze_media",
        lambda _: VideoInfo(
            filepath=source,
            duration=120.0,
            size=2_000_000,
            width=1920,
            height=1080,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
            video_bitrate=900_000,
            audio_bitrate=128_000,
            total_bitrate=1_028_000,
            frame_count=3600,
        ),
    )
    monkeypatch.setattr(compressor, "_optimize_video_profile", lambda profile, media_info=None: profile)
    monkeypatch.setattr(
        compressor.codec_manager,
        "get_compatible_container",
        lambda codec, container: VideoContainer.MP4,
    )

    profile = CompressionProfile(
        name="Skip Test",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.HEVC,
        target_size_mb=3,
        target_size_mode="strict",
    )

    result = compressor.compress(source, output, profile, job_id="skip-test")

    assert result.success is True
    assert result.skipped is True
    assert result.output_file is None
    assert "not smaller" in result.note.lower()


def _encoder_name(cmd):
    return cmd[cmd.index("-c:v") + 1]


def _hw_target_compressor(monkeypatch):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    monkeypatch.setattr(VideoCompressor, "_get_thread_count", lambda *args, **kwargs: 4)

    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = {
        "libx264",
        "libx265",
        "h264_nvenc",
        "hevc_nvenc",
        "aac",
        "libopus",
    }
    compressor.hw_detector._info = HardwareInfo(
        os_name="Linux",
        os_version="test",
        cpu_name="cpu",
        cpu_cores=4,
        cpu_threads=8,
        total_ram_gb=16,
        preferred_hw_encoder="amd",
        has_hw_encoder=True,
        gpus=[
            GPUInfo(
                "AMD",
                GPUVendor.AMD,
                encoder_support={"h264": True, "hevc": True},
            ),
            GPUInfo(
                "NVIDIA",
                GPUVendor.NVIDIA,
                encoder_support={"h264": True, "hevc": True},
            ),
        ],
        recommended_threads=4,
    )
    return compressor


def _run_target_job(compressor, profile, source, output, outputs):
    """Encode with a stubbed FFmpeg and return every argv plus the result."""

    commands = []

    def fake_run(cmd, *, job, job_id, media_info, start_time, **kwargs):
        commands.append(list(cmd))
        encoder = _encoder_name(cmd)
        size = outputs(encoder)
        if size is None:
            return 1, [f"{encoder} failed"]
        job.output_file.parent.mkdir(parents=True, exist_ok=True)
        job.output_file.write_bytes(b"0" * size)
        return 0, []

    compressor._run_ffmpeg_process = fake_run
    compressor.analyze_media = lambda path: VideoInfo(
        filepath=path,
        duration=10.0,
        size=8_000_000,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=5_000_000,
        audio_bitrate=128_000,
        total_bitrate=5_128_000,
        frame_count=300,
    )
    result = compressor.compress(source, output, profile, job_id=output.stem)
    return commands, result


def test_exact_target_keeps_quick_lite_hardware_until_confirmed(monkeypatch, tmp_path):
    """Exact size tries H.264 hardware first and does not swap to software."""

    compressor = _hw_target_compressor(monkeypatch)
    monkeypatch.setattr(compressor, "_apply_exact_target_fallback", lambda profile, media: None)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    profile = CompressionProfile.from_dict(build_quick_compress_profiles()["Quick Lite"].to_dict())
    profile.target_size_mb = 1
    profile.target_size_mode = "exact"

    commands, result = _run_target_job(
        compressor,
        profile,
        source,
        tmp_path / "exact.mp4",
        lambda encoder: 2_000_000,
    )

    assert commands
    assert {_encoder_name(cmd) for cmd in commands} == {"h264_nvenc"}
    assert result.success is True
    assert result.software_fallback_required is True
    assert result.software_fallback_reason == SoftwareFallbackReason.SIZE_MISS.value
    assert "Confirm before retrying with libx264" in result.software_fallback_message
    assert "Confirm before retrying with libx264" in result.note
    assert result.encoder_name == "h264_nvenc"
    assert "libx264" not in result.encoder_name
    assert "libx265" not in " ".join(" ".join(cmd) for cmd in commands)


def test_exact_target_software_retry_runs_only_after_confirmation(monkeypatch, tmp_path):
    compressor = _hw_target_compressor(monkeypatch)
    monkeypatch.setattr(compressor, "_apply_exact_target_fallback", lambda profile, media: None)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    profile = CompressionProfile.from_dict(build_quick_compress_profiles()["Quick Lite"].to_dict())
    profile.target_size_mb = 1
    profile.target_size_mode = "exact"
    asked = []

    def confirm(request):
        asked.append(request)
        return False

    compressor.set_software_fallback_callback(confirm)
    commands, declined = _run_target_job(
        compressor,
        profile,
        source,
        tmp_path / "declined.mp4",
        lambda encoder: 2_000_000,
    )

    assert len(asked) == 1
    assert asked[0].reason is SoftwareFallbackReason.SIZE_MISS
    assert asked[0].hw_encoder == "h264_nvenc"
    assert asked[0].software_encoder == "libx264"
    assert {_encoder_name(cmd) for cmd in commands} == {"h264_nvenc"}
    assert declined.software_fallback_required is True

    compressor.set_software_fallback_callback(lambda _request: True)
    commands, confirmed = _run_target_job(
        compressor,
        profile,
        source,
        tmp_path / "confirmed.mp4",
        lambda encoder: 1_000_000 if encoder == "libx264" else 2_000_000,
    )

    assert _encoder_name(commands[0]) == "h264_nvenc"
    assert _encoder_name(commands[-1]) == "libx264"
    assert confirmed.success is True
    assert confirmed.software_fallback_required is False
    assert confirmed.encoder_name == "libx264"


def test_target_size_hardware_failure_does_not_silently_use_software(monkeypatch, tmp_path):
    compressor = _hw_target_compressor(monkeypatch)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    profile = CompressionProfile.from_dict(build_quick_compress_profiles()["HEVC Max"].to_dict())
    profile.target_size_mb = 1
    profile.target_size_mode = "fast"

    commands, failed = _run_target_job(
        compressor,
        profile,
        source,
        tmp_path / "failed.mkv",
        lambda encoder: None,
    )

    assert [_encoder_name(cmd) for cmd in commands] == ["hevc_nvenc"]
    assert failed.success is False
    assert failed.software_fallback_required is True
    assert failed.software_fallback_reason == SoftwareFallbackReason.ENCODE_FAILED.value
    assert "Confirm before retrying with libx265" in failed.error_message
    assert "libx265" not in " ".join(" ".join(cmd) for cmd in commands)

    compressor.set_software_fallback_callback(lambda _request: True)
    commands, confirmed = _run_target_job(
        compressor,
        profile,
        source,
        tmp_path / "failed-then-sw.mkv",
        lambda encoder: None if encoder == "hevc_nvenc" else 1_000_000,
    )

    assert [_encoder_name(cmd) for cmd in commands] == ["hevc_nvenc", "libx265"]
    assert confirmed.success is True
    assert confirmed.software_fallback_required is False
    assert confirmed.encoder_name == "libx265"


def test_exact_target_profile_keeps_hardware_when_an_encoder_exists(monkeypatch, tmp_path):
    compressor = _hw_target_compressor(monkeypatch)
    media_info = VideoInfo(
        filepath=tmp_path / "clip.mp4",
        duration=600.0,
        size=400_000_000,
        width=1920,
        height=1080,
        fps=60.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=4_000_000,
        audio_bitrate=192_000,
        total_bitrate=4_192_000,
        frame_count=36_000,
    )
    profile = CompressionProfile(
        name="Exact HW",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.HEVC,
        audio_codec=AudioCodec.AAC,
        audio_bitrate=192_000,
        use_hw_accel=True,
        target_size_mb=10,
        target_size_mode="exact",
    )

    prepared = compressor._prepare_target_size_profile(profile, media_info)

    assert prepared.use_hw_accel is True
    assert prepared.video_codec == VideoCodec.HEVC
    assert compressor._select_hw_encoder(prepared.video_codec) == "hevc_nvenc"
