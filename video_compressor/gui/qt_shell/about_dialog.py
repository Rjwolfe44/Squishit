"""About dialog, update banner and background update helpers for the Qt shell.

Everything slow (the version check, the installer download) runs on a plain
worker thread. Workers never touch widgets. They post messages through a
small ``QObject`` that lives in the GUI thread, and Qt delivers those
messages there. Closing a dialog while a worker is running cancels the
work, and a late message from the worker is simply dropped.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...config import APP_NAME, APP_VERSION
from ...core import updater as _updater
from ...core.updater import ISSUES_URL, RELEASES_URL
from ...core.utils import format_size
from ..copy import GITHUB_REPO_URL

logger = logging.getLogger(__name__)

__all__ = [
    "AboutDialog",
    "UpdateBanner",
    "UpdateCheck",
    "UpdateInstaller",
    "launch_installer",
    "quit_application",
    "release_notes_snippet",
    "start_update_check",
]

# After the installer starts, wait this long before closing SquishIt so the
# setup program has a moment to take over (the old window did the same).
INSTALL_QUIT_DELAY_MS = 1500

LICENCE_LINE = (
    "Source-available under the PolyForm Noncommercial License 1.0.0. "
    "Commercial rights are reserved to Rjwolfe44."
)
FFMPEG_LINE = "Compression is done by FFmpeg, which keeps its own licence."

STATUS_IDLE = "Check whether a newer version is out."
STATUS_CHECKING = "Looking for updates…"
CHECK_FAILED_MESSAGE = (
    "Couldn't check for updates. Check your internet connection and try again."
)
DOWNLOAD_FAILED_MESSAGE = (
    "The download didn't finish. Check your internet connection and try again."
)
NO_DOWNLOAD_MESSAGE = "This release has no installer to download."
FALLBACK_HINT = "You can also get the newest version from the download page."

_BANNER_QSS = """
QFrame#updateBanner {
    background: #1d3461;
    border: 1px solid #3b82f6;
    border-radius: 10px;
}
QFrame#updateBanner QLabel {
    background: transparent;
}
QFrame#updateBanner QLabel#updateTitle {
    font-weight: 700;
}
"""


# ── small helpers ────────────────────────────────────────────────


def _open_url(url: str) -> None:
    """Open *url* in the default browser."""

    QDesktopServices.openUrl(QUrl(url))


def launch_installer(path: Any) -> None:
    """Run the downloaded setup quietly, detached from this process."""

    command = [str(path), "/VERYSILENT", "/NORESTART"]
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    subprocess.Popen(command, **kwargs)


def quit_application() -> None:
    """Close every window and leave the event loop."""

    app = QApplication.instance()
    if app is not None:
        app.closeAllWindows()
        app.quit()


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_MARKS = re.compile(r"(\*\*|__|`)")
_HTML_TAG = re.compile(r"<[^>]+>")


def release_notes_snippet(text: Any, max_lines: int = 8, max_chars: int = 600) -> str:
    """Plain-text start of a GitHub release body (markdown marks removed)."""

    lines = []
    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("```") or set(line) <= set("-=_*"):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*+]\s+", "• ", line)
        line = _MD_LINK.sub(r"\1", line)
        line = _HTML_TAG.sub("", line)
        line = _MD_MARKS.sub("", line).strip()
        if line:
            lines.append(line)
    snippet = "\n".join(lines[:max_lines])
    truncated = len(lines) > max_lines
    if len(snippet) > max_chars:
        snippet = snippet[:max_chars].rstrip()
        truncated = True
    return snippet + "…" if truncated else snippet


# ── worker -> GUI hand-off ───────────────────────────────────────

# Relays stay referenced here until their last message arrives, so a worker
# that outlives its caller can still post safely.
_LIVE_RELAYS: set = set()


class _Relay(QObject):
    """Lives in the GUI thread. ``post`` is safe to call from any thread."""

    _posted = Signal(object, bool)

    def __init__(self, callback: Callable[[Any], None]):
        super().__init__()
        self._callback: Optional[Callable[[Any], None]] = callback
        self._cancelled = False
        self._posted.connect(self._deliver)
        _LIVE_RELAYS.add(self)

    def post(self, payload: Any, final: bool = False) -> None:
        try:
            self._posted.emit(payload, final)
        except RuntimeError:  # the relay's C++ side is already gone
            pass

    def cancel(self, *_args) -> None:
        self._cancelled = True
        self._callback = None

    @Slot(object, bool)
    def _deliver(self, payload: Any, final: bool) -> None:
        if final:
            _LIVE_RELAYS.discard(self)
        callback = self._callback
        if self._cancelled or callback is None:
            return
        try:
            callback(payload)
        except Exception:
            logger.exception("Update callback failed")


class UpdateCheck:
    """Handle for a running version check."""

    def __init__(self, relay: _Relay, thread: threading.Thread):
        self._relay = relay
        self._thread = thread

    def cancel(self) -> None:
        """Drop the result. The worker may still finish, quietly."""

        self._relay.cancel()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()


def start_update_check(
    parent: Optional[QObject],
    on_result: Callable[[Any], None],
    force: bool = False,
    on_error: Optional[Callable[[str], None]] = None,
) -> UpdateCheck:
    """Check for a newer version without blocking the window.

    ``on_result(info_or_None)`` is called on the GUI thread, where *info* is
    an ``UpdateInfo`` (or ``None`` when there is nothing newer). If the check
    itself raises, ``on_error(message)`` is called instead; without an
    ``on_error`` the error is treated as "no update". Nothing is delivered if
    *parent* is destroyed or the returned handle is cancelled first.

    Call this from the GUI thread. Honouring the ``check_updates_on_startup``
    setting is up to the caller.
    """

    def deliver(payload) -> None:
        kind, value = payload
        if kind == "error" and on_error is not None:
            on_error(value)
        else:
            on_result(None if kind == "error" else value)

    relay = _Relay(deliver)
    if parent is not None:
        try:
            parent.destroyed.connect(relay.cancel)
        except (AttributeError, RuntimeError):
            pass

    def work() -> None:
        try:
            info = _updater.check_for_update(APP_VERSION, force=force)
        except Exception as exc:
            logger.debug("Update check failed", exc_info=True)
            relay.post(("error", str(exc) or exc.__class__.__name__), True)
        else:
            relay.post(("result", info), True)

    thread = threading.Thread(target=work, name="squishit-update-check", daemon=True)
    thread.start()
    return UpdateCheck(relay, thread)


class _Cancelled(Exception):
    """Raised inside the download progress callback to stop the download."""


def _discard(path: Any) -> None:
    try:
        if path:
            Path(str(path)).unlink()
    except OSError:
        pass


def _download_worker(url: str, relay: _Relay, cancel_event: threading.Event) -> None:
    last = {"pct": -1}

    def on_progress(done: int, total: int) -> None:
        if cancel_event.is_set():
            raise _Cancelled()
        pct = int(done * 100 / total) if total else -1
        if pct != last["pct"]:
            last["pct"] = pct
            relay.post(("progress", int(done), int(total)))

    try:
        path = _updater.download_update(url, progress_cb=on_progress)
    except _Cancelled:
        relay.post(("cancelled",), True)
        return
    except Exception as exc:
        logger.debug("Update download failed", exc_info=True)
        relay.post(("failed", f"The download stopped: {exc}"), True)
        return
    if cancel_event.is_set():
        _discard(path)
        relay.post(("cancelled",), True)
        return
    if not path:
        relay.post(("failed", DOWNLOAD_FAILED_MESSAGE), True)
        return
    relay.post(("downloaded", str(path)), True)


class _InstallState:
    """Plain-Python state, so a destroyed QObject can still be told to stop."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.relay: Optional[_Relay] = None
        self.running = False

    def abort(self, *_args) -> None:
        self.cancel_event.set()
        self.running = False
        if self.relay is not None:
            self.relay.cancel()


