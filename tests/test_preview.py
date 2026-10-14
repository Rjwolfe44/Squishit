"""Tests for the Qt-free preview service (frame grabs, filmstrip, playback).

Everything except the ``smoke`` tests runs without ffmpeg: grabs go
through a fake tracker/grab and playback through a fake process, fake stream
and fake clock.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from video_compressor.core import preview
from video_compressor.core.preview import (
    FrameScheduler,
    PlaybackSession,
    PreviewService,
    ThumbnailCache,
    build_frame_cmd,
    build_playback_cmd,
    display_size,
    filmstrip_count,
    grab_frame,
)

WAIT = 5.0  # generous upper bound for waiting on worker threads


def _wait_until(predicate, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _arg_after(cmd: list, flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# ---------------------------------------------------------------------------
# argv builders
# ---------------------------------------------------------------------------


def test_frame_cmd_exact_argv_accurate():
    cmd = build_frame_cmd("ffmpeg", "clip.mp4", 12.3456, 640, 360)
    assert cmd == [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-ss", "12.345",
        "-i", "clip.mp4",
        "-map", "0:v:0", "-frames:v", "1", "-an", "-sn", "-dn",
        "-vf", "scale=640:360:flags=bilinear,setsar=1,format=rgb24",
        "-f", "rawvideo", "-",
    ]


def test_frame_cmd_fast_adds_input_side_flags_before_input():
    fast = build_frame_cmd("ffmpeg", "clip.mp4", 5, 64, 48, fast=True)
    accurate = build_frame_cmd("ffmpeg", "clip.mp4", 5, 64, 48, fast=False)
    assert "-noaccurate_seek" in fast
    assert "-noaccurate_seek" not in accurate
    # input options must precede -i, and -ss stays an input option too
    assert fast.index("-noaccurate_seek") < fast.index("-i")
    assert fast.index("-ss") < fast.index("-i")
    # everything else is identical
    assert [a for a in fast if a != "-noaccurate_seek"] == accurate


def test_frame_cmd_never_uses_skip_frame_nokey():
    # -skip_frame nokey yields no frame in the last GOP with the bundled ffmpeg.
    assert "-skip_frame" not in build_frame_cmd("ffmpeg", "a.mp4", 1, 16, 16, fast=True)


@pytest.mark.parametrize(
    "t, expected",
    [(0, "0.000"), (1.5, "1.500"), (0.9999, "0.999"), (19.9667, "19.966"), (3661.25, "3661.250"), (-4, "0.000")],
)
def test_seek_time_is_floored_to_milliseconds(t, expected):
    # Rounding up could skip the very frame the user asked for.
    assert _arg_after(build_frame_cmd("ffmpeg", "a.mp4", t, 16, 16), "-ss") == expected
    assert _arg_after(build_playback_cmd("ffmpeg", "a.mp4", t, 16, 16, 24), "-ss") == expected


def test_float_noise_does_not_lose_a_millisecond():
    # 0.1 + 0.2 == 0.30000000000000004, 1.1 * 1000 == 1100.0000000000002
    assert _arg_after(build_frame_cmd("ffmpeg", "a.mp4", 0.3, 16, 16), "-ss") == "0.300"
    assert _arg_after(build_frame_cmd("ffmpeg", "a.mp4", 4.35, 16, 16), "-ss") == "4.350"


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\me\My Videos\holiday clip.mp4",
        "/tmp/vid\u00e9o \u65e5\u672c\u8a9e/\u30af\u30ea\u30c3\u30d7 \u2603.mkv",
        "weird,name:with'filter;chars[0]=x.mp4",
    ],
)
def test_path_is_one_argv_element_and_never_in_filtergraph(path):
    for cmd in (
        build_frame_cmd("ffmpeg", path, 1.0, 320, 180),
        build_frame_cmd("ffmpeg", path, 1.0, 320, 180, fast=True),
        build_playback_cmd("ffmpeg", path, 1.0, 320, 180, 30, duration=5),
    ):
        assert cmd.count(path) == 1
        assert cmd[cmd.index("-i") + 1] == path
        vf = _arg_after(cmd, "-vf")
        assert path not in vf
        assert all(path not in part for i, part in enumerate(cmd) if part != path)


def test_paths_accept_pathlike(tmp_path):
    p = tmp_path / "a b.mp4"
    assert build_frame_cmd("ffmpeg", p, 0, 8, 8)[build_frame_cmd("ffmpeg", p, 0, 8, 8).index("-i") + 1] == str(p)


def test_playback_cmd_exact_argv():
    cmd = build_playback_cmd("ffmpeg", "clip.mp4", 2.5, 960, 540, 29.97)
    assert cmd == [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-ss", "2.500",
        "-i", "clip.mp4",
        "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", "fps=29.97,scale=960:540,setsar=1,format=rgb24",
        "-f", "rawvideo", "-",
    ]


def test_playback_cmd_duration_is_an_input_option():
    cmd = build_playback_cmd("ffmpeg", "clip.mp4", 10, 64, 48, 30.0, duration=4.25)
    assert _arg_after(cmd, "-t") == "4.250"
    assert cmd.index("-t") < cmd.index("-i")
    assert _arg_after(cmd, "-vf").startswith("fps=30,")
    assert "-t" not in build_playback_cmd("ffmpeg", "clip.mp4", 10, 64, 48, 30.0)


def test_commands_drop_audio_subtitles_and_data():
    for cmd in (build_frame_cmd("ffmpeg", "a.mp4", 0, 8, 8), build_playback_cmd("ffmpeg", "a.mp4", 0, 8, 8, 24)):
        for flag in ("-an", "-sn", "-dn", "-nostdin"):
            assert flag in cmd
        assert _arg_after(cmd, "-loglevel") == "error"
        assert _arg_after(cmd, "-map") == "0:v:0"


# ---------------------------------------------------------------------------
# display_size
# ---------------------------------------------------------------------------


def test_display_size_fits_and_keeps_aspect():
    assert display_size(1920, 1080) == (960, 540)
    assert display_size(3840, 2160) == (960, 540)
    assert display_size(1920, 800) == (960, 400)


@pytest.mark.parametrize("rotation", [90, 270, -90, 450, 90.0])
def test_display_size_swaps_for_quarter_turns(rotation):
    # 1280x720 shot in portrait: shown 720x1280, fitted by height.
    assert display_size(1280, 720, rotation) == (302, 540)


@pytest.mark.parametrize("rotation", [0, 180, -180, 360])
def test_display_size_keeps_orientation_for_half_turns(rotation):
    assert display_size(1280, 720, rotation) == (960, 540)


def test_display_size_applies_sample_aspect_ratio():
    # 720x480 with 4:3 pixels is displayed 960x480.
    assert display_size(720, 480, sar="4:3") == (960, 480)
    # "N/D" is accepted too, and a narrow-pixel SAR shrinks the width.
    assert display_size(720, 480, sar="4/3") == (960, 480)
    assert display_size(720, 480, sar="1:2") == (360, 480)


@pytest.mark.parametrize("sar", ["0:1", "1:0", "0:0", "N/A", "", "abc", "-1:1", None, 5])
def test_display_size_ignores_unusable_sar(sar):
    assert display_size(640, 360, sar=sar) == (640, 360)


def test_display_size_rotation_and_sar_combine():
    # Stored 720x480 with 4:3 pixels, rotated: displayed 480x960 -> fit 540 tall.
    w, h = display_size(720, 480, 90, "4:3")
    assert h == 540 and w == 270


def test_display_size_never_upscales():
    assert display_size(320, 240) == (320, 240)
    assert display_size(100, 50) == (100, 50)
    assert display_size(100, 50, max_w=4000, max_h=4000) == (100, 50)
    assert display_size(480, 640, max_w=4000, max_h=4000) == (480, 640)


def test_display_size_honors_custom_limits():
    assert display_size(1920, 1080, max_w=640, max_h=640) == (640, 360)
    assert display_size(1080, 1920, max_w=640, max_h=640) == (360, 640)


@pytest.mark.parametrize(
    "dims",
    [(1, 1), (3, 3), (1919, 1081), (1081, 1919), (2, 1000), (1000, 2), (641, 359), (7, 5)],
)
def test_display_size_is_even_and_at_least_two(dims):
    for rotation in (0, 90):
        w, h = display_size(dims[0], dims[1], rotation)
        assert w % 2 == 0 and h % 2 == 0
        assert w >= 2 and h >= 2
        assert w <= 960 and h <= 540


def test_display_size_one_by_one_edge():
    assert display_size(1, 1) == (2, 2)
    assert display_size(1, 1, 90, "3:1") == (2, 2)


def test_display_size_extreme_aspect_stays_valid():
    w, h = display_size(2, 1000)
    assert (w, h) == (2, 540)


@pytest.mark.parametrize("dims", [(0, 0), (-5, 10), (640, 0)])
def test_display_size_unknown_dimensions_fall_back_to_limits(dims):
    assert display_size(dims[0], dims[1]) == (960, 540)


def test_display_size_no_float_noise_loss():
    # 1919x1081 fits the height exactly; float error must not cost 2 px.
    assert display_size(1919, 1081)[1] == 540


# ---------------------------------------------------------------------------
# filmstrip_count
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "width, thumb, expected",
    [(0, 80, 6), (100, 80, 6), (480, 80, 6), (560, 80, 7), (1200, 80, 15), (2400, 80, 30), (10000, 80, 30), (800, 0, 6), (800, -3, 6)],
)
def test_filmstrip_count_clamps(width, thumb, expected):
    assert filmstrip_count(width, thumb) == expected


# ---------------------------------------------------------------------------
# grab_frame (fake tracker; no ffmpeg)
# ---------------------------------------------------------------------------


class FakeTracker:
    """Stands in for the process tracker: scripted outputs, recorded argv."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.cmds: list = []

    def run_capture(self, cmd, timeout):
        self.cmds.append((list(cmd), timeout))
        return self.outputs.pop(0) if self.outputs else b""


