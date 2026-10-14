"""
Video preview service for the trim editor (no Qt, no Pillow).

Everything here goes through the bundled ffmpeg and hands back raw ``rgb24``
bytes (``width * height * 3``), so the GUI layer decides how to turn them
into pixmaps. Provided:

* single frame grabs while scrubbing (:func:`grab_frame`, :class:`FrameScheduler`),
* a filmstrip of thumbnails and row poster thumbnails (background workers),
* a byte-budgeted LRU cache of grabbed frames (:class:`ThumbnailCache`),
* real-time playback from one ffmpeg process (:class:`PlaybackSession`),
  optionally with sound (:class:`AudioSession`, raw PCM from a second ffmpeg
  into :mod:`~video_compressor.core.audio_out`) whose device clock paces the
  picture,
* a sound wave: the loudness of the whole track, measured in the background
  (:class:`PeakReducer`),

all behind the :class:`PreviewService` facade.

Threading contract
------------------
This module never touches Qt or tkinter. Every callback is invoked on a worker
thread owned by this module, never on the caller's thread:

* ``preview-interactive``: frame grabs from :meth:`PreviewService.request_frame`
  (latest request wins).
* ``preview-posters``: :meth:`PreviewService.request_poster` (FIFO).
* ``preview-filmstrip``: :meth:`PreviewService.request_filmstrip` (sequential).
* ``preview-playback``: one reader thread per :class:`PlaybackSession`.
* ``preview-audio``: one thread per :class:`AudioSession` (sound status).
* ``preview-waveform``: :meth:`PreviewService.request_waveform` (latest wins).

Callbacks must be quick and must not raise; a raising callback is logged and
swallowed (for playback it ends the session with ``'error'``). Marshal into the
GUI thread with a queued signal. Worker threads are daemons and are started
lazily, so constructing a service costs nothing.

Subprocess rules: argv lists only (paths with spaces/unicode work, and a path
is never embedded in a filtergraph), ``-nostdin``, ``stdin=DEVNULL`` and no
console window on Windows.
"""

from __future__ import annotations

import array
import collections
import logging
import math
import os
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional, Tuple, Union

from .audio_out import (
    CHANNELS,
    SAMPLE_BYTES,
    SAMPLE_RATE,
    AudioError,
    AudioOutput,
    SyncClock,
    clamp_volume,
    default_audio_output,
)

logger = logging.getLogger(__name__)

PathLike = Union[str, "os.PathLike[str]"]

__all__ = [
    "DEFAULT_CACHE_BYTES",
    "AudioSession",
    "FrameScheduler",
    "PeakReducer",
    "PlaybackSession",
    "PreviewService",
    "ThumbnailCache",
    "build_audio_cmd",
    "build_frame_cmd",
    "build_playback_cmd",
    "build_waveform_cmd",
    "display_size",
    "filmstrip_count",
    "grab_frame",
    "wave_level",
    "waveform_buckets",
]

DEFAULT_CACHE_BYTES = 64 * 1024 * 1024

#: Filmstrip thumbnail count bounds (see :func:`filmstrip_count`).
FILMSTRIP_MIN = 6
FILMSTRIP_MAX = 30

#: End-of-file retry: decode at most this many seconds / frames before the end.
_TAIL_MAX_SECONDS = 1.0
_TAIL_MAX_FRAMES = 30
_TAIL_FALLBACK_FPS = 30.0

#: Playback waits end with a precise sleep of at most this many seconds.
_COARSE_WAIT_TAIL = 0.05
#: While paced by the sound, re-read its clock at least this often (seconds).
_MASTER_POLL = 0.1
#: A catch-up restart never aims further ahead of the sound than this (seconds).
_MAX_CATCH_UP_LEAD = 5.0


