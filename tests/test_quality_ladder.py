"""Locked Quick / Balanced / Max encoder arguments.

The expected argv is hardcoded so a GUI, CLI, or Quick Compress edit cannot
silently drift from the ladder.
"""

import pytest

from cli import create_parser, get_profile
from video_compressor.core.codecs import CodecSettings, VideoCodec
from video_compressor.core.compressor import CompressionJob, VideoCompressor, VideoInfo
from video_compressor.core.hardware import GPUInfo, GPUVendor, HardwareInfo
from video_compressor.core.profiles import (
    QUICK_COMPRESS_ORDER,
    CompressionProfile,
    ProfileManager,
    build_quick_compress_profiles,
    normalize_quick_compress_name,
    profile_video_ffmpeg_args,
    retarget_quick_compress_fallback,
)
from video_compressor.core.quality_ladder import (
    ARCHIVAL_PROFILE_NAME,
    HW_VENDOR_PREFERENCE,
    QUICK_COMPRESS_LITE_LABEL,
    QUICK_COMPRESS_MAX_LABEL,
    QualityRung,
    get_step,
    ladder_video_ffmpeg_args,
    ordered_hw_vendors,
    profile_picker_label,
    resolve_encoder_choice,
)

# Software argv for every public codec lane. VP9 and libaom express the
# preset as cpu-used; the step preset is asserted separately.
EXPECTED_SOFTWARE_ARGS = {
    (VideoCodec.HEVC, QualityRung.QUICK): [
        "-c:v",
        "libx265",
        "-crf",
        "24",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.HEVC, QualityRung.BALANCED): [
        "-c:v",
        "libx265",
        "-crf",
        "28",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.HEVC, QualityRung.MAX): [
        "-c:v",
        "libx265",
        "-crf",
        "30",
        "-preset",
        "slow",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.SVT_AV1, QualityRung.QUICK): [
        "-c:v",
        "libsvtav1",
        "-crf",
        "32",
        "-preset",
        "10",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.SVT_AV1, QualityRung.BALANCED): [
        "-c:v",
        "libsvtav1",
        "-crf",
        "35",
        "-preset",
        "8",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.SVT_AV1, QualityRung.MAX): [
        "-c:v",
        "libsvtav1",
        "-crf",
        "35",
        "-preset",
        "6",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.AV1, QualityRung.QUICK): [
        "-c:v",
        "libaom-av1",
        "-crf",
        "26",
        "-b:v",
        "0",
        "-usage",
        "good",
        "-cpu-used",
        "8",
        "-aq-mode",
        "1",
        "-lag-in-frames",
        "25",
        "-row-mt",
        "1",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.AV1, QualityRung.BALANCED): [
        "-c:v",
        "libaom-av1",
        "-crf",
        "30",
        "-b:v",
        "0",
        "-usage",
        "good",
        "-cpu-used",
        "6",
        "-aq-mode",
        "1",
        "-lag-in-frames",
        "25",
        "-row-mt",
        "1",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.AV1, QualityRung.MAX): [
        "-c:v",
        "libaom-av1",
        "-crf",
        "34",
        "-b:v",
        "0",
        "-usage",
        "good",
        "-cpu-used",
        "4",
        "-aq-mode",
        "1",
        "-lag-in-frames",
        "25",
        "-row-mt",
        "1",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.H264, QualityRung.QUICK): [
        "-c:v",
        "libx264",
        "-crf",
        "26",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.H264, QualityRung.BALANCED): [
        "-c:v",
        "libx264",
        "-crf",
        "28",
        "-preset",
        "medium",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.H264, QualityRung.MAX): [
        "-c:v",
        "libx264",
        "-crf",
        "32",
        "-preset",
        "slow",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.VP9, QualityRung.QUICK): [
        "-c:v",
        "libvpx-vp9",
        "-crf",
        "28",
        "-b:v",
        "0",
        "-cpu-used",
        "4",
        "-row-mt",
        "1",
        "-deadline",
        "good",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.VP9, QualityRung.BALANCED): [
        "-c:v",
        "libvpx-vp9",
        "-crf",
        "31",
        "-b:v",
        "0",
        "-cpu-used",
        "2",
        "-row-mt",
        "1",
        "-deadline",
        "good",
        "-pix_fmt",
        "yuv420p",
    ],
    (VideoCodec.VP9, QualityRung.MAX): [
        "-c:v",
        "libvpx-vp9",
        "-crf",
        "34",
        "-b:v",
        "0",
        "-cpu-used",
        "1",
        "-row-mt",
        "1",
        "-deadline",
        "good",
        "-pix_fmt",
        "yuv420p",
    ],
}

