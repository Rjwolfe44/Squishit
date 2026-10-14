"""Sound output for the trim editor preview: the AudioOutput contract, waveOut
driven through a fake winmm (so it runs on Linux CI), the fallback to silence,
and the master clock that paces video frames.

No test makes a sound. The last test opens the real Windows sound device, but
only when asked to (``SQUISHIT_REAL_AUDIO=1``) on Windows with a sound device;
it writes one second of silence at volume 0 and checks that the device opened
and its clock moved.
"""

from __future__ import annotations

import array
import ctypes
import os
import sys
import threading
import time

import pytest

from video_compressor.core import audio_out
from video_compressor.core.audio_out import (
    TIME_BYTES,
    TIME_MS,
    TIME_SAMPLES,
    WAVE_FORMAT_PCM,
    WAVE_MAPPER,
    WAVERR_STILLPLAYING,
    WHDR_DONE,
    WHDR_INQUEUE,
    WHDR_PREPARED,
    AudioError,
    AudioOutput,
    NullAudio,
    SyncClock,
    WaveOutAudio,
    clamp_volume,
    default_audio_output,
    has_audio_device,
    open_audio_output,
    scale_pcm16,
)

WAIT = 5.0
RATE, CHANS = 48_000, 2
BLOCK = CHANS * 2


