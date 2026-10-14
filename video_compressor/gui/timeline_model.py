"""Pure logic behind the trim editor: timeline geometry and the editable clip list.

No Qt, no ffmpeg. ``TimelineView`` maps time to pixels for a zoomable ruler;
``ClipEditModel`` holds the clips being edited, the selection, the join/separate
mode and an undo history. The Qt widgets only call into this module and repaint.

Change notifications: ``ClipEditModel.subscribe`` callbacks run after *every*
state change (each edit, even inside a gesture, plus select, undo and redo), and
once more when ``end_gesture`` commits an undo step (so undo buttons can
refresh). Rejected or no-op calls never notify.

All clip times are whole milliseconds. Internally the model works in integer
milliseconds, so limits and minimum lengths are exact.
"""

from __future__ import annotations

import bisect
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from ..core.clips import (
    MAX_JOIN_CLIPS,
    MIN_CLIP_SECONDS,
    WHOLE_SOURCE_TOLERANCE,
    Clip,
    ClipMode,
    ClipPlan,
    find_overlaps,
    normalize_clips,
)
from ..core.utils import format_size, format_timecode, parse_timecode

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_PX_PER_SEC",
    "TimelineView",
    "ClipEditModel",
    "describe_selection",
    "format_tick_label",
    "format_timecode",
    "parse_time_field",
    "parse_timecode",
]

#: Most the ruler can be zoomed in (pixels per second of video).
MAX_PX_PER_SEC = 2000.0

#: "Nice" ruler steps in seconds, smallest first.
_TICK_STEPS: Tuple[float, ...] = (
    0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
    1, 2, 5, 10, 15, 30,
    60, 120, 300, 600, 1800, 3600,
    7200, 21600, 86400,
)  # fmt: skip
#: Minor ticks per major tick, by major step (default 5).
_MINOR_DIVISIONS = {
    0.02: 4, 0.2: 4, 2: 4, 15: 3, 30: 3, 60: 6, 120: 4, 1800: 6, 3600: 6, 7200: 4,
    21600: 6, 86400: 4,
}  # fmt: skip
_MAX_TICKS = 5000
_INF_MS = 10**15  # stands in for +/- infinity when converting to milliseconds
_SEP = " · "


def _finite(value: Any, default: float = 0.0) -> float:
    """``float(value)``, or ``default`` for NaN, infinity or junk."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _to_ms(value: Any) -> Optional[int]:
    """Seconds -> whole milliseconds. ``None`` for NaN or non-numbers; +/-inf saturate."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    if math.isinf(number):
        return _INF_MS if number > 0 else -_INF_MS
    return int(round(number * 1000.0))


def _from_ms(ms: int) -> float:
    return ms / 1000.0


