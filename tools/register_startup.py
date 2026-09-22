"""Register SquishIt tray startup in the user's Startup folder."""

from __future__ import annotations

import os
import sys
from pathlib import Path

STARTUP_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def main() -> int:
    STARTUP_DIR.mkdir(parents=True, exist_ok=True)
    shortcut = STARTUP_DIR / "SquishIt Tray.bat"
    if getattr(sys, "frozen", False):
        command = f'@echo off\r\nstart "" "{Path(sys.executable)}" --tray\r\n'
    else:
        command = f'@echo off\r\nstart "" "{Path(sys.executable)}" -m video_compressor --tray\r\n'
    shortcut.write_text(command, encoding="utf-8")
    print(f"Startup launcher created at {shortcut}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
