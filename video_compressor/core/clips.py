"""Clip plans: cut one or more time ranges out of a video.

This module is pure (no Qt, no ffmpeg calls). It defines what to encode; the
compressor decides how. Clips are kept per job, never in a saved profile, so a
custom profile can't silently trim every file.

Typical flow::

    plan = ClipPlan((Clip(5, 10), Clip(30, 42)), ClipMode.SEPARATE)
    plan = resolve_clip_plan(plan, profile, source_duration)   # normalized or None
    for job in expand_clip_jobs(plan):                         # 1 job per clip, or 1 joined job
        compressor.compress(..., clips=job.plan)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from .utils import parse_timecode, sanitize_filename

#: Shortest clip we keep. Anything shorter is dropped by ``normalize_clips``.
MIN_CLIP_SECONDS = 0.1
#: Soft cap for "join" mode (every clip is another ffmpeg input).
MAX_JOIN_CLIPS = 30
#: A clip that covers the whole source to within this many seconds means "no trim".
WHOLE_SOURCE_TOLERANCE = 0.05


class ClipMode(str, Enum):
    """What to do when a source has more than one clip."""

    SEPARATE = "separate"  # one output file per clip
    JOIN = "join"  # all clips, in list order, in one output file


@dataclass(frozen=True)
class Clip:
    """A time range of the source, in seconds. ``end`` may be ``math.inf`` ("to the end")."""

    start: float
    end: float
    label: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True)
class ClipPlan:
    """The clips to encode for one source file, in output order."""

    clips: Tuple[Clip, ...]
    mode: ClipMode = ClipMode.SEPARATE

    def __post_init__(self) -> None:
        object.__setattr__(self, "clips", tuple(self.clips))
        object.__setattr__(self, "mode", ClipMode(self.mode))

    @property
    def total_duration(self) -> float:
        return sum(clip.duration for clip in self.clips)

    @property
    def count(self) -> int:
        return len(self.clips)


@dataclass(frozen=True)
class ClipJob:
    """One encode to run: the plan for that encode plus naming/progress info."""

    plan: Optional[ClipPlan]  # None = the whole source, untrimmed
    index: int = 0
    count: int = 1
    token: str = ""  # file-name suffix such as "clip01"; empty = no suffix
    title: str = ""  # progress label such as "Clip 2 of 3"; empty = no label


def _ms(value: float) -> float:
    """Round to whole milliseconds (``inf`` passes through)."""
    if math.isinf(value):
        return value
    return round(value * 1000.0) / 1000.0


def normalize_clips(
    clips: Iterable[Clip],
    duration: float,
    min_len: float = MIN_CLIP_SECONDS,
) -> Tuple[Clip, ...]:
    """Clamp clips to the source, fix reversed ends, drop tiny ones, keep list order.

    Overlaps are *not* merged: they are legitimate in separate mode and repeats
    are legitimate in join mode. Use ``find_overlaps`` to warn about them.
    An unknown duration (``<= 0``) skips the upper clamp.
    """
    known = duration is not None and duration > 0
    kept: List[Clip] = []
    for clip in clips:
        start, end = float(clip.start), float(clip.end)
        if math.isnan(start) or math.isnan(end):
            continue
        if end < start:
            start, end = end, start
        start = max(0.0, start)
        if known:
            start = min(start, duration)
            end = duration if math.isinf(end) else min(end, duration)
        if math.isinf(end):
            # Unknown duration and an open end: cannot be validated further.
            kept.append(Clip(_ms(start), end, clip.label))
            continue
        start, end = _ms(start), _ms(end)
        if end - start + 1e-9 < min_len:
            continue
        kept.append(Clip(start, end, clip.label))
    return tuple(kept)


def find_overlaps(clips: Sequence[Clip]) -> List[Tuple[int, int]]:
    """Index pairs ``(i, j)``, ``i < j``, of clips whose time ranges overlap."""
    pairs: List[Tuple[int, int]] = []
    for i in range(len(clips)):
        for j in range(i + 1, len(clips)):
            a, b = clips[i], clips[j]
            if a.start < b.end and b.start < a.end:
                pairs.append((i, j))
    return pairs


def resolve_clip_plan(
    clips: Optional[ClipPlan],
    profile: Any,
    duration: float,
    tolerance: float = WHOLE_SOURCE_TOLERANCE,
) -> Optional[ClipPlan]:
    """Work out the plan to encode, or ``None`` for "the whole file, untouched".

    Explicit ``clips`` win. Otherwise the legacy single-range trim on the profile
    (``trim_enabled`` / ``trim_start`` / ``trim_end``) becomes a one-clip plan, so
    old saved profiles and the old command line keep working.
    """
    mode = ClipMode.SEPARATE
    raw: Sequence[Clip]
    if clips is not None and clips.clips:
        raw = clips.clips
        mode = clips.mode
    elif getattr(profile, "trim_enabled", False):
        start = parse_timecode(getattr(profile, "trim_start", None)) or 0.0
        end = parse_timecode(getattr(profile, "trim_end", None))
        raw = (Clip(start, math.inf if end is None else end),)
    else:
        return None

    normalized = normalize_clips(raw, duration)
    if not normalized:
        return None
    if len(normalized) == 1 and duration and duration > 0:
        only = normalized[0]
        if only.start <= tolerance and only.end >= duration - tolerance:
            return None
    return ClipPlan(normalized, mode)


def effective_media_info(info: Any, plan: Optional[ClipPlan]) -> Any:
    """Copy of ``info`` as if the source were only the planned clips.

    Duration, frame count and size shrink to match, so progress, ETA and
    target-size planning are right for trimmed jobs. Bitrates and resolution are
    rates/shape, not totals, so they are left alone. Works on any dataclass with
    ``duration`` (``VideoInfo``).
    """
    if plan is None or not plan.clips:
        return info
    source_duration = float(getattr(info, "duration", 0.0) or 0.0)
    total = plan.total_duration
    if math.isinf(total):
        return info
    if source_duration > 0:
        total = min(total, source_duration)
    changes: dict = {"duration": total}
    fps = float(getattr(info, "avg_fps", 0.0) or getattr(info, "fps", 0.0) or 0.0)
    if hasattr(info, "frame_count") and fps > 0:
        changes["frame_count"] = int(round(total * fps))
    size = getattr(info, "size", 0) or 0
    if size and source_duration > 0:
        changes["size"] = int(round(size * total / source_duration))
    return replace(info, **changes)


def _clip_token(clip: Clip, index: int, used: set) -> str:
    base = ""
    if clip.label.strip():
        base = sanitize_filename(clip.label.strip())[:40].strip("._ -")
    if not base:
        base = f"clip{index + 1:02d}"
    token, bump = base, 2
    while token.lower() in used:
        token = f"{base}-{bump}"
        bump += 1
    used.add(token.lower())
    return token


def expand_clip_jobs(plan: Optional[ClipPlan]) -> List[ClipJob]:
    """Turn a plan into the encodes to run.

    * ``None`` -> one job for the whole file (no suffix).
    * separate mode, 2+ clips -> one job per clip (``clip01``... or the clip's label).
    * join mode, or a single clip -> one job with the whole plan.
    """
    if plan is None or not plan.clips:
        return [ClipJob(None)]
    if plan.mode is ClipMode.JOIN or len(plan.clips) == 1:
        return [ClipJob(plan)]
    used: set = set()
    count = len(plan.clips)
    return [
        ClipJob(
            ClipPlan((clip,), ClipMode.SEPARATE),
            index=i,
            count=count,
            token=_clip_token(clip, i, used),
            title=f"Clip {i + 1} of {count}",
        )
        for i, clip in enumerate(plan.clips)
    ]


def build_concat_filtergraph(
    n: int,
    *,
    with_video: bool = True,
    with_audio: bool = True,
    video_filters: Sequence[str] = (),
    audio_filters: Sequence[str] = (),
) -> Tuple[str, Optional[str], Optional[str]]:
    """``filter_complex`` that joins ``n`` inputs (one seeked input per clip).

    Returns ``(graph, video_label, audio_label)``; the labels are what to pass to
    ``-map``. ``video_filters`` / ``audio_filters`` (e.g. the scale filter) are
    folded in after the join because ``-vf`` cannot be combined with
    ``-filter_complex``. A label is ``None`` when that stream is not produced.
    """
    if n < 1:
        raise ValueError("need at least one input to join")
    if not (with_video or with_audio):
        raise ValueError("need video or audio to join")
    pads = "".join(
        (f"[{i}:v:0]" if with_video else "") + (f"[{i}:a:0]" if with_audio else "")
        for i in range(n)
    )
    outputs = ("[vcat]" if with_video else "") + ("[acat]" if with_audio else "")
    parts = [f"{pads}concat=n={n}:v={int(with_video)}:a={int(with_audio)}{outputs}"]
    vlabel = "vcat" if with_video else None
    alabel = "acat" if with_audio else None
    vf = [f for f in video_filters if f]
    af = [f for f in audio_filters if f]
    if with_video and vf:
        parts.append(f"[vcat]{','.join(vf)}[vout]")
        vlabel = "vout"
    if with_audio and af:
        parts.append(f"[acat]{','.join(af)}[aout]")
        alabel = "aout"
    return ";".join(parts), vlabel, alabel


_VIDEO_FILTER_FLAGS = {"-vf", "-filter:v"}
_AUDIO_FILTER_FLAGS = {"-af", "-filter:a"}


def split_filter_args(extra_args: Sequence[str]) -> Tuple[List[str], List[str], List[str]]:
    """Pull ``-vf``/``-af`` chains out of user "extra ffmpeg args".

    Returns ``(video_chains, audio_chains, remaining_args)``. Needed for join mode,
    where the chains must move into the ``filter_complex`` graph.
    """
    video: List[str] = []
    audio: List[str] = []
    rest: List[str] = []
    args = list(extra_args)
    i = 0
    while i < len(args):
        flag = args[i]
        if flag in _VIDEO_FILTER_FLAGS and i + 1 < len(args):
            video.append(args[i + 1].strip("\"'"))
            i += 2
        elif flag in _AUDIO_FILTER_FLAGS and i + 1 < len(args):
            audio.append(args[i + 1].strip("\"'"))
            i += 2
        else:
            rest.append(flag)
            i += 1
    return video, audio, rest


def parse_clip_range(text: str) -> Clip:
    """Parse ``"START-END"`` (seconds or ``[h:]mm:ss[.mmm]``) into a ``Clip``.

    Open ends are allowed: ``"-30"`` is 0 to 30 s, ``"1:00-"`` is 1:00 to the end.
    Raises ``ValueError`` for anything else.
    """
    raw = (text or "").strip()
    start_text, sep, end_text = raw.partition("-")
    if not sep:
        raise ValueError(f"'{text}' is not a clip range; use START-END, e.g. 1:30-2:45")
    start_text, end_text = start_text.strip(), end_text.strip()
    start = 0.0
    if start_text:
        parsed = parse_timecode(start_text)
        if parsed is None:
            raise ValueError(f"'{start_text}' is not a valid start time")
        start = parsed
    end = math.inf
    if end_text:
        parsed = parse_timecode(end_text)
        if parsed is None:
            raise ValueError(f"'{end_text}' is not a valid end time")
        end = parsed
    if end <= start:
        raise ValueError(f"clip '{text}' ends before it starts")
    return Clip(_ms(start), end)
