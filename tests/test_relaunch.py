"""Relaunch argv for frozen SquishIt.exe and source runs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from video_compressor.gui import relaunch
from video_compressor.gui.relaunch import app_relaunch_argv, spawn_app
from video_compressor.gui.tray import _spawn

EXE = r"C:\Program Files\SquishIt\SquishIt.exe"
PYTHON = r"C:\Python312\python.exe"


def _record_popen(monkeypatch):
    calls: list[list[str]] = []

    def fake_popen(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return object()

    monkeypatch.setattr(relaunch.subprocess, "Popen", fake_popen)
    return calls


def test_frozen_argv_is_the_exe_without_module_flag(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", EXE)

    tray = app_relaunch_argv(["--tray"])
    quick = app_relaunch_argv(["--quick-compress", r"C:\clips\a.mp4"])
    opened = app_relaunch_argv(["--open", r"C:\clips\a.mp4"])
    bare = app_relaunch_argv([])

    assert tray == [EXE, "--tray"]
    assert quick == [EXE, "--quick-compress", r"C:\clips\a.mp4"]
    assert opened == [EXE, "--open", r"C:\clips\a.mp4"]
    assert bare == [EXE]
    for argv in (tray, quick, opened, bare):
        assert "-m" not in argv
        assert "video_compressor" not in argv


def test_unfrozen_argv_keeps_the_module_entry(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", PYTHON)

    opened = app_relaunch_argv(["--open", "clip.mp4"])
    quick = app_relaunch_argv(["--quick-compress", "clip.mp4"])
    bare = app_relaunch_argv([])

    assert opened == [PYTHON, "-m", "video_compressor", "--open", "clip.mp4"]
    assert quick == [PYTHON, "-m", "video_compressor", "--quick-compress", "clip.mp4"]
    assert bare == [PYTHON, "-m", "video_compressor"]


def test_frozen_false_matches_the_source_entry(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", PYTHON)
    assert app_relaunch_argv(["--tray"]) == [
        PYTHON,
        "-m",
        "video_compressor",
        "--tray",
    ]


def test_tray_spawn_frozen_does_not_pass_module_flag(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", EXE)
    calls = _record_popen(monkeypatch)

    _spawn(["--quick-compress", r"D:\in.mkv"])
    _spawn([])

    assert calls == [
        [EXE, "--quick-compress", r"D:\in.mkv"],
        [EXE],
    ]
    assert "-m" not in calls[0]
    assert "-m" not in calls[1]


def test_tray_spawn_unfrozen_still_uses_the_module(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", PYTHON)
    calls = _record_popen(monkeypatch)

    _spawn(["--open", "clip.mp4"])

    assert calls == [[PYTHON, "-m", "video_compressor", "--open", "clip.mp4"]]


def test_spawn_app_passes_argv_to_popen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", EXE)
    calls = _record_popen(monkeypatch)

    proc = spawn_app(["--tray"])

    assert calls == [[EXE, "--tray"]]
    assert proc is not None
    assert "-m" not in calls[0]


def test_qt_quick_window_open_uses_frozen_exe_argv(monkeypatch):
    pytest.importorskip("PySide6")
    from video_compressor.gui.qt_shell.quick_window import QuickCompressWindow

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", EXE)
    calls = _record_popen(monkeypatch)
    window = QuickCompressWindow.__new__(QuickCompressWindow)
    window.input_file = Path(r"D:\clip.mp4")
    window.close = lambda: calls.append(["closed"])

    window._open_full_app()

    assert calls[0] == [EXE, "--open", r"D:\clip.mp4"]
    assert "-m" not in calls[0]
    assert calls[1] == ["closed"]


def test_qt_quick_window_open_unfrozen_uses_module(monkeypatch):
    pytest.importorskip("PySide6")
    from video_compressor.gui.qt_shell.quick_window import QuickCompressWindow

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", PYTHON)
    calls = _record_popen(monkeypatch)
    window = QuickCompressWindow.__new__(QuickCompressWindow)
    window.input_file = Path("clip.mp4")
    window.close = lambda: None

    window._open_full_app()

    assert calls == [[PYTHON, "-m", "video_compressor", "--open", "clip.mp4"]]


def test_relaunch_helpers_do_not_hardcode_module_argv():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "video_compressor/gui/tray.py",
        "video_compressor/gui/qt_shell/quick_window.py",
        "video_compressor/gui/quick_compress.py",
    ):
        text = (root / relative).read_text(encoding="utf-8")
        assert '["-m", "video_compressor"' not in text
        assert "spawn_app(" in text


def test_relaunch_does_not_touch_subprocess_until_spawn(monkeypatch):
    """Building argv must not start a process."""

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", EXE)

    def fail_popen(*args, **kwargs):
        raise AssertionError("Popen should not run while building argv")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    assert app_relaunch_argv(["--tray"]) == [EXE, "--tray"]