def _wait_until(predicate, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


# ---------------------------------------------------------------------------
# A fake winmm
# ---------------------------------------------------------------------------


class FakeWinmm:
    """Behaves like winmm waveOut for one device; the test decides when buffers play."""

    def __init__(self, *, open_rc=0, write_rc=0, prepare_rc=0, position_type=TIME_SAMPLES, devices=1):
        self.open_rc = open_rc
        self.write_rc = write_rc
        self.prepare_rc = prepare_rc
        self.position_type = position_type
        self.devices = devices
        self.handle_value = 0x5150
        self.calls: list = []
        self.queue: list = []  # headers handed to the device, oldest first
        self.data = bytearray()
        self.played = 0  # bytes
        self.raw_position = None  # force a raw MMTIME value
        self.volume = None
        self.format = None
        self.lock = threading.Lock()

    @property
    def header_size(self) -> int:
        return ctypes.sizeof(audio_out._types().WAVEHDR)

    def waveOutGetNumDevs(self):
        return self.devices

    def waveOutOpen(self, phandle, device, fmt, callback, instance, flags):
        self.calls.append("open")
        self.device, self.flags = device, flags
        self.format = (
            fmt.wFormatTag, fmt.nChannels, fmt.nSamplesPerSec, fmt.nAvgBytesPerSec,
            fmt.nBlockAlign, fmt.wBitsPerSample, fmt.cbSize,
        )
        if self.open_rc:
            return self.open_rc
        phandle._obj.value = self.handle_value
        return 0

    def _check(self, handle, size):
        assert handle.value == self.handle_value
        assert size == self.header_size

    def waveOutPrepareHeader(self, handle, header, size):
        self._check(handle, size)
        self.calls.append("prepare")
        if self.prepare_rc:
            return self.prepare_rc
        assert not header.dwFlags & WHDR_PREPARED
        header.dwFlags |= WHDR_PREPARED
        return 0

    def waveOutWrite(self, handle, header, size):
        self._check(handle, size)
        self.calls.append("write")
        if self.write_rc:
            return self.write_rc
        assert header.dwFlags & WHDR_PREPARED
        with self.lock:
            header.dwFlags = (header.dwFlags & ~WHDR_DONE) | WHDR_INQUEUE
            self.data += ctypes.string_at(header.lpData, header.dwBufferLength)
            self.queue.append(header)
        return 0

    def play(self, count: int = 1) -> None:
        """The device finishes the ``count`` oldest queued buffers."""
        with self.lock:
            for header in self.queue[:count]:
                header.dwFlags = (header.dwFlags & ~WHDR_INQUEUE) | WHDR_DONE
                self.played += header.dwBufferLength
            del self.queue[:count]

    def waveOutUnprepareHeader(self, handle, header, size):
        self._check(handle, size)
        self.calls.append("unprepare")
        if header.dwFlags & WHDR_INQUEUE:
            return WAVERR_STILLPLAYING
        header.dwFlags &= ~WHDR_PREPARED
        return 0

    def waveOutGetPosition(self, handle, stamp, size):
        assert size == ctypes.sizeof(audio_out._types().MMTIME)
        assert stamp.wType == TIME_SAMPLES  # we always ask for samples
        stamp.wType = self.position_type
        if self.raw_position is not None:
            stamp.u = self.raw_position
        elif self.position_type == TIME_SAMPLES:
            stamp.u = self.played // BLOCK
        elif self.position_type == TIME_BYTES:
            stamp.u = self.played
        else:
            stamp.u = self.played * 1000 // (RATE * BLOCK)
        return 0

    def waveOutSetVolume(self, handle, value):
        self.calls.append("volume")
        self.volume = value
        return 0

    def waveOutPause(self, handle):
        self.calls.append("pause")
        return 0

    def waveOutRestart(self, handle):
        self.calls.append("restart")
        return 0

    def waveOutReset(self, handle):
        self.calls.append("reset")
        self.play(len(self.queue))
        return 0

    def waveOutClose(self, handle):
        self.calls.append("close")
        if any(header.dwFlags & WHDR_INQUEUE for header in self.queue):
            return WAVERR_STILLPLAYING
        return 0


def _device(fake=None, **kw) -> WaveOutAudio:
    fake = fake or FakeWinmm()
    out = WaveOutAudio(api=fake, poll=0.001, **kw)
    assert out.open(RATE, CHANS)
    return out


def _pcm(n_bytes: int) -> bytes:
    return bytes(i % 251 for i in range(n_bytes))


# ---------------------------------------------------------------------------
# Structures, NullAudio, helpers
# ---------------------------------------------------------------------------


def test_winmm_structures_match_the_windows_layout():
    types = audio_out._types()
    pointer = ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(types.WAVEFORMATEX) == 18  # byte-packed, as in mmsystem.h
    assert ctypes.sizeof(types.WAVEHDR) == (48 if pointer == 8 else 32)
    assert ctypes.sizeof(types.MMTIME) == 12


@pytest.mark.parametrize("value, expected", [(0.5, 0.5), (-1, 0.0), (3, 1.0), ("x", 0.0), (None, 0.0), (float("nan"), 0.0)])
def test_clamp_volume(value, expected):
    assert clamp_volume(value) == expected


def test_null_audio_accepts_everything_silently_until_stopped():
    out = NullAudio()
    assert out.is_null and isinstance(out, AudioOutput)
    assert out.open(RATE, CHANS)
    assert out.write(b"\0" * 1000) == 1000
    assert out.position_seconds() == 0.0
    assert out.drain(0.01) is True
    out.set_volume(2)
    assert out.volume == 1.0
    out.pause(), out.resume()
    out.stop()
    out.stop()  # idempotent
    assert out.write(b"\0" * 10) == 0
    assert not out.open(RATE, CHANS)


def test_default_output_is_silent_off_windows(monkeypatch):
    monkeypatch.setattr(audio_out.sys, "platform", "linux")
    assert isinstance(default_audio_output(), NullAudio)
    monkeypatch.setattr(audio_out.sys, "platform", "win32")
    assert isinstance(default_audio_output(), WaveOutAudio)


def test_loading_winmm_off_windows_is_a_clean_error(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("this checks the non-Windows path")
    with pytest.raises(AudioError):
        audio_out._load_winmm()
    assert has_audio_device() is False
    assert WaveOutAudio().open(RATE, CHANS) is False  # no crash, just no device


def test_has_audio_device_uses_the_device_count():
    assert has_audio_device(FakeWinmm(devices=2)) is True
    assert has_audio_device(FakeWinmm(devices=0)) is False


# ---------------------------------------------------------------------------
# WaveOutAudio through the fake winmm
# ---------------------------------------------------------------------------


def test_open_asks_for_16_bit_pcm_on_the_default_device():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=3)
    assert fake.format == (WAVE_FORMAT_PCM, 2, 48_000, 192_000, 4, 16, 0)
    assert fake.device == WAVE_MAPPER and fake.flags == 0  # CALLBACK_NULL: we poll the flags
    assert out.buffer_bytes == 480 * BLOCK
    assert "volume" not in fake.calls  # the Windows mixer is never touched
    out.stop()


def test_open_failure_is_reported_not_raised():
    out = WaveOutAudio(api=FakeWinmm(open_rc=2))
    assert out.open(RATE, CHANS) is False
    out.stop()  # nothing to close
    assert WaveOutAudio(api=FakeWinmm()).open(0, 2) is False


def test_write_queues_every_byte_in_order_split_into_buffers():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=4)
    data = _pcm(out.buffer_bytes * 2 + 100)
    assert out.write(data) == len(data)
    assert bytes(fake.data) == data
    assert [h.dwBufferLength for h in fake.queue] == [1920, 1920, 100]
    out.stop()


