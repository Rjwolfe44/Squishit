"""Relaunch SquishIt from the tray or a child window.

A frozen PyInstaller build (``SquishIt.exe``) has no ``python -m`` entry.
``register_startup`` and the Explorer context menu already start that exe
with flags the packaged entry understands (``--tray``, ``--open``,
``--quick-compress``). Source runs keep ``python -m video_compressor``.
"""

from __future__ import annotations

import subprocess
import sys


def is_frozen() -> bool:
    """True when this process is a packaged executable."""

    return bool(getattr(sys, "frozen", False))


def app_relaunch_argv(args: list[str] | None = None) -> list[str]:
    """Argv for another SquishIt process.

    Frozen: ``[SquishIt.exe, ...flags]``.
    Source: ``[python, "-m", "video_compressor", ...flags]``.
    """

    extra = list(args or [])
    if is_frozen():
        return [sys.executable, *extra]
    return [sys.executable, "-m", "video_compressor", *extra]


def spawn_app(args: list[str] | None = None) -> subprocess.Popen:
    """Start another SquishIt process and return it."""

    return subprocess.Popen(app_relaunch_argv(args))
