"""CLI tests for --clip / --join (clip plans, job expansion, dry run, error handling)."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import attach_clip_values, create_parser, get_clip_plan, main
from video_compressor.core.clips import MAX_JOIN_CLIPS, Clip, ClipMode, ClipPlan
from video_compressor.core.compressor import CompressionResult, VideoCompressor, VideoInfo


def _plan(*argv: str) -> ClipPlan:
    args = create_parser().parse_args(["movie.mp4", *argv])
    plan = get_clip_plan(args)
    assert plan is not None
    return plan


# ── Fake compressor wiring ───────────────────────────────────────────────────


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake analyze/compress so no ffmpeg is needed; records every call."""
    monkeypatch.setattr("video_compressor.core.profiles.PROFILES_DIR", tmp_path / "profiles")
    rec = SimpleNamespace(
        calls=[],
        analyzed=[],
        fail_outputs=set(),
        duration=120.0,
        readable=True,
        dir=tmp_path,
    )

    def fake_analyze(self, filepath):
        rec.analyzed.append(Path(filepath))
        if not rec.readable:
            return None
        return VideoInfo(
            filepath=Path(filepath),
            duration=rec.duration,
            size=120_000_000,
            width=1920,
            height=1080,
            fps=30.0,
            video_codec="h264",
            audio_codec="aac",
            frame_count=int(rec.duration * 30),
        )

    def fake_compress(self, input_file, output_file, profile, job_id=None, parallel_jobs=1, **kwargs):
        rec.calls.append(
            SimpleNamespace(input=Path(input_file), output=Path(output_file), kwargs=kwargs)
        )
        ok = Path(output_file).name not in rec.fail_outputs
        return CompressionResult(
            success=ok,
            input_file=Path(input_file),
            output_file=Path(output_file),
            original_size=10_000_000,
            compressed_size=4_000_000,
            error_message="" if ok else "encoder exploded",
        )

    monkeypatch.setattr(VideoCompressor, "analyze_media", fake_analyze)
    monkeypatch.setattr(VideoCompressor, "compress", fake_compress)
    return rec


def _video(tmp_path, name="movie.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"0")
    return path


def _names(rec):
    return [call.output.name for call in rec.calls]


# ── Parsing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "start", "end"),
    [
        ("5-10", 5.0, 10.0),
        ("1:30-2:45.5", 90.0, 165.5),
        ("0:01:00-0:02:00", 60.0, 120.0),
        ("90-1:45", 90.0, 105.0),
        ("-30", 0.0, 30.0),  # open start
        ("1:00-", 60.0, math.inf),  # open end
    ],
)
def test_clip_flag_accepts_seconds_timecodes_and_open_ends(text, start, end):
    plan = _plan("--clip", text)

    assert plan.mode is ClipMode.SEPARATE
    assert plan.count == 1
    assert plan.clips[0].start == start
    assert plan.clips[0].end == end


def test_clip_flag_is_repeatable_and_keeps_order():
    plan = _plan("--clip", "20-30", "--clip", "5-10", "--clip", "1:00-")

    assert [(c.start, c.end) for c in plan.clips] == [(20, 30), (5, 10), (60, math.inf)]
    assert plan.mode is ClipMode.SEPARATE


def test_join_flag_selects_join_mode():
    plan = _plan("--clip", "1-5", "--clip", "10-15", "--join")

    assert plan.mode is ClipMode.JOIN
    assert plan.count == 2


def test_no_clip_means_no_plan():
    args = create_parser().parse_args(["movie.mp4"])

    assert args.clip is None
    assert args.join is False
    assert get_clip_plan(args) is None


def test_attach_clip_values_lets_an_open_start_with_a_colon_through():
    assert attach_clip_values(["a.mp4", "--clip", "-1:30", "--join"]) == [
        "a.mp4",
        "--clip=-1:30",
        "--join",
    ]
    # Plain values and anything after "--" are left alone.
    assert attach_clip_values(["--clip", "5-10", "--", "--clip", "-3"]) == [
        "--clip",
        "5-10",
        "--",
        "--clip",
        "-3",
    ]


def test_open_start_with_colon_works_from_main(env, capsys):
    movie = _video(env.dir)

    assert main([str(movie), "--clip", "-1:30", "--quiet"]) == 0

    clip = env.calls[0].kwargs["clips"].clips[0]
    assert (clip.start, clip.end) == (0.0, 90.0)