def _grab(tracker, **kw):
    return grab_frame("ffmpeg", "clip.mp4", kw.pop("t", 1.0), 4, 2, tracker=tracker, **kw)


def test_grab_returns_exact_frame():
    frame = b"\x07" * 24
    tracker = FakeTracker(frame)
    assert _grab(tracker, timeout=3.5) == frame
    assert len(tracker.cmds) == 1
    assert tracker.cmds[0][1] == 3.5
    assert tracker.cmds[0][0] == build_frame_cmd("ffmpeg", "clip.mp4", 1.0, 4, 2)


def test_grab_rejects_wrong_sized_output():
    tracker = FakeTracker(b"\x01" * 23)
    assert _grab(tracker, duration=10.0, fps=25.0, t=10.0) is None
    assert len(tracker.cmds) == 1  # truncated data is not an EOF case: no retry
    assert _grab(FakeTracker(b"\x01" * 25)) is None


def test_grab_timeout_or_launch_failure_is_none_without_retry():
    tracker = FakeTracker(None)
    assert _grab(tracker, t=10.0, duration=10.0, fps=25.0) is None
    assert len(tracker.cmds) == 1


def test_grab_retries_once_with_the_last_frame_at_eof():
    frame = b"\x09" * 24
    tracker = FakeTracker(b"", frame)
    assert _grab(tracker, t=2.0, duration=2.0, fps=25.0) == frame
    assert len(tracker.cmds) == 2
    first, retry = tracker.cmds[0][0], tracker.cmds[1][0]
    assert _arg_after(first, "-ss") == "2.000"
    assert first == build_frame_cmd("ffmpeg", "clip.mp4", 2.0, 4, 2)
    # the retry decodes the final second and keeps the last frame
    assert _arg_after(retry, "-ss") == "1.000"
    assert _arg_after(retry, "-vf").endswith(",reverse")
    assert retry == build_frame_cmd("ffmpeg", "clip.mp4", 1.0, 4, 2, tail=True)


def test_grab_eof_retry_covers_container_longer_than_video():
    # Audio runs on past the last video frame, so t is before the container end
    # yet after the last video frame: still an end-of-file case.
    tracker = FakeTracker(b"", b"\x01" * 24)
    assert _grab(tracker, t=9.4, duration=10.0, fps=25.0) is not None
    assert len(tracker.cmds) == 2


def test_grab_eof_window_is_bounded_by_frame_count():
    # 30 frames at most: 60 fps -> half a second, 120 fps -> a quarter second
    for fps, expected in ((25.0, "9.000"), (60.0, "9.500"), (120.0, "9.750"), (None, "9.000")):
        tracker = FakeTracker(b"", b"\x00" * 24)
        assert _grab(tracker, t=99.0, duration=10.0, fps=fps) is not None
        assert _arg_after(tracker.cmds[1][0], "-ss") == expected


def test_grab_eof_retry_is_always_accurate_even_after_a_fast_grab():
    tracker = FakeTracker(b"", b"\x00" * 24)
    _grab(tracker, t=5.0, duration=5.0, fps=10.0, fast=True)
    assert "-noaccurate_seek" in tracker.cmds[0][0]
    assert "-noaccurate_seek" not in tracker.cmds[1][0]


def test_grab_retries_only_once():
    tracker = FakeTracker(b"", b"", b"\x00" * 24)
    assert _grab(tracker, t=5.0, duration=5.0, fps=10.0) is None
    assert len(tracker.cmds) == 2


def test_grab_no_retry_without_duration_or_before_the_end():
    tracker = FakeTracker(b"", b"\x00" * 24)
    assert _grab(tracker, t=5.0) is None
    assert len(tracker.cmds) == 1
    tracker = FakeTracker(b"", b"\x00" * 24)
    assert _grab(tracker, t=1.0, duration=10.0, fps=25.0) is None  # empty mid-file: a real failure
    assert len(tracker.cmds) == 1
    tracker = FakeTracker(b"", b"\x00" * 24)
    assert _grab(tracker, t=1.0, duration=0.0, fps=25.0) is None  # unusable duration
    assert len(tracker.cmds) == 1


def test_grab_eof_window_never_starts_before_zero():
    tracker = FakeTracker(b"", b"\x00" * 24)
    assert _grab(tracker, t=1.0, duration=0.02, fps=10.0) is not None
    assert _arg_after(tracker.cmds[1][0], "-ss") == "0.000"


def test_frame_cmd_tail_mode_reverses_after_scaling():
    cmd = build_frame_cmd("ffmpeg", "clip.mp4", 9.0, 640, 360, tail=True)
    assert _arg_after(cmd, "-vf") == "scale=640:360:flags=bilinear,setsar=1,format=rgb24,reverse"
    assert _arg_after(cmd, "-frames:v") == "1"
    assert "reverse" not in _arg_after(build_frame_cmd("ffmpeg", "clip.mp4", 9.0, 640, 360), "-vf")


def test_grab_invalid_size_is_none():
    tracker = FakeTracker(b"")
    assert grab_frame("ffmpeg", "a.mp4", 0, 0, 2, tracker=tracker) is None
    assert tracker.cmds == []


def test_grab_missing_ffmpeg_returns_none(tmp_path):
    assert grab_frame(str(tmp_path / "no-such-ffmpeg"), "a.mp4", 0, 4, 2) is None


# ---------------------------------------------------------------------------
# process tracker (real subprocess: python itself, no ffmpeg)
# ---------------------------------------------------------------------------


