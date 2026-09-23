"""Dark Fusion theme for the PySide6 shell.

Scrollbars stay high-contrast on purpose. The CustomTkinter shell used a
near-invisible thumb on a near-black track, which made long settings and
queues hard to scan.
"""

from __future__ import annotations

DARK_STYLESHEET = """
QWidget {
    background: #0d0d0f;
    color: #fafafa;
    font-size: 13px;
}
QMainWindow, QDialog {
    background: #0d0d0f;
}
QFrame#panel {
    background: #18181b;
    border: 1px solid #3f3f46;
    border-radius: 12px;
}
QFrame#dropZone {
    background: #18181b;
    border: 1px dashed #a1a1aa;
    border-radius: 12px;
}
QFrame#dropZone[active="true"] {
    background: #1d3461;
    border: 1px solid #3b82f6;
}
QFrame#card {
    background: #18181b;
    border: 1px solid #3f3f46;
    border-radius: 10px;
}
QLabel#title {
    font-size: 22px;
    font-weight: 700;
    background: transparent;
}
QLabel#section {
    font-size: 12px;
    font-weight: 700;
    color: #d4d4d8;
    background: transparent;
    letter-spacing: 0.4px;
}
QLabel#muted, QLabel#hint {
    color: #d4d4d8;
    background: transparent;
}
QLabel#hint {
    font-size: 12px;
}
QPushButton {
    background: #27272a;
    color: #fafafa;
    border: 1px solid #3f3f46;
    border-radius: 8px;
    padding: 8px 12px;
}
QPushButton:hover {
    background: #3f3f46;
}
QPushButton:disabled {
    color: #a1a1aa;
    background: #18181b;
}
QPushButton#primary {
    background: #3b82f6;
    color: #ffffff;
    border: none;
    font-weight: 700;
    padding: 10px 16px;
}
QPushButton#primary:hover {
    background: #60a5fa;
}
QPushButton#primary:disabled {
    background: #1d3461;
    color: #d4d4d8;
}
QPushButton#pill {
    font-weight: 600;
    padding: 8px 14px;
}
QPushButton#pill[selected="true"] {
    background: #3b82f6;
    color: #ffffff;
    border: 1px solid #60a5fa;
}
QLineEdit, QComboBox, QSpinBox {
    background: #27272a;
    color: #fafafa;
    border: 1px solid #52525b;
    border-radius: 7px;
    padding: 5px 8px;
    min-height: 22px;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: #18181b;
    color: #fafafa;
    selection-background-color: #3b82f6;
    selection-color: #ffffff;
    border: 1px solid #52525b;
    outline: none;
}
QCheckBox {
    background: transparent;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #d4d4d8;
    border-radius: 4px;
    background: #27272a;
}
QCheckBox::indicator:checked {
    background: #3b82f6;
    border: 1px solid #93c5fd;
}
QProgressBar {
    background: #27272a;
    border: 1px solid #52525b;
    border-radius: 6px;
    text-align: center;
    color: #fafafa;
    min-height: 14px;
}
QProgressBar::chunk {
    background: #3b82f6;
    border-radius: 5px;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QSplitter::handle {
    background: #3f3f46;
}
QScrollBar:vertical {
    background: #3f3f46;
    width: 14px;
    margin: 2px;
    border-radius: 7px;
}
QScrollBar::handle:vertical {
    background: #d4d4d8;
    min-height: 36px;
    border-radius: 6px;
    margin: 2px;
}
QScrollBar::handle:vertical:hover {
    background: #fafafa;
}
QScrollBar:horizontal {
    background: #3f3f46;
    height: 14px;
    margin: 2px;
    border-radius: 7px;
}
QScrollBar::handle:horizontal {
    background: #d4d4d8;
    min-width: 36px;
    border-radius: 6px;
    margin: 2px;
}
QScrollBar::handle:horizontal:hover {
    background: #fafafa;
}
QScrollBar::add-line, QScrollBar::sub-line,
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
    height: 0;
    width: 0;
}
QMenu {
    background: #18181b;
    color: #fafafa;
    border: 1px solid #52525b;
    padding: 4px;
}
QMenu::item {
    padding: 6px 18px;
    background: transparent;
}
QMenu::item:selected {
    background: #3b82f6;
    color: #ffffff;
}
QToolTip {
    background: #27272a;
    color: #fafafa;
    border: 1px solid #d4d4d8;
    padding: 4px;
}
"""


def apply_theme(app) -> None:
    """Fusion plus the dark sheet. Safe to call once per process."""

    app.setStyle("Fusion")
    app.setStyleSheet(DARK_STYLESHEET)
