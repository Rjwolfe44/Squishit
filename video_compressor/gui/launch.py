"""Choose the desktop shell.

Qt is the default. ``SQUISHIT_UI=ctk`` still opens the CustomTkinter windows
when that extra is installed (``pip install -e ".[legacy-ui]"``). Packaged
builds follow the Qt import and do not bundle CustomTkinter.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Optional

LEGACY_UI_VALUES = {"ctk", "customtkinter", "legacy"}


def ui_backend() -> str:
    """``qt`` unless ``SQUISHIT_UI`` asks for the legacy CustomTkinter shell."""

    value = os.environ.get("SQUISHIT_UI", "qt").strip().lower()
    if value in LEGACY_UI_VALUES:
        return "ctk"
    return "qt"


def _legacy_attr(module_name: str, attr: str):
    # importlib keeps the CustomTkinter modules out of the packaged import graph.
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _legacy_module(leaf: str) -> str:
    return ".".join(("video_compressor", "gui", leaf))


def run_app(startup_files: Optional[list[Path]] = None, auto_start: bool = False):
    """Launch the desktop app. Qt unless ``SQUISHIT_UI`` selects CustomTkinter."""

    if ui_backend() == "ctk":
        run_legacy = _legacy_attr(_legacy_module("main_window"), "run_app")
        return run_legacy(startup_files=startup_files, auto_start=auto_start)
    from video_compressor.gui.qt_shell import run_app as run_qt

    return run_qt(startup_files=startup_files, auto_start=auto_start)


def run_quick_compress(input_file: Path):
    """Launch Quick Compress. Qt unless ``SQUISHIT_UI`` selects CustomTkinter."""

    if ui_backend() == "ctk":
        run_legacy = _legacy_attr(_legacy_module("quick_compress"), "run_quick_compress")
        return run_legacy(input_file)
    from video_compressor.gui.qt_shell import run_quick_compress as run_qt

    return run_qt(input_file)