def test_tracker_captures_stdout():
    tracker = preview._ProcessTracker()
    out = tracker.run_capture([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'abc')"], 20.0)
    assert out == b"abc"


def test_tracker_timeout_returns_none():
    tracker = preview._ProcessTracker()
    started = time.monotonic()
    assert tracker.run_capture([sys.executable, "-c", "import time; time.sleep(30)"], 0.3) is None
    assert time.monotonic() - started < 10


def test_tracker_kill_all_unblocks_running_grab():
    tracker = preview._ProcessTracker()
    result: list = []
    thread = threading.Thread(
        target=lambda: result.append(tracker.run_capture([sys.executable, "-c", "import time; time.sleep(30)"], 60.0))
    )
    thread.start()
    assert _wait_until(lambda: tracker._procs)
    started = time.monotonic()
    tracker.kill_all()
    thread.join(10)
    assert not thread.is_alive()
    assert result == [None]
    assert time.monotonic() - started < 8
    # still usable after kill_all, unlike close()
    assert tracker.run_capture([sys.executable, "-c", "pass"], 20.0) == b""


def test_tracker_close_refuses_new_work():
    tracker = preview._ProcessTracker()
    tracker.close()
    assert tracker.run_capture([sys.executable, "-c", "pass"], 5.0) is None
    assert not tracker._procs


# ---------------------------------------------------------------------------
# ThumbnailCache
# ---------------------------------------------------------------------------


def test_cache_get_put_and_accounting():
    cache = ThumbnailCache(1000)
    assert len(cache) == 0 and cache.bytes_used == 0
    cache.put("a", b"x" * 100)
    cache.put("b", b"y" * 200)
    assert len(cache) == 2 and cache.bytes_used == 300
    assert cache.get("a") == b"x" * 100
    assert cache.get("missing") is None
    # replacing a key adjusts the byte count instead of double counting
    cache.put("a", b"z" * 50)
    assert cache.bytes_used == 250 and len(cache) == 2
    cache.clear()
    assert len(cache) == 0 and cache.bytes_used == 0


def test_cache_evicts_least_recently_used_by_bytes():
    cache = ThumbnailCache(100)
    cache.put("a", b"a" * 40)
    cache.put("b", b"b" * 40)
    cache.get("a")  # touch: b is now the LRU entry
    cache.put("c", b"c" * 40)  # 120 > 100 -> evict b
    assert cache.get("b") is None
    assert cache.get("a") is not None and cache.get("c") is not None
    assert cache.bytes_used == 80


def test_cache_one_large_put_evicts_several():
    cache = ThumbnailCache(100)
    for name in "abc":
        cache.put(name, name.encode() * 30)
    cache.put("big", b"B" * 90)
    assert len(cache) == 1 and cache.get("big") is not None
    assert cache.bytes_used == 90


def test_cache_skips_values_over_budget_and_none():
    cache = ThumbnailCache(100)
    cache.put("keep", b"k" * 10)
    cache.put("huge", b"h" * 101)
    cache.put("none", None)
    cache.put(None, b"x")
    assert len(cache) == 1 and cache.get("huge") is None
    # an oversized replacement must not leave the stale old value behind
    cache.put("keep", b"k" * 500)
    assert cache.get("keep") is None and cache.bytes_used == 0


def test_cache_default_budget_is_64_megabytes():
    assert ThumbnailCache().max_bytes == 64 * 1024 * 1024


def test_cache_key_changes_with_file_and_parameters(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"0" * 100)
    base = ThumbnailCache.make_key(f, 1.5, 64, 48, False)
    assert base == ThumbnailCache.make_key(str(f), 1.5, 64, 48, False)
    assert base[0] == str(f)
    # every field of the key matters
    assert ThumbnailCache.make_key(f, 1.5, 64, 48, True) != base
    assert ThumbnailCache.make_key(f, 1.5, 65, 48, False) != base
    assert ThumbnailCache.make_key(f, 1.5, 64, 49, False) != base
    assert ThumbnailCache.make_key(f, 1.6, 64, 48, False) != base
    # millisecond resolution: sub-millisecond differences share an entry
    assert ThumbnailCache.make_key(f, 1.5004, 64, 48, False) == base

    cache = ThumbnailCache(10_000)
    cache.put(base, b"frame")
    assert cache.get(base) == b"frame"

    # mtime changes -> miss
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    after_touch = ThumbnailCache.make_key(f, 1.5, 64, 48, False)
    assert after_touch != base and cache.get(after_touch) is None

    # size changes -> miss (restore mtime so only the size differs)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert ThumbnailCache.make_key(f, 1.5, 64, 48, False) == base
    f.write_bytes(b"0" * 101)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    after_resize = ThumbnailCache.make_key(f, 1.5, 64, 48, False)
    assert after_resize != base and cache.get(after_resize) is None


def test_cache_key_is_none_for_missing_file(tmp_path):
    assert ThumbnailCache.make_key(tmp_path / "nope.mp4", 1, 8, 8, False) is None
    assert ThumbnailCache(100).get(None) is None


def test_cache_is_thread_safe():
    cache = ThumbnailCache(5_000)
    errors: list = []

    def hammer(seed: int) -> None:
        try:
            for i in range(500):
                key = (seed, i % 40)
                cache.put(key, bytes([seed]) * (10 + i % 50))
                cache.get((seed, (i * 7) % 40))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=hammer, args=(s,)) for s in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert 0 <= cache.bytes_used <= 5_000
    assert cache.bytes_used == sum(len(v) for v in cache._entries.values())


# ---------------------------------------------------------------------------
# FrameScheduler
# ---------------------------------------------------------------------------


class GatedGrab:
    """A fake grab that records calls and can block until released."""

    def __init__(self, gate_first: int = 0):
        self.calls: list = []
        self.started = threading.Event()
        self.gates: list = [threading.Event() for _ in range(gate_first)]
        self.lock = threading.Lock()
        self.raise_for: set = set()

    def __call__(self, ffmpeg, path, t, w, h, *, fast=False, duration=None, fps=None, timeout=10.0):
        with self.lock:
            index = len(self.calls)
            self.calls.append((path, t, w, h, fast))
        self.started.set()
        if index < len(self.gates):
            assert self.gates[index].wait(WAIT)
        if t in self.raise_for:
            raise RuntimeError("boom")
        return bytes([int(t) % 256]) * (w * h * 3)


def _req(sched, t, results, *, fast=True, path="clip.mp4"):
    return sched.request("ffmpeg", path, t, 2, 2, lambda rgb, t=t: results.append((t, rgb)), fast=fast)


def test_scheduler_latest_pending_request_wins():
    grab = GatedGrab(gate_first=1)
    sched = FrameScheduler("test-latest", grab=grab)
    results: list = []
    try:
        _req(sched, 1, results)
        assert grab.started.wait(WAIT)  # request 1 is now running, blocked
        _req(sched, 2, results)
        _req(sched, 3, results)
        last = _req(sched, 4, results)
        assert last == sched.generation
        grab.gates[0].set()
        assert _wait_until(lambda: len(results) == 2)
    finally:
        sched.shutdown()
    assert [c[1] for c in grab.calls] == [1, 4]  # 2 and 3 were overwritten, never grabbed
    assert [t for t, _ in results] == [1, 4]  # the running one is never interrupted
    assert results[1][1] == bytes([4]) * 12


def test_scheduler_runs_on_its_own_named_thread():
    names: list = []
    sched = FrameScheduler("test-named", grab=GatedGrab())
    try:
        sched.request("ffmpeg", "a.mp4", 0, 2, 2, lambda rgb: names.append(threading.current_thread().name))
        assert _wait_until(lambda: names)
    finally:
        sched.shutdown()
    assert names == ["test-named"]
    assert names[0] != threading.current_thread().name


def test_scheduler_passes_grab_arguments():
    grab = GatedGrab()
    sched = FrameScheduler("test-args", grab=grab)
    results: list = []
    try:
        sched.request("ffmpeg", "x.mp4", 7.5, 6, 4, results.append, fast=True, duration=60.0, fps=24.0)
        assert _wait_until(lambda: results)
    finally:
        sched.shutdown()
    assert grab.calls == [("x.mp4", 7.5, 6, 4, True)]
    assert results[0] == bytes([7]) * (6 * 4 * 3)


def test_scheduler_cancel_discards_running_and_pending():
    grab = GatedGrab(gate_first=1)
    sched = FrameScheduler("test-cancel", grab=grab)
    results: list = []
    try:
        _req(sched, 1, results)
        assert grab.started.wait(WAIT)
        _req(sched, 2, results)  # pending
        sched.cancel()
        grab.gates[0].set()
        # a request made after cancel() is delivered normally
        assert _wait_until(lambda: len(grab.calls) >= 1)
        _req(sched, 3, results)
        assert _wait_until(lambda: [t for t, _ in results] == [3])
    finally:
        sched.shutdown()
    assert [c[1] for c in grab.calls] == [1, 3]  # pending 2 was dropped by cancel
    assert [t for t, _ in results] == [3]  # stale generation 1 was never delivered


def test_scheduler_drop_superseded_gives_strict_latest_only():
    grab = GatedGrab(gate_first=1)
    sched = FrameScheduler("test-strict", grab=grab, drop_superseded=True)
    results: list = []
    try:
        _req(sched, 1, results)
        assert grab.started.wait(WAIT)
        _req(sched, 2, results)
        grab.gates[0].set()
        assert _wait_until(lambda: [t for t, _ in results] == [2])
    finally:
        sched.shutdown()
    assert [c[1] for c in grab.calls] == [1, 2]
    assert [t for t, _ in results] == [2]  # stale generation 1 discarded


