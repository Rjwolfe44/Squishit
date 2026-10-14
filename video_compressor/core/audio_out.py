"""Sound output for the trim editor preview (no Qt, no extra packages).

:class:`AudioOutput` is the small interface the preview uses: open a device for
16-bit PCM, :meth:`~AudioOutput.write` samples (blocking while the device queue
is full, which is the back-pressure that paces the decoder), read the device
clock, set the volume, pause/resume and stop. Implementations:

* :class:`WaveOutAudio`: Windows ``winmm`` waveOut through ``ctypes``. ctypes
  and ``winmm.dll`` are loaded lazily, and only on Windows.
* :class:`NullAudio`: silent. Used on other platforms, when there is no sound
  device, and after any error, so playback carries on without sound.

The volume is applied to the samples (:func:`scale_pcm16`), never with
``waveOutSetVolume``: that call changes the program's slider in the Windows
volume mixer, and Windows remembers it for that program for good.

:class:`SyncClock` turns the device clock into the master clock that video
frames are paced against.

Everything here is thread-safe and imports on any platform (tests run on
Linux with a fake ``winmm``). Under pytest the real sound device is never
opened unless ``SQUISHIT_REAL_AUDIO=1`` is set (:func:`real_device_allowed`).
"""

from __future__ import annotations

import array
import logging
import math
import os
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "AudioError",
    "AudioOutput",
    "NullAudio",
    "SyncClock",
    "WaveOutAudio",
    "clamp_volume",
    "default_audio_output",
    "has_audio_device",
    "open_audio_output",
    "real_device_allowed",
    "scale_pcm16",
]

#: Set to 1 to let tests use the real sound device (they still only write silence).
REAL_AUDIO_ENV = "SQUISHIT_REAL_AUDIO"


def real_device_allowed() -> bool:
    """False while pytest runs, unless ``SQUISHIT_REAL_AUDIO=1``: tests never reach the speakers."""
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return True
    return os.environ.get(REAL_AUDIO_ENV, "").strip() == "1"

#: PCM format the preview decodes to (``-f s16le -ac 2 -ar 48000``).
SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_BYTES = 2

# winmm constants (mmsystem.h / mmeapi.h)
WAVE_MAPPER = 0xFFFFFFFF
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001
WHDR_PREPARED = 0x00000002
WHDR_INQUEUE = 0x00000010
TIME_MS = 0x0001
TIME_SAMPLES = 0x0002
TIME_BYTES = 0x0004
MMSYSERR_NOERROR = 0
WAVERR_STILLPLAYING = 33


class AudioError(RuntimeError):
    """A sound device call failed; the caller should carry on silently."""


def clamp_volume(volume: Any) -> float:
    """``volume`` as a float in ``[0, 1]`` (junk and NaN become 0)."""
    try:
        value = float(volume)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(value):
        return 0.0
    return min(1.0, max(0.0, value))


def scale_pcm16(data: bytes, volume: float) -> bytes:
    """Signed 16-bit little-endian PCM with every sample scaled by ``volume`` (0..1).

    Full volume returns ``data`` untouched, zero volume returns silence of the
    same length, and a trailing odd byte is kept as is.
    """
    volume = clamp_volume(volume)
    data = bytes(data)
    if volume >= 1.0:
        return data
    if volume <= 0.0:
        return bytes(len(data))
    whole = len(data) - len(data) % 2
    samples = array.array("h")
    samples.frombytes(data[:whole])
    if sys.byteorder == "big":
        samples.byteswap()
    factor = int(round(volume * 32768))
    scaled = array.array("h", [(sample * factor) >> 15 for sample in samples])
    if sys.byteorder == "big":
        scaled.byteswap()
    return scaled.tobytes() + data[whole:]


