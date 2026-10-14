"""Tests for the pure trim-editor model: timeline geometry, clip editing, undo, text helpers."""

from __future__ import annotations

import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

from video_compressor.core import utils
from video_compressor.core.clips import Clip, ClipMode, ClipPlan
from video_compressor.core.utils import format_size
from video_compressor.gui import timeline_model
from video_compressor.gui.timeline_model import (
    MAX_PX_PER_SEC,
    ClipEditModel,
    TimelineView,
    describe_selection,
    format_tick_label,
    parse_time_field,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pairs(model: ClipEditModel):
    return [(c.start, c.end) for c in model.clips]


def _two_clip_model(**kwargs) -> ClipEditModel:
    """100 s source with clips 10-20 and 30-40."""
    return ClipEditModel(100.0, clips=[Clip(10, 20), Clip(30, 40)], **kwargs)


def _three_clip_model() -> ClipEditModel:
    return ClipEditModel(
        100.0, clips=[Clip(10, 20, "a"), Clip(30, 40, "b"), Clip(50, 60, "c")]
    )


# ── TimelineView: mapping and zoom ───────────────────────────────────────────


def test_view_starts_fit():
    view = TimelineView(100.0, 800)
    assert view.is_fit
    assert view.px_per_sec == pytest.approx(8.0)
    assert view.offset == 0.0
    assert view.visible_range() == (0.0, 100.0)


def test_time_x_round_trip_at_several_zooms():
    view = TimelineView(100.0, 1000)
    for factor, x in ((1, 0), (4, 250), (3, 900)):
        view.zoom_at(x, factor)
        t0, t1 = view.visible_range()
        for t in (t0, t0 + (t1 - t0) * 0.3, (t0 + t1) / 2, t1):
            assert view.x_to_time(view.time_to_x(t)) == pytest.approx(t, abs=1e-9)


def test_x_to_time_clamps_to_the_video():
    view = TimelineView(100.0, 1000)
    assert view.x_to_time(-50) == 0.0
    assert view.x_to_time(5000) == 100.0
    view.zoom_at(500, 10)
    assert view.x_to_time(-1e6) == 0.0
    assert view.x_to_time(1e6) == 100.0


def test_fit_resets_zoom_and_offset():
    view = TimelineView(100.0, 1000)
    view.zoom_at(500, 20)
    view.pan(300)
    assert not view.is_fit
    view.fit()
    assert view.is_fit and view.offset == 0.0
    assert view.px_per_sec == pytest.approx(10.0)


@pytest.mark.parametrize("x", [0, 1, 123.4, 500, 999.5, 1000])
@pytest.mark.parametrize("factor", [2.0, 3.7, 10.0])
def test_zoom_in_keeps_the_time_under_the_cursor(x, factor):
    view = TimelineView(100.0, 1000)
    before = view.x_to_time(x)
    assert view.zoom_at(x, factor)
    assert view.x_to_time(x) == pytest.approx(before, abs=1e-6)
    assert view.px_per_sec == pytest.approx(10.0 * factor)


def test_zoom_sequence_in_then_out_keeps_the_anchor():
    view = TimelineView(100.0, 1000)
    view.zoom_at(400, 8)
    view.pan(250)
    anchor = view.x_to_time(600)
    view.zoom_at(600, 2)
    assert view.x_to_time(600) == pytest.approx(anchor, abs=1e-6)
    view.zoom_at(600, 0.5)
    assert view.x_to_time(600) == pytest.approx(anchor, abs=1e-6)


def test_zoom_is_limited_between_fit_and_the_maximum():
    view = TimelineView(100.0, 1000)
    view.zoom_at(500, 1e12)
    assert view.px_per_sec == MAX_PX_PER_SEC == 2000.0
    assert not view.zoom_at(500, 2)  # already at the limit
    view.zoom_at(500, 1e-12)
    assert view.is_fit and view.offset == 0.0
    assert not view.zoom_at(500, 0.5)  # already fit


def test_zoom_ignores_bad_factors():
    view = TimelineView(100.0, 1000)
    view.zoom_at(500, 4)
    state = (view.offset, view.px_per_sec)
    for factor in (0, -2, float("nan")):
        assert not view.zoom_at(500, factor)
    assert (view.offset, view.px_per_sec) == state


def test_very_short_video_cannot_zoom_past_fit():
    view = TimelineView(0.2, 1000)  # fit is 5000 px/s, above the usual maximum
    assert view.px_per_sec == pytest.approx(5000.0)
    assert not view.zoom_at(500, 10)
    assert view.is_fit and view.max_px_per_sec == pytest.approx(5000.0)


def test_zoom_out_near_the_end_slides_the_window_inside_the_video():
    view = TimelineView(100.0, 1000)
    view.zoom_at(1000, 10)
    view.pan(1e9)  # far right
    assert view.visible_range() == (pytest.approx(90.0), 100.0)
    view.zoom_at(1000, 0.5)
    t0, t1 = view.visible_range()
    assert t1 == pytest.approx(100.0) and t0 == pytest.approx(80.0)


# ── TimelineView: offset, pan, resize ────────────────────────────────────────


def test_clamp_offset_keeps_the_window_inside_the_video():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)  # 100 px/s, 10 s visible
    view.offset = 1e9
    view.clamp_offset()
    assert view.offset == pytest.approx(90.0)
    view.offset = -5
    view.clamp_offset()
    assert view.offset == 0.0


def test_offset_is_always_zero_when_fit():
    view = TimelineView(100.0, 1000)
    view.offset = 12
    view.clamp_offset()
    assert view.offset == 0.0
    assert not view.pan(500)


def test_pan_moves_toward_later_times_and_stops_at_the_ends():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)
    assert view.pan(500) and view.offset == pytest.approx(5.0)
    assert view.pan(-200) and view.offset == pytest.approx(3.0)
    view.pan(1e9)
    assert view.offset == pytest.approx(90.0)
    assert not view.pan(10)
    view.pan(-1e9)
    assert view.offset == 0.0


def test_set_width_keeps_fit_mode():
    view = TimelineView(100.0, 1000)
    view.set_width(500)
    assert view.is_fit and view.px_per_sec == pytest.approx(5.0)
    view.set_width(2000)
    assert view.is_fit and view.px_per_sec == pytest.approx(20.0)
    assert view.visible_range() == (0.0, 100.0)


