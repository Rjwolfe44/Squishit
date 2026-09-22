"""Budget math, thread argv, and encode concurrency caps."""

import threading
import time
from pathlib import Path

import pytest

from video_compressor.core.codecs import AudioCodec, VideoCodec
from video_compressor.core.compressor import BatchProcessor, CompressionJob, CompressionResult, VideoCompressor, VideoInfo
from video_compressor.core.governor import (
    EncodeCancelled,
    EncodeResourceGovernor,
    HW_SESSION_LIMITS,
    MachineResources,
    apply_thread_policy,
    threads_for_job,
)
from video_compressor.core.hardware import GPUInfo, GPUVendor, HardwareInfo
from video_compressor.core.profiles import (
    ARCHIVAL_PROFILE_NAME,
    CompressionProfile,
    ProfileManager,
    ProfileType,
    build_quick_compress_profiles,
)


MACHINE = MachineResources(cpu_threads=16, cpu_cores=8, total_ram_gb=32.0)


def _threads(**overrides) -> int:
    params = dict(
        cpu_threads=MACHINE.cpu_threads,
        cpu_cores=MACHINE.cpu_cores,
        total_ram_gb=MACHINE.total_ram_gb,
        profile_threads=None,
        resource_governor="auto",
        codec="hevc",
        hw_encoder=None,
        parallel_jobs=1,
        frame_height=1080,
    )
    params.update(overrides)
    return threads_for_job(**params)


def _video_info(path: Path) -> VideoInfo:
    return VideoInfo(
        filepath=path,
        duration=10.0,
        size=5_000_000,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=3_000_000,
        audio_bitrate=128_000,
        total_bitrate=3_128_000,
        frame_count=300,
    )


def _compressor(monkeypatch) -> VideoCompressor:
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    compressor = VideoCompressor()
    compressor.hw_detector._info = HardwareInfo(
        os_name="Linux",
        os_version="test",
        cpu_name="cpu",
        cpu_cores=8,
        cpu_threads=16,
        total_ram_gb=32.0,
        recommended_threads=14,
    )
    return compressor


def _job(tmp_path: Path, profile: CompressionProfile, *, parallel_jobs: int, name: str) -> CompressionJob:
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    job = CompressionJob(
        id=name,
        input_file=source,
        output_file=tmp_path / f"{name}.mkv",
        profile=profile,
        parallel_jobs=parallel_jobs,
    )
    job.video_info = _video_info(source)
    return job


def test_parallel_wave_splits_one_cpu_budget():
    alone = _threads(codec="svt-av1", parallel_jobs=1)
    shared = _threads(codec="svt-av1", parallel_jobs=4)
    assert alone > shared
    assert shared * 4 <= MACHINE.cpu_threads

    hevc_shares = [_threads(codec="hevc", parallel_jobs=4) for _ in range(4)]
    assert len(set(hevc_shares)) == 1
    assert sum(hevc_shares) <= MACHINE.cpu_threads
    assert hevc_shares[0] < _threads(codec="hevc", parallel_jobs=1)


def test_explicit_threads_are_divided_across_the_wave_and_clamped_for_x265():
    assert _threads(codec="hevc", profile_threads=32, parallel_jobs=1) == 16
    divided = _threads(codec="hevc", profile_threads=32, parallel_jobs=4)
    assert divided == MACHINE.cpu_threads // 4
    assert divided * 4 <= MACHINE.cpu_threads


def test_session_caps_follow_encoder_family_and_cpu():
    wide = EncodeResourceGovernor(MACHINE)
    assert wide.max_concurrent(8, hw_encoder="h264_nvenc") == HW_SESSION_LIMITS["nvenc"]
    assert wide.max_concurrent(8, hw_encoder="hevc_qsv") == HW_SESSION_LIMITS["qsv"]
    assert wide.max_concurrent(8, hw_encoder="hevc_amf") == HW_SESSION_LIMITS["amf"]
    assert wide.max_concurrent(8, hw_encoder="h264_videotoolbox") == HW_SESSION_LIMITS["videotoolbox"]
    assert wide.max_concurrent(8, hw_encoder=None) == MACHINE.cpu_threads // 2
    assert wide.max_concurrent(8, hw_encoder="h264_nvenc", queued=1) == 1

    small = EncodeResourceGovernor(MachineResources(cpu_threads=4, cpu_cores=2, total_ram_gb=8.0))
    assert small.session_limit("h264_nvenc") == 2
    assert small.max_concurrent(8, hw_encoder=None) == 2