def test_help_documents_clip_and_join():
    help_text = create_parser().format_help()

    assert "--clip START-END" in help_text
    assert "--join" in help_text
    assert "--clip 1:30-2:45 --clip 5:00-5:30" in help_text
    assert "--join" in help_text.split("Examples:")[1]


# ── Argument errors ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    ["abc", "10-5", "5-abc", "abc-10", "5-10-20", "7-7"],
)
def test_bad_clip_value_is_a_clear_one_line_error(bad, env, capsys):
    movie = _video(env.dir)

    code = main([str(movie), f"--clip={bad}"])
    lines = capsys.readouterr().out.strip().splitlines()

    assert code == 1
    assert len(lines) == 1
    assert lines[0].startswith("Error: Invalid --clip value")
    assert env.calls == []


def test_join_without_two_clips_is_an_error(env, capsys):
    movie = _video(env.dir)

    assert main([str(movie), "--join"]) == 1
    assert "--join needs at least two --clip" in capsys.readouterr().out

    assert main([str(movie), "--clip", "1-5", "--join"]) == 1
    assert "--join needs at least two --clip" in capsys.readouterr().out
    assert env.calls == []


def test_join_is_capped_at_max_join_clips(env, capsys):
    movie = _video(env.dir)
    ok = [x for i in range(MAX_JOIN_CLIPS) for x in ("--clip", f"{i * 2}-{i * 2 + 1}")]
    too_many = ok + ["--clip", "100-101"]

    assert get_clip_plan(create_parser().parse_args([str(movie), *ok, "--join"])).count == MAX_JOIN_CLIPS

    assert main([str(movie), *too_many, "--join"]) == 1
    assert f"at most {MAX_JOIN_CLIPS}" in capsys.readouterr().out
    assert env.calls == []


# ── Job expansion ────────────────────────────────────────────────────────────


