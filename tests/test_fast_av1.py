"""Fast AV1: verified hardware, SVT-AV1 speed policy, and the fallback chain.

No GPU and no real FFmpeg here. ``HardwareAv1Probe`` runs through a fake
runner, and ``compress()`` runs through a fake FFmpeg process that records
each argv. The chain under test:

    verified hardware AV1 (NVENC, QSV, AMF) -> libsvtav1 at a fast preset
    -> libaom realtime -> HEVC (or H.264) with a note on the result.

Max / Archival (SVT-AV1) does not use any of it.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from video_compressor.core import fast_av1
from video_compressor.core.codecs import AudioCodec, CodecSettings, RateControl, VideoCodec
from video_compressor.core.compressor import VideoCompressor, VideoInfo
from video_compressor.core.fast_av1 import (
    HardwareAv1Probe,
    RunOutput,
    aom_tiles,
    fast_av1_svt_preset,
    output_pixels,
    probe_command,
    svt_lp_memory_cap,
    svt_preset_floor,
)
from video_compressor.core.governor import threads_for_job
from video_compressor.core.hardware import GPUInfo, GPUVendor, HardwareDetector, HardwareInfo
from video_compressor.core.profiles import CompressionProfile, ProfileType
from video_compressor.core.quality_ladder import QualityRung, get_step, matching_rung
from video_compressor.gui.copy import AV1_PLAYBACK_HINT
from video_compressor.gui.hw_status import (
    FoundHardware,
    HardwareProbe,
    describe_hw_status,
    probe_from_compressor,
)

_ALL_AV1 = {"av1_nvenc", "av1_qsv", "av1_amf", "libsvtav1", "libaom-av1", "libx265", "libx264", "aac", "libopus"}
_FORBIDDEN_WORDS = ("live", "rtmp", "broadcast")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class Runner:
    """Fake FFmpeg for the probe: per-encoder outcomes, and a call log."""

    def __init__(self, outcomes=None, default="fail"):
        self.outcomes = dict(outcomes or {})
        self.default = default
        self.calls: list[list[str]] = []
        self.lock = threading.Lock()

    def __call__(self, cmd, timeout):
        with self.lock:
            self.calls.append(list(cmd))
        encoder = cmd[cmd.index("-c:v") + 1]
        outcome = self.outcomes.get(encoder, self.default)
        if callable(outcome):
            return outcome(cmd, timeout)
        if outcome == "ok":
            return RunOutput(0, "frame=8\nprogress=end\n", "")
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        if outcome == "missing":
            raise FileNotFoundError("ffmpeg")
        if outcome == "no-frames":
            return RunOutput(0, "frame=0\nprogress=end\n", "")
        return RunOutput(
            4294967274,
            "frame=0\nprogress=end\n",
            f"[{encoder} @ 000001c6] Cannot load nvcuda.dll\n"
            f"[vost#0:0/{encoder} @ 0000] Could not open encoder before EOF\n"
            "Nothing was written into output file",
        )

    def encoders_run(self) -> list[str]:
        return [cmd[cmd.index("-c:v") + 1] for cmd in self.calls]


def _info(*, threads=4, ram_gb=8.0, gpus=()) -> HardwareInfo:
    return HardwareInfo(
        os_name="Windows",
        os_version="11",
        cpu_name="cpu",
        cpu_cores=max(1, threads // 2),
        cpu_threads=threads,
        total_ram_gb=ram_gb,
        gpus=list(gpus),
        recommended_threads=threads,
    )


def _compressor(monkeypatch, *, encoders=_ALL_AV1, threads=4, ram_gb=8.0, gpus=(), probe=None):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    detector = HardwareDetector()
    detector._info = _info(threads=threads, ram_gb=ram_gb, gpus=gpus)
    compressor = VideoCompressor(hw_detector=detector, av1_probe=probe)
    compressor.codec_manager._available_encoders = set(encoders)
    return compressor


def _probe(*passing: str, runner=None) -> HardwareAv1Probe:
    runner = runner or Runner({name: "ok" for name in passing})
    return HardwareAv1Probe("ffmpeg", runner=runner, cache_path=None)


def _video_info(path: Path, *, width=1920, height=1080, size=5_000_000) -> VideoInfo:
    return VideoInfo(
        filepath=path,
        duration=10.0,
        size=size,
        width=width,
        height=height,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=3_000_000,
        audio_bitrate=128_000,
        total_bitrate=3_128_000,
        frame_count=300,
    )


def _fast_av1(rung=QualityRung.BALANCED, **overrides) -> CompressionProfile:
    step = get_step(VideoCodec.AV1, rung)
    values = dict(
        name="Fast AV1",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.AV1,
        crf=step.crf,
        preset=step.preset,
        audio_codec=AudioCodec.OPUS,
        audio_bitrate=96_000,
        video_container="mkv",
        use_hw_accel=True,
    )
    values.update(overrides)
    return CompressionProfile(**values)


def _run(compressor, profile, tmp_path, outcomes, *, width=1920, height=1080, size=5_000_000, write_bytes=b"encoded"):
    """compress() with a fake FFmpeg. ``outcomes``: "ok" or a list of stderr lines."""

    monkey_cmds: list[list[str]] = []

    def fake_run(cmd, *, job, job_id, media_info, start_time, **kwargs):
        monkey_cmds.append(list(cmd))
        outcome = outcomes[min(len(monkey_cmds), len(outcomes)) - 1]
        if outcome == "ok":
            job.output_file.parent.mkdir(parents=True, exist_ok=True)
            job.output_file.write_bytes(write_bytes)
            return 0, []
        return 1, list(outcome)

    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    compressor._run_ffmpeg_process = fake_run
    compressor.analyze_media = lambda path: _video_info(path, width=width, height=height, size=size)
    result = compressor.compress(source, tmp_path / "out.mkv", profile, job_id="fast-av1")
    return monkey_cmds, result


def _flag(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _lp(cmd) -> int:
    params = dict(part.split("=", 1) for part in _flag(cmd, "-svtav1-params").split(":") if "=" in part)
    return int(params["lp"])


# ---------------------------------------------------------------------------
# The hardware check itself
# ---------------------------------------------------------------------------


def test_probe_command_is_a_tiny_silent_test_encode():
    cmd = probe_command("ffmpeg", "av1_amf")
    assert cmd[0] == "ffmpeg"
    assert "-nostdin" in cmd
    assert _flag(cmd, "-f") == "lavfi"
    # av1_amf rejects frames narrower than 320 px, so the clip is 640x360.
    assert _flag(cmd, "-i") == "testsrc2=size=640x360:rate=30"
    assert _flag(cmd, "-frames:v") == "8"
    assert _flag(cmd, "-c:v") == "av1_amf"
    assert cmd[-3:] == ["-f", "null", "-"]
    assert _flag(cmd, "-progress") == "pipe:1"


def test_probe_success_failure_no_frames_timeout_and_exception():
    runner = Runner(
        {
            "av1_amf": "ok",
            "av1_nvenc": "fail",
            "av1_qsv": "timeout",
            "av1_vulkan": "no-frames",
            "av1_mf": "missing",
        }
    )
    probe = HardwareAv1Probe("ffmpeg", runner=runner, cache_path=None, timeout=8.0)

    ok = probe.check("av1_amf")
    assert ok.ok is True and ok.reason == fast_av1.REASON_OK and ok.vendor == "amd"

    failed = probe.check("av1_nvenc")
    assert failed.ok is False and failed.reason == fast_av1.REASON_FAILED
    # The useful line, without FFmpeg's "[encoder @ address]" prefix.
    assert failed.detail == "Cannot load nvcuda.dll"

    timed_out = probe.check("av1_qsv")
    assert timed_out.ok is False and timed_out.reason == fast_av1.REASON_TIMEOUT
    assert "8 s" in timed_out.detail

    no_frames = probe.check("av1_vulkan")
    assert no_frames.ok is False and no_frames.reason == fast_av1.REASON_FAILED

    crashed = probe.check("av1_mf")
    assert crashed.ok is False and crashed.reason == fast_av1.REASON_ERROR

    # Every answer is kept for the session: nothing runs twice.
    for encoder in ("av1_amf", "av1_nvenc", "av1_qsv", "av1_vulkan", "av1_mf"):
        probe.check(encoder)
    assert len(runner.calls) == 5
    assert probe.peek("av1_amf").ok is True
    assert probe.peek("av1_hevc_nothing") is None

    probe.check("av1_amf", refresh=True)
    assert len(runner.calls) == 6


class _FakePopen:
    """Stands in for subprocess.Popen in the real runner."""

    instances: list = []
    hang = False

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.returncode = None
        self.killed = False
        self.waits: list = []
        _FakePopen.instances.append(self)

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        if _FakePopen.hang and not self.killed:
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        self.returncode = -9 if self.killed else 0
        return ("frame=8\n" if not self.killed else ""), ""

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@pytest.fixture
def fake_popen(monkeypatch):
    _FakePopen.instances = []
    _FakePopen.hang = False
    monkeypatch.setattr(fast_av1.subprocess, "Popen", _FakePopen)
    return _FakePopen


def test_default_runner_has_timeout_no_stdin_and_no_console_window(monkeypatch, fake_popen):
    monkeypatch.setattr(fast_av1.platform, "system", lambda: "Windows")
    output = fast_av1.subprocess_runner(["ffmpeg", "-version"], 8.0)

    assert output.returncode == 0 and output.stdout == "frame=8\n"
    proc = fake_popen.instances[0]
    assert proc.waits == [8.0]
    assert proc.kwargs["stdin"] is subprocess.DEVNULL
    assert proc.kwargs["stdout"] is subprocess.PIPE and proc.kwargs["stderr"] is subprocess.PIPE
    assert proc.kwargs["creationflags"] == int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # Finished runs are not kept for the exit-time kill.
    assert fast_av1._RUNNING == set()


def test_a_hung_probe_is_killed_on_timeout_and_at_exit(fake_popen):
    fake_popen.hang = True
    with pytest.raises(subprocess.TimeoutExpired):
        fast_av1.subprocess_runner(["ffmpeg"], 8.0)
    assert fake_popen.instances[0].killed is True
    assert fast_av1._RUNNING == set()

    # A probe still running when the app exits: atexit kills it.
    still_running = _FakePopen(["ffmpeg"])
    with fast_av1._RUNNING_LOCK:
        fast_av1._RUNNING.add(still_running)
    try:
        assert fast_av1.kill_running_probes() == 1
        assert still_running.killed is True
    finally:
        with fast_av1._RUNNING_LOCK:
            fast_av1._RUNNING.discard(still_running)


def test_concurrent_callers_share_one_probe():
    started = threading.Event()
    release = threading.Event()

    def slow_ok(cmd, timeout):
        started.set()
        release.wait(5)
        return RunOutput(0, "frame=8\n", "")

    runner = Runner({"av1_amf": slow_ok})
    probe = HardwareAv1Probe("ffmpeg", runner=runner, cache_path=None)
    results = []
    threads = [threading.Thread(target=lambda: results.append(probe.check("av1_amf"))) for _ in range(3)]
    for thread in threads:
        thread.start()
    assert started.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)

    assert [result.ok for result in results] == [True, True, True]
    assert len(runner.calls) == 1


def test_saved_results_skip_the_probe_until_driver_gpu_or_ffmpeg_changes(tmp_path):
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"build one")
    cache = tmp_path / "av1_hardware_check.json"
    signature = {"value": "amd:AMD Radeon RX 9070 XT@32.0.32015.2008|amd:AMD Radeon(TM) Graphics@32.0.21045"}
    now = {"t": 1_000_000.0}

    def make(runner):
        return HardwareAv1Probe(
            str(ffmpeg),
            runner=runner,
            cache_path=cache,
            gpu_signature=lambda: signature["value"],
            clock=lambda: now["t"],
        )

    first = Runner({"av1_amf": "ok"})
    probe = make(first)
    assert probe.first_working(["av1_nvenc", "av1_qsv", "av1_amf"]) == "av1_amf"
    assert first.encoders_run() == ["av1_nvenc", "av1_qsv", "av1_amf"]
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["version"] == 1 and len(saved["entries"]) == 3

    # A normal launch: same build, same drivers. Nothing runs.
    second = Runner(default="ok")
    relaunch = make(second)
    assert relaunch.first_working(["av1_nvenc", "av1_qsv", "av1_amf"]) == "av1_amf"
    assert second.calls == []
    assert relaunch.check("av1_amf").cached is True
    assert relaunch.check("av1_nvenc").ok is False

    # New graphics driver: probe again.
    signature["value"] = signature["value"].replace("2008", "2100")
    third = Runner({"av1_amf": "ok"})
    assert make(third).check("av1_amf").cached is False
    assert third.encoders_run() == ["av1_amf"]

    # FFmpeg replaced by another build: probe again.
    ffmpeg.write_bytes(b"build two is longer")
    fourth = Runner({"av1_amf": "ok"})
    make(fourth).check("av1_amf")
    assert fourth.encoders_run() == ["av1_amf"]

    # A pass is trusted for 30 days, a failure for 7.
    now["t"] += 8 * 24 * 3600
    fifth = Runner({"av1_amf": "ok"})
    later = make(fifth)
    assert later.check("av1_amf").cached is True
    later.check("av1_nvenc")
    assert fifth.encoders_run() == ["av1_nvenc"]


def test_timeouts_and_crashes_are_not_saved_and_a_bad_file_is_ignored(tmp_path):
    cache = tmp_path / "check.json"
    cache.write_text("{not json", encoding="utf-8")
    runner = Runner({"av1_qsv": "timeout", "av1_mf": "missing", "av1_amf": "ok"})
    probe = HardwareAv1Probe("ffmpeg", runner=runner, cache_path=cache)
    assert probe.check("av1_qsv").reason == fast_av1.REASON_TIMEOUT
    assert probe.check("av1_mf").reason == fast_av1.REASON_ERROR
    assert probe.check("av1_amf").ok is True

    entries = json.loads(cache.read_text(encoding="utf-8"))["entries"]
    assert [entry["encoder"] for entry in entries.values()] == ["av1_amf"]


def test_no_disk_writes_without_a_cache_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    probe = HardwareAv1Probe("ffmpeg", runner=Runner({"av1_amf": "ok"}), cache_path=None)
    probe.check("av1_amf")
    assert list(tmp_path.iterdir()) == []


def _write_entry(path: Path, probe: HardwareAv1Probe, encoder: str, **entry):
    record = {"encoder": encoder, "ok": True, "reason": "ok", "detail": "", "seconds": 0.2, "checked_at": 1_000_000.0}
    record.update(entry)
    path.write_text(json.dumps({"version": 1, "entries": {probe._key(encoder): record}}), encoding="utf-8")


@pytest.mark.parametrize(
    "bad",
    [
        {"seconds": "fast"},
        {"seconds": [1]},
        {"ok": "false"},
        {"ok": "true"},
        {"ok": 1},
        {"ok": False},  # contradicts reason "ok"
        {"reason": 5},
        {"reason": "timeout"},
        {"checked_at": "yesterday"},
        {"checked_at": None},
        {"checked_at": True},
        {"checked_at": float("nan")},
        {"checked_at": 1_000_000.0 + 3600},  # from the future (clock skew)
        {"detail": {"x": 1}},
        {"detail": None},
    ],
)
def test_a_corrupt_saved_entry_is_ignored_and_reprobed_never_raised(tmp_path, bad):
    cache = tmp_path / "check.json"
    runner = Runner({"av1_amf": "ok"})
    probe = HardwareAv1Probe(
        "ffmpeg", runner=runner, cache_path=cache, gpu_signature=lambda: "amd:X@1", clock=lambda: 1_000_000.0
    )
    _write_entry(cache, probe, "av1_amf", **bad)

    first = probe.check("av1_amf")
    assert first.ok is True and first.cached is False
    assert len(runner.calls) == 1
    # The fresh answer is kept in memory and replaces the bad entry on disk.
    assert probe.check("av1_amf") is first
    assert len(runner.calls) == 1
    entries = json.loads(cache.read_text(encoding="utf-8"))["entries"]
    assert list(entries.values())[0]["seconds"] >= 0


def test_a_well_formed_saved_failure_is_trusted(tmp_path):
    cache = tmp_path / "check.json"
    runner = Runner()
    probe = HardwareAv1Probe("ffmpeg", runner=runner, cache_path=cache, clock=lambda: 1_000_000.0 + 6 * 86400)
    _write_entry(cache, probe, "av1_nvenc", ok=False, reason="failed", detail="Cannot load nvcuda.dll")
    saved = probe.check("av1_nvenc")
    assert (saved.ok, saved.cached, saved.detail) == (False, True, "Cannot load nvcuda.dll")
    assert runner.calls == []
    # Older than the 7-day failure TTL: checked again.
    stale = HardwareAv1Probe("ffmpeg", runner=runner, cache_path=cache, clock=lambda: 1_000_000.0 + 8 * 86400)
    assert stale.check("av1_nvenc").cached is False
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    "payload",
    ['{"version": 1, "entries": []}', '{"version": 1, "entries": {"a": 5}}', "[1, 2]", '"x"', "{bad json", ""],
)
def test_a_broken_cache_file_never_blocks_the_in_memory_result(tmp_path, payload):
    cache = tmp_path / "check.json"
    cache.write_text(payload, encoding="utf-8")
    runner = Runner({"av1_amf": "ok"})
    probe = HardwareAv1Probe("ffmpeg", runner=runner, cache_path=cache)
    assert probe.first_working(["av1_amf"]) == "av1_amf"
    assert probe.peek("av1_amf").ok is True
    assert probe.first_working(["av1_amf"]) == "av1_amf"
    assert len(runner.calls) == 1


def test_a_failed_save_leaves_no_temp_file_and_keeps_the_answer(tmp_path, monkeypatch):
    cache = tmp_path / "check.json"
    runner = Runner({"av1_amf": "ok"})
    probe = HardwareAv1Probe("ffmpeg", runner=runner, cache_path=cache)

    def refuse(src, dst):
        raise PermissionError("the file is open in another process")

    monkeypatch.setattr(fast_av1.os, "replace", refuse)
    monkeypatch.setattr(fast_av1.time, "sleep", lambda seconds: None)
    assert probe.check("av1_amf").ok is True
    assert list(tmp_path.glob("*.tmp")) == []
    assert not cache.exists()
    assert probe.peek("av1_amf").ok is True
    assert probe.check("av1_amf").ok is True
    assert len(runner.calls) == 1


def test_a_busy_gpu_is_not_saved_so_the_next_launch_checks_again(tmp_path):
    cache = tmp_path / "check.json"

    def busy(cmd, timeout):
        return RunOutput(1, "frame=0\n", "[av1_nvenc @ 0] OpenEncodeSessionEx failed: out of memory (10)")

    probe = HardwareAv1Probe("ffmpeg", runner=Runner({"av1_nvenc": busy}), cache_path=cache)
    result = probe.check("av1_nvenc")
    assert (result.ok, result.reason) == (False, fast_av1.REASON_BUSY)
    assert "next launch" in result.describe()
    assert not cache.exists()
    relaunch = Runner({"av1_nvenc": "ok"})
    assert HardwareAv1Probe("ffmpeg", runner=relaunch, cache_path=cache).check("av1_nvenc").ok is True


def test_the_cache_path_is_looked_up_when_used_not_when_built(tmp_path, monkeypatch):
    """A probe built before the path was patched (a module-scoped fixture) still obeys it."""

    probe = HardwareAv1Probe("ffmpeg", runner=Runner({"av1_amf": "ok"}))
    assert probe.cache_path is None  # tests/conftest.py: no cache file in tests
    later = tmp_path / "later.json"
    monkeypatch.setattr(fast_av1, "default_cache_path", lambda: later)
    probe.check("av1_amf")
    assert later.exists()


def test_no_test_can_reach_the_real_cache_file():
    import conftest

    real = conftest.REAL_DEFAULT_CACHE_PATH()
    assert real is not None and real.name == fast_av1.CACHE_FILE_NAME
    # Patched for the whole session, including module-scoped fixtures.
    assert fast_av1.default_cache_path() is None
    assert fast_av1.default_runner is not fast_av1.subprocess_runner
    probe = HardwareAv1Probe("ffmpeg")
    assert probe.cache_path is None
    assert probe.check("av1_amf").ok is False


# ---------------------------------------------------------------------------
# Picking the hardware encoder
# ---------------------------------------------------------------------------


def _deus_gpus():
    return [
        GPUInfo("AMD Radeon RX 9070 XT", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True, "av1": True}, driver_version="32.0.32015.2008"),
        GPUInfo("AMD Radeon(TM) Graphics", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True, "av1": False}, driver_version="32.0.21045.11001"),
    ]


def test_igpu_plus_dgpu_uses_the_encoder_that_passed(monkeypatch):
    runner = Runner({"av1_amf": "ok"})
    compressor = _compressor(monkeypatch, gpus=_deus_gpus(), probe=None)
    compressor.av1_probe = HardwareAv1Probe(
        "ffmpeg", runner=runner, cache_path=None, gpu_signature=compressor._gpu_signature
    )

    assert compressor._select_hw_encoder(VideoCodec.AV1) == "av1_amf"
    assert runner.encoders_run() == ["av1_nvenc", "av1_qsv", "av1_amf"]
    # Both adapters and their drivers key the saved answer.
    signature = compressor._gpu_signature()
    assert "AMD Radeon RX 9070 XT@32.0.32015.2008" in signature
    assert "AMD Radeon(TM) Graphics@32.0.21045.11001" in signature
    # Later calls (every command build) do not probe again.
    compressor._select_hw_encoder(VideoCodec.AV1)
    assert len(runner.calls) == 3


def test_remote_desktop_answers_are_kept_apart_from_local_ones(monkeypatch):
    import video_compressor.core.compressor as compressor_module

    compressor = _compressor(monkeypatch, gpus=_deus_gpus(), probe=_probe())
    monkeypatch.setattr(compressor_module, "remote_session", lambda: False)
    local = compressor._gpu_signature()
    monkeypatch.setattr(compressor_module, "remote_session", lambda: True)
    remote = compressor._gpu_signature()
    assert remote == f"{local}|remote"
    monkeypatch.setattr(fast_av1.platform, "system", lambda: "Linux")
    assert fast_av1.remote_session() is False


def test_intel_igpu_and_old_nvidia_without_av1_encode_fall_back_to_svt(monkeypatch):
    gpus = [
        GPUInfo("Intel(R) UHD Graphics 770", GPUVendor.INTEL, encoder_support={"h264": True, "hevc": True, "av1": False}),
        GPUInfo("NVIDIA GeForce RTX 3060", GPUVendor.NVIDIA, encoder_support={"h264": True, "hevc": True, "av1": False}),
    ]
    compressor = _compressor(monkeypatch, gpus=gpus, probe=_probe())
    assert compressor._select_hw_encoder(VideoCodec.AV1) is None
    route = compressor.fast_av1_route(use_hw_accel=True)
    assert (route.kind, route.encoder) == ("software", "libsvtav1")


def test_remote_desktop_or_vm_without_a_gpu_still_checks_and_uses_the_cpu(monkeypatch):
    runner = Runner()
    compressor = _compressor(monkeypatch, gpus=(), probe=_probe(runner=runner))
    assert compressor._select_hw_encoder(VideoCodec.AV1) is None
    # Every listed encoder was tried once; none was trusted on its name.
    assert runner.encoders_run() == ["av1_nvenc", "av1_qsv", "av1_amf"]
    assert compressor.fast_av1_route().kind == "software"


def test_listed_but_failing_encoders_are_skipped_and_unlisted_ones_never_run(monkeypatch):
    runner = Runner({"av1_qsv": "ok", "av1_amf": "ok"})
    encoders = (_ALL_AV1 - {"av1_qsv"})
    compressor = _compressor(monkeypatch, encoders=encoders, probe=_probe(runner=runner))
    assert compressor._select_hw_encoder(VideoCodec.AV1) == "av1_amf"
    assert runner.encoders_run() == ["av1_nvenc", "av1_amf"]


def test_old_driver_answer_from_the_probe_wins_over_the_gpu_name(monkeypatch):
    """An RX 7900 XTX on a driver without AV1 encode: the name says yes, the check says no."""

    gpus = [GPUInfo("AMD Radeon RX 7900 XTX", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True, "av1": True})]
    compressor = _compressor(monkeypatch, gpus=gpus, probe=_probe())
    assert compressor._select_hw_encoder(VideoCodec.AV1) is None
    assert compressor.fast_av1_route().encoder == "libsvtav1"


def test_hevc_and_h264_lanes_do_not_run_the_av1_check(monkeypatch):
    runner = Runner(default="ok")
    gpus = [GPUInfo("AMD Radeon RX 9070 XT", GPUVendor.AMD, encoder_support={"h264": True, "hevc": True, "av1": True})]
    compressor = _compressor(monkeypatch, encoders=_ALL_AV1 | {"hevc_amf", "h264_amf"}, gpus=gpus, probe=_probe(runner=runner))
    assert compressor._select_hw_encoder(VideoCodec.HEVC) == "hevc_amf"
    assert compressor._select_hw_encoder(VideoCodec.H264) == "h264_amf"
    assert compressor._select_hw_encoder(VideoCodec.SVT_AV1) is None
    assert runner.calls == []


# ---------------------------------------------------------------------------
# SVT-AV1 speed and memory policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("threads", "width", "height", "floor"),
    [
        (1, 1920, 1080, 12),
        (2, 1920, 1080, 10),
        (4, 1920, 1080, 9),
        (8, 1920, 1080, 8),
        (32, 1920, 1080, 8),
        (2, 1280, 720, 9),
        (4, 3840, 2160, 12),
        (8, 3840, 2160, 10),
        (16, 3840, 2160, 9),
        (32, 3840, 2160, 8),
    ],
)
def test_svt_preset_floor_by_cpu_count_and_frame_size(threads, width, height, floor):
    assert svt_preset_floor(threads, width * height) == floor


def test_svt_preset_is_the_rung_preset_or_faster_never_slower():
    pixels_1080 = output_pixels(1920, 1080)
    assert fast_av1_svt_preset(10, 32, pixels_1080) == 10
    assert fast_av1_svt_preset(8, 32, pixels_1080) == 8
    assert fast_av1_svt_preset(8, 4, pixels_1080) == 9
    assert fast_av1_svt_preset(9, 2, pixels_1080) == 10
    # A custom slow preset is raised to the lane's slowest (preset 8).
    assert fast_av1_svt_preset(4, 64, pixels_1080) == 8
    assert fast_av1_svt_preset(13, 2, pixels_1080) == 13
    # max_resolution counts: a 4K source capped at 1080p is a 1080p encode.
    assert output_pixels(3840, 2160, 1080) == 1920 * 1080
    assert output_pixels(0, 0) == pixels_1080


def test_svt_lp_is_capped_only_for_big_frames_on_small_ram():
    k4 = 3840 * 2160
    hd = 1920 * 1080
    assert svt_lp_memory_cap(k4, 32.0) is None
    assert svt_lp_memory_cap(k4, 16.0) is None
    assert svt_lp_memory_cap(k4, 8.0) == 4
    assert svt_lp_memory_cap(k4, 4.0) == 1
    # README: the 4K cap stops at about 13 GB (a "16 GB" PC reports ~15.7 GB).
    assert svt_lp_memory_cap(k4, 13.0) == 5
    assert svt_lp_memory_cap(k4, 13.3) is None
    assert svt_lp_memory_cap(k4, 15.7) is None
    assert svt_lp_memory_cap(hd, 8.0) is None
    assert svt_lp_memory_cap(hd, 2.0) == 3
    assert svt_lp_memory_cap(hd, 0.0) is None


def test_aom_tiles_grow_with_the_frame():
    assert aom_tiles(3840, 2160) == "4x2"
    assert aom_tiles(1920, 1080) == "2x2"
    assert aom_tiles(1280, 720) == "2x2"
    assert aom_tiles(854, 480) == "2x1"
    assert aom_tiles(320, 240) is None
    assert aom_tiles(0, 0) == "2x2"


def test_one_fast_av1_cpu_job_gets_every_logical_cpu_with_enough_ram():
    def threads(**kwargs):
        params = dict(
            cpu_threads=4,
            cpu_cores=2,
            total_ram_gb=8.0,
            profile_threads=None,
            resource_governor="auto",
            codec="av1",
            hw_encoder=None,
            parallel_jobs=1,
            frame_height=1080,
        )
        params.update(kwargs)
        return threads_for_job(**params)

    assert threads() == 4
    assert threads(cpu_threads=32, cpu_cores=16) == 32
    # Low RAM, a wave, or an explicit Low governor keep the shared budget.
    assert threads(total_ram_gb=4.0) == 2
    assert threads(cpu_threads=8, cpu_cores=4, parallel_jobs=2) < 8
    assert threads(resource_governor="low") < 4
    # Hardware AV1 is not a CPU job.
    assert threads(hw_encoder="av1_amf") <= 4


# ---------------------------------------------------------------------------
# The encode commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rung", "threads", "preset"),
    [
        (QualityRung.QUICK, 32, "10"),
        (QualityRung.BALANCED, 32, "9"),
        (QualityRung.MAX, 32, "8"),
        (QualityRung.BALANCED, 4, "9"),
        (QualityRung.MAX, 4, "9"),
        (QualityRung.BALANCED, 2, "10"),
        (QualityRung.QUICK, 1, "12"),
    ],
)
def test_no_av1_hardware_runs_svt_at_a_preset_this_cpu_can_carry(monkeypatch, tmp_path, rung, threads, preset):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    compressor = _compressor(monkeypatch, threads=threads, ram_gb=16.0, probe=_probe())
    cmds, result = _run(compressor, _fast_av1(rung), tmp_path, ["ok"])

    assert result.success, result.error_message
    assert result.encoder_name == "libsvtav1"
    assert result.video_codec == "av1"
    cmd = cmds[0]
    assert _flag(cmd, "-c:v") == "libsvtav1"
    assert _flag(cmd, "-crf") == str(get_step(VideoCodec.AV1, rung).crf)
    assert _flag(cmd, "-preset") == preset
    assert _lp(cmd) == threads
    assert "-threads" not in cmd
    # The Fast lane does not take Max / Archival's D3D11VA decode path.
    assert "-hwaccel" not in cmd
    assert result.note == ""


def test_hardware_turned_off_never_runs_the_check(monkeypatch, tmp_path):
    runner = Runner(default="ok")
    compressor = _compressor(monkeypatch, threads=8, probe=_probe(runner=runner))
    cmds, result = _run(compressor, _fast_av1(use_hw_accel=False), tmp_path, ["ok"])
    assert result.success and result.encoder_name == "libsvtav1"
    assert runner.calls == []


def test_verified_amf_is_used_with_q_index_qps(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, threads=32, ram_gb=32.0, gpus=_deus_gpus(), probe=_probe("av1_amf"))
    cmds, result = _run(compressor, _fast_av1(QualityRung.BALANCED), tmp_path, ["ok"])
    assert result.encoder_name == "av1_amf"
    cmd = cmds[0]
    assert _flag(cmd, "-quality") == "balanced"
    assert _flag(cmd, "-qp_i") == "120"
    assert _flag(cmd, "-qp_p") == "120"
    assert "libsvtav1" not in cmd


def test_4k_on_8gb_ram_caps_svt_parallelism(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, threads=16, ram_gb=8.0, probe=_probe())
    cmds, result = _run(compressor, _fast_av1(), tmp_path, ["ok"], width=3840, height=2160)
    cmd = cmds[0]
    assert _flag(cmd, "-preset") == "9"
    assert _lp(cmd) == 4
    assert result.threads_used == 4

    roomy = _compressor(monkeypatch, threads=16, ram_gb=32.0, probe=_probe())
    cmds, _ = _run(roomy, _fast_av1(), tmp_path, ["ok"], width=3840, height=2160)
    assert _lp(cmds[0]) == 16


def test_no_svt_uses_libaom_realtime_with_tiles_and_threads(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, encoders=_ALL_AV1 - {"libsvtav1"}, threads=8, probe=_probe())
    cmds, result = _run(compressor, _fast_av1(QualityRung.MAX), tmp_path, ["ok"])

    assert result.success and result.encoder_name == "libaom-av1"
    assert result.video_codec == "av1"
    cmd = cmds[0]
    assert _flag(cmd, "-c:v") == "libaom-av1"
    assert _flag(cmd, "-crf") == "36"
    assert _flag(cmd, "-usage") == "realtime"
    assert _flag(cmd, "-cpu-used") == "8"
    assert _flag(cmd, "-row-mt") == "1"
    assert _flag(cmd, "-tiles") == "2x2"
    assert _flag(cmd, "-threads") == "8"
    assert "-lag-in-frames" not in cmd


def test_no_av1_encoder_at_all_runs_hevc_with_a_note(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, encoders={"libx265", "libx264", "aac", "libopus"}, probe=_probe())
    cmds, result = _run(compressor, _fast_av1(QualityRung.BALANCED), tmp_path, ["ok"])

    assert result.success, result.error_message
    assert result.video_codec == "hevc"
    assert result.encoder_name == "libx265"
    hevc = get_step(VideoCodec.HEVC, QualityRung.BALANCED)
    assert (_flag(cmds[0], "-crf"), _flag(cmds[0], "-preset")) == (str(hevc.crf), hevc.preset)
    assert result.note.startswith("This copy of FFmpeg has no AV1 encoder, so SquishIt used HEVC instead.")
    assert compressor.fast_av1_route().fallback_codec == "hevc"


def test_no_av1_and_no_hevc_runs_h264_with_a_note(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, encoders={"libx264", "aac", "libopus"}, probe=_probe())
    cmds, result = _run(compressor, _fast_av1(QualityRung.QUICK), tmp_path, ["ok"])
    assert result.video_codec == "h264"
    assert result.encoder_name == "libx264"
    assert "used H.264 instead" in result.note


def test_hardware_failure_without_a_size_target_retries_on_svt_without_asking(monkeypatch, tmp_path):
    asked = []
    compressor = _compressor(monkeypatch, threads=8, gpus=_deus_gpus(), probe=_probe("av1_amf"))
    compressor.set_software_fallback_callback(lambda request: asked.append(request) or False)
    cmds, result = _run(
        compressor,
        _fast_av1(QualityRung.QUICK),
        tmp_path,
        [["[av1_amf @ 0] encoder->Init() failed with error 5"], "ok"],
    )

    assert result.success, result.error_message
    assert asked == []
    assert [_flag(cmd, "-c:v") for cmd in cmds] == ["av1_amf", "libsvtav1"]
    assert result.encoder_name == "libsvtav1"
    assert result.attempt_count == 2
    assert "Fast AV1 used the processor (SVT-AV1) instead" in result.note


def test_target_size_hardware_failure_still_asks_and_the_retry_is_svt(monkeypatch, tmp_path):
    asked = []

    def decline(request):
        asked.append(request)
        return False

    compressor = _compressor(monkeypatch, threads=8, gpus=_deus_gpus(), probe=_probe("av1_amf"))
    compressor.set_software_fallback_callback(decline)
    profile = _fast_av1(target_size_mb=2, target_size_mode="balanced")
    cmds, result = _run(compressor, profile, tmp_path, [["[av1_amf @ 0] Init failed"]])

    assert len(asked) == 1
    assert asked[0].hw_encoder == "av1_amf"
    assert asked[0].software_encoder == "libsvtav1"
    assert result.success is False
    assert result.software_fallback_required is True
    assert [_flag(cmd, "-c:v") for cmd in cmds] == ["av1_amf"]

    confirmed = _compressor(monkeypatch, threads=8, gpus=_deus_gpus(), probe=_probe("av1_amf"))
    confirmed.set_software_fallback_callback(lambda request: True)
    cmds, result = _run(
        confirmed,
        profile,
        tmp_path,
        [["[av1_amf @ 0] Init failed"], "ok"],
        write_bytes=b"0" * 2_000_000,
    )
    assert result.success, result.error_message
    assert [_flag(cmd, "-c:v") for cmd in cmds][:2] == ["av1_amf", "libsvtav1"]
    assert result.encoder_name == "libsvtav1"


def test_option_error_retries_once_with_the_widest_supported_settings(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, threads=8, probe=_probe())
    cmds, result = _run(
        compressor,
        _fast_av1(QualityRung.QUICK, use_hw_accel=False),
        tmp_path,
        [["Svt[error]: Instance 1: preset 10 is not supported"], "ok"],
    )
    assert result.success
    assert [_flag(cmd, "-preset") for cmd in cmds] == ["10", "8"]

    aom = _compressor(monkeypatch, encoders=_ALL_AV1 - {"libsvtav1"}, threads=8, probe=_probe())
    cmds, result = _run(
        aom,
        _fast_av1(use_hw_accel=False),
        tmp_path,
        [["[libaom-av1 @ 0] Error applying encoder options: Result too large"], "ok"],
    )
    assert result.success
    assert [_flag(cmd, "-usage") for cmd in cmds] == ["realtime", "good"]


def _old_profile_file(directory: Path, name: str, crf, preset, **extra) -> Path:
    """A profile YAML the way 2.1.0 wrote it: no settings_version."""

    import yaml

    data = CompressionProfile(name=name, profile_type=ProfileType.CUSTOM, video_codec=VideoCodec.AV1,
                              crf=crf, preset=preset, use_hw_accel=False).to_dict()
    data.pop("settings_version")
    data.update(extra)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name.lower().replace(' ', '_')}.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("crf", "preset", "rung"),
    [(26, "8", QualityRung.QUICK), (30, "6", QualityRung.BALANCED), (34, "4", QualityRung.MAX)],
)
def test_old_saved_fast_av1_rungs_are_migrated_when_loaded(tmp_path, crf, preset, rung):
    from video_compressor.core.profiles import ProfileManager

    _old_profile_file(tmp_path / "profiles", "Old AV1", crf, preset)
    loaded = ProfileManager(config_dir=tmp_path / "profiles").get_profile("Old AV1")
    step = get_step(VideoCodec.AV1, rung)
    assert (loaded.crf, loaded.preset) == (step.crf, step.preset)
    assert matching_rung(VideoCodec.AV1, loaded.crf, loaded.preset) is rung


def test_new_custom_fast_av1_settings_are_never_rewritten(monkeypatch, tmp_path):
    """The old libaom pairs are fine new SVT-AV1 settings (the editor's default preset is 8)."""

    from video_compressor.core.profiles import ProfileManager

    for crf, preset in ((26, "8"), (30, "6"), (34, "4")):
        assert matching_rung(VideoCodec.AV1, crf, preset) is None

    # Saved by this version: kept as typed through save, reload, and a clone.
    manager = ProfileManager(config_dir=tmp_path / "profiles")
    mine = CompressionProfile(name="My AV1", profile_type=ProfileType.CUSTOM, video_codec=VideoCodec.AV1,
                              crf=26, preset="8", use_hw_accel=False)
    assert manager.save_profile(mine)
    reloaded = ProfileManager(config_dir=tmp_path / "profiles").get_profile("My AV1")
    assert (reloaded.crf, reloaded.preset) == (26, "8")
    clone = CompressionProfile.from_dict(reloaded.to_dict())
    assert (clone.crf, clone.preset) == (26, "8")
    # An old file whose numbers are not an old rung is left alone too.
    _old_profile_file(tmp_path / "old", "Odd", 27, "8")
    odd = ProfileManager(config_dir=tmp_path / "old").get_profile("Odd")
    assert (odd.crf, odd.preset) == (27, "8")

    # And the encode uses them as given (no rung CRF, no speed floor on a custom preset).
    compressor = _compressor(monkeypatch, threads=2, probe=_probe())
    for crf, preset in ((26, "8"), (30, "6"), (34, "4")):
        cmds, _ = _run(compressor, _fast_av1(crf=crf, preset=preset, use_hw_accel=False), tmp_path, ["ok"])
        assert (_flag(cmds[0], "-crf"), _flag(cmds[0], "-preset")) == (str(crf), preset)


def test_cli_explicit_fast_av1_crf_is_used_as_given(monkeypatch, tmp_path):
    """``--profile max --codec av1 --crf 26`` is CRF 26 at the Max preset 8, the old Quick pair."""

    from cli import create_parser, get_profile
    from video_compressor.core.profiles import ProfileManager, profile_video_ffmpeg_args

    args = create_parser().parse_args(["clip.mp4", "--profile", "max", "--codec", "av1", "--crf", "26", "--no-hw-accel"])
    profile = get_profile(args, manager=ProfileManager(config_dir=tmp_path / "profiles"))
    assert (profile.crf, profile.preset) == (26, "8")
    video = profile_video_ffmpeg_args(profile)
    assert (_flag(video, "-crf"), _flag(video, "-preset")) == ("26", "8")
    compressor = _compressor(monkeypatch, threads=8, probe=_probe())
    cmds, _ = _run(compressor, profile, tmp_path, ["ok"])
    assert (_flag(cmds[0], "-crf"), _flag(cmds[0], "-preset")) == ("26", "8")


def test_qt_profile_import_migrates_only_old_files(tmp_path):
    pytest.importorskip("PySide6")
    from video_compressor.gui.qt_shell.profile_editor import ProfileEditorDialog

    old = _old_profile_file(tmp_path, "Old import", 30, "6")
    new = tmp_path / "new.yaml"
    import yaml

    new.write_text(
        yaml.safe_dump(
            CompressionProfile(name="New import", profile_type=ProfileType.CUSTOM, video_codec=VideoCodec.AV1,
                               crf=30, preset="6").to_dict()
        ),
        encoding="utf-8",
    )
    read = ProfileEditorDialog._read_profile_file
    old_profile = read(None, old)
    new_profile = read(None, new)
    assert (old_profile.crf, old_profile.preset) == (33, "9")
    assert (new_profile.crf, new_profile.preset) == (30, "6")


def test_custom_preset_is_used_as_given_and_rungs_keep_the_speed_floor(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, threads=4, probe=_probe())
    cmds, _ = _run(compressor, _fast_av1(crf=28, preset="5", use_hw_accel=False), tmp_path, ["ok"])
    assert (_flag(cmds[0], "-crf"), _flag(cmds[0], "-preset")) == ("28", "5")
    # A size target clears the CRF, but the rung's floor still applies (Max p8 -> p9 on 4 CPUs).
    cmds, _ = _run(
        compressor,
        _fast_av1(QualityRung.MAX, use_hw_accel=False, target_size_mb=2, target_size_mode="balanced"),
        tmp_path,
        ["ok"],
        write_bytes=b"0" * 2_000_000,
    )
    assert _flag(cmds[0], "-preset") == "9"
    assert "-crf" not in cmds[0]


def test_parallel_jobs_pick_the_preset_from_each_jobs_share(monkeypatch, tmp_path):
    """Two jobs on 8 CPUs get about 2 threads each, so Balanced runs preset 10, not 9."""

    compressor = _compressor(monkeypatch, threads=8, ram_gb=16.0, probe=_probe())
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    cmds = []

    def fake_run(cmd, *, job, **kwargs):
        cmds.append(list(cmd))
        job.output_file.write_bytes(b"encoded")
        return 0, []

    compressor._run_ffmpeg_process = fake_run
    compressor.analyze_media = lambda path: _video_info(path)
    result = compressor.compress(source, tmp_path / "a.mkv", _fast_av1(use_hw_accel=False), job_id="a", parallel_jobs=2)
    assert result.success
    assert _lp(cmds[0]) == 2
    assert _flag(cmds[0], "-preset") == "10"


def test_max_archival_never_runs_the_av1_check(monkeypatch, tmp_path):
    runner = Runner(default="ok")
    compressor = _compressor(monkeypatch, threads=16, ram_gb=32.0, gpus=_deus_gpus(), probe=_probe(runner=runner))
    archival = _fast_av1(
        video_codec=VideoCodec.SVT_AV1,
        crf=get_step(VideoCodec.SVT_AV1, QualityRung.MAX).crf,
        preset=get_step(VideoCodec.SVT_AV1, QualityRung.MAX).preset,
        use_hw_accel=True,
        resource_governor="max",
    )
    cmds, result = _run(compressor, archival, tmp_path, ["ok"])
    assert result.encoder_name == "libsvtav1"
    assert _flag(cmds[0], "-preset") == "6"
    assert runner.calls == []


def test_amf_av1_cq_is_a_q_index_and_hevc_amf_is_unchanged():
    def amf(encoder, codec, crf):
        settings = CodecSettings(video_codec=codec, crf=crf, preset="balanced", disable_audio=True)
        return settings.to_ffmpeg_args(hw_encoder=encoder)

    av1 = amf("av1_amf", VideoCodec.AV1, 30)
    assert (_flag(av1, "-qp_i"), _flag(av1, "-qp_p")) == ("120", "120")
    assert "-qp_b" not in av1
    assert _flag(amf("av1_amf", VideoCodec.AV1, 26), "-qp_i") == "104"
    assert _flag(amf("av1_amf", VideoCodec.AV1, 70), "-qp_i") == "255"
    hevc = amf("hevc_amf", VideoCodec.HEVC, 28)
    assert (_flag(hevc, "-qp_i"), _flag(hevc, "-qp_p")) == ("28", "30")
    # Bitrate modes (target size) are untouched.
    vbr = CodecSettings(video_codec=VideoCodec.AV1, video_bitrate=2_000_000, rate_control=RateControl.VBR)
    assert _flag(vbr.to_ffmpeg_args(hw_encoder="av1_amf"), "-rc") == "vbr_peak"


def test_ladder_rungs_for_fast_av1():
    quick, balanced, maximum = (get_step(VideoCodec.AV1, rung) for rung in QualityRung)
    assert quick.crf < balanced.crf < maximum.crf
    assert int(quick.preset) > int(balanced.preset) > int(maximum.preset)
    # Hardware CQ is unchanged; av1_amf turns it into a q-index.
    assert (quick.hw_cq, balanced.hw_cq, maximum.hw_cq) == (26, 30, 34)
    assert all(not step.force_software for step in (quick, balanced, maximum))
    assert VideoCodec.AV1.software_encoders == ("libsvtav1", "libaom-av1")
    assert VideoCodec.AV1.ffmpeg_encoder == "libsvtav1"


# ---------------------------------------------------------------------------
# What the window and CLI say
# ---------------------------------------------------------------------------


def _status(probe, *, use_hw=True, software_name="libsvtav1"):
    return describe_hw_status(
        probe,
        codec=VideoCodec.AV1,
        use_hw=use_hw,
        force_software=False,
        action="Quick Compress",
        software_name=software_name,
    )


def test_hardware_card_says_what_fast_av1_will_do():
    amd = (FoundHardware("AMF", "AMD Radeon RX 9070 XT"),)
    cases = {
        "amf": _status(HardwareProbe(found=amd, encoders={"av1": "av1_amf"}, av1_software="libsvtav1")),
        "nvenc": _status(HardwareProbe(encoders={"av1": "av1_nvenc"}, av1_software="libsvtav1")),
        "qsv": _status(HardwareProbe(encoders={"av1": "av1_qsv"}, av1_software="libsvtav1")),
        "cpu": _status(HardwareProbe(found=amd, encoders={"av1": None}, av1_software="libsvtav1")),
        "nogpu": _status(HardwareProbe(encoders={"av1": None}, av1_software="libsvtav1")),
        "two": _status(
            HardwareProbe(
                found=(FoundHardware("NVENC", "RTX 3060"), FoundHardware("QSV", "UHD 770")),
                encoders={"av1": None},
                av1_software="libsvtav1",
            )
        ),
        "off": _status(HardwareProbe(found=amd, encoders={"av1": "av1_amf"}, av1_software="libsvtav1"), use_hw=False),
        "aom": _status(HardwareProbe(encoders={"av1": None}, av1_software="libaom-av1")),
        "none": _status(HardwareProbe(encoders={"av1": None}, av1_software="", av1_fallback="hevc")),
        "none264": _status(HardwareProbe(encoders={"av1": None}, av1_software="", av1_fallback="h264")),
    }
    assert cases["amf"].using == "Fast AV1 will use your AMD graphics card."
    assert (cases["amf"].badge, cases["amf"].state, cases["amf"].tooltip) == ("AMF", "ready", "av1_amf")
    assert cases["nvenc"].using == "Fast AV1 will use your NVIDIA graphics card."
    assert cases["qsv"].using == "Fast AV1 will use your Intel graphics."
    # The card is listed as found, so the sentence says it failed the AV1 test
    # rather than claiming no hardware was found.
    assert cases["cpu"].found == "Found AMD Radeon RX 9070 XT (AMF)."
    assert cases["cpu"].using == (
        "Your AMD graphics card did not pass the AV1 test, so Fast AV1 will use your processor (SVT-AV1)."
    )
    assert cases["cpu"].state == "software"
    assert cases["nogpu"].using == (
        "No AV1 graphics hardware was found, so Fast AV1 will use your processor (SVT-AV1)."
    )
    assert cases["two"].using.startswith("Your graphics hardware did not pass the AV1 test")
    assert cases["off"].using == (
        "Hardware encoding is turned off, so Fast AV1 will use your processor (SVT-AV1)."
    )
    assert "libaom, which is slower than SVT-AV1" in cases["aom"].using
    assert cases["none"].using == "This copy of FFmpeg has no AV1 encoder; HEVC will be used instead."
    assert "H.264 will be used instead" in cases["none264"].using
    for status in cases.values():
        words = set(f"{status.found} {status.using}".lower().replace(".", " ").split())
        assert not words & set(_FORBIDDEN_WORDS), status


def test_settings_hint_names_the_cpu_encoder_this_ffmpeg_really_has():
    from video_compressor.gui.qt_shell.encode_form import EncodeForm

    form = EncodeForm()
    form.set_codec(VideoCodec.AV1)
    form.use_hw_accel = False
    assert form.hw_hint() == "Hardware encoding is off, so Fast AV1 will use your processor."
    form.av1_software = "libsvtav1"
    assert form.hw_hint() == "Hardware encoding is off, so Fast AV1 will use your processor (SVT-AV1)."
    form.av1_software = "libaom-av1"
    assert "libaom, which is slower than SVT-AV1" in form.hw_hint()
    form.use_hw_accel = True
    assert "otherwise your processor (libaom, which is slower than SVT-AV1)" in form.hw_hint()
    form.av1_software, form.av1_fallback = "", "hevc"
    assert form.hw_hint() == "This copy of FFmpeg has no AV1 encoder; HEVC will be used instead."
    form.av1_fallback = "h264"
    assert "H.264 will be used instead" in form.hw_hint()


def test_av1_playback_hint_names_players():
    for name in ("VLC", "Edge", "Chrome", "AV1 Video Extension"):
        assert name in AV1_PLAYBACK_HINT
    assert not any(word in AV1_PLAYBACK_HINT.lower() for word in _FORBIDDEN_WORDS)


def test_home_probe_reports_the_verified_encoder_and_the_cpu_route(monkeypatch):
    compressor = _compressor(monkeypatch, gpus=_deus_gpus(), probe=_probe("av1_amf"))
    probe = probe_from_compressor(compressor)
    assert probe.encoder_for(VideoCodec.AV1) == "av1_amf"
    assert probe.av1_software == "libsvtav1"
    assert _status(probe).using == "Fast AV1 will use your AMD graphics card."

    bare = _compressor(monkeypatch, encoders={"libx265", "aac"}, probe=_probe())
    probe = probe_from_compressor(bare)
    assert probe.encoder_for(VideoCodec.AV1) is None
    assert (probe.av1_software, probe.av1_fallback) == ("", "hevc")


def test_cli_hardware_prints_each_av1_check_and_the_route(monkeypatch, capsys):
    from cli import describe_fast_av1_route, print_fast_av1_check

    runner = Runner({"av1_amf": "ok"})
    compressor = _compressor(
        monkeypatch,
        encoders=_ALL_AV1 - {"av1_qsv"},
        gpus=_deus_gpus(),
        probe=_probe(runner=runner),
    )
    compressor._select_hw_encoder(VideoCodec.AV1)
    calls_before = len(runner.calls)
    print_fast_av1_check(compressor)
    out = capsys.readouterr().out

    assert "av1_nvenc  no" in out and "Cannot load nvcuda.dll" in out
    assert "av1_qsv    no   not in this FFmpeg" in out
    assert "av1_amf    yes  works" in out
    assert "Fast AV1 will use: av1_amf (AMD graphics card)" in out
    # --hardware probes fresh instead of trusting the saved answer.
    assert len(runner.calls) == calls_before + 2

    assert "SVT-AV1" in describe_fast_av1_route(fast_av1.FastAv1Route("software", "libsvtav1"))
    assert "no SVT-AV1" in describe_fast_av1_route(fast_av1.FastAv1Route("software", "libaom-av1"))
    assert describe_fast_av1_route(fast_av1.FastAv1Route("fallback", "", fallback_codec="hevc")) == (
        "HEVC (this FFmpeg has no AV1 encoder)"
    )


@pytest.fixture
def quiet_config(monkeypatch):
    monkeypatch.setattr("video_compressor.config.ConfigManager.update_config", lambda self, **kwargs: None)
    monkeypatch.setattr("video_compressor.config.ConfigManager.save_config", lambda self: None)


def test_main_window_shows_fast_av1_route_and_playback_hint(monkeypatch, quiet_config):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from video_compressor.gui.qt_shell.main_window import SquishItWindow

    monkeypatch.setattr(SquishItWindow, "_start_hw_probe", lambda self: None)
    app = QApplication.instance() or QApplication([])
    window = SquishItWindow()
    try:
        window.show()
        app.processEvents()
        window.apply_hardware_probe(
            HardwareProbe(
                found=(FoundHardware("AMF", "AMD Radeon RX 9070 XT"),),
                encoders={"av1": None, "hevc": "hevc_amf", "h264": "h264_amf"},
                av1_software="libsvtav1",
            )
        )
        window.form.set_codec(VideoCodec.AV1)
        window._settings.load(window.form)
        window._refresh_hw_status()
        app.processEvents()
        assert window._hw_card.using.text() == (
            "Your AMD graphics card did not pass the AV1 test, so Fast AV1 will use your processor (SVT-AV1)."
        )
        assert AV1_PLAYBACK_HINT in window._settings._hw_hint.text()
        assert window._settings._hw.text() == "Use the graphics card for AV1 when it can"

        window.form.set_codec(VideoCodec.HEVC)
        window._settings.load(window.form)
        window._refresh_hw_status()
        assert AV1_PLAYBACK_HINT not in window._settings._hw_hint.text()
    finally:
        window.close()
        app.processEvents()


# ---------------------------------------------------------------------------
# The UI thread never runs or waits for the hardware check
# ---------------------------------------------------------------------------


class _GatedRunner(Runner):
    """A probe that blocks until released, recording which thread ran it."""

    def __init__(self, passing=("av1_amf",)):
        super().__init__({name: "ok" for name in passing})
        self.started = threading.Event()
        self.release = threading.Event()
        self.threads: list = []

    def __call__(self, cmd, timeout):
        self.threads.append(threading.current_thread())
        self.started.set()
        assert self.release.wait(10), "the gated probe was never released"
        return super().__call__(cmd, timeout)


def _exact_av1(mb=20):
    return _fast_av1(target_size_mb=mb, target_size_mode="exact")


def _ui_calls(compressor):
    """Everything the window does with the compressor on its UI thread."""

    from types import SimpleNamespace

    from video_compressor.gui.qt_shell.main_window import SquishItWindow

    info = _video_info(Path("x.mp4"), size=200_000_000)
    shown = []
    fake_window = SimpleNamespace(
        _compressor=compressor,
        _settings=SimpleNamespace(set_size_preview=shown.append),
        _first_video_item=lambda: {"info": info},
    )
    started = time.perf_counter()
    SquishItWindow._refresh_size_preview(fake_window, _exact_av1())
    plan = compressor._resolve_target_size_plan(_exact_av1(), info)
    slots = compressor.queue_slots(_fast_av1(), requested_parallel=4, queued_files=4, media_info=info)
    route = compressor.fast_av1_route()
    chosen = compressor._select_hw_encoder(VideoCodec.AV1)
    return time.perf_counter() - started, chosen, slots, route, plan, shown


def test_ui_thread_calls_return_at_once_while_the_startup_check_runs(monkeypatch):
    pytest.importorskip("PySide6")
    runner = _GatedRunner()
    compressor = _compressor(monkeypatch, threads=16, ram_gb=32.0, gpus=_deus_gpus(), probe=_probe(runner=runner))
    compressor.ui_thread = threading.current_thread()

    # Nothing checked yet and nothing running: the UI thread does not start a probe.
    elapsed, chosen, slots, route, plan, shown = _ui_calls(compressor)
    assert elapsed < 2.0 and runner.calls == []
    assert chosen is None and route.kind == "software"

    # The window's hardware-check worker starts the probe; the UI thread keeps going.
    worker = threading.Thread(target=lambda: probe_from_compressor(compressor), daemon=True)
    worker.start()
    assert runner.started.wait(5)
    elapsed, chosen, slots, route, plan, shown = _ui_calls(compressor)
    assert elapsed < 2.0, f"the UI thread waited {elapsed:.1f} s for the AV1 check"
    assert chosen is None and route.kind == "software"
    assert shown and "unavailable" not in shown[-1]
    assert slots == 4  # counted as CPU jobs until the GPU is verified

    runner.release.set()
    worker.join(10)
    assert not worker.is_alive()
    assert all(thread is not compressor.ui_thread for thread in runner.threads)
    # Once the worker has the answer, the UI thread sees the verified GPU.
    assert compressor._select_hw_encoder(VideoCodec.AV1) == "av1_amf"
    assert compressor.fast_av1_route().encoder == "av1_amf"
    assert compressor.queue_slots(_fast_av1(), requested_parallel=4, queued_files=4) == 2  # AMF sessions


def test_worker_threads_still_check_and_wait_when_a_ui_thread_is_set(monkeypatch):
    runner = _GatedRunner()
    runner.release.set()
    compressor = _compressor(monkeypatch, gpus=_deus_gpus(), probe=_probe(runner=runner))
    compressor.ui_thread = threading.Thread(target=lambda: None)  # some other thread is the UI
    assert compressor._select_hw_encoder(VideoCodec.AV1) == "av1_amf"
    assert runner.encoders_run() == ["av1_nvenc", "av1_qsv", "av1_amf"]


def test_first_verified_keeps_the_preference_order():
    probe = _probe("av1_qsv", "av1_amf")
    assert probe.first_verified(["av1_nvenc", "av1_qsv", "av1_amf"]) is None  # nothing known yet
    probe.check("av1_amf")
    assert probe.first_verified(["av1_nvenc", "av1_qsv", "av1_amf"]) is None  # NVENC/QSV unknown
    probe.check("av1_nvenc")
    probe.check("av1_qsv")
    assert probe.first_verified(["av1_nvenc", "av1_qsv", "av1_amf"]) == "av1_qsv"


def test_window_redraws_the_card_and_the_size_preview_when_the_check_finishes(monkeypatch, quiet_config):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from video_compressor.gui.qt_shell.main_window import SquishItWindow

    monkeypatch.setattr(SquishItWindow, "_start_hw_probe", lambda self: None)
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    previews = []
    real_preview = SquishItWindow._refresh_size_preview
    monkeypatch.setattr(
        SquishItWindow,
        "_refresh_size_preview",
        lambda self, profile: (previews.append(profile.video_codec), real_preview(self, profile)),
    )
    app = QApplication.instance() or QApplication([])
    window = SquishItWindow()
    try:
        assert window._get_compressor().ui_thread is window._ui_thread
        window.form.set_codec(VideoCodec.AV1)
        previews.clear()
        window._handle(
            (
                "hw_probe",
                HardwareProbe(encoders={"av1": "av1_amf"}, av1_software="libaom-av1"),
            )
        )
        app.processEvents()
        assert previews, "the size preview was not redrawn after the hardware check"
        assert window._hw_card.using.text() == "Fast AV1 will use your AMD graphics card."
        assert window.form.av1_software == "libaom-av1"
    finally:
        window.close()
        app.processEvents()


# ---------------------------------------------------------------------------
# The oversized-output retry is for real hardware encodes only
# ---------------------------------------------------------------------------


def test_an_oversized_cpu_encode_is_not_retried_as_if_it_were_hardware(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, threads=8, probe=_probe())  # no GPU passes
    cmds, result = _run(compressor, _fast_av1(use_hw_accel=True), tmp_path, ["ok"], write_bytes=b"0" * 6_000_000)
    assert [_flag(cmd, "-c:v") for cmd in cmds] == ["libsvtav1"]
    assert _flag(cmds[0], "-crf") == "33"
    assert result.kept_original is True
    assert result.attempt_count == 1


def test_an_oversized_hardware_encode_still_retries_on_the_cpu(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch, threads=8, gpus=_deus_gpus(), probe=_probe("av1_amf"))
    sizes = iter([6_000_000, 1_000])
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    cmds = []

    def fake_run(cmd, *, job, **kwargs):
        cmds.append(list(cmd))
        job.output_file.write_bytes(b"0" * next(sizes))
        return 0, []

    compressor._run_ffmpeg_process = fake_run
    compressor.analyze_media = lambda path: _video_info(path)
    result = compressor.compress(source, tmp_path / "out.mkv", _fast_av1(), job_id="big")
    assert [_flag(cmd, "-c:v") for cmd in cmds] == ["av1_amf", "libsvtav1"]
    assert result.encoder_name == "libsvtav1"


def test_the_main_thread_never_waits_for_another_threads_check(monkeypatch):
    """No ui_thread set (a script, the CLI): the main thread still skips a probe in flight."""

    runner = _GatedRunner()
    compressor = _compressor(monkeypatch, gpus=_deus_gpus(), probe=_probe(runner=runner))
    assert threading.current_thread() is threading.main_thread()
    worker = threading.Thread(target=lambda: compressor._select_hw_encoder(VideoCodec.AV1), daemon=True)
    worker.start()
    assert runner.started.wait(5)
    started = time.perf_counter()
    assert compressor._select_hw_encoder(VideoCodec.AV1) is None
    plan = compressor._resolve_target_size_plan(_exact_av1(), _video_info(Path("x.mp4"), size=200_000_000))
    assert plan is not None
    assert time.perf_counter() - started < 2.0
    runner.release.set()
    worker.join(10)
    # Nothing in flight any more: the main thread may check (and here reads the answer).
    assert compressor._select_hw_encoder(VideoCodec.AV1) == "av1_amf"