def _popen_flags() -> int:
    """Return the Windows no-console flag, or 0 on other platforms."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _parse_sar(sar: Any) -> float:
    """Parse an ``"N:D"`` (or ``"N/D"``) sample aspect ratio; bad input is 1.0."""
    if not isinstance(sar, str):
        return 1.0
    text = sar.strip()
    sep = ":" if ":" in text else "/"
    try:
        num_text, den_text = text.split(sep)
        num, den = float(num_text), float(den_text)
    except ValueError:
        return 1.0
    if not (math.isfinite(num) and math.isfinite(den)) or num <= 0 or den <= 0:
        return 1.0
    return num / den


def _even_floor(value: float) -> int:
    """Largest even integer <= value (not clamped; tolerant of float noise)."""
    return int(math.floor(value / 2.0 + 1e-9)) * 2


def display_size(
    width: int,
    height: int,
    rotation: int = 0,
    sar: str = "1:1",
    max_w: int = 960,
    max_h: int = 540,
) -> Tuple[int, int]:
    """Return the size at which a source frame should be previewed.

    The displayed picture is the stored frame with non-square pixels applied
    (``sar`` is ``"N:D"`` as reported by ffprobe) and, for a 90/270 degree
    ``rotation`` (the display-matrix angle), width and height swapped. That
    picture is aspect-fitted inside ``max_w`` x ``max_h`` and never enlarged
    beyond its own display size. Both returned dimensions are even and at
    least 2 (so a 1x1 source yields ``(2, 2)``). Unknown dimensions (<= 0)
    fall back to the even-floored maximum.
    """
    cap_w = max(2, _even_floor(max_w))
    cap_h = max(2, _even_floor(max_h))
    try:
        src_w, src_h = float(width), float(height)
    except (TypeError, ValueError):
        return cap_w, cap_h
    if not (src_w > 0 and src_h > 0):
        return cap_w, cap_h

    disp_w = src_w * _parse_sar(sar)
    disp_h = src_h
    try:
        turns = int(round(float(rotation))) % 360
    except (TypeError, ValueError):
        turns = 0
    if turns in (90, 270):
        disp_w, disp_h = disp_h, disp_w

    scale = min(cap_w / disp_w, cap_h / disp_h, 1.0)
    out_w = min(cap_w, max(2, _even_floor(disp_w * scale)))
    out_h = min(cap_h, max(2, _even_floor(disp_h * scale)))
    return out_w, out_h


def filmstrip_count(width_px: int, thumb_w: int) -> int:
    """Number of filmstrip thumbnails for a strip ``width_px`` wide.

    ``clamp(width_px // thumb_w, 6, 30)``; a non-positive ``thumb_w`` yields
    the minimum.
    """
    if thumb_w <= 0:
        return FILMSTRIP_MIN
    return max(FILMSTRIP_MIN, min(FILMSTRIP_MAX, int(width_px) // int(thumb_w)))


def _format_seek(t: float) -> str:
    """Format a seek time as seconds with millisecond precision, rounded DOWN.

    Rounding down (never up) matters for frame stepping: a seek that lands a
    hair after a frame's timestamp skips that frame (or, at the very end of the
    file, yields no frame at all).
    """
    try:
        value = float(t)
    except (TypeError, ValueError):
        value = 0.0
    if not math.isfinite(value) or value < 0:
        value = 0.0
    millis = int(math.floor(value * 1000.0 + 1e-6))
    return f"{millis // 1000}.{millis % 1000:03d}"


def _format_fps(fps: float) -> str:
    """Format a frame rate for the ``fps`` filter."""
    return f"{float(fps):.6g}"


def build_frame_cmd(
    ffmpeg: str,
    path: PathLike,
    t: float,
    w: int,
    h: int,
    *,
    fast: bool = False,
    tail: bool = False,
) -> list:
    """Build the argv that writes one ``w`` x ``h`` ``rgb24`` frame to stdout.

    ``path`` is a plain argv element (never part of the filtergraph), so any
    characters are safe. The seek time is floored to milliseconds.

    ``fast=False`` is an accurate seek: ffmpeg decodes from the previous
    keyframe up to ``t``, so the frame is the one displayed at ``t``.
    ``fast=True`` adds ``-noaccurate_seek``: ffmpeg seeks to the keyframe at or
    before ``t`` and emits the first frame it decodes. That is much cheaper on
    long-GOP media and is meant for dragging; follow it with an accurate grab
    once the user stops.

    ``tail=True`` instead returns the *last* frame of the stream (from ``t``
    on): the remaining, already downscaled frames are buffered and reversed,
    so use it only with ``t`` a short distance (about a second) before the end.
    It exists for the end-of-file fallback in :func:`grab_frame`.

    ``-skip_frame nokey`` is deliberately not used: with the bundled ffmpeg it
    returns no frame at all when seeking into the last GOP of a file, and on
    open-GOP media it can return a frame from a much later keyframe.
    """
    cmd = [str(ffmpeg), "-nostdin", "-loglevel", "error"]
    if fast:
        cmd.append("-noaccurate_seek")
    vf = f"scale={int(w)}:{int(h)}:flags=bilinear,setsar=1,format=rgb24"
    if tail:
        vf += ",reverse"
    cmd += [
        "-ss", _format_seek(t),
        "-i", os.fspath(path),
        "-map", "0:v:0",
        "-frames:v", "1",
        "-an", "-sn", "-dn",
        "-vf", vf,
        "-f", "rawvideo",
        "-",
    ]
    return cmd


def build_playback_cmd(
    ffmpeg: str,
    path: PathLike,
    start: float,
    w: int,
    h: int,
    fps_out: float,
    duration: Optional[float] = None,
) -> list:
    """Build the argv that streams silent ``rgb24`` frames of ``w`` x ``h``.

    The ``fps`` filter makes the stream constant-frame-rate, so frame ``k``
    belongs at ``start + k / fps_out`` seconds. ``duration`` (seconds to play,
    not the media length) adds ``-t``; ``None`` plays to the end of the file.
    Audio, subtitles and data streams are dropped.
    """
    cmd = [str(ffmpeg), "-nostdin", "-loglevel", "error", "-ss", _format_seek(start)]
    if duration is not None:
        cmd += ["-t", _format_seek(duration)]
    cmd += [
        "-i", os.fspath(path),
        "-map", "0:v:0",
        "-an", "-sn", "-dn",
        "-vf", f"fps={_format_fps(fps_out)},scale={int(w)}:{int(h)},setsar=1,format=rgb24",
        "-f", "rawvideo",
        "-",
    ]
    return cmd


def build_audio_cmd(
    ffmpeg: str,
    path: PathLike,
    start: float,
    *,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
) -> list:
    """Build the argv that streams the first sound track as raw PCM from ``start``.

    Signed 16-bit little-endian, ``channels`` x ``sample_rate`` Hz, to stdout.
    The seek is accurate (ffmpeg decodes up to ``start``), so sample 0 belongs
    at ``start`` exactly like frame 0 of :func:`build_playback_cmd`.
    ``aresample=async=1:first_pts=0`` fills gaps in the sound's timestamps
    with silence (and pads a track that starts late), so the sample count
    keeps matching the timeline over long files. A file without sound makes
    ffmpeg exit with an error and no output. Only the first sound track is
    used; if that one cannot be decoded the preview is silent (``'none'``)
    even when a later track could be.
    """
    return [
        str(ffmpeg), "-nostdin", "-loglevel", "error",
        "-ss", _format_seek(start),
        "-i", os.fspath(path),
        "-map", "0:a:0",
        "-vn", "-sn", "-dn",
        "-af", "aresample=async=1:first_pts=0",
        "-f", "s16le",
        "-ac", str(int(channels)),
        "-ar", str(int(sample_rate)),
        "-",
    ]


# ---------------------------------------------------------------------------
# Sound wave (loudness overview)
# ---------------------------------------------------------------------------

#: Sample rate the sound wave is measured at (mono). Longer videos use less.
WAVE_RATE = 8000
WAVE_RATE_LONG = 2000
#: Videos longer than this (seconds) are measured at ``WAVE_RATE_LONG``.
WAVE_LONG_SECONDS = 3600.0
#: Wave detail: buckets per second of video, and the most buckets kept.
WAVE_BUCKETS_PER_SECOND = 50
WAVE_MIN_BUCKETS = 100
WAVE_MAX_BUCKETS = 20000
#: Give up (keeping what was measured) after this much wall time.
WAVE_MAX_SECONDS = 180.0


def waveform_buckets(duration: float) -> int:
    """How many loudness buckets to measure for a video ``duration`` seconds long."""
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(seconds) or seconds <= 0:
        return 0
    wanted = int(math.ceil(seconds * WAVE_BUCKETS_PER_SECOND))
    return max(WAVE_MIN_BUCKETS, min(WAVE_MAX_BUCKETS, wanted))


def waveform_rate(duration: float) -> int:
    """Sample rate to measure the sound wave at (lower for very long videos)."""
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        seconds = 0.0
    return WAVE_RATE_LONG if seconds > WAVE_LONG_SECONDS else WAVE_RATE


def build_waveform_cmd(ffmpeg: str, path: PathLike, sample_rate: int = WAVE_RATE) -> list:
    """Build the argv that decodes the first sound track as low-rate mono PCM.

    One decoder thread (``-threads 1``) keeps it light; the caller also runs
    it at a lower priority. As in :func:`build_audio_cmd`, gaps and a late
    start are filled with silence so sample ``n`` sits at ``n / sample_rate``
    seconds on the timeline.
    """
    return [
        str(ffmpeg), "-nostdin", "-loglevel", "error",
        "-threads", "1",
        "-i", os.fspath(path),
        "-map", "0:a:0",
        "-vn", "-sn", "-dn",
        "-af", "aresample=async=1:first_pts=0",
        "-ac", "1",
        "-ar", str(int(sample_rate)),
        "-f", "s16le",
        "-",
    ]


def wave_level(peak: float) -> float:
    """Bar height (0..1) for a peak (0..1): a square root, so quiet speech still shows."""
    try:
        value = float(peak)
    except (TypeError, ValueError):
        return 0.0
    if not value > 0:
        return 0.0
    return min(1.0, math.sqrt(value))


class PeakReducer:
    """Folds a stream of mono ``s16le`` PCM into ``buckets`` peak values (0..1).

    Bucket ``i`` covers samples ``[i * total // buckets, (i + 1) * total // buckets)``
    of an expected ``total_samples``. :meth:`feed` takes any chunk size (odd
    byte counts included) and returns the buckets it completed as
    ``(index, peak)``; :meth:`finish` returns the last, partly filled one.
    Samples beyond the last bucket are ignored. Memory stays at one chunk.
    """

    def __init__(self, total_samples: int, buckets: int) -> None:
        self.buckets = max(1, int(buckets))
        self.total = max(1, int(total_samples))
        self.index = 0
        self._pos = 0
        self._peak = 0
        self._carry = b""

    def _end(self, index: int) -> int:
        return (index + 1) * self.total // self.buckets

    def feed(self, data: bytes) -> list:
        data = self._carry + bytes(data)
        if len(data) % 2:
            data, self._carry = data[:-1], data[-1:]
        else:
            self._carry = b""
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder == "big":
            samples.byteswap()
        done: list = []
        i, n = 0, len(samples)
        while i < n and self.index < self.buckets:
            take = min(n - i, max(0, self._end(self.index) - self._pos))
            if take:
                chunk = samples[i:i + take]
                self._peak = max(self._peak, max(chunk), -min(chunk))
                i += take
                self._pos += take
            if self._pos >= self._end(self.index):
                done.append((self.index, min(1.0, self._peak / 32768.0)))
                self.index += 1
                self._peak = 0
        return done

    def finish(self) -> list:
        """The partly filled bucket at the end of the stream, if any."""
        start = self.index * self.total // self.buckets
        if self.index < self.buckets and self._pos > start:
            peak = min(1.0, self._peak / 32768.0)
            done = [(self.index, peak)]
            self.index += 1
            self._peak = 0
            return done
        return []


# ---------------------------------------------------------------------------
# One-shot frame grab
# ---------------------------------------------------------------------------


class _ProcessTracker:
    """Registry of short-lived ffmpeg processes so they can be killed together."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: set = set()
        self._closed = False
        self._kill_generation = 0

    def run_capture(self, cmd: list, timeout: float) -> Optional[bytes]:
        """Run ``cmd`` and return its stdout.

        Returns ``None`` if the process could not start, timed out, or was
        killed through :meth:`kill_all` / :meth:`close`; otherwise the (possibly
        empty) stdout bytes. stdin is DEVNULL and stderr is discarded.
        """
        with self._lock:
            if self._closed:
                return None
            generation = self._kill_generation
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    creationflags=_popen_flags(),
                )
            except OSError as exc:
                logger.debug("preview: could not start ffmpeg: %s", exc)
                return None
            self._procs.add(proc)
        try:
            try:
                out, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.debug("preview: frame grab timed out after %ss", timeout)
                self._kill(proc)
                try:
                    proc.communicate(timeout=2.0)
                except Exception:  # pragma: no cover - defensive reap
                    pass
                return None
            except (OSError, ValueError) as exc:
                logger.debug("preview: frame grab failed: %s", exc)
                self._kill(proc)
                return None
        finally:
            with self._lock:
                self._procs.discard(proc)
        with self._lock:
            if generation != self._kill_generation or self._closed:
                return None
        return out

    @staticmethod
    def _kill(proc: "subprocess.Popen") -> None:
        try:
            proc.kill()
        except OSError:
            pass

    def kill_all(self) -> None:
        """Kill every process currently running; their callers get ``None``."""
        with self._lock:
            self._kill_generation += 1
            procs = list(self._procs)
        for proc in procs:
            self._kill(proc)

    def close(self) -> None:
        """Kill everything and refuse to start new processes."""
        with self._lock:
            self._closed = True
        self.kill_all()