EXPECTED_PRESET = {
    (VideoCodec.HEVC, QualityRung.QUICK): "veryfast",
    (VideoCodec.HEVC, QualityRung.BALANCED): "medium",
    (VideoCodec.HEVC, QualityRung.MAX): "slow",
    (VideoCodec.SVT_AV1, QualityRung.QUICK): "10",
    (VideoCodec.SVT_AV1, QualityRung.BALANCED): "8",
    (VideoCodec.SVT_AV1, QualityRung.MAX): "6",
    (VideoCodec.AV1, QualityRung.QUICK): "8",
    (VideoCodec.AV1, QualityRung.BALANCED): "6",
    (VideoCodec.AV1, QualityRung.MAX): "4",
    (VideoCodec.H264, QualityRung.QUICK): "fast",
    (VideoCodec.H264, QualityRung.BALANCED): "medium",
    (VideoCodec.H264, QualityRung.MAX): "slow",
    (VideoCodec.VP9, QualityRung.QUICK): "veryfast",
    (VideoCodec.VP9, QualityRung.BALANCED): "medium",
    (VideoCodec.VP9, QualityRung.MAX): "slow",
}


@pytest.mark.parametrize("codec,rung", list(EXPECTED_SOFTWARE_ARGS))
def test_ladder_argv_crf_and_preset_are_locked(codec, rung):
    step = get_step(codec, rung)
    args = ladder_video_ffmpeg_args(codec, rung)

    assert step.crf == int(
        EXPECTED_SOFTWARE_ARGS[(codec, rung)][
            EXPECTED_SOFTWARE_ARGS[(codec, rung)].index("-crf") + 1
        ]
    )
    assert step.preset == EXPECTED_PRESET[(codec, rung)]
    assert args == EXPECTED_SOFTWARE_ARGS[(codec, rung)]
    assert args[args.index("-crf") + 1] == str(step.crf)


def test_hevc_rungs_move_from_fast_large_files_to_max_compression():
    quick = get_step(VideoCodec.HEVC, QualityRung.QUICK)
    balanced = get_step(VideoCodec.HEVC, QualityRung.BALANCED)
    maximum = get_step(VideoCodec.HEVC, QualityRung.MAX)

    assert quick.preset == "veryfast"
    assert balanced.preset == "medium"
    assert maximum.preset == "slow"
    assert quick.crf < balanced.crf < maximum.crf
    assert maximum.allow_hw_accel is True
    assert maximum.force_software is False


def test_archival_max_is_svt_av1_with_hardware_off():
    step = get_step(VideoCodec.SVT_AV1, QualityRung.MAX)
    args = ladder_video_ffmpeg_args(
        VideoCodec.SVT_AV1,
        QualityRung.MAX,
        hw_encoder="av1_nvenc",
    )

    assert step.crf == 35
    assert step.preset == "6"
    assert step.force_software is True
    assert step.allow_hw_accel is False
    assert args == EXPECTED_SOFTWARE_ARGS[(VideoCodec.SVT_AV1, QualityRung.MAX)]
    assert "libsvtav1" in args
    assert "av1_nvenc" not in args


@pytest.mark.parametrize(
    ("encoder", "expected"),
    [
        (
            "hevc_nvenc",
            [
                "-c:v",
                "hevc_nvenc",
                "-preset",
                "p7",
                "-cq",
                "30",
                "-b:v",
                "0",
                "-pix_fmt",
                "yuv420p",
            ],
        ),
        (
            "hevc_qsv",
            [
                "-c:v",
                "hevc_qsv",
                "-preset",
                "slow",
                "-global_quality",
                "30",
                "-pix_fmt",
                "yuv420p",
            ],
        ),
        (
            "hevc_amf",
            [
                "-c:v",
                "hevc_amf",
                "-usage",
                "transcoding",
                "-quality",
                "quality",
                "-rc",
                "cqp",
                "-qp_i",
                "30",
                "-qp_p",
                "32",
                "-pix_fmt",
                "yuv420p",
            ],
        ),
    ],
)
def test_hevc_max_hardware_cq_placeholders(encoder, expected):
    assert (
        ladder_video_ffmpeg_args(
            VideoCodec.HEVC,
            QualityRung.MAX,
            hw_encoder=encoder,
        )
        == expected
    )


