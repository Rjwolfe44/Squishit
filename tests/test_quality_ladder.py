"""Locked Quick / Balanced / Max encoder arguments.

The expected argv is hardcoded so a GUI, CLI, or Quick Compress edit cannot
silently drift from the ladder.
"""

import pytest

from cli import create_parser, get_profile
from video_compressor.core.codecs import CodecSettings, VideoCodec
from video_compressor.core.profiles import (
    QUICK_COMPRESS_ORDER,
    ProfileManager,
    build_quick_compress_profiles,
    normalize_quick_compress_name,
    profile_video_ffmpeg_args,
)
from video_compressor.core.quality_ladder import (
    ARCHIVAL_PROFILE_NAME,
    HW_VENDOR_PREFERENCE,
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


def test_quick_compress_profiles_use_the_hevc_lane():
    profiles = build_quick_compress_profiles()

    assert QUICK_COMPRESS_ORDER == ["Quick", "Balanced", "HEVC Max"]
    assert normalize_quick_compress_name("Lite") == "Quick"
    assert normalize_quick_compress_name("Max") == "HEVC Max"
    assert (
        profile_video_ffmpeg_args(profiles["Quick"])
        == EXPECTED_SOFTWARE_ARGS[(VideoCodec.HEVC, QualityRung.QUICK)]
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