def test_scheduler_continuous_requests_still_show_frames_by_default():
    # Scrubbing faster than a grab completes must not starve the display.
    grab = GatedGrab(gate_first=1)
    sched = FrameScheduler("test-starve", grab=grab)
    results: list = []
    try:
        _req(sched, 1, results)
        assert grab.started.wait(WAIT)
        _req(sched, 2, results)  # arrives while 1 runs
        grab.gates[0].set()
        assert _wait_until(lambda: len(results) == 2)
    finally:
        sched.shutdown()
    assert results[0][0] == 1


def test_scheduler_grab_exception_delivers_none_and_keeps_working():
    grab = GatedGrab()
    grab.raise_for = {1}
    sched = FrameScheduler("test-raise", grab=grab)
    results: list = []
    try:
        _req(sched, 1, results)
        assert _wait_until(lambda: len(results) == 1)
        _req(sched, 2, results)
        assert _wait_until(lambda: len(results) == 2)
    finally:
        sched.shutdown()
    assert results[0] == (1, None)
    assert results[1][1] is not None


def test_scheduler_callback_exception_does_not_kill_worker():
    grab = GatedGrab()
    sched = FrameScheduler("test-cb-raise", grab=grab)
    results: list = []

    def bad(_rgb):
        raise ValueError("callback bug")

    try:
        sched.request("ffmpeg", "a.mp4", 1, 2, 2, bad)
        assert _wait_until(lambda: len(grab.calls) == 1)
        _req(sched, 2, results)
        assert _wait_until(lambda: len(results) == 1)
    finally:
        sched.shutdown()


def test_scheduler_shutdown_joins_and_is_idempotent():
    sched = FrameScheduler("test-shutdown", grab=GatedGrab())
    results: list = []
    _req(sched, 1, results)
    assert _wait_until(lambda: results)
    thread = sched._thread
    assert thread is not None and thread.is_alive()
    assert sched.shutdown() is True
    assert not thread.is_alive()
    assert sched.shutdown() is True  # idempotent
    assert _req(sched, 2, results) == -1  # ignored after shutdown
    time.sleep(0.05)
    assert [t for t, _ in results] == [1]


def test_scheduler_shutdown_never_started_and_pending_dropped():
    sched = FrameScheduler("test-idle", grab=GatedGrab())
    assert sched.shutdown() is True
    grab = GatedGrab(gate_first=1)
    sched = FrameScheduler("test-pending", grab=grab)
    results: list = []
    _req(sched, 1, results)
    assert grab.started.wait(WAIT)
    _req(sched, 2, results)
    # shutdown while a grab is blocked: returns after the timeout, drops everything
    started = time.monotonic()
    assert sched.shutdown(timeout=0.1) is False
    assert time.monotonic() - started < 2
    grab.gates[0].set()
    assert sched.join(WAIT) is True
    assert results == []  # nothing delivered after shutdown
    assert [c[1] for c in grab.calls] == [1]


# ---------------------------------------------------------------------------
# PlaybackSession (fake process, fake stream, fake clock)
# ---------------------------------------------------------------------------

W, H = 2, 2
FRAME = W * H * 3  # 12 bytes


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeStream:
    """A pipe stand-in: yields frames, optionally in small chunks and with a read cost."""

    def __init__(self, frames, *, tail: bytes = b"", clock=None, read_cost: float = 0.0, chunk=None, fail_after=None):
        self._queue = list(frames)
        if tail:
            self._queue.append(tail)
        self._cur = memoryview(b"")
        self._clock = clock
        self._cost = read_cost
        self._chunk = chunk
        self._fail_after = fail_after
        self._reads = 0
        self.closed = False
        self.eof = threading.Event()

    def readinto(self, buf) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if not len(self._cur):
            if self._fail_after is not None and self._reads >= self._fail_after:
                raise OSError("pipe broke")
            if not self._queue:
                self.eof.set()
                return 0
            self._cur = memoryview(self._queue.pop(0))
            self._reads += 1
            if self._clock is not None:
                self._clock.now += self._cost
        n = min(len(buf), len(self._cur), self._chunk or len(self._cur))
        buf[:n] = self._cur[:n]
        self._cur = self._cur[n:]
        return n

    def close(self) -> None:
        self.closed = True


class BlockingStream(FakeStream):
    """Yields ``frames`` then blocks (like ffmpeg stuck on a full pipe) until killed."""

    def __init__(self, frames):
        super().__init__(frames)
        self.release = threading.Event()

    def readinto(self, buf) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if not len(self._cur) and not self._queue:
            self.release.wait(WAIT)
            return 0
        return super().readinto(buf)


class FakeProc:
    def __init__(self, stream, returncode: int = 0):
        self.stdout = stream
        self._rc = returncode
        self.returncode = None
        self.killed = False

    def poll(self):
        if self.returncode is None and (self.killed or getattr(self.stdout, "eof", threading.Event()).is_set()):
            self.returncode = -9 if self.killed else self._rc
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        release = getattr(self.stdout, "release", None)
        if release is not None:
            release.set()

    def wait(self, timeout=None):
        return self.poll() if self.poll() is not None else self._rc


class FakePopen:
    def __init__(self, proc=None, exc=None):
        self.proc = proc
        self.exc = exc
        self.calls: list = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.proc


class Recorder:
    def __init__(self):
        self.frames: list = []  # (t, data)
        self.ends: list = []
        self.done = threading.Event()

    def on_frame(self, t, data):
        self.frames.append((t, data))

    def on_end(self, reason):
        self.ends.append(reason)
        self.done.set()


def _frames(n: int) -> list:
    return [bytes([i + 1]) * FRAME for i in range(n)]


def _session(rec, popen, *, start=0.0, fps=10.0, clock=None, duration=None, **kw):
    kwargs = dict(popen=popen)
    if clock is not None:
        kwargs.update(clock=clock, sleep=clock.sleep)
    kwargs.update(kw)
    return PlaybackSession("ffmpeg", "clip.mp4", start, W, H, fps, rec.on_frame, rec.on_end, duration, **kwargs)


def test_playback_paces_frames_against_the_clock():
    clock = FakeClock()
    rec = Recorder()
    deliver_times: list = []
    frames = _frames(5)
    proc = FakeProc(FakeStream(frames, clock=clock))

    def on_frame(t, data):
        rec.on_frame(t, data)
        deliver_times.append(clock.now)

    session = PlaybackSession("ffmpeg", "clip.mp4", 3.0, W, H, 10.0, on_frame, rec.on_end,
                              popen=FakePopen(proc), clock=clock, sleep=clock.sleep)
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["eof"]
    # playhead time is exactly start + k / fps_out, bytes untouched and in order
    assert [t for t, _ in rec.frames] == [3.0 + k / 10.0 for k in range(5)]
    assert [d for _, d in rec.frames] == frames
    # the first frame is delivered immediately; frame k is due k/fps later
    assert deliver_times == pytest.approx([1000.0 + k * 0.1 for k in range(5)])
    assert clock.sleeps == pytest.approx([0.1] * 4)
    assert (session.frames_read, session.frames_delivered, session.frames_dropped) == (5, 5, 0)
    assert proc.killed or proc.poll() is not None  # tidied up


def test_playback_handles_partial_pipe_reads():
    clock = FakeClock()
    rec = Recorder()
    frames = _frames(3)
    proc = FakeProc(FakeStream(frames, clock=clock, chunk=5))
    _session(rec, FakePopen(proc), clock=clock).start()
    assert rec.done.wait(WAIT)
    assert [d for _, d in rec.frames] == frames
    assert rec.ends == ["eof"]


def test_playback_frames_are_independent_copies():
    clock = FakeClock()
    rec = Recorder()
    proc = FakeProc(FakeStream(_frames(3), clock=clock))
    _session(rec, FakePopen(proc), clock=clock).start()
    assert rec.done.wait(WAIT)
    assert all(isinstance(d, bytes) and len(d) == FRAME for _, d in rec.frames)
    assert len({d for _, d in rec.frames}) == 3  # buffer reuse must not alias frames


def test_playback_drops_frames_when_consumer_is_behind():
    clock = FakeClock()
    rec = Recorder()
    proc = FakeProc(FakeStream(_frames(8), clock=clock))

    def on_frame(t, data):
        rec.on_frame(t, data)
        if round(t * 10) == 1:  # frame 1: a slow consumer, 3.5 frame intervals
            clock.now += 0.35

    session = PlaybackSession("ffmpeg", "clip.mp4", 0.0, W, H, 10.0, on_frame, rec.on_end,
                              popen=FakePopen(proc), clock=clock, sleep=clock.sleep)
    session.start()
    assert rec.done.wait(WAIT)
    ks = [round(t * 10) for t, _ in rec.frames]
    # frames 2 and 3 were already stale when the consumer returned: dropped, not queued
    assert ks == [0, 1, 4, 5, 6, 7]
    assert session.frames_dropped == 2
    assert session.frames_read == 8
    assert rec.ends == ["eof"]
    # delivered frames carry their own content/time, not a shifted one
    assert [d for _, d in rec.frames] == [bytes([k + 1]) * FRAME for k in ks]