def test_hw_vendor_preference_is_nvenc_then_qsv_then_amf():
    assert HW_VENDOR_PREFERENCE == ("nvidia", "intel", "amd")
    assert ordered_hw_vendors(["amd", "intel", "nvidia", "apple"]) == [
        "nvidia",
        "intel",
        "amd",
        "apple",
    ]


def test_dual_max_labels_stay_distinct():
    assert profile_picker_label(ARCHIVAL_PROFILE_NAME) == "Max / Archival"
    assert QUICK_COMPRESS_MAX_LABEL == "HEVC Max"
    assert "Archival" in profile_picker_label(ARCHIVAL_PROFILE_NAME)
    assert profile_picker_label(ARCHIVAL_PROFILE_NAME) != QUICK_COMPRESS_MAX_LABEL
    assert "HEVC" in get_step(VideoCodec.HEVC, QualityRung.MAX).hint
    assert "SVT-AV1" in get_step(VideoCodec.SVT_AV1, QualityRung.MAX).hint
    assert "Not Max / Archival" in get_step(VideoCodec.HEVC, QualityRung.MAX).hint
    assert (
        "Not Quick Compress Max" in get_step(VideoCodec.SVT_AV1, QualityRung.MAX).hint
    )


def test_builtin_profiles_match_the_ladder(tmp_path):
    manager = ProfileManager(config_dir=tmp_path)
    fast = manager.get_profile("Fast")
    balanced = manager.get_profile("Balanced")
    archival = manager.get_profile(ARCHIVAL_PROFILE_NAME)

    assert (
        profile_video_ffmpeg_args(fast)
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.H264, QualityRung.QUICK)]
    )
    assert (
        profile_video_ffmpeg_args(balanced)
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.HEVC, QualityRung.BALANCED)]
    )
    assert (
        profile_video_ffmpeg_args(archival)
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.SVT_AV1, QualityRung.MAX)]
    )
    assert archival.use_hw_accel is False
    assert archival.video_codec is VideoCodec.SVT_AV1
    assert "SVT-AV1" in archival.description
    assert "Not Quick Compress Max" in archival.description


def test_quick_compress_profiles_keep_lite_h264_and_max_hevc():
    profiles = build_quick_compress_profiles()

    assert QUICK_COMPRESS_ORDER == ["Quick Lite", "Balanced", "HEVC Max"]
    assert QUICK_COMPRESS_LITE_LABEL == "Quick Lite"
    assert normalize_quick_compress_name("Lite") == "Quick Lite"
    assert normalize_quick_compress_name("Quick") == "Quick Lite"
    assert normalize_quick_compress_name("Max") == "HEVC Max"
    assert profiles["Quick Lite"].video_codec is VideoCodec.H264
    assert profiles["Quick Lite"].use_hw_accel is True
    assert (
        profile_video_ffmpeg_args(profiles["Quick Lite"])
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.H264, QualityRung.QUICK)]
    )
    assert (
        profile_video_ffmpeg_args(profiles["Balanced"])
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.HEVC, QualityRung.BALANCED)]
    )
    assert (
        profile_video_ffmpeg_args(profiles["HEVC Max"])
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.HEVC, QualityRung.MAX)]
    )
    assert profiles["HEVC Max"].use_hw_accel is True
    assert profiles["HEVC Max"].video_codec is VideoCodec.HEVC
    assert "AV1" not in profiles["Quick Lite"].description


def test_gui_choice_matches_cli_ladder_args():
    choice = resolve_encoder_choice(VideoCodec.HEVC, QualityRung.MAX)
    settings = CodecSettings(
        video_codec=choice.codec,
        crf=choice.crf,
        preset=choice.preset,
        disable_audio=True,
    )
    args = settings.to_ffmpeg_args()
    if args[-1:] == ["-an"]:
        args = args[:-1]

    assert choice.crf == 30
    assert choice.preset == "slow"
    assert args == EXPECTED_SOFTWARE_ARGS[(VideoCodec.HEVC, QualityRung.MAX)]


def test_cli_help_names_both_max_settings_and_offers_av1():
    help_text = create_parser().format_help()

    assert "Max / Archival" in help_text
    assert "SVT-AV1" in help_text
    assert "Quick Compress Max" in help_text
    assert "--codec hevc" in help_text
    assert "svt-av1" in help_text
    assert "av1" in help_text