def test_set_width_keeps_the_time_scale_when_zoomed():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)
    view.pan(300)
    view.set_width(500)
    assert view.px_per_sec == pytest.approx(100.0)
    assert view.offset == pytest.approx(3.0)
    assert not view.is_fit


def test_set_width_clamps_offset_when_the_window_grows():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)
    view.pan(1e9)  # offset 90
    view.set_width(2000)  # now 20 s visible
    assert view.offset == pytest.approx(80.0)


def test_set_width_never_leaves_zoom_below_fit():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 2)  # 20 px/s
    view.set_width(3000)  # fit is now 30 px/s
    assert view.is_fit and view.px_per_sec == pytest.approx(30.0)


def test_ensure_visible_scrolls_only_when_needed():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)  # 100 px/s, window 0-10
    assert not view.ensure_visible(5.0)
    assert view.ensure_visible(40.0, margin_px=0)
    assert view.visible_range()[0] <= 40.0 <= view.visible_range()[1]
    assert view.ensure_visible(2.0, margin_px=50)
    assert view.time_to_x(2.0) == pytest.approx(50.0)
    fit = TimelineView(100.0, 1000)
    assert not fit.ensure_visible(99.0)


def test_zero_length_video_does_not_crash():
    view = TimelineView(0.0, 400)
    assert view.x_to_time(100) == 0.0
    assert view.visible_range() == (0.0, 0.0)
    assert view.ticks()[0][0] == 0.0
    view.zoom_at(10, 3)
    view.pan(50)


# ── TimelineView: ruler ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "px_per_sec, expected",
    [(2000, 0.05), (100, 1), (10, 10), (1.0, 120), (0.2778, 300), (0.1, 1800)],
)
def test_tick_step_picks_the_nice_step(px_per_sec, expected):
    view = TimelineView(100.0, 1000)
    view.px_per_sec = px_per_sec
    assert view.tick_step() == pytest.approx(expected)


def test_tick_step_gets_finer_as_you_zoom_in_and_keeps_spacing():
    view = TimelineView(100.0, 1000)
    steps = []
    pps = 0.001
    while pps < 2000:
        view.px_per_sec = pps
        step = view.tick_step()
        steps.append(step)
        assert step * pps >= 80 - 1e-9
        pps *= 1.07
    assert steps == sorted(steps, reverse=True)
    assert steps[0] > steps[-1]


def test_tick_step_respects_min_px():
    view = TimelineView(100.0, 1000)
    view.px_per_sec = 100
    assert view.tick_step(min_px=30) == pytest.approx(0.5)
    assert view.tick_step(min_px=150) == pytest.approx(2)


def test_tick_step_for_a_whole_video():
    assert TimelineView(3600.0, 1000).tick_step() == 300  # 1 h across 1000 px
    assert TimelineView(600.0, 1000).tick_step() == 60


def test_ticks_mark_majors_and_minors():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)  # 100 px/s, 0-10 s visible, 1 s steps
    ticks = view.ticks()
    assert len(ticks) == 51  # a minor every 0.2 s
    majors = [t for t, is_major in ticks if is_major]
    assert majors == pytest.approx([float(i) for i in range(11)])
    assert [t for t, _ in ticks] == sorted(t for t, _ in ticks)
    assert ticks[1] == (pytest.approx(0.2), False)


def test_ticks_cover_exactly_the_visible_range_after_panning():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)
    view.pan(250)  # offset 2.5
    t0, t1 = view.visible_range()
    ticks = view.ticks()
    assert all(t0 - 1e-9 <= t <= t1 + 1e-9 for t, _ in ticks)
    assert ticks[0][0] == pytest.approx(2.6)
    assert any(is_major for _, is_major in ticks)


def test_ticks_never_run_past_the_end_of_the_video():
    view = TimelineView(10.5, 1050)
    ticks = view.ticks()
    assert ticks[-1][0] <= 10.5 + 1e-9
    assert ticks[0] == (0.0, True)


@pytest.mark.parametrize(
    "step", [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300]
)
def test_every_step_has_whole_minor_divisions(step):
    view = TimelineView(10_000.0, 1000)
    view.px_per_sec = 80.0 / step
    assert view.tick_step() == pytest.approx(step)
    times = [t for t, major in view.ticks() if major]
    assert all(abs(t / step - round(t / step)) < 1e-6 for t in times)


@pytest.mark.parametrize(
    "t, step, expected",
    [
        (0, 60, "0:00"),
        (90, 30, "1:30"),
        (3725, 5, "1:02:05"),
        (1.5, 0.5, "0:01.5"),
        (61.25, 0.05, "1:01.25"),
        (0.1, 0.1, "0:00.1"),
        (3600.0, 600, "1:00:00"),
    ],
)
def test_format_tick_label(t, step, expected):
    assert format_tick_label(t, step) == expected


# ── TimelineView: snap and hit-test ──────────────────────────────────────────


def test_snap_pulls_to_the_nearest_candidate_inside_the_tolerance():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)  # 100 px/s -> 8 px = 0.08 s
    assert view.snap(5.07, [5.0, 9.0]) == 5.0
    assert view.snap(4.93, [5.0]) == 5.0
    assert view.snap(5.0, [5.0]) == 5.0
    assert view.snap(5.04, [5.0, 5.05]) == 5.05  # nearest of several


def test_snap_leaves_the_time_alone_outside_the_tolerance():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)
    assert view.snap(5.09, [5.0]) == 5.09
    assert view.snap(5.09, []) == 5.09


def test_snap_tolerance_is_in_pixels_so_it_follows_the_zoom():
    view = TimelineView(100.0, 1000)  # 10 px/s: 8 px = 0.8 s
    assert view.snap(5.7, [5.0]) == 5.0
    assert view.snap(5.9, [5.0]) == 5.9
    assert view.snap(5.9, [5.0], tol_px=12) == 5.0


