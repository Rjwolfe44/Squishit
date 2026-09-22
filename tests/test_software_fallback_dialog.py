"""Main-window confirm dialog for a target-size software retry."""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from video_compressor.core.compressor import (
    SoftwareFallbackReason,
    SoftwareFallbackRequest,
    VideoCompressor,
)
from video_compressor.core.profiles import build_quick_compress_profiles
from video_compressor.gui.software_fallback_dialog import (
    SOFTWARE_FALLBACK_QUEUE_KIND,
    bind_main_window_software_fallback,
    declined_software_fallback_notice,
    detail_without_fallback_message,
    finish_software_fallback_prompt,
    handle_software_fallback_queue_message,
    show_software_fallback_dialog,
    software_fallback_prompt,
    with_declined_fallback_summary,
)

_ROOT = Path(__file__).resolve().parents[1]


def test_dialog_helper_imports_without_the_main_window():
    import sys

    import video_compressor.gui.software_fallback_dialog as dialog

    assert "customtkinter" not in sys.modules
    assert "video_compressor.gui.main_window" not in sys.modules
    assert dialog.SOFTWARE_FALLBACK_QUEUE_KIND == "software_fallback"


class _Window:
    def __init__(self, exists: bool = True):
        self._exists = exists

    def winfo_exists(self) -> bool:
        return self._exists


def _request(
    reason: SoftwareFallbackReason = SoftwareFallbackReason.SIZE_MISS,
) -> SoftwareFallbackRequest:
    if reason is SoftwareFallbackReason.ENCODE_FAILED:
        message = (
            "Hardware encoder hevc_nvenc failed for this target-size job. "
            "Confirm before retrying with libx265."
        )
        encoder = "libx265"
        hw_encoder = "hevc_nvenc"
    else:
        message = (
            "Hardware encoder h264_nvenc missed the 1 MB target (finished at 2.0 MB). "
            "Confirm before retrying with libx264."
        )
        encoder = "libx264"
        hw_encoder = "h264_nvenc"
    return SoftwareFallbackRequest(
        reason=reason,
        codec="h264" if reason is SoftwareFallbackReason.SIZE_MISS else "hevc",
        hw_encoder=hw_encoder,
        software_encoder=encoder,
        message=message,
        target_size_mb=1,
    )


def test_prompt_uses_reason_and_message():
    title, size_body = software_fallback_prompt(
        _request(SoftwareFallbackReason.SIZE_MISS)
    )
    _, failed_body = software_fallback_prompt(
        _request(SoftwareFallbackReason.ENCODE_FAILED)
    )

    assert title == "Retry with software encoder?"
    assert "Hardware encode missed the target size" in size_body
    assert "Confirm before retrying with libx264." in size_body
    assert "Yes retries this file with libx264." in size_body
    assert "Hardware encode failed" in failed_body
    assert "Confirm before retrying with libx265." in failed_body
    assert "Yes retries this file with libx265." in failed_body


def test_dialog_yes_no_and_dismiss():
    window = _Window()
    request = _request()
    seen = {}

    def ask(title, body, **kwargs):
        seen["title"] = title
        seen["body"] = body
        seen["kwargs"] = kwargs
        return seen["answer"]

    seen["answer"] = True
    assert show_software_fallback_dialog(window, request, ask=ask) is True
    assert seen["kwargs"]["parent"] is window
    assert seen["kwargs"]["default"] == "no"
    assert "Confirm before retrying with libx264." in seen["body"]

    seen["answer"] = False
    assert show_software_fallback_dialog(window, request, ask=ask) is False

    seen["answer"] = None
    assert show_software_fallback_dialog(window, request, ask=ask) is False


def test_dialog_error_or_closed_window_declines():
    def boom(*_args, **_kwargs):
        raise RuntimeError("tk")

    assert show_software_fallback_dialog(_Window(), _request(), ask=boom) is False

    called = []

    def ask(*_args, **_kwargs):
        called.append(True)
        return True

    assert (
        show_software_fallback_dialog(_Window(exists=False), _request(), ask=ask)
        is False
    )
    assert called == []


def test_worker_yes_waits_for_the_ui_queue(monkeypatch):
    compressor = VideoCompressor()
    ui_queue = queue.Queue()
    window = _Window()
    monkeypatch.setattr(
        "video_compressor.gui.software_fallback_dialog.show_software_fallback_dialog",
        lambda _window, request: request.software_encoder == "libx264",
    )
    bind_main_window_software_fallback(
        compressor,
        window,
        ui_queue,
        ui_thread=threading.main_thread(),
    )
    request = _request()
    outcome = {}

    def worker():
        outcome["value"] = compressor._software_fallback_callback(request)

    thread = threading.Thread(target=worker, name="compress-worker")
    thread.start()
    kind, queued, holder, done = ui_queue.get(timeout=2)

    assert kind == SOFTWARE_FALLBACK_QUEUE_KIND
    assert queued is request
    assert threading.current_thread() is threading.main_thread()
    finish_software_fallback_prompt(window, queued, holder, done)
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert outcome["value"] is True
    assert holder["value"] is True


