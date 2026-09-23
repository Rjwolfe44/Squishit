"""Yes/No ask before a target-size job leaves hardware for software.

The compressor callback can run on a worker thread. The main window posts
the request onto its UI queue and shows one modal there. Yes retries on
``request.software_encoder``. No, dismissing the dialog, or any failure
returns False so the hardware result stays.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from ..core.compressor import SoftwareFallbackReason, SoftwareFallbackRequest

logger = logging.getLogger(__name__)

SOFTWARE_FALLBACK_QUEUE_KIND = "software_fallback"

_REASON_HEADINGS = {
    SoftwareFallbackReason.ENCODE_FAILED.value: "Hardware encode failed",
    SoftwareFallbackReason.SIZE_MISS.value: "Hardware encode missed the target size",
}


def _reason_value(reason: Any) -> str:
    return (
        reason.value
        if isinstance(reason, SoftwareFallbackReason)
        else str(reason or "")
    )


def software_fallback_prompt(request: SoftwareFallbackRequest) -> tuple[str, str]:
    """Title and body for the confirm dialog, from the request reason and message."""

    encoder = request.software_encoder or "the software encoder"
    title = "Retry with software encoder?"
    lines = [
        _REASON_HEADINGS.get(_reason_value(request.reason), "Software encoder"),
        "",
    ]
    message = (request.message or "").strip()
    if message:
        lines.extend([message, ""])
    lines.append(f"Yes retries this file with {encoder}. No keeps the hardware result.")
    return title, "\n".join(lines)


def show_software_fallback_dialog(
    parent: Any,
    request: SoftwareFallbackRequest,
    *,
    ask: Optional[Callable[..., Any]] = None,
) -> bool:
    """Show the modal on the current thread. Yes is True. No or dismiss is False."""

    title, body = software_fallback_prompt(request)
    try:
        if ask is None:
            from tkinter import messagebox

            ask = messagebox.askyesno
        if (
            parent is not None
            and hasattr(parent, "winfo_exists")
            and not parent.winfo_exists()
        ):
            return False
        # messagebox.NO is the string "no". Defaulting to No keeps hardware.
        answer = ask(title, body, parent=parent, default="no")
    except Exception:
        logger.exception(
            "Software fallback dialog failed; keeping the hardware encoder"
        )
        return False
    return bool(answer)


def bind_main_window_software_fallback(
    compressor: Any,
    window: Any,
    ui_queue: Any,
    *,
    ui_thread: Optional[threading.Thread] = None,
    ask: Optional[Callable[..., Any]] = None,
) -> None:
    """Install the main-window confirm callback on ``compressor``.

    ``ask`` replaces the Tk yes/no box. The Qt shell passes its own dialog.
    The queued path reads ``window._software_fallback_ask`` so the worker
    thread still gets that same dialog on the UI thread.
    """

    window._software_fallback_ask = ask
    owner = ui_thread or threading.current_thread()

    def callback(request: SoftwareFallbackRequest) -> bool:
        if threading.current_thread() is owner:
            if ask is None:
                return show_software_fallback_dialog(window, request)
            return show_software_fallback_dialog(window, request, ask=ask)

        holder = {"value": False}
        done = threading.Event()
        try:
            if hasattr(window, "winfo_exists") and not window.winfo_exists():
                return False
            ui_queue.put((SOFTWARE_FALLBACK_QUEUE_KIND, request, holder, done))
        except Exception:
            logger.exception(
                "Could not ask about software fallback; keeping the hardware encoder"
            )
            return False

        while not done.wait(0.2):
            try:
                if hasattr(window, "winfo_exists") and not window.winfo_exists():
                    return False
            except Exception:
                return False
        return bool(holder.get("value", False))

    compressor.set_software_fallback_callback(callback)


def finish_software_fallback_prompt(
    window: Any,
    request: SoftwareFallbackRequest,
    holder: dict,
    done: threading.Event,
) -> None:
    """Run on the UI thread after the queue pump receives the ask."""

    try:
        ask = getattr(window, "_software_fallback_ask", None)
        if ask is None:
            holder["value"] = show_software_fallback_dialog(window, request)
        else:
            holder["value"] = show_software_fallback_dialog(window, request, ask=ask)
    except Exception:
        logger.exception(
            "Software fallback dialog failed; keeping the hardware encoder"
        )
        holder["value"] = False
    finally:
        done.set()


def handle_software_fallback_queue_message(window: Any, message: tuple) -> bool:
    """Handle one main-window queue item when it is the software-fallback ask.

    ``MainWindow._handle`` delegates here before any other queue kind. The
    ask runs on this thread through :func:`finish_software_fallback_prompt`.
    Yes retries on the software encoder. No, dismiss, or a dialog error keeps
    the hardware result. Returns False when ``message`` is some other item.
    """

    if not message or message[0] != SOFTWARE_FALLBACK_QUEUE_KIND:
        return False
    _, request, holder, done = message
    try:
        window._set_software_fallback_wait(request, True)
    finally:
        try:
            finish_software_fallback_prompt(window, request, holder, done)
        finally:
            window._set_software_fallback_wait(request, False)
    return True


def declined_software_fallback_notice(result: object) -> str:
    """Text for a result whose software retry was declined. Empty when not declined."""

    if not getattr(result, "software_fallback_required", False):
        return ""
    message = str(getattr(result, "software_fallback_message", "") or "").strip()
    if message:
        return f"Software encoder was not used. {message}"
    reason = _reason_value(getattr(result, "software_fallback_reason", ""))
    heading = _REASON_HEADINGS.get(reason)
    if heading:
        return f"Software encoder was not used. {heading}."
    return "Software encoder was not used."


def detail_without_fallback_message(text: str, result: object) -> str:
    """Drop the fallback message when the result card already shows it as a notice."""

    body = (text or "").strip()
    if not getattr(result, "software_fallback_required", False):
        return body
    message = str(getattr(result, "software_fallback_message", "") or "").strip()
    if not message or message not in body:
        return body
    return body.replace(message, "").strip()


def with_declined_fallback_summary(message: str, results: object) -> str:
    """Append a queue-summary clause when any result declined the software retry."""

    count = 0
    for result in results:
        if getattr(result, "software_fallback_required", False):
            count += 1
    if not count:
        return message
    return f"{message}, {count} kept on hardware"