def test_hit_test_finds_handles_body_and_empty_space():
    view = TimelineView(100.0, 1000)  # 10 px/s
    clips = [Clip(10, 20)]  # x 100..200
    assert view.hit_test(100, clips) == (0, "start")
    assert view.hit_test(104, clips) == (0, "start")
    assert view.hit_test(94, clips) == (0, "start")  # handles reach outside the clip
    assert view.hit_test(107, clips) == (0, "body")
    assert view.hit_test(150, clips) == (0, "body")
    assert view.hit_test(196, clips) == (0, "end")
    assert view.hit_test(205, clips) == (0, "end")
    assert view.hit_test(207, clips) is None
    assert view.hit_test(50, clips) is None
    assert view.hit_test(50, []) is None


def test_hit_test_handles_win_over_body_and_width_is_adjustable():
    view = TimelineView(100.0, 1000)
    clips = [Clip(10, 20)]
    assert view.hit_test(103, clips) == (
        0,
        "start",
    )  # inside the clip but on the handle
    assert view.hit_test(103, clips, handle_px=2) == (0, "body")


def test_hit_test_closest_edge_wins_between_overlapping_handles():
    view = TimelineView(100.0, 1000)
    clips = [Clip(10, 20), Clip(20.5, 30)]  # ends at x=200, next starts at x=205
    assert view.hit_test(201, clips) == (0, "end")
    assert view.hit_test(204, clips) == (1, "start")


def test_hit_test_touching_clips_keep_both_edges_grabbable():
    view = TimelineView(100.0, 1000)
    clips = [Clip(10, 20), Clip(20, 30)]  # shared edge at x=200
    assert view.hit_test(197, clips) == (0, "end")  # cursor over the left clip
    assert view.hit_test(203, clips) == (1, "start")  # cursor over the right clip
    assert view.hit_test(200, clips) == (1, "start")  # exact tie: the later clip


def test_hit_test_prefers_the_later_clip_in_time_not_in_list_order():
    view = TimelineView(100.0, 1000)
    clips = [Clip(20, 30), Clip(10, 20)]  # list order reversed
    assert view.hit_test(200, clips) == (0, "start")


def test_hit_test_tiny_clip_picks_the_closest_edge_then_the_end():
    view = TimelineView(100.0, 1000)
    clips = [Clip(10, 11)]  # x 100..110: both handles cover the whole clip
    assert view.hit_test(101, clips) == (0, "start")
    assert view.hit_test(109, clips) == (0, "end")
    assert view.hit_test(105, clips) == (0, "end")


def test_hit_test_overlapping_bodies_resolve_to_the_later_clip():
    view = TimelineView(100.0, 1000)
    clips = [Clip(10, 30), Clip(20, 40)]
    assert view.hit_test(250, clips) == (1, "body")
    assert view.hit_test(150, clips) == (0, "body")


def test_hit_test_follows_zoom_and_scroll():
    view = TimelineView(100.0, 1000)
    view.zoom_at(0, 10)
    view.pan(500)  # offset 5 s, 100 px/s
    clips = [Clip(10, 20)]
    assert view.hit_test(500, clips) == (0, "start")  # x for t=10 is 500
    assert view.hit_test(300, clips) is None


# ── ClipEditModel: construction ──────────────────────────────────────────────


def test_default_model_is_one_whole_video_clip():
    model = ClipEditModel(600.0)
    assert model.clips == (Clip(0.0, 600.0),)
    assert model.selected == 0 and model.count == 1
    assert model.mode is ClipMode.SEPARATE
    assert model.is_whole_video and model.to_plan() is None
    assert not model.can_undo and not model.can_redo


def test_empty_clip_list_means_whole_video():
    assert ClipEditModel(60.0, clips=[]).clips == (Clip(0.0, 60.0),)
    assert ClipEditModel(60.0, clips=None).clips == (Clip(0.0, 60.0),)
    assert ClipEditModel(60.0, clips=[Clip(5, 5.01)]).clips == (Clip(0.0, 60.0),)


def test_initial_clips_are_normalized_but_keep_order_and_labels():
    model = ClipEditModel(
        100.0, clips=[Clip(90, 200, "end"), Clip(-5, 10), Clip(50, 40), Clip(70, 70.05)]
    )
    assert model.clips == (Clip(90.0, 100.0, "end"), Clip(0.0, 10.0), Clip(40.0, 50.0))


@pytest.mark.parametrize(
    "fps, expected",
    [
        (30, 0.1),
        (60, 0.1),
        (24, 0.125),
        (10, 0.3),
        (29.97, 0.101),
        (0, 0.1),
        (-5, 0.1),
        (float("nan"), 0.1),
    ],
)
def test_min_len_is_the_longer_of_0_1_s_and_three_frames(fps, expected):
    assert ClipEditModel(100.0, fps=fps).min_len == pytest.approx(expected)


def test_duration_must_be_positive_and_is_floored_to_a_millisecond():
    for bad in (0, -1, float("nan"), float("inf"), 0.0004):
        with pytest.raises(ValueError):
            ClipEditModel(bad)
    assert ClipEditModel(10.0009).duration == 10.0


# ── ClipEditModel: add_clip ──────────────────────────────────────────────────


def test_add_clip_inserts_in_start_order_and_selects_it():
    model = _two_clip_model()
    assert model.add_clip(25, 35) == 1
    assert [c.start for c in model.clips] == [10, 25, 30]  # 25-35 trimmed to 25-30
    assert model.selected == 1
    assert model.add_clip(60, 70) == 3
    assert model.add_clip(0, 5) == 0
    assert [c.start for c in model.clips] == [0, 10, 25, 30, 60]
    assert model.selected == 0


def test_add_clip_clamps_to_the_video_and_swaps_reversed_ends():
    model = _two_clip_model()
    model.add_clip(-5, 5)
    model.add_clip(95, 150)
    assert Clip(0.0, 5.0) in model.clips and Clip(95.0, 100.0) in model.clips
    other = _two_clip_model()
    assert other.add_clip(28, 22) is not None  # reversed drag
    assert (22.0, 28.0) in _pairs(other)


def test_add_clip_is_trimmed_to_the_free_gap_it_mostly_covers():
    model = _two_clip_model()
    assert model.add_clip(15, 25) == 1  # 15-20 is taken; 20-25 is free
    assert model.clips[1] == Clip(20.0, 25.0)
    model = _two_clip_model()
    model.add_clip(5, 45)  # spans both clips; the middle gap (20-40) is the biggest
    assert (20.0, 30.0) in _pairs(model)


