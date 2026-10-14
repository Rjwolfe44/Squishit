"""Queue rows for the Qt shell: a file waiting to run, a running job, a finished job.

Kept out of ``main_window`` so the window only decides *what* to show. File rows
carry the per-file actions (Trim, Move to top, ...) and a thumbnail slot that
the window fills in once the preview service has a frame.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices, QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.utils import format_size, format_time
from ..copy import human_progress_status, progress_detail
from ..hw_status import encoder_result_label
from ..software_fallback_dialog import declined_software_fallback_notice

logger = logging.getLogger(__name__)

THUMB_W, THUMB_H = 112, 63
#: Videos shorter than this have nothing to cut; the editor needs a real timeline.
MIN_TRIM_SECONDS = 0.5


@dataclass
class RowActions:
    """What a file row can ask the window to do. Any callable may be ``None``."""

    remove: Optional[Callable[[Path], None]] = None
    trim: Optional[Callable[[Path], None]] = None
    clear_trim: Optional[Callable[[Path], None]] = None
    move_top: Optional[Callable[[Path], None]] = None
    compress_only: Optional[Callable[[Path], None]] = None
    compare_sample: Optional[Callable[[Path], None]] = None


def reveal_in_folder(path: Path) -> None:
    """Show ``path`` in Explorer (selected), or open its folder elsewhere."""

    path = Path(path)
    try:
        if os.name == "nt" and path.exists():
            subprocess.Popen(["explorer", f"/select,{path}"])
            return
        folder = path if path.is_dir() else path.parent
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
    except Exception:
        logger.debug("Could not open %s", path, exc_info=True)


def open_path(path: Path) -> None:
    """Open a file with its default app."""

    try:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    except Exception:
        logger.debug("Could not open %s", path, exc_info=True)


def pixmap_from_rgb(rgb: bytes, width: int, height: int) -> QPixmap:
    """Build a pixmap from packed RGB24 bytes. GUI thread only."""

    image = QImage(rgb, width, height, width * 3, QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(image)


def clock(seconds: float) -> str:
    """Whole-second length as ``m:ss`` or ``h:mm:ss``."""

    total = max(0, int(round(float(seconds or 0))))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def clip_summary(plan) -> str:
    """Badge text for a file's clips: 'Trimmed to 0:42', '3 clips · saved separately · 1:23'."""

    if plan is None or not getattr(plan, "clips", None):
        return ""
    total = clock(plan.total_duration)
    count = len(plan.clips)
    if count == 1:
        return f"Trimmed to {total}"
    if getattr(plan.mode, "value", plan.mode) == "join":
        return f"{count} clips joined · {total}"
    return f"{count} clips · saved separately · {total}"


def describe_media(info) -> str:
    """'0:42 · 1920x1080 · 48 MB · h264' from whatever the analysis returned."""

    if info is None:
        return ""
    parts = []
    duration = getattr(info, "duration", 0) or 0
    if duration and hasattr(info, "video_codec"):
        parts.append(clock(duration))
    width = getattr(info, "width", None)
    height = getattr(info, "height", None)
    if width and height:
        parts.append(f"{width}×{height}")
    size = getattr(info, "size", None)
    if size:
        parts.append(format_size(size))
    codec = getattr(info, "video_codec", None) or getattr(info, "image_format", None)
    if codec:
        parts.append(str(codec))
    return " · ".join(parts)


