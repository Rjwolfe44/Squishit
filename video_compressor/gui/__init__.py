"""
GUI package.

The default shell is PySide6 (``video_compressor.gui.qt_shell``). Importing
this package does not load Qt or CustomTkinter. ``MainWindow`` resolves to
the Qt window. The legacy CustomTkinter widgets stay in ``widgets.py`` and
load only when something asks for them.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "MainWindow",
    "run_quick_compress",
    "DropZone",
    "FileQueueCard",
    "VideoCard",
    "ProgressCard",
    "ResultCard",
    "ProfileBar",
    "SettingsPanel",
    "HardwareBadge",
    "COLORS",
]

_WIDGET_EXPORTS = {
    "DropZone",
    "FileQueueCard",
    "VideoCard",
    "ProgressCard",
    "ResultCard",
    "ProfileBar",
    "SettingsPanel",
    "HardwareBadge",
    "COLORS",
}


def __getattr__(name: str) -> Any:
    if name == "MainWindow":
        from .qt_shell.main_window import SquishItWindow

        return SquishItWindow
    if name == "run_quick_compress":
        from .launch import run_quick_compress

        return run_quick_compress
    if name in _WIDGET_EXPORTS:
        widgets = importlib.import_module(
            ".".join(("video_compressor", "gui", "widgets"))
        )
        if name == "VideoCard":
            return widgets.FileQueueCard
        return getattr(widgets, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