def test_write_blocks_while_every_buffer_is_queued_back_pressure():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=2)
    data = _pcm(out.buffer_bytes * 3)
    result: list = []
    writer = threading.Thread(target=lambda: result.append(out.write(data)))
    writer.start()
    assert _wait_until(lambda: len(fake.queue) == 2)
    time.sleep(0.05)
    assert writer.is_alive() and len(fake.queue) == 2  # the device is full: write waits
    fake.play(1)  # one buffer finished: it is reclaimed and reused
    writer.join(WAIT)
    assert result == [len(data)]
    assert bytes(fake.data) == data
    assert fake.calls.count("unprepare") == 1
    out.stop()


def test_write_with_a_timeout_returns_what_fit():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=2)
    started = time.monotonic()
    assert out.write(_pcm(out.buffer_bytes * 3), timeout=0.05) == out.buffer_bytes * 2
    assert time.monotonic() - started < 1.0
    out.stop()


def test_stop_wakes_a_blocked_writer_and_releases_the_device_once():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=2)
    result: list = []
    writer = threading.Thread(target=lambda: result.append(out.write(_pcm(out.buffer_bytes * 5))))
    writer.start()
    assert _wait_until(lambda: len(fake.queue) == 2)
    started = time.monotonic()
    out.stop()
    writer.join(WAIT)
    assert not writer.is_alive() and time.monotonic() - started < 1.0
    assert result == [out.buffer_bytes * 2]
    tail = fake.calls[fake.calls.index("reset"):]
    assert tail == ["reset", "unprepare", "unprepare", "close"]  # nothing left prepared
    before = list(fake.calls)
    out.stop()  # idempotent
    assert fake.calls == before
    assert out.write(b"\0" * 8) == 0
    assert out.stopped


def test_a_device_error_raises_and_leaves_no_buffer_prepared():
    fake = FakeWinmm(write_rc=5)
    out = _device(fake)
    with pytest.raises(AudioError, match="waveOutWrite"):
        out.write(_pcm(64))
    assert fake.calls[-1] == "unprepare"
    out.stop()


def test_a_prepare_error_raises():
    out = _device(FakeWinmm(prepare_rc=11))
    with pytest.raises(AudioError, match="waveOutPrepareHeader"):
        out.write(_pcm(64))
    out.stop()


@pytest.mark.parametrize("kind", [TIME_SAMPLES, TIME_BYTES, TIME_MS])
def test_position_is_seconds_played_whatever_unit_the_driver_reports(kind):
    fake = FakeWinmm(position_type=kind)
    out = _device(fake, buffer_ms=100, buffers=4)
    out.write(_pcm(out.buffer_bytes * 3))
    assert out.position_seconds() == 0.0
    fake.play(2)
    assert out.position_seconds() == pytest.approx(0.2, abs=1e-3)
    out.stop()
    assert out.position_seconds() == pytest.approx(0.2, abs=1e-3)  # keeps its last value