def test_add_clip_inside_an_existing_clip_or_too_short_is_refused():
    model = _two_clip_model()
    before = model.clips
    assert model.add_clip(12, 18) is None
    assert model.add_clip(25, 25.05) is None
    assert model.add_clip(19.95, 20.05) is None  # only 0.05 s is free
    assert model.add_clip(float("nan"), 5) is None
    assert model.clips == before
    assert not model.can_undo


def test_add_clip_keeps_a_remainder_that_is_just_long_enough():
    model = _two_clip_model()
    assert model.add_clip(19, 20.1) is not None  # 20.0-20.1 free, exactly min_len
    assert (20.0, 20.1) in _pairs(model)


def test_add_clip_can_overlap_when_asked():
    model = _two_clip_model()
    index = model.add_clip(15, 35, allow_overlap=True)
    assert model.clips[index] == Clip(15.0, 35.0)
    assert model.overlaps()


def test_add_clip_strips_the_label_and_rounds_to_milliseconds():
    model = _two_clip_model()
    index = model.add_clip(50.0004, 60.0006, "  Intro ")
    assert model.clips[index] == Clip(50.0, 60.001, "Intro")


def test_add_clip_appends_when_the_list_was_reordered():
    model = _two_clip_model()
    model.reorder(0, 1)  # export order now 30-40, 10-20 (not sorted by start)
    index = model.add_clip(50, 60)
    assert index == 2 and model.clips[-1] == Clip(50.0, 60.0)


def test_add_clip_is_refused_while_the_whole_video_is_one_clip():
    model = ClipEditModel(100.0)
    assert model.add_clip(10, 20) is None


# ── ClipEditModel: set_in / set_out / resize / set_times / move ──────────────


def test_set_in_moves_the_start_within_limits():
    model = _two_clip_model()
    assert model.set_in(0, 5) and model.clips[0] == Clip(5.0, 20.0)
    model.set_in(0, -10)
    assert model.clips[0].start == 0.0
    model.set_in(0, 19.95)  # cannot get shorter than min_len
    assert _pairs(model)[0] == (19.9, 20.0)
    model.set_in(0, 25)
    assert _pairs(model)[0] == (19.9, 20.0)


def test_set_in_is_stopped_by_the_previous_clip():
    model = _two_clip_model()
    model.set_in(1, 15)
    assert _pairs(model)[1] == (20.0, 40.0)


def test_set_out_moves_the_end_within_limits():
    model = _two_clip_model()
    model.set_out(0, 35)  # stopped by the next clip at 30
    assert _pairs(model)[0] == (10.0, 30.0)
    model.set_out(1, 500)
    assert _pairs(model)[1] == (30.0, 100.0)
    model.set_out(0, 10.01)
    assert _pairs(model)[0] == (10.0, 10.1)


def test_edits_reject_bad_indices_and_values_without_side_effects():
    model = _two_clip_model()
    before = model.clips
    for index in (-1, 2, 99, None):
        assert not model.set_in(index, 12)
        assert not model.set_out(index, 18)
        assert not model.move(index, 1)
        assert not model.set_times(index, 11, 19)
        assert not model.rename(index, "x")
        assert not model.delete(index)
    assert not model.set_in(0, float("nan"))
    assert not model.set_out(0, "abc")
    assert not model.move(0, float("nan"))
    assert model.clips == before and not model.can_undo


def test_a_clamped_edit_that_changes_nothing_is_valid_but_not_an_undo_step():
    model = _two_clip_model()
    assert model.set_in(0, 10.0) is True
    assert model.set_out(0, 1000) is True  # clamped to 30, which does change
    assert model.undo()
    assert not model.can_undo


def test_resize_dispatches_by_edge():
    model = _two_clip_model()
    assert model.resize(0, "start", 12) and model.clips[0].start == 12
    assert model.resize(0, "end", 18) and model.clips[0].end == 18
    assert not model.resize(0, "middle", 15)
    assert model.clips[0] == Clip(12.0, 18.0)


def test_set_times_clamps_both_ends():
    model = _two_clip_model()
    assert model.set_times(0, 12, 18) and _pairs(model)[0] == (12.0, 18.0)
    model.set_times(0, 5, 50)
    assert _pairs(model)[0] == (5.0, 30.0)
    model.set_times(1, 0, 500)
    assert _pairs(model)[1] == (30.0, 100.0)
    model.set_times(0, 18, 12)  # reversed -> shortest legal clip at the new start
    assert _pairs(model)[0] == (18.0, 18.1)


def test_move_keeps_the_length_and_stops_at_neighbours_and_the_video():
    model = _two_clip_model()
    model.move(0, 5)
    assert _pairs(model)[0] == (15.0, 25.0)
    model.move(0, 50)
    assert _pairs(model)[0] == (20.0, 30.0)  # next clip starts at 30
    model.move(0, -50)
    assert _pairs(model)[0] == (0.0, 10.0)
    model.move(1, 100)
    assert _pairs(model)[1] == (90.0, 100.0)
    model.move(1, -100)
    assert _pairs(model)[1] == (10.0, 20.0)  # previous clip ends at 10


def test_short_clips_from_a_plan_are_grown_to_min_len_not_shrunk_below_it():
    model = ClipEditModel(100.0, fps=10.0, clips=[Clip(10, 10.1)])  # min_len is 0.3 s
    model.set_out(0, 10.2)
    assert _pairs(model)[0] == (10.0, 10.3)
    model.set_out(0, 10.0)
    assert _pairs(model)[0] == (10.0, 10.3)
    model.set_in(0, 9.5)
    assert _pairs(model)[0] == (9.5, 10.3)


def test_limits_follow_time_order_not_list_order():
    model = _three_clip_model()
    assert model.limits(0) == (0.0, 30.0)
    assert model.limits(1) == (20.0, 50.0)
    assert model.limits(2) == (40.0, 100.0)
    assert model.limits(9) is None
    model.reorder(0, 2)  # export order: b, c, a
    # clip "a" is now index 2 but still sits first in time
    assert model.clips[2].label == "a"
    assert model.limits(2) == (0.0, 30.0)
    model.set_out(2, 99)
    assert model.clips[2] == Clip(10.0, 30.0, "a")
    model.set_in(0, 5)  # "b": stopped by "a" ending at 30
    assert model.clips[0] == Clip(30.0, 40.0, "b")


