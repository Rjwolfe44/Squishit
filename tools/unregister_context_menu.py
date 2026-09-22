"""Remove SquishIt Windows Explorer context-menu commands."""

from __future__ import annotations

import winreg

APP_LABEL = "SquishIt"
ROOT = winreg.HKEY_CURRENT_USER
ROOT_PREFIX = r"Software\Classes"
COMPRESS_VERB = "SquishItCompress"
OPEN_VERB = "SquishItOpen"


def _classes_path(path: str) -> str:
    return ROOT_PREFIX + "\\" + path


def _delete_tree(root: int, path: str) -> None:
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                    _delete_tree(root, path + "\\" + child)
                except OSError:
                    break
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        return


def unregister_context_menu() -> None:
    _delete_tree(ROOT, _classes_path(rf"*\shell\{APP_LABEL}"))
    _delete_tree(ROOT, _classes_path(rf"*\shell\{COMPRESS_VERB}"))
    _delete_tree(ROOT, _classes_path(rf"*\shell\{OPEN_VERB}"))
    print(f"Removed context menu for {APP_LABEL}")


if __name__ == "__main__":
    unregister_context_menu()