def test_nvenc_reserve_holds_the_fourth_job_until_a_slot_frees():
    governor = EncodeResourceGovernor(MACHINE)
    started = []
    lock = threading.Lock()
    release = threading.Event()

    def run(index: int) -> None:
        governor.reserve(
            f"job-{index}",
            declared_parallel=HW_SESSION_LIMITS["nvenc"],
            hw_encoder="h264_nvenc",
            codec="h264",
            resource_governor="max",
        )
        with lock:
            started.append(index)
        release.wait(timeout=2)
        governor.release(f"job-{index}")

    threads = [threading.Thread(target=run, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()

    deadline = time.time() + 2
    while time.time() < deadline and len(started) < HW_SESSION_LIMITS["nvenc"]:
        time.sleep(0.02)
    time.sleep(0.2)
    assert len(started) == HW_SESSION_LIMITS["nvenc"]
    with governor._cv:
        leases = list(governor._leases.values())
    assert len(leases) == HW_SESSION_LIMITS["nvenc"]
    assert sum(lease.threads for lease in leases) <= MACHINE.cpu_threads

    release.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert governor.active_count() == 0


def test_reserve_cancel_does_not_keep_the_slot():
    governor = EncodeResourceGovernor(MachineResources(cpu_threads=2, cpu_cores=1, total_ram_gb=8.0))
    governor.reserve("holder", declared_parallel=1, hw_encoder=None, codec="hevc")
    cancelled = {"value": False}

    def wait_for_slot() -> None:
        with pytest.raises(EncodeCancelled):
            governor.reserve(
                "waiting",
                declared_parallel=1,
                hw_encoder=None,
                codec="hevc",
                cancel_check=lambda: cancelled["value"],
            )

    thread = threading.Thread(target=wait_for_slot)
    thread.start()
    time.sleep(0.1)
    cancelled["value"] = True
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert governor.holds("holder")
    assert not governor.holds("waiting")
    governor.release("holder")


def test_apply_thread_policy_collapses_conflicting_encoder_flags():
    x265 = apply_thread_policy(
        [
            "ffmpeg",
            "-c:v",
            "libx265",
            "-x265-params",
            "pools=16:frame-threads=4:lossless=1",
            "-threads",
            "8",
            "-threads",
            "4",
            "out.mkv",
        ],
        threads=6,
        encoder="libx265",
    )
    assert x265.count("-threads") == 1
    assert x265[x265.index("-threads") + 1] == "6"
    assert "pools" not in " ".join(x265)
    assert "frame-threads" not in " ".join(x265)
    assert "lossless=1" in x265

    x264 = apply_thread_policy(
        [
            "ffmpeg",
            "-c:v",
            "libx264",
            "-x264-params",
            "threads=12:lookahead-threads=4:rc-lookahead=60",
            "out.mp4",
        ],
        threads=4,
        encoder="libx264",
    )
    assert x264.count("-threads") == 1
    assert x264[x264.index("-threads") + 1] == "4"
    params = x264[x264.index("-x264-params") + 1]
    assert params == "rc-lookahead=60"
    assert "lookahead-threads" not in params

    svt = apply_thread_policy(
        [
            "ffmpeg",
            "-c:v",
            "libsvtav1",
            "-svtav1-params",
            "film-grain=8:lp=2",
            "-svtav1-params",
            "lp=9",
            "-threads",
            "8",
            "out.mkv",
        ],
        threads=4,
        encoder="libsvtav1",
    )
    assert svt.count("-threads") == 0
    assert svt.count("-svtav1-params") == 1
    assert svt[svt.index("-svtav1-params") + 1] == "film-grain=8:lp=4"


def test_compressor_argv_has_one_thread_policy(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    hevc = CompressionProfile(
        name="HEVC Threads",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.HEVC,
        audio_codec=AudioCodec.OPUS,
        audio_bitrate=96_000,
        threads=32,
        use_hw_accel=False,
        custom_encoder_opts="-x265-params pools=32:numa-pools=8",
    )
    hevc_job = _job(tmp_path, hevc, parallel_jobs=1, name="hevc")
    hevc_cmd = compressor._build_video_ffmpeg_command(hevc_job, hevc)
    assert hevc_cmd.count("-threads") == 1
    assert hevc_cmd[hevc_cmd.index("-threads") + 1] == "16"
    assert "pools" not in " ".join(hevc_cmd)
    assert "numa-pools" not in " ".join(hevc_cmd)

    archival = ProfileManager(config_dir=tmp_path / "profiles").get_profile(ARCHIVAL_PROFILE_NAME)
    alone = _job(tmp_path, archival, parallel_jobs=1, name="svt-alone")
    shared = _job(tmp_path, archival, parallel_jobs=4, name="svt-shared")
    alone_cmd = compressor._build_video_ffmpeg_command(alone, archival)
    shared_cmd = compressor._build_video_ffmpeg_command(shared, archival)

    for cmd in (alone_cmd, shared_cmd):
        assert cmd[cmd.index("-c:v") + 1] == "libsvtav1"
        assert cmd.count("-threads") == 0
        assert cmd.count("-svtav1-params") == 1
        assert "lp=" in cmd[cmd.index("-svtav1-params") + 1]
        assert not any(token in " ".join(cmd) for token in ("nvenc", "qsv", "amf", "videotoolbox"))

    alone_lp = int(alone_cmd[alone_cmd.index("-svtav1-params") + 1].split("lp=")[1])
    shared_lp = int(shared_cmd[shared_cmd.index("-svtav1-params") + 1].split("lp=")[1])
    assert alone.threads_used == alone_lp
    assert shared.threads_used == shared_lp
    assert alone_lp > shared_lp
    assert shared_lp * 4 <= MACHINE.cpu_threads


def test_queue_slots_cap_nvenc_and_leave_archival_on_software(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    compressor.hw_detector._info.gpus = [
        GPUInfo("NVIDIA", GPUVendor.NVIDIA, encoder_support={"h264": True, "hevc": True})
    ]
    compressor.codec_manager._available_encoders = {
        "h264_nvenc",
        "hevc_nvenc",
        "libx264",
        "libx265",
        "libsvtav1",
        "aac",
        "libopus",
    }

    lite = build_quick_compress_profiles()["Quick Lite"]
    archival = ProfileManager(config_dir=tmp_path / "profiles").get_profile(ARCHIVAL_PROFILE_NAME)

    assert compressor.queue_slots(lite, requested_parallel=8, queued_files=8) == HW_SESSION_LIMITS["nvenc"]
    assert compressor._select_hw_encoder(VideoCodec.H264) == "h264_nvenc"
    software_cap = MACHINE.cpu_threads // 2
    assert compressor.queue_slots(archival, requested_parallel=8, queued_files=8) == software_cap
    assert archival.use_hw_accel is False


def test_batch_processor_respects_concurrency_cap(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = CompressionProfile(
        name="Batch",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.H264,
        use_hw_accel=False,
    )
    state = {"current": 0, "max": 0}
    lock = threading.Lock()
    both_running = threading.Event()
    release = threading.Event()

    def fake_compress(input_file, output_file, profile, job_id=None, parallel_jobs=1):
        with lock:
            state["current"] += 1
            state["max"] = max(state["max"], state["current"])
            state.setdefault("planned", []).append(parallel_jobs)
            if state["current"] >= 2:
                both_running.set()
        both_running.wait(timeout=2)
        release.wait(timeout=2)
        with lock:
            state["current"] -= 1
        return CompressionResult(
            success=True,
            input_file=Path(input_file),
            output_file=Path(output_file),
        )

    compressor.compress = fake_compress
    processor = BatchProcessor(compressor)
    for index in range(2):
        processor.add_job(tmp_path / f"{index}.mp4", tmp_path / f"{index}-out.mp4", profile, job_id=f"b{index}")

    processor.start(max_concurrent=2)
    assert both_running.wait(timeout=2)
    release.set()
    deadline = time.time() + 3
    while time.time() < deadline and len(processor.get_all_results()) < 2:
        time.sleep(0.02)
    processor.stop()

    assert state["max"] == 2
    assert state["planned"] == [2, 2]
    assert len(processor.get_all_results()) == 2
