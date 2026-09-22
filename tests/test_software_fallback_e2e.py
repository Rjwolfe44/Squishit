"""Encode through the production ask-before-software handler.

Target-size jobs that miss or fail on hardware block in the GUI callback.
That callback queues ``software_fallback``. ``MainWindow._handle`` dispatches
the tuple to ``handle_software_fallback_queue_message``, which shows the real
Yes/No dialog. These tests stub only ``messagebox.askyesno`` and pump that
handler.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import types

import pytest

from video_compressor.core.compressor import (
    SoftwareFallbackReason,
    VideoCompressor,
    VideoInfo,
)
from video_compressor.core.hardware import (
    GPUInfo,
    GPUVendor,
    HardwareDetector,
    HardwareInfo,
)
from video_compressor.core.profiles import (
    CompressionProfile,
    build_quick_compress_profiles,
)
from video_compressor.gui.software_fallback_dialog import (
    bind_main_window_software_fallback,
    handle_software_fallback_queue_message,
)


class _UiWindow:
    """Stand-in parent for the dialog and the progress-card wait hook."""

    def __init__(self):
        self.waits: list[tuple[str, bool]] = []

    def winfo_exists(self) -> bool:
        return True

    def _set_software_fallback_wait(self, request, waiting: bool) -> None:
        self.waits.append((request.software_encoder, bool(waiting)))

    @property
    def waiting(self) -> bool:
        return bool(self.waits and self.waits[-1][1])


def _encoder_name(cmd):
    return cmd[cmd.index("-c:v") + 1]


def _hw_target_compressor(monkeypatch):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    monkeypatch.setattr(VideoCompressor, "_get_thread_count", lambda *args, **kwargs: 4)

    detector = HardwareDetector()
    detector._info = HardwareInfo(
        os_name="Linux",
        os_version="test",
        cpu_name="cpu",
        cpu_cores=4,
        cpu_threads=8,
        total_ram_gb=16,
        preferred_hw_encoder="amd",
        has_hw_encoder=True,
        gpus=[
            GPUInfo(
                "AMD",
                GPUVendor.AMD,
                encoder_support={"h264": True, "hevc": True},
            ),
            GPUInfo(
                "NVIDIA",
                GPUVendor.NVIDIA,
                encoder_support={"h264": True, "hevc": True},
            ),
        ],
        recommended_threads=4,
    )
    compressor = VideoCompressor(hw_detector=detector)
    compressor.codec_manager._available_encoders = {
        "libx264",
        "libx265",
        "h264_nvenc",
        "hevc_nvenc",
        "aac",
        "libopus",
    }
    return compressor


def _analyze_media(path):
    return VideoInfo(
        filepath=path,
        duration=10.0,
        size=8_000_000,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=5_000_000,
        audio_bitrate=128_000,
        total_bitrate=5_128_000,
        frame_count=300,
    )


def _install_answer(monkeypatch, window, answer, worker):
    """Stub the Tk modal. The dialog function itself still builds the prompt."""

    seen = []

    def ask(title, body, **kwargs):
        assert threading.current_thread() is threading.main_thread()
        assert worker["thread"].is_alive()
        assert window.waiting is True
        seen.append({"title": title, "body": body, "kwargs": kwargs})
        if answer == "error":
            raise RuntimeError("tk")
        return answer

    try:
        from tkinter import messagebox
    except ModuleNotFoundError:
        tkinter_mod = types.ModuleType("tkinter")
        messagebox = types.ModuleType("tkinter.messagebox")
        tkinter_mod.messagebox = messagebox
        monkeypatch.setitem(sys.modules, "tkinter", tkinter_mod)
        monkeypatch.setitem(sys.modules, "tkinter.messagebox", messagebox)
    monkeypatch.setattr(messagebox, "askyesno", ask, raising=False)
    return seen


def _run_through_handler(monkeypatch, tmp_path, profile, outputs, answer, *, job_name):
    compressor = _hw_target_compressor(monkeypatch)
    if profile.target_size_mode == "exact":
        monkeypatch.setattr(
            compressor,
            "_apply_exact_target_fallback",
            lambda profile, media: None,
        )
    source = tmp_path / "input.mp4"
    source.write_bytes(b"0")
    output = tmp_path / f"{job_name}.mp4"
    commands = []

    def fake_run(cmd, *, job, job_id, media_info, start_time, **kwargs):
        commands.append(list(cmd))
        encoder = _encoder_name(cmd)
        size = outputs(encoder)
        if size is None:
            return 1, [f"{encoder} failed"]
        job.output_file.parent.mkdir(parents=True, exist_ok=True)
        job.output_file.write_bytes(b"0" * size)
        return 0, []

    compressor._run_ffmpeg_process = fake_run
    compressor.analyze_media = lambda path: _analyze_media(path)

    ui_queue = queue.Queue()
    window = _UiWindow()
    worker = {"thread": None}
    seen = _install_answer(monkeypatch, window, answer, worker)
    bind_main_window_software_fallback(
        compressor,
        window,
        ui_queue,
        ui_thread=threading.main_thread(),
    )
    box = {}

    def run():
        try:
            box["result"] = compressor.compress(
                source, output, profile, job_id=job_name
            )
        except Exception as exc:  # noqa: BLE001 - report the worker failure
            box["error"] = exc

    thread = threading.Thread(target=run, name="compress-worker")
    worker["thread"] = thread
    thread.start()
    handled = 0
    deadline = time.monotonic() + 5
    while thread.is_alive() and time.monotonic() < deadline:
        try:
            message = ui_queue.get(timeout=0.05)
        except queue.Empty:
            continue
        assert handle_software_fallback_queue_message(window, message) is True
        handled += 1
    thread.join(timeout=1)

    assert not thread.is_alive(), "encode blocked in the software-fallback ask"
    assert "error" not in box, box.get("error")
    assert ui_queue.empty()
    return commands, box["result"], seen, window, handled


def _profile(name: str, *, target_size_mb: int | None, mode: str | None):
    profile = CompressionProfile.from_dict(build_quick_compress_profiles()[name].to_dict())
    if target_size_mb is not None:
        profile.target_size_mb = target_size_mb
        profile.target_size_mode = mode
    return profile


def _assert_dialog_shown(seen, window, software_encoder: str, heading: str):
    assert len(seen) == 1
    assert seen[0]["title"] == "Retry with software encoder?"
    assert heading in seen[0]["body"]
    assert f"Yes retries this file with {software_encoder}." in seen[0]["body"]
    assert seen[0]["kwargs"]["default"] == "no"
    assert isinstance(seen[0]["kwargs"]["parent"], _UiWindow)
    assert window.waits == [(software_encoder, True), (software_encoder, False)]
    assert window.waiting is False


def test_exact_size_miss_yes_retries_through_the_gui_handler(monkeypatch, tmp_path):
    profile = _profile("Quick Lite", target_size_mb=1, mode="exact")
    commands, result, seen, window, handled = _run_through_handler(
        monkeypatch,
        tmp_path,
        profile,
        lambda encoder: 1_000_000 if encoder == "libx264" else 2_000_000,
        True,
        job_name="exact-yes",
    )

    assert handled == 1
    _assert_dialog_shown(
        seen,
        window,
        "libx264",
        "Hardware encode missed the target size",
    )
    assert _encoder_name(commands[0]) == "h264_nvenc"
    assert _encoder_name(commands[-1]) == "libx264"
    assert result.success is True
    assert result.software_fallback_required is False
    assert result.encoder_name == "libx264"


def test_encode_failure_yes_retries_through_the_gui_handler(monkeypatch, tmp_path):
    profile = _profile("HEVC Max", target_size_mb=1, mode="fast")
    commands, result, seen, window, handled = _run_through_handler(
        monkeypatch,
        tmp_path,
        profile,
        lambda encoder: None if encoder == "hevc_nvenc" else 1_000_000,
        True,
        job_name="encode-yes",
    )

    assert handled == 1
    _assert_dialog_shown(seen, window, "libx265", "Hardware encode failed")
    assert [_encoder_name(cmd) for cmd in commands] == ["hevc_nvenc", "libx265"]
    assert result.success is True
    assert result.software_fallback_required is False
    assert result.encoder_name == "libx265"


@pytest.mark.parametrize(
    ("scenario", "answer"),
    [
        ("size_miss", False),
        ("size_miss", None),
        ("size_miss", "error"),
        ("encode_fail", False),
        ("encode_fail", None),
        ("encode_fail", "error"),
    ],
)
def test_declined_ask_keeps_the_hardware_result(monkeypatch, tmp_path, scenario, answer):
    if scenario == "size_miss":
        profile = _profile("Quick Lite", target_size_mb=1, mode="exact")
        outputs = lambda encoder: 2_000_000
        hw_encoder = "h264_nvenc"
        software_encoder = "libx264"
        heading = "Hardware encode missed the target size"
        reason = SoftwareFallbackReason.SIZE_MISS
        job_name = f"exact-{answer}"
    else:
        profile = _profile("HEVC Max", target_size_mb=1, mode="fast")
        outputs = lambda encoder: None
        hw_encoder = "hevc_nvenc"
        software_encoder = "libx265"
        heading = "Hardware encode failed"
        reason = SoftwareFallbackReason.ENCODE_FAILED
        job_name = f"fail-{answer}"

    commands, result, seen, window, handled = _run_through_handler(
        monkeypatch,
        tmp_path,
        profile,
        outputs,
        answer,
        job_name=job_name,
    )

    assert handled == 1
    _assert_dialog_shown(seen, window, software_encoder, heading)
    assert {_encoder_name(cmd) for cmd in commands} == {hw_encoder}
    assert software_encoder not in " ".join(" ".join(cmd) for cmd in commands)
    assert result.software_fallback_required is True
    assert result.software_fallback_reason == reason.value
    assert f"Confirm before retrying with {software_encoder}" in (
        result.note if result.success else result.error_message
    )
    assert result.encoder_name == hw_encoder
    if scenario == "size_miss":
        assert result.success is True
    else:
        assert result.success is False


def test_quick_compress_crf_failure_never_asks(monkeypatch, tmp_path):
    profile = _profile("Quick Lite", target_size_mb=None, mode=None)
    assert profile.target_size_mb is None
    assert profile.crf
    commands, result, seen, window, handled = _run_through_handler(
        monkeypatch,
        tmp_path,
        profile,
        lambda encoder: None,
        True,
        job_name="quick-crf",
    )

    assert handled == 0
    assert seen == []
    assert window.waits == []
    assert [_encoder_name(cmd) for cmd in commands] == ["h264_nvenc"]
    assert result.success is False
    assert result.software_fallback_required is False
    assert "Confirm before retrying" not in (result.error_message or "")
    assert "libx264" not in result.encoder_name
