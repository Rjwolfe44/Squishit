"""System tray integration for SquishIt."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw
import pystray

from ..config import APP_NAME


def _create_icon_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (20, 24, 32, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=14, fill=(47, 128, 237, 255))
    draw.text((20, 16), "S", fill=(255, 255, 255, 255))
    return image


def run_tray(startup_file: Optional[Path] = None) -> None:
    from .main_window import run_app
    from .quick_compress import run_quick_compress

    def open_app(_icon, _item) -> None:
        threading.Thread(target=run_app, daemon=True).start()

    def quick_open(_icon, _item) -> None:
        if startup_file:
            threading.Thread(target=lambda: run_quick_compress(startup_file), daemon=True).start()
        else:
            threading.Thread(target=run_app, daemon=True).start()

    def quit_app(_icon, _item) -> None:
        icon.stop()

    icon = pystray.Icon(
        APP_NAME,
        _create_icon_image(),
        APP_NAME,
        menu=pystray.Menu(
            pystray.MenuItem("Open SquishIt", open_app, default=True),
            pystray.MenuItem("Quick Compress", quick_open),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app),
        ),
    )
    icon.run()