@pytest.mark.parametrize(
    ("argv", "codec", "rung", "hw"),
    [
        (["clip.mp4", "--profile", "max"], VideoCodec.SVT_AV1, QualityRung.MAX, False),
        (
            ["clip.mp4", "--profile", "max", "--codec", "hevc"],
            VideoCodec.HEVC,
            QualityRung.MAX,
            True,
        ),
        (
            ["clip.mp4", "--profile", "balanced"],
            VideoCodec.HEVC,
            QualityRung.BALANCED,
            True,
        ),
        (
            ["clip.mp4", "--profile", "balanced", "--codec", "svt-av1"],
            VideoCodec.SVT_AV1,
            QualityRung.BALANCED,
            False,
        ),
        (
            ["clip.mp4", "--profile", "fast", "--codec", "av1"],
            VideoCodec.AV1,
            QualityRung.QUICK,
            True,
        ),
        (["clip.mp4", "--profile", "fast"], VideoCodec.H264, QualityRung.QUICK, True),
        (
            ["clip.mp4", "--profile", "max", "--codec", "svt-av1"],
            VideoCodec.SVT_AV1,
            QualityRung.MAX,
            False,
        ),
        (
            ["clip.mp4", "--profile", "max", "--codec", "av1"],
            VideoCodec.AV1,
            QualityRung.MAX,
            True,
        ),
    ],
)
def test_cli_profile_emits_the_same_video_args_as_the_ladder(
    argv, codec, rung, hw, tmp_path
):
    parser = create_parser()
    profile = get_profile(
        parser.parse_args(argv), manager=ProfileManager(config_dir=tmp_path)
    )

    assert profile.video_codec is codec
    assert profile.crf == get_step(codec, rung).crf
    assert profile.preset == get_step(codec, rung).preset
    assert profile.use_hw_accel is hw
    assert profile_video_ffmpeg_args(profile) == EXPECTED_SOFTWARE_ARGS[(codec, rung)]


def test_cli_explicit_crf_and_preset_still_override_the_ladder(tmp_path):
    parser = create_parser()
    profile = get_profile(
        parser.parse_args(
            [
                "clip.mp4",
                "--profile",
                "max",
                "--codec",
                "hevc",
                "--crf",
                "18",
                "--preset",
                "medium",
                "--no-hw-accel",
            ]
        ),
        manager=ProfileManager(config_dir=tmp_path),
    )

    assert profile.video_codec is VideoCodec.HEVC
    assert profile.crf == 18
    assert profile.preset == "medium"
    assert profile.use_hw_accel is False


def test_cli_non_ladder_profile_keeps_its_own_crf(tmp_path):
    parser = create_parser()
    profile = get_profile(
        parser.parse_args(["clip.mp4", "--profile", "youtube", "--codec", "hevc"]),
        manager=ProfileManager(config_dir=tmp_path),
    )

    assert profile.video_codec is VideoCodec.HEVC
    assert profile.crf == 23
    assert profile.preset == "fast"


def test_x264_preset_names_do_not_replace_numeric_av1_presets():
    svt_named = resolve_encoder_choice(
        VideoCodec.SVT_AV1, QualityRung.MAX, preset_override="medium"
    )
    svt_slow = resolve_encoder_choice(
        VideoCodec.SVT_AV1, QualityRung.MAX, preset_override="slow"
    )
    svt_numeric = resolve_encoder_choice(
        VideoCodec.SVT_AV1, QualityRung.MAX, preset_override="4"
    )
    av1_named = resolve_encoder_choice(
        VideoCodec.AV1, QualityRung.BALANCED, preset_override="medium"
    )
    hevc_named = resolve_encoder_choice(
        VideoCodec.HEVC, QualityRung.MAX, preset_override="medium"
    )

    assert svt_named.preset == "6"
    assert svt_named.crf == 35
    assert svt_named.force_software is True
    assert svt_slow.preset == "6"
    assert svt_numeric.preset == "4"
    assert av1_named.preset == "6"
    assert av1_named.crf == 30
    assert hevc_named.preset == "medium"
    assert hevc_named.force_software is False


def test_quick_max_switch_keeps_archival_hardware_off(tmp_path):
    profiles = build_quick_compress_profiles()
    lite = profiles["Quick Lite"]
    hevc_max = profiles["HEVC Max"]
    archival = ProfileManager(config_dir=tmp_path).get_profile(ARCHIVAL_PROFILE_NAME)
    archival_choice = resolve_encoder_choice(
        VideoCodec.SVT_AV1, QualityRung.MAX, preset_override="slow"
    )
    hevc_choice = resolve_encoder_choice(VideoCodec.HEVC, QualityRung.MAX)

    assert (lite.crf, lite.preset, lite.use_hw_accel) == (26, "fast", True)
    assert lite.video_codec is VideoCodec.H264
    assert (hevc_max.crf, hevc_max.preset, hevc_max.use_hw_accel) == (30, "slow", True)
    assert hevc_choice.crf == 30
    assert hevc_choice.preset == "slow"
    assert hevc_choice.force_software is False
    assert archival.use_hw_accel is False
    assert archival.video_codec is VideoCodec.SVT_AV1
    assert archival_choice.crf == 35
    assert archival_choice.preset == "6"
    assert archival_choice.force_software is True


