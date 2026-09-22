"""Shared GUI scaling helpers for high-DPI displays."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Optional

import customtkinter as ctk

UI_SCALE_OPTIONS = ["auto", "100%", "115%", "130%", "145%"]


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def enable_high_dpi_mode() -> None:
    """Enable the sharpest DPI mode Windows exposes before any root window exists."""

    if os.name != "nt":
        return

    try:
        from ctypes import windll

        try:
            windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _system_scale_factor() -> float:
    if os.name != "nt":
        return 1.0

    try:
        from ctypes import windll

        try:
            dpi = windll.user32.GetDpiForSystem()
            if dpi:
                return max(1.0, dpi / 96.0)
        except Exception:
            factor = windll.shcore.GetScaleFactorForDevice(0)
            if factor:
                return max(1.0, factor / 100.0)
    except Exception:
        pass
    return 1.0


def resolve_ui_scale(mode: Optional[str], screen_height: int = 0) -> float:
    """Resolve the configured UI scale into a multiplier."""

    normalized = (mode or "auto").strip().lower()
    if normalized.endswith("%"):
        try:
            return _clamp(float(normalized[:-1]) / 100.0, 0.85, 1.45)
        except ValueError:
            normalized = "auto"

    if normalized != "auto":
        presets = {
            "compact": 0.92,
            "normal": 1.0,
            "large": 1.15,
            "larger": 1.3,
        }
        return presets.get(normalized, 1.0)

    dpi_scale = _system_scale_factor()
    height_scale = 1.0
    if screen_height >= 2160:
        height_scale = 1.26
    elif screen_height >= 1800:
        height_scale = 1.18
    elif screen_height >= 1440:
        height_scale = 1.08
    elif 0 < screen_height <= 900:
        height_scale = 0.95

    return _clamp(max(dpi_scale, height_scale), 0.95, 1.35)


def apply_customtkinter_scale(scale: float) -> None:
    """Apply a scale multiplier to CustomTkinter widgets and windows."""

    ctk.set_widget_scaling(scale)
    ctk.set_window_scaling(scale)


def apply_tk_scaling(root, scale: float) -> None:
    """Apply the same scale multiplier to classic Tk widgets."""

    try:
        root.tk.call("tk", "scaling", max(1.0, (96.0 / 72.0) * scale))
    except Exception:
        pass


def scaled(value: int, scale: float) -> int:
    return max(1, int(round(value * scale)))


@dataclass(frozen=True)
class WindowLayout:
    width: int
    height: int
    min_width: int
    min_height: int


def resolve_main_window_layout(
    screen_width: int,
    screen_height: int,
    ui_scale: float,
    configured_width: Optional[int] = None,
    configured_height: Optional[int] = None,
) -> WindowLayout:
    """Pick a responsive initial and minimum size for the main window."""

    if screen_height >= 1800:
        base_width, base_height = 1840, 1180
    elif screen_height >= 1440:
        base_width, base_height = 1560, 1020
    else:
        base_width, base_height = 1344, 880

    usable_width = max(1080, screen_width - 72)
    usable_height = max(760, screen_height - 72)

    min_width = min(usable_width, max(1080, scaled(1100, ui_scale)))
    min_height = min(usable_height, max(740, scaled(780, ui_scale)))

    width = configured_width or scaled(base_width, ui_scale)
    height = configured_height or scaled(base_height, ui_scale)
    width = min(usable_width, max(min_width, width))
    height = min(usable_height, max(min_height, height))
    return WindowLayout(width=width, height=height, min_width=min_width, min_height=min_height)


def center_window(window, master=None, width: Optional[int] = None, height: Optional[int] = None) -> None:
    """Center a toplevel on its parent or on the current screen."""

    window.update_idletasks()
    resolved_width = width or window.winfo_width()
    resolved_height = height or window.winfo_height()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()

    if master is not None and getattr(master, "winfo_exists", lambda: False)():
        x = master.winfo_rootx() + (master.winfo_width() - resolved_width) // 2
        y = master.winfo_rooty() + (master.winfo_height() - resolved_height) // 2
    else:
        x = (screen_width - resolved_width) // 2
        y = (screen_height - resolved_height) // 2

    x = max(16, min(x, screen_width - resolved_width - 16))
    y = max(16, min(y, screen_height - resolved_height - 16))
    window.geometry(f"{resolved_width}x{resolved_height}+{x}+{y}")


def apply_dialog_geometry(
    window,
    master,
    *,
    base_width: int,
    base_height: int,
    min_width: Optional[int] = None,
    min_height: Optional[int] = None,
    resizable: bool = True,
) -> float:
    """Scale and center a dialog using the main window's UI scale preference."""

    scale_mode = getattr(getattr(master, "app_config", None), "ui_scale_mode", "auto")
    screen_height = 0
    try:
        screen_height = master.winfo_screenheight()
    except Exception:
        try:
            screen_height = window.winfo_screenheight()
        except Exception:
            screen_height = 0

    scale = resolve_ui_scale(scale_mode, screen_height=screen_height)
    width = scaled(base_width, scale)
    height = scaled(base_height, scale)
    minimum_width = scaled(min_width or int(base_width * 0.88), scale)
    minimum_height = scaled(min_height or int(base_height * 0.88), scale)

    max_width = max(minimum_width, window.winfo_screenwidth() - 72)
    max_height = max(minimum_height, window.winfo_screenheight() - 72)
    width = min(width, max_width)
    height = min(height, max_height)
    minimum_width = min(minimum_width, width)
    minimum_height = min(minimum_height, height)

    if resizable:
        window.minsize(minimum_width, minimum_height)
    else:
        window.resizable(False, False)

    center_window(window, master=master, width=width, height=height)
    return scale


enable_high_dpi_mode()