def _tail_window(fps: Optional[float]) -> float:
    """Seconds of video decoded by the end-of-file fallback (<= 1 s, <= 30 frames)."""
    rate = fps if (fps and fps > 0) else _TAIL_FALLBACK_FPS
    return max(0.1, min(_TAIL_MAX_SECONDS, _TAIL_MAX_FRAMES / rate))


def _tail_start(t: float, duration: Optional[float], fps: Optional[float]) -> Optional[float]:
    """Where the end-of-file fallback starts, or ``None`` if ``t`` is not near the end.

    The fallback applies when ``t`` falls in the last :func:`_tail_window`
    seconds of a file whose ``duration`` is known.
    """
    if duration is None or not math.isfinite(duration) or duration <= 0:
        return None
    start = max(0.0, duration - _tail_window(fps))
    return start if t >= start - 1e-9 else None


def grab_frame(
    ffmpeg: str,
    path: PathLike,
    t: float,
    w: int,
    h: int,
    *,
    fast: bool = False,
    duration: Optional[float] = None,
    fps: Optional[float] = None,
    timeout: float = 10.0,
    tracker: Optional[_ProcessTracker] = None,
) -> Optional[bytes]:
    """Grab the frame at ``t`` seconds as exactly ``w * h * 3`` ``rgb24`` bytes.

    Blocking. Returns ``None`` if ffmpeg could not run, timed out (``timeout``
    seconds) or produced anything but a complete frame.

    Asking for a time past the last video frame makes ffmpeg produce nothing.
    When the media ``duration`` is known and ``t`` lies in its final second,
    the grab is therefore retried once, returning the last frame of the video
    (found by decoding that final stretch, at most 1 s or 30 frames at
    preview size). Merely clamping the time to ``duration - 1/fps`` is not
    enough: the container is usually a little longer than the video stream
    (audio runs on), so that time can still be past the last video frame.
    Pass the container ``duration`` as the editor knows it; ``fps`` (optional)
    only bounds the amount decoded. ``tracker`` is internal: it lets
    :class:`PreviewService` kill grabs that are still running at shutdown.
    """
    w, h = int(w), int(h)
    if w <= 0 or h <= 0:
        return None
    expected = w * h * 3
    runner = tracker if tracker is not None else _DEFAULT_TRACKER

    data = runner.run_capture(build_frame_cmd(ffmpeg, path, t, w, h, fast=fast), timeout)
    if data is None:
        return None
    if len(data) == expected:
        return data
    if data:
        return None  # truncated frame: do not guess

    start = _tail_start(t, duration, fps)
    if start is None:
        return None
    data = runner.run_capture(build_frame_cmd(ffmpeg, path, start, w, h, tail=True), timeout)
    if data is not None and len(data) == expected:
        return data
    return None


_DEFAULT_TRACKER = _ProcessTracker()


# ---------------------------------------------------------------------------
# Thumbnail cache
# ---------------------------------------------------------------------------


class ThumbnailCache:
    """Thread-safe LRU cache of grabbed frames, bounded by total bytes.

    Keys come from :meth:`make_key` and identify the file contents cheaply as
    ``(path, mtime_ns, size, t_ms, w, h, fast)``, so editing or replacing a
    file invalidates its entries without any explicit call. A value larger than
    the whole budget is not stored.
    """

    def __init__(self, max_bytes: int = DEFAULT_CACHE_BYTES) -> None:
        self._max_bytes = max(0, int(max_bytes))
        self._entries: "collections.OrderedDict[tuple, bytes]" = collections.OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def make_key(path: PathLike, t: float, w: int, h: int, fast: bool) -> Optional[tuple]:
        """Return the cache key for a frame, or ``None`` if the file can't be stat'ed."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        # Floor to the millisecond exactly like the -ss value sent to ffmpeg
        # (_format_seek), so two times that ask ffmpeg for the same frame
        # share one entry and two that don't never do.
        try:
            t_ms = int(math.floor(float(t) * 1000.0 + 1e-6))
        except (TypeError, ValueError, OverflowError):
            t_ms = 0
        return (os.fspath(path), st.st_mtime_ns, st.st_size, t_ms, int(w), int(h), bool(fast))

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, key: Optional[tuple]) -> Optional[bytes]:
        """Return the cached bytes (marking them most recently used) or ``None``."""
        if key is None:
            return None
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def put(self, key: Optional[tuple], value: Optional[bytes]) -> None:
        """Store ``value``, evicting least recently used entries to fit the budget."""
        if key is None or value is None:
            return
        size = len(value)
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            if size > self._max_bytes:
                return
            self._entries[key] = value
            self._bytes += size
            while self._bytes > self._max_bytes and self._entries:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


# ---------------------------------------------------------------------------
# Latest-wins scheduler
# ---------------------------------------------------------------------------


class _Request:
    __slots__ = ("gen", "args", "kwargs", "callback")

    def __init__(self, gen: int, args: tuple, kwargs: dict, callback: Callable) -> None:
        self.gen = gen
        self.args = args
        self.kwargs = kwargs
        self.callback = callback


class FrameScheduler:
    """One worker thread that runs frame grabs, latest request wins.

    :meth:`request` keeps at most one *pending* request: a newer request
    replaces it, so a burst of scrub events collapses to the last one. The
    request currently running is never interrupted. Each request gets a
    generation number; :meth:`cancel` (and :meth:`shutdown`) invalidate every
    request issued so far, and the result of an invalidated request is
    discarded instead of delivered.

    By default a result is *also* delivered when a newer request arrived while
    it was running (it is merely superseded, not cancelled). Without that,
    dragging faster than a grab completes would never display anything. Pass
    ``drop_superseded=True`` for strict latest-only delivery.

    ``callback(rgb_or_none)`` runs on the worker thread. The thread starts on
    the first request.
    """

    def __init__(
        self,
        name: str,
        grab: Optional[Callable[..., Optional[bytes]]] = None,
        *,
        drop_superseded: bool = False,
    ) -> None:
        self._name = name
        self._grab = grab if grab is not None else grab_frame
        self._drop_superseded = drop_superseded
        self._cv = threading.Condition()
        self._pending: Optional[_Request] = None
        self._thread: Optional[threading.Thread] = None
        self._shutdown = False
        self._generation = 0
        self._cancelled_upto = 0

    @property
    def generation(self) -> int:
        """Generation number of the most recent request (or cancel)."""
        with self._cv:
            return self._generation

    def request(
        self,
        ffmpeg: str,
        path: PathLike,
        t: float,
        w: int,
        h: int,
        callback: Callable[[Optional[bytes]], None],
        *,
        fast: bool = False,
        duration: Optional[float] = None,
        fps: Optional[float] = None,
        timeout: float = 10.0,
    ) -> int:
        """Queue a grab, replacing any pending one; returns its generation.

        Returns ``-1`` (and never calls back) after :meth:`shutdown`.
        """
        kwargs = {"fast": fast, "duration": duration, "fps": fps, "timeout": timeout}
        with self._cv:
            if self._shutdown:
                return -1
            self._generation += 1
            gen = self._generation
            self._pending = _Request(gen, (ffmpeg, path, t, w, h), kwargs, callback)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._cv.notify_all()
            return gen

    def cancel(self) -> None:
        """Drop the pending request and discard the result of a running one."""
        with self._cv:
            self._generation += 1
            self._cancelled_upto = self._generation
            self._pending = None

    def signal_shutdown(self) -> None:
        """Stop accepting work and wake the worker (does not wait)."""
        with self._cv:
            self._shutdown = True
            self._generation += 1
            self._cancelled_upto = self._generation
            self._pending = None
            self._cv.notify_all()

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for the worker to exit; True if it has (or never started)."""
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def shutdown(self, timeout: float = 1.0) -> bool:
        """Cancel everything and join the worker; idempotent. True if it exited."""
        self.signal_shutdown()
        return self.join(timeout)

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._pending is None and not self._shutdown:
                    self._cv.wait()
                if self._shutdown:
                    return
                req = self._pending
                self._pending = None
            try:
                result = self._grab(*req.args, **req.kwargs)
            except Exception:
                logger.exception("preview: frame grab raised in %s", self._name)
                result = None
            with self._cv:
                deliver = (
                    not self._shutdown
                    and req.gen > self._cancelled_upto
                    and not (self._drop_superseded and req.gen != self._generation)
                )
            if deliver:
                try:
                    req.callback(result)
                except Exception:
                    logger.exception("preview: frame callback raised in %s", self._name)


