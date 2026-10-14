"""Timeline widget for the trim editor: ruler, filmstrip, sound wave, clip regions, playhead.

All the geometry and editing rules live in ``gui/timeline_model.py`` (pure,
tested). This file only paints that state and turns mouse input into model
calls, so it stays thin.
"""

from __future__ import annotations

import array
import math
from typing import Optional, Sequence

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...core.preview import wave_level
from ...core.utils import format_timecode
from ..timeline_model import ClipEditModel, TimelineView, format_tick_label

RULER_H = 26
TRACK_H = 76
#: Height of the optional sound wave lane under the filmstrip.
WAVE_H = 44
HANDLE_W = 9
_BG = QColor("#0d0d0f")
_TRACK = QColor("#18181b")
_WAVE_BG = QColor("#111114")
_WAVE = QColor("#2dd4bf")
_WAVE_MID = QColor("#27272a")
_TEXT = QColor("#d4d4d8")
_TICK = QColor("#71717a")
_ACCENT = QColor("#3b82f6")
_ACCENT_LIGHT = QColor("#93c5fd")
_PLAYHEAD = QColor("#fb7185")
_CLICK_SLOP_PX = 3


class TimelineWidget(QWidget):
    """Shows the whole video as a strip with the chosen clips drawn on top.

    * Click the ruler or an empty part of the strip to move the playhead.
    * Drag a clip's edge to trim it, drag its middle to move it.
    * Drag across empty space to make a new clip.
    * Mouse wheel zooms around the pointer; Shift + wheel scrolls.
    * Holding Alt while dragging turns edge snapping off.

    The optional sound wave lane (``set_wave_visible``) sits under the
    filmstrip and follows the same zoom and scroll; clips, dimming and the
    playhead are drawn over it, and it edits like the filmstrip.
    """

    seekRequested = Signal(float)  # a click landed here: show this frame properly
    scrubbing = Signal(float)  # dragging: show a quick frame at this time
    scrubFinished = Signal(float)  # drag ended: show the exact frame
    viewChanged = Signal()

    def __init__(self, model: ClipEditModel, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.model = model
        self.view = TimelineView(model.duration, 600)
        self._playhead = 0.0
        self._thumbs: dict[int, QPixmap] = {}
        self._thumb_count = 0
        self._drag: Optional[dict] = None
        self._hover_x: Optional[float] = None
        self._create_rect: Optional[tuple[float, float]] = None
        self._wave_visible = False
        self._wave = array.array("f")  # peak per bucket, 0..1; -1 = not measured yet
        self._wave_version = 0
        self._wave_message = ""
        self._wave_cache: Optional[tuple] = None  # (key, pixmap)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._apply_height()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(
            "Click to move the playhead. Drag a clip's edge to trim it, or drag "
            "its middle to move it. Mouse wheel zooms; Shift + wheel scrolls."
        )
        self._unsubscribe = model.subscribe(self._on_model_changed)

    # -- public ---------------------------------------------------------

    @property
    def playhead(self) -> float:
        return self._playhead

    def set_playhead(self, t: float, follow: bool = False) -> None:
        self._playhead = max(0.0, min(float(t), self.model.duration))
        if follow and self.view.ensure_visible(self._playhead):
            self.viewChanged.emit()
        self.update()

    def set_filmstrip(self, count: int) -> None:
        self._thumb_count = max(0, int(count))
        self._thumbs.clear()
        self.update()

    def set_thumbnail(self, index: int, pixmap: QPixmap) -> None:
        if 0 <= index < self._thumb_count and pixmap is not None and not pixmap.isNull():
            self._thumbs[index] = pixmap
            self.update()

    # -- sound wave -----------------------------------------------------

    @property
    def wave_visible(self) -> bool:
        return self._wave_visible

    @property
    def track_height(self) -> int:
        """Height of everything under the ruler: filmstrip, plus the sound wave if shown."""
        return TRACK_H + (WAVE_H if self._wave_visible else 0)

    def set_wave_visible(self, visible: bool) -> None:
        visible = bool(visible)
        if visible == self._wave_visible:
            return
        self._wave_visible = visible
        self._apply_height()
        self.update()

    def set_wave_buckets(self, count: int) -> None:
        """Start a new sound wave of ``count`` buckets, none measured yet."""
        self._wave = array.array("f", [-1.0]) * max(0, int(count))
        self._wave_version += 1
        self.update()

    def set_wave_peaks(self, first: int, peaks: Sequence[float]) -> None:
        """Fill buckets ``first..`` with measured peaks (0..1); out-of-range ones are ignored."""
        count = len(self._wave)
        first = int(first)
        for offset, peak in enumerate(peaks):
            index = first + offset
            if 0 <= index < count:
                self._wave[index] = max(0.0, min(1.0, float(peak)))
        self._wave_version += 1
        if self._wave_visible:
            self.update()

    def set_wave_message(self, text: str) -> None:
        """Words shown in the lane while there is no wave to draw (e.g. still reading)."""
        self._wave_message = str(text or "")
        if self._wave_visible:
            self.update()

    @property
    def wave_measured(self) -> int:
        """How many buckets have a measured peak (tests, progress)."""
        return sum(1 for peak in self._wave if peak >= 0.0)

    def wave_rect(self) -> QRectF:
        """Where the sound wave lane is drawn (empty when hidden)."""
        if not self._wave_visible:
            return QRectF()
        return QRectF(0, RULER_H + TRACK_H, self.width(), WAVE_H)

    def _apply_height(self) -> None:
        self.setFixedHeight(RULER_H + self.track_height + 6)
        self.updateGeometry()

    def zoom(self, factor: float) -> None:
        centre = self.width() / 2.0
        if self.view.zoom_at(centre, factor):
            self.viewChanged.emit()
        self.update()

    def fit(self) -> None:
        self.view.fit()
        self.viewChanged.emit()
        self.update()

    @property
    def is_dragging(self) -> bool:
        return self._drag is not None

    def cancel_drag(self) -> bool:
        """Abandon a drag in progress and undo what it changed (Esc). True if one was open."""

        return self._abort_drag(commit=False)

    def finish_drag(self) -> bool:
        """End a drag in progress as if the button was released here."""

        return self._abort_drag(commit=True)

    def _abort_drag(self, *, commit: bool) -> bool:
        drag, self._drag = self._drag, None
        self._create_rect = None
        if drag is None:
            return False
        if drag["kind"] in ("edge", "move"):
            if commit:
                self.model.end_gesture()
            else:
                self.model.cancel_gesture()
        self.update()
        return True

    def dispose(self) -> None:
        """Stop listening to the model (the dialog is going away)."""

        self._abort_drag(commit=False)
        try:
            self._unsubscribe()
        except Exception:
            pass

    # -- model / size ---------------------------------------------------

    def _on_model_changed(self) -> None:
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.view.set_width(max(50, self.width()))
        super().resizeEvent(event)

    # -- painting -------------------------------------------------------

    def _clip_rect(self, clip) -> QRectF:
        x0 = self.view.time_to_x(clip.start)
        x1 = self.view.time_to_x(clip.end)
        return QRectF(x0, RULER_H, max(1.0, x1 - x0), self.track_height)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        film = QRectF(0, RULER_H, width, TRACK_H)
        painter.fillRect(self.rect(), _BG)
        painter.fillRect(film, _TRACK)
        self._paint_filmstrip(painter, film)
        if self._wave_visible:
            self._paint_wave(painter, self.wave_rect())
        self._paint_dimming(painter, QRectF(0, RULER_H, width, self.track_height))
        self._paint_ruler(painter)
        self._paint_clips(painter)
        self._paint_create(painter)
        self._paint_playhead(painter)
        self._paint_hover(painter)
        painter.end()

    def _paint_wave(self, painter: QPainter, lane: QRectF) -> None:
        painter.fillRect(lane, _WAVE_BG)
        mid = lane.top() + lane.height() / 2.0
        painter.setPen(QPen(_WAVE_MID, 1))
        painter.drawLine(QPointF(lane.left(), mid), QPointF(lane.right(), mid))
        pixmap = self._wave_pixmap(int(lane.width()), int(lane.height()))
        if pixmap is not None:
            painter.drawPixmap(QPointF(lane.left(), lane.top()), pixmap)
        elif self._wave_message:
            painter.setPen(QPen(_TICK, 1))
            painter.drawText(lane, Qt.AlignmentFlag.AlignCenter, self._wave_message)

    def _wave_pixmap(self, width: int, height: int) -> Optional[QPixmap]:
        """The bars for the visible stretch, cached until the view or the data change."""
        if width <= 0 or height <= 0 or not len(self._wave) or self.view.duration <= 0:
            return None
        ratio = max(1.0, float(self.devicePixelRatioF()))
        key = (
            round(self.view.offset, 9),
            round(self.view.px_per_sec, 9),
            width,
            height,
            ratio,
            self._wave_version,
        )
        if self._wave_cache is not None and self._wave_cache[0] == key:
            return self._wave_cache[1]
        lines = self._wave_lines(width, height)
        if not lines:
            self._wave_cache = (key, None)
            return None
        pixmap = QPixmap(int(width * ratio), int(height * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setPen(QPen(_WAVE, 1))
        painter.drawLines(lines)
        painter.end()
        self._wave_cache = (key, pixmap)
        return pixmap

    def _wave_lines(self, width: int, height: int) -> list:
        """One vertical bar per pixel column: the loudest bucket under that column."""
        data = self._wave
        count = len(data)
        duration = self.view.duration
        per_second = count / duration
        pps = self.view.px_per_sec
        offset = self.view.offset
        mid = height / 2.0
        half = max(1.0, mid - 3.0)
        lines = []
        for x in range(width):
            t0 = offset + x / pps
            if t0 >= duration:
                break
            first = int(t0 * per_second)
            last = max(first + 1, int(math.ceil((t0 + 1.0 / pps) * per_second)))
            peak = max(data[first:min(last, count)], default=-1.0)
            if peak <= 0.0:
                continue  # silent, or not measured yet
            reach = max(0.5, wave_level(peak) * half)
            lines.append(QLineF(x + 0.5, mid - reach, x + 0.5, mid + reach))
        return lines

    def _paint_filmstrip(self, painter: QPainter, track: QRectF) -> None:
        count = self._thumb_count
        if not count or not self._thumbs:
            return
        duration = self.view.duration
        painter.save()
        painter.setClipRect(track)
        for index, pixmap in self._thumbs.items():
            x0 = self.view.time_to_x(index * duration / count)
            x1 = self.view.time_to_x((index + 1) * duration / count)
            if x1 < 0 or x0 > track.width():
                continue
            target = QRectF(x0, track.top(), max(1.0, x1 - x0), track.height())
            pw, ph = pixmap.width(), pixmap.height()
            if pw <= 0 or ph <= 0:
                continue
            target_ratio = target.width() / target.height()
            if pw / ph > target_ratio:  # source is wider: crop the sides
                sw = ph * target_ratio
                source = QRectF((pw - sw) / 2.0, 0, sw, ph)
            else:  # source is taller: crop top and bottom
                sh = pw / target_ratio
                source = QRectF(0, (ph - sh) / 2.0, pw, sh)
            painter.drawPixmap(target, pixmap, source)
        painter.restore()

    def _paint_dimming(self, painter: QPainter, track: QRectF) -> None:
        dim = QPainterPath()
        dim.addRect(track)
        for clip in self.model.clips:
            cut = QPainterPath()
            cut.addRect(self._clip_rect(clip))
            dim = dim.subtracted(cut)
        painter.fillPath(dim, QColor(0, 0, 0, 175))

    def _paint_ruler(self, painter: QPainter) -> None:
        painter.fillRect(QRectF(0, 0, self.width(), RULER_H), _BG)
        painter.setPen(QPen(_TICK, 1))
        painter.drawLine(0, RULER_H - 1, self.width(), RULER_H - 1)
        font = QFont(self.font())
        font.setPointSizeF(max(7.5, font.pointSizeF() - 1.5))
        painter.setFont(font)
        step = self.view.tick_step()
        for t, major in self.view.ticks():
            x = self.view.time_to_x(t)
            if major:
                painter.setPen(QPen(_TICK, 1))
                painter.drawLine(QPointF(x, RULER_H - 11), QPointF(x, RULER_H - 1))
                painter.setPen(QPen(_TEXT, 1))
                painter.drawText(
                    QRectF(x + 3, 0, 80, RULER_H - 10),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    format_tick_label(t, step),
                )
            else:
                painter.setPen(QPen(_TICK, 1))
                painter.drawLine(QPointF(x, RULER_H - 5), QPointF(x, RULER_H - 1))

    def _paint_clips(self, painter: QPainter) -> None:
        selected = self.model.selected
        font = QFont(self.font())
        font.setBold(True)
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1.0))
        for index, clip in enumerate(self.model.clips):
            rect = self._clip_rect(clip)
            if rect.right() < 0 or rect.left() > self.width():
                continue
            is_selected = index == selected
            fill = QColor(_ACCENT)
            fill.setAlpha(55 if is_selected else 28)
            painter.fillRect(rect, fill)
            pen = QPen(_ACCENT_LIGHT if is_selected else _ACCENT, 2.5 if is_selected else 1.5)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(1, 1, -1, -1))
            for edge_x in (rect.left(), rect.right() - HANDLE_W):
                handle = QRectF(edge_x, rect.top(), HANDLE_W, rect.height())
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(_ACCENT_LIGHT if is_selected else _ACCENT)
                painter.drawRoundedRect(handle, 3, 3)
                painter.setPen(QPen(QColor(255, 255, 255, 190), 1))
                mid = handle.center().x()
                painter.drawLine(QPointF(mid - 1.5, rect.center().y() - 9), QPointF(mid - 1.5, rect.center().y() + 9))
                painter.drawLine(QPointF(mid + 1.5, rect.center().y() - 9), QPointF(mid + 1.5, rect.center().y() + 9))
            if rect.width() > 46:
                painter.setFont(font)
                badge = QRectF(rect.left() + HANDLE_W + 4, rect.top() + 4, 22, 18)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(0, 0, 0, 170))
                painter.drawRoundedRect(badge, 8, 8)
                painter.setPen(QPen(QColor("#fafafa"), 1))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(index + 1))

    def _paint_create(self, painter: QPainter) -> None:
        if self._create_rect is None:
            return
        a, b = self._create_rect
        rect = QRectF(min(a, b), RULER_H, abs(b - a), self.track_height)
        fill = QColor(_ACCENT)
        fill.setAlpha(70)
        painter.fillRect(rect, fill)
        painter.setPen(QPen(_ACCENT_LIGHT, 1.5, Qt.PenStyle.DashLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

    def _paint_playhead(self, painter: QPainter) -> None:
        x = self.view.time_to_x(self._playhead)
        if x < -2 or x > self.width() + 2:
            return
        painter.setPen(QPen(_PLAYHEAD, 2))
        painter.drawLine(QPointF(x, 0), QPointF(x, RULER_H + self.track_height))
        head = QPainterPath()
        head.moveTo(x - 6, 0)
        head.lineTo(x + 6, 0)
        head.lineTo(x, 9)
        head.closeSubpath()
        painter.fillPath(head, _PLAYHEAD)

    def _paint_hover(self, painter: QPainter) -> None:
        if self._hover_x is None or self._drag is not None:
            return
        t = self.view.x_to_time(self._hover_x)
        text = format_timecode(t)
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1.0))
        painter.setFont(font)
        width = painter.fontMetrics().horizontalAdvance(text) + 12
        x = min(max(2.0, self._hover_x - width / 2.0), self.width() - width - 2.0)
        box = QRectF(x, RULER_H + 4, width, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 200))
        painter.drawRoundedRect(box, 5, 5)
        painter.setPen(QPen(QColor("#fafafa"), 1))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    # -- mouse ----------------------------------------------------------

    def _snap(self, t: float, exclude: Optional[int], event) -> float:
        if event.modifiers() & Qt.KeyboardModifier.AltModifier:
            return t
        candidates = list(self.model.snap_points(exclude_index=exclude))
        # The editor moves the playhead onto the edge being dragged; snapping to
        # that moving target would pull the edge back. Use where it was at press.
        fixed = self._drag.get("playhead") if self._drag else None
        candidates.append(self._playhead if fixed is None else fixed)
        return self.view.snap(t, candidates)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x, y = event.position().x(), event.position().y()
        self._abort_drag(commit=True)  # a release may have been lost
        if y < RULER_H:
            self._drag = {"kind": "playhead"}
            self._scrub_to(x)
            return
        hit = self.view.hit_test(x, self.model.clips)
        if hit is None:
            self._drag = {"kind": "create", "anchor_x": x, "anchor_t": self.view.x_to_time(x)}
            return
        index, part = hit
        self.model.select(index)
        self.model.begin_gesture()
        if part in ("start", "end"):
            self._drag = {
                "kind": "edge",
                "index": index,
                "which": part,
                "playhead": self._playhead,
                "expect": self.model.clips[index],
            }
        else:
            clip = self.model.clips[index]
            self._drag = {
                "kind": "move",
                "index": index,
                "press_x": x,
                "anchor_t": self.view.x_to_time(x),
                "origin": clip.start,
                "moved": False,
                "playhead": self._playhead,
                "expect": clip,
            }

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        x = event.position().x()
        drag = self._drag
        if drag is None:
            self._hover_x = x
            self._update_cursor(x, event.position().y())
            self.update()
            return
        kind = drag["kind"]
        if kind in ("edge", "move") and not self._drag_target_ok(drag):
            # The list changed under the mouse (Delete, Undo...): stop dragging
            # but keep what happened, rather than guess which clip to move.
            self._abort_drag(commit=True)
            return
        if kind == "playhead":
            self._scrub_to(x)
        elif kind == "edge":
            self._autopan(x)
            index = drag["index"]
            t = self._snap(self.view.x_to_time(x), index, event)
            self.model.resize(index, drag["which"], t)
            clip = self.model.clips[index]
            drag["expect"] = clip
            self.scrubbing.emit(clip.start if drag["which"] == "start" else clip.end)
        elif kind == "move":
            if abs(x - drag["press_x"]) > _CLICK_SLOP_PX:
                drag["moved"] = True
            if drag["moved"]:
                self._autopan(x)
                index = drag["index"]
                clip = self.model.clips[index]
                wanted = drag["origin"] + (self.view.x_to_time(x) - drag["anchor_t"])
                # snap whichever edge lands nearest something
                length = clip.end - clip.start
                snapped_start = self._snap(wanted, index, event)
                snapped_end = self._snap(wanted + length, index, event) - length
                if abs(snapped_start - wanted) <= abs(snapped_end - wanted):
                    wanted = snapped_start
                else:
                    wanted = snapped_end
                self.model.move(index, wanted - clip.start)
                drag["expect"] = self.model.clips[index]
                self.scrubbing.emit(self.model.clips[index].start)
        elif kind == "create":
            if abs(x - drag["anchor_x"]) > _CLICK_SLOP_PX + 1:
                self._create_rect = (drag["anchor_x"], x)
                self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self._drag is None:
            return
        drag, self._drag = self._drag, None
        x = event.position().x()
        kind = drag["kind"]
        if kind in ("edge", "move") and not self._drag_target_ok(drag):
            self.model.end_gesture()
            self.update()
            return
        if kind == "playhead":
            self.scrubFinished.emit(self._playhead)
        elif kind == "edge":
            self.model.end_gesture()
            clip = self.model.clips[drag["index"]]
            self.scrubFinished.emit(clip.start if drag["which"] == "start" else clip.end)
        elif kind == "move":
            self.model.end_gesture()
            if drag["moved"]:
                self.scrubFinished.emit(self.model.clips[drag["index"]].start)
            else:
                # a plain click on a clip just moves the playhead there
                self._seek_at(x)
        elif kind == "create":
            made = False
            if self._create_rect is not None:
                a = self.view.x_to_time(drag["anchor_x"])
                b = self.view.x_to_time(x)
                self.model.begin_gesture()
                made = self.model.add_clip(min(a, b), max(a, b)) is not None
                self.model.end_gesture()
            self._create_rect = None
            if not made:
                self._seek_at(drag["anchor_x"])
            self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_x = None
        self.update()
        super().leaveEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        delta = event.angleDelta().y() or event.angleDelta().x()
        if not delta:
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            changed = self.view.pan(-delta / 120.0 * 80.0)
        else:
            changed = self.view.zoom_at(event.position().x(), 1.25 ** (delta / 120.0))
        if changed:
            self.viewChanged.emit()
        self.update()
        event.accept()

    # -- helpers --------------------------------------------------------

    def _drag_target_ok(self, drag: dict) -> bool:
        """True while the clip being dragged is still the one the drag started on.

        Delete, Undo or a typed edit can change the list under the mouse; the
        index alone would then point at a different clip.
        """
        index = drag["index"]
        return 0 <= index < self.model.count and self.model.clips[index] == drag["expect"]

    def _scrub_to(self, x: float) -> None:
        t = self.view.x_to_time(x)
        self._playhead = t
        self.update()
        self.scrubbing.emit(t)

    def _seek_at(self, x: float) -> None:
        t = self.view.x_to_time(x)
        self._playhead = t
        self.update()
        self.seekRequested.emit(t)

    def _autopan(self, x: float) -> None:
        """Nudge the view when a drag reaches either end of the strip."""

        if x < 14 and self.view.pan(-18):
            self.viewChanged.emit()
        elif x > self.width() - 14 and self.view.pan(18):
            self.viewChanged.emit()

    def _update_cursor(self, x: float, y: float) -> None:
        shape = Qt.CursorShape.ArrowCursor
        if y >= RULER_H:
            hit = self.view.hit_test(x, self.model.clips)
            if hit is not None:
                shape = (
                    Qt.CursorShape.SizeHorCursor
                    if hit[1] in ("start", "end")
                    else Qt.CursorShape.OpenHandCursor
                )
            else:
                shape = Qt.CursorShape.CrossCursor
        self.setCursor(QCursor(shape))