def test_hevc_missing_fallback_retargets_crf_and_preset():
    profiles = build_quick_compress_profiles()
    hevc_max = CompressionProfile.from_dict(profiles["HEVC Max"].to_dict())
    balanced = CompressionProfile.from_dict(profiles["Balanced"].to_dict())

    retarget_quick_compress_fallback(hevc_max, VideoCodec.H264, "HEVC Max")
    retarget_quick_compress_fallback(balanced, VideoCodec.SVT_AV1, "Balanced")

    assert hevc_max.video_codec is VideoCodec.H264
    assert hevc_max.crf == get_step(VideoCodec.H264, QualityRung.MAX).crf
    assert hevc_max.preset == "slow"
    assert hevc_max.crf != 30
    assert hevc_max.use_hw_accel is True
    assert (
        profile_video_ffmpeg_args(hevc_max)
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.H264, QualityRung.MAX)]
    )
    assert balanced.video_codec is VideoCodec.SVT_AV1
    assert balanced.crf == 35
    assert balanced.preset == "8"
    assert balanced.use_hw_accel is False
    assert (
        profile_video_ffmpeg_args(balanced)
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.SVT_AV1, QualityRung.BALANCED)]
    )


def test_preferred_hw_vendor_follows_nvenc_then_qsv_then_amf():
    """Detector order is NVIDIA, AMD, Intel. Preference is not that order."""

    detector_cls = __import__(
        "video_compressor.core.hardware", fromlist=["HardwareDetector"]
    ).HardwareDetector
    detector = detector_cls()
    has_hw, vendor = detector._check_hw_encoders(
        [
            GPUInfo("AMD", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True}),
            GPUInfo("Intel", GPUVendor.INTEL, encoder_support={"h264": True, "hevc": True}),
        ]
    )

    assert has_hw is True
    assert vendor == "intel"

    has_hw, vendor = detector._check_hw_encoders(
        [
            GPUInfo("AMD", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True}),
            GPUInfo("Intel", GPUVendor.INTEL, encoder_support={"h264": True, "hevc": True}),
            GPUInfo("NVIDIA", GPUVendor.NVIDIA, encoder_support={"h264": True, "hevc": True}),
        ]
    )
    assert vendor == "nvidia"


def test_select_hw_encoder_prefers_nvenc_then_qsv_then_amf(monkeypatch):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)

    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = {
        "h264_nvenc",
        "h264_qsv",
        "h264_amf",
        "libx264",
    }
    compressor.hw_detector._info = HardwareInfo(
        os_name="Linux",
        os_version="test",
        cpu_name="cpu",
        cpu_cores=4,
        cpu_threads=8,
        total_ram_gb=16,
        preferred_hw_encoder="amd",
        gpus=[
            GPUInfo(
                "AMD",
                GPUVendor.AMD,
                encoder_support={"h264": True, "hevc": True},
            ),
            GPUInfo(
                "Intel",
                GPUVendor.INTEL,
                encoder_support={"h264": True, "hevc": True},
            ),
            GPUInfo(
                "NVIDIA",
                GPUVendor.NVIDIA,
                encoder_support={"h264": True, "hevc": True},
            ),
        ],
    )

    assert compressor._select_hw_encoder(VideoCodec.H264) == "h264_nvenc"

    compressor.hw_detector.info.gpus[2].encoder_support["h264"] = False
    assert compressor._select_hw_encoder(VideoCodec.H264) == "h264_qsv"

    compressor.hw_detector.info.gpus[1].encoder_support["h264"] = False
    assert compressor._select_hw_encoder(VideoCodec.H264) == "h264_amf"

    compressor.codec_manager._available_encoders.discard("h264_amf")
    assert compressor._select_hw_encoder(VideoCodec.H264) is None


