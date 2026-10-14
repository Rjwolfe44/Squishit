"""Timeline widget for the trim editor: ruler, filmstrip, clip regions, playhead.

All the geometry and editing rules live in ``gui/timeline_model.py`` (pure,
tested). This file only paints that state and turns mouse input into model
calls, so it stays thin.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
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

from ...core.utils import format_timecode
from ..timeline_model import ClipEditModel, TimelineView, format_tick_label

RULER_H = 26
TRACK_H = 76
HANDLE_W = 9
_BG = QColor("#0d0d0f")
_TRACK = QColor("#18181b")
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
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMinimumHeight(RULER_H + TRACK_H + 6)
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

    def zoom(self, factor: float) -> None:
        centre = self.width() / 2.0
        if self.view.zoom_at(centre, factor):
            self.viewChanged.emit()
        self.update()

    def fit(self) -> None:
        self.view.fit()
        self.viewChanged.emit()
        self.update()

    def dispose(self) -> None:
        """Stop listening to the model (the dialog is going away)."""

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
        return QRectF(x0, RULER_H, max(1.0, x1 - x0), TRACK_H)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        track = QRectF(0, RULER_H, width, TRACK_H)
        painter.fillRect(self.rect(), _BG)
        painter.fillRect(track, _TRACK)
        self._paint_filmstrip(painter, track)
        self._paint_dimming(painter, track)
        self._paint_ruler(painter)
        self._paint_clips(painter)
        self._paint_create(painter)
        self._paint_playhead(painter)
        self._paint_hover(painter)
        painter.end()

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
        rect = QRectF(min(a, b), RULER_H, abs(b - a), TRACK_H)
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
        painter.drawLine(QPointF(x, 0), QPointF(x, RULER_H + TRACK_H))
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
        candidates.append(self._playhead)
        return self.view.snap(t, candidates)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x, y = event.position().x(), event.position().y()
        self._drag = None
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
            self._drag = {"kind": "edge", "index": index, "which": part}
        else:
            clip = self.model.clips[index]
            self._drag = {
                "kind": "move",
                "index": index,
                "press_x": x,
                "anchor_t": self.view.x_to_time(x),
                "origin": clip.start,
                "moved": False,
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
        if kind == "playhead":
            self._scrub_to(x)
        elif kind == "edge":
            self._autopan(x)
            index = drag["index"]
            t = self._snap(self.view.x_to_time(x), index, event)
            self.model.resize(index, drag["which"], t)
            clip = self.model.clips[index]
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
