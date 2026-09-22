"""
GUI module for the video compressor.
"""

from .main_window import MainWindow
from .quick_compress import run_quick_compress
from .widgets import (
    DropZone,
    FileQueueCard,
    VideoCard,       # alias for FileQueueCard
    ProgressCard,
    ResultCard,
    ProfileBar,
    SettingsPanel,
    HardwareBadge,
    COLORS,
)

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