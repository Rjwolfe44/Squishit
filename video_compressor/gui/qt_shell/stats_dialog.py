"""Stats dashboard for the Qt shell.

A grid of simple cards built from the compression history: how many files,
how much space was saved, the average reduction, favourites and the last
week. Only ``get_entries()`` is required from the history object;
``get_stats()`` is used for the headline numbers when it is there.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...config import APP_NAME
from ...core.history import HistoryEntry
from ...core.utils import format_size, format_time
from .history_dialog import default_history, friendly_format, parse_timestamp

logger = logging.getLogger(__name__)

EMPTY_TITLE = "No stats yet"
EMPTY_HINT = "Compress a few files and your totals will show up here."

_LOCAL_QSS = """
QLabel#statValueSmall {
    font-size: 15px;
    font-weight: 700;
    background: transparent;
}
"""

CARD_FILES = "Files compressed"
CARD_SAVED = "Space saved"
CARD_AVERAGE = "Average size reduction"
CARD_TIME = "Time spent compressing"
CARD_BEST = "Biggest saving"
CARD_FORMAT = "Most used format"
CARD_PROFILE = "Most used profile"
CARD_WEEK = "Last 7 days"


def _saved_bytes(entry: HistoryEntry) -> int:
    return max(0, int(entry.original_size_bytes or 0) - int(entry.compressed_size_bytes or 0))


def _favourite(values: Sequence[str]) -> Optional[Tuple[str, int]]:
    counts = Counter(v for v in values if v)
    if not counts:
        return None
    # most_common keeps first-seen order on ties; entries come newest first,
    # so a tie goes to whichever was used most recently.
    name, count = counts.most_common(1)[0]
    return name, count


def summarize(
    entries: Sequence[HistoryEntry],
    stats: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Numbers shown on the dashboard. Pure, so it is easy to test."""

    # Newest first, so a tie for "most used" goes to the more recent choice.
    entries = sorted(
        entries,
        key=lambda e: parse_timestamp(e.timestamp) or datetime.min,
        reverse=True,
    )
    stats = stats or {}
    now = now or datetime.now()
    derived_saved = max(
        0,
        sum(int(e.original_size_bytes or 0) - int(e.compressed_size_bytes or 0) for e in entries),
    )
    count = int(stats.get("count", len(entries)) or 0)
    total_saved = int(stats.get("total_saved_bytes", derived_saved) or 0)
    total_original = sum(int(e.original_size_bytes or 0) for e in entries)
    total_compressed = sum(int(e.compressed_size_bytes or 0) for e in entries)
    average = (
        sum(float(e.reduction_pct or 0) for e in entries) / len(entries) if entries else 0.0
    )
    week_ago = now - timedelta(days=7)
    recent: List[HistoryEntry] = []
    for entry in entries:
        stamp = parse_timestamp(entry.timestamp)
        if stamp is not None and stamp >= week_ago:
            recent.append(entry)
    return {
        "count": count,
        "total_saved": total_saved,
        "total_original": total_original,
        "total_compressed": total_compressed,
        "average_reduction": average,
        "total_seconds": sum(float(e.duration_seconds or 0) for e in entries),
        "best": max(entries, key=_saved_bytes) if entries else None,
        "top_format": _favourite([friendly_format(e.codec) for e in entries if e.codec]),
        "top_profile": _favourite([str(e.profile_name or "") for e in entries]),
        "week_count": len(recent),
        "week_saved": sum(_saved_bytes(e) for e in recent),
    }


