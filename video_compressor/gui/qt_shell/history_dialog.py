"""History viewer for the Qt shell.

Shows past compressions newest first in a table, with a search box,
"Open folder" (select the finished file in the system file manager) and
"Clear history". It only needs ``get_entries()`` and ``clear()`` from the
history object, so any ``HistoryManager``-like object works.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...config import APP_NAME, get_config
from ...core.history import HistoryEntry, HistoryManager
from ...core.utils import format_size

logger = logging.getLogger(__name__)

COLUMNS = ("File", "Profile", "Format", "Before → After", "Saved", "When")
COL_FILE, COL_PROFILE, COL_FORMAT, COL_SIZES, COL_SAVED, COL_WHEN = range(6)

# Columns where the first click should sort biggest/newest first.
_DESCENDING_FIRST = {COL_SIZES, COL_SAVED, COL_WHEN}

EMPTY_TITLE = "Nothing here yet"
EMPTY_HINT = "Files you compress will show up here."
NO_MATCH_TITLE = "No matches"

_TABLE_QSS = """
QTableWidget {
    background: #18181b;
    border: 1px solid #3f3f46;
    border-radius: 10px;
    gridline-color: #27272a;
    selection-background-color: #1d3461;
    selection-color: #fafafa;
    outline: none;
}
QTableWidget::item {
    padding: 6px 8px;
    border-bottom: 1px solid #27272a;
}
QTableWidget::item:selected {
    background: #1d3461;
    color: #fafafa;
}
QHeaderView::section {
    background: #27272a;
    color: #d4d4d8;
    padding: 6px 8px;
    border: none;
    border-bottom: 1px solid #3f3f46;
    font-weight: 700;
}
QTableCornerButton::section {
    background: #27272a;
    border: none;
}
"""

_FORMAT_NAMES = {
    "h264": "H.264",
    "x264": "H.264",
    "avc": "H.264",
    "libx264": "H.264",
    "hevc": "HEVC",
    "h265": "HEVC",
    "x265": "HEVC",
    "libx265": "HEVC",
    "vp9": "VP9",
    "libvpx-vp9": "VP9",
    "av1": "AV1",
    "libaom-av1": "AV1",
    "svt-av1": "AV1 (best compression)",
    "libsvtav1": "AV1 (best compression)",
}


def friendly_format(codec: Any) -> str:
    """Readable name for the video format stored in a history entry."""

    raw = str(codec or "").strip()
    if not raw:
        return "—"
    key = raw.lower()
    if key in _FORMAT_NAMES:
        return _FORMAT_NAMES[key]
    for marker, label in (("hevc", "HEVC"), ("265", "HEVC"), ("264", "H.264")):
        if marker in key:
            return label
    if "av1" in key:
        return "AV1"
    return raw.upper()


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a stored ISO timestamp into a naive local datetime (or ``None``)."""

    try:
        stamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is not None:
        try:
            stamp = stamp.astimezone().replace(tzinfo=None)
        except (OSError, ValueError, OverflowError):
            stamp = stamp.replace(tzinfo=None)
    return stamp


def _clock(stamp: datetime) -> str:
    hour = stamp.hour % 12 or 12
    return f"{hour}:{stamp.minute:02d} {'AM' if stamp.hour < 12 else 'PM'}"


def format_when(value: Any, now: Optional[datetime] = None) -> str:
    """"Today, 3:45 PM", "Yesterday, 9:10 AM" or "Oct 3, 2026, 9:10 AM"."""

    stamp = parse_timestamp(value)
    if stamp is None:
        return str(value or "—")
    now = now or datetime.now()
    today = now.date()
    if stamp.date() == today:
        day = "Today"
    elif stamp.date() == today - timedelta(days=1):
        day = "Yesterday"
    else:
        day = f"{stamp:%b} {stamp.day}, {stamp.year}"
    return f"{day}, {_clock(stamp)}"


def default_history() -> HistoryManager:
    """The app's real history log, sized like the main window does."""

    try:
        return HistoryManager(max_entries=get_config().history_max_entries)
    except Exception:
        return HistoryManager()


def reveal_in_file_manager(path: Any) -> str:
    """Show *path* in the system file manager.

    Returns an empty string on success, or a short plain-language message
    when the file is gone or the file manager could not be started. If only
    the file is missing, its folder is opened and the message says so.
    """

    target = Path(str(path))
    try:
        exists = target.exists()
        folder_exists = target.parent.is_dir()
    except OSError:
        exists = folder_exists = False
    if not exists and not folder_exists:
        return "That file and its folder are gone, so there is nothing to open."
    note = "" if exists else (
        "The file was moved or deleted, so its folder was opened instead."
    )
    platform = _platform()
    try:
        if exists and platform == "win32":
            # Explorer needs backslashes and one quoted argument after the comma.
            subprocess.Popen(f'explorer /select,"{os.path.normpath(str(target))}"')
        elif exists and platform == "darwin":
            subprocess.Popen(["open", "-R", str(target)])
        elif not QDesktopServices.openUrl(QUrl.fromLocalFile(str(target.parent))):
            return "Couldn't open that folder."
    except OSError as exc:
        return f"Couldn't open that folder: {exc}"
    return note