class UpdateInstaller(QObject):
    """Download an update on a worker thread, then start the installer.

    Signals (always delivered on the GUI thread):
    ``progress(done_bytes, total_bytes)`` (total is 0 when unknown),
    ``launched(installer_path)`` once the setup program has started, and
    ``failed(message)`` with plain-language text.
    """

    progress = Signal(int, int)
    launched = Signal(str)
    failed = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._state = _InstallState()
        self.destroyed.connect(self._state.abort)

    @property
    def running(self) -> bool:
        return self._state.running

    def start(self, info: Any) -> bool:
        """Begin downloading ``info.download_url``. False if already busy."""

        state = self._state
        if state.running:
            return False
        url = str(getattr(info, "download_url", "") or "")
        if not url:
            self.failed.emit(NO_DOWNLOAD_MESSAGE)
            return False
        state.cancel_event = threading.Event()
        relay = _Relay(self._on_message)
        state.relay = relay
        state.running = True
        threading.Thread(
            target=_download_worker,
            args=(url, relay, state.cancel_event),
            name="squishit-update-download",
            daemon=True,
        ).start()
        return True

    def cancel(self) -> None:
        """Stop a running download (the partial file is removed)."""

        self._state.abort()

    def _on_message(self, payload: Any) -> None:
        kind = payload[0]
        if kind == "progress":
            self.progress.emit(int(payload[1]), int(payload[2]))
            return
        self._state.running = False
        if kind == "failed":
            self.failed.emit(str(payload[1]))
        elif kind == "downloaded":
            path = payload[1]
            try:
                launch_installer(path)
            except Exception as exc:
                logger.debug("Could not start the installer", exc_info=True)
                self.failed.emit(
                    f"The download finished, but the installer couldn't start: {exc}"
                )
            else:
                self.launched.emit(path)


