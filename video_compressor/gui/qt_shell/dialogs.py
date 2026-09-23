"""Qt stand-ins for the few dialogs the shell needs itself."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox, QWidget


def qt_yes_no_ask(title: str, body: str, parent=None, default: str = "no") -> bool:
    """Yes/No box used by the software-fallback ask. Default is No.

    No, or closing the box, keeps the hardware result. The compressor treats
    that the same way as the Tk dialog.
    """

    buttons = QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    default_button = (
        QMessageBox.StandardButton.No
        if str(default).lower() == "no"
        else QMessageBox.StandardButton.Yes
    )
    answer = QMessageBox.question(parent, title, body, buttons, default_button)
    return answer == QMessageBox.StandardButton.Yes


def show_about(parent: QWidget, title: str, body: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(body)
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    box.exec()