class AudioOutput:
    """Where decoded PCM goes. Subclasses implement every method.

    Contract (all methods are safe from any thread):

    * :meth:`open` returns ``True`` when the device is ready. Signed 16-bit
      little-endian interleaved PCM only.
    * :meth:`write` queues ``data`` and blocks while the device queue is full
      (back-pressure). It returns how many bytes were queued: all of them,
      fewer after :meth:`stop` or when ``timeout`` runs out, and 0 once
      stopped. It raises :class:`AudioError` if the device fails.
    * :meth:`position_seconds` is the device clock: seconds of sound played
      since :meth:`open`. It never runs backwards.
    * :meth:`pause` / :meth:`resume` hold and release the device; while
      paused, writes queue up (pre-roll) and the clock stands still.
    * :meth:`drain` waits until everything written has been played.
    * :meth:`stop` silences the device at once and releases it. Idempotent;
      it also wakes a blocked :meth:`write`.
    """

    #: True for the silent stand-in (its position is not a real clock).
    is_null = False

    def open(self, sample_rate: int, channels: int) -> bool:
        raise NotImplementedError

    def write(self, data: bytes, timeout: Optional[float] = None) -> int:
        raise NotImplementedError

    def position_seconds(self) -> float:
        raise NotImplementedError

    def set_volume(self, volume: float) -> None:
        raise NotImplementedError

    def pause(self) -> None:
        raise NotImplementedError

    def resume(self) -> None:
        raise NotImplementedError

    def drain(self, timeout: Optional[float] = None) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class NullAudio(AudioOutput):
    """Silent output: accepts everything at once and plays nothing."""

    is_null = True

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stopped = False
        self.volume = 1.0
        self.sample_rate = 0
        self.channels = 0

    def open(self, sample_rate: int, channels: int) -> bool:
        with self._lock:
            self.sample_rate, self.channels = int(sample_rate), int(channels)
            return not self._stopped

    def write(self, data: bytes, timeout: Optional[float] = None) -> int:
        with self._lock:
            return 0 if self._stopped else len(data)

    def position_seconds(self) -> float:
        return 0.0

    def set_volume(self, volume: float) -> None:
        self.volume = clamp_volume(volume)

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def drain(self, timeout: Optional[float] = None) -> bool:
        return True

    def stop(self) -> None:
        with self._lock:
            self._stopped = True

    @property
    def stopped(self) -> bool:
        return self._stopped


# ---------------------------------------------------------------------------
# Windows waveOut
# ---------------------------------------------------------------------------


_LOAD_LOCK = threading.Lock()
_TYPES: Optional[SimpleNamespace] = None
_WINMM: Optional[SimpleNamespace] = None


def _types() -> SimpleNamespace:
    """The winmm structures, built once on first use (ctypes is imported lazily).

    Built under a lock: two threads racing here must not end up with two
    different ``WAVEHDR`` classes, which ctypes would refuse to mix.
    """
    global _TYPES
    if _TYPES is None:
        with _LOAD_LOCK:
            if _TYPES is None:
                _TYPES = _build_types()
    return _TYPES


def _build_types() -> SimpleNamespace:
    import ctypes

    class WAVEFORMATEX(ctypes.Structure):
        _pack_ = 1  # mmsystem.h packs to one byte: sizeof == 18
        _fields_ = [
            ("wFormatTag", ctypes.c_uint16),
            ("nChannels", ctypes.c_uint16),
            ("nSamplesPerSec", ctypes.c_uint32),
            ("nAvgBytesPerSec", ctypes.c_uint32),
            ("nBlockAlign", ctypes.c_uint16),
            ("wBitsPerSample", ctypes.c_uint16),
            ("cbSize", ctypes.c_uint16),
        ]

    class WAVEHDR(ctypes.Structure):
        _fields_ = [
            ("lpData", ctypes.c_void_p),
            ("dwBufferLength", ctypes.c_uint32),
            ("dwBytesRecorded", ctypes.c_uint32),
            ("dwUser", ctypes.c_size_t),
            ("dwFlags", ctypes.c_uint32),
            ("dwLoops", ctypes.c_uint32),
            ("lpNext", ctypes.c_void_p),
            ("reserved", ctypes.c_size_t),
        ]

    class MMTIME(ctypes.Structure):
        # wType, then an 8-byte union; every member we read is its first DWORD.
        _fields_ = [
            ("wType", ctypes.c_uint32),
            ("u", ctypes.c_uint32),
            ("_pad", ctypes.c_uint32),
        ]

    return SimpleNamespace(
        ctypes=ctypes,
        WAVEFORMATEX=WAVEFORMATEX,
        WAVEHDR=WAVEHDR,
        MMTIME=MMTIME,
    )