# ── ClipEditModel: split / delete / reorder / rename / mode / select ─────────


def test_split_at_cuts_the_clip_under_t_in_two():
    model = ClipEditModel(100.0, clips=[Clip(10, 20, "Intro"), Clip(30, 40)])
    assert model.split_at(15) == 1
    assert model.clips == (
        Clip(10.0, 15.0, "Intro"),
        Clip(15.0, 20.0, ""),
        Clip(30.0, 40.0),
    )
    assert model.selected == 1
    assert model.undo()
    assert model.count == 2  # one undo step


def test_split_the_whole_video_default():
    model = ClipEditModel(100.0)
    assert model.split_at(40) == 1
    assert _pairs(model) == [(0.0, 40.0), (40.0, 100.0)]


def test_split_refuses_edges_gaps_and_halves_that_would_be_too_small():
    model = ClipEditModel(100.0, clips=[Clip(10, 20), Clip(30, 40)])
    before = model.clips
    for t in (10, 20, 10.05, 19.95, 25, -1, 100, float("nan")):
        assert model.split_at(t) is None, t
    assert model.clips == before and not model.can_undo


def test_split_allows_halves_of_exactly_min_len():
    model = ClipEditModel(100.0, clips=[Clip(10, 20)])
    assert model.split_at(10.1) == 1
    assert _pairs(model) == [(10.0, 10.1), (10.1, 20.0)]
    assert model.split_at(19.9) == 2
    assert _pairs(model)[-1] == (19.9, 20.0)


def test_delete_removes_a_clip_but_never_the_last_one():
    model = _two_clip_model()
    assert model.delete(0)
    assert model.clips == (Clip(30.0, 40.0),)
    assert not model.delete(0)
    assert model.count == 1
    assert not model.delete(5)


@pytest.mark.parametrize(
    "selected, deleted, expected",
    [(1, 1, 1), (2, 2, 1), (0, 1, 0), (2, 0, 1), (1, 0, 0), (None, 1, None)],
)
def test_delete_keeps_the_selection_sensible(selected, deleted, expected):
    model = _three_clip_model()
    model.select(selected)
    model.delete(deleted)
    assert model.selected == expected


def test_reorder_changes_export_order_only():
    model = _three_clip_model()
    assert model.reorder(0, 2) == 2
    assert [c.label for c in model.clips] == ["b", "c", "a"]
    assert model.reorder(2, 0) == 0
    assert [c.label for c in model.clips] == ["a", "b", "c"]
    assert model.reorder(1, 99) == 2  # clamped to the end
    assert [c.label for c in model.clips] == ["a", "c", "b"]
    assert model.reorder(0, -5) == 0  # already first


def test_reorder_noop_and_invalid_make_no_undo_step():
    model = _three_clip_model()
    assert model.reorder(1, 1) == 1
    assert model.reorder(7, 0) is None
    assert model.reorder(0, None) is None
    assert not model.can_undo


@pytest.mark.parametrize(
    "selected, move_from, move_to, expected",
    [
        (0, 0, 2, 2),
        (1, 0, 2, 0),
        (2, 0, 2, 1),
        (2, 2, 0, 0),
        (0, 2, 0, 1),
        (1, 2, 0, 2),
        (None, 0, 2, None),
    ],
)
def test_reorder_selection_follows_its_clip(selected, move_from, move_to, expected):
    model = _three_clip_model()
    label = model.clips[selected].label if selected is not None else None
    model.select(selected)
    model.reorder(move_from, move_to)
    assert model.selected == expected
    if label is not None:
        assert model.clips[model.selected].label == label


def test_reordered_clips_come_out_in_that_order_in_the_plan():
    model = _three_clip_model()
    model.set_mode(ClipMode.JOIN)
    model.reorder(2, 0)
    plan = model.to_plan()
    assert [c.label for c in plan.clips] == ["c", "a", "b"]


def test_rename_strips_and_ignores_unchanged_labels():
    model = _two_clip_model()
    assert model.rename(0, "  Goal  ")
    assert model.clips[0].label == "Goal"
    assert model.rename(0, None)
    assert model.clips[0].label == ""
    undo_depth = 0
    while model.undo():
        undo_depth += 1
    assert undo_depth == 2
    assert model.rename(0, "")  # already empty: valid, but no new undo step
    assert not model.can_undo


def test_set_mode_is_an_undoable_edit():
    model = _two_clip_model()
    assert model.set_mode(ClipMode.JOIN) and model.mode is ClipMode.JOIN
    assert model.set_mode("separate") and model.mode is ClipMode.SEPARATE
    assert not model.set_mode("sideways")
    assert model.undo() and model.mode is ClipMode.JOIN


def test_select_validates_and_is_not_an_undo_step():
    model = _two_clip_model()
    assert model.select(1) and model.selected == 1
    assert model.select(None) and model.selected is None
    assert not model.select(2) and not model.select(-1)
    assert model.selected is None
    assert not model.can_undo


def test_reset_to_whole_replaces_everything_and_can_be_undone():
    model = _three_clip_model()
    assert model.reset_to_whole()
    assert model.clips == (Clip(0.0, 100.0),) and model.is_whole_video
    assert not model.reset_to_whole()  # already whole
    assert model.undo() and model.count == 3


# ── ClipEditModel: undo / redo / gestures ────────────────────────────────────


def test_undo_and_redo_walk_the_history():
    model = _two_clip_model()
    model.set_out(0, 25)
    model.set_in(1, 35)
    assert model.can_undo and not model.can_redo
    assert model.undo() and _pairs(model) == [(10.0, 25.0), (30.0, 40.0)]
    assert model.undo() and _pairs(model) == [(10.0, 20.0), (30.0, 40.0)]
    assert not model.undo() and not model.can_undo
    assert model.can_redo
    assert model.redo() and model.redo()
    assert _pairs(model) == [(10.0, 25.0), (35.0, 40.0)]
    assert not model.redo()


