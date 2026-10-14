"""The Trim editor: pick one or more clips out of a video.

The dialog edits a copy. Cancel throws the changes away; "Save clips" hands the
plan back to the window (``result_plan()``), and "Use whole video" clears it.
Pictures come from ``core.preview`` (ffmpeg). Playback is silent on purpose.

Threading: the preview service calls back on worker threads. Everything is
posted to ``_Bridge`` signals so widgets are only ever touched on the GUI
thread.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.clips import MAX_JOIN_CLIPS, ClipMode, ClipPlan
from ...core.preview import display_size, filmstrip_count
from ...core.utils import format_timecode
from ..timeline_model import (
    ClipEditModel,
    describe_selection,
    parse_time_field,
)
from .rows import pixmap_from_rgb
from .timeline import TimelineWidget

logger = logging.getLogger(__name__)

FRAME_MAX = (960, 540)
THUMB_MAX = (160, 90)
THUMB_SLOT_PX = 96
SCRUB_SETTLE_MS = 120
NEW_CLIP_SECONDS = 5.0


class _Bridge(QObject):
    """Carries results from preview worker threads to the GUI thread."""

    frame = Signal(float, int, int, object, bool)
    thumb = Signal(int, float, int, int, object)
    play_tick = Signal()
    play_end = Signal(int, str)


class _VideoView(QWidget):
    """Black box that shows the current frame, scaled to fit."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._message = "Loading…"
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_pixmap(self, pixmap: Optional[QPixmap]) -> None:
        self._pixmap = pixmap
        self.update()

    def set_message(self, text: str) -> None:
        self._message = text
        self.update()

    @property
    def has_picture(self) -> bool:
        return self._pixmap is not None

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#000000"))
        if self._pixmap is None or self._pixmap.isNull():
            painter.setPen(QColor("#a1a1aa"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._message)
            painter.end()
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scaled = self._pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - scaled.width()) / 2.0
        y = (self.height() - scaled.height()) / 2.0
        painter.drawPixmap(QRectF(x, y, scaled.width(), scaled.height()), self._pixmap, QRectF(self._pixmap.rect()))
        painter.end()