class _TaskWorker:
    """One FIFO worker thread; :meth:`clear` drops queued tasks and mutes running ones."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._cv = threading.Condition()
        self._queue: "collections.deque" = collections.deque()
        self._thread: Optional[threading.Thread] = None
        self._shutdown = False
        self._epoch = 0

    def submit(self, task: Callable[[Callable[[], bool]], None]) -> bool:
        """Queue ``task(cancelled)``; ``cancelled()`` turns True after clear/shutdown."""
        with self._cv:
            if self._shutdown:
                return False
            epoch = self._epoch
            self._queue.append((epoch, task))
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
            self._cv.notify_all()
            return True

    def clear(self) -> None:
        with self._cv:
            self._epoch += 1
            self._queue.clear()

    def signal_shutdown(self) -> None:
        with self._cv:
            self._shutdown = True
            self._epoch += 1
            self._queue.clear()
            self._cv.notify_all()

    def join(self, timeout: Optional[float] = None) -> bool:
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _is_stale(self, epoch: int) -> bool:
        with self._cv:
            return self._shutdown or epoch != self._epoch

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._shutdown:
                    self._cv.wait()
                if self._shutdown:
                    return
                epoch, task = self._queue.popleft()
            try:
                task(lambda e=epoch: self._is_stale(e))
            except Exception:
                logger.exception("preview: background task raised in %s", self._name)


# ---------------------------------------------------------------------------
# Silent real-time playback
# ---------------------------------------------------------------------------


def _kill_process(proc: Any) -> None:
    """Kill ``proc`` if it is still running (never raises)."""
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:  # already gone / platform quirks
        pass


def _read_exactly(stream: Any, view: memoryview, size: int) -> Tuple[int, bool]:
    """Fill ``view[:size]`` from a pipe; ``(bytes_read, pipe_broke)``. Short at EOF."""
    got = 0
    while got < size:
        try:
            n = stream.readinto(view[got:size])
        except (OSError, ValueError):
            return got, True
        if not n:
            break
        got += n
    return got, False


class PlaybackSession:
    """Silent real-time playback of one file through a single ffmpeg process.

    ``start()`` launches ffmpeg (``build_playback_cmd``) on a reader thread
    that reads fixed ``w * h * 3`` byte frames from the pipe and paces them
    against a monotonic clock. ``on_frame(t, rgb)`` receives the playhead time
    ``start + k / fps_out`` (``k`` counts every frame ffmpeg produced, dropped
    ones included) and the frame bytes. ``on_end(reason)`` is called exactly
    once for a started session, always after the last ``on_frame``, with:

    * ``'eof'``: the stream ended normally (ffmpeg exited with status 0),
    * ``'stopped'``: :meth:`stop` was called,
    * ``'error'``: ffmpeg could not start, died, the pipe broke or a callback
      raised.

    Pacing: the first frame is delivered immediately and anchors the clock;
    frame ``k`` is due at ``t0 + k / fps_out``. If delivery falls at least one
    frame behind because the consumer was slow (frames are already waiting in
    the pipe), the late frames are *dropped*, never queued, until delivery
    catches up. If instead ffmpeg itself cannot produce frames fast enough,
    nothing is dropped: the clock is re-anchored and playback simply runs
    slower than real time, with exact timestamps. ``on_frame`` runs on the
    reader thread, so it must return quickly (hand the bytes to the GUI thread
    and keep only the newest).

    ``duration`` is how many seconds to play (``-t``), not the media length.
    ``clock``, ``sleep`` and ``popen`` are test seams (inject ``clock`` and
    ``sleep`` together): ``sleep(seconds)`` replaces the interruptible wait and
    ``popen(cmd, **kwargs)`` must return a
    ``subprocess.Popen``-like object (``stdout.readinto``, ``poll``, ``wait``,
    ``kill``).

    With sound, pass ``master_clock``: seconds of media played since playback
    began (an :class:`~video_compressor.core.audio_out.SyncClock` following
    the sound device). Frame ``k`` is then due when that clock reads
    ``k / fps_out``, so the picture follows the sound and cannot drift. The
    clock is never re-anchored: late frames are dropped while newer ones are
    already waiting, and if ffmpeg itself falls ``max_lag`` seconds behind,
    the session restarts it ahead of the sound (``catch_ups`` counts those)
    rather than lag further. How far ahead adapts to how long ffmpeg was
    seen to take to start (plus ``catch_up_margin``, at least
    ``catch_up_lead``). A catch-up that shows less than ``min_hold`` seconds
    before the next one is needed did not help; after
    ``max_failed_catch_ups`` of those in a row the session stops chasing the
    sound, calls ``on_sync_lost()`` once and plays on in smooth slow motion.
    Whenever the clock is not following a live sound (its ``following_audio``
    is false: no device, no sound from here, the track ended) frames are paced
    on real time exactly like silent playback. ``on_first_frame()`` runs right
    after the first frame was delivered (it starts the sound, so both begin
    together).
    """

    #: Counters, valid while and after the session runs (diagnostics/tests).
    frames_read: int
    frames_delivered: int
    frames_dropped: int
    catch_ups: int

    def __init__(
        self,
        ffmpeg: str,
        path: PathLike,
        start: float,
        w: int,
        h: int,
        fps_out: float,
        on_frame: Callable[[float, bytes], None],
        on_end: Callable[[str], None],
        duration: Optional[float] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        popen: Optional[Callable[..., Any]] = None,
        master_clock: Optional[Callable[[], float]] = None,
        on_first_frame: Optional[Callable[[], None]] = None,
        max_lag: float = 0.6,
        catch_up_lead: float = 0.3,
        catch_up_margin: float = 0.3,
        min_hold: float = 1.0,
        max_failed_catch_ups: int = 3,
        on_sync_lost: Optional[Callable[[], None]] = None,
    ) -> None:
        if int(w) <= 0 or int(h) <= 0:
            raise ValueError("w and h must be positive")
        if not (fps_out and fps_out > 0):
            raise ValueError("fps_out must be positive")
        self._master = master_clock
        self._on_first_frame = on_first_frame
        self._max_lag = max(float(max_lag), 2.0 / float(fps_out))
        self._catch_up_lead = max(0.0, float(catch_up_lead))
        self._catch_up_margin = max(0.0, float(catch_up_margin))
        self._min_hold = max(0.0, float(min_hold))
        self._max_failed_catch_ups = max(1, int(max_failed_catch_ups))
        self._on_sync_lost = on_sync_lost
        self._sync_lost = False
        self._ffmpeg = ffmpeg
        self._path = path
        self._start = float(start)
        self._w = int(w)
        self._h = int(h)
        self._fps = float(fps_out)
        self._dt = 1.0 / self._fps
        self._frame_bytes = self._w * self._h * 3
        self._duration = duration
        self._on_frame = on_frame
        self._on_end = on_end
        self._clock = clock if clock is not None else time.monotonic
        # Timing ffmpeg's reads against the sound needs better than the 15.6 ms
        # ticks time.monotonic has on Windows.
        self._read_clock = clock if clock is not None else time.perf_counter
        self._sleep = sleep
        self._popen = popen if popen is not None else subprocess.Popen

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._proc: Any = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._ended = False
        self.frames_read = 0
        self.frames_delivered = 0
        self.frames_dropped = 0
        self.catch_ups = 0

    # -- public ---------------------------------------------------------

    @property
    def process(self) -> Any:
        """The ffmpeg process (``None`` until it has been launched)."""
        return self._proc

    @property
    def running(self) -> bool:
        """True while the reader thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Begin playback; a no-op if already started or already stopped."""
        with self._lock:
            if self._started or self._stop_event.is_set():
                return
            self._started = True
            self._thread = threading.Thread(target=self._run, name="preview-playback", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop playback: kill ffmpeg, close the pipe, join the reader (<= ~1 s).

        Idempotent and safe from any thread, including from inside a callback
        (it then does not join itself). After it returns ``on_frame`` is not
        called again unless a callback is stuck for more than a second;
        ``on_end('stopped')`` follows from the reader thread (once) if the
        session had been started.
        """
        with self._lock:
            self._stop_event.set()
            proc = self._proc
            thread = self._thread
        if proc is not None:
            self._kill(proc)
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)

    # -- reader thread --------------------------------------------------

    @staticmethod
    def _kill(proc: Any) -> None:
        _kill_process(proc)

    def _wait_until(self, deadline: float) -> bool:
        """Wait until the clock reaches ``deadline``; True if stopped meanwhile.

        ``threading.Event.wait`` only wakes on the ~15.6 ms Windows timer tick
        (a 16 ms wait takes 30 ms), which ruins 30 fps pacing, while
        ``time.sleep`` is precise there. So the bulk of a long wait is an
        interruptible ``Event.wait`` and the last stretch a plain sleep.
        """
        if self._sleep is not None:
            self._sleep(max(0.0, deadline - self._clock()))
            return self._stop_event.is_set()
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return self._stop_event.is_set()
            if remaining > _COARSE_WAIT_TAIL:
                if self._stop_event.wait(remaining - _COARSE_WAIT_TAIL):
                    return True
            else:
                time.sleep(remaining)
                return self._stop_event.is_set()

    def _run(self) -> None:
        reason = "error"
        try:
            reason = self._pump() if self._master is None else self._pump_synced()
        except Exception:
            logger.exception("preview: playback reader crashed")
            reason = "error"
        finally:
            self._teardown()
            if self._stop_event.is_set():
                reason = "stopped"
            with self._lock:
                already = self._ended
                self._ended = True
            if not already:
                try:
                    self._on_end(reason)
                except Exception:
                    logger.exception("preview: on_end callback raised")

    def _teardown(self) -> None:
        proc = self._proc
        if proc is None:
            return
        self._kill(proc)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass

    def _launch(self, start: float, duration: Optional[float]) -> Any:
        """Start ffmpeg streaming frames from ``start``; ``None`` if it could not start."""
        cmd = build_playback_cmd(self._ffmpeg, self._path, start, self._w, self._h, self._fps, duration)
        try:
            proc = self._popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=_popen_flags(),
            )
        except OSError as exc:
            logger.warning("preview: could not start ffmpeg for playback: %s", exc)
            return None
        with self._lock:
            self._proc = proc
        return proc

    @staticmethod
    def _read_frame(stream: Any, view: memoryview, size: int) -> Tuple[int, bool]:
        return _read_exactly(stream, view, size)

    def _pump(self) -> str:
        proc = self._launch(self._start, self._duration)
        if proc is None:
            return "error"
        if self._stop_event.is_set():
            return "stopped"

        size = self._frame_bytes
        buf = bytearray(size)
        view = memoryview(buf)
        stream = proc.stdout
        clock = self._clock
        dt = self._dt
        t0: Optional[float] = None
        k = 0

        while not self._stop_event.is_set():
            read_started = clock()
            got, broken = self._read_frame(stream, view, size)
            if got < size:
                return self._exit_reason(proc, broken)
            now = clock()
            self.frames_read += 1

            if t0 is None:
                t0 = now  # the first frame anchors the clock
            due = t0 + k * dt
            if due > now:
                if self._wait_until(due):
                    return "stopped"
            elif now - due >= dt - 1e-9:
                if now - read_started >= dt:
                    # ffmpeg itself is the bottleneck: dropping cannot help.
                    t0 = now - k * dt
                else:
                    # Consumer is behind and a newer frame is already due.
                    self.frames_dropped += 1
                    k += 1
                    continue

            if self._stop_event.is_set():
                break
            try:
                self._on_frame(self._start + k / self._fps, bytes(buf))
            except Exception:
                logger.exception("preview: on_frame callback raised; ending playback")
                return "error"
            self.frames_delivered += 1
            k += 1
        return "stopped"

    # -- paced by the sound ---------------------------------------------

    def _wait_master(self, deadline: float) -> bool:
        """Wait until the master clock reaches ``deadline``; True if stopped meanwhile.

        The master clock can stand still (the sound is starting or caught a
        hiccup), so it is re-read at least every ``_MASTER_POLL`` seconds.
        """
        master = self._master
        while self._sleep is not None and not self._stop_event.is_set():
            remaining = deadline - master()
            if remaining <= 1e-9:  # float noise must not keep us waiting
                return False
            self._sleep(remaining)
        while not self._stop_event.is_set():
            remaining = deadline - master()
            if remaining <= 1e-9:
                return False
            if remaining > _COARSE_WAIT_TAIL:
                if self._stop_event.wait(min(remaining - _COARSE_WAIT_TAIL, _MASTER_POLL)):
                    return True
            else:
                time.sleep(remaining)
        return True

    def _retire(self, proc: Any) -> None:
        """Kill a replaced ffmpeg and close its pipe."""
        self._kill(proc)
        try:
            proc.wait(timeout=1.0)
        except Exception:
            pass
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass

    def _following_sound(self) -> bool:
        """True while frames are paced by a live sound clock (not given up on)."""
        if self._sync_lost:
            return False
        return bool(getattr(self._master, "following_audio", True))

    def _lose_sync(self) -> None:
        """Catching up keeps failing: pace on real time (slow motion) from here."""
        self._sync_lost = True
        if self._on_sync_lost is not None:
            try:
                self._on_sync_lost()
            except Exception:
                logger.exception("preview: on_sync_lost callback raised")

    def _pump_synced(self) -> str:
        master = self._master
        rclock = self._read_clock
        launched_at = rclock()
        proc = self._launch(self._start, self._duration)
        if proc is None:
            return "error"
        if self._stop_event.is_set():
            return "stopped"

        size = self._frame_bytes
        buf = bytearray(size)
        view = memoryview(buf)
        dt = self._dt
        offset = 0.0  # media seconds from self._start to this ffmpeg's first frame
        k = 0
        fresh = True  # the first frame after a (re)start is always shown
        announced = False
        startup = 0.0  # longest wait seen from starting ffmpeg to its first frame
        free_t0: Optional[float] = None  # real-time anchor once there is no sound to follow
        held: Optional[int] = None  # frames shown since the last catch-up
        failed = 0  # catch-ups in a row that did not hold

        while not self._stop_event.is_set():
            read_started = rclock()
            got, broken = self._read_frame(proc.stdout, view, size)
            if got < size:
                return self._exit_reason(proc, broken)
            read_done = rclock()
            if fresh:
                startup = max(startup, read_done - launched_at)
            slow_read = read_done - read_started >= dt
            self.frames_read += 1

            if not self._following_sound():
                # No live sound clock (no device, no sound from here, the track
                # ended, or catching up was hopeless): plain real-time pacing as
                # in silent playback, slowing down when ffmpeg is the bottleneck.
                now = self._clock()
                if free_t0 is None:
                    free_t0 = now - k * dt  # carry on from this frame
                due = free_t0 + k * dt
                if due > now:
                    if self._wait_until(due):
                        return "stopped"
                elif now - due >= dt - 1e-9 and not fresh:
                    if slow_read:
                        free_t0 = now - k * dt
                    else:
                        self.frames_dropped += 1
                        k += 1
                        continue
            else:
                due = offset + k * dt
                now = master()
                if due > now:
                    if self._wait_master(due):
                        return "stopped"
                elif not fresh:
                    late = now - due
                    if late >= self._max_lag and slow_read:
                        if held is not None:
                            failed = failed + 1 if held * dt < self._min_hold else 0
                        if failed >= self._max_failed_catch_ups:
                            self._lose_sync()  # show this frame now, then slow motion
                        else:
                            # ffmpeg cannot keep up with the sound: restart it far
                            # enough ahead (its own measured start-up time plus a
                            # margin) that its first frame is not already late.
                            lead = min(
                                _MAX_CATCH_UP_LEAD,
                                max(self._catch_up_lead, startup + self._catch_up_margin),
                            )
                            jump = now + lead
                            left: Optional[float] = None
                            if self._duration is not None:
                                left = self._duration - jump
                                if left <= 0:
                                    return "eof"
                            self._retire(proc)
                            if self._stop_event.is_set():
                                return "stopped"
                            launched_at = rclock()
                            proc = self._launch(self._start + jump, left)
                            if proc is None:
                                return "error"
                            if self._stop_event.is_set():
                                return "stopped"
                            offset, k, fresh, held = jump, 0, True, 0
                            self.catch_ups += 1
                            continue
                    elif late >= dt - 1e-9 and not slow_read:
                        # A newer frame is already due: drop this one.
                        self.frames_dropped += 1
                        k += 1
                        continue

            if self._stop_event.is_set():
                break
            try:
                self._on_frame(self._start + offset + k / self._fps, bytes(buf))
            except Exception:
                logger.exception("preview: on_frame callback raised; ending playback")
                return "error"
            self.frames_delivered += 1
            if held is not None:
                held += 1
            fresh = False
            k += 1
            if not announced:
                announced = True
                if self._on_first_frame is not None:
                    try:
                        self._on_first_frame()
                    except Exception:
                        logger.exception("preview: on_first_frame callback raised")
        return "stopped"

    def _exit_reason(self, proc: Any, broken: bool) -> str:
        """Classify the end of the pipe: 'eof' for a clean exit, else 'error'."""
        if self._stop_event.is_set():
            return "stopped"
        rc = proc.poll()
        if rc is None:
            try:
                rc = proc.wait(timeout=1.0)
            except Exception:
                rc = None
        if rc is None:
            return "error" if broken else "eof"
        return "eof" if rc == 0 else "error"


