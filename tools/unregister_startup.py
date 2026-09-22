"""Remove the SquishIt tray startup launcher."""

from __future__ import annotations

import os
from pathlib import Path

STARTUP_FILE = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "SquishIt Tray.bat"


def main() -> int:
    STARTUP_FILE.unlink(missing_ok=True)
    print(f"Removed {STARTUP_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