def test_separate_clips_make_one_encode_per_clip(env, capsys):
    movie = _video(env.dir)

    code = main([str(movie), "--clip", "5-10", "--clip", "20-25", "--clip", "60-", "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert _names(env) == [
        "movie_compressed_clip01.mp4",
        "movie_compressed_clip02.mp4",
        "movie_compressed_clip03.mp4",
    ]
    plans = [call.kwargs["clips"] for call in env.calls]
    assert [(p.count, p.mode) for p in plans] == [(1, ClipMode.SEPARATE)] * 3
    # The open end is resolved against the 120 s source before encoding.
    assert [(p.clips[0].start, p.clips[0].end) for p in plans] == [(5, 10), (20, 25), (60, 120)]
    assert all(call.kwargs.keys() == {"clips"} for call in env.calls)
    # Reporting: each clip is announced, totals count every output.
    assert "Clip 2 of 3" in out
    assert "Clips: 3 (saved as separate files)" in out
    assert "movie.mp4: 3 of 3 clips encoded" in out
    assert "Source files: 1  ·  Outputs: 3" in out
    assert "Successful: 3" in out


def test_join_makes_one_encode_with_the_whole_plan(env, capsys):
    movie = _video(env.dir)

    code = main([str(movie), "--clip", "5-10", "--clip", "20-25", "--clip", "30-40", "--join", "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert _names(env) == ["movie_compressed.mp4"]
    plan = env.calls[0].kwargs["clips"]
    assert plan.mode is ClipMode.JOIN
    assert [(c.start, c.end) for c in plan.clips] == [(5, 10), (20, 25), (30, 40)]
    assert "joining 3 clips" in out
    assert "Successful: 1" in out


def test_single_clip_trims_into_the_normal_output_name(env):
    movie = _video(env.dir)

    assert main([str(movie), "--clip", "10-20", "--quiet"]) == 0

    assert _names(env) == ["movie_compressed.mp4"]
    plan = env.calls[0].kwargs["clips"]
    assert (plan.count, plan.clips[0].start, plan.clips[0].end) == (1, 10, 20)


def test_no_clip_leaves_compress_untouched(env):
    movie = _video(env.dir)

    assert main([str(movie), "--quiet"]) == 0

    assert _names(env) == ["movie_compressed.mp4"]
    assert "clips" not in env.calls[0].kwargs
    assert env.calls[0].kwargs == {}
    assert env.analyzed == []  # no extra analysis on the plain path


def test_no_clip_still_works_with_a_fixed_signature_compressor(monkeypatch, env):
    movie = _video(env.dir)
    seen = []

    def old_style(self, input_file, output_file, profile, job_id=None, parallel_jobs=1):
        seen.append(Path(output_file).name)
        return CompressionResult(
            success=True,
            input_file=Path(input_file),
            output_file=Path(output_file),
            original_size=10,
            compressed_size=5,
        )

    monkeypatch.setattr(VideoCompressor, "compress", old_style)

    assert main([str(movie), "--quiet"]) == 0
    assert seen == ["movie_compressed.mp4"]


def test_clips_cover_whole_file_runs_as_a_plain_encode(env, capsys):
    movie = _video(env.dir)

    assert main([str(movie), "--clip", "0-120", "--quiet"]) == 0

    assert _names(env) == ["movie_compressed.mp4"]
    assert "clips" not in env.calls[0].kwargs
    assert "covers all of movie.mp4" in capsys.readouterr().out


def test_clip_past_the_end_is_cut_and_reported(env, capsys):
    movie = _video(env.dir)

    assert main([str(movie), "--clip", "100-200", "--quiet"]) == 0
    out = capsys.readouterr().out

    clip = env.calls[0].kwargs["clips"].clips[0]
    assert (clip.start, clip.end) == (100, 120)
    assert "Warning: movie.mp4: clip 1" in out
    assert "cut at 02:00.000" in out


def test_clip_outside_the_video_is_dropped_with_a_warning(env, capsys):
    movie = _video(env.dir)

    code = main([str(movie), "--clip", "5-10", "--clip", "500-600", "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert _names(env) == ["movie_compressed.mp4"]  # one clip left -> plain name
    assert (env.calls[0].kwargs["clips"].clips[0].start, env.calls[0].kwargs["clips"].clips[0].end) == (5, 10)
    assert "clip 2 (08:20.000 - 10:00.000)" in out
    assert "outside the video" in out


def test_every_clip_outside_the_video_fails_without_encoding(env, capsys):
    movie = _video(env.dir)

    code = main([str(movie), "--clip", "500-600", "--quiet"])
    out = capsys.readouterr().out

    assert code == 1
    assert env.calls == []
    assert "none of the --clip ranges fall inside this video" in out


def test_unreadable_video_fails_cleanly(env, capsys):
    env.readable = False
    movie = _video(env.dir)

    code = main([str(movie), "--clip", "5-10", "--quiet"])

    assert code == 1
    assert env.calls == []
    assert "Could not analyze movie.mp4" in capsys.readouterr().out


def test_failed_clip_sets_a_non_zero_exit_code_and_is_named(env, capsys):
    movie = _video(env.dir)
    env.fail_outputs = {"movie_compressed_clip02.mp4"}

    code = main([str(movie), "--clip", "5-10", "--clip", "20-25", "--clip", "30-35", "--quiet"])
    out = capsys.readouterr().out

    assert code == 1
    assert len(env.calls) == 3  # one bad clip does not stop the others
    assert "Successful: 2" in out
    assert "Failed: 1" in out
    assert "movie.mp4 (movie_compressed_clip02.mp4): encoder exploded" in out
    assert "movie.mp4: 2 of 3 clips encoded" in out


def test_output_dir_and_suffix_apply_to_clip_names(env, tmp_path):
    movie = _video(env.dir)
    out_dir = tmp_path / "out"

    code = main([str(movie), "-o", str(out_dir), "--suffix", "_cut", "--clip", "1-2", "--clip", "3-4", "--quiet"])

    assert code == 0
    assert [call.output for call in env.calls] == [
        out_dir / "movie_cut_clip01.mp4",
        out_dir / "movie_cut_clip02.mp4",
    ]


def test_batch_with_parallel_jobs_expands_every_file(monkeypatch, env, capsys):
    first = _video(env.dir, "a.mp4")
    second = _video(env.dir, "b.mp4")

    def fake_slots(self, profile, *, requested_parallel, queued_files, media_info=None):
        return 2

    monkeypatch.setattr(VideoCompressor, "queue_slots", fake_slots)

    code = main([str(first), str(second), "-j", "2", "--clip", "1-5", "--clip", "10-15", "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert sorted(_names(env)) == [
        "a_compressed_clip01.mp4",
        "a_compressed_clip02.mp4",
        "b_compressed_clip01.mp4",
        "b_compressed_clip02.mp4",
    ]
    assert "Source files: 2  ·  Outputs: 4" in out
    assert "Successful: 4" in out


# ── Images and other modes ───────────────────────────────────────────────────


def test_images_ignore_clips_with_a_warning(env, capsys):
    photo = _video(env.dir, "photo.png")

    code = main([str(photo), "--clip", "1-2", "--clip", "3-4", "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert "--clip is ignored for images" in out
    assert len(env.calls) == 1
    assert env.calls[0].output.name.startswith("photo_compressed.")
    assert "clips" not in env.calls[0].kwargs
    assert env.analyzed == []


def test_images_dry_run_also_warns(env, capsys):
    photo = _video(env.dir, "photo.png")

    assert main([str(photo), "--clip", "1-2", "--dry-run", "--quiet"]) == 0
    out = capsys.readouterr().out

    assert "--clip is ignored for images" in out
    assert "Would compress: photo.png" in out
    assert env.calls == []


def test_compare_mode_notes_that_clips_are_ignored(monkeypatch, env, capsys):
    movie = _video(env.dir)
    monkeypatch.setattr(VideoCompressor, "compare_sample", lambda self, **kwargs: [])

    assert main([str(movie), "--compare", "--clip", "1-5", "--quiet"]) == 0

    assert "--clip is ignored with --compare" in capsys.readouterr().out
    assert env.calls == []


# ── Dry run ──────────────────────────────────────────────────────────────────


def test_dry_run_lists_clips_mode_duration_and_outputs(env, capsys):
    movie = _video(env.dir)

    code = main([str(movie), "--clip", "5-10", "--clip", "20-25.5", "--clip", "1:30-", "--dry-run", "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert env.calls == []  # nothing is encoded
    assert "Would compress: movie.mp4" in out
    assert "Mode: separate (one file per clip)" in out
    assert "Clips: 3" in out
    assert "1. 00:05.000 - 00:10.000" in out
    assert "2. 00:20.000 - 00:25.500" in out
    assert "3. 01:30.000 - 02:00.000" in out  # open end resolved to the 2:00 source
    assert "Duration: 00:40.500 of 02:00.000" in out  # 5 + 5.5 + 30 s
    assert "Output: movie_compressed_clip01.mp4 (Clip 1 of 3, 00:05.000)" in out
    assert "Output: movie_compressed_clip02.mp4 (Clip 2 of 3, 00:05.500)" in out
    assert "Output: movie_compressed_clip03.mp4 (Clip 3 of 3, 00:30.000)" in out


def test_dry_run_join_shows_one_output(env, capsys):
    movie = _video(env.dir)

    code = main([str(movie), "--clip", "5-10", "--clip", "20-30", "--join", "--dry-run", "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert env.calls == []
    assert "Mode: join (all clips in one file)" in out
    assert "Duration: 00:15.000 of 02:00.000" in out
    assert out.count("Output:") == 1
    assert "Output: movie_compressed.mp4 (2 clips joined, 00:15.000)" in out


def test_dry_run_without_clips_is_unchanged(env, capsys):
    movie = _video(env.dir)

    assert main([str(movie), "--dry-run", "--quiet"]) == 0
    out = capsys.readouterr().out

    assert "Would compress: movie.mp4" in out
    assert "  -> Output: movie_compressed.mp4" in out
    assert "Clips" not in out
    assert "Duration" not in out
    assert env.calls == []
    assert env.analyzed == []


def test_dry_run_target_size_preview_uses_the_clip_length(monkeypatch, env, capsys):
    movie = _video(env.dir)
    seen = []

    def fake_plan(self, profile, analyzed):
        seen.append(analyzed.duration)
        return SimpleNamespace(
            target_size_mb=profile.target_size_mb,
            mode=SimpleNamespace(value="balanced"),
            video_bitrate=2_000_000,
            tolerance_ratio=0.05,
            target_size_bytes=profile.target_size_mb * 1_000_000,
        )

    monkeypatch.setattr(VideoCompressor, "_resolve_target_size_plan", fake_plan)

    code = main([str(movie), "--clip", "5-15", "--clip", "20-50", "--target-size", "100", "--dry-run", "--quiet"])
    out = capsys.readouterr().out

    assert code == 0
    assert seen == [10.0, 30.0]  # sized per clip, not for the full 120 s source
    assert out.count("Target: 100 MB (balanced)") == 2
    assert "Skip:" not in out