class FileRow(QFrame):
    """One queued file: thumbnail, details, trim badge, and per-file actions."""

    def __init__(self, path: Path, actions: Optional[RowActions] = None):
        super().__init__()
        self.path = Path(path)
        self.actions = actions or RowActions()
        self._ready = False  # analysis finished and the file is a video
        self._busy = False
        self._has_clips = False
        self.setObjectName("card")

        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 8, 12, 8)
        outer.setSpacing(12)

        self._thumb = QLabel("Video")
        self._thumb.setObjectName("thumb")
        self._thumb.setFixedSize(THUMB_W, THUMB_H)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self._thumb)

        text = QVBoxLayout()
        text.setSpacing(2)
        self._name = QLabel(self.path.name)
        self._name.setObjectName("rowName")
        self._name.setToolTip(str(self.path))
        self._meta = QLabel("Reading the file…")
        self._meta.setObjectName("muted")
        self._meta.setWordWrap(True)
        badge_row = QHBoxLayout()
        badge_row.setSpacing(6)
        self._badge = QLabel("")
        self._badge.setObjectName("clipBadge")
        self._badge.hide()
        self._badge_clear = QToolButton()
        self._badge_clear.setObjectName("badgeClear")
        self._badge_clear.setText("×")
        self._badge_clear.setToolTip("Remove the trim and use the whole video")
        self._badge_clear.clicked.connect(self._on_clear_trim)
        self._badge_clear.hide()
        badge_row.addWidget(self._badge)
        badge_row.addWidget(self._badge_clear)
        badge_row.addStretch(1)
        text.addWidget(self._name)
        text.addWidget(self._meta)
        text.addLayout(badge_row)
        text.addStretch(1)
        outer.addLayout(text, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self._trim_btn = QPushButton("Trim…")
        self._trim_btn.setToolTip("Cut one or more clips out of this video")
        self._trim_btn.setEnabled(False)
        self._trim_btn.clicked.connect(self._on_trim)
        self._more_btn = QToolButton()
        self._more_btn.setObjectName("moreButton")
        self._more_btn.setText("⋯")
        self._more_btn.setToolTip("More actions")
        self._more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._more_btn.setMenu(self._build_menu())
        self._remove_btn = QPushButton("Remove")
        self._remove_btn.clicked.connect(self._on_remove)
        buttons.addWidget(self._trim_btn)
        buttons.addWidget(self._more_btn)
        buttons.addWidget(self._remove_btn)
        outer.addLayout(buttons)

    # -- menu / actions -------------------------------------------------

    def _build_menu(self) -> QMenu:
        menu = QMenu(self)
        self._menu_trim = menu.addAction("Trim…", self._on_trim)
        self._menu_compress = menu.addAction(
            "Compress this file only", self._on_compress_only
        )
        self._menu_compare = menu.addAction(
            "Compare sample presets", self._on_compare_sample
        )
        menu.addSeparator()
        self._menu_top = menu.addAction("Move to top", self._on_move_top)
        menu.addSeparator()
        menu.addAction("Show in folder", lambda: reveal_in_folder(self.path))
        menu.addAction("Copy file path", self._copy_path)
        menu.addSeparator()
        self._menu_remove = menu.addAction("Remove from queue", self._on_remove)
        return menu

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        self._sync_menu()
        self._more_btn.menu().exec(event.globalPos())

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._trim_btn.isEnabled():
            self._on_trim()
        event.accept()

    def _sync_menu(self) -> None:
        idle = not self._busy
        self._menu_trim.setEnabled(idle and self._ready and bool(self.actions.trim))
        self._menu_compress.setEnabled(idle and bool(self.actions.compress_only))
        self._menu_compare.setEnabled(
            idle and self._ready and bool(self.actions.compare_sample)
        )
        self._menu_top.setEnabled(idle and bool(self.actions.move_top))
        self._menu_remove.setEnabled(idle and bool(self.actions.remove))

    def _call(self, action) -> None:
        if action is not None and not self._busy:
            action(self.path)

    def _on_trim(self) -> None:
        self._call(self.actions.trim)

    def _on_clear_trim(self) -> None:
        self._call(self.actions.clear_trim)

    def _on_remove(self) -> None:
        self._call(self.actions.remove)

    def _on_move_top(self) -> None:
        self._call(self.actions.move_top)

    def _on_compress_only(self) -> None:
        self._call(self.actions.compress_only)

    def _on_compare_sample(self) -> None:
        self._call(self.actions.compare_sample)

    def _copy_path(self) -> None:
        QGuiApplication.clipboard().setText(str(self.path))

    # -- state ----------------------------------------------------------

    def set_info(self, info) -> None:
        """Analysis finished. ``None`` means the file could not be read."""

        if info is None:
            self._meta.setText("Can't read this file")
            self._ready = False
        else:
            self._meta.setText(describe_media(info) or str(self.path.parent))
            length = float(getattr(info, "duration", 0) or 0)
            self._ready = hasattr(info, "video_codec") and length >= MIN_TRIM_SECONDS
            if not hasattr(info, "video_codec"):
                self._thumb.setText("Image")
        self._sync_buttons()

    def set_thumbnail(self, pixmap: QPixmap) -> None:
        if pixmap is None or pixmap.isNull():
            return
        scaled = pixmap.scaled(
            THUMB_W,
            THUMB_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._thumb.setPixmap(scaled)

    def set_clip_summary(self, text: str) -> None:
        """Badge under the file name; empty text hides it."""

        self._has_clips = bool(text)
        self._badge.setText(text)
        self._badge.setVisible(bool(text))
        self._badge_clear.setVisible(bool(text) and not self._busy)
        self._trim_btn.setText("Edit trim…" if text else "Trim…")

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._sync_buttons()
        self._badge_clear.setVisible(self._has_clips and not busy)

    def _sync_buttons(self) -> None:
        idle = not self._busy
        self._trim_btn.setEnabled(idle and self._ready and bool(self.actions.trim))
        self._remove_btn.setEnabled(idle and bool(self.actions.remove))
        self._more_btn.setEnabled(idle)

    @property
    def can_trim(self) -> bool:
        return self._trim_btn.isEnabled()


class ProgressRow(QFrame):
    """A running (or waiting) encode."""

    def __init__(self, job_id: str, title: str, on_cancel):
        super().__init__()
        self.job_id = job_id
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        header = QHBoxLayout()
        self._title = QLabel(title)
        self._title.setObjectName("rowName")
        header.addWidget(self._title, 1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda: on_cancel(job_id))
        header.addWidget(cancel)
        layout.addLayout(header)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        layout.addWidget(self._bar)
        self._status = QLabel("Waiting in the queue…")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    def update_job(self, job, *, awaiting: bool) -> None:
        try:
            percent = int(float(getattr(job, "progress", 0) or 0))
        except (TypeError, ValueError):
            percent = 0
        self._bar.setValue(max(0, min(100, percent)))
        self._status.setText(
            "\n".join(
                part
                for part in (
                    human_progress_status(job, awaiting_software_fallback=awaiting),
                    progress_detail(job),
                )
                if part
            )
        )


class ResultRow(QFrame):
    """A finished encode, with a way to find the file."""

    def __init__(self, result, *, title_suffix: str = ""):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        if getattr(result, "cancelled", False):
            title = "Cancelled"
        elif getattr(result, "skipped", False):
            title = "Skipped"
        elif getattr(result, "success", False):
            title = "Kept original" if getattr(result, "kept_original", False) else "Compressed"
        else:
            title = "Failed"
        name = ""
        output = getattr(result, "output_file", None)
        source = getattr(result, "input_file", None)
        if output:
            name = Path(output).name
        elif source:
            name = Path(source).name
        header = QHBoxLayout()
        heading = QLabel(f"{title} — {name}{title_suffix}" if name else title)
        heading.setObjectName("rowName")
        heading.setWordWrap(True)
        header.addWidget(heading, 1)
        self.output_path: Optional[Path] = Path(output) if output else None
        succeeded = bool(getattr(result, "success", False)) and not getattr(
            result, "cancelled", False
        )
        if self.output_path is not None and succeeded:
            self._show_btn = QPushButton("Show in folder")
            self._show_btn.clicked.connect(lambda: reveal_in_folder(self.output_path))
            self._open_btn = QPushButton("Open")
            self._open_btn.clicked.connect(lambda: open_path(self.output_path))
            header.addWidget(self._open_btn)
            header.addWidget(self._show_btn)
        layout.addLayout(header)
        bits = []
        if getattr(result, "success", False) and not getattr(result, "skipped", False):
            original = getattr(result, "original_size", 0) or 0
            compressed = getattr(result, "compressed_size", 0) or 0
            if original and compressed:
                bits.append(f"{format_size(original)} → {format_size(compressed)}")
            reduction = getattr(result, "reduction_percent", None)
            if reduction is not None and not getattr(result, "kept_original", False):
                try:
                    bits.append(f"{float(reduction):.0f}% smaller")
                except (TypeError, ValueError):
                    pass
            elapsed = getattr(result, "encoding_time", 0) or 0
            if elapsed:
                bits.append(format_time(elapsed))
        encoder = encoder_result_label(str(getattr(result, "encoder_name", "") or ""))
        if encoder:
            bits.append(f"Encoder: {encoder}")
        note = getattr(result, "note", None) or getattr(result, "error_message", None)
        if note:
            bits.append(str(note))
        notice = declined_software_fallback_notice(result)
        if notice:
            bits.append(notice)
        body = QLabel(" · ".join(bits) if bits else "")
        body.setObjectName("hint")
        body.setWordWrap(True)
        layout.addWidget(body)


def section_header(text: str, *, extra: Optional[QWidget] = None) -> QWidget:
    """Small caption row above a group of rows (e.g. 'Results')."""

    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 4, 0, 0)
    label = QLabel(text)
    label.setObjectName("section")
    layout.addWidget(label)
    layout.addStretch(1)
    if extra is not None:
        layout.addWidget(extra)
    return box


__all__ = [
    "FileRow",
    "ProgressRow",
    "ResultRow",
    "RowActions",
    "MIN_TRIM_SECONDS",
    "THUMB_H",
    "THUMB_W",
    "clip_summary",
    "clock",
    "describe_media",
    "open_path",
    "pixmap_from_rgb",
    "reveal_in_folder",
    "section_header",
]