def test_a_new_edit_clears_redo():
    model = _two_clip_model()
    model.set_out(0, 25)
    model.undo()
    assert model.can_redo
    model.set_out(0, 22)
    assert not model.can_redo
    assert not model.redo()


def test_undo_restores_selection_and_added_clips():
    model = _two_clip_model()
    model.select(1)
    model.add_clip(50, 60)
    assert model.selected == 2
    model.undo()
    assert model.count == 2 and model.selected == 1
    model.redo()
    assert model.count == 3 and model.selected == 2


def test_failed_edits_do_not_touch_history():
    model = _two_clip_model()
    model.set_out(0, 25)
    model.add_clip(12, 18)
    model.split_at(25)  # in the gap between the clips
    model.delete(9)
    assert model.undo() and not model.can_undo


def test_a_whole_drag_is_one_undo_step():
    model = _two_clip_model()
    model.begin_gesture()
    for i in range(50):
        assert model.set_out(0, 20 + 0.1 * (i + 1))
    assert model.clips[0].end == pytest.approx(25.0)
    assert model.end_gesture() is True
    assert model.can_undo
    assert model.undo()
    assert model.clips[0] == Clip(10.0, 20.0)
    assert not model.can_undo
    assert model.redo()
    assert model.clips[0].end == pytest.approx(25.0)


def test_a_gesture_without_changes_leaves_no_undo_step():
    model = _two_clip_model()
    model.begin_gesture()
    model.set_in(0, 10.0)  # clamped to where it already is
    assert model.end_gesture() is False
    assert not model.can_undo
    assert model.end_gesture() is False  # unbalanced end is harmless


def test_gestures_nest_and_commit_once_at_the_outermost_end():
    model = _two_clip_model()
    model.begin_gesture()
    model.begin_gesture()
    model.move(0, 3)
    assert model.end_gesture() is False
    assert model.in_gesture
    model.move(0, 2)
    assert model.end_gesture() is True
    assert not model.in_gesture
    model.undo()
    assert not model.can_undo and model.clips[0] == Clip(10.0, 20.0)


def test_undo_is_ignored_during_a_gesture_and_redo_survives_until_it_commits():
    model = _two_clip_model()
    model.set_out(0, 25)
    model.undo()
    model.begin_gesture()
    model.move(1, 2)
    assert not model.undo() and not model.can_undo and not model.can_redo
    model.end_gesture()
    assert not model.can_redo  # the committed gesture is a new edit
    assert model.can_undo


def test_cancel_gesture_restores_the_state_without_an_undo_step():
    model = _two_clip_model()
    model.set_out(0, 25)
    model.undo()
    model.begin_gesture()
    model.move(0, 4)
    model.set_out(1, 90)
    assert model.cancel_gesture() is True
    assert _pairs(model) == [(10.0, 20.0), (30.0, 40.0)]
    assert not model.in_gesture and not model.can_undo
    assert model.can_redo  # untouched by the cancelled drag
    assert model.cancel_gesture() is False


def test_history_is_capped_at_100_steps():
    model = ClipEditModel(100.0)
    for i in range(105):
        model.rename(0, f"v{i}")
    steps = 0
    while model.undo():
        steps += 1
    assert steps == ClipEditModel.UNDO_LIMIT == 100
    assert model.clips[0].label == "v4"  # the 5 oldest states fell off
    while model.redo():
        pass
    assert model.clips[0].label == "v104"


# ── ClipEditModel: plans ─────────────────────────────────────────────────────


def test_to_plan_is_none_for_the_whole_video_within_50_ms():
    model = ClipEditModel(600.0)
    assert model.to_plan() is None
    model.set_out(0, 599.97)
    assert model.to_plan() is None
    model.set_out(0, 599.9)
    plan = model.to_plan()
    assert isinstance(plan, ClipPlan) and plan.clips == (Clip(0.0, 599.9),)
    model.set_out(0, 600)
    model.set_in(0, 0.04)
    assert model.to_plan() is None
    model.set_in(0, 0.06)
    assert model.to_plan() is not None


def test_to_plan_carries_mode_and_list_order():
    model = _two_clip_model(mode=ClipMode.JOIN)
    model.reorder(1, 0)
    plan = model.to_plan()
    assert plan.mode is ClipMode.JOIN
    assert [(c.start, c.end) for c in plan.clips] == [(30.0, 40.0), (10.0, 20.0)]


def test_a_single_partial_clip_is_a_plan_and_a_whole_clip_with_a_label_is_not():
    model = ClipEditModel(100.0)
    model.rename(0, "Everything")
    assert model.to_plan() is None
    model.set_in(0, 20)
    assert model.to_plan() is not None


def test_from_plan_round_trips():
    plan = ClipPlan((Clip(30, 40, "b"), Clip(10, 20, "a")), ClipMode.JOIN)
    model = ClipEditModel.from_plan(plan, 100.0, 24.0)
    assert model.clips == plan.clips and model.mode is ClipMode.JOIN
    assert model.selected == 0 and not model.can_undo
    assert model.to_plan() == plan


def test_from_plan_with_nothing_is_the_whole_video():
    for plan in (None, ClipPlan(())):
        model = ClipEditModel.from_plan(plan, 50.0)
        assert model.is_whole_video and model.to_plan() is None


def test_from_plan_clamps_open_ends_and_keeps_overlaps():
    plan = ClipPlan((Clip(60, math.inf), Clip(70, 90)))
    model = ClipEditModel.from_plan(plan, 100.0)
    assert model.clips[0] == Clip(60.0, 100.0)
    assert model.overlaps() == [(0, 1)]
    assert model.to_plan().clips == model.clips


def test_to_plan_is_none_when_the_only_clip_is_the_whole_video_even_in_join_mode():
    model = ClipEditModel(100.0, mode=ClipMode.JOIN)
    assert model.to_plan() is None


# ── ClipEditModel: UI helpers ────────────────────────────────────────────────


def test_total_duration_and_count():
    model = _two_clip_model()
    assert model.count == 2
    assert model.total_duration == pytest.approx(20.0)
    model.add_clip(50, 55.5)
    assert model.total_duration == pytest.approx(25.5)