def _schedule_quit(quit_app: Optional[Callable[[], None]]) -> None:
    QTimer.singleShot(INSTALL_QUIT_DELAY_MS, quit_app or quit_application)


# ── update banner ────────────────────────────────────────────────


class UpdateBanner(QFrame):
    """Slim "Version X is available" bar for the top of the main window.

    ``set_update(info)`` shows it, ``set_update(None)`` hides it.
    ``installRequested(info)`` fires when "Download and install" is clicked;
    ``dismissed()`` fires when "Later" is clicked (the banner hides itself).

    With ``handle_install=True`` (the default) the banner also does the
    download, shows progress, starts the installer and then closes the app
    (``installLaunched()`` is emitted first). Pass ``handle_install=False``
    to only get the signals and do the work yourself.
    """

    installRequested = Signal(object)
    dismissed = Signal()
    installLaunched = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        handle_install: bool = True,
        quit_app: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self.setObjectName("updateBanner")
        self.setStyleSheet(_BANNER_QSS)
        self._info: Any = None
        self._handle_install = handle_install
        self._quit_app = quit_app
        self._fallback_url = RELEASES_URL
        self._installer = UpdateInstaller(self)
        self._installer.progress.connect(self._on_progress)
        self._installer.launched.connect(self._on_launched)
        self._installer.failed.connect(self._on_failed)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(2)
        self._title = QLabel("")
        self._title.setObjectName("updateTitle")
        self._detail = QLabel("")
        self._detail.setObjectName("hint")
        self._detail.setWordWrap(True)
        text.addWidget(self._title)
        text.addWidget(self._detail)
        row.addLayout(text, 1)

        self._progress = QProgressBar()
        self._progress.setFixedWidth(180)
        self._progress.setRange(0, 100)
        self._progress.hide()
        row.addWidget(self._progress)

        self._fallback_btn = QPushButton("Open download page")
        self._fallback_btn.clicked.connect(lambda: _open_url(self._fallback_url))
        self._fallback_btn.hide()
        row.addWidget(self._fallback_btn)

        self._install_btn = QPushButton("Download and install")
        self._install_btn.setObjectName("primary")
        self._install_btn.clicked.connect(self._on_install_clicked)
        row.addWidget(self._install_btn)

        self._later_btn = QPushButton("Later")
        self._later_btn.clicked.connect(self._on_later_clicked)
        row.addWidget(self._later_btn)

        self.setVisible(False)

    # ── public ───────────────────────────────────────────────────

    @property
    def update_info(self) -> Any:
        return self._info

    def set_update(self, info: Any) -> None:
        """Show the banner for *info*, or hide it when *info* is ``None``."""

        if self._installer.running:
            self._installer.cancel()
        self._info = info
        if info is None:
            self.setVisible(False)
            return
        self._title.setText(f"Version {info.latest_version} is available")
        self._show_idle()
        self.setVisible(True)

    def begin_install(self) -> None:
        """Download and install the current update (used when handling it here)."""

        if self._info is None or self._installer.running:
            return
        self._install_btn.setEnabled(False)
        self._install_btn.setText("Downloading…")
        self._later_btn.setText("Cancel")
        self._fallback_btn.hide()
        self._detail.setText("Starting the download…")
        self._progress.setRange(0, 0)
        self._progress.show()
        self._installer.start(self._info)

    # ── internals ────────────────────────────────────────────────

    def _show_idle(self) -> None:
        self._detail.setText(f"You have version {APP_VERSION}.")
        self._progress.hide()
        self._fallback_btn.hide()
        self._install_btn.setText("Download and install")
        self._install_btn.setEnabled(True)
        self._later_btn.setText("Later")

    def _on_install_clicked(self) -> None:
        if self._info is None or self._installer.running:
            return
        self.installRequested.emit(self._info)
        if self._handle_install:
            self.begin_install()

    def _on_later_clicked(self) -> None:
        if self._installer.running:
            self._installer.cancel()
            self._show_idle()
            return
        self.setVisible(False)
        self.dismissed.emit()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = max(0, min(100, int(done * 100 / total)))
            self._progress.setRange(0, 100)
            self._progress.setValue(pct)
            self._detail.setText(f"Downloading… {pct}%")
        else:
            self._progress.setRange(0, 0)

    def _on_launched(self, _path: str) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._install_btn.setText("Installing…")
        self._later_btn.setEnabled(False)
        self._detail.setText("Installing. SquishIt will close so the update can finish.")
        self.installLaunched.emit()
        _schedule_quit(self._quit_app)

    def _on_failed(self, message: str) -> None:
        self._progress.hide()
        self._install_btn.setText("Try again")
        self._install_btn.setEnabled(True)
        self._later_btn.setText("Later")
        self._later_btn.setEnabled(True)
        self._detail.setText(f"{message} {FALLBACK_HINT}")
        self._fallback_btn.show()


