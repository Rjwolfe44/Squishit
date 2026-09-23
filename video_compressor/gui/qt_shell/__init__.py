"""PySide6 desktop shell. The default ``python -m video_compressor`` path.

Submodules such as ``encode_form`` stay importable without constructing a
window. The window classes load on first attribute access.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "QuickCompressWindow",
    "SquishItWindow",
    "run_app",
    "run_quick_compress",
]


def __getattr__(name: str) -> Any:
    if name in {"SquishItWindow", "run_app"}:
        from .main_window import SquishItWindow, run_app

        return {"SquishItWindow": SquishItWindow, "run_app": run_app}[name]
    if name in {"QuickCompressWindow", "run_quick_compress"}:
        from .quick_window import QuickCompressWindow, run_quick_compress

        return {
            "QuickCompressWindow": QuickCompressWindow,
            "run_quick_compress": run_quick_compress,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
