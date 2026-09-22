#!/usr/bin/env python3
"""Entry point for running the package directly."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from video_compressor.gui.ipc import send_open_request


def main() -> None:
    parser = argparse.ArgumentParser(prog="squishit")
    parser.add_argument("--open", dest="open_file", type=Path)
    parser.add_argument("--quick-compress", dest="quick_compress", type=Path)
    parser.add_argument("--tray", action="store_true", help="Run the system tray app")
    parser.add_argument("--register-context", action="store_true", help="Register Explorer context menu")
    parser.add_argument("--unregister-context", action="store_true", help="Remove Explorer context menu")
    parser.add_argument("--register-startup", action="store_true", help="Register tray startup")
    parser.add_argument("--unregister-startup", action="store_true", help="Remove tray startup")
    args = parser.parse_args()

    if args.register_context:
        from tools.register_context_menu import register_context_menu

        register_context_menu(Path(sys.executable).resolve())
        return

    if args.unregister_context:
        from tools.unregister_context_menu import unregister_context_menu

        unregister_context_menu()
        return

    if args.register_startup:
        from tools.register_startup import main as register_startup

        register_startup()
        return

    if args.unregister_startup:
        from tools.unregister_startup import main as unregister_startup

        unregister_startup()
        return

    startup_files = []

    if args.tray:
        from video_compressor.gui.tray import run_tray

        run_tray(args.open_file or args.quick_compress)
        return
    if args.open_file:
        if send_open_request([args.open_file]):
            return
        startup_files = [args.open_file]
    elif args.quick_compress:
        from video_compressor.gui.quick_compress import run_quick_compress

        run_quick_compress(args.quick_compress)
        return

    from video_compressor.gui.main_window import run_app

    run_app(startup_files=startup_files, auto_start=False)


if __name__ == "__main__":
    main()