def _sample_video_info(input_file):
    return VideoInfo(
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


def test_max_lane_ffmpeg_commands_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    monkeypatch.setattr(VideoCompressor, "_get_thread_count", lambda *args, **kwargs: 4)

    compressor = VideoCompressor()
    input_file = tmp_path / "input.mp4"
    input_file.write_bytes(b"0")
    archival = ProfileManager(config_dir=tmp_path).get_profile(ARCHIVAL_PROFILE_NAME)
    archival_out = tmp_path / "archival.mkv"
    archival_job = CompressionJob(
        id="job-archival",
        input_file=input_file,
        output_file=archival_out,
        profile=archival,
        parallel_jobs=1,
    )
    archival_job.video_info = _sample_video_info(input_file)

    archival_cmd = compressor._build_video_ffmpeg_command(archival_job, archival)

    assert archival_cmd == [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(input_file),
        "-c:v",
        "libsvtav1",
        "-crf",
        "35",
        "-preset",
        "6",
        "-svtav1-params",
        "lp=4",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "libopus",
        "-b:a",
        "96000",
        "-threads",
        "4",
        str(archival_out),
    ]
    assert "nvenc" not in " ".join(archival_cmd)

    hevc_max = build_quick_compress_profiles()["HEVC Max"]
    monkeypatch.setattr(compressor, "_select_hw_encoder", lambda codec: "hevc_nvenc")
    hevc_out = tmp_path / "hevc-max.mkv"
    hevc_job = CompressionJob(
        id="job-hevc-max",
        input_file=input_file,
        output_file=hevc_out,
        profile=hevc_max,
        parallel_jobs=1,
    )
    hevc_job.video_info = _sample_video_info(input_file)

    hevc_cmd = compressor._build_video_ffmpeg_command(hevc_job, hevc_max)

    assert hevc_cmd == [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(input_file),
        "-c:v",
        "hevc_nvenc",
        "-preset",
        "p7",
        "-cq",
        "30",
        "-b:v",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "libopus",
        "-b:a",
        "96000",
        "-threads",
        "4",
        str(hevc_out),
    ]


_SOFTWARE_ENCODERS = {
    "libx264",
    "libx265",
    "libsvtav1",
    "aac",
    "libopus",
}


def _ladder_compressor(monkeypatch, *, gpus, preferred_hw, encoders):
    """Compressor whose encoder list and GPUs are fixed for argv checks."""

    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    monkeypatch.setattr(VideoCompressor, "_get_thread_count", lambda *args, **kwargs: 4)

    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = set(encoders)
    compressor.hw_detector._info = HardwareInfo(
        os_name="Linux",
        os_version="test",
        cpu_name="cpu",
        cpu_cores=4,
        cpu_threads=8,
        total_ram_gb=16,
        preferred_hw_encoder=preferred_hw,
        gpus=gpus,
        recommended_threads=4,
    )
    return compressor


def _compress_argv(compressor, profile, source, output):
    """Run compress() with FFmpeg stubbed and return the argv it would execute."""

    captured = []

    def fake_run(cmd, *, job, job_id, media_info, start_time, **kwargs):
        captured.append(list(cmd))
        job.output_file.parent.mkdir(parents=True, exist_ok=True)
        job.output_file.write_bytes(b"encoded")
        return 0, []

    compressor._run_ffmpeg_process = fake_run
    compressor.analyze_media = lambda path: _sample_video_info(path)
    result = compressor.compress(source, output, profile, job_id=output.stem)
    assert result.success, result.error_message or result.note
    assert result.attempt_count == 1
    assert len(captured) == 1
    return captured[0], result


def _encoder(cmd):
    return cmd[cmd.index("-c:v") + 1]


def _flag(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_compress_keeps_quick_lite_h264_and_distinct_maxes(monkeypatch, tmp_path):
    """Quick Lite stays H.264, and the two Max lanes do not share one argv.

    No GPU is advertised, so hardware-on profiles fall through to software.
    Archival stays on SVT-AV1 with hardware off.
    """

    compressor = _ladder_compressor(
        monkeypatch,
        gpus=[],
        preferred_hw=None,
        encoders=_SOFTWARE_ENCODERS,
    )
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    profiles = build_quick_compress_profiles()
    archival = ProfileManager(config_dir=tmp_path / "profiles").get_profile(
        ARCHIVAL_PROFILE_NAME
    )

    lite_out = tmp_path / "lite.mp4"
    hevc_out = tmp_path / "hevc-max.mkv"
    archival_out = tmp_path / "archival.mkv"
    lite_cmd, lite_result = _compress_argv(
        compressor, profiles["Quick Lite"], source, lite_out
    )
    hevc_cmd, hevc_result = _compress_argv(
        compressor, profiles["HEVC Max"], source, hevc_out
    )
    archival_cmd, archival_result = _compress_argv(
        compressor, archival, source, archival_out
    )

    assert lite_result.video_codec == "h264"
    assert lite_result.encoder_name == "libx264"
    assert _encoder(lite_cmd) == "libx264"
    assert "libx265" not in lite_cmd
    assert "hevc" not in " ".join(lite_cmd)
    assert lite_cmd == [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(source),
        "-c:v",
        "libx264",
        "-crf",
        "26",
        "-preset",
        "fast",
        "-threads",
        "4",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128000",
        "-movflags",
        "+faststart",
        str(lite_out),
    ]

    assert hevc_result.video_codec == "hevc"
    assert hevc_result.encoder_name == "libx265"
    assert hevc_cmd == [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(source),
        "-c:v",
        "libx265",
        "-crf",
        "30",
        "-preset",
        "slow",
        "-threads",
        "4",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "libopus",
        "-b:a",
        "96000",
        str(hevc_out),
    ]

    assert archival_result.video_codec == "svt-av1"
    assert archival_result.encoder_name == "libsvtav1"
    assert archival.use_hw_accel is False
    assert archival_cmd == [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-i",
        str(source),
        "-c:v",
        "libsvtav1",
        "-crf",
        "35",
        "-preset",
        "6",
        "-svtav1-params",
        "lp=4",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "libopus",
        "-b:a",
        "96000",
        "-threads",
        "4",
        str(archival_out),
    ]
    assert not any(
        token in " ".join(archival_cmd)
        for token in ("nvenc", "qsv", "amf", "videotoolbox")
    )
    assert hevc_cmd != archival_cmd
    assert _encoder(hevc_cmd) != _encoder(archival_cmd)
    assert (_flag(hevc_cmd, "-crf"), _flag(hevc_cmd, "-preset")) != (
        _flag(archival_cmd, "-crf"),
        _flag(archival_cmd, "-preset"),
    )


def test_compress_hardware_keeps_lite_on_h264_and_maxes_apart(monkeypatch, tmp_path):
    """With NVENC available, Quick Lite still is not HEVC, and Archival stays software."""

    compressor = _ladder_compressor(
        monkeypatch,
        gpus=[
            GPUInfo(
                "NVIDIA",
                GPUVendor.NVIDIA,
                encoder_support={"h264": True, "hevc": True},
            )
        ],
        preferred_hw="nvidia",
        encoders=_SOFTWARE_ENCODERS | {"h264_nvenc", "hevc_nvenc"},
    )
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    profiles = build_quick_compress_profiles()
    archival = ProfileManager(config_dir=tmp_path / "profiles").get_profile(
        ARCHIVAL_PROFILE_NAME
    )

    lite_cmd, lite_result = _compress_argv(
        compressor, profiles["Quick Lite"], source, tmp_path / "lite.mp4"
    )
    hevc_cmd, hevc_result = _compress_argv(
        compressor, profiles["HEVC Max"], source, tmp_path / "hevc-max.mkv"
    )
    archival_cmd, archival_result = _compress_argv(
        compressor, archival, source, tmp_path / "archival.mkv"
    )

    assert lite_result.encoder_name == "h264_nvenc"
    assert _encoder(lite_cmd) == "h264_nvenc"
    assert "libx265" not in lite_cmd
    assert "hevc_nvenc" not in lite_cmd
    assert _flag(lite_cmd, "-preset") == "p3"
    assert _flag(lite_cmd, "-cq") == "26"

    assert hevc_result.encoder_name == "hevc_nvenc"
    assert _encoder(hevc_cmd) == "hevc_nvenc"
    assert _flag(hevc_cmd, "-preset") == "p7"
    assert _flag(hevc_cmd, "-cq") == "30"

    assert archival_result.encoder_name == "libsvtav1"
    assert _encoder(archival_cmd) == "libsvtav1"
    assert _flag(archival_cmd, "-crf") == "35"
    assert _flag(archival_cmd, "-preset") == "6"
    assert not any(
        token in " ".join(archival_cmd)
        for token in ("nvenc", "qsv", "amf", "videotoolbox")
    )
    assert hevc_cmd != archival_cmd
    assert lite_cmd != hevc_cmd


def _support(gpu, codec_key, enabled):
    gpu.encoder_support[codec_key] = enabled


def test_compress_argv_follows_nvenc_then_qsv_then_amf(monkeypatch, tmp_path):
    """Vendor choice is NVENC, then QSV, then AMF, even if detection preferred AMD.

    Quick Lite stays on the H.264 encoder for that vendor. Quick Max stays on
    the HEVC encoder. Max / Archival stays on libsvtav1 with hardware off.
    """

    gpus = [
        GPUInfo("AMD", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True}),
        GPUInfo("Intel", GPUVendor.INTEL, encoder_support={"h264": True, "hevc": True}),
        GPUInfo("NVIDIA", GPUVendor.NVIDIA, encoder_support={"h264": True, "hevc": True}),
    ]
    compressor = _ladder_compressor(
        monkeypatch,
        gpus=gpus,
        preferred_hw="amd",
        encoders=_SOFTWARE_ENCODERS
        | {
            "h264_nvenc",
            "h264_qsv",
            "h264_amf",
            "hevc_nvenc",
            "hevc_qsv",
            "hevc_amf",
        },
    )
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    profiles = build_quick_compress_profiles()
    archival = ProfileManager(config_dir=tmp_path / "profiles").get_profile(
        ARCHIVAL_PROFILE_NAME
    )

    def encode(profile, name):
        cmd, result = _compress_argv(compressor, profile, source, tmp_path / name)
        return cmd, result

    lite_cmd, _lite = encode(profiles["Quick Lite"], "lite-nv.mp4")
    hevc_cmd, _hevc = encode(profiles["HEVC Max"], "hevc-nv.mkv")
    archival_cmd, archival_result = encode(archival, "archival-nv.mkv")

    assert _encoder(lite_cmd) == "h264_nvenc"
    assert _flag(lite_cmd, "-preset") == "p3"
    assert _flag(lite_cmd, "-cq") == "26"
    assert _encoder(hevc_cmd) == "hevc_nvenc"
    assert _flag(hevc_cmd, "-preset") == "p7"
    assert _flag(hevc_cmd, "-cq") == "30"
    assert archival_result.encoder_name == "libsvtav1"
    assert _encoder(archival_cmd) == "libsvtav1"
    assert not any(
        token in " ".join(archival_cmd) for token in ("nvenc", "qsv", "amf", "videotoolbox")
    )

    for gpu in gpus:
        if gpu.vendor is GPUVendor.NVIDIA:
            _support(gpu, "h264", False)
            _support(gpu, "hevc", False)

    lite_cmd, _lite = encode(profiles["Quick Lite"], "lite-qsv.mp4")
    hevc_cmd, _hevc = encode(profiles["HEVC Max"], "hevc-qsv.mkv")
    assert _encoder(lite_cmd) == "h264_qsv"
    assert _flag(lite_cmd, "-preset") == "veryfast"
    assert _flag(lite_cmd, "-global_quality") == "26"
    assert _encoder(hevc_cmd) == "hevc_qsv"
    assert _flag(hevc_cmd, "-preset") == "slow"
    assert _flag(hevc_cmd, "-global_quality") == "30"
    assert _encoder(encode(archival, "archival-qsv.mkv")[0]) == "libsvtav1"

    for gpu in gpus:
        if gpu.vendor is GPUVendor.INTEL:
            _support(gpu, "h264", False)
            _support(gpu, "hevc", False)

    lite_cmd, _lite = encode(profiles["Quick Lite"], "lite-amf.mp4")
    hevc_cmd, _hevc = encode(profiles["HEVC Max"], "hevc-amf.mkv")
    assert _encoder(lite_cmd) == "h264_amf"
    assert _flag(lite_cmd, "-quality") == "speed"
    assert _encoder(hevc_cmd) == "hevc_amf"
    assert _flag(hevc_cmd, "-quality") == "quality"
    assert _encoder(encode(archival, "archival-amf.mkv")[0]) == "libsvtav1"

    for gpu in gpus:
        if gpu.vendor is GPUVendor.AMD:
            _support(gpu, "h264", False)
            _support(gpu, "hevc", False)

    lite_cmd, lite_result = encode(profiles["Quick Lite"], "lite-sw.mp4")
    hevc_cmd, hevc_result = encode(profiles["HEVC Max"], "hevc-sw.mkv")
    archival_cmd, archival_result = encode(archival, "archival-sw.mkv")
    assert lite_result.encoder_name == "libx264"
    assert _encoder(lite_cmd) == "libx264"
    assert _flag(lite_cmd, "-preset") == "fast"
    assert hevc_result.encoder_name == "libx265"
    assert _encoder(hevc_cmd) == "libx265"
    assert _flag(hevc_cmd, "-preset") == "slow"
    assert archival_result.encoder_name == "libsvtav1"
    assert _encoder(archival_cmd) == "libsvtav1"
