"""End to end: the Qt window turns clips into real files through the real engine."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from video_compressor.core.clips import Clip, ClipMode, ClipPlan

pytestmark = pytest.mark.smoke

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _tool(name: str) -> str:
    from video_compressor.core.compressor import VideoCompressor

    compressor = VideoCompressor()
    path = compressor.ffmpeg_path if name == "ffmpeg" else compressor.ffprobe_path
    if shutil.which(path) or Path(path).exists():
        return path
    pytest.skip(f"{name} is not available")


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True, creationflags=_CREATE_NO_WINDOW)


def _duration(ffprobe: str, path: Path) -> float:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        creationflags=_CREATE_NO_WINDOW,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    ffmpeg = _tool("ffmpeg")
    folder = tmp_path_factory.mktemp("clips")
    path = folder / "talk.mp4"
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ]
    )
    return path


@pytest.fixture
def window(monkeypatch, source):
    from PySide6.QtWidgets import QApplication

    from video_compressor.gui.qt_shell import main_window as mw

    monkeypatch.setattr(
        "video_compressor.config.ConfigManager.update_config", lambda self, **kwargs: None
    )
    monkeypatch.setattr("video_compressor.config.ConfigManager.save_config", lambda self: None)
    monkeypatch.setattr(mw.SquishItWindow, "_start_hw_probe", lambda self: None)
    app = QApplication.instance() or QApplication([])
    win = mw.SquishItWindow()
    win._history = SimpleNamespace(add_entry=lambda entry: None)
    win._notify_done = lambda ok, total: None
    monkeypatch.setattr(mw, "reveal_in_folder", lambda path: None)
    win.select_quick("Quick Lite")
    win.form.use_hw_accel = False  # the same on every machine
    win.form.output_name_template = "{name}_small"
    try:
        yield win, app
    finally:
        win.close()
        app.processEvents()


def _wait(app, win, predicate, seconds=120.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        win._pump()
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _load(win, app, source: Path) -> str:
    win.add_paths([str(source)])
    key = str(source.resolve())
    assert _wait(app, win, lambda: win._files[key]["info"] is not None, seconds=30)
    assert win._files[key]["row"].can_trim
    return key


def _run_all(win, app) -> None:
    win.start_compression()
    assert _wait(app, win, lambda: not win._running)


def test_separate_clips_become_separate_files(window, source):
    win, app = window
    ffprobe = _tool("ffprobe")
    for stale in source.parent.glob("talk_small*"):
        stale.unlink()
    key = _load(win, app, source)
    win.set_clips(key, ClipPlan((Clip(1, 3), Clip(5, 6.5)), ClipMode.SEPARATE))
    _run_all(win, app)

    outputs = sorted(Path(r.output_file) for r in win._results if r.success)
    assert [p.name for p in outputs] == ["talk_small_clip01.mp4", "talk_small_clip02.mp4"]
    assert _duration(ffprobe, outputs[0]) == pytest.approx(2.0, abs=0.25)
    assert _duration(ffprobe, outputs[1]) == pytest.approx(1.5, abs=0.25)
    assert win._files[key]["clips"] is not None  # still queued after the run

    # running the same files again never overwrites the first results
    _run_all(win, app)
    again = sorted(Path(r.output_file).name for r in win._results if r.success)
    assert again == ["talk_small_clip01 (2).mp4", "talk_small_clip02 (2).mp4"]
    assert all((source.parent / name).exists() for name in again)


def test_joined_clips_become_one_file(window, source):
    win, app = window
    ffprobe = _tool("ffprobe")
    for stale in source.parent.glob("talk_small*"):
        stale.unlink()
    key = _load(win, app, source)
    win.set_clips(key, ClipPlan((Clip(1, 3), Clip(5, 6.5)), ClipMode.JOIN))
    _run_all(win, app)

    outputs = [Path(r.output_file) for r in win._results if r.success]
    assert [p.name for p in outputs] == ["talk_small.mp4"]
    assert _duration(ffprobe, outputs[0]) == pytest.approx(3.5, abs=0.3)
    result = win._results[0]
    assert result.original_size < source.stat().st_size  # judged against the clips only


def test_a_trimmed_run_reports_sensible_progress(window, source):
    win, app = window
    for stale in source.parent.glob("talk_small*"):
        stale.unlink()
    key = _load(win, app, source)
    win.set_clips(key, ClipPlan((Clip(2, 4),), ClipMode.SEPARATE))
    seen = []
    original = win._handle

    def spy(message):
        if message[0] == "progress":
            seen.append(float(getattr(message[1], "progress", 0) or 0))
        original(message)

    win._handle = spy
    _run_all(win, app)
    assert win._results[0].success
    assert seen and max(seen) >= 90.0  # the bar reaches the end of the 2 s clip


def test_a_join_that_repeats_a_part_is_longer_than_that_part_and_reported_so(window, source):
    win, app = window
    ffprobe = _tool("ffprobe")
    for stale in source.parent.glob("talk_small*"):
        stale.unlink()
    key = _load(win, app, source)
    win.set_clips(key, ClipPlan((Clip(0, 3), Clip(0, 3)), ClipMode.JOIN))  # the same 3 s twice
    lengths = set()
    original = win._handle

    def spy(message):
        if message[0] == "progress":
            info = getattr(message[1], "video_info", None)
            if info is not None:
                lengths.add(round(float(getattr(info, "duration", 0) or 0), 2))
        original(message)

    win._handle = spy
    _run_all(win, app)

    result = win._results[0]
    assert result.success
    output = Path(result.output_file)
    assert _duration(ffprobe, output) == pytest.approx(6.0, abs=0.35)
    assert result.duration_seconds == pytest.approx(6.0, abs=0.01)  # not capped at the 10 s source
    # progress and ETA divide by the length of what is encoded: 6 s, not 3 s or 10 s
    assert lengths == {6.0}