def test_playback_slow_decoder_slows_down_instead_of_dropping():
    clock = FakeClock()
    rec = Recorder()
    # ffmpeg needs 0.25 s per frame but 0.1 s is requested
    proc = FakeProc(FakeStream(_frames(6), clock=clock, read_cost=0.25))
    session = _session(rec, FakePopen(proc), clock=clock)
    session.start()
    assert rec.done.wait(WAIT)
    assert [round(t * 10) for t, _ in rec.frames] == [0, 1, 2, 3, 4, 5]  # nothing dropped
    assert session.frames_dropped == 0
    assert clock.sleeps == []  # never waits for a producer that is already late
    assert rec.ends == ["eof"]


def test_playback_on_frame_runs_on_reader_thread():
    clock = FakeClock()
    rec = Recorder()
    names: list = []
    proc = FakeProc(FakeStream(_frames(2), clock=clock))
    session = PlaybackSession("ffmpeg", "c.mp4", 0, W, H, 10, lambda t, d: names.append(threading.current_thread().name),
                              rec.on_end, popen=FakePopen(proc), clock=clock, sleep=clock.sleep)
    session.start()
    assert rec.done.wait(WAIT)
    assert set(names) == {"preview-playback"}


def test_playback_launch_arguments():
    clock = FakeClock()
    rec = Recorder()
    popen = FakePopen(FakeProc(FakeStream([], clock=clock)))
    path = r"C:\clips\my video \u00e9.mp4"
    session = PlaybackSession("ffmpeg-bin", path, 4.0, W, H, 12.0, rec.on_frame, rec.on_end, 3.0,
                              popen=popen, clock=clock, sleep=clock.sleep)
    session.start()
    assert rec.done.wait(WAIT)
    (cmd, kwargs), = popen.calls
    assert cmd == build_playback_cmd("ffmpeg-bin", path, 4.0, W, H, 12.0, 3.0)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert session.process is popen.proc


def test_playback_eof_with_clean_exit_and_no_frames():
    clock = FakeClock()
    rec = Recorder()
    _session(rec, FakePopen(FakeProc(FakeStream([], clock=clock), returncode=0)), clock=clock).start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["eof"] and rec.frames == []


def test_playback_truncated_last_frame_is_not_delivered():
    clock = FakeClock()
    rec = Recorder()
    proc = FakeProc(FakeStream(_frames(2), tail=b"\x09" * 7, clock=clock))
    _session(rec, FakePopen(proc), clock=clock).start()
    assert rec.done.wait(WAIT)
    assert len(rec.frames) == 2
    assert rec.ends == ["eof"]


def test_playback_ffmpeg_failure_is_error():
    clock = FakeClock()
    rec = Recorder()
    proc = FakeProc(FakeStream(_frames(2), clock=clock), returncode=1)
    _session(rec, FakePopen(proc), clock=clock).start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["error"]
    assert len(rec.frames) == 2  # what arrived before the failure was still shown


def test_playback_broken_pipe_is_error():
    clock = FakeClock()
    rec = Recorder()
    proc = FakeProc(FakeStream(_frames(5), clock=clock, fail_after=2), returncode=1)
    _session(rec, FakePopen(proc), clock=clock).start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["error"]
    assert len(rec.frames) == 2


def test_playback_cannot_launch_is_error():
    rec = Recorder()
    session = _session(rec, FakePopen(exc=FileNotFoundError("no ffmpeg")))
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["error"] and rec.frames == []
    assert session.process is None
    session.stop()  # still safe
    assert rec.ends == ["error"]


def test_playback_raising_on_frame_ends_with_error_and_kills_ffmpeg():
    clock = FakeClock()
    rec = Recorder()
    proc = FakeProc(FakeStream(_frames(5), clock=clock))

    def on_frame(t, data):
        raise RuntimeError("consumer bug")

    PlaybackSession("ffmpeg", "c.mp4", 0, W, H, 10, on_frame, rec.on_end,
                    popen=FakePopen(proc), clock=clock, sleep=clock.sleep).start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["error"]
    assert proc.killed


def test_playback_raising_on_end_is_swallowed():
    clock = FakeClock()
    done = threading.Event()

    def on_end(reason):
        done.set()
        raise RuntimeError("bug")

    session = PlaybackSession("ffmpeg", "c.mp4", 0, W, H, 10, lambda t, d: None, on_end,
                              popen=FakePopen(FakeProc(FakeStream([], clock=clock))), clock=clock, sleep=clock.sleep)
    session.start()
    assert done.wait(WAIT)
    assert _wait_until(lambda: not session.running)


def test_playback_stop_mid_stream_kills_closes_and_reports_once():
    rec = Recorder()
    stream = BlockingStream(_frames(3))
    proc = FakeProc(stream)
    session = PlaybackSession("ffmpeg", "c.mp4", 0, W, H, 20.0, rec.on_frame, rec.on_end, popen=FakePopen(proc))
    session.start()
    assert _wait_until(lambda: len(rec.frames) == 3)  # real clock/sleep, now blocked in readinto
    started = time.monotonic()
    session.stop()
    assert time.monotonic() - started < 1.5
    assert proc.killed
    assert stream.closed
    assert rec.ends == ["stopped"]
    assert not session.running
    session.stop()  # idempotent
    session.stop()
    assert rec.ends == ["stopped"]
    count = len(rec.frames)
    time.sleep(0.05)
    assert len(rec.frames) == count  # nothing after stop


def test_playback_stop_interrupts_pacing_wait():
    rec = Recorder()
    stream = FakeStream(_frames(50))
    proc = FakeProc(stream)
    session = PlaybackSession("ffmpeg", "c.mp4", 0, W, H, 0.5, rec.on_frame, rec.on_end, popen=FakePopen(proc))
    session.start()  # 2 s between frames: the reader is sleeping in the pacing wait
    assert _wait_until(lambda: len(rec.frames) == 1)
    started = time.monotonic()
    session.stop()
    assert time.monotonic() - started < 1.5
    assert rec.ends == ["stopped"] and len(rec.frames) == 1 and proc.killed


def test_playback_stop_from_inside_on_frame_does_not_deadlock():
    clock = FakeClock()
    rec = Recorder()
    proc = FakeProc(FakeStream(_frames(10), clock=clock))
    holder: dict = {}

    def on_frame(t, data):
        rec.on_frame(t, data)
        if len(rec.frames) == 3:
            holder["s"].stop()

    session = PlaybackSession("ffmpeg", "c.mp4", 0, W, H, 10, on_frame, rec.on_end,
                              popen=FakePopen(proc), clock=clock, sleep=clock.sleep)
    holder["s"] = session
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["stopped"]
    assert len(rec.frames) == 3
    assert proc.killed


def test_playback_stop_before_start_is_inert():
    rec = Recorder()
    popen = FakePopen(FakeProc(FakeStream(_frames(2))))
    session = _session(rec, popen)
    session.stop()
    session.start()
    time.sleep(0.05)
    assert popen.calls == [] and rec.ends == [] and not session.running


def test_playback_start_twice_launches_once():
    clock = FakeClock()
    rec = Recorder()
    popen = FakePopen(FakeProc(FakeStream(_frames(2), clock=clock)))
    session = _session(rec, popen, clock=clock)
    session.start()
    session.start()
    assert rec.done.wait(WAIT)
    assert _wait_until(lambda: not session.running)
    assert len(popen.calls) == 1 and rec.ends == ["eof"]


def test_playback_stop_during_launch_race_still_kills_process():
    # stop() lands between "thread started" and "process stored": the process must still die.
    rec = Recorder()
    proc = FakeProc(BlockingStream(_frames(1)))
    gate = threading.Event()

    def slow_popen(cmd, **kwargs):
        assert gate.wait(WAIT)
        return proc

    session = PlaybackSession("ffmpeg", "c.mp4", 0, W, H, 100.0, rec.on_frame, rec.on_end, popen=slow_popen)
    session.start()
    stopper = threading.Thread(target=session.stop)
    stopper.start()
    time.sleep(0.05)
    gate.set()
    stopper.join(5)
    assert rec.done.wait(WAIT)
    assert rec.ends == ["stopped"]
    assert proc.killed


