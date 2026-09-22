"""Register SquishIt Windows Explorer context-menu commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
import winreg

APP_LABEL = "SquishIt"
ROOT = winreg.HKEY_CURRENT_USER
ROOT_PREFIX = r"Software\Classes"
COMPRESS_VERB = "SquishItCompress"
OPEN_VERB = "SquishItOpen"
SUPPORTED_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".flv", ".m4v",
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif",
}


def _build_launch_command(executable: Path, action: str) -> str:
    is_python_launcher = executable.stem.lower() in {"python", "pythonw"}
    if getattr(sys, "frozen", False) or (executable.suffix.lower() == ".exe" and not is_python_launcher):
        return f'"{executable}" {action} "%1"'
    # Source mode: pass the absolute path to the project root __main__.py so that
    # Python sets sys.path[0] to the project root, letting `video_compressor`
    # resolve without relying on CWD (which Explorer does not set to the project).
    # Use pythonw.exe to suppress the console window.
    pythonw = executable.parent / "pythonw.exe"
    if not pythonw.exists():
        pythonw = executable
    main_py = Path(__file__).resolve().parent.parent / "__main__.py"
    return f'"{pythonw}" "{main_py}" {action} "%1"'


def _write_key(root: int, path: str, name: str | None, value: str) -> None:
    key = winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE)
    with key:
        winreg.SetValueEx(key, name or "", 0, winreg.REG_SZ, value)


def _classes_path(path: str) -> str:
    return ROOT_PREFIX + "\\" + path


def register_context_menu(executable: Path) -> None:
    compress_base = rf"*\shell\{COMPRESS_VERB}"
    _write_key(ROOT, _classes_path(compress_base), None, "Compress with SquishIt")
    _write_key(ROOT, _classes_path(compress_base), "Icon", f'"{executable}",0')
    _write_key(
        ROOT,
        _classes_path(compress_base + r"\command"),
        None,
        _build_launch_command(executable, "--quick-compress"),
    )

    open_base = rf"*\shell\{OPEN_VERB}"
    _write_key(ROOT, _classes_path(open_base), None, "Open in SquishIt")
    _write_key(ROOT, _classes_path(open_base), "Icon", f'"{executable}",0')
    _write_key(
        ROOT,
        _classes_path(open_base + r"\command"),
        None,
        _build_launch_command(executable, "--open"),
    )

    print(f"Registered context menu for {APP_LABEL} using {executable}")
    print("Note: SquishIt filters unsupported file types at runtime.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register SquishIt context-menu entries")
    parser.add_argument("--exe", type=Path, default=Path(sys.executable), help="Path to SquishIt executable")
    args = parser.parse_args()
    register_context_menu(args.exe.resolve())