def test_join_cap_is_only_exceeded_in_join_mode_above_30_clips():
    clips = [Clip(i * 20, i * 20 + 10) for i in range(31)]
    join = ClipEditModel(1000.0, clips=clips, mode=ClipMode.JOIN)
    assert join.count == 31 and join.join_cap_exceeded
    assert not ClipEditModel(1000.0, clips=clips).join_cap_exceeded
    assert not ClipEditModel(
        1000.0, clips=clips[:30], mode=ClipMode.JOIN
    ).join_cap_exceeded


def test_clip_at():
    model = ClipEditModel(100.0, clips=[Clip(10, 20), Clip(20, 30), Clip(90, 100)])
    assert model.clip_at(15) == 0
    assert model.clip_at(10) == 0
    assert model.clip_at(20) == 1  # the boundary belongs to the clip that starts there
    assert model.clip_at(9.99) is None
    assert model.clip_at(30) is None
    assert model.clip_at(100) == 2  # the very end counts for a clip ending there
    assert model.clip_at(float("nan")) is None


def test_clip_at_prefers_the_selected_clip_when_clips_overlap():
    model = ClipEditModel(100.0, clips=[Clip(10, 30), Clip(20, 40)])
    assert model.clip_at(25) == 0  # selected is 0
    model.select(1)
    assert model.clip_at(25) == 1
    model.select(None)
    assert model.clip_at(25) == 1  # otherwise the later start


def test_snap_points_are_sorted_unique_and_can_exclude_a_clip():
    model = ClipEditModel(100.0, clips=[Clip(10, 20), Clip(20, 30), Clip(50, 60)])
    assert model.snap_points() == [10.0, 20.0, 30.0, 50.0, 60.0]
    assert model.snap_points(exclude_index=1) == [10.0, 20.0, 50.0, 60.0]
    assert model.snap_points(exclude_index=2) == [10.0, 20.0, 30.0]


# ── ClipEditModel: observers ─────────────────────────────────────────────────


def _watch(model: ClipEditModel):
    calls = []
    unsubscribe = model.subscribe(lambda: calls.append(1))
    return calls, unsubscribe


def test_observers_fire_once_per_state_change():
    model = _two_clip_model()
    calls, _ = _watch(model)
    model.set_out(0, 25)
    assert len(calls) == 1
    model.add_clip(50, 60)
    model.rename(0, "x")
    model.set_mode(ClipMode.JOIN)
    model.select(0)
    model.undo()
    model.redo()
    assert len(calls) == 7


def test_observers_do_not_fire_for_rejected_or_unchanged_requests():
    model = _two_clip_model()
    calls, _ = _watch(model)
    model.add_clip(12, 18)
    model.split_at(5)
    model.delete(9)
    model.select(0)  # already selected
    model.set_in(0, 10.0)  # clamped to where it is
    model.reorder(0, 0)
    model.undo()  # nothing to undo
    model.redo()
    assert calls == []


def test_observers_fire_for_each_edit_in_a_gesture_and_again_when_it_commits():
    model = _two_clip_model()
    calls, _ = _watch(model)
    model.begin_gesture()
    assert calls == []
    for step in range(3):
        model.set_out(0, 21 + step)
    assert len(calls) == 3  # repaint during the drag
    model.end_gesture()
    assert len(calls) == 4  # undo availability changed
    model.begin_gesture()
    model.end_gesture()
    assert len(calls) == 4  # an empty gesture is silent


def test_cancel_gesture_notifies_once():
    model = _two_clip_model()
    model.begin_gesture()
    model.move(0, 2)
    calls, _ = _watch(model)
    model.cancel_gesture()
    assert len(calls) == 1


def test_unsubscribe_stops_notifications_and_is_idempotent():
    model = _two_clip_model()
    calls, unsubscribe = _watch(model)
    unsubscribe()
    unsubscribe()
    model.set_out(0, 25)
    assert calls == []


def test_a_failing_observer_does_not_break_the_model_or_other_observers():
    model = _two_clip_model()

    def boom() -> None:
        raise RuntimeError("observer bug")

    model.subscribe(boom)
    calls, _ = _watch(model)
    assert model.set_out(0, 25)
    assert model.clips[0].end == 25.0
    assert len(calls) == 1


# ── invariants under random edits ────────────────────────────────────────────


def _check_invariants(model: ClipEditModel) -> None:
    assert model.count >= 1
    for clip in model.clips:
        assert clip.start >= 0.0
        assert clip.end <= model.duration + 1e-9
        assert clip.end - clip.start >= model.min_len - 1e-9
        assert abs(clip.start * 1000 - round(clip.start * 1000)) < 1e-6
        assert abs(clip.end * 1000 - round(clip.end * 1000)) < 1e-6
    assert model.overlaps() == []
    assert model.selected is None or 0 <= model.selected < model.count


def test_random_edits_never_break_the_clip_rules():
    rng = random.Random(20240601)
    model = ClipEditModel(120.0, fps=24.0, clips=[Clip(10, 20), Clip(40, 60)])
    for _ in range(800):
        index = rng.randrange(model.count)
        t = rng.uniform(-10, 130)
        op = rng.randrange(12)
        if op == 0:
            model.add_clip(t, t + rng.uniform(-30, 40), "x")
        elif op == 1:
            model.set_in(index, t)
        elif op == 2:
            model.set_out(index, t)
        elif op == 3:
            model.move(index, rng.uniform(-40, 40))
        elif op == 4:
            model.set_times(index, t, t + rng.uniform(-5, 30))
        elif op == 5:
            model.split_at(t)
        elif op == 6:
            model.delete(index)
        elif op == 7:
            model.reorder(index, rng.randrange(model.count))
        elif op == 8:
            model.undo()
        elif op == 9:
            model.redo()
        elif op == 10:
            model.begin_gesture()
            model.move(index, rng.uniform(-5, 5))
            model.end_gesture() if rng.random() < 0.7 else model.cancel_gesture()
        else:
            model.resize(index, rng.choice(["start", "end"]), t)
        _check_invariants(model)
    while model.undo():
        _check_invariants(model)