def _hms(total_seconds: int) -> str:
    """``m:ss``, or ``h:mm:ss`` from one hour up."""
    hours, rest = divmod(total_seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _clock(seconds: float) -> str:
    """Whole-second clock text; a real span never reads ``0:00``."""
    seconds = max(0.0, _finite(seconds))
    total = int(seconds + 0.5)
    if total == 0 and seconds > 0:
        total = 1
    return _hms(total)


def format_tick_label(t: float, step: float) -> str:
    """Ruler label for time ``t`` when major ticks are ``step`` seconds apart.

    ``1:30`` / ``1:02:03`` for whole-second steps, with one or two decimals
    (``0:01.5``, ``0:01.25``) when the steps are finer than a second.
    """
    total_ms = max(0, int(round(_finite(t) * 1000.0)))
    if step >= 1:
        return _hms((total_ms + 500) // 1000)
    whole, frac = divmod(total_ms, 1000)
    decimals = 1 if step >= 0.1 else 2
    return f"{_hms(whole)}.{frac:03d}"[: len(_hms(whole)) + 1 + decimals]


# ── timeline geometry ────────────────────────────────────────────────────────


class TimelineView:
    """Time <-> pixel mapping for a horizontally zoomable, scrollable ruler.

    ``offset`` is the time (seconds) at the left edge and ``px_per_sec`` the zoom.
    Zoom stays between "fit" (the whole video exactly fills the width) and
    ``MAX_PX_PER_SEC`` (or fit, if that is already higher); ``offset`` stays inside
    the video. Both attributes may be set directly; call ``clamp_offset()`` after.
    """

    def __init__(self, duration: float, width_px: int) -> None:
        self.duration = max(0.0, _finite(duration))
        self.width_px = max(1, int(width_px))
        self.offset = 0.0
        self.px_per_sec = self.fit_px_per_sec

    # zoom limits

    @property
    def fit_px_per_sec(self) -> float:
        """Zoom at which the whole video fits the width (the minimum zoom)."""
        return self.width_px / self.duration if self.duration > 0 else 1.0

    @property
    def max_px_per_sec(self) -> float:
        return max(MAX_PX_PER_SEC, self.fit_px_per_sec)

    @property
    def is_fit(self) -> bool:
        """True while the whole video is visible (zoomed all the way out)."""
        return self.px_per_sec <= self.fit_px_per_sec * (1.0 + 1e-9)

    # mapping

    def time_to_x(self, t: float) -> float:
        return (t - self.offset) * self.px_per_sec

    def x_to_time(self, x: float) -> float:
        """Time under pixel ``x``, clamped to ``[0, duration]``."""
        return min(max(self.offset + x / self.px_per_sec, 0.0), self.duration)

    def visible_range(self) -> Tuple[float, float]:
        """``(t0, t1)`` currently on screen (``t1`` never past the end of the video)."""
        return self.offset, min(
            self.duration, self.offset + self.width_px / self.px_per_sec
        )

    # view changes (each returns True if the view changed)

    def fit(self) -> None:
        """Show the whole video."""
        self.px_per_sec = self.fit_px_per_sec
        self.offset = 0.0

    def clamp_offset(self) -> None:
        """Keep the visible window inside ``[0, duration]``."""
        limit = max(0.0, self.duration - self.width_px / self.px_per_sec)
        if limit < 1e-9:
            limit = 0.0
        self.offset = min(max(self.offset, 0.0), limit)

    def zoom_at(self, x_px: float, factor: float) -> bool:
        """Zoom by ``factor`` (>1 zooms in), keeping the time under ``x_px`` fixed.

        The anchor only moves when the offset limits force it (zooming out near an end).
        """
        if not factor > 0:
            return False
        x = min(max(_finite(x_px), 0.0), float(self.width_px))
        before = (self.offset, self.px_per_sec)
        anchor = self.offset + x / self.px_per_sec
        self.px_per_sec = min(
            max(self.px_per_sec * factor, self.fit_px_per_sec), self.max_px_per_sec
        )
        self.offset = anchor - x / self.px_per_sec
        self.clamp_offset()
        return (self.offset, self.px_per_sec) != before

    def set_width(self, px: int) -> None:
        """Resize the ruler. Stays fit if it was fit, otherwise keeps the time scale."""
        was_fit = self.is_fit
        self.width_px = max(1, int(px))
        if was_fit:
            self.fit()
            return
        self.px_per_sec = min(
            max(self.px_per_sec, self.fit_px_per_sec), self.max_px_per_sec
        )
        self.clamp_offset()

    def pan(self, dx_px: float) -> bool:
        """Scroll by ``dx_px`` pixels; positive moves toward later times."""
        before = self.offset
        self.offset += _finite(dx_px) / self.px_per_sec
        self.clamp_offset()
        return self.offset != before

    def ensure_visible(self, t: float, margin_px: float = 16.0) -> bool:
        """Scroll just enough to show time ``t`` (e.g. to follow the playhead)."""
        before = self.offset
        margin = min(max(0.0, margin_px), self.width_px / 2.0) / self.px_per_sec
        span = self.width_px / self.px_per_sec
        if t < self.offset + margin:
            self.offset = t - margin
        elif t > self.offset + span - margin:
            self.offset = t - span + margin
        self.clamp_offset()
        return self.offset != before

    # ruler

    def tick_step(self, min_px: float = 80.0) -> float:
        """Smallest "nice" step (seconds) whose ticks are at least ``min_px`` apart."""
        for step in _TICK_STEPS:
            if step * self.px_per_sec >= min_px - 1e-9:
                return float(step)
        return float(_TICK_STEPS[-1])

    def ticks(self, min_px: float = 80.0) -> List[Tuple[float, bool]]:
        """``(time, is_major)`` for the visible range, ascending.

        Majors fall on multiples of ``tick_step(min_px)`` (label those, see
        ``format_tick_label``); minors subdivide each major step.
        """
        min_px = max(float(min_px), 4.0)
        step = self.tick_step(min_px)
        divisions = _MINOR_DIVISIONS.get(step, 5)
        minor = step / divisions
        t0, t1 = self.visible_range()
        first = max(0, math.ceil(t0 / minor - 1e-9))
        last = min(math.floor(t1 / minor + 1e-9), first + _MAX_TICKS)
        return [
            (round(k * minor, 9), k % divisions == 0) for k in range(first, last + 1)
        ]

    # editing aids

    def snap(self, t: float, candidates: Iterable[float], tol_px: float = 8.0) -> float:
        """The nearest candidate within ``tol_px`` pixels of ``t``, else ``t``."""
        tolerance = tol_px / self.px_per_sec + 1e-12
        best: Optional[float] = None
        best_gap = 0.0
        for candidate in candidates:
            gap = abs(candidate - t)
            if gap <= tolerance and (best is None or gap < best_gap):
                best, best_gap = candidate, gap
        return t if best is None else best

    def hit_test(
        self, x_px: float, clips: Sequence[Any], handle_px: float = 6.0
    ) -> Optional[Tuple[int, str]]:
        """What is under pixel ``x_px``: ``(clip_index, "start" | "end" | "body")`` or None.

        A handle is the zone within ``handle_px`` of a clip edge; handles win over
        bodies. Between overlapping handles the closest edge wins. On a tie (for
        example the shared edge of two touching clips) the handle on the side of
        the cursor wins, so each clip's own edge stays grabbable; after that the
        clip later in time, then the end handle. Overlapping bodies resolve to
        the later clip.
        """
        order = sorted(range(len(clips)), key=lambda i: (clips[i].start, i))
        best_key: Optional[Tuple[float, int, int, int]] = None
        best: Optional[Tuple[int, str]] = None
        body: Optional[int] = None
        for rank, i in enumerate(order):
            x_start = self.time_to_x(clips[i].start)
            x_end = self.time_to_x(clips[i].end)
            for part_rank, (part, edge) in enumerate(
                (("start", x_start), ("end", x_end))
            ):
                distance = abs(x_px - edge)
                if distance > handle_px:
                    continue
                # A handle faces its clip's body: start looks right, end looks left.
                off_side = int(x_px < edge) if part == "start" else int(x_px > edge)
                key = (round(distance, 6), off_side, -rank, -part_rank)
                if best_key is None or key < best_key:
                    best_key, best = key, (i, part)
            if x_start <= x_px <= x_end:
                body = i
        if best is not None:
            return best
        if body is not None:
            return body, "body"
        return None


# ── clip list ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Snapshot:
    clips: Tuple[Clip, ...]
    selected: Optional[int]
    mode: ClipMode


class ClipEditModel:
    """The clips being edited, plus selection, mode and undo/redo.

    List order is export order (it matters for join mode). Neighbour limits while
    editing use *time* order, so reordering for a join never changes drag limits.
    Starts as one clip covering the whole video unless ``clips`` is given. Every
    edit validates and clamps; a request that cannot be honoured is a no-op that
    returns ``None``/``False``. A ``True``/index result means the request was valid
    (it may have been clamped, or have left the clip unchanged).
    """

    UNDO_LIMIT = 100

    def __init__(
        self,
        duration: float,
        fps: float = 30.0,
        clips: Optional[Sequence[Clip]] = None,
        mode: ClipMode = ClipMode.SEPARATE,
    ) -> None:
        seconds = _finite(duration)
        self._dur_ms = int(math.floor(seconds * 1000.0 + 1e-6))
        if self._dur_ms <= 0:
            raise ValueError("duration must be positive")
        #: Source length, floored to a whole millisecond so clips never pass the end.
        self.duration = _from_ms(self._dur_ms)
        fps = _finite(fps, 30.0)
        self.fps = min(max(fps if fps > 0 else 30.0, 1.0), 1000.0)
        self._min_ms = max(
            1, math.ceil(round(max(MIN_CLIP_SECONDS, 3.0 / self.fps) * 1000.0, 6))
        )
        #: Shortest clip the editor will make: ``max(0.1 s, 3 frames)``, in whole ms.
        self.min_len = _from_ms(self._min_ms)

        initial = normalize_clips(tuple(clips or ()), self.duration)
        self._clips: Tuple[Clip, ...] = initial or (Clip(0.0, self.duration),)
        self._selected: Optional[int] = 0
        self._mode = ClipMode(mode)

        self._undo: List[_Snapshot] = []
        self._redo: List[_Snapshot] = []
        self._observers: List[Callable[[], None]] = []
        self._gesture_depth = 0
        self._gesture_base: Optional[_Snapshot] = None
        self._gesture_dirty = False

    @classmethod
    def from_plan(
        cls, plan: Optional[ClipPlan], duration: float, fps: float = 30.0
    ) -> "ClipEditModel":
        """Model for an existing plan (``None`` or empty means the whole video)."""
        if plan is None or not plan.clips:
            return cls(duration, fps)
        return cls(duration, fps, plan.clips, plan.mode)

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def clips(self) -> Tuple[Clip, ...]:
        return self._clips

    @property
    def selected(self) -> Optional[int]:
        return self._selected

    @property
    def mode(self) -> ClipMode:
        return self._mode

    @property
    def count(self) -> int:
        return len(self._clips)

    @property
    def total_duration(self) -> float:
        """Seconds of video that will be encoded (overlaps count twice)."""
        return sum(clip.duration for clip in self._clips)

    @property
    def join_cap_exceeded(self) -> bool:
        return self._mode is ClipMode.JOIN and len(self._clips) > MAX_JOIN_CLIPS

    @property
    def is_whole_video(self) -> bool:
        """One clip covering the whole video, to within 50 ms."""
        return len(self._clips) == 1 and self._covers_whole(self._clips[0])

    def _covers_whole(self, clip: Clip) -> bool:
        tolerance = WHOLE_SOURCE_TOLERANCE + 1e-9
        return clip.start <= tolerance and clip.end >= self.duration - tolerance

    def overlaps(self) -> List[Tuple[int, int]]:
        """Index pairs of clips whose ranges overlap."""
        return find_overlaps(self._clips)

    def clip_at(self, t: float) -> Optional[int]:
        """Index of the clip under time ``t`` (``start <= t < end``; the end of the video counts).

        Overlaps resolve to the selected clip, else the one starting latest.
        """
        tm = _to_ms(t)
        if tm is None:
            return None
        hits = []
        for i in range(len(self._clips)):
            start, end = self._span(i)
            if start <= tm < end or (tm == end and end >= self._dur_ms):
                hits.append(i)
        if not hits:
            return None
        if self._selected in hits:
            return self._selected
        return max(hits, key=lambda i: (self._span(i)[0], i))

    def snap_points(self, exclude_index: Optional[int] = None) -> List[float]:
        """Sorted, de-duplicated starts and ends of all clips (minus ``exclude_index``)."""
        points = set()
        for i, clip in enumerate(self._clips):
            if i != exclude_index:
                points.add(clip.start)
                points.add(clip.end)
        return sorted(points)

    def limits(self, index: int) -> Optional[Tuple[float, float]]:
        """``(lo, hi)`` seconds clip ``index`` may span without touching its time neighbours."""
        if not self._valid(index):
            return None
        lo, hi = self._limits_ms(index)
        return _from_ms(lo), _from_ms(hi)

    # ── observers ────────────────────────────────────────────────────────────

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback()`` after every state change; returns a function that unsubscribes."""
        self._observers.append(callback)

        def unsubscribe() -> None:
            try:
                self._observers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def _notify(self) -> None:
        for callback in list(self._observers):
            try:
                callback()
            except Exception:  # an observer must not corrupt the model
                logger.exception("clip model observer failed")

    # ── internals ────────────────────────────────────────────────────────────

    def _valid(self, index: Any) -> bool:
        return (
            isinstance(index, int)
            and not isinstance(index, bool)
            and 0 <= index < len(self._clips)
        )

    def _span(self, index: int) -> Tuple[int, int]:
        clip = self._clips[index]
        return int(round(clip.start * 1000.0)), int(round(clip.end * 1000.0))

    def _limits_ms(self, index: int) -> Tuple[int, int]:
        start, end = self._span(index)
        lo, hi = 0, self._dur_ms
        for j in range(len(self._clips)):
            if j == index:
                continue
            other_start, other_end = self._span(j)
            if other_end <= start:
                lo = max(lo, other_end)
            elif other_start >= end:
                hi = min(hi, other_start)
        return lo, hi

    def _snapshot(self) -> _Snapshot:
        return _Snapshot(self._clips, self._selected, self._mode)

    def _push_undo(self, snap: _Snapshot) -> None:
        self._undo.append(snap)
        if len(self._undo) > self.UNDO_LIMIT:
            del self._undo[0]

    def _commit(
        self, clips: Tuple[Clip, ...], selected: Optional[int], mode: ClipMode
    ) -> bool:
        """Apply a new state. Records undo (or marks the open gesture dirty) when
        clips or mode changed; selection alone never creates an undo step."""
        changed = clips != self._clips or mode != self._mode
        if not changed and selected == self._selected:
            return False
        if changed:
            if self._gesture_depth:
                self._gesture_dirty = True
            else:
                self._push_undo(self._snapshot())
                self._redo.clear()
        self._clips, self._selected, self._mode = clips, selected, mode
        self._notify()
        return True

    def _replace(self, index: int, start_ms: int, end_ms: int) -> bool:
        label = self._clips[index].label
        clip = Clip(_from_ms(start_ms), _from_ms(end_ms), label)
        clips = self._clips[:index] + (clip,) + self._clips[index + 1 :]
        self._commit(clips, self._selected, self._mode)
        return True

    # ── edits ────────────────────────────────────────────────────────────────

    def add_clip(
        self, start: float, end: float, label: str = "", allow_overlap: bool = False
    ) -> Optional[int]:
        """Add ``start..end`` (clamped to the video, reversed ends swapped) and select it.

        Unless ``allow_overlap``, the range is trimmed to the free gap it covers
        most; it is refused (``None``) if nothing at least ``min_len`` long is left.
        The clip goes where the list stays ordered by start, or at the end when the
        list has been reordered. Returns the new clip's index.
        """
        a, b = _to_ms(start), _to_ms(end)
        if a is None or b is None:
            return None
        a, b = max(0, min(a, b)), min(self._dur_ms, max(a, b))
        if not allow_overlap:
            gap = self._best_gap(a, b)
            if gap is None:
                return None
            a, b = max(a, gap[0]), min(b, gap[1])
        if b - a < self._min_ms:
            return None
        starts = [self._span(i)[0] for i in range(len(self._clips))]
        if starts == sorted(starts):
            position = bisect.bisect_right(starts, a)
        else:
            position = len(self._clips)
        clip = Clip(_from_ms(a), _from_ms(b), str(label or "").strip())
        clips = self._clips[:position] + (clip,) + self._clips[position:]
        self._commit(clips, position, self._mode)
        return position

    def _best_gap(self, start_ms: int, end_ms: int) -> Optional[Tuple[int, int]]:
        """The free gap sharing the most time with ``start_ms..end_ms``."""
        spans = sorted(self._span(i) for i in range(len(self._clips)))
        gaps: List[Tuple[int, int]] = []
        cursor = 0
        for span_start, span_end in spans:
            if span_start > cursor:
                gaps.append((cursor, span_start))
            cursor = max(cursor, span_end)
        if cursor < self._dur_ms:
            gaps.append((cursor, self._dur_ms))
        best: Optional[Tuple[int, int]] = None
        best_overlap = 0
        for gap_start, gap_end in gaps:
            overlap = min(end_ms, gap_end) - max(start_ms, gap_start)
            if overlap > best_overlap:
                best, best_overlap = (gap_start, gap_end), overlap
        return best

    def set_in(self, index: int, t: float) -> bool:
        """Move a clip's start to ``t``, limited by its neighbour and ``min_len``."""
        tm = _to_ms(t)
        if tm is None or not self._valid(index):
            return False
        lo, _hi = self._limits_ms(index)
        _start, end = self._span(index)
        new_start = min(max(tm, lo), end - self._min_ms)
        if new_start < lo:
            return False
        return self._replace(index, new_start, end)

    def set_out(self, index: int, t: float) -> bool:
        """Move a clip's end to ``t``, limited by its neighbour and ``min_len``."""
        tm = _to_ms(t)
        if tm is None or not self._valid(index):
            return False
        _lo, hi = self._limits_ms(index)
        start, _end = self._span(index)
        new_end = max(min(tm, hi), start + self._min_ms)
        if new_end > hi:
            return False
        return self._replace(index, start, new_end)

    def resize(self, index: int, which: str, t: float) -> bool:
        """``set_in`` for ``which == "start"``, ``set_out`` for ``"end"``."""
        if which == "start":
            return self.set_in(index, t)
        if which == "end":
            return self.set_out(index, t)
        return False

    def set_times(self, index: int, start: float, end: float) -> bool:
        """Set both ends (clamped to the neighbours; at least ``min_len`` long)."""
        a, b = _to_ms(start), _to_ms(end)
        if a is None or b is None or not self._valid(index):
            return False
        lo, hi = self._limits_ms(index)
        if hi - lo < self._min_ms:
            return False
        new_start = min(max(a, lo), hi - self._min_ms)
        new_end = min(max(b, new_start + self._min_ms), hi)
        return self._replace(index, new_start, new_end)

    def move(self, index: int, delta_seconds: float) -> bool:
        """Shift a clip by ``delta_seconds``, keeping its length, stopped by neighbours."""
        delta = _to_ms(delta_seconds)
        if delta is None or not self._valid(index):
            return False
        lo, hi = self._limits_ms(index)
        start, end = self._span(index)
        length = end - start
        new_start = min(max(start + delta, lo), hi - length)
        if new_start < lo:
            return False
        return self._replace(index, new_start, new_start + length)

    def split_at(self, t: float) -> Optional[int]:
        """Split the clip under ``t`` in two (each half at least ``min_len``).

        The left half keeps the label, the right half is unlabeled and gets
        selected. Returns the right half's index, or ``None`` if nothing was split.
        """
        tm = _to_ms(t)
        index = self.clip_at(t)
        if tm is None or index is None:
            return None
        start, end = self._span(index)
        if tm - start < self._min_ms or end - tm < self._min_ms:
            return None
        left = Clip(_from_ms(start), _from_ms(tm), self._clips[index].label)
        right = Clip(_from_ms(tm), _from_ms(end), "")
        clips = self._clips[:index] + (left, right) + self._clips[index + 1 :]
        self._commit(clips, index + 1, self._mode)
        return index + 1

    def delete(self, index: int) -> bool:
        """Remove a clip. The last remaining clip cannot be deleted (use ``reset_to_whole``)."""
        if not self._valid(index) or len(self._clips) <= 1:
            return False
        clips = self._clips[:index] + self._clips[index + 1 :]
        selected = self._selected
        if selected is not None:
            if selected == index:
                selected = min(index, len(clips) - 1)
            elif selected > index:
                selected -= 1
        self._commit(clips, selected, self._mode)
        return True

    def reorder(self, index: int, new_index: int) -> Optional[int]:
        """Move a clip within the list (export order). Selection follows the clips.

        ``new_index`` is clamped to the list. Returns the clip's final index.
        """
        if not self._valid(index) or not isinstance(new_index, int):
            return None
        new_index = max(0, min(new_index, len(self._clips) - 1))
        if new_index == index:
            return index
        items = list(self._clips)
        items.insert(new_index, items.pop(index))
        selected = self._selected
        if selected is not None:
            if selected == index:
                selected = new_index
            elif index < selected <= new_index:
                selected -= 1
            elif new_index <= selected < index:
                selected += 1
        self._commit(tuple(items), selected, self._mode)
        return new_index

    def rename(self, index: int, label: str) -> bool:
        if not self._valid(index):
            return False
        clip = self._clips[index]
        clips = (
            self._clips[:index]
            + (Clip(clip.start, clip.end, str(label or "").strip()),)
            + self._clips[index + 1 :]
        )
        self._commit(clips, self._selected, self._mode)
        return True

    def set_mode(self, mode: ClipMode) -> bool:
        """Switch between separate files and one joined file (undoable)."""
        try:
            new_mode = ClipMode(mode)
        except ValueError:
            return False
        self._commit(self._clips, self._selected, new_mode)
        return True

    def select(self, index: Optional[int]) -> bool:
        """Select a clip (``None`` clears). Not an undo step."""
        if index is not None and not self._valid(index):
            return False
        self._commit(self._clips, index, self._mode)
        return True

    def reset_to_whole(self) -> bool:
        """Replace everything with one clip covering the whole video. Undoable."""
        return self._commit((Clip(0.0, self.duration),), 0, self._mode)

    # ── undo / redo ──────────────────────────────────────────────────────────

    @property
    def can_undo(self) -> bool:
        return not self._gesture_depth and bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return not self._gesture_depth and bool(self._redo)

    @property
    def in_gesture(self) -> bool:
        return self._gesture_depth > 0

    def begin_gesture(self) -> None:
        """Start grouping edits (a drag) into a single undo step. Calls may nest."""
        if self._gesture_depth == 0:
            self._gesture_base = self._snapshot()
            self._gesture_dirty = False
        self._gesture_depth += 1

    def end_gesture(self) -> bool:
        """Finish the gesture. Returns True if it left an undo step behind."""
        if self._gesture_depth == 0:
            return False
        self._gesture_depth -= 1
        if self._gesture_depth:
            return False
        base, dirty = self._gesture_base, self._gesture_dirty
        self._gesture_base, self._gesture_dirty = None, False
        if not dirty or base is None:
            return False
        self._push_undo(base)
        self._redo.clear()
        self._notify()
        return True

    def cancel_gesture(self) -> bool:
        """Abandon the gesture and restore the state from before it (e.g. Esc mid-drag)."""
        if self._gesture_depth == 0:
            return False
        base, dirty = self._gesture_base, self._gesture_dirty
        self._gesture_depth, self._gesture_base, self._gesture_dirty = 0, None, False
        if not dirty or base is None:
            return False
        self._restore(base)
        return True

    def undo(self) -> bool:
        if not self.can_undo:
            return False
        self._redo.append(self._snapshot())
        self._restore(self._undo.pop())
        return True

    def redo(self) -> bool:
        if not self.can_redo:
            return False
        self._push_undo(self._snapshot())
        self._restore(self._redo.pop())
        return True

    def _restore(self, snap: _Snapshot) -> None:
        self._clips, self._selected, self._mode = snap.clips, snap.selected, snap.mode
        self._notify()

    # ── output ───────────────────────────────────────────────────────────────

    def to_plan(self) -> Optional[ClipPlan]:
        """The plan to encode, or ``None`` when the whole video is kept as is."""
        clips = normalize_clips(self._clips, self.duration)
        if not clips:
            return None
        if len(clips) == 1 and self._covers_whole(clips[0]):
            return None
        return ClipPlan(clips, self._mode)


# ── text helpers ─────────────────────────────────────────────────────────────

_TIME_FIELD = re.compile(r"(?:[0-9]+:){0,2}(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", re.ASCII)


def parse_time_field(text: Optional[str], duration: float) -> Optional[float]:
    """Parse a typed time (``90``, ``1:30``, ``1:30.5``, ``00:01:30.250``) into seconds.

    Returns ``None`` for anything invalid or past ``duration`` (a 1 ms overshoot is
    clamped to ``duration``). The result is rounded to whole milliseconds.
    """
    raw = str(text if text is not None else "").strip()
    if not _TIME_FIELD.fullmatch(raw):
        return None
    seconds = parse_timecode(raw)
    if seconds is None or not math.isfinite(seconds):
        return None
    if duration is not None and seconds > duration + 0.001:
        return None
    seconds = round(seconds * 1000.0) / 1000.0
    return seconds if duration is None else min(seconds, duration)


def describe_selection(
    model: ClipEditModel, source_size_bytes: Optional[int] = None
) -> str:
    """One-line summary such as ``3 clips · 1:23 of 10:00 · about 143.05 MB of source``.

    The size part appears only when ``source_size_bytes`` is given; it is the
    source size scaled by the share of the video the clips cover.
    """
    count = model.count
    parts = [
        f"{count} clip" if count == 1 else f"{count} clips",
        f"{_clock(model.total_duration)} of {_clock(model.duration)}",
    ]
    if source_size_bytes and source_size_bytes > 0 and model.duration > 0:
        share = min(model.total_duration, model.duration) / model.duration
        parts.append(f"about {format_size(source_size_bytes * share)} of source")
    return _SEP.join(parts)