# ---------------------------------------------------------------------------
# Sound
# ---------------------------------------------------------------------------


class AudioSession:
    """Plays a file's sound from ``start`` and keeps the clock the picture follows.

    :meth:`start` runs a ``preview-audio`` thread that opens the sound output
    (``output_factory()``, then ``open``), starts ffmpeg
    (:func:`build_audio_cmd`) and copies PCM from the pipe into the output.
    The output's back-pressure paces the decoder, so only a few hundred
    milliseconds are ever decoded ahead. The output is held paused until
    :meth:`release` (call it once the first video frame is on screen), so
    picture and sound start together. :attr:`clock` is a
    :class:`~video_compressor.core.audio_out.SyncClock` on the output's
    position: pass it to :class:`PlaybackSession` as ``master_clock``.

    Nothing here may stop the picture. With no sound device, no sound track or
    any error, the sound is dropped, the clock carries on in real time, and
    ``on_status(state)`` (worker thread) says why:

    * ``'playing'``: the sound is on its way,
    * ``'unavailable'``: no sound device could be opened,
    * ``'none'``: ffmpeg failed and produced no sound (no track, or it could
      not be read),
    * ``'past_end'``: the sound track ends before ``start`` (ffmpeg finished
      cleanly without a sample),
    * ``'failed'``: the device or ffmpeg failed, or the sound never started,
    * ``'behind'``: the picture could not keep up, so the sound was dropped
      (:meth:`fall_behind`),
    * ``'ended'``: the sound track finished (it may be shorter than the video).

    :meth:`stop` is idempotent, safe from any thread (also from a callback)
    and kills ffmpeg; with ``wait=False`` it does not wait for the worker,
    which finishes and tidies up on its own. ``popen`` and ``mono`` are test
    seams.
    """

    def __init__(
        self,
        ffmpeg: str,
        path: PathLike,
        start: float,
        *,
        volume: float = 1.0,
        output_factory: Optional[Callable[[], AudioOutput]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        popen: Optional[Callable[..., Any]] = None,
        mono: Optional[Callable[[], float]] = None,
        grace: float = 2.0,
    ) -> None:
        self._ffmpeg = ffmpeg
        self._path = path
        self._start = max(0.0, float(start))
        self._volume = clamp_volume(volume)
        self._factory = output_factory if output_factory is not None else default_audio_output
        self._on_status = on_status
        self._popen = popen if popen is not None else subprocess.Popen
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Any = None
        self._output: Optional[AudioOutput] = None
        self._released = False
        self._started = False
        self._states: list = []
        self.clock = SyncClock(self._position, mono=mono, grace=grace, on_give_up=self._give_up)

    # -- public ---------------------------------------------------------

    @property
    def output(self) -> Optional[AudioOutput]:
        """The open sound output (``None`` before it opens or without sound)."""
        return self._output

    @property
    def states(self) -> list:
        """Every state reported so far, in order (diagnostics/tests)."""
        return list(self._states)

    def start(self) -> None:
        """Open the sound and start decoding; a no-op if started or stopped."""
        with self._lock:
            if self._started or self._stopping.is_set():
                return
            self._started = True
            self._thread = threading.Thread(target=self._run, name="preview-audio", daemon=True)
            self._thread.start()

    def release(self) -> None:
        """The picture has started: start the sound and the clock. Idempotent."""
        with self._lock:
            if self._released:
                return
            self._released = True
            output = self._output
        self.clock.start()
        if output is not None:
            try:
                output.resume()
            except Exception:
                logger.debug("preview: could not start the sound", exc_info=True)

    def set_volume(self, volume: float) -> None:
        with self._lock:
            self._volume = clamp_volume(volume)
            output = self._output
            level = self._volume
        if output is not None:
            try:
                output.set_volume(level)
            except Exception:
                logger.debug("preview: could not set the volume", exc_info=True)

    def stop(self, wait: bool = True) -> None:
        """Silence the sound and kill ffmpeg now; with ``wait``, also join the worker (<= ~1 s).

        ``wait=False`` is for the GUI thread: the worker may be stuck for a
        moment inside the sound driver (opening a device that is waking up),
        and it closes the device and reaps ffmpeg by itself once it gets out.
        """
        self._halt()
        if wait:
            self.join(1.0)

    def join(self, timeout: Optional[float] = None) -> bool:
        """Wait for the worker to finish; True if it has (or never started)."""
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def finished(self) -> bool:
        thread = self._thread
        return thread is None or not thread.is_alive()

    def fall_behind(self) -> None:
        """The picture cannot keep step with the sound: drop the sound (``'behind'``)."""
        if self._stopping.is_set():
            return
        self._report("behind")
        self.clock.detach()
        self._halt()

    # -- internals ------------------------------------------------------

    def _position(self) -> float:
        output = self._output
        return output.position_seconds() if output is not None else 0.0

    def _halt(self) -> None:
        with self._lock:
            self._stopping.set()
            proc, output = self._proc, self._output
        if proc is not None:
            _kill_process(proc)
        if output is not None:
            try:
                output.stop()
            except Exception:
                logger.debug("preview: could not stop the sound", exc_info=True)

    def _give_up(self) -> None:
        """The sound never started (clock callback): drop it, keep the picture."""
        if self._stopping.is_set():
            return
        self._report("failed")
        self._halt()

    def _report(self, state: str) -> None:
        if self._stopping.is_set():
            return
        self._states.append(state)
        if self._on_status is not None:
            try:
                self._on_status(state)
            except Exception:
                logger.exception("preview: on_status callback raised")

    def _silent(self, state: str) -> None:
        self.clock.detach()
        self._report(state)

    def _open_output(self) -> Optional[AudioOutput]:
        try:
            output = self._factory()
        except Exception:
            logger.info("preview: no sound output", exc_info=True)
            return None
        try:
            ok = bool(output.open(SAMPLE_RATE, CHANNELS))
        except Exception:
            logger.info("preview: the sound output did not open", exc_info=True)
            ok = False
        if not ok or output.is_null:
            try:
                output.stop()
            except Exception:
                pass
            return None
        with self._lock:
            if self._stopping.is_set():
                output.stop()
                return None
            self._output = output
            if not self._released:
                output.pause()  # pre-roll: the device starts with the first frame
            else:
                output.resume()  # the picture is already up (a no-op for a running device)
            output.set_volume(self._volume)
        return output

    def _launch(self) -> Any:
        cmd = build_audio_cmd(self._ffmpeg, self._path, self._start)
        try:
            proc = self._popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=_popen_flags(),
            )
        except OSError as exc:
            logger.info("preview: could not start ffmpeg for sound: %s", exc)
            return None
        with self._lock:
            self._proc = proc
            stopping = self._stopping.is_set()
        if stopping:
            _kill_process(proc)
        return proc

    def _copy(self, proc: Any, output: AudioOutput) -> bool:
        """Pipe -> output until the track ends or we stop; True if any sound came."""
        frame = CHANNELS * SAMPLE_BYTES
        block = int(getattr(output, "buffer_bytes", 0) or (SAMPLE_RATE // 20) * frame)
        block = max(frame, block - block % frame)
        buf = bytearray(block)
        view = memoryview(buf)
        got_any = False
        while not self._stopping.is_set():
            got, broken = _read_exactly(proc.stdout, view, block)
            usable = got - got % frame
            if usable:
                got_any = True
                if output.write(view[:usable]) < usable:
                    break  # stopped while waiting for room
            if got < block or broken:
                break
        return got_any

    @staticmethod
    def _exit_code(proc: Any) -> Optional[int]:
        """ffmpeg's exit status once its output has ended (``None`` if unknown)."""
        try:
            return proc.wait(timeout=2.0)
        except Exception:
            return None

    def _run(self) -> None:
        output: Optional[AudioOutput] = None
        proc: Any = None
        try:
            output = self._open_output()
            if output is None:
                self._silent("unavailable")
                return
            proc = self._launch()
            if proc is None:
                self._silent("none")
                return
            self._report("playing")
            got_any = self._copy(proc, output)
            if self._stopping.is_set():
                return
            if not got_any:
                # A clean exit with no samples means the track is over by
                # ``start`` (a shorter sound track); anything else is a failure.
                self._silent("past_end" if self._exit_code(proc) == 0 else "none")
                return
            output.drain()  # let the device play what it holds (stop() cuts it short)
            self._silent("ended")
        except AudioError as exc:
            logger.info("preview: the sound stopped working: %s", exc)
            self._silent("failed")
        except Exception:
            logger.exception("preview: sound reader crashed")
            self._silent("failed")
        finally:
            if output is not None:
                try:
                    output.stop()
                except Exception:
                    pass
            if proc is not None:
                _kill_process(proc)
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
                try:
                    if proc.stdout is not None:
                        proc.stdout.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


class PreviewService:
    """Everything the trim editor needs from ffmpeg, behind one object.

    Three kinds of work run on separate threads so they never block each
    other: interactive frame grabs for scrubbing (latest request wins), row
    poster thumbnails (FIFO) and the filmstrip (sequential, cancellable).
    Playback is its own ffmpeg process with a dedicated reader thread; with
    sound, a second ffmpeg feeds an :class:`AudioSession` whose clock paces
    the picture. The sound wave is measured on its own low-priority thread.
    All grabs share one :class:`ThumbnailCache`. Every callback runs on a
    worker thread (see the module docstring); a cache hit is also delivered
    from the worker thread, never inline on the caller's thread.

    ``grab`` is a test seam replacing :func:`grab_frame`;
    ``audio_output_factory`` makes the sound output (default: waveOut on
    Windows, silent elsewhere) and ``popen`` starts the sound and sound-wave
    ffmpeg processes.
    """

    def __init__(
        self,
        ffmpeg_path: str,
        ffprobe_path: Optional[str] = None,
        cache_bytes: int = DEFAULT_CACHE_BYTES,
        *,
        grab: Optional[Callable[..., Optional[bytes]]] = None,
        audio_output_factory: Optional[Callable[[], AudioOutput]] = None,
        popen: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.cache = ThumbnailCache(cache_bytes)
        self._tracker = _ProcessTracker()
        if grab is not None:
            self._raw_grab = grab
        else:
            def _tracked_grab(*args: Any, **kwargs: Any) -> Optional[bytes]:
                return grab_frame(*args, tracker=self._tracker, **kwargs)

            self._raw_grab = _tracked_grab
        self._interactive = FrameScheduler("preview-interactive", grab=self._cached_grab)
        self._posters = _TaskWorker("preview-posters")
        self._filmstrips = _TaskWorker("preview-filmstrip")
        self._waves = _TaskWorker("preview-waveform")
        self._lock = threading.Lock()
        self._filmstrip_cancel: Optional[threading.Event] = None
        self._session: Optional[PlaybackSession] = None
        self._sound: Optional[AudioSession] = None
        self._retired: list = []  # stopped sound sessions whose worker may still be finishing
        self._audio_factory = audio_output_factory
        self._popen = popen if popen is not None else subprocess.Popen
        self._volume = 1.0
        self._wave_cancel: Optional[threading.Event] = None
        self._wave_proc: Any = None
        self._closed = False

    # -- shared grab ----------------------------------------------------

    def _cached_grab(
        self,
        ffmpeg: str,
        path: PathLike,
        t: float,
        w: int,
        h: int,
        *,
        fast: bool = False,
        duration: Optional[float] = None,
        fps: Optional[float] = None,
        timeout: float = 10.0,
    ) -> Optional[bytes]:
        """Cache lookup, falling back to a real grab (always on a worker thread)."""
        key = ThumbnailCache.make_key(path, t, w, h, fast)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        data = self._raw_grab(ffmpeg, path, t, w, h, fast=fast, duration=duration, fps=fps, timeout=timeout)
        if data is not None:
            self.cache.put(key, data)
        return data

    # -- interactive frames ---------------------------------------------

    def request_frame(
        self,
        path: PathLike,
        t: float,
        size: Tuple[int, int],
        *,
        fast: bool,
        callback: Callable[[float, int, int, Optional[bytes], bool], None],
        duration: Optional[float] = None,
        fps: Optional[float] = None,
    ) -> None:
        """Request one frame for scrubbing; ``callback(t, w, h, rgb_or_none, fast)``.

        Latest wins: a request replaces any still-pending one (see
        :class:`FrameScheduler` for the exact delivery rule). The callback runs
        on the ``preview-interactive`` thread, including for cache hits.
        ``rgb`` is ``None`` when the frame could not be read. Pass the media
        ``duration`` (and ``fps``) so a request at/after the end returns the
        last frame.
        """
        w, h = int(size[0]), int(size[1])
        self._interactive.request(
            self.ffmpeg_path, path, t, w, h,
            lambda rgb: callback(t, w, h, rgb, fast),
            fast=fast, duration=duration, fps=fps,
        )

    # -- posters --------------------------------------------------------

    def request_poster(
        self,
        path: PathLike,
        t: float,
        size: Tuple[int, int],
        callback: Callable[[PathLike, int, int, Optional[bytes]], None],
        *,
        duration: Optional[float] = None,
        fps: Optional[float] = None,
    ) -> None:
        """Request a row poster thumbnail; ``callback(path, w, h, rgb_or_none)``.

        Runs on a dedicated FIFO thread (every request is served, in order),
        separate from scrubbing. Uses the cheap keyframe seek (``fast=True``).
        Dropped by :meth:`cancel_all` and :meth:`shutdown`.
        """
        w, h = int(size[0]), int(size[1])

        def task(cancelled: Callable[[], bool]) -> None:
            if cancelled():
                return
            rgb = self._cached_grab(self.ffmpeg_path, path, t, w, h, fast=True, duration=duration, fps=fps)
            if not cancelled():
                callback(path, w, h, rgb)

        self._posters.submit(task)

    # -- filmstrip ------------------------------------------------------

    def request_filmstrip(
        self,
        path: PathLike,
        duration: float,
        count: int,
        size: Tuple[int, int],
        on_thumb: Callable[[int, float, int, int, Optional[bytes]], None],
        on_done: Optional[Callable[[], None]] = None,
    ) -> None:
        """Grab ``count`` evenly spaced thumbnails, one at a time, in order.

        Thumbnail ``i`` is taken at ``(i + 0.5) * duration / count`` with a
        keyframe seek and reported as ``on_thumb(i, t, w, h, rgb_or_none)``
        (``rgb`` is ``None`` for a frame that could not be read) as soon as it
        is ready. ``on_done()`` is called after the last thumbnail, but not if
        the filmstrip was cancelled. A new request cancels the previous
        filmstrip; cancellation takes effect between grabs. Runs on the
        ``preview-filmstrip`` thread.
        """
        w, h = int(size[0]), int(size[1])
        count = int(count)
        with self._lock:
            if self._closed:
                return
            if self._filmstrip_cancel is not None:
                self._filmstrip_cancel.set()
            cancel = threading.Event()
            self._filmstrip_cancel = cancel

        def task(cancelled: Callable[[], bool]) -> None:
            for i in range(max(0, count)):
                if cancel.is_set() or cancelled():
                    return
                t = (i + 0.5) * duration / count
                rgb = self._cached_grab(self.ffmpeg_path, path, t, w, h, fast=True, duration=duration)
                if cancel.is_set() or cancelled():
                    return
                on_thumb(i, t, w, h, rgb)
            if on_done is not None and not (cancel.is_set() or cancelled()):
                on_done()

        if not self._filmstrips.submit(task):
            cancel.set()

    def cancel_filmstrip(self) -> None:
        """Cancel the running/queued filmstrip (between grabs); no ``on_done``."""
        with self._lock:
            cancel = self._filmstrip_cancel
            self._filmstrip_cancel = None
        if cancel is not None:
            cancel.set()

    # -- playback -------------------------------------------------------

    def start_playback(
        self,
        path: PathLike,
        start: float,
        size: Tuple[int, int],
        fps_out: float,
        on_frame: Callable[[float, bytes], None],
        on_end: Callable[[str], None],
        duration: Optional[float] = None,
        *,
        audio: bool = False,
        volume: Optional[float] = None,
        on_audio: Optional[Callable[[str], None]] = None,
    ) -> PlaybackSession:
        """Start playback at ``start`` seconds, stopping any previous session.

        Returns the started :class:`PlaybackSession` (callbacks as documented
        there; ``duration`` is how long to play, ``None`` for to the end).
        After :meth:`shutdown` the returned session is already stopped and
        never starts.

        ``audio=True`` also plays the sound (at ``volume``, 0..1; default the
        last volume set) through an :class:`AudioSession`, and the picture is
        paced by the sound's clock. ``on_audio(state)`` reports how the sound
        is doing (see :class:`AudioSession`). Without sound, or if it fails,
        playback runs silently on real time. When the picture ends for any
        reason the sound stops too.
        """
        sound: Optional[AudioSession] = None
        ended = on_end
        paced: dict = {}
        if audio:
            if volume is not None:
                self._volume = clamp_volume(volume)
            sound = AudioSession(
                self.ffmpeg_path, path, start,
                volume=self._volume,
                output_factory=self._audio_factory,
                on_status=on_audio,
                popen=self._popen,
            )
            paced = {
                "master_clock": sound.clock,
                "on_first_frame": sound.release,
                "on_sync_lost": sound.fall_behind,
            }

            def ended_with_sound(reason: str, _sound: AudioSession = sound) -> None:
                _sound.stop(wait=False)
                on_end(reason)

            ended = ended_with_sound

        session = PlaybackSession(
            self.ffmpeg_path, path, start, int(size[0]), int(size[1]), fps_out,
            on_frame, ended, duration, **paced,
        )
        with self._lock:
            previous, previous_sound = self._session, self._sound
            closed = self._closed
            self._session = None if closed else session
            self._sound = None if closed else sound
        if previous_sound is not None:
            self._retire_sound(previous_sound)
        if previous is not None:
            previous.stop()
        if closed:
            session.stop()
            return session
        if sound is not None:
            sound.start()
        session.start()
        return session

    def _retire_sound(self, sound: AudioSession) -> None:
        """Silence a sound session without waiting for its worker (GUI thread).

        The worker may be stuck for a moment in the sound driver; it closes
        the device and reaps its ffmpeg by itself. :meth:`shutdown` waits for
        any still running.
        """
        sound.stop(wait=False)
        with self._lock:
            self._retired = [s for s in self._retired if not s.finished]
            if not sound.finished:
                self._retired.append(sound)

    def stop_playback(self) -> None:
        """Stop the current playback session and its sound, if any.

        The sound goes silent and its ffmpeg is killed at once; this never
        waits for the sound driver.
        """
        with self._lock:
            session, sound = self._session, self._sound
            self._session = None
            self._sound = None
        if sound is not None:
            self._retire_sound(sound)  # silence first: the picture's reader may take a moment
        if session is not None:
            session.stop()

    def set_volume(self, volume: float) -> None:
        """Change the sound volume (0..1), now and for later playback."""
        with self._lock:
            self._volume = clamp_volume(volume)
            sound = self._sound
            level = self._volume
        if sound is not None:
            sound.set_volume(level)

    # -- sound wave -----------------------------------------------------

    def request_waveform(
        self,
        path: PathLike,
        duration: float,
        on_peaks: Callable[[int, list], None],
        on_done: Optional[Callable[[bool], None]] = None,
        *,
        buckets: Optional[int] = None,
        max_seconds: float = WAVE_MAX_SECONDS,
    ) -> None:
        """Measure the loudness of the whole sound track in the background.

        The track is decoded once as low-rate mono PCM (:func:`build_waveform_cmd`,
        one thread, below-normal priority on Windows) and folded into
        ``buckets`` peaks (default :func:`waveform_buckets`) of 0..1.
        ``on_peaks(first_index, peaks)`` delivers runs of finished buckets as
        they come (a few times a second); ``on_done(complete)`` follows, with
        ``complete`` False if the track could not be read completely or the
        ``max_seconds`` budget ran out. Nothing is called after
        :meth:`cancel_waveform`, a newer request, :meth:`cancel_all` or
        :meth:`shutdown`, all of which kill ffmpeg. Runs on the
        ``preview-waveform`` thread.
        """
        count = int(buckets) if buckets else waveform_buckets(duration)
        if count <= 0:
            return
        rate = waveform_rate(duration)
        with self._lock:
            if self._closed:
                return
            if self._wave_cancel is not None:
                self._wave_cancel.set()
            cancel = threading.Event()
            self._wave_cancel = cancel
        self._kill_wave()

        def task(cancelled: Callable[[], bool]) -> None:
            def stale() -> bool:
                return cancel.is_set() or cancelled()

            if stale():
                return
            complete = self._measure_wave(path, duration, rate, count, max_seconds, on_peaks, stale)
            if on_done is not None and not stale():
                on_done(complete)

        if not self._waves.submit(task):
            cancel.set()

    def _measure_wave(
        self,
        path: PathLike,
        duration: float,
        rate: int,
        count: int,
        max_seconds: float,
        on_peaks: Callable[[int, list], None],
        stale: Callable[[], bool],
    ) -> bool:
        reducer = PeakReducer(int(round(float(duration) * rate)), count)
        flags = _popen_flags() | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        try:
            proc = self._popen(
                build_waveform_cmd(self.ffmpeg_path, path, rate),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=flags,
            )
        except OSError as exc:
            logger.info("preview: could not start ffmpeg for the sound wave: %s", exc)
            return False
        with self._lock:
            self._wave_proc = proc
        if stale():
            _kill_process(proc)
        pending: list = []

        def flush() -> None:
            if pending and not stale():
                on_peaks(pending[0][0], [peak for _index, peak in pending])
            pending.clear()

        complete = False
        began = last = time.monotonic()
        try:
            while not stale():
                try:
                    chunk = proc.stdout.read(65536)
                except (OSError, ValueError):
                    break
                if not chunk:
                    pending.extend(reducer.finish())
                    complete = reducer.index > 0 and self._exit_ok(proc)
                    break
                pending.extend(reducer.feed(chunk))
                now = time.monotonic()
                if now - last >= 0.25:
                    flush()
                    last = now
                if now - began > max_seconds:
                    logger.info("preview: sound wave took too long; keeping what was measured")
                    break
            flush()
        finally:
            _kill_process(proc)
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass
            with self._lock:
                if self._wave_proc is proc:
                    self._wave_proc = None
        return complete and not stale()

    @staticmethod
    def _exit_ok(proc: Any) -> bool:
        try:
            rc = proc.wait(timeout=2.0)
        except Exception:
            return True  # it produced the whole stream; the exit code is a detail
        return rc in (0, None)

    def _kill_wave(self) -> None:
        with self._lock:
            proc = self._wave_proc
        if proc is not None:
            _kill_process(proc)

    def cancel_waveform(self) -> None:
        """Stop measuring the sound wave (kills ffmpeg); nothing more is delivered."""
        with self._lock:
            cancel = self._wave_cancel
            self._wave_cancel = None
        if cancel is not None:
            cancel.set()
        self._kill_wave()

    # -- lifecycle ------------------------------------------------------

    def cancel_frames(self) -> None:
        """Drop only the pending scrub-frame request.

        For a dialog that shares this service with the main window: posters
        for the queue rows must keep loading, so it must not use ``cancel_all``.
        """
        self._interactive.cancel()

    def cancel_all(self) -> None:
        """Cancel pending/running grabs, posters, the filmstrip, the sound wave
        and playback (with its sound).

        The service stays usable afterwards (e.g. to switch to another file).
        """
        self._interactive.cancel()
        self._posters.clear()
        self.cancel_filmstrip()
        self._filmstrips.clear()
        self.cancel_waveform()
        self._waves.clear()
        self._tracker.kill_all()
        self.stop_playback()

    def shutdown(self, timeout: float = 2.0) -> None:
        """Kill every process and stop every thread; idempotent.

        Blocks at most about ``timeout`` seconds in total (plus the playback
        reader's one second join); stopped sound sessions still finishing get
        the rest of ``timeout``. Nothing is delivered afterwards.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.cancel_filmstrip()
        self.cancel_waveform()
        self.stop_playback()
        self._interactive.signal_shutdown()
        self._posters.signal_shutdown()
        self._filmstrips.signal_shutdown()
        self._waves.signal_shutdown()
        self._tracker.close()
        self._kill_wave()
        deadline = time.monotonic() + max(0.0, timeout)
        for worker in (self._interactive, self._posters, self._filmstrips, self._waves):
            worker.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            retired, self._retired = self._retired, []
        for sound in retired:
            sound.join(max(0.0, deadline - time.monotonic()))
        self.cache.clear()
