from cli import create_parser, get_profile


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