def _load_winmm() -> SimpleNamespace:
    """``winmm.dll`` waveOut functions with argument types set (Windows only, loaded once)."""
    global _WINMM
    if sys.platform != "win32":
        raise AudioError("waveOut sound needs Windows")
    types = _types()
    if _WINMM is None:
        with _LOAD_LOCK:
            if _WINMM is None:
                _WINMM = _bind_winmm(types)
    return _WINMM


def _bind_winmm(types: SimpleNamespace) -> SimpleNamespace:
    ctypes = types.ctypes
    dll = ctypes.WinDLL("winmm")
    handle = ctypes.c_void_p
    uint = ctypes.c_uint

    def bind(name: str, argtypes: list) -> Callable[..., int]:
        function = getattr(dll, name)
        function.argtypes = argtypes
        function.restype = uint
        return function

    header = ctypes.POINTER(types.WAVEHDR)
    return SimpleNamespace(
        waveOutGetNumDevs=bind("waveOutGetNumDevs", []),
        waveOutOpen=bind(
            "waveOutOpen",
            [
                ctypes.POINTER(handle),
                uint,
                ctypes.POINTER(types.WAVEFORMATEX),
                ctypes.c_size_t,
                ctypes.c_size_t,
                ctypes.c_uint32,
            ],
        ),
        waveOutPrepareHeader=bind("waveOutPrepareHeader", [handle, header, uint]),
        waveOutUnprepareHeader=bind("waveOutUnprepareHeader", [handle, header, uint]),
        waveOutWrite=bind("waveOutWrite", [handle, header, uint]),
        waveOutGetPosition=bind(
            "waveOutGetPosition", [handle, ctypes.POINTER(types.MMTIME), uint]
        ),
        # No waveOutSetVolume on purpose: see the module docstring.
        waveOutPause=bind("waveOutPause", [handle]),
        waveOutRestart=bind("waveOutRestart", [handle]),
        waveOutReset=bind("waveOutReset", [handle]),
        waveOutClose=bind("waveOutClose", [handle]),
    )


def has_audio_device(api: Any = None) -> bool:
    """True when Windows reports at least one sound output device."""
    try:
        winmm = api if api is not None else _load_winmm()
        return int(winmm.waveOutGetNumDevs()) > 0
    except Exception:
        return False