@pytest.mark.parametrize("kwargs", [dict(w=0), dict(h=-2), dict(fps_out=0), dict(fps_out=-1)])
def test_playback_rejects_bad_geometry(kwargs):
    args = dict(w=W, h=H, fps_out=10)
    args.update(kwargs)
    with pytest.raises(ValueError):
        PlaybackSession("ffmpeg", "c.mp4", 0, args["w"], args["h"], args["fps_out"], lambda t, d: None, lambda r: None)


# ---------------------------------------------------------------------------
# PreviewService (fake grab / fake playback; no ffmpeg)
# ---------------------------------------------------------------------------


@pytest.fixture
def media(tmp_path) -> Path:
    path = tmp_path / "some clip.mp4"
    path.write_bytes(b"not really a video")
    return path


@pytest.fixture
def service_factory():
    made: list = []

    def make(grab=None, **kw):
        svc = PreviewService("ffmpeg", "ffprobe", grab=grab or GatedGrab(), **kw)
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.shutdown()


def test_service_defaults():
    svc = PreviewService("ffmpeg")
    try:
        assert svc.ffprobe_path is None
        assert svc.cache.max_bytes == 64 * 1024 * 1024
    finally:
        svc.shutdown()


def test_service_request_frame_callback_signature_and_thread(service_factory, media):
    svc = service_factory()
    got: list = []
    svc.request_frame(media, 3.5, (4, 2), fast=True,
                      callback=lambda *a: got.append((a, threading.current_thread().name)))
    assert _wait_until(lambda: got)
    (args, thread_name), = got
    t, w, h, rgb, fast = args
    assert (t, w, h, fast) == (3.5, 4, 2, True)
    assert rgb == bytes([3]) * 24
    assert thread_name == "preview-interactive"


def test_service_request_frame_latest_wins(service_factory, media):
    grab = GatedGrab(gate_first=1)
    svc = service_factory(grab)
    got: list = []
    for t in (1, 2, 3, 4):
        svc.request_frame(media, t, (4, 2), fast=True, callback=lambda t, w, h, rgb, fast: got.append(t))
        if t == 1:
            assert grab.started.wait(WAIT)
    grab.gates[0].set()
    assert _wait_until(lambda: len(got) == 2)
    assert got == [1, 4]
    assert [c[1] for c in grab.calls] == [1, 4]


def test_service_cache_hit_skips_ffmpeg_but_still_calls_back_on_worker(service_factory, media):
    grab = GatedGrab()
    svc = service_factory(grab)
    got: list = []

    def cb(t, w, h, rgb, fast):
        got.append((rgb, threading.current_thread().name))

    svc.request_frame(media, 2.0, (4, 2), fast=False, callback=cb)
    assert _wait_until(lambda: len(got) == 1)
    svc.request_frame(media, 2.0, (4, 2), fast=False, callback=cb)
    assert _wait_until(lambda: len(got) == 2)
    assert len(grab.calls) == 1  # second answered from the cache
    assert got[0][0] == got[1][0] is not None
    assert got[1][1] == "preview-interactive"  # even a hit never runs on the caller's thread
    # a different mode (or size) is a different entry
    svc.request_frame(media, 2.0, (4, 2), fast=True, callback=cb)
    assert _wait_until(lambda: len(got) == 3)
    assert len(grab.calls) == 2


def test_service_cache_invalidated_when_file_changes(service_factory, media):
    grab = GatedGrab()
    svc = service_factory(grab)
    got: list = []
    cb = lambda t, w, h, rgb, fast: got.append(rgb)  # noqa: E731
    svc.request_frame(media, 1.0, (4, 2), fast=True, callback=cb)
    assert _wait_until(lambda: len(got) == 1)
    media.write_bytes(b"replaced with something longer")
    svc.request_frame(media, 1.0, (4, 2), fast=True, callback=cb)
    assert _wait_until(lambda: len(got) == 2)
    assert len(grab.calls) == 2


def test_service_failed_grab_calls_back_with_none_and_is_not_cached(service_factory, media):
    results = {"value": None}

    def failing(ffmpeg, path, t, w, h, **kw):
        return results["value"]

    svc = service_factory(failing)
    got: list = []
    cb = lambda t, w, h, rgb, fast: got.append(rgb)  # noqa: E731
    svc.request_frame(media, 1.0, (4, 2), fast=True, callback=cb)
    assert _wait_until(lambda: got == [None])
    assert len(svc.cache) == 0
    results["value"] = b"\x01" * 24
    svc.request_frame(media, 1.0, (4, 2), fast=True, callback=cb)
    assert _wait_until(lambda: len(got) == 2)
    assert got[1] == b"\x01" * 24


def test_service_forwards_duration_and_fps_for_eof_clamp(service_factory, media):
    seen: list = []

    def grab(ffmpeg, path, t, w, h, *, fast=False, duration=None, fps=None, timeout=10.0):
        seen.append((duration, fps))
        return b"\x00" * (w * h * 3)

    svc = service_factory(grab)
    done = threading.Event()
    svc.request_frame(media, 9.0, (2, 2), fast=False, duration=9.0, fps=30.0, callback=lambda *a: done.set())
    assert done.wait(WAIT)
    assert seen == [(9.0, 30.0)]


def test_service_poster_callback_and_fifo_order(service_factory, tmp_path):
    grab = GatedGrab()
    svc = service_factory(grab)
    files = []
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        f = tmp_path / name
        f.write_bytes(b"x")
        files.append(f)
    got: list = []
    for f in files:
        svc.request_poster(f, 1.0, (4, 2), lambda path, w, h, rgb: got.append((path, w, h, rgb is not None)))
    assert _wait_until(lambda: len(got) == 3)
    assert got == [(f, 4, 2, True) for f in files]  # every poster served, in order
    assert all(call[4] is True for call in grab.calls)  # keyframe grabs


def test_service_poster_work_never_blocks_scrubbing(service_factory, tmp_path):
    poster_file = tmp_path / "poster.mp4"
    poster_file.write_bytes(b"p")
    scrub_file = tmp_path / "scrub.mp4"
    scrub_file.write_bytes(b"s")
    release_poster = threading.Event()
    poster_started = threading.Event()

    def grab(ffmpeg, path, t, w, h, **kw):
        if Path(path) == poster_file:
            poster_started.set()
            assert release_poster.wait(WAIT)
        return b"\x00" * (w * h * 3)

    svc = service_factory(grab)
    posters: list = []
    frames: list = []
    svc.request_poster(poster_file, 0.0, (4, 2), lambda *a: posters.append(a))
    assert poster_started.wait(WAIT)  # the poster thread is now stuck inside a slow grab
    svc.request_frame(scrub_file, 1.0, (4, 2), fast=True, callback=lambda *a: frames.append(a))
    assert _wait_until(lambda: len(frames) == 1)  # scrubbing is unaffected
    assert posters == []
    release_poster.set()
    assert _wait_until(lambda: len(posters) == 1)


def test_service_filmstrip_order_times_and_progressive_delivery(service_factory, media):
    grab = GatedGrab(gate_first=4)
    for g in grab.gates[:3]:
        g.set()  # first three grabs go through; the fourth blocks
    svc = service_factory(grab)
    thumbs: list = []
    done = threading.Event()
    svc.request_filmstrip(media, 12.0, 6, (4, 2),
                          lambda i, t, w, h, rgb: thumbs.append((i, t, w, h, rgb)), done.set)
    assert _wait_until(lambda: len(thumbs) == 3)
    time.sleep(0.05)
    assert len(thumbs) == 3 and not done.is_set()  # progressive: first thumbs arrive while the rest are pending
    grab.gates[3].set()
    assert done.wait(WAIT)
    assert [th[0] for th in thumbs] == list(range(6))
    assert [th[1] for th in thumbs] == [1.0, 3.0, 5.0, 7.0, 9.0, 11.0]  # (i + 0.5) * duration / count
    assert all(th[2:4] == (4, 2) and th[4] is not None for th in thumbs)
    assert all(call[4] is True for call in grab.calls)  # fast grabs
    assert [c[1] for c in grab.calls] == [1.0, 3.0, 5.0, 7.0, 9.0, 11.0]


