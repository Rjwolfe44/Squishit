"""Home-path hardware card shared by the main window and Quick Compress."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from ..hw_status import HwStatusText


def _polish(widget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class HardwareStatusCard(QWidget):
    """Badge plus the two plain sentences from ``describe_hw_status``."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("hwCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.badge = QLabel("Checking…")
        self.badge.setObjectName("hwBadge")
        self.badge.setProperty("state", "pending")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            QSizePolicy.Policy.Fixed,
        )
        row.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.found = QLabel("Checking this PC for a hardware encoder.")
        self.found.setObjectName("hwFound")
        self.found.setWordWrap(True)
        self.using = QLabel("")
        self.using.setObjectName("hwUsing")
        self.using.setWordWrap(True)
        self.activity = QLabel("")
        self.activity.setObjectName("activityStatus")
        self.activity.setWordWrap(True)
        self.activity.setVisible(False)
        text.addWidget(self.found)
        text.addWidget(self.using)
        text.addWidget(self.activity)
        row.addLayout(text, 1)
        layout.addLayout(row)

    def apply(self, status: HwStatusText) -> None:
        self.badge.setText(status.badge)
        self.badge.setProperty("state", status.state)
        self.badge.setToolTip(status.tooltip)
        _polish(self.badge)
        self.found.setText(status.found)
        self.using.setText(status.using)
        if status.tooltip:
            self.using.setToolTip(status.tooltip)

    def set_activity(self, text: str) -> None:
        self.activity.setText(text or "")
        self.activity.setVisible(bool(text))