# ── text helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("90", 90.0),
        (" 45 ", 45.0),
        ("0", 0.0),
        ("600", 600.0),
        ("1:30", 90.0),
        ("1:30.5", 90.5),
        ("00:01:30.250", 90.25),
        ("1:00:00", 3600.0),
        (".5", 0.5),
        ("5.", 5.0),
        ("1.23456", 1.235),
        ("600.001", 600.0),  # a 1 ms overshoot is clamped
    ],
)
def test_parse_time_field_accepts(text, expected):
    assert parse_time_field(
        text, 3700.0 if text == "1:00:00" else 600.0
    ) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["", "   ", "abc", "1:30:", ":30", "-5", "1:-30", "1e3", "nan", "inf", "1:2:3:4", "1,5",
     "1 30", "0x10", "601", "600.002", "1:00:01", "٣", None],
)  # fmt: skip
def test_parse_time_field_rejects(text):
    assert parse_time_field(text, 600.0) is None


def test_parse_time_field_result_is_whole_milliseconds():
    assert parse_time_field("12.3456", 100.0) == 12.346


def test_describe_selection_single_clip():
    model = ClipEditModel(600.0)
    assert describe_selection(model) == "1 clip · 10:00 of 10:00"
    model.set_in(0, 60)
    model.set_out(0, 102)
    assert describe_selection(model) == "1 clip · 0:42 of 10:00"


def test_describe_selection_many_clips_with_and_without_size():
    clips = [Clip(0, 30), Clip(100, 130), Clip(200, 223)]  # 83 s in total
    model = ClipEditModel(600.0, clips=clips)
    assert describe_selection(model) == "3 clips · 1:23 of 10:00"
    expected = format_size(600_000_000 * 83.0 / 600.0)
    assert (
        describe_selection(model, 600_000_000)
        == f"3 clips · 1:23 of 10:00 · about {expected} of source"
    )
    assert describe_selection(model, None) == "3 clips · 1:23 of 10:00"
    assert describe_selection(model, 0) == "3 clips · 1:23 of 10:00"


def test_describe_selection_uses_hours_for_long_videos():
    model = ClipEditModel(7500.0, clips=[Clip(0, 3725)])
    assert describe_selection(model) == "1 clip · 1:02:05 of 2:05:00"


def test_describe_selection_never_shows_zero_time_for_a_real_clip():
    model = ClipEditModel(600.0, clips=[Clip(5, 5.4)])
    assert describe_selection(model).startswith("1 clip · 0:01 of")


def test_describe_selection_caps_the_size_estimate_at_the_source_size():
    model = ClipEditModel(100.0, clips=[Clip(0, 80), Clip(10, 90)], mode=ClipMode.JOIN)
    assert f"about {format_size(1_000_000)} of source" in describe_selection(
        model, 1_000_000
    )


# ── module contract ──────────────────────────────────────────────────────────


def test_timecode_helpers_are_the_core_ones():
    assert timeline_model.format_timecode is utils.format_timecode
    assert timeline_model.parse_timecode is utils.parse_timecode


def test_module_stays_free_of_qt_and_tkinter():
    code = (
        "import sys\n"
        "import video_compressor.gui.timeline_model\n"
        "bad = [m for m in ('PySide6', 'PyQt5', 'PyQt6', 'tkinter', 'customtkinter') if m in sys.modules]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_a_drag_that_ends_where_it_started_leaves_no_undo_step():
    model = ClipEditModel(60.0, 30.0, [Clip(10, 20), Clip(30, 40)])
    model.begin_gesture()
    model.resize(0, "end", 25.0)
    model.resize(0, "end", 20.0)  # back to where it began
    assert model.end_gesture() is False
    assert not model.can_undo
    assert model.clips[0].end == 20.0


# ── keeping or dropping the sound ────────────────────────────────────────────


def test_the_sound_is_kept_by_default_and_round_trips_through_a_plan():
    model = _two_clip_model()
    assert model.keep_audio is True
    assert model.to_plan().keep_audio is True
    silent = ClipPlan((Clip(10, 20), Clip(30, 40)), ClipMode.JOIN, keep_audio=False)
    again = ClipEditModel.from_plan(silent, 100.0)
    assert again.keep_audio is False and again.mode is ClipMode.JOIN
    assert again.to_plan() == silent


def test_the_whole_video_without_sound_is_a_plan_not_no_change():
    model = ClipEditModel(60.0)
    assert model.to_plan() is None
    model.set_keep_audio(False)
    plan = model.to_plan()
    assert plan == ClipPlan((Clip(0.0, 60.0),), ClipMode.SEPARATE, keep_audio=False)
    reopened = ClipEditModel.from_plan(plan, 60.0)
    assert reopened.is_whole_video and reopened.keep_audio is False
    reopened.set_keep_audio(True)
    assert reopened.to_plan() is None


def test_the_sound_choice_is_an_undoable_edit_that_notifies():
    model = _two_clip_model()
    seen: list = []
    model.subscribe(lambda: seen.append(model.keep_audio))
    assert model.set_keep_audio(False) is True
    assert seen == [False] and model.can_undo
    model.set_keep_audio(False)  # no change, no notice, no extra undo step
    assert seen == [False]
    model.undo()
    assert model.keep_audio is True and model.can_redo
    model.redo()
    assert model.keep_audio is False
    model.split_at(15.0)
    model.undo()
    assert model.keep_audio is False  # undoing a later edit leaves the choice alone
    model.reset_to_whole()
    assert model.keep_audio is False


def test_a_gesture_that_only_changes_the_sound_is_one_undo_step():
    model = _two_clip_model()
    model.begin_gesture()
    model.set_keep_audio(False)
    assert model.end_gesture() is True
    model.undo()
    assert model.keep_audio is True and not model.can_undo
    model.begin_gesture()
    model.set_keep_audio(False)
    assert model.cancel_gesture() is True
    assert model.keep_audio is True


def test_describe_selection_says_when_the_sound_is_left_out():
    model = ClipEditModel(60.0, clips=[Clip(0, 30)])
    assert "no sound" not in describe_selection(model)
    model.set_keep_audio(False)
    assert describe_selection(model) == "1 clip · 0:30 of 1:00 · no sound"
    assert describe_selection(model, 60_000_000).endswith(
        f"· no sound · about {format_size(30_000_000)} of source"
    )