class StatsDialog(QDialog):
    """Lifetime totals from the compression history, as simple cards."""

    def __init__(self, parent: Optional[QWidget] = None, history: Any = None):
        super().__init__(parent)
        self._history = history if history is not None else default_history()
        self._cards: Dict[str, Tuple[QLabel, QLabel]] = {}
        self.stats: Dict[str, Any] = {}
        self.is_empty = True
        self.setWindowTitle(f"{APP_NAME} stats")
        self.setStyleSheet(_LOCAL_QSS)
        self.setMinimumSize(560, 420)
        self.resize(700, 600)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)
        title = QLabel("Stats")
        title.setObjectName("title")
        outer.addWidget(title)
        self._subtitle = QLabel("")
        self._subtitle.setObjectName("hint")
        outer.addWidget(self._subtitle)

        self._body = QVBoxLayout()
        self._body.setSpacing(10)
        outer.addLayout(self._body, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("primary")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        outer.addLayout(buttons)

        self.refresh()

    # ── data ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        """Reload from the history object and rebuild the cards."""

        try:
            entries = list(self._history.get_entries())
        except Exception:
            logger.debug("Could not read history", exc_info=True)
            entries = []
        stats: Dict[str, Any] = {}
        getter = getattr(self._history, "get_stats", None)
        if callable(getter):
            try:
                stats = dict(getter() or {})
            except Exception:
                logger.debug("Could not read history stats", exc_info=True)
        self.stats = summarize(entries, stats)
        self.is_empty = not entries and not self.stats["count"]
        self._rebuild()

    def card_values(self) -> Dict[str, str]:
        """Card title -> big value text (handy for tests and tooltips)."""

        return {name: value.text() for name, (value, _detail) in self._cards.items()}

    def card_details(self) -> Dict[str, str]:
        return {name: detail.text() for name, (_value, detail) in self._cards.items()}

    # ── layout ───────────────────────────────────────────────────

    def _clear_body(self) -> None:
        self._cards.clear()
        while self._body.count():
            item = self._body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                self._drop_layout(item.layout())

    @staticmethod
    def _drop_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _make_card(self, caption: str, value: str, detail: str = "", *, small: bool = False) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        head = QLabel(caption)
        head.setObjectName("section")
        layout.addWidget(head)
        big = QLabel(value)
        big.setObjectName("statValueSmall" if small else "title")
        big.setWordWrap(True)
        big.setMinimumHeight(24 if small else 32)
        big.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(big)
        note = QLabel(detail)
        note.setObjectName("hint")
        note.setWordWrap(True)
        note.setVisible(bool(detail))
        layout.addWidget(note)
        layout.addStretch(1)
        self._cards[caption] = (big, note)
        return card

    def _rebuild(self) -> None:
        self._clear_body()
        if self.is_empty:
            self._subtitle.setText("")
            self._body.addWidget(self._empty_card(), 1)
            return
        s = self.stats
        count = s["count"]
        self._subtitle.setText("Based on the files in your history.")

        cards: List[QFrame] = [
            self._make_card(CARD_FILES, str(count), "Saved in your history"),
            self._make_card(
                CARD_SAVED,
                format_size(s["total_saved"]),
                f"{format_size(s['total_original'])} became {format_size(s['total_compressed'])}",
            ),
            self._make_card(
                CARD_AVERAGE,
                f"{s['average_reduction']:.1f}%",
                "smaller than the original, on average",
            ),
            self._make_card(
                CARD_TIME,
                format_time(s["total_seconds"]),
                "Adds up every finished file",
            ),
        ]
        best = s["best"]
        if best is not None and _saved_bytes(best) > 0:
            name = Path(str(best.input_path)).name or str(best.input_path)
            cards.append(
                self._make_card(
                    CARD_BEST,
                    name,
                    f"Saved {format_size(_saved_bytes(best))} "
                    f"({format_size(best.original_size_bytes)} → "
                    f"{format_size(best.compressed_size_bytes)})",
                    small=True,
                )
            )
        if s["top_format"]:
            name, used = s["top_format"]
            cards.append(
                self._make_card(CARD_FORMAT, name, self._used_text(used, count), small=True)
            )
        if s["top_profile"]:
            name, used = s["top_profile"]
            cards.append(
                self._make_card(CARD_PROFILE, name, self._used_text(used, count), small=True)
            )
        week_count = s["week_count"]
        noun = "file" if week_count == 1 else "files"
        cards.append(
            self._make_card(
                CARD_WEEK,
                f"{week_count} {noun}",
                f"Saved {format_size(s['week_saved'])}",
            )
        )

        grid = QGridLayout()
        grid.setSpacing(10)
        for index, card in enumerate(cards):
            grid.addWidget(card, index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self._body.addLayout(grid)
        self._body.addStretch(1)

    @staticmethod
    def _used_text(used: int, total: int) -> str:
        noun = "file" if total == 1 else "files"
        return f"Used for {used} of {total} {noun}"

    def _empty_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        title = QLabel(EMPTY_TITLE)
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel(EMPTY_HINT)
        hint.setObjectName("hint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addStretch(1)
        self._cards[EMPTY_TITLE] = (title, hint)
        return card
