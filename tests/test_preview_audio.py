"""Sound in the trim editor preview: argv, the sound wave, the sound session,
video paced by the sound clock, and the service wiring.

No sound hardware and no ffmpeg are needed except for the ``smoke`` tests at
the end (real ffmpeg). The very last one uses a Windows sound device only when
``SQUISHIT_REAL_AUDIO=1`` is set, and even then plays pure silence at volume
0: no test makes a sound. Fake pipes, a fake sound output and fake clocks
stand in for the rest.
"""

from __future__ import annotations

import array
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from video_compressor.core import preview
from video_compressor.core.audio_out import REAL_AUDIO_ENV, AudioError, AudioOutput, NullAudio, SyncClock
from video_compressor.core.preview import (
    AudioSession,
    PeakReducer,
    PlaybackSession,
    PreviewService,
    build_audio_cmd,
    build_playback_cmd,
    build_waveform_cmd,
    wave_level,
    waveform_buckets,
    waveform_rate,
)

WAIT = 5.0
BLOCK = 4  # bytes per stereo s16 sample frame


def _wait_until(predicate, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Pipe:
    """ffmpeg's stdout: hands out ``data`` (optionally in small reads), then EOF.

    ``hold=True`` blocks at the end instead (ffmpeg still running) until the
    process is killed. ``clock``/``cost`` charge each frame-sized read, and
    ``first_cost`` (if given) the first one instead: ffmpeg's start-up and seek.
    """

    def __init__(self, data: bytes = b"", *, chunk=None, hold=False, frames=None, clock=None, cost=0.0,
                 first_cost=None):
        self._parts = list(frames) if frames is not None else ([bytes(data)] if data else [])
        self._cur = memoryview(b"")
        self._chunk = chunk
        self._hold = hold
        self._clock = clock
        self._cost = cost
        self._first_cost = first_cost
        self.closed = False
        self.ended = threading.Event()
        self.killed = threading.Event()

    def _take(self, n: int) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        if not len(self._cur):
            if not self._parts:
                if self._hold and not self.killed.is_set():
                    self.killed.wait(WAIT)
                self.ended.set()
                return b""
            self._cur = memoryview(self._parts.pop(0))
            if self._clock is not None:
                cost = self._cost if self._first_cost is None else self._first_cost
                self._first_cost = None
                self._clock.now += cost
        n = min(n, len(self._cur), self._chunk or n)
        out = self._cur[:n].tobytes()
        self._cur = self._cur[n:]
        return out

    def readinto(self, buf) -> int:
        data = self._take(len(buf))
        buf[: len(data)] = data
        return len(data)

    def read(self, n: int = -1) -> bytes:
        return self._take(n if n and n > 0 else 1 << 30)

    def close(self) -> None:
        self.closed = True
        self.killed.set()


class Proc:
    def __init__(self, stdout: Pipe, rc: int = 0):
        self.stdout = stdout
        self._rc = rc
        self.killed = False
        self.waited = False

    def poll(self):
        if self.killed:
            return -9
        return self._rc if self.stdout.ended.is_set() else None

    def kill(self) -> None:
        self.killed = True
        self.stdout.killed.set()

    def wait(self, timeout=None):
        self.waited = True
        rc = self.poll()
        return self._rc if rc is None else rc


class Popen:
    """Returns the given processes in turn and records every launch."""

    def __init__(self, *procs, error=None):
        self.procs = list(procs)
        self.error = error
        self.calls: list = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if self.error is not None:
            raise self.error
        return self.procs.pop(0)


class FakeOutput(AudioOutput):
    """An in-memory sound device holding ``capacity`` bytes.

    ``autoplay`` plays everything at once; otherwise the test calls
    :meth:`play`. Position = bytes played / 192000.
    """

    def __init__(self, *, capacity: int = 1 << 20, autoplay: bool = True, open_ok: bool = True, fail_on_write: bool = False):
        self.capacity = capacity
        self.autoplay = autoplay
        self.open_ok = open_ok
        self.fail_on_write = fail_on_write
        self.cv = threading.Condition()
        self.queued = 0
        self.played = 0
        self.data = bytearray()
        self.events: list = []
        self.volume = None
        self.stopped = False
        self.opened = None

    def open(self, sample_rate, channels):
        self.opened = (sample_rate, channels)
        self.events.append("open")
        return self.open_ok

    def write(self, data, timeout=None):
        if self.fail_on_write:
            raise AudioError("waveOutWrite failed with code 5")
        view = memoryview(data).cast("B")
        done = 0
        with self.cv:
            if not self.events or self.events[-1] != "write":
                self.events.append("write")
            while done < len(view):
                if self.stopped:
                    break
                room = self.capacity - self.queued
                if room <= 0:
                    self.cv.wait(0.05)
                    continue
                n = min(room, len(view) - done)
                self.data += view[done:done + n].tobytes()
                self.queued += n
                done += n
                if self.autoplay:
                    self.played += self.queued
                    self.queued = 0
        return done

    def play(self, n: int) -> None:
        with self.cv:
            n = min(n, self.queued)
            self.queued -= n
            self.played += n
            self.cv.notify_all()

    def position_seconds(self):
        return self.played / 192_000

    def set_volume(self, volume):
        self.volume = volume

    def pause(self):
        self.events.append("pause")

    def resume(self):
        self.events.append("resume")

    def drain(self, timeout=None):
        with self.cv:
            while self.queued and not self.stopped:
                self.cv.wait(0.05)
        self.events.append("drain")
        return True

    def stop(self):
        with self.cv:
            if not self.stopped:
                self.events.append("stop")
            self.stopped = True
            self.cv.notify_all()


def _pcm(n_bytes: int) -> bytes:
    return bytes((i * 7) % 256 for i in range(n_bytes))


def _session(output, popen, *, start=4.0, on_status=None, **kw):
    states: list = []

    def report(state):
        states.append(state)
        if on_status is not None:
            on_status(state)

    session = AudioSession(
        "ffmpeg", "clip.mp4", start,
        output_factory=lambda: output,
        on_status=report,
        popen=popen,
        **kw,
    )
    return session, states


# ---------------------------------------------------------------------------
# argv
# ---------------------------------------------------------------------------


def test_audio_cmd_exact_argv():
    assert build_audio_cmd("ffmpeg", "a.mp4", 12.3456) == [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-ss", "12.345",
        "-i", "a.mp4",
        "-map", "0:a:0",
        "-vn", "-sn", "-dn",
        "-af", "aresample=async=1:first_pts=0",
        "-f", "s16le", "-ac", "2", "-ar", "48000",
        "-",
    ]


@pytest.mark.parametrize(
    "path",
    [
        r"C:\My Videos\holiday clip.mp4",
        "/tmp/vidéo 日本語 ñ.mkv",
        "weird;name,with[brackets]'quote\".mp4",
        Path("folder with space") / "x.mov",
    ],
)
def test_sound_argv_keeps_any_path_as_one_element(path):
    for cmd in (build_audio_cmd("ff", path, 0), build_waveform_cmd("ff", path)):
        assert cmd[cmd.index("-i") + 1] == os.fspath(path)
        assert sum(os.fspath(path) in part for part in cmd) == 1


def test_waveform_cmd_exact_argv():
    assert build_waveform_cmd("ffmpeg", "a.mp4", 2000) == [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-threads", "1",
        "-i", "a.mp4",
        "-map", "0:a:0",
        "-vn", "-sn", "-dn",
        "-af", "aresample=async=1:first_pts=0",
        "-ac", "1", "-ar", "2000",
        "-f", "s16le",
        "-",
    ]


@pytest.mark.parametrize(
    "duration, expected",
    [(0, 0), (-3, 0), (float("nan"), 0), ("x", 0), (1.0, 100), (60.0, 3000), (10_000, 20000)],
)
def test_waveform_bucket_count_is_bounded(duration, expected):
    assert waveform_buckets(duration) == expected


def test_long_videos_are_measured_at_a_lower_rate():
    assert waveform_rate(600) == 8000
    assert waveform_rate(4 * 3600) == 2000


def test_wave_level_is_a_gentle_curve():
    assert wave_level(0) == 0.0 and wave_level(-1) == 0.0 and wave_level("x") == 0.0
    assert wave_level(1.0) == 1.0 and wave_level(4.0) == 1.0
    assert wave_level(0.25) == pytest.approx(0.5)
    assert wave_level(0.01) == pytest.approx(0.1)  # quiet sound still shows


# ---------------------------------------------------------------------------
# PeakReducer
# ---------------------------------------------------------------------------


def _s16(values) -> bytes:
    data = array.array("h", values)
    if sys.byteorder == "big":
        data.byteswap()
    return data.tobytes()


def test_peaks_per_bucket_use_the_loudest_sample_either_side():
    reducer = PeakReducer(total_samples=8, buckets=4)
    done = reducer.feed(_s16([100, -200, 0, 16384, -32768, 5, 0, 0]))
    assert done == [
        (0, pytest.approx(200 / 32768)),
        (1, pytest.approx(0.5)),
        (2, 1.0),
        (3, 0.0),
    ]
    assert reducer.finish() == []


def test_peaks_do_not_depend_on_how_the_pipe_splits_the_bytes():
    rng = random.Random(7)
    samples = [rng.randint(-32768, 32767) for _ in range(5003)]
    data = _s16(samples)
    whole = PeakReducer(len(samples), 37)
    expected = whole.feed(data) + whole.finish()
    pieces = PeakReducer(len(samples), 37)
    got: list = []
    pos = 0
    while pos < len(data):
        step = rng.randint(1, 999)  # odd sizes split samples in half
        got += pieces.feed(data[pos:pos + step])
        pos += step
    got += pieces.finish()
    assert got == expected
    assert [index for index, _ in got] == list(range(37))


def test_a_short_stream_finishes_its_last_bucket_and_extra_samples_are_ignored():
    short = PeakReducer(total_samples=100, buckets=4)
    assert short.feed(_s16([1000] * 30)) == [(0, pytest.approx(1000 / 32768))]
    assert short.finish() == [(1, pytest.approx(1000 / 32768))]
    assert short.finish() == []
    long = PeakReducer(total_samples=4, buckets=2)
    assert len(long.feed(_s16([1, 2, 3, 4, 30000, 30000]))) == 2
    assert long.finish() == []


def test_more_buckets_than_samples_still_counts_every_bucket():
    reducer = PeakReducer(total_samples=3, buckets=6)
    done = reducer.feed(_s16([3276, 3276, 3276])) + reducer.finish()
    assert [index for index, _ in done] == [0, 1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# AudioSession
# ---------------------------------------------------------------------------


def test_sound_is_held_paused_until_the_first_frame_then_starts():
    output = FakeOutput(autoplay=False)
    proc = Proc(Pipe(_pcm(48_000), hold=True))
    session, states = _session(output, Popen(proc), volume=0.4)
    session.start()
    assert _wait_until(lambda: output.queued > 0)
    assert output.events[:2] == ["open", "pause"]  # pre-roll fills a paused device
    assert output.volume == pytest.approx(0.4)
    assert output.opened == (48_000, 2)
    assert session.clock() == 0.0 and not session.clock.started
    session.release()
    session.release()  # idempotent
    assert output.events.count("resume") == 1 and session.clock.started
    assert states == ["playing"]
    session.stop()
    assert proc.killed and "stop" in output.events


def test_release_before_the_device_opens_skips_the_pause():
    gate = threading.Event()
    output = FakeOutput()

    def factory():
        gate.wait(WAIT)
        return output

    session = AudioSession("ffmpeg", "x.mp4", 0, output_factory=factory, popen=Popen(Proc(Pipe(_pcm(4000)))))
    session.start()
    session.release()
    gate.set()
    assert _wait_until(lambda: "drain" in output.events)
    assert "pause" not in output.events
    assert output.events.index("resume") < output.events.index("write")  # started, even if it opened paused
    session.stop()


def test_the_decoder_is_paced_by_the_device_back_pressure():
    output = FakeOutput(capacity=8_000, autoplay=False)
    data = _pcm(40_000)
    session, states = _session(output, Popen(Proc(Pipe(data))))
    session.start()
    session.release()
    assert _wait_until(lambda: output.queued == 8_000)
    time.sleep(0.05)
    assert len(output.data) == 8_000  # full: nothing more is decoded until it plays
    deadline = time.monotonic() + WAIT
    while len(output.data) < len(data) and time.monotonic() < deadline:
        output.play(4_000)
        time.sleep(0.002)
    output.play(10**9)
    assert _wait_until(lambda: "ended" in states)
    assert bytes(output.data) == data
    session.stop()


def test_end_of_the_track_drains_then_hands_the_clock_to_real_time():
    output = FakeOutput()
    proc = Proc(Pipe(_pcm(19_200 * 2), chunk=1000))
    session, states = _session(output, Popen(proc))
    session.start()
    session.release()
    assert _wait_until(lambda: states == ["playing", "ended"])
    assert output.events[-2:] == ["drain", "stop"]
    assert not session.clock.following_audio
    assert proc.waited and proc.stdout.closed
    assert len(output.data) == 19_200 * 2


def test_a_partial_sample_frame_at_the_end_is_dropped():
    output = FakeOutput()
    session, states = _session(output, Popen(Proc(Pipe(_pcm(4003)))))
    session.start()
    session.release()
    assert _wait_until(lambda: "ended" in states)
    assert len(output.data) == 4000


def test_no_sound_from_ffmpeg_means_silence_not_an_error():
    output = FakeOutput()
    session, states = _session(output, Popen(Proc(Pipe(b"", ), rc=1)))
    session.start()
    assert _wait_until(lambda: states == ["playing", "none"])
    assert not session.clock.following_audio
    session.stop()


def test_playing_after_a_shorter_sound_track_ends_is_not_a_read_error():
    """ffmpeg exits cleanly with no samples when the sound is already over at the playhead."""
    output = FakeOutput()
    session, states = _session(output, Popen(Proc(Pipe(b""), rc=0)), start=30.0)
    session.start()
    assert _wait_until(lambda: states == ["playing", "past_end"])
    assert not session.clock.following_audio  # the picture plays on in real time
    session.stop()


class SlowOpenOutput(FakeOutput):
    """A device that takes a while to open (a Bluetooth headset waking up)."""

    def __init__(self, delay: float, **kw):
        super().__init__(**kw)
        self.delay = delay

    def open(self, sample_rate, channels):
        time.sleep(self.delay)
        return super().open(sample_rate, channels)


def test_stopping_never_waits_for_a_slow_sound_driver(service, videos):
    outputs: list = []

    def factory():
        outputs.append(SlowOpenOutput(0.8, autoplay=False))
        return outputs[-1]

    procs = [Proc(Pipe(_pcm(100_000), hold=True)) for _ in range(2)]
    popen = Popen(*procs)
    svc = service(audio_output_factory=factory, popen=popen)
    svc.start_playback("a.mp4", 0.0, (64, 36), 30.0, lambda t, d: None, lambda r: None, audio=True)
    assert _wait_until(lambda: len(outputs) == 1)  # its worker is now inside the slow open
    started = time.monotonic()
    svc.start_playback("a.mp4", 5.0, (64, 36), 30.0, lambda t, d: None, lambda r: None, audio=True)  # seek
    svc.stop_playback()
    assert time.monotonic() - started < 0.5  # the GUI thread is not held up (the open takes 0.8 s)
    # Both workers finish on their own: devices closed, and nothing was left running.
    assert _wait_until(lambda: len(outputs) == 2 and all(o.stopped for o in outputs))
    assert _wait_until(lambda: all(sound.finished for sound in svc._retired))
    assert popen.calls == []  # stopped before ffmpeg was ever started
    svc.shutdown()
    assert svc._retired == []

@pytest.mark.parametrize("output", [FakeOutput(open_ok=False), NullAudio()])
def test_no_sound_device_means_silence_and_no_decoder(output):
    popen = Popen(Proc(Pipe(_pcm(100))))
    session, states = _session(output, popen)
    session.start()
    assert _wait_until(lambda: states == ["unavailable"])
    assert popen.calls == []  # nothing decoded for nobody to hear
    session.release()
    time.sleep(0.02)
    assert session.clock() > 0.0  # the picture runs on real time
    session.stop()


def test_an_output_that_cannot_even_be_made_means_silence():
    def broken():
        raise OSError("winmm missing")

    states: list = []
    session = AudioSession("ffmpeg", "x.mp4", 0, output_factory=broken, on_status=states.append, popen=Popen())
    session.start()
    assert _wait_until(lambda: states == ["unavailable"])
    session.stop()


def test_ffmpeg_that_will_not_start_means_silence():
    output = FakeOutput()
    session, states = _session(output, Popen(error=OSError("no ffmpeg")))
    session.start()
    assert _wait_until(lambda: states == ["none"])
    assert "stop" in output.events
    session.stop()


def test_a_device_error_while_playing_falls_back_to_silence():
    output = FakeOutput(fail_on_write=True)
    proc = Proc(Pipe(_pcm(10_000), hold=True))
    session, states = _session(output, Popen(proc))
    session.start()
    assert _wait_until(lambda: states == ["playing", "failed"])
    assert not session.clock.following_audio
    assert proc.killed and "stop" in output.events
    session.stop()


def test_stop_is_prompt_idempotent_and_leaves_no_ffmpeg_behind():
    output = FakeOutput(capacity=4000, autoplay=False)
    proc = Proc(Pipe(_pcm(100_000)))
    session, states = _session(output, Popen(proc))
    session.start()
    assert _wait_until(lambda: output.queued == 4000)  # writer blocked on a full device
    started = time.monotonic()
    session.stop()
    assert time.monotonic() - started < 1.5
    assert proc.killed and output.stopped
    assert not any(t.name == "preview-audio" and t.is_alive() for t in threading.enumerate())
    session.stop()
    assert states == ["playing"]  # nothing reported after stop


def test_stop_before_start_never_launches_anything():
    popen = Popen(Proc(Pipe(_pcm(10))))
    session, states = _session(FakeOutput(), popen)
    session.stop()
    session.start()
    time.sleep(0.02)
    assert popen.calls == [] and states == []


def test_volume_changes_reach_the_open_device():
    output = FakeOutput(autoplay=False)
    session, _ = _session(output, Popen(Proc(Pipe(_pcm(50_000), hold=True))))
    session.set_volume(0.3)
    session.start()
    assert _wait_until(lambda: output.volume == pytest.approx(0.3))
    session.set_volume(1.7)
    assert output.volume == 1.0
    session.stop()


def test_sound_that_never_starts_is_dropped_and_the_picture_goes_on():
    output = FakeOutput(autoplay=False)  # nothing ever plays: position stays 0
    proc = Proc(Pipe(_pcm(50_000), hold=True))
    session, states = _session(output, Popen(proc), grace=0.05)
    session.start()
    assert _wait_until(lambda: output.queued > 0)
    session.release()
    time.sleep(0.08)
    session.clock()  # past the grace period: give up on the sound
    assert _wait_until(lambda: proc.killed and output.stopped)
    assert states == ["playing", "failed"]
    time.sleep(0.02)
    assert session.clock() > 0.0
    session.stop()


def test_the_sound_launch_arguments():
    popen = Popen(Proc(Pipe(_pcm(400))))
    session, states = _session(FakeOutput(), popen, start=7.5)
    session.start()
    session.release()
    assert _wait_until(lambda: "ended" in states)
    (cmd, kwargs), = popen.calls
    assert cmd == build_audio_cmd("ffmpeg", "clip.mp4", 7.5)
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# Video paced by the sound clock
# ---------------------------------------------------------------------------

W, H = 2, 2
FRAME = W * H * 3


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        if len(self.sleeps) > 10_000:
            raise RuntimeError("waiting forever")  # fail the session instead of hanging
        self.sleeps.append(seconds)
        self.now += seconds


def _frames(n: int) -> list:
    return [bytes([(i + 1) % 256]) * FRAME for i in range(n)]


class Recorder:
    def __init__(self, clock=None):
        self.clock = clock
        self.frames: list = []
        self.at: list = []
        self.ends: list = []
        self.done = threading.Event()

    def on_frame(self, t, data):
        self.frames.append((t, data))
        if self.clock is not None:
            self.at.append(self.clock.now)

    def on_end(self, reason):
        self.ends.append(reason)
        self.done.set()


def _synced(rec, popen, clock, master, *, start=0.0, fps=10.0, duration=None, **kw):
    return PlaybackSession(
        "ffmpeg", "clip.mp4", start, W, H, fps, rec.on_frame, rec.on_end, duration,
        popen=popen, clock=clock, sleep=clock.sleep, master_clock=master, **kw,
    )


def test_frames_follow_the_master_clock_and_the_first_frame_starts_the_sound():
    clock = FakeClock()
    rec = Recorder(clock)
    started: list = []
    master = lambda: clock.now - 1000.0  # noqa: E731
    session = _synced(rec, Popen(Proc(Pipe(frames=_frames(5)))), clock, master, start=2.0,
                      on_first_frame=lambda: started.append(len(rec.frames)))
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["eof"]
    assert [t for t, _ in rec.frames] == pytest.approx([2.0 + k / 10.0 for k in range(5)])
    assert rec.at == pytest.approx([1000.0 + k * 0.1 for k in range(5)])
    assert started == [1]  # once, right after the first frame was shown
    assert (session.frames_delivered, session.frames_dropped, session.catch_ups) == (5, 0, 0)


def test_the_picture_waits_while_the_sound_has_not_started():
    clock = FakeClock()
    rec = Recorder(clock)
    sound = {"pos": 0.0}

    def master():
        return sound["pos"]

    def sleep(seconds):
        if clock.now > 1100:
            raise RuntimeError("waiting forever")
        clock.now += max(seconds, 0.01)
        if clock.now > 1000.5:  # the sound starts half a second late
            sound["pos"] = clock.now - 1000.5

    session = PlaybackSession("ffmpeg", "c.mp4", 0.0, W, H, 10.0, rec.on_frame, rec.on_end,
                              popen=Popen(Proc(Pipe(frames=_frames(3)))), clock=clock, sleep=sleep,
                              master_clock=master)
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.at[0] == pytest.approx(1000.0)
    assert rec.at[1] >= 1000.5 + 0.1 - 1e-6  # frame 1 waited for the late sound


def test_late_frames_are_dropped_when_the_consumer_falls_behind():
    clock = FakeClock()
    rec = Recorder(clock)
    master = lambda: clock.now - 1000.0  # noqa: E731

    def on_frame(t, data):
        rec.on_frame(t, data)
        if round(t * 10) == 1:
            clock.now += 0.35  # a slow GUI

    session = PlaybackSession("ffmpeg", "c.mp4", 0.0, W, H, 10.0, on_frame, rec.on_end,
                              popen=Popen(Proc(Pipe(frames=_frames(8)))), clock=clock,
                              sleep=clock.sleep, master_clock=master)
    session.start()
    assert rec.done.wait(WAIT)
    assert [round(t * 10) for t, _ in rec.frames] == [0, 1, 4, 5, 6, 7]
    assert session.frames_dropped == 2
    assert [d for _, d in rec.frames] == [bytes([k + 1]) * FRAME for k in (0, 1, 4, 5, 6, 7)]


def test_a_decoder_that_cannot_keep_up_is_restarted_at_the_sound():
    clock = FakeClock()
    rec = Recorder(clock)
    master = lambda: clock.now - 1000.0  # noqa: E731
    slow = Proc(Pipe(frames=_frames(20), clock=clock, cost=0.25))  # 2.5x slower than real time
    fresh = Proc(Pipe(frames=_frames(3)))
    popen = Popen(slow, fresh)
    session = _synced(rec, popen, clock, master, start=10.0, max_lag=0.6, catch_up_lead=0.3)
    session.start()
    assert rec.done.wait(WAIT)
    assert session.catch_ups == 1
    assert slow.killed and slow.stdout.closed
    (first_cmd, _), (second_cmd, _) = popen.calls
    assert first_cmd == build_playback_cmd("ffmpeg", "clip.mp4", 10.0, W, H, 10.0, None)
    jump_seek = float(second_cmd[second_cmd.index("-ss") + 1])
    # It restarts ahead of the sound, never behind it.
    assert jump_seek > 10.0 + 0.6
    times = [t for t, _ in rec.frames]
    assert times[-3:] == pytest.approx([jump_seek + k / 10.0 for k in range(3)], abs=1e-3)
    assert times == sorted(times)
    assert rec.ends == ["eof"]


def test_a_catch_up_past_the_requested_length_ends_cleanly():
    clock = FakeClock()
    rec = Recorder(clock)
    master = lambda: clock.now - 1000.0  # noqa: E731
    slow = Proc(Pipe(frames=_frames(20), clock=clock, cost=0.5))
    popen = Popen(slow)
    session = _synced(rec, popen, clock, master, duration=1.0, max_lag=0.6, catch_up_lead=0.5)
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["eof"] and len(popen.calls) == 1


def test_synced_stop_mid_stream_reports_once():
    rec = Recorder()
    proc = Proc(Pipe(frames=_frames(2), hold=True))
    session = PlaybackSession("ffmpeg", "c.mp4", 0.0, W, H, 10.0, rec.on_frame, rec.on_end,
                              popen=Popen(proc), master_clock=SyncClock(None))
    session.start()
    assert _wait_until(lambda: len(rec.frames) >= 1)
    session.stop()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["stopped"] and proc.killed


class Spawner:
    """popen stand-in that makes a fresh fake ffmpeg per launch (``make(n)``, n from 0)."""

    def __init__(self, make, limit=50):
        self.make = make
        self.limit = limit
        self.calls: list = []

    def __call__(self, cmd, **kwargs):
        if len(self.calls) >= self.limit:
            raise OSError("too many launches")
        self.calls.append(list(cmd))
        return self.make(len(self.calls) - 1)

    def seeks(self) -> list:
        return [float(cmd[cmd.index("-ss") + 1]) for cmd in self.calls]


class Live:
    """A master clock on the fake clock that says whether it follows live sound."""

    def __init__(self, clock, following=True):
        self.clock = clock
        self.following_audio = following

    def __call__(self) -> float:
        return self.clock.now - 1000.0


def test_without_live_sound_the_picture_is_paced_like_silent_playback():
    """No device / no sound from here / track over: no catch-up restarts, smooth slow motion."""
    clock = FakeClock()
    rec = Recorder(clock)
    master = SyncClock(None, mono=clock)  # what a session with no live sound has
    popen = Popen(Proc(Pipe(frames=_frames(12), clock=clock, cost=0.25)))  # 2.5x too slow
    session = PlaybackSession("ffmpeg", "c.mp4", 0.0, W, H, 10.0, rec.on_frame, rec.on_end,
                              popen=popen, clock=clock, sleep=clock.sleep,
                              master_clock=master, on_first_frame=master.start)
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["eof"] and len(popen.calls) == 1
    assert (session.frames_delivered, session.frames_dropped, session.catch_ups) == (12, 0, 0)
    assert [round(t * 10) for t, _ in rec.frames] == list(range(12))


def test_when_the_sound_stops_being_live_pacing_switches_to_real_time():
    clock = FakeClock()
    rec = Recorder(clock)
    master = Live(clock)

    def on_frame(t, data):
        rec.on_frame(t, data)
        if len(rec.frames) == 2:
            master.following_audio = False  # e.g. the sound track ended

    popen = Popen(Proc(Pipe(frames=_frames(10), clock=clock, cost=0.25)))
    session = PlaybackSession("ffmpeg", "c.mp4", 0.0, W, H, 10.0, on_frame, rec.on_end,
                              popen=popen, clock=clock, sleep=clock.sleep, master_clock=master)
    session.start()
    assert rec.done.wait(WAIT)
    assert session.catch_ups == 0 and len(popen.calls) == 1  # would have restarted while live
    assert session.frames_delivered == 10


def test_catch_up_restarts_aim_past_ffmpegs_measured_start_up():
    """1.2 s to start, 40 ms a frame at 30 fps: each restart must still show a long run of frames."""
    clock = FakeClock()
    rec = Recorder(clock)
    master = Live(clock)
    launches_at_frame: list = []

    def make(n):
        if n < 2:  # slow decoders: they keep falling behind
            return Proc(Pipe(frames=_frames(400), clock=clock, cost=0.040, first_cost=1.2))
        return Proc(Pipe(frames=_frames(60), clock=clock, cost=0.0, first_cost=1.2))  # keeps up

    spawner = Spawner(make)

    def on_frame(t, data):
        rec.on_frame(t, data)
        launches_at_frame.append(len(spawner.calls))

    session = PlaybackSession("ffmpeg", "c.mp4", 0.0, W, H, 30.0, on_frame, rec.on_end,
                              popen=spawner, clock=clock, sleep=clock.sleep, master_clock=master)
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["eof"]
    assert session.catch_ups == 2
    per_launch = [launches_at_frame.count(n) for n in (1, 2, 3)]
    # The old fixed 0.3 s lead showed one frame per restart here.
    assert per_launch[1] >= 30, per_launch
    assert per_launch[2] == 60, per_launch  # landed ahead of the sound: nothing late, nothing dropped
    seeks = spawner.seeks()
    assert seeks[1] - rec.frames[launches_at_frame.index(2) - 1][0] >= 1.2  # aimed past the start-up time
    times = [t for t, _ in rec.frames]
    assert times == sorted(times)


def test_hopeless_catch_ups_give_up_and_play_on_in_slow_motion():
    clock = FakeClock()
    rec = Recorder(clock)
    master = Live(clock)
    lost: list = []
    spawner = Spawner(lambda n: Proc(Pipe(frames=_frames(40), clock=clock, cost=0.2, first_cost=0.1)))
    session = PlaybackSession("ffmpeg", "c.mp4", 0.0, W, H, 30.0, rec.on_frame, rec.on_end,
                              popen=spawner, clock=clock, sleep=clock.sleep, master_clock=master,
                              on_sync_lost=lambda: lost.append(len(spawner.calls)))
    session.start()
    assert rec.done.wait(WAIT)
    assert rec.ends == ["eof"]
    assert lost == [4]  # once, after three catch-ups in a row that did not hold
    assert session.catch_ups == 3 and len(spawner.calls) == 4
    # From there on it is smooth slow motion: every remaining frame of that ffmpeg is shown.
    tail = [t for t, _ in rec.frames if t >= float(spawner.calls[-1][spawner.calls[-1].index("-ss") + 1])]
    assert len(tail) == 40


def test_a_session_that_loses_sync_drops_the_sound(service, videos):
    output = FakeOutput(autoplay=False)
    proc = Proc(Pipe(_pcm(200_000), hold=True))
    svc = service(audio_output_factory=lambda: output, popen=Popen(proc))
    states: list = []
    video = svc.start_playback("a.mp4", 0.0, (64, 36), 30.0, lambda t, d: None, lambda r: None,
                               audio=True, on_audio=states.append)
    assert _wait_until(lambda: output.queued > 0)
    sound = svc._sound
    video.kw["on_first_frame"]()
    video.kw["on_sync_lost"]()  # the picture gave up keeping up
    assert states == ["playing", "behind"]
    assert output.stopped and proc.killed
    assert not sound.clock.following_audio  # the picture now runs on real time


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class VideoSession:
    """Stands in for PlaybackSession inside the service."""

    instances: list = []

    def __init__(self, ffmpeg, path, start, w, h, fps_out, on_frame, on_end, duration=None, **kw):
        self.args = (ffmpeg, path, start, w, h, fps_out, duration)
        self.kw = kw
        self.on_frame, self.on_end = on_frame, on_end
        self.started = self.stopped = 0
        VideoSession.instances.append(self)

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


@pytest.fixture
def videos(monkeypatch):
    VideoSession.instances = []
    monkeypatch.setattr(preview, "PlaybackSession", VideoSession)
    return VideoSession.instances


@pytest.fixture
def service():
    made: list = []

    def make(**kw):
        svc = PreviewService("ffmpeg", "ffprobe", grab=lambda *a, **k: None, **kw)
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.shutdown()


def test_service_with_sound_paces_the_picture_by_the_sound(service, videos):
    output = FakeOutput(autoplay=False)
    proc = Proc(Pipe(_pcm(200_000), hold=True))
    popen = Popen(proc)
    svc = service(audio_output_factory=lambda: output, popen=popen)
    ends: list = []
    states: list = []
    video = svc.start_playback("a b.mp4", 3.0, (64, 36), 30.0, lambda t, d: None, ends.append,
                               audio=True, volume=0.25, on_audio=states.append)
    assert video.started == 1
    sound = svc._sound
    assert video.kw["master_clock"] is sound.clock
    assert video.kw["on_first_frame"] == sound.release
    assert _wait_until(lambda: output.queued > 0)
    assert popen.calls[0][0] == build_audio_cmd("ffmpeg", "a b.mp4", 3.0)
    assert output.volume == pytest.approx(0.25)
    video.kw["on_first_frame"]()  # the first frame is up: the sound starts
    assert "resume" in output.events
    video.on_end("eof")  # the picture ended: so does the sound
    assert ends == ["eof"] and proc.killed and output.stopped
    assert states == ["playing"]


def test_service_without_sound_is_the_old_silent_playback(service, videos):
    popen = Popen()
    svc = service(audio_output_factory=FakeOutput, popen=popen)
    ends: list = []
    video = svc.start_playback("a.mp4", 1.0, (64, 36), 30.0, lambda t, d: None, ends.append)
    assert video.kw == {} and svc._sound is None and popen.calls == []
    video.on_end("eof")
    assert ends == ["eof"]


def test_service_stop_seek_and_volume(service, videos):
    outputs: list = []

    def factory():
        outputs.append(FakeOutput(autoplay=False))
        return outputs[-1]

    procs = [Proc(Pipe(_pcm(100_000), hold=True)) for _ in range(3)]
    popen = Popen(*procs)
    svc = service(audio_output_factory=factory, popen=popen)
    first = svc.start_playback("a.mp4", 0.0, (64, 36), 30.0, lambda t, d: None, lambda r: None, audio=True)
    assert _wait_until(lambda: outputs and outputs[0].queued > 0)
    svc.set_volume(0.6)
    assert outputs[0].volume == pytest.approx(0.6)
    # Seeking starts a new session: the old picture and sound both stop.
    second = svc.start_playback("a.mp4", 9.0, (64, 36), 30.0, lambda t, d: None, lambda r: None, audio=True)
    assert first.stopped == 1 and procs[0].killed and outputs[0].stopped  # at once, not after a join
    assert _wait_until(lambda: len(outputs) == 2 and outputs[1].volume == pytest.approx(0.6))
    assert _wait_until(lambda: outputs[1].queued > 0)  # its ffmpeg is running
    svc.stop_playback()
    assert second.stopped == 1 and procs[1].killed and outputs[1].stopped
    svc.stop_playback()  # nothing left
    third = svc.start_playback("a.mp4", 0.0, (64, 36), 30.0, lambda t, d: None, lambda r: None, audio=True)
    assert _wait_until(lambda: len(outputs) == 3)
    svc.cancel_all()
    assert third.stopped == 1
    # However far its worker had got, the third sound ends silent with no ffmpeg left
    # (killed if it was started, never started otherwise).
    assert _wait_until(lambda: all(sound.finished for sound in svc._retired))
    assert outputs[2].stopped
    assert procs[2].killed or len(popen.calls) == 2


def test_a_closed_service_starts_no_sound(service, videos):
    popen = Popen()
    svc = service(audio_output_factory=FakeOutput, popen=popen)
    svc.shutdown()
    video = svc.start_playback("a.mp4", 0.0, (64, 36), 30.0, lambda t, d: None, lambda r: None, audio=True)
    time.sleep(0.02)
    assert video.started == 0 and video.stopped == 1 and popen.calls == []


def _wave_pcm(seconds: float, rate: int, loud_from: float) -> bytes:
    n = int(seconds * rate)
    start = int(loud_from * rate)
    return _s16([0] * start + [16384 if i % 2 else -16384 for i in range(n - start)])


def test_service_measures_the_sound_wave_in_the_background(service):
    data = _wave_pcm(4.0, 8000, loud_from=2.0)
    popen = Popen(Proc(Pipe(data, chunk=4096)))
    svc = service(popen=popen)
    got: dict = {}
    done: list = []

    def on_peaks(first, peaks):
        assert threading.current_thread().name == "preview-waveform"
        for i, peak in enumerate(peaks):
            got[first + i] = peak

    svc.request_waveform("my clip.mp4", 4.0, on_peaks, done.append, buckets=8)
    assert _wait_until(lambda: done == [True])
    assert [round(got[i], 3) for i in range(8)] == [0, 0, 0, 0, 0.5, 0.5, 0.5, 0.5]
    (cmd, kwargs), = popen.calls
    assert cmd == build_waveform_cmd("ffmpeg", "my clip.mp4", 8000)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    assert kwargs["creationflags"] == flags
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL


def test_a_track_that_cannot_be_read_is_not_complete(service):
    svc = service(popen=Popen(Proc(Pipe(b""), rc=1)))
    done: list = []
    svc.request_waveform("x.mp4", 10.0, lambda *a: pytest.fail("no peaks"), done.append)
    assert _wait_until(lambda: done == [False])


def test_cancelling_the_sound_wave_kills_ffmpeg_and_stays_quiet(service):
    proc = Proc(Pipe(_wave_pcm(1.0, 8000, 0.0), hold=True))
    svc = service(popen=Popen(proc))
    calls: list = []
    svc.request_waveform("x.mp4", 100.0, lambda *a: calls.append("peaks"), lambda c: calls.append(c))
    assert _wait_until(lambda: proc.stdout.ended.is_set() or len(proc.stdout._parts) == 0)
    svc.cancel_waveform()
    assert _wait_until(lambda: proc.killed and proc.stdout.closed)
    time.sleep(0.05)
    assert False not in calls and True not in calls  # no on_done after a cancel


def test_a_new_sound_wave_request_replaces_the_old_one(service):
    old = Proc(Pipe(_wave_pcm(0.5, 8000, 0.0), hold=True))
    new = Proc(Pipe(_wave_pcm(1.0, 8000, 0.0)))
    svc = service(popen=Popen(old, new))
    seen: list = []
    svc.request_waveform("x.mp4", 50.0, lambda *a: None, lambda c: seen.append(("old", c)))
    assert _wait_until(lambda: not old.stdout._parts)
    svc.request_waveform("x.mp4", 1.0, lambda *a: None, lambda c: seen.append(("new", c)), buckets=10)
    assert _wait_until(lambda: seen == [("new", True)])
    assert old.killed


def test_shutdown_stops_the_sound_wave(service):
    proc = Proc(Pipe(b"", hold=True))
    svc = service(popen=Popen(proc))
    svc.request_waveform("x.mp4", 30.0, lambda *a: None)
    assert _wait_until(lambda: len(svc._waves._queue) == 0 and svc._wave_proc is proc)
    svc.shutdown()
    assert proc.killed
    svc.request_waveform("x.mp4", 30.0, lambda *a: pytest.fail("closed"))


def test_nothing_to_measure_for_an_empty_video(service):
    popen = Popen()
    svc = service(popen=popen)
    svc.request_waveform("x.mp4", 0.0, lambda *a: None)
    time.sleep(0.02)
    assert popen.calls == []


# ---------------------------------------------------------------------------
# Real ffmpeg (smoke)
# ---------------------------------------------------------------------------


def _ffmpeg() -> str:
    from video_compressor.core.compressor import VideoCompressor

    path = VideoCompressor().ffmpeg_path
    if not (shutil.which(path) or Path(path).exists()):
        pytest.skip("ffmpeg is not available")
    return path


def _make_media(ffmpeg: str, path: Path, *, audio: bool, seconds: int = 4, silent: bool = False) -> Path:
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=30:duration={seconds}"]
    if audio and silent:
        # A real sound track of pure silence (for the opt-in real-device check).
        cmd += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={seconds}"]
    elif audio:
        # Silent for the first second, then a tone. Only ever decoded, never played.
        cmd += ["-f", "lavfi", "-i",
                f"aevalsrc='0.5*sin(2*PI*440*t)*gte(t,1)':s=48000:d={seconds}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "96k", "-shortest"] if audio else ["-an"]
    cmd.append(str(path))
    subprocess.run(cmd, check=True, capture_output=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return path


@pytest.mark.smoke
def test_real_ffmpeg_streams_the_sound_from_the_playhead(tmp_path):
    ffmpeg = _ffmpeg()
    media = _make_media(ffmpeg, tmp_path / "tone clip é.mp4", audio=True)
    out = subprocess.run(build_audio_cmd(ffmpeg, media, 1.5), capture_output=True, timeout=60,
                         stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    seconds = len(out.stdout) / (48_000 * BLOCK)
    assert seconds == pytest.approx(2.5, abs=0.1)
    silent = _make_media(ffmpeg, tmp_path / "silent.mp4", audio=False)
    none = subprocess.run(build_audio_cmd(ffmpeg, silent, 0), capture_output=True, timeout=60,
                          stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert none.stdout == b""  # no sound track: nothing to play, and the session says so


@pytest.mark.smoke
def test_real_ffmpeg_lines_up_a_sound_track_that_starts_late(tmp_path):
    ffmpeg = _ffmpeg()
    media = tmp_path / "late.mp4"
    subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=4",
         "-itsoffset", "1", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
         "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", str(media)],
        check=True, capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = subprocess.run(build_audio_cmd(ffmpeg, media, 0.0), capture_output=True, timeout=60,
                         stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    samples = array.array("h")
    samples.frombytes(out.stdout[: len(out.stdout) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    left = samples[::2]
    first = next(i for i, value in enumerate(left) if abs(value) > 100)
    # The sound starts a second into the video, so it must not play from 0:00.
    assert first / 48_000 == pytest.approx(1.0, abs=0.05)
    assert len(left) / 48_000 == pytest.approx(4.0, abs=0.1)


@pytest.mark.smoke
def test_real_ffmpeg_sound_wave_shows_silence_then_sound(tmp_path):
    ffmpeg = _ffmpeg()
    media = _make_media(ffmpeg, tmp_path / "wave.mp4", audio=True)
    svc = PreviewService(ffmpeg)
    got: dict = {}
    done: list = []
    try:
        svc.request_waveform(media, 4.0, lambda first, peaks: got.update(
            {first + i: p for i, p in enumerate(peaks)}), done.append, buckets=40)
        assert _wait_until(lambda: done, timeout=30)
    finally:
        svc.shutdown()
    assert done == [True]
    assert max(got[i] for i in range(0, 9)) < 0.05  # the silent first second
    assert min(got[i] for i in range(12, 38)) > 0.3  # then the tone


@pytest.mark.smoke
@pytest.mark.skipif(
    os.environ.get(REAL_AUDIO_ENV) != "1",
    reason="real sound device checks are opt-in: set SQUISHIT_REAL_AUDIO=1",
)
def test_real_sound_session_on_this_pc_keeps_the_picture_in_step(tmp_path):
    """Opt-in, Windows with a sound device only. Completely silent: the track
    is pure silence and the volume is 0 (samples are zeroed before the device)."""
    if sys.platform != "win32":
        pytest.skip("waveOut is Windows only")
    from video_compressor.core.audio_out import WaveOutAudio, has_audio_device

    if not has_audio_device():
        pytest.skip("no sound output device")
    ffmpeg = _ffmpeg()
    media = _make_media(ffmpeg, tmp_path / "av.mp4", audio=True, silent=True)
    svc = PreviewService(ffmpeg, audio_output_factory=WaveOutAudio)
    frames: list = []
    states: list = []
    ends: list = []
    try:
        svc.start_playback(media, 1.0, (160, 90), 30.0, lambda t, d: frames.append(t), ends.append,
                           audio=True, volume=0.0, on_audio=states.append)
        sound = svc._sound
        lags: list = []
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if frames and sound.clock.started:
                lags.append((frames[-1] - 1.0) - sound.clock())
            time.sleep(0.05)
    finally:
        svc.shutdown()
    if "unavailable" in states:
        pytest.skip("the sound device could not be opened")
    assert states[0] == "playing"
    assert len(frames) > 30
    # The picture never runs ahead of the sound, nor more than a few frames behind it.
    assert max(lags) <= 1.0 / 30 + 0.02
    assert min(lags) >= -0.15