class TrimEditorDialog(QDialog):
    """Cut clips out of one video.

    ``plan`` is the file's current clips (``None`` = the whole video).
    ``preview`` is a ``core.preview.PreviewService``. ``target_note`` is an
    optional reminder such as "Target size: 25 MB per file".
    """

    def __init__(
        self,
        parent,
        path: Path,
        info,
        plan: Optional[ClipPlan],
        preview,
        target_note: str = "",
    ):
        super().__init__(parent)
        self.path = Path(path)
        self.info = info
        self.preview = preview
        self.target_note = target_note
        self.duration = float(getattr(info, "duration", 0) or 0)
        fps = float(getattr(info, "avg_fps", 0) or getattr(info, "fps", 0) or 30.0)
        self.fps = fps if 1.0 <= fps <= 240.0 else 30.0
        self.model = ClipEditModel.from_plan(plan, self.duration, self.fps)
        self._result: Optional[ClipPlan] = None
        self._t = 0.0
        self._playing = False
        self._play_id = 0
        self._syncing = False
        self._closed = False
        self._pending_play: Optional[tuple] = None
        self._play_lock = threading.Lock()
        self._tick_queued = False
        self._frame_size = display_size(
            int(getattr(info, "width", 0) or 0),
            int(getattr(info, "height", 0) or 0),
            int(getattr(info, "rotation", 0) or 0),
            str(getattr(info, "sar", "1:1") or "1:1"),
            *FRAME_MAX,
        )
        self._bridge = _Bridge(self)
        self._bridge.frame.connect(self._on_frame)
        self._bridge.thumb.connect(self._on_thumb)
        self._bridge.play_tick.connect(self._drain_play)
        self._bridge.play_end.connect(self._on_play_end)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SCRUB_SETTLE_MS)
        self._settle.timeout.connect(self._show_exact_frame)

        self.setWindowTitle(f"Trim — {self.path.name}")
        self._build()
        self._install_shortcuts()
        self._unsubscribe = self.model.subscribe(self._on_model_changed)
        self._on_model_changed()
        self._place()
        QTimer.singleShot(0, self._start_preview)
        self.video.setFocus()

    # -- layout ---------------------------------------------------------

    def _place(self) -> None:
        screen = QApplication.primaryScreen()
        width, height = 1180, 800
        if screen is not None:
            avail = screen.availableGeometry()
            width = min(width, int(avail.width() * 0.92))
            height = min(height, int(avail.height() * 0.92))
        self.resize(width, height)

    def _icon(self, name: str):
        return self.style().standardIcon(getattr(QStyle.StandardPixmap, name))

    def _button(self, text: str, tip: str, handler, *, icon: str = "") -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setAutoDefault(False)
        button.setDefault(False)
        if icon:
            button.setIcon(self._icon(icon))
        button.clicked.connect(lambda _checked=False: handler())
        return button

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        intro = QLabel(
            "Drag the edges of the blue boxes to choose what to keep. "
            "Everything outside them is cut. Add more boxes to make more clips."
        )
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        root.addWidget(intro)

        top = QHBoxLayout()
        top.setSpacing(14)
        root.addLayout(top, 1)

        left = QVBoxLayout()
        left.setSpacing(8)
        self.video = _VideoView()
        self.video.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        left.addWidget(self.video, 1)
        left.addLayout(self._build_time_row())
        left.addLayout(self._build_transport())
        top.addLayout(left, 3)

        top.addWidget(self._build_clip_panel(), 2)

        edit_row = QHBoxLayout()
        edit_row.setSpacing(6)
        self.set_in_btn = self._button(
            "Start here (I)", "Make the selected clip begin at the playhead", self.set_in
        )
        self.set_out_btn = self._button(
            "End here (O)", "Make the selected clip end at the playhead", self.set_out
        )
        self.split_btn = self._button(
            "Split (S)", "Cut the clip in two at the playhead", self.split
        )
        self.add_btn = self._button(
            "Add clip", "Add a 5 second clip at the playhead", self.add_clip
        )
        self.undo_btn = self._button("Undo", "Undo the last change (Ctrl+Z)", self.undo)
        self.redo_btn = self._button("Redo", "Redo (Ctrl+Y)", self.redo)
        for widget in (
            self.set_in_btn,
            self.set_out_btn,
            self.split_btn,
            self.add_btn,
            self.undo_btn,
            self.redo_btn,
        ):
            edit_row.addWidget(widget)
        edit_row.addStretch(1)
        zoom_label = QLabel("Zoom")
        zoom_label.setObjectName("hint")
        edit_row.addWidget(zoom_label)
        edit_row.addWidget(self._button("−", "Zoom out (Ctrl+−)", lambda: self.timeline.zoom(0.8)))
        edit_row.addWidget(self._button("+", "Zoom in (Ctrl++)", lambda: self.timeline.zoom(1.25)))
        edit_row.addWidget(self._button("Fit", "Show the whole video (Ctrl+0)", self.timeline_fit))
        root.addLayout(edit_row)

        self.timeline = TimelineWidget(self.model)
        self.timeline.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.timeline.seekRequested.connect(self._on_seek_requested)
        self.timeline.scrubbing.connect(self._on_scrubbing)
        self.timeline.scrubFinished.connect(self._on_scrub_finished)
        root.addWidget(self.timeline)

        self.status = QLabel("")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        footer = QHBoxLayout()
        self.summary = QLabel("")
        self.summary.setObjectName("rowName")
        self.summary.setWordWrap(True)
        footer.addWidget(self.summary, 1)
        self.whole_btn = QPushButton("Use whole video")
        self.whole_btn.setToolTip("Remove all clips and keep the entire video")
        self.whole_btn.setAutoDefault(False)
        self.whole_btn.clicked.connect(self.use_whole_video)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setAutoDefault(False)
        self.cancel_btn.clicked.connect(self.reject)
        self.save_btn = QPushButton("Save clips")
        self.save_btn.setObjectName("primary")
        self.save_btn.setAutoDefault(False)
        self.save_btn.setDefault(False)
        self.save_btn.clicked.connect(self.save)
        footer.addWidget(self.whole_btn)
        footer.addWidget(self.cancel_btn)
        footer.addWidget(self.save_btn)
        root.addLayout(footer)

    def _build_transport(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self._button("Start", "Go to the beginning (Home)", lambda: self.seek(0.0)))
        row.addWidget(self._button("−1 s", "Back one second (Shift+Left)", lambda: self.step_seconds(-1)))
        row.addWidget(self._button("−1 frame", "Back one frame (Left)", lambda: self.step_frames(-1)))
        self.play_btn = self._button("Play", "Play or pause (Space)", self.toggle_play, icon="SP_MediaPlay")
        self.play_btn.setMinimumWidth(96)
        row.addWidget(self.play_btn)
        row.addWidget(self._button("+1 frame", "Forward one frame (Right)", lambda: self.step_frames(1)))
        row.addWidget(self._button("+1 s", "Forward one second (Shift+Right)", lambda: self.step_seconds(1)))
        row.addWidget(self._button("End", "Go to the end (End)", lambda: self.seek(self.duration)))
        row.addStretch(1)
        return row

    def _build_time_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        note = QLabel("Preview has no sound.")
        note.setObjectName("hint")
        row.addWidget(note)
        row.addStretch(1)
        self.time_field = QLineEdit(format_timecode(0))
        self.time_field.setFixedWidth(120)
        self.time_field.setToolTip("Type a time such as 1:30 and press Enter to jump there")
        self.time_field.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.time_field.returnPressed.connect(self._on_time_entered)
        row.addWidget(self.time_field)
        total = QLabel(f"/ {format_timecode(self.duration)}")
        total.setObjectName("hint")
        row.addWidget(total)
        return row

    def _build_clip_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        heading = QLabel("CLIPS")
        heading.setObjectName("section")
        layout.addWidget(heading)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Start", "End", "Length"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._on_table_selection)
        self.table.itemChanged.connect(self._on_table_edited)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.up_btn = self._button("Move up", "Play this clip earlier in a joined video", lambda: self.move_clip(-1))
        self.down_btn = self._button("Move down", "Play this clip later in a joined video", lambda: self.move_clip(1))
        self.delete_btn = self._button("Delete", "Remove the selected clip (Del)", self.delete_clip)
        for widget in (self.up_btn, self.down_btn, self.delete_btn):
            buttons.addWidget(widget)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.mode_box = QWidget()
        self.mode_box.setObjectName("modeBox")
        self.mode_box.setStyleSheet("QWidget#modeBox { background: transparent; }")
        mode_layout = QVBoxLayout(self.mode_box)
        mode_layout.setContentsMargins(0, 4, 0, 0)
        mode_layout.setSpacing(4)
        what = QLabel("WHEN YOU COMPRESS")
        what.setObjectName("section")
        mode_layout.addWidget(what)
        self.separate_radio = QRadioButton("Save each clip as its own file")
        self.join_radio = QRadioButton("Join all clips into one video")
        self.separate_radio.toggled.connect(self._on_mode_radio)
        self.join_radio.toggled.connect(self._on_join_radio)
        mode_layout.addWidget(self.separate_radio)
        mode_layout.addWidget(self.join_radio)
        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("hint")
        self.mode_hint.setWordWrap(True)
        mode_layout.addWidget(self.mode_hint)
        layout.addWidget(self.mode_box)
        return panel

    # -- keys -----------------------------------------------------------

    def _install_shortcuts(self) -> None:
        bindings = [
            ("Space", self.toggle_play),
            ("I", self.set_in),
            ("O", self.set_out),
            ("S", self.split),
            ("Left", lambda: self.step_frames(-1)),
            ("Right", lambda: self.step_frames(1)),
            ("Shift+Left", lambda: self.step_seconds(-1)),
            ("Shift+Right", lambda: self.step_seconds(1)),
            ("Home", lambda: self.seek(0.0)),
            ("End", lambda: self.seek(self.duration)),
            ("Delete", self.delete_clip),
            ("Ctrl+Z", self.undo),
            ("Ctrl+Y", self.redo),
            ("Ctrl+Shift+Z", self.redo),
            ("Ctrl+=", lambda: self.timeline.zoom(1.25)),
            ("Ctrl++", lambda: self.timeline.zoom(1.25)),
            ("Ctrl+-", lambda: self.timeline.zoom(0.8)),
            ("Ctrl+0", self.timeline_fit),
            ("Ctrl+Return", self.save),
            ("Ctrl+Enter", self.save),
        ]
        self._shortcuts = []
        for sequence, handler in bindings:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            focus = self.focusWidget()
            if not isinstance(focus, QLineEdit):
                self.save()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- model -> widgets -----------------------------------------------

    def _on_model_changed(self) -> None:
        self._refresh_table()
        self._refresh_chrome()

    def _refresh_table(self) -> None:
        self._syncing = True
        try:
            clips = self.model.clips
            self.table.setRowCount(len(clips))
            for row, clip in enumerate(clips):
                values = (
                    clip.label or f"Clip {row + 1}",
                    format_timecode(clip.start),
                    format_timecode(clip.end),
                    format_timecode(clip.end - clip.start),
                )
                for column, text in enumerate(values):
                    item = self.table.item(row, column)
                    if item is None:
                        item = QTableWidgetItem()
                        self.table.setItem(row, column, item)
                    item.setText(text)
                    editable = column in (0, 1, 2)
                    flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                    if editable:
                        flags |= Qt.ItemFlag.ItemIsEditable
                    item.setFlags(flags)
                    item.setForeground(QColor("#fafafa"))
            selected = self.model.selected
            if selected is not None and 0 <= selected < len(clips):
                self.table.selectRow(selected)
            else:
                self.table.clearSelection()
        finally:
            self._syncing = False

    def _refresh_chrome(self) -> None:
        model = self.model
        selected = model.selected
        has_selection = selected is not None
        many = model.count >= 2
        self.undo_btn.setEnabled(model.can_undo)
        self.redo_btn.setEnabled(model.can_redo)
        self.delete_btn.setEnabled(has_selection and model.count > 1)
        self.up_btn.setEnabled(many and has_selection and selected > 0)
        self.down_btn.setEnabled(many and has_selection and selected < model.count - 1)
        self.mode_box.setVisible(many)
        self._syncing = True
        try:
            self.join_radio.setChecked(model.mode is ClipMode.JOIN)
            self.separate_radio.setChecked(model.mode is not ClipMode.JOIN)
        finally:
            self._syncing = False
        parts = [describe_selection(model, getattr(self.info, "size", None) or None)]
        if self.target_note:
            parts.append(self.target_note)
        self.summary.setText("  ·  ".join(part for part in parts if part))
        if many:
            if model.mode is ClipMode.JOIN:
                self.mode_hint.setText("The clips play one after another, in the order listed.")
            else:
                self.mode_hint.setText(f"You will get {model.count} files.")
        problems = []
        if model.join_cap_exceeded:
            problems.append(
                f"Joining works for up to {MAX_JOIN_CLIPS} clips. Remove some or save them separately."
            )
        elif model.overlaps() and model.mode is ClipMode.JOIN:
            problems.append("Some clips overlap, so that part of the video repeats.")
        if problems:
            self._set_status(" ".join(problems), error=model.join_cap_exceeded)
        self.save_btn.setEnabled(not model.join_cap_exceeded)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status.setText(text)
        self.status.setStyleSheet("color: #fca5a5;" if error and text else "")

    def _read_time(self, text: str) -> Optional[float]:
        """Seconds for typed text, or ``None`` after explaining what is wrong."""

        value = parse_time_field(text, self.duration)
        if value is not None:
            return value
        if parse_time_field(text, 10**9) is not None:
            self._set_status(
                f"The video is only {format_timecode(self.duration)} long.", error=True
            )
        else:
            self._set_status("That is not a time. Try 1:30 or 90.5.", error=True)
        return None

    # -- table edits ----------------------------------------------------

    def _on_table_selection(self) -> None:
        if self._syncing:
            return
        rows = self.table.selectionModel().selectedRows()
        self.model.select(rows[0].row() if rows else None)

    def _on_table_edited(self, item: QTableWidgetItem) -> None:
        if self._syncing:
            return
        row, column = item.row(), item.column()
        if not 0 <= row < self.model.count:
            return
        text = item.text()
        if column == 0:
            self.model.rename(row, text.strip())
            return
        value = self._read_time(text)
        clip = self.model.clips[row]
        if value is None:
            QTimer.singleShot(0, self._refresh_table)
            return
        start, end = (value, clip.end) if column == 1 else (clip.start, value)
        if not self.model.set_times(row, start, end):
            self._set_status("A clip must end after it starts and fit in the video.", error=True)
            QTimer.singleShot(0, self._refresh_table)
            return
        self._set_status("")
        self._request_frame(value, fast=False)
        self._set_playhead(value)

    def _on_mode_radio(self, checked: bool) -> None:
        if self._syncing or not checked:
            return
        self.model.set_mode(ClipMode.SEPARATE)

    def _on_join_radio(self, checked: bool) -> None:
        if self._syncing or not checked:
            return
        self.model.set_mode(ClipMode.JOIN)

    # -- editing actions --------------------------------------------------

    def set_in(self) -> None:
        selected = self.model.selected
        if selected is None:
            self.add_clip()
            return
        if not self.model.set_in(selected, self._t):
            self._set_status("The start has to come before the end of the clip.", error=True)

    def set_out(self) -> None:
        selected = self.model.selected
        if selected is None:
            self.add_clip()
            return
        if not self.model.set_out(selected, self._t):
            self._set_status("The end has to come after the start of the clip.", error=True)

    def split(self) -> None:
        if self.model.split_at(self._t) is None:
            self._set_status("Move the playhead inside a clip, away from its edges, to split it.", error=True)
        else:
            self._set_status("")

    def add_clip(self) -> None:
        end = min(self.duration, self._t + NEW_CLIP_SECONDS)
        if self.model.add_clip(self._t, end) is None:
            self._set_status(
                "There is no free space at the playhead. Move it outside the blue boxes, or press S to split a clip.",
                error=True,
            )
        else:
            self._set_status("")

    def delete_clip(self) -> None:
        selected = self.model.selected
        if selected is None:
            return
        if not self.model.delete(selected):
            self._set_status("That is the only clip. Use 'Use whole video' to remove it.", error=True)

    def move_clip(self, direction: int) -> None:
        selected = self.model.selected
        if selected is not None:
            self.model.reorder(selected, selected + direction)

    def undo(self) -> None:
        self.model.undo()

    def redo(self) -> None:
        self.model.redo()

    def timeline_fit(self) -> None:
        self.timeline.fit()

    # -- finishing --------------------------------------------------------

    def result_plan(self) -> Optional[ClipPlan]:
        """The clips to use, or ``None`` for the whole video. Valid after OK."""

        return self._result

    def save(self) -> None:
        if self.model.join_cap_exceeded:
            return
        self._result = self.model.to_plan()
        self.accept()

    def use_whole_video(self) -> None:
        self._result = None
        self.accept()

    def reject(self) -> None:
        if self.model.can_undo and not self._confirm_discard():
            return
        super().reject()

    def _confirm_discard(self) -> bool:
        answer = QMessageBox.question(
            self,
            "Discard changes?",
            "You have unsaved changes to these clips. Close without saving?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def done(self, result: int) -> None:
        self._shutdown()
        super().done(result)

    def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._settle.stop()
        self._playing = False
        self._play_id += 1
        try:
            self.preview.cancel_all()
        except Exception:
            logger.debug("Could not stop the preview", exc_info=True)
        self.timeline.dispose()
        try:
            self._unsubscribe()
        except Exception:
            pass

    # -- preview ----------------------------------------------------------

    def _start_preview(self) -> None:
        if self._closed or self.duration <= 0:
            return
        count = filmstrip_count(max(self.timeline.width(), 600), THUMB_SLOT_PX)
        self.timeline.set_filmstrip(count)
        thumb = display_size(
            int(getattr(self.info, "width", 0) or 0),
            int(getattr(self.info, "height", 0) or 0),
            int(getattr(self.info, "rotation", 0) or 0),
            str(getattr(self.info, "sar", "1:1") or "1:1"),
            *THUMB_MAX,
        )
        try:
            self.preview.request_filmstrip(
                self.path,
                self.duration,
                count,
                thumb,
                lambda i, t, w, h, rgb: self._bridge.thumb.emit(i, t, w, h, rgb),
            )
        except Exception:
            logger.debug("Could not start the filmstrip", exc_info=True)
        self._request_frame(0.0, fast=False)

    def _request_frame(self, t: float, *, fast: bool) -> None:
        if self._closed:
            return
        try:
            self.preview.request_frame(
                self.path,
                t,
                self._frame_size,
                fast=fast,
                callback=lambda ft, w, h, rgb, was_fast: self._bridge.frame.emit(ft, w, h, rgb, was_fast),
                duration=self.duration,
                fps=self.fps,
            )
        except Exception:
            logger.debug("Could not request a frame", exc_info=True)

    def _on_frame(self, t: float, w: int, h: int, rgb, fast: bool) -> None:
        if self._closed or self._playing:
            return
        if rgb:
            self.video.set_pixmap(pixmap_from_rgb(rgb, w, h))
        elif not self.video.has_picture:
            self.video.set_message("This frame could not be read.")

    def _on_thumb(self, index: int, t: float, w: int, h: int, rgb) -> None:
        if self._closed or not rgb:
            return
        self.timeline.set_thumbnail(index, pixmap_from_rgb(rgb, w, h))

    # -- playhead ---------------------------------------------------------

    def _set_playhead(self, t: float, *, follow: bool = False) -> None:
        self._t = max(0.0, min(float(t), self.duration))
        self.timeline.set_playhead(self._t, follow=follow)
        if not self.time_field.hasFocus():
            self.time_field.setText(format_timecode(self._t))

    def seek(self, t: float) -> None:
        """Jump to ``t`` and show the exact frame (stops playback)."""

        self._pause()
        self._set_playhead(t, follow=True)
        self._request_frame(self._t, fast=False)

    def step_frames(self, count: int) -> None:
        frame = 1.0 / self.fps
        index = round(self._t / frame) + count
        self.seek(index * frame)

    def step_seconds(self, seconds: float) -> None:
        self.seek(self._t + seconds)

    def _on_time_entered(self) -> None:
        value = self._read_time(self.time_field.text())
        if value is None:
            self.time_field.setText(format_timecode(self._t))
            return
        self._set_status("")
        self.seek(value)
        self.time_field.clearFocus()

    def _on_seek_requested(self, t: float) -> None:
        self.seek(t)

    def _on_scrubbing(self, t: float) -> None:
        self._pause()
        self._set_playhead(t)
        self._request_frame(self._t, fast=True)
        self._settle.start()

    def _on_scrub_finished(self, t: float) -> None:
        self._settle.stop()
        self._set_playhead(t)
        self._request_frame(self._t, fast=False)

    def _show_exact_frame(self) -> None:
        self._request_frame(self._t, fast=False)

    # -- playback ---------------------------------------------------------

    def toggle_play(self) -> None:
        if self._playing:
            self._pause()
            self._request_frame(self._t, fast=False)
        else:
            self._play()

    def _play(self) -> None:
        if self._closed or self.duration <= 0:
            return
        if self._t >= self.duration - 1.0 / self.fps:
            self._set_playhead(0.0, follow=True)
        self._settle.stop()
        self._play_id += 1
        session = self._play_id
        self._playing = True
        self.play_btn.setText("Pause")
        self.play_btn.setIcon(self._icon("SP_MediaPause"))
        size = display_size(
            int(getattr(self.info, "width", 0) or 0),
            int(getattr(self.info, "height", 0) or 0),
            int(getattr(self.info, "rotation", 0) or 0),
            str(getattr(self.info, "sar", "1:1") or "1:1"),
            *FRAME_MAX,
        )
        try:
            self.preview.start_playback(
                self.path,
                self._t,
                size,
                min(self.fps, 30.0),
                lambda t, rgb, _size=size: self._on_play_frame(t, rgb, _size),
                lambda reason, _id=session: self._bridge.play_end.emit(_id, reason),
            )
        except Exception:
            logger.debug("Could not start playback", exc_info=True)
            self._playing = False
            self._reset_play_button()

    def _pause(self) -> None:
        if not self._playing:
            return
        self._playing = False
        self._play_id += 1
        try:
            self.preview.stop_playback()
        except Exception:
            logger.debug("Could not stop playback", exc_info=True)
        self._reset_play_button()

    def _reset_play_button(self) -> None:
        self.play_btn.setText("Play")
        self.play_btn.setIcon(self._icon("SP_MediaPlay"))

    def _on_play_frame(self, t: float, rgb: bytes, size) -> None:
        """Worker thread: keep only the newest frame and wake the GUI once."""

        with self._play_lock:
            self._pending_play = (t, rgb, size)
            queued, self._tick_queued = self._tick_queued, True
        if not queued:
            self._bridge.play_tick.emit()

    def _drain_play(self) -> None:
        with self._play_lock:
            frame, self._pending_play = self._pending_play, None
            self._tick_queued = False
        if frame is None or self._closed or not self._playing:
            return
        t, rgb, (w, h) = frame
        self.video.set_pixmap(pixmap_from_rgb(rgb, w, h))
        self._set_playhead(t, follow=True)
        if t >= self.duration - 1.0 / self.fps:
            self._pause()

    def _on_play_end(self, session: int, reason: str) -> None:
        if session != self._play_id or self._closed:
            return
        self._playing = False
        self._reset_play_button()
        if reason == "error":
            self._set_status("Playback stopped. The frame view still works.", error=True)
        elif reason == "eof":
            self._set_playhead(self.duration, follow=True)
