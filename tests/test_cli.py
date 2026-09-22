import pytest

from cli import create_parser, get_profile, main, sort_input_files
from video_compressor.core.codecs import AudioCodec, VideoCodec
from video_compressor.core.profiles import ProfileManager


def test_cli_supports_utility_mode_without_inputs():
    parser = create_parser()
    args = parser.parse_args(["--list-codecs"])

    assert args.inputs == []
    assert args.list_codecs is True


def test_cli_profile_accepts_new_format_and_thread_options():
    parser = create_parser()
    args = parser.parse_args([
        "sample.mp4",
        "--codec",
        "vp9",
        "--container",
        "mkv",
        "--image-format",
        "avif",
        "--threads",
        "12",
        "--target-mode",
        "strict",
        "--resource-governor",
        "low",
    ])
    profile = get_profile(args)

    assert profile.video_codec.value == "vp9"
    assert profile.video_container == "mkv"
    assert profile.image_format == "avif"
    assert profile.threads == 12
    assert profile.target_size_mode == "strict"
    assert profile.resource_governor == "low"


def test_cli_profile_accepts_auto_target_mode():
    parser = create_parser()
    args = parser.parse_args([
        "sample.mp4",
        "--target-mode",
        "auto",
    ])
    profile = get_profile(args)

    assert profile.target_size_mode == "auto"


def test_cli_profile_accepts_exact_target_mode():
    parser = create_parser()
    args = parser.parse_args([
        "sample.mp4",
        "--target-mode",
        "exact",
    ])
    profile = get_profile(args)

    assert profile.target_size_mode == "exact"


def test_cli_profile_accepts_exact_controls():
    parser = create_parser()
    args = parser.parse_args([
        "sample.mp4",
        "--target-mode",
        "exact",
        "--exact-audio-policy",
        "drop",
        "--exact-two-pass",
    ])
    profile = get_profile(args)

    assert profile.target_size_mode == "exact"
    assert profile.exact_audio_policy == "drop"
    assert profile.exact_two_pass is True


@pytest.mark.parametrize(
    ("profile_flag", "codec", "container"),
    [
        ("fast", VideoCodec.H264, "mp4"),
        ("balanced", VideoCodec.HEVC, "mp4"),
        ("max", VideoCodec.SVT_AV1, "mkv"),
        ("youtube", VideoCodec.H264, "mp4"),
        ("mobile", VideoCodec.HEVC, "mp4"),
        ("streaming", VideoCodec.HEVC, "mp4"),
    ],
)
def test_cli_profile_flags_select_builtin_defaults(profile_flag, codec, container, tmp_path):
    parser = create_parser()
    args = parser.parse_args(["sample.mp4", "--profile", profile_flag])
    profile = get_profile(args, manager=ProfileManager(config_dir=tmp_path))

    assert profile.video_codec is codec
    assert profile.video_container == container
    if profile_flag == "youtube":
        assert profile.audio_codec is AudioCodec.AAC
        assert profile.crf == 23
    if profile_flag == "max":
        assert profile.use_hw_accel is False
        assert profile.preset == "6"


def test_cli_codec_override_retargets_incompatible_container_and_audio(tmp_path):
    parser = create_parser()
    args = parser.parse_args([
        "sample.mp4",
        "--profile",
        "youtube",
        "--codec",
        "vp9",
        "--audio-codec",
        "aac",
    ])
    profile = get_profile(args, manager=ProfileManager(config_dir=tmp_path))

    assert profile.video_codec is VideoCodec.VP9
    assert profile.video_container == "webm"
    assert profile.audio_codec is AudioCodec.OPUS


def test_cli_keeps_an_explicit_compatible_container(tmp_path):
    parser = create_parser()
    args = parser.parse_args([
        "sample.mp4",
        "--codec",
        "vp9",
        "--container",
        "mkv",
        "--audio-codec",
        "aac",
    ])
    profile = get_profile(args, manager=ProfileManager(config_dir=tmp_path))

    assert profile.video_container == "mkv"
    assert profile.audio_codec is AudioCodec.AAC


def test_cli_unknown_profile_is_rejected(tmp_path):
    parser = create_parser()
    args = parser.parse_args(["sample.mp4"])
    args.profile = "archive"

    with pytest.raises(ValueError, match="Unknown profile"):
        get_profile(args, manager=ProfileManager(config_dir=tmp_path))


def test_cli_resolution_and_hw_overrides(tmp_path):
    parser = create_parser()
    args = parser.parse_args([
        "sample.mp4",
        "--profile",
        "fast",
        "--crf",
        "18",
        "--preset",
        "slow",
        "--resolution",
        "720p",
        "--no-hw-accel",
    ])
    profile = get_profile(args, manager=ProfileManager(config_dir=tmp_path))

    assert profile.crf == 18
    assert profile.preset == "slow"
    assert profile.max_resolution == 720
    assert profile.use_hw_accel is False


def test_cli_dry_run_reports_compatible_extension(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("video_compressor.core.profiles.PROFILES_DIR", tmp_path / "profiles")
    sample = tmp_path / "clip.mp4"
    sample.write_bytes(b"0")

    code = main([
        str(sample),
        "--codec",
        "vp9",
        "--dry-run",
        "--quiet",
    ])
    output = capsys.readouterr().out

    assert code == 0
    assert "clip_compressed.webm" in output
    assert "vp9 in webm" in output


def test_cli_rejects_a_run_with_no_inputs(capsys):
    code = main([])
    assert code == 1
    assert "No input files" in capsys.readouterr().out


def test_cli_sorts_batch_largest_first(tmp_path):
    from video_compressor.core.compressor import VideoCompressor

    small = tmp_path / "small.mp4"
    large = tmp_path / "large.mp4"
    small.write_bytes(b"0" * 10)
    large.write_bytes(b"0" * 50)

    ordered = sort_input_files([small, large], "largest-first", VideoCompressor())

    assert ordered == [large, small]


def test_cli_accepts_compare_and_batch_flags():
    parser = create_parser()
    args = parser.parse_args([
        "sample.mp4",
        "--compare",
        "--compare-seconds",
        "12",
        "--batch-order",
        "largest-first",
    ])

    assert args.compare is True
    assert args.compare_seconds == 12
    assert args.batch_order == "largest-first"