def test_worker_no_keeps_hardware(monkeypatch):
    compressor = VideoCompressor()
    ui_queue = queue.Queue()
    monkeypatch.setattr(
        "video_compressor.gui.software_fallback_dialog.show_software_fallback_dialog",
        lambda _window, _request: False,
    )
    bind_main_window_software_fallback(
        compressor,
        _Window(),
        ui_queue,
        ui_thread=threading.main_thread(),
    )
    outcome = {}

    def worker():
        outcome["value"] = compressor._software_fallback_callback(_request())

    thread = threading.Thread(target=worker)
    thread.start()
    _kind, request, holder, done = ui_queue.get(timeout=2)
    finish_software_fallback_prompt(_Window(), request, holder, done)
    thread.join(timeout=2)

    assert outcome["value"] is False


def test_ui_thread_callback_does_not_queue(monkeypatch):
    compressor = VideoCompressor()
    ui_queue = queue.Queue()
    monkeypatch.setattr(
        "video_compressor.gui.software_fallback_dialog.show_software_fallback_dialog",
        lambda _window, _request: True,
    )
    bind_main_window_software_fallback(
        compressor,
        _Window(),
        ui_queue,
        ui_thread=threading.current_thread(),
    )

    assert compressor._software_fallback_callback(_request()) is True
    assert ui_queue.empty()


def test_closed_window_declines_without_waiting_for_a_dialog():
    compressor = VideoCompressor()
    ui_queue = queue.Queue()
    bind_main_window_software_fallback(
        compressor,
        _Window(exists=False),
        ui_queue,
        ui_thread=threading.main_thread(),
    )
    outcome = {}

    def worker():
        outcome["value"] = compressor._software_fallback_callback(_request())

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert outcome["value"] is False
    assert ui_queue.empty()


def test_window_closed_while_waiting_declines():
    compressor = VideoCompressor()
    ui_queue = queue.Queue()
    window = _Window()
    bind_main_window_software_fallback(
        compressor,
        window,
        ui_queue,
        ui_thread=threading.main_thread(),
    )
    outcome = {}

    def worker():
        outcome["value"] = compressor._software_fallback_callback(_request())

    thread = threading.Thread(target=worker)
    thread.start()
    ui_queue.get(timeout=2)
    window._exists = False
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert outcome["value"] is False


def test_declined_result_notice_is_separate_from_the_encode_note():
    message = "Confirm before retrying with libx264."
    result = SimpleNamespace(
        software_fallback_required=True,
        software_fallback_reason=SoftwareFallbackReason.SIZE_MISS.value,
        software_fallback_message=message,
        note=f"Best effort finished at 2.0 MB. {message}",
        error_message=f"{message}\nh264_nvenc failed",
        success=True,
    )

    notice = declined_software_fallback_notice(result)

    assert notice.startswith("Software encoder was not used.")
    assert message in notice
    assert (
        detail_without_fallback_message(result.note, result)
        == "Best effort finished at 2.0 MB."
    )
    assert (
        detail_without_fallback_message(result.error_message, result)
        == "h264_nvenc failed"
    )
    assert (
        declined_software_fallback_notice(
            SimpleNamespace(software_fallback_required=False)
        )
        == ""
    )
    assert with_declined_fallback_summary("Done — 1 compressed", [result]) == (
        "Done — 1 compressed, 1 kept on hardware"
    )
    assert (
        with_declined_fallback_summary("Done — 1 compressed", [])
        == "Done — 1 compressed"
    )


def test_quick_compress_presets_are_not_target_size_jobs():
    """Quick Compress does not install the dialog because these presets never ask."""

    for profile in build_quick_compress_profiles().values():
        assert profile.target_size_mb is None
        assert profile.target_reduction_percent is None


def test_queue_handler_leaves_other_messages_for_the_window(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "video_compressor.gui.software_fallback_dialog.show_software_fallback_dialog",
        lambda *_args, **_kwargs: calls.append(True) or True,
    )
    window = _Window()
    window._set_software_fallback_wait = lambda *_args: None

    assert handle_software_fallback_queue_message(window, ("result", object())) is False
    assert calls == []


def test_main_window_installs_the_dialog_and_quick_compress_does_not():
    main = (_ROOT / "video_compressor" / "gui" / "main_window.py").read_text(
        encoding="utf-8"
    )
    widgets = (_ROOT / "video_compressor" / "gui" / "widgets.py").read_text(
        encoding="utf-8"
    )
    quick = (_ROOT / "video_compressor" / "gui" / "quick_compress.py").read_text(
        encoding="utf-8"
    )

    assert "bind_main_window_software_fallback" in main
    assert "handle_software_fallback_queue_message" in main
    assert "with_declined_fallback_summary" in main
    assert "declined_software_fallback_notice" in widgets
    assert "detail_without_fallback_message" in widgets
    assert "bind_main_window_software_fallback" not in quick
    assert "set_software_fallback_callback" not in quick