def test_service_filmstrip_runs_on_background_thread_without_done_callback(service_factory, media):
    names: list = []
    svc = service_factory()
    svc.request_filmstrip(media, 6.0, 6, (4, 2), lambda *a: names.append(threading.current_thread().name))
    assert _wait_until(lambda: len(names) == 6)
    assert set(names) == {"preview-filmstrip"}


def test_service_filmstrip_cancel_between_grabs(service_factory, media):
    grab = GatedGrab(gate_first=3)
    grab.gates[0].set()
    grab.gates[1].set()
    svc = service_factory(grab)
    thumbs: list = []
    first_done = threading.Event()
    svc.request_filmstrip(media, 10.0, 10, (4, 2), lambda i, *a: thumbs.append(i), first_done.set)
    assert _wait_until(lambda: len(grab.calls) == 3)  # third grab is in flight, blocked
    svc.cancel_filmstrip()
    grab.gates[2].set()
    # a following filmstrip runs on the same worker, so its completion proves the first one ended
    second: list = []
    second_done = threading.Event()
    svc.request_filmstrip(media, 4.0, 6, (4, 2), lambda i, *a: second.append(i), second_done.set)
    assert second_done.wait(WAIT)
    assert thumbs == [0, 1]  # the in-flight grab's result was discarded, nothing after it ran
    assert not first_done.is_set()
    assert second == list(range(6))
    assert len(grab.calls) == 3 + 6


def test_service_new_filmstrip_cancels_the_previous_one(service_factory, media):
    grab = GatedGrab(gate_first=1)
    svc = service_factory(grab)
    first: list = []
    second: list = []
    done = threading.Event()
    svc.request_filmstrip(media, 8.0, 8, (4, 2), lambda i, *a: first.append(i))
    assert grab.started.wait(WAIT)
    svc.request_filmstrip(media, 8.0, 8, (4, 2), lambda i, *a: second.append(i), done.set)
    grab.gates[0].set()
    assert done.wait(WAIT)
    assert first == []
    assert second == list(range(8))


def test_service_filmstrip_uses_and_fills_the_cache(service_factory, media):
    grab = GatedGrab()
    svc = service_factory(grab)
    for _ in range(2):
        done = threading.Event()
        svc.request_filmstrip(media, 6.0, 6, (4, 2), lambda *a: None, done.set)
        assert done.wait(WAIT)
    assert len(grab.calls) == 6  # the second pass came entirely from the cache


def test_service_filmstrip_degenerate_counts(service_factory, media):
    svc = service_factory()
    thumbs: list = []
    done = threading.Event()
    svc.request_filmstrip(media, 10.0, 0, (4, 2), lambda *a: thumbs.append(a), done.set)
    assert done.wait(WAIT)
    assert thumbs == []


def test_service_filmstrip_reports_failed_grabs_as_none(service_factory, media):
    def grab(ffmpeg, path, t, w, h, **kw):
        return None if t == 3.5 else b"\x00" * (w * h * 3)  # thumbnail 3 of 6 over 6 s

    svc = service_factory(grab)
    thumbs: list = []
    done = threading.Event()
    svc.request_filmstrip(media, 6.0, 6, (4, 2), lambda i, t, w, h, rgb: thumbs.append((i, rgb)), done.set)
    assert done.wait(WAIT)
    assert [i for i, _ in thumbs] == list(range(6))
    assert thumbs[3][1] is None and thumbs[2][1] is not None


class FakeSession:
    instances: list = []

    def __init__(self, ffmpeg, path, start, w, h, fps_out, on_frame, on_end, duration=None):
        self.args = (ffmpeg, path, start, w, h, fps_out, duration)
        self.on_frame, self.on_end = on_frame, on_end
        self.started = 0
        self.stopped = 0
        FakeSession.instances.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


@pytest.fixture
def fake_sessions(monkeypatch):
    FakeSession.instances = []
    monkeypatch.setattr(preview, "PlaybackSession", FakeSession)
    return FakeSession.instances


def test_service_start_playback_stops_the_previous_session(service_factory, fake_sessions, media):
    svc = service_factory()
    a = svc.start_playback(media, 1.0, (64, 36), 24.0, lambda t, d: None, lambda r: None, duration=5.0)
    assert a is fake_sessions[0] and a.started == 1 and a.stopped == 0
    assert a.args == ("ffmpeg", media, 1.0, 64, 36, 24.0, 5.0)
    b = svc.start_playback(media, 2.0, (64, 36), 24.0, lambda t, d: None, lambda r: None)
    assert a.stopped == 1 and b.started == 1 and b.stopped == 0
    svc.stop_playback()
    assert b.stopped == 1
    svc.stop_playback()  # nothing left to stop
    assert b.stopped == 1 and a.stopped == 1


def test_service_cancel_all_and_shutdown_stop_playback(service_factory, fake_sessions, media):
    svc = service_factory()
    a = svc.start_playback(media, 0.0, (64, 36), 24.0, lambda t, d: None, lambda r: None)
    svc.cancel_all()
    assert a.stopped == 1
    b = svc.start_playback(media, 0.0, (64, 36), 24.0, lambda t, d: None, lambda r: None)
    svc.shutdown()
    assert b.stopped == 1
    c = svc.start_playback(media, 0.0, (64, 36), 24.0, lambda t, d: None, lambda r: None)
    assert c.started == 0 and c.stopped == 1  # a closed service never starts new playback


def test_service_cancel_all_cancels_pending_work_but_stays_usable(service_factory, media):
    grab = GatedGrab(gate_first=1)
    svc = service_factory(grab)
    frames: list = []
    thumbs: list = []
    svc.request_frame(media, 1.0, (4, 2), fast=True, callback=lambda *a: frames.append(a))
    assert grab.started.wait(WAIT)
    svc.request_frame(media, 2.0, (4, 2), fast=True, callback=lambda *a: frames.append(a))  # pending
    svc.request_filmstrip(media, 6.0, 6, (4, 2), lambda *a: thumbs.append(a))
    svc.cancel_all()
    grab.gates[0].set()
    time.sleep(0.2)
    assert frames == [] and thumbs == []
    # still usable for a new file afterwards
    svc.request_frame(media, 5.0, (4, 2), fast=True, callback=lambda *a: frames.append(a))
    assert _wait_until(lambda: len(frames) == 1)
    assert frames[0][0] == 5.0


def test_service_shutdown_joins_every_thread_and_is_idempotent(service_factory, media):
    svc = service_factory()
    svc.request_frame(media, 1.0, (4, 2), fast=True, callback=lambda *a: None)
    svc.request_poster(media, 1.0, (4, 2), lambda *a: None)
    svc.request_filmstrip(media, 6.0, 6, (4, 2), lambda *a: None)
    assert _wait_until(lambda: all(w._thread is not None for w in (svc._interactive, svc._posters, svc._filmstrips)))
    threads = [svc._interactive._thread, svc._posters._thread, svc._filmstrips._thread]
    started = time.monotonic()
    svc.shutdown()
    assert time.monotonic() - started < 4
    assert not any(t.is_alive() for t in threads)
    svc.shutdown()  # idempotent
    assert len(svc.cache) == 0


def test_service_nothing_is_delivered_after_shutdown(service_factory, media):
    svc = service_factory()
    svc.shutdown()
    got: list = []
    svc.request_frame(media, 1.0, (4, 2), fast=True, callback=lambda *a: got.append(a))
    svc.request_poster(media, 1.0, (4, 2), lambda *a: got.append(a))
    svc.request_filmstrip(media, 6.0, 6, (4, 2), lambda *a: got.append(a), lambda: got.append("done"))
    time.sleep(0.15)
    assert got == []


def test_service_shutdown_kills_a_running_grab(media):
    # A "slow ffmpeg": python sleeping. The scheduler must not wait out its timeout.
    svc = PreviewService(sys.executable)
    try:
        svc._raw_grab = lambda ffmpeg, path, t, w, h, **kw: svc._tracker.run_capture(
            [sys.executable, "-c", "import time; time.sleep(60)"], 120.0
        )
        svc.request_frame(media, 1.0, (4, 2), fast=True, callback=lambda *a: None)
        assert _wait_until(lambda: svc._tracker._procs)
        started = time.monotonic()
        svc.shutdown()
        assert time.monotonic() - started < 6
        assert not svc._interactive._thread.is_alive()
    finally:
        svc.shutdown()


# ---------------------------------------------------------------------------
# Smoke tests: real ffmpeg
# ---------------------------------------------------------------------------


