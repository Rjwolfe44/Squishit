"""
GUI module for the video compressor.

Submodules stay importable without CustomTkinter. The window and widgets
load on first attribute access.
"""

from __future__ import annotations

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
        from .main_window import MainWindow

        return MainWindow
    if name == "run_quick_compress":
        from .quick_compress import run_quick_compress

        return run_quick_compress
    if name in _WIDGET_EXPORTS:
        from . import widgets

        if name == "VideoCard":
            return widgets.FileQueueCard
        return getattr(widgets, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