def _platform() -> str:
    return sys.platform


def _sort_key(entry: HistoryEntry, column: int):
    if column == COL_FILE:
        return Path(str(entry.input_path)).name.lower()
    if column == COL_PROFILE:
        return str(entry.profile_name or "").lower()
    if column == COL_FORMAT:
        return friendly_format(entry.codec).lower()
    if column == COL_SIZES:
        return float(entry.original_size_bytes or 0)
    if column == COL_SAVED:
        return float(entry.reduction_pct or 0)
    return parse_timestamp(entry.timestamp) or datetime.min


def _when_key(entry: HistoryEntry) -> datetime:
    return parse_timestamp(entry.timestamp) or datetime.min


class HistoryDialog(QDialog):
    """Past compressions, newest first, with search, open-folder and clear."""

    def __init__(self, parent: Optional[QWidget] = None, history: Any = None):
        super().__init__(parent)
        self._history = history if history is not None else default_history()
        self._entries: List[HistoryEntry] = []
        self._shown: List[HistoryEntry] = []
        self._sort_col = COL_WHEN
        self._sort_desc = True
        self.setWindowTitle(f"{APP_NAME} history")
        self.setMinimumSize(760, 440)
        self.resize(900, 560)
        self._build()
        self.refresh()

    # ── layout ───────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        title = QLabel("History")
        title.setObjectName("title")
        outer.addWidget(title)
        self._summary = QLabel("")
        self._summary.setObjectName("hint")
        outer.addWidget(self._summary)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Search by file, profile or format")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        outer.addWidget(self._filter)

        self._stack = QStackedWidget()
        outer.addWidget(self._stack, 1)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setObjectName("historyTable")
        self._table.setStyleSheet(_TABLE_QSS)
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(38)
        header = self._table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.setSectionResizeMode(COL_FILE, QHeaderView.ResizeMode.Stretch)
        for column in (COL_PROFILE, COL_FORMAT, COL_SIZES, COL_SAVED, COL_WHEN):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.sectionClicked.connect(self._on_header_clicked)
        self._table.itemSelectionChanged.connect(self._update_buttons)
        self._table.itemDoubleClicked.connect(lambda _item: self._open_folder())
        self._stack.addWidget(self._table)

        empty = QFrame()
        empty.setObjectName("card")
        empty_layout = QVBoxLayout(empty)
        empty_layout.setContentsMargins(24, 24, 24, 24)
        empty_layout.addStretch(1)
        self._empty_title = QLabel(EMPTY_TITLE)
        self._empty_title.setObjectName("title")
        self._empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint = QLabel(EMPTY_HINT)
        self._empty_hint.setObjectName("hint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setWordWrap(True)
        empty_layout.addWidget(self._empty_title)
        empty_layout.addWidget(self._empty_hint)
        empty_layout.addStretch(1)
        self._empty_page = empty
        self._stack.addWidget(empty)

        self._status = QLabel("")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        buttons = QHBoxLayout()
        self._open_btn = QPushButton("Open folder")
        self._open_btn.setToolTip("Show the finished file in its folder")
        self._open_btn.clicked.connect(self._open_folder)
        buttons.addWidget(self._open_btn)
        self._clear_btn = QPushButton("Clear history")
        self._clear_btn.setToolTip("Forget this list. Your compressed files stay where they are.")
        self._clear_btn.clicked.connect(self._clear_history)
        buttons.addWidget(self._clear_btn)
        buttons.addStretch(1)
        self._close_btn = QPushButton("Close")
        self._close_btn.setObjectName("primary")
        self._close_btn.setDefault(True)
        self._close_btn.clicked.connect(self.accept)
        buttons.addWidget(self._close_btn)
        outer.addLayout(buttons)

    # ── data ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Reload entries from the history object and redraw."""

        try:
            entries = list(self._history.get_entries())
        except Exception:
            logger.debug("Could not read history", exc_info=True)
            entries = []
        # Newest first no matter how the history object orders them. The sort
        # is stable, so entries without a readable date keep their order.
        self._entries = sorted(entries, key=_when_key, reverse=True)
        if not self._entries and self._filter.text():
            self._filter.blockSignals(True)
            self._filter.clear()
            self._filter.blockSignals(False)
        self._update_summary()
        self._apply_filter()

    def _update_summary(self) -> None:
        count = len(self._entries)
        if not count:
            self._summary.setText("No files compressed yet")
            return
        saved = sum(
            max(0, int(e.original_size_bytes or 0) - int(e.compressed_size_bytes or 0))
            for e in self._entries
        )
        noun = "file" if count == 1 else "files"
        self._summary.setText(f"{count} {noun} compressed · {format_size(saved)} saved")

    @staticmethod
    def _matches(entry: HistoryEntry, needle: str) -> bool:
        haystack = " ".join(
            (
                Path(str(entry.input_path)).name,
                Path(str(entry.output_path)).name,
                str(entry.profile_name or ""),
                str(entry.codec or ""),
                friendly_format(entry.codec),
            )
        ).lower()
        return all(part in haystack for part in needle.split())

    def _apply_filter(self, *_args) -> None:
        needle = self._filter.text().strip().lower()
        shown = [e for e in self._entries if not needle or self._matches(e, needle)]
        shown.sort(
            key=lambda e: _sort_key(e, self._sort_col),
            reverse=self._sort_desc,
        )
        self._shown = shown
        self._fill_table()
        if shown:
            self._stack.setCurrentWidget(self._table)
        else:
            if self._entries:
                self._empty_title.setText(NO_MATCH_TITLE)
                self._empty_hint.setText(f"Nothing in your history matches “{self._filter.text().strip()}”.")
            else:
                self._empty_title.setText(EMPTY_TITLE)
                self._empty_hint.setText(EMPTY_HINT)
            self._stack.setCurrentWidget(self._empty_page)
        self._filter.setEnabled(bool(self._entries))
        self._update_buttons()

    def _fill_table(self) -> None:
        header = self._table.horizontalHeader()
        header.blockSignals(True)
        header.setSortIndicator(
            self._sort_col,
            Qt.SortOrder.DescendingOrder if self._sort_desc else Qt.SortOrder.AscendingOrder,
        )
        header.blockSignals(False)
        self._table.setUpdatesEnabled(False)
        self._table.clearContents()
        self._table.setRowCount(len(self._shown))
        for row, entry in enumerate(self._shown):
            input_name = Path(str(entry.input_path)).name or str(entry.input_path)
            tooltip = f"From: {entry.input_path}\nSaved as: {entry.output_path}"
            before = format_size(entry.original_size_bytes)
            after = format_size(entry.compressed_size_bytes)
            cells = {
                COL_FILE: input_name,
                COL_PROFILE: str(entry.profile_name or "—"),
                COL_FORMAT: friendly_format(entry.codec),
                COL_SIZES: f"{before} → {after}",
                COL_SAVED: f"{float(entry.reduction_pct or 0):.1f}%",
                COL_WHEN: format_when(entry.timestamp),
            }
            for column, text in cells.items():
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setToolTip(tooltip)
                if column in (COL_SIZES, COL_SAVED):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self._table.setItem(row, column, item)
        self._table.setUpdatesEnabled(True)

    def _on_header_clicked(self, column: int) -> None:
        if column == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = column
            self._sort_desc = column in _DESCENDING_FIRST
        self._apply_filter()

    # ── actions ──────────────────────────────────────────────────

    def selected_entry(self) -> Optional[HistoryEntry]:
        row = self._table.currentRow()
        if self._table.selectedItems() and 0 <= row < len(self._shown):
            return self._shown[row]
        return None

    def _update_buttons(self) -> None:
        self._open_btn.setEnabled(self.selected_entry() is not None)
        self._clear_btn.setEnabled(bool(self._entries))

    def _open_folder(self) -> None:
        entry = self.selected_entry()
        if entry is None:
            self._status.setText("Pick a row first, then choose Open folder.")
            return
        self._status.setText(reveal_in_file_manager(entry.output_path))

    def _clear_history(self) -> None:
        if not self._entries:
            return
        count = len(self._entries)
        noun = "entry" if count == 1 else "entries"
        answer = QMessageBox.question(
            self,
            "Clear history",
            f"Remove all {count} {noun} from your history?\n\n"
            "Your compressed files are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._history.clear()
        except Exception as exc:
            logger.debug("Could not clear history", exc_info=True)
            self._status.setText(f"Couldn't clear the history: {exc}")
            return
        self._status.setText("History cleared.")
        self.refresh()

    # ── test helpers ─────────────────────────────────────────────

    def visible_rows(self) -> List[HistoryEntry]:
        """The entries currently listed, in table order."""

        return list(self._shown)
