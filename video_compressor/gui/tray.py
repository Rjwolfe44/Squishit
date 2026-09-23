"""System tray integration for SquishIt."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from ..config import APP_NAME
from .relaunch import spawn_app


def _create_icon_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (20, 24, 32, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 56, 56), radius=14, fill=(47, 128, 237, 255))
    draw.text((20, 16), "S", fill=(255, 255, 255, 255))
    return image


def _spawn(args: list[str]) -> None:
    """Start a Qt process. The tray icon keeps the GUI off its own thread.

    Frozen ``SquishIt.exe`` is launched with the flags it already accepts.
    Source runs still use ``python -m video_compressor``.
    """

    spawn_app(args)


def run_tray(startup_file: Path | None = None) -> None:
    import pystray

    def open_app(_icon, _item) -> None:
        _spawn([])

    def quick_open(_icon, _item) -> None:
        if startup_file:
            _spawn(["--quick-compress", str(startup_file)])
        else:
            _spawn([])

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