def test_position_never_runs_ahead_of_what_was_written_or_backwards():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=100, buffers=4)
    out.write(_pcm(out.buffer_bytes))
    fake.raw_position = 48_000 * 5  # a confused driver
    assert out.position_seconds() == pytest.approx(0.1)
    fake.raw_position = 10
    assert out.position_seconds() == pytest.approx(0.1)
    out.stop()


def test_position_survives_the_32_bit_counter_wrapping():
    fake = FakeWinmm()
    out = _device(fake)
    out._written = 10**12  # pretend a very long session
    fake.raw_position = 0xFFFFFF00
    first = out.position_seconds()
    fake.raw_position = 0x100
    assert out.position_seconds() == pytest.approx(first + 0x200 / RATE)
    out.stop()


def _s16(values) -> bytes:
    data = array.array("h", values)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


def test_volume_scales_the_samples_and_never_touches_the_windows_mixer():
    fake = FakeWinmm()
    out = WaveOutAudio(api=fake, poll=0.001)
    out.set_volume(0.5)  # before open: kept
    assert out.open(RATE, CHANS)
    loud = _s16([1000, -1000, 32767, -32768])
    out.write(loud)
    out.set_volume(0)
    out.write(loud)
    out.set_volume(7)  # clamped to full
    out.write(loud)
    assert bytes(fake.data) == _s16([500, -500, 16383, -16384]) + bytes(len(loud)) + loud
    assert "volume" not in fake.calls and fake.volume is None
    assert out.volume == 1.0
    out.stop()


@pytest.mark.parametrize(
    "volume, samples, expected",
    [
        (1.0, [5, -5, 32767, -32768], [5, -5, 32767, -32768]),
        (0.0, [5, -5, 32767, -32768], [0, 0, 0, 0]),
        (0.5, [100, -100, 32767, -32768], [50, -50, 16383, -16384]),
        (0.25, [4, -4], [1, -1]),
        (float("nan"), [123], [0]),
    ],
)
def test_scale_pcm16(volume, samples, expected):
    assert scale_pcm16(_s16(samples), volume) == _s16(expected)


def test_scale_pcm16_keeps_a_trailing_odd_byte_and_length():
    data = _s16([1000, 2000]) + b"\x07"
    out = scale_pcm16(data, 0.5)
    assert len(out) == len(data) and out[-1:] == b"\x07"
    assert scale_pcm16(data, 0) == bytes(len(data))


def test_the_real_device_is_never_opened_by_tests_without_permission(monkeypatch):
    def forbidden():
        raise AssertionError("tests must not load the real winmm")

    monkeypatch.setattr(audio_out, "_load_winmm", forbidden)
    monkeypatch.delenv(audio_out.REAL_AUDIO_ENV, raising=False)
    assert audio_out.real_device_allowed() is False  # pytest sets PYTEST_CURRENT_TEST
    assert WaveOutAudio().open(RATE, CHANS) is False
    assert isinstance(open_audio_output(), NullAudio)
    monkeypatch.setenv(audio_out.REAL_AUDIO_ENV, "1")
    assert audio_out.real_device_allowed() is True
    monkeypatch.setenv(audio_out.REAL_AUDIO_ENV, "0")
    assert audio_out.real_device_allowed() is False
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert audio_out.real_device_allowed() is True  # the app itself
    # A fake winmm is always fine.
    out = WaveOutAudio(api=FakeWinmm())
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.delenv(audio_out.REAL_AUDIO_ENV, raising=False)
    assert out.open(RATE, CHANS) is True
    out.stop()


def test_a_device_that_stops_taking_sound_is_reported_not_waited_on_forever():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=2, stall_timeout=0.1)
    started = time.monotonic()
    with pytest.raises(AudioError, match="stopped playing"):
        out.write(_pcm(out.buffer_bytes * 4))  # nothing ever comes back (unplugged)
    assert time.monotonic() - started < 2.0
    out.stop()