# ── about dialog ─────────────────────────────────────────────────


class AboutDialog(QDialog):
    """Version, licence, computer summary, links and the update check."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        hardware_summary: str = "",
        update_info: Any = None,
        quit_app: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self._hardware_summary = (hardware_summary or "").strip()
        self._update_info: Any = None
        self._quit_app = quit_app
        self._check: Optional[UpdateCheck] = None
        self._installer = UpdateInstaller(self)
        self._installer.progress.connect(self._on_progress)
        self._installer.launched.connect(self._on_launched)
        self._installer.failed.connect(self._on_install_failed)
        self.links = {
            "Download page": RELEASES_URL,
            "GitHub issues": ISSUES_URL,
            "Project page": GITHUB_REPO_URL,
        }
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(480)
        self.resize(500, 10)
        self._build()
        if update_info is not None:
            self._show_update(update_info)
        self._fit()

    # ── layout ───────────────────────────────────────────────────

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(22, 20, 22, 16)
        outer.setSpacing(8)

        title = QLabel(APP_NAME)
        title.setObjectName("title")
        outer.addWidget(title)
        self._version = QLabel(f"Version {APP_VERSION}")
        self._version.setObjectName("muted")
        outer.addWidget(self._version)
        tagline = QLabel("Desktop media compression.")
        tagline.setObjectName("hint")
        outer.addWidget(tagline)
        self._licence = QLabel(f"{LICENCE_LINE} {FFMPEG_LINE}")
        self._licence.setObjectName("hint")
        self._licence.setWordWrap(True)
        outer.addWidget(self._licence)

        self._hardware_section = QLabel("YOUR COMPUTER")
        self._hardware_section.setObjectName("section")
        self._hardware_card = QFrame()
        self._hardware_card.setObjectName("card")
        hw_layout = QVBoxLayout(self._hardware_card)
        hw_layout.setContentsMargins(12, 10, 12, 10)
        self._hardware = QLabel(self._hardware_summary)
        self._hardware.setObjectName("muted")
        self._hardware.setWordWrap(True)
        self._hardware.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        hw_layout.addWidget(self._hardware)
        outer.addSpacing(6)
        outer.addWidget(self._hardware_section)
        outer.addWidget(self._hardware_card)
        has_hardware = bool(self._hardware_summary)
        self._hardware_section.setVisible(has_hardware)
        self._hardware_card.setVisible(has_hardware)

        updates_label = QLabel("UPDATES")
        updates_label.setObjectName("section")
        outer.addSpacing(6)
        outer.addWidget(updates_label)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 12)
        card_layout.setSpacing(8)

        row = QHBoxLayout()
        self._status = QLabel(STATUS_IDLE)
        self._status.setObjectName("muted")
        self._status.setWordWrap(True)
        row.addWidget(self._status, 1)
        self._check_btn = QPushButton("Check for updates")
        self._check_btn.clicked.connect(self._start_check)
        row.addWidget(self._check_btn)
        card_layout.addLayout(row)

        self._notes_heading = QLabel("")
        self._notes_heading.setObjectName("section")
        self._notes = QLabel("")
        self._notes.setObjectName("hint")
        self._notes.setWordWrap(True)
        card_layout.addWidget(self._notes_heading)
        card_layout.addWidget(self._notes)
        self._notes_heading.hide()
        self._notes.hide()

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.hide()
        card_layout.addWidget(self._progress)

        self._error = QLabel("")
        self._error.setObjectName("muted")
        self._error.setStyleSheet("color: #fca5a5; background: transparent;")
        self._error.setWordWrap(True)
        self._error.hide()
        card_layout.addWidget(self._error)

        actions = QHBoxLayout()
        self._install_btn = QPushButton("Download and install")
        self._install_btn.setObjectName("primary")
        self._install_btn.clicked.connect(self._start_install)
        self._install_btn.hide()
        actions.addWidget(self._install_btn)
        self._fallback_btn = QPushButton("Open download page")
        self._fallback_btn.clicked.connect(lambda: _open_url(RELEASES_URL))
        self._fallback_btn.hide()
        actions.addWidget(self._fallback_btn)
        actions.addStretch(1)
        card_layout.addLayout(actions)
        outer.addWidget(card)

        outer.addSpacing(6)
        footer = QHBoxLayout()
        self._link_buttons = {}
        for label, url in self.links.items():
            button = QPushButton(label)
            button.setToolTip(url)
            button.clicked.connect(lambda _checked=False, target=url: _open_url(target))
            footer.addWidget(button)
            self._link_buttons[label] = button
        footer.addStretch(1)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        footer.addWidget(close)
        outer.addLayout(footer)

    # ── update check ─────────────────────────────────────────────

    def _start_check(self) -> None:
        if self._check is not None and self._check.running:
            return
        self._clear_problem()
        self._check_btn.setEnabled(False)
        self._check_btn.setText("Checking…")
        self._status.setText(STATUS_CHECKING)
        self._check = start_update_check(
            self,
            self._on_check_result,
            force=True,
            on_error=self._on_check_error,
        )

    def _finish_check(self) -> None:
        self._check = None
        self._check_btn.setEnabled(True)
        self._check_btn.setText("Check for updates")

    def _on_check_result(self, info: Any) -> None:
        self._finish_check()
        if info is None:
            self._status.setText(f"You're up to date. Version {APP_VERSION} is the newest.")
            return
        self._show_update(info)

    def _on_check_error(self, _message: str) -> None:
        self._finish_check()
        self._status.setText("Update check didn't work.")
        self._show_problem(f"{CHECK_FAILED_MESSAGE} {FALLBACK_HINT}")

    def _show_update(self, info: Any) -> None:
        self._update_info = info
        version = info.latest_version
        self._status.setText(
            f"Version {version} is available. You have version {APP_VERSION}."
        )
        snippet = release_notes_snippet(getattr(info, "release_notes", ""))
        self._notes_heading.setText(f"WHAT'S NEW IN {version}")
        self._notes.setText(snippet)
        self._notes_heading.setVisible(bool(snippet))
        self._notes.setVisible(bool(snippet))
        self._clear_problem()
        self._check_btn.hide()
        self._install_btn.setText("Download and install")
        self._install_btn.setEnabled(True)
        self._install_btn.show()
        self._fit()

    # ── download and install ─────────────────────────────────────

    def _start_install(self) -> None:
        info = self._update_info
        if info is None or self._installer.running:
            return
        self._clear_problem()
        self._install_btn.setEnabled(False)
        self._install_btn.setText("Downloading…")
        self._status.setText(f"Downloading version {info.latest_version}…")
        self._progress.setRange(0, 0)
        self._progress.show()
        self._fit()
        self._installer.start(info)

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = max(0, min(100, int(done * 100 / total)))
            self._progress.setRange(0, 100)
            self._progress.setValue(pct)
            self._status.setText(
                f"Downloading version {self._update_info.latest_version}… "
                f"{format_size(done)} of {format_size(total)}"
            )
        else:
            self._progress.setRange(0, 0)

    def _on_launched(self, _path: str) -> None:
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._install_btn.setText("Installing…")
        self._install_btn.setEnabled(False)
        self._status.setText(
            "Installing the update. SquishIt will close so it can finish."
        )
        _schedule_quit(self._quit_app)

    def _on_install_failed(self, message: str) -> None:
        self._progress.hide()
        self._status.setText("The update didn't install.")
        self._install_btn.setText("Try again")
        self._install_btn.setEnabled(True)
        self._install_btn.setVisible(self._update_info is not None)
        self._show_problem(f"{message} {FALLBACK_HINT}")

    # ── shared bits ──────────────────────────────────────────────

    def _show_problem(self, text: str) -> None:
        self._error.setText(text)
        self._error.show()
        self._fallback_btn.show()
        self._fit()

    def _fit(self) -> None:
        """Grow (never shrink) to fit text that just appeared."""

        layout = self.layout()
        if layout is None:
            return
        layout.activate()
        needed = layout.totalHeightForWidth(self.width())
        if needed > self.height():
            self.resize(self.width(), needed)

    def _clear_problem(self) -> None:
        self._error.hide()
        self._error.setText("")
        self._fallback_btn.hide()

    def _cancel_background(self) -> None:
        if self._check is not None:
            self._check.cancel()
            self._check = None
        self._installer.cancel()

    def done(self, result: int) -> None:  # noqa: A003 - Qt override
        self._cancel_background()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._cancel_background()
        super().closeEvent(event)