def _require_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    vendored = Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg" / "ffmpeg.exe"
    if vendored.is_file():
        return str(vendored)
    if os.environ.get("CI"):
        pytest.fail("ffmpeg is required in CI but was not found on PATH")
    pytest.skip("ffmpeg is not available; install it to run the preview smoke tests")


def _make_clip(ffmpeg: str, path: Path, *, seconds: int, size: str, rate: int = 25) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", str(rate), "-an",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return path


@pytest.fixture(scope="module")
def short_clip(tmp_path_factory) -> tuple:
    ffmpeg = _require_ffmpeg()
    # spaces + non-ASCII in the path: argv lists must cope
    folder = tmp_path_factory.mktemp("preview") / "my cl\u00efps \u65e5\u672c"
    return ffmpeg, _make_clip(ffmpeg, folder / "t\u00e9st clip.mp4", seconds=2, size="160x120")


@pytest.fixture(scope="module")
def long_clip(tmp_path_factory) -> tuple:
    ffmpeg = _require_ffmpeg()
    return ffmpeg, _make_clip(ffmpeg, tmp_path_factory.mktemp("preview_long") / "long.mp4",
                              seconds=12, size="320x240", rate=30)


@pytest.mark.smoke
def test_smoke_grab_frame_returns_exact_rgb_bytes(short_clip):
    ffmpeg, clip = short_clip
    w, h = 80, 60
    early = grab_frame(ffmpeg, clip, 0.2, w, h)
    late = grab_frame(ffmpeg, clip, 1.5, w, h)
    assert early is not None and late is not None
    assert len(early) == len(late) == w * h * 3
    assert early != late  # testsrc2 animates, so different timestamps differ

    # fast (keyframe) grabs work everywhere, including the last GOP
    for t in (0.0, 0.5, 1.2, 1.9):
        fast = grab_frame(ffmpeg, clip, t, w, h, fast=True)
        assert fast is not None and len(fast) == w * h * 3, t


@pytest.mark.smoke
def test_smoke_grab_frame_at_end_of_file_uses_the_clamp(short_clip):
    ffmpeg, clip = short_clip
    w, h = 80, 60
    # without a duration there is nothing to clamp to; with one the last frame comes back
    assert grab_frame(ffmpeg, clip, 5.0, w, h) is None
    assert len(grab_frame(ffmpeg, clip, 2.0, w, h, duration=2.0, fps=25.0) or b"") == w * h * 3
    assert len(grab_frame(ffmpeg, clip, 99.0, w, h, duration=2.0, fps=25.0) or b"") == w * h * 3
    assert len(grab_frame(ffmpeg, clip, 2.0, w, h, duration=2.0, fps=25.0, fast=True) or b"") == w * h * 3


@pytest.mark.smoke
def test_smoke_grab_frame_at_end_when_audio_outlasts_video(tmp_path):
    ffmpeg = _require_ffmpeg()
    clip = tmp_path / "av.mp4"
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=duration=2.5",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac",
        str(clip),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    w, h = 80, 60
    last_frame = grab_frame(ffmpeg, clip, 1.95, w, h)  # the last video frame sits at 1.96 s
    assert last_frame is not None
    # the container is 2.5 s long, but there are no video frames after 2 s
    for t in (2.2, 2.5, 99.0):
        assert grab_frame(ffmpeg, clip, t, w, h, duration=2.5, fps=25.0) == last_frame, t
    assert grab_frame(ffmpeg, clip, 2.5, w, h) is None  # unknown duration: nothing to fall back on


@pytest.mark.smoke
def test_smoke_service_frame_and_filmstrip(short_clip):
    ffmpeg, clip = short_clip
    svc = PreviewService(ffmpeg)
    try:
        frames: list = []
        svc.request_frame(clip, 1.0, (80, 60), fast=False, callback=lambda *a: frames.append(a))
        assert _wait_until(lambda: frames, timeout=20)
        t, w, h, rgb, fast = frames[0]
        assert (t, w, h, fast) == (1.0, 80, 60, False) and len(rgb) == 80 * 60 * 3

        thumbs: list = []
        done = threading.Event()
        svc.request_filmstrip(clip, 2.0, 6, (40, 30), lambda i, t, w, h, rgb: thumbs.append((i, rgb)), done.set)
        assert done.wait(30)
        assert [i for i, _ in thumbs] == list(range(6))
        assert all(rgb is not None and len(rgb) == 40 * 30 * 3 for _, rgb in thumbs)
        assert len({rgb for _, rgb in thumbs}) > 1
    finally:
        svc.shutdown()


@pytest.mark.smoke
def test_smoke_playback_delivers_frames_and_ends_with_eof(short_clip):
    ffmpeg, clip = short_clip
    w, h, fps, start = 80, 60, 15.0, 0.5
    rec = Recorder()
    session = PlaybackSession(ffmpeg, clip, start, w, h, fps, rec.on_frame, rec.on_end)
    session.start()
    assert rec.done.wait(30), "playback never finished"
    assert rec.ends == ["eof"]
    assert len(rec.frames) >= 15  # 1.5 s at 15 fps = 22 frames; allow for drops under load
    assert all(len(data) == w * h * 3 for _, data in rec.frames)
    times = [t for t, _ in rec.frames]
    assert times[0] == start
    assert times == sorted(times) and len(set(times)) == len(times)
    ks = [round((t - start) * fps) for t in times]
    assert all(abs(t - (start + k / fps)) < 1e-9 for t, k in zip(times, ks, strict=True))  # exact CFR timeline
    assert rec.frames[0][1] != rec.frames[-1][1]
    assert _wait_until(lambda: not session.running)
    assert session.process.poll() is not None


@pytest.mark.smoke
def test_smoke_playback_stop_mid_stream_leaves_no_ffmpeg(long_clip):
    ffmpeg, clip = long_clip
    w, h = 160, 120
    rec = Recorder()
    session = PlaybackSession(ffmpeg, clip, 0.0, w, h, 30.0, rec.on_frame, rec.on_end)
    session.start()
    assert _wait_until(lambda: len(rec.frames) >= 3, timeout=30)
    proc = session.process
    assert proc is not None and proc.poll() is None  # genuinely still running mid-stream
    started = time.monotonic()
    session.stop()
    assert time.monotonic() - started < 2.0
    assert proc.poll() is not None
    assert rec.ends == ["stopped"]
    session.stop()  # idempotent
    assert rec.ends == ["stopped"]
    assert not session.running


@pytest.mark.smoke
def test_smoke_service_shutdown_kills_playback_process(long_clip):
    ffmpeg, clip = long_clip
    svc = PreviewService(ffmpeg)
    rec = Recorder()
    session = svc.start_playback(clip, 0.0, (160, 120), 30.0, rec.on_frame, rec.on_end)
    assert _wait_until(lambda: len(rec.frames) >= 2, timeout=30)
    proc = session.process
    # starting another session stops the first
    second = svc.start_playback(clip, 1.0, (160, 120), 30.0, lambda t, d: None, lambda r: None)
    assert proc.poll() is not None and rec.ends == ["stopped"]
    assert _wait_until(lambda: second.process is not None, timeout=10)
    second_proc = second.process
    svc.shutdown()
    assert second_proc.poll() is not None


# ── fixes found in review ────────────────────────────────────────────────────


def test_cache_key_matches_the_millisecond_ffmpeg_is_asked_for(tmp_path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    key = ThumbnailCache.make_key
    # 1.0004 s and 1.0 s both seek to 1.000, so they are the same frame...
    assert key(clip, 1.0004, 8, 8, False) == key(clip, 1.0, 8, 8, False)
    # ...but 1.0009 s must not be served the 1.001 s picture (and vice versa).
    assert key(clip, 1.0016, 8, 8, False) != key(clip, 1.0004, 8, 8, False)
    assert key(clip, 1.0009, 8, 8, False)[3] == 1000


def test_cancel_frames_leaves_posters_and_the_filmstrip_alone(tmp_path):
    import threading

    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    gate = threading.Event()
    poster_done = threading.Event()
    seen = []

    def grab(*args, **kwargs):
        if not gate.wait(5):
            return None
        return bytes(2 * 2 * 3)

    service = PreviewService("ffmpeg", grab=grab)
    try:
        service.request_poster(clip, 0.0, (2, 2), lambda *a: (seen.append(a), poster_done.set()))
        service.cancel_frames()
        gate.set()
        assert poster_done.wait(5), "cancel_frames must not drop queued posters"
    finally:
        service.shutdown()