class WaveOutAudio(AudioOutput):
    """Streams PCM to the default Windows sound device with winmm waveOut.

    ``buffers`` headers of ``buffer_ms`` each are cycled: :meth:`write` copies
    into a free one, prepares and queues it, and when all are queued waits
    until the device marks one ``WHDR_DONE`` (polling the flags, so no kernel
    event is needed). Each ``WAVEHDR`` and its memory live in ``self._slots``
    for as long as this object does, so the device never points at freed
    memory. Every winmm call happens under one lock. The volume scales the
    samples as they are copied (:func:`scale_pcm16`), so a change is heard
    within one queue length (about 0.2 s).

    A device that stops taking sound while not paused (for example unplugged
    mid-play: no buffer comes back for ``stall_timeout`` seconds) makes
    :meth:`write` and :meth:`drain` raise :class:`AudioError` instead of
    waiting forever. ``api`` is a test seam replacing :func:`_load_winmm`;
    without it the real device is only opened when :func:`real_device_allowed`.
    """

    def __init__(
        self,
        *,
        buffer_ms: int = 50,
        buffers: int = 4,
        api: Any = None,
        poll: float = 0.005,
        stall_timeout: float = 3.0,
    ) -> None:
        self._api_override = api
        self._api: Any = None
        self._buffer_ms = max(5, int(buffer_ms))
        self._count = max(2, int(buffers))
        self._poll = max(0.001, float(poll))
        self._stall_timeout = max(0.01, float(stall_timeout))
        self._paused = False
        self._progress_at = time.monotonic()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._handle: Any = None
        self._slots: list = []
        self._in_flight: set = set()
        self._open = False
        self._stopped = False
        self._volume = 1.0
        self._rate = 0
        self._block = 0
        self._buf_bytes = 0
        self._written = 0
        self._last_raw = 0
        self._wraps = 0
        self._position = 0.0

    # -- setup ------------------------------------------------------------

    def open(self, sample_rate: int, channels: int) -> bool:
        with self._lock:
            if self._open or self._stopped:
                return self._open
            if self._api_override is None and not real_device_allowed():
                logger.debug("preview: the real sound device is off while tests run")
                return False
            try:
                api = self._api_override if self._api_override is not None else _load_winmm()
                types = _types()
                ctypes = types.ctypes
                rate, chans = int(sample_rate), int(channels)
                if rate <= 0 or chans <= 0:
                    return False
                block = chans * SAMPLE_BYTES
                fmt = types.WAVEFORMATEX()
                fmt.wFormatTag = WAVE_FORMAT_PCM
                fmt.nChannels = chans
                fmt.nSamplesPerSec = rate
                fmt.nAvgBytesPerSec = rate * block
                fmt.nBlockAlign = block
                fmt.wBitsPerSample = 8 * SAMPLE_BYTES
                fmt.cbSize = 0
                handle = ctypes.c_void_p()
                rc = api.waveOutOpen(ctypes.byref(handle), WAVE_MAPPER, fmt, 0, 0, CALLBACK_NULL)
                if rc != MMSYSERR_NOERROR or not handle.value:
                    logger.info("preview: no sound device (waveOutOpen returned %s)", rc)
                    return False
                self._api, self._handle = api, handle
                self._rate, self._block = rate, block
                frames = max(1, rate * self._buffer_ms // 1000)
                self._buf_bytes = frames * block
                self._slots = [
                    (types.WAVEHDR(), ctypes.create_string_buffer(self._buf_bytes))
                    for _ in range(self._count)
                ]
                self._open = True
                self._progress_at = time.monotonic()
                return True
            except Exception:
                logger.info("preview: could not open the sound device", exc_info=True)
                return False

    # -- writing ----------------------------------------------------------

    @property
    def buffer_bytes(self) -> int:
        """Size of one device buffer (a convenient read size for the decoder)."""
        return self._buf_bytes

    def write(self, data: bytes, timeout: Optional[float] = None) -> int:
        view = memoryview(data).cast("B")
        total = len(view)
        done = 0
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while done < total:
            with self._lock:
                if not self._open or self._stopped:
                    break
                slot = self._free_slot()
                if slot is not None:
                    count = min(self._buf_bytes, total - done)
                    self._queue(slot, view[done:done + count])
                    done += count
                    continue
                self._check_stall()
            if deadline is not None and time.monotonic() >= deadline:
                break
            self._wake.wait(self._poll)  # all buffers queued: wait for one to play out
        return done

    def _check_stall(self) -> None:
        """Raise if the device has taken nothing for too long while playing (lock held)."""
        if self._paused or not self._in_flight:
            self._progress_at = time.monotonic()
            return
        if time.monotonic() - self._progress_at > self._stall_timeout:
            raise AudioError("the sound device stopped playing (unplugged or switched off?)")

    def _free_slot(self) -> Optional[int]:
        """Index of a header that is not queued, reclaiming finished ones (lock held)."""
        types = _types()
        size = types.ctypes.sizeof(types.WAVEHDR)
        for index, (header, _memory) in enumerate(self._slots):
            if index not in self._in_flight:
                return index
            if header.dwFlags & WHDR_DONE:
                rc = self._api.waveOutUnprepareHeader(self._handle, header, size)
                if rc != MMSYSERR_NOERROR:
                    self._fail("waveOutUnprepareHeader", rc)
                self._in_flight.discard(index)
                self._progress_at = time.monotonic()
                return index
        return None

    def _queue(self, slot: int, chunk: memoryview) -> None:
        """Copy ``chunk`` into header ``slot`` (at the volume) and hand it to the device (lock held)."""
        types = _types()
        ctypes = types.ctypes
        header, memory = self._slots[slot]
        count = len(chunk)
        ctypes.memmove(memory, scale_pcm16(chunk.tobytes(), self._volume), count)
        header.lpData = ctypes.addressof(memory)
        header.dwBufferLength = count
        header.dwBytesRecorded = 0
        header.dwUser = 0
        header.dwFlags = 0
        header.dwLoops = 0
        header.lpNext = None
        header.reserved = 0
        size = ctypes.sizeof(types.WAVEHDR)
        rc = self._api.waveOutPrepareHeader(self._handle, header, size)
        if rc != MMSYSERR_NOERROR:
            self._fail("waveOutPrepareHeader", rc)
        rc = self._api.waveOutWrite(self._handle, header, size)
        if rc != MMSYSERR_NOERROR:
            self._api.waveOutUnprepareHeader(self._handle, header, size)
            self._fail("waveOutWrite", rc)
        self._in_flight.add(slot)
        self._written += count

    def _fail(self, call: str, rc: int) -> None:
        raise AudioError(f"{call} failed with code {rc}")

    # -- clock, volume, transport ------------------------------------------

    def position_seconds(self) -> float:
        with self._lock:
            if not self._open or self._stopped or self._rate <= 0:
                return self._position
            types = _types()
            stamp = types.MMTIME()
            stamp.wType = TIME_SAMPLES
            try:
                rc = self._api.waveOutGetPosition(
                    self._handle, stamp, types.ctypes.sizeof(types.MMTIME)
                )
            except Exception:
                return self._position
            if rc != MMSYSERR_NOERROR:
                return self._position
            raw = int(stamp.u)
            if raw < self._last_raw and self._last_raw - raw > 0x7FFFFFFF:
                self._wraps += 1  # the 32-bit counter wrapped (about a day of sound)
            self._last_raw = raw
            value = raw + self._wraps * 0x1_0000_0000
            if stamp.wType == TIME_SAMPLES:
                seconds = value / self._rate
            elif stamp.wType == TIME_BYTES:
                seconds = value / (self._rate * self._block)
            elif stamp.wType == TIME_MS:
                seconds = value / 1000.0
            else:
                return self._position
            written = self._written / (self._rate * self._block)
            self._position = max(self._position, min(seconds, written))
            return self._position

    def set_volume(self, volume: float) -> None:
        """Scale what is written from now on (0..1). Never touches the Windows mixer."""
        with self._lock:
            self._volume = clamp_volume(volume)

    @property
    def volume(self) -> float:
        return self._volume

    def pause(self) -> None:
        with self._lock:
            if self._open and not self._stopped:
                self._api.waveOutPause(self._handle)
                self._paused = True

    def resume(self) -> None:
        with self._lock:
            if self._open and not self._stopped:
                self._api.waveOutRestart(self._handle)
                self._paused = False
                self._progress_at = time.monotonic()

    def drain(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        last_busy = -1
        while True:
            with self._lock:
                if not self._open or self._stopped:
                    return True
                busy = sum(
                    1 for index in self._in_flight
                    if not self._slots[index][0].dwFlags & WHDR_DONE
                )
                if not busy:
                    return True
                if busy != last_busy:
                    last_busy = busy
                    self._progress_at = time.monotonic()
                self._check_stall()
            if deadline is not None and time.monotonic() >= deadline:
                return False
            self._wake.wait(self._poll)

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._wake.set()
            if not self._open:
                return
            self._open = False
            types = _types()
            size = types.ctypes.sizeof(types.WAVEHDR)
            try:
                self._api.waveOutReset(self._handle)  # silence now; every buffer comes back done
                for index in sorted(self._in_flight):
                    self._api.waveOutUnprepareHeader(self._handle, self._slots[index][0], size)
                self._in_flight.clear()
                rc = self._api.waveOutClose(self._handle)
                if rc != MMSYSERR_NOERROR:
                    logger.debug("preview: waveOutClose returned %s", rc)
            except Exception:
                logger.debug("preview: error while closing the sound device", exc_info=True)

    @property
    def stopped(self) -> bool:
        return self._stopped


# ---------------------------------------------------------------------------
# Choosing an output
# ---------------------------------------------------------------------------


def default_audio_output() -> AudioOutput:
    """An unopened output for this platform: waveOut on Windows, else silent."""
    if sys.platform == "win32":
        return WaveOutAudio()
    return NullAudio()


def open_audio_output(
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    factory: Optional[Callable[[], AudioOutput]] = None,
) -> AudioOutput:
    """Open the platform output, or a :class:`NullAudio` if that fails in any way."""
    output: Optional[AudioOutput] = None
    try:
        output = (factory or default_audio_output)()
        if output.open(sample_rate, channels):
            return output
    except Exception:
        logger.info("preview: sound output failed to open", exc_info=True)
    if output is not None:
        try:
            output.stop()
        except Exception:
            pass
    silent = NullAudio()
    silent.open(sample_rate, channels)
    return silent


# ---------------------------------------------------------------------------
# Master clock
# ---------------------------------------------------------------------------


class SyncClock:
    """Seconds of media played since playback began, for pacing video frames.

    While sound plays, the clock follows the device position, so the picture
    keeps step with what is heard and cannot drift. When the sound is absent,
    has ended or failed, the clock runs on real time from where it was. It
    never goes backwards.

    * Before :meth:`start` it reads 0.
    * After :meth:`start`, until the device position first moves, it holds at
      0 for up to ``grace`` seconds (the sound is starting); if the sound has
      not started by then the clock gives up on it (``on_give_up`` is called
      so the caller can silence it) and runs on real time.
    * Between device updates it glides forward on real time for at most
      ``glide`` seconds, so frames are paced smoothly even when the device
      reports its position coarsely.
    * If the device position stalls (the decoder fell behind), the clock waits
      for it, but only for ``stall`` seconds; after that it runs on.
    * :meth:`detach` (sound ended or failed) switches to real time for good.

    ``position`` returns the device position in seconds; ``mono`` is the
    real-time clock (a test seam; ``time.perf_counter`` by default because
    ``time.monotonic`` only ticks every 15.6 ms on Windows). Thread-safe.
    """

    def __init__(
        self,
        position: Optional[Callable[[], float]] = None,
        *,
        mono: Optional[Callable[[], float]] = None,
        grace: float = 2.0,
        glide: float = 0.15,
        stall: float = 1.0,
        on_give_up: Optional[Callable[[], None]] = None,
    ) -> None:
        self._position = position
        self._mono = mono if mono is not None else time.perf_counter
        self._grace = max(0.0, float(grace))
        self._glide = max(0.0, float(glide))
        self._stall = max(self._glide, float(stall))
        self._on_give_up = on_give_up
        self._lock = threading.Lock()
        self._origin: Optional[float] = None
        self._value = 0.0
        self._audio_pos = 0.0
        self._audio_at: Optional[float] = None
        self._free_at: Optional[float] = None  # real time when the clock left the sound
        self._free_value = 0.0

    @property
    def started(self) -> bool:
        return self._origin is not None

    @property
    def following_audio(self) -> bool:
        """True while the clock is driven by the sound device."""
        return self._position is not None and self._free_at is None

    def start(self) -> None:
        """Start counting (the first frame is on screen). Idempotent."""
        with self._lock:
            if self._origin is None:
                self._origin = self._mono()
                if self._position is None:
                    self._free_at, self._free_value = self._origin, 0.0

    def detach(self) -> None:
        """The sound has ended or failed: carry on in real time from here."""
        with self._lock:
            if self._free_at is not None and self._position is None:
                return
            now = self._mono()
            if self._origin is not None and self._free_at is None:
                estimate = self._estimate(now)
                if estimate is not None:
                    self._value = max(self._value, estimate)
            self._position = None
            self._free_at, self._free_value = now, self._value

    def __call__(self) -> float:
        give_up = False
        with self._lock:
            if self._origin is None:
                return 0.0
            now = self._mono()
            if self._free_at is not None:
                candidate = self._free_value + (now - self._free_at)
            else:
                candidate = self._estimate(now)
                if candidate is None:  # the sound never started: run without it
                    self._position = None
                    self._free_at, self._free_value = now, self._value
                    candidate = self._value
                    give_up = True
            if candidate > self._value:
                self._value = candidate
            value = self._value
        if give_up and self._on_give_up is not None:
            try:
                self._on_give_up()
            except Exception:
                logger.debug("preview: on_give_up raised", exc_info=True)
        return value

    def _estimate(self, now: float) -> Optional[float]:
        """Clock value from the device position (lock held); ``None`` = give up."""
        try:
            pos = float(self._position()) if self._position is not None else None
        except Exception:
            pos = None
        if pos is None or math.isnan(pos):
            self._position = None
            self._free_at, self._free_value = now, self._value
            return self._value
        if pos > self._audio_pos + 1e-9:
            self._audio_pos, self._audio_at = pos, now
            return pos
        if self._audio_at is None:  # still waiting for the first sound
            if now - self._origin < self._grace:
                return 0.0
            return None
        since = now - self._audio_at
        if since <= self._glide:
            return self._audio_pos + since
        if since <= self._stall:
            return self._audio_pos + self._glide
        return self._audio_pos + self._glide + (since - self._stall)
