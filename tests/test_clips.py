"""Tests for the pure clip-plan module and the timecode/output-name helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from video_compressor.core.clips import (
    Clip,
    ClipMode,
    ClipPlan,
    build_concat_filtergraph,
    effective_media_info,
    expand_clip_jobs,
    find_overlaps,
    normalize_clips,
    parse_clip_range,
    resolve_clip_plan,
    split_filter_args,
)
from video_compressor.core.utils import format_timecode, parse_timecode, unique_output_path


@dataclass
class _Info:
    duration: float = 600.0
    size: int = 600_000_000
    fps: float = 30.0
    frame_count: int = 18000
    video_bitrate: int = 8_000_000


# ── normalize_clips ──────────────────────────────────────────────────────────


def test_normalize_clamps_swaps_and_drops_short_clips():
    clips = [
        Clip(-5, 10),  # start clamped to 0
        Clip(50, 20),  # reversed -> 20..50
        Clip(100, 100.05),  # shorter than 0.1 s -> dropped
        Clip(590, 900),  # end clamped to the 600 s source
    ]
    out = normalize_clips(clips, 600)
    assert [(c.start, c.end) for c in out] == [(0.0, 10.0), (20.0, 50.0), (590.0, 600.0)]


def test_normalize_keeps_list_order_and_overlaps():
    clips = [Clip(30, 40), Clip(5, 25), Clip(20, 35)]
    out = normalize_clips(clips, 100)
    assert [c.start for c in out] == [30, 5, 20]
    assert find_overlaps(out) == [(0, 2), (1, 2)]


def test_normalize_open_end_becomes_source_duration():
    out = normalize_clips([Clip(60, math.inf)], 600)
    assert out == (Clip(60.0, 600.0),)


def test_normalize_rounds_to_milliseconds():
    out = normalize_clips([Clip(1.00049, 2.99951)], 10)
    assert (out[0].start, out[0].end) == (1.0, 3.0)


# ── resolve_clip_plan ────────────────────────────────────────────────────────


def test_resolve_returns_none_without_clips_or_legacy_trim():
    profile = SimpleNamespace(trim_enabled=False, trim_start=None, trim_end=None)
    assert resolve_clip_plan(None, profile, 600) is None


def test_resolve_maps_legacy_profile_trim_to_one_clip():
    profile = SimpleNamespace(trim_enabled=True, trim_start="0:10", trim_end=40)
    plan = resolve_clip_plan(None, profile, 600)
    assert plan is not None
    assert [(c.start, c.end) for c in plan.clips] == [(10.0, 40.0)]


def test_resolve_legacy_trim_with_only_a_start_runs_to_the_end():
    profile = SimpleNamespace(trim_enabled=True, trim_start=90, trim_end=None)
    plan = resolve_clip_plan(None, profile, 600)
    assert plan.clips == (Clip(90.0, 600.0),)


def test_resolve_whole_source_single_clip_is_no_trim():
    profile = SimpleNamespace(trim_enabled=False)
    plan = ClipPlan((Clip(0, 600),), ClipMode.SEPARATE)
    assert resolve_clip_plan(plan, profile, 600) is None
    # ...but within tolerance only, a real cut is kept
    assert resolve_clip_plan(ClipPlan((Clip(0, 599),)), profile, 600) is not None


def test_resolve_explicit_clips_beat_legacy_trim_and_keep_mode():
    profile = SimpleNamespace(trim_enabled=True, trim_start=1, trim_end=2)
    plan = ClipPlan((Clip(10, 20), Clip(30, 40)), ClipMode.JOIN)
    out = resolve_clip_plan(plan, profile, 600)
    assert out.mode is ClipMode.JOIN
    assert [(c.start, c.end) for c in out.clips] == [(10.0, 20.0), (30.0, 40.0)]


def test_resolve_all_clips_invalid_means_no_plan():
    plan = ClipPlan((Clip(100, 100.01),))
    assert resolve_clip_plan(plan, SimpleNamespace(trim_enabled=False), 600) is None


# ── effective_media_info ─────────────────────────────────────────────────────


def test_effective_info_scales_duration_frames_and_size_only():
    plan = ClipPlan((Clip(0, 30), Clip(100, 130)), ClipMode.JOIN)
    eff = effective_media_info(_Info(), plan)
    assert eff.duration == 60.0
    assert eff.frame_count == 1800
    assert eff.size == 60_000_000
    assert eff.video_bitrate == 8_000_000  # rates are unchanged


def test_effective_info_none_plan_returns_same_object():
    info = _Info()
    assert effective_media_info(info, None) is info


def test_effective_info_never_exceeds_source_duration():
    plan = ClipPlan((Clip(0, 900),))
    assert effective_media_info(_Info(), plan).duration == 600.0


# ── expand_clip_jobs ─────────────────────────────────────────────────────────


def test_expand_none_is_one_untrimmed_job():
    jobs = expand_clip_jobs(None)
    assert len(jobs) == 1 and jobs[0].plan is None and jobs[0].token == ""


def test_expand_separate_makes_one_job_per_clip_with_tokens():
    plan = ClipPlan((Clip(0, 5), Clip(10, 15, "Intro!"), Clip(20, 25)), ClipMode.SEPARATE)
    jobs = expand_clip_jobs(plan)
    assert [j.token for j in jobs] == ["clip01", "Intro!", "clip03"]
    assert [j.title for j in jobs] == ["Clip 1 of 3", "Clip 2 of 3", "Clip 3 of 3"]
    assert all(len(j.plan.clips) == 1 for j in jobs)


def test_expand_duplicate_labels_get_unique_tokens():
    plan = ClipPlan((Clip(0, 5, "a"), Clip(6, 9, "A")))
    assert [j.token for j in expand_clip_jobs(plan)] == ["a", "A-2"]


def test_expand_join_and_single_clip_are_one_job():
    join = ClipPlan((Clip(0, 5), Clip(10, 15)), ClipMode.JOIN)
    jobs = expand_clip_jobs(join)
    assert len(jobs) == 1 and jobs[0].plan is join and jobs[0].token == ""
    single = ClipPlan((Clip(3, 9),))
    assert len(expand_clip_jobs(single)) == 1


# ── filter graph / args ──────────────────────────────────────────────────────


def test_concat_graph_with_audio():
    graph, v, a = build_concat_filtergraph(2)
    assert graph == "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[vcat][acat]"
    assert (v, a) == ("vcat", "acat")


def test_concat_graph_without_audio_has_no_audio_pads():
    graph, v, a = build_concat_filtergraph(3, with_audio=False)
    assert graph == "[0:v:0][1:v:0][2:v:0]concat=n=3:v=1:a=0[vcat]"
    assert (v, a) == ("vcat", None)


def test_concat_graph_folds_post_filters_in():
    graph, v, a = build_concat_filtergraph(
        2, video_filters=["scale=-2:720", "fps=30"], audio_filters=["volume=0.5"]
    )
    assert graph.endswith("[vcat]scale=-2:720,fps=30[vout];[acat]volume=0.5[aout]")
    assert (v, a) == ("vout", "aout")


def test_concat_graph_audio_only():
    graph, v, a = build_concat_filtergraph(2, with_video=False)
    assert graph == "[0:a:0][1:a:0]concat=n=2:v=0:a=1[acat]"
    assert (v, a) == (None, "acat")


def test_concat_graph_rejects_empty():
    with pytest.raises(ValueError):
        build_concat_filtergraph(0)
    with pytest.raises(ValueError):
        build_concat_filtergraph(2, with_video=False, with_audio=False)


def test_split_filter_args_extracts_vf_and_af():
    vf, af, rest = split_filter_args(["-vf", "hue=s=0", "-crf", "20", "-af", "volume=2", "-y"])
    assert vf == ["hue=s=0"] and af == ["volume=2"]
    assert rest == ["-crf", "20", "-y"]


# ── CLI range parsing ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, start, end",
    [("5-10", 5.0, 10.0), ("1:30-2:45", 90.0, 165.0), ("-30", 0.0, 30.0), ("0:00:01.5-3", 1.5, 3.0)],
)
def test_parse_clip_range(text, start, end):
    clip = parse_clip_range(text)
    assert (clip.start, clip.end) == (start, end)


def test_parse_clip_range_open_end():
    clip = parse_clip_range("1:00-")
    assert clip.start == 60.0 and math.isinf(clip.end)


@pytest.mark.parametrize("text", ["", "10", "abc-5", "5-abc", "10-5", "5-5"])
def test_parse_clip_range_rejects_bad_input(text):
    with pytest.raises(ValueError):
        parse_clip_range(text)


# ── timecodes and output names ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "00:00.000"),
        (1.5, "00:01.500"),
        (59.9996, "01:00.000"),  # carries instead of printing 00:60.000
        (61.25, "01:01.250"),
        (3600, "1:00:00.000"),
        (3725.04, "1:02:05.040"),
        (-3, "00:00.000"),
        (None, "00:00.000"),
    ],
)
def test_format_timecode(seconds, expected):
    assert format_timecode(seconds) == expected


def test_format_timecode_always_hours():
    assert format_timecode(5, always_hours=True) == "0:00:05.000"


@pytest.mark.parametrize("seconds", [0.0, 1.5, 61.25, 3725.04, 7.123])
def test_timecode_round_trip(seconds):
    assert parse_timecode(format_timecode(seconds)) == pytest.approx(seconds, abs=0.0006)


def test_unique_output_path_numbers_taken_names(tmp_path):
    first = tmp_path / "movie.mp4"
    assert unique_output_path(first) == first
    first.write_bytes(b"x")
    second = unique_output_path(first)
    assert second.name == "movie (2).mp4"
    second.write_bytes(b"x")
    assert unique_output_path(first).name == "movie (3).mp4"


def test_unique_output_path_respects_reserved_names(tmp_path):
    reserved = {str(tmp_path / "clip.mp4").lower()}
    result = unique_output_path(tmp_path / "clip.mp4", lambda p: str(p).lower() in reserved)
    assert result.name == "clip (2).mp4"