def test_a_paused_device_is_not_a_stalled_one():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=2, stall_timeout=0.05)
    out.pause()
    assert out.write(_pcm(out.buffer_bytes * 4), timeout=0.2) == out.buffer_bytes * 2  # pre-roll, no error
    out.resume()
    fake.play(2)
    assert out.write(_pcm(out.buffer_bytes)) == out.buffer_bytes
    out.stop()


def test_drain_gives_up_on_a_stalled_device():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=2, stall_timeout=0.1)
    out.write(_pcm(out.buffer_bytes))
    with pytest.raises(AudioError):
        out.drain()
    out.stop()


def test_the_winmm_structures_are_built_once_even_when_threads_race(monkeypatch):
    built: list = []
    real_build = audio_out._build_types

    def slow_build():
        built.append(1)
        time.sleep(0.05)
        return real_build()

    monkeypatch.setattr(audio_out, "_TYPES", None)
    monkeypatch.setattr(audio_out, "_build_types", slow_build)
    seen: list = []
    gate = threading.Barrier(6)

    def grab():
        gate.wait(WAIT)
        seen.append(audio_out._types())

    threads = [threading.Thread(target=grab) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert len(built) == 1
    assert len(seen) == 6 and all(types is seen[0] for types in seen)
    assert seen[0].WAVEHDR is audio_out._types().WAVEHDR


def test_pause_resume_and_drain():
    fake = FakeWinmm()
    out = _device(fake, buffer_ms=10, buffers=4)
    out.pause()
    out.write(_pcm(out.buffer_bytes * 2))
    out.resume()
    assert fake.calls.count("pause") == 1 and fake.calls.count("restart") == 1
    assert out.drain(timeout=0.03) is False  # still playing
    fake.play(2)
    assert out.drain(timeout=1.0) is True
    out.stop()
    assert out.drain(timeout=0) is True


def test_open_audio_output_falls_back_to_silence():
    assert isinstance(open_audio_output(factory=lambda: WaveOutAudio(api=FakeWinmm(open_rc=2))), NullAudio)

    def broken():
        raise OSError("no winmm here")

    assert isinstance(open_audio_output(factory=broken), NullAudio)
    real = open_audio_output(factory=lambda: WaveOutAudio(api=FakeWinmm()))
    assert isinstance(real, WaveOutAudio)
    real.stop()


# ---------------------------------------------------------------------------
# SyncClock: the master clock
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Device:
    def __init__(self) -> None:
        self.pos = 0.0
        self.error = False

    def __call__(self) -> float:
        if self.error:
            raise AudioError("gone")
        return self.pos


def _clock(**kw):
    mono, device = Clock(), Device()
    return SyncClock(device, mono=mono, **kw), mono, device


def test_clock_reads_zero_until_started_and_waits_for_the_sound():
    clock, mono, device = _clock(grace=2.0)
    mono.now += 5
    assert clock() == 0.0 and not clock.started
    clock.start()
    mono.now += 0.5
    assert clock() == 0.0  # the sound has not begun: hold the first frame
    device.pos = 0.01
    mono.now += 0.01
    assert clock() == pytest.approx(0.01)
    assert clock.following_audio


def test_clock_follows_the_device_and_glides_between_coarse_updates():
    clock, mono, device = _clock(glide=0.15)
    clock.start()
    device.pos = 1.0
    assert clock() == 1.0
    mono.now += 0.05  # the device has not reported again yet
    assert clock() == pytest.approx(1.05)
    device.pos = 1.04  # reported a little behind our glide: never go backwards
    mono.now += 0.001
    assert clock() == pytest.approx(1.05)
    device.pos = 1.2
    assert clock() == pytest.approx(1.2)


def test_clock_waits_for_a_stalled_device_then_runs_on():
    clock, mono, device = _clock(glide=0.1, stall=1.0)
    clock.start()
    device.pos = 2.0
    assert clock() == 2.0
    mono.now += 0.5  # an underrun: the picture holds
    assert clock() == pytest.approx(2.1)
    mono.now += 0.4
    assert clock() == pytest.approx(2.1)
    mono.now += 0.6  # stuck too long: carry on in real time
    assert clock() == pytest.approx(2.1 + 0.5)


def test_clock_gives_up_on_sound_that_never_starts():
    gave_up: list = []
    clock, mono, device = _clock(grace=1.0, on_give_up=lambda: gave_up.append(1))
    clock.start()
    mono.now += 0.9
    assert clock() == 0.0 and not gave_up
    mono.now += 0.2
    assert clock() == 0.0 and gave_up == [1]
    assert not clock.following_audio
    mono.now += 0.5
    device.pos = 0.3  # late sound is ignored from now on
    assert clock() == pytest.approx(0.5)
    assert gave_up == [1]


def test_detach_continues_smoothly_in_real_time():
    clock, mono, device = _clock()
    clock.start()
    device.pos = 3.0
    assert clock() == 3.0
    mono.now += 0.05
    clock.detach()  # the sound track ended
    value = clock()
    assert value == pytest.approx(3.05)
    mono.now += 1.0
    device.pos = 0.0
    assert clock() == pytest.approx(4.05)
    clock.detach()  # idempotent
    assert clock() == pytest.approx(4.05)


def test_a_failing_device_turns_into_real_time():
    clock, mono, device = _clock()
    clock.start()
    device.pos = 1.0
    assert clock() == 1.0
    device.error = True
    mono.now += 0.2
    assert clock() == 1.0
    mono.now += 0.3
    assert clock() == pytest.approx(1.3)


def test_clock_without_a_device_is_real_time_from_start():
    mono = Clock()
    clock = SyncClock(None, mono=mono)
    assert clock() == 0.0
    clock.start()
    mono.now += 0.25
    assert clock() == pytest.approx(0.25)
    assert not clock.following_audio


def test_detached_before_start_counts_from_start():
    clock, mono, device = _clock()
    clock.detach()  # e.g. no sound device
    mono.now += 3
    assert clock() == 0.0
    clock.start()
    mono.now += 0.5
    assert clock() == pytest.approx(0.5)


def test_clock_is_safe_to_read_from_many_threads():
    clock = SyncClock(lambda: time.perf_counter() % 1000)
    clock.start()
    values: list = []

    def read():
        last = 0.0
        for _ in range(2000):
            value = clock()
            assert value >= last
            last = value
        values.append(last)

    threads = [threading.Thread(target=read) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    assert len(values) == 4


# ---------------------------------------------------------------------------
# The real device (this Windows PC only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="waveOut is Windows only")
@pytest.mark.skipif(
    os.environ.get(audio_out.REAL_AUDIO_ENV) != "1",
    reason="real sound device checks are opt-in: set SQUISHIT_REAL_AUDIO=1",
)
def test_real_sound_device_opens_plays_and_its_clock_advances():
    if not has_audio_device():
        pytest.skip("no sound output device on this machine")
    out = WaveOutAudio()
    if not out.open(RATE, CHANS):
        pytest.skip("the sound device is present but busy or unavailable")
    try:
        out.set_volume(0.0)
        # One second of SILENCE at volume 0: the test must never make a sound on
        # anyone's PC. Opening the device and watching its clock advance proves
        # the same thing.
        tone = bytes(RATE * CHANS * 2)
        started = time.perf_counter()
        assert out.write(tone) == len(tone)
        first = out.position_seconds()
        time.sleep(0.15)
        second = out.position_seconds()
        assert second > first  # the device clock moves on its own
        assert out.drain(timeout=3.0)
        assert out.position_seconds() == pytest.approx(1.0, abs=0.02)
        assert time.perf_counter() - started < 3.0
    finally:
        stop_started = time.perf_counter()
        out.stop()
        assert time.perf_counter() - stop_started < 0.5
