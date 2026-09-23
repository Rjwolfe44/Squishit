"""PySide6 main window.

The window queues files and calls ``VideoCompressor.compress_async``. Encoder
selection, the quality ladder, and the ask-before-software rule stay in the
existing backend. Target-size jobs ask. Quick Compress presets do not set a
target size, so they do not ask.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...config import APP_NAME, APP_VERSION, get_config, get_config_manager
from ...core.history import HistoryEntry, HistoryManager
from ...core.profiles import (
    QUICK_COMPRESS_ORDER,
    CompressionProfile,
    ProfileManager,
    build_quick_compress_profiles,
    describe_target_size_plan,
    normalize_quick_compress_name,
)
from ...core.updater import RELEASES_URL
from ...core.utils import collect_media_files, format_size, format_time
from ..copy import (
    DROP_ZONE_NEXT,
    DROP_ZONE_TITLE,
    EMPTY_QUEUE,
    HELP_TRAY,
    HOME_TAGLINE,
    QUICK_COMPRESS_TITLE,
    QUICK_PRESET_BLURBS,
    TRAY_HELP,
    TRAY_HINT,
    help_menu_items,
    human_progress_status,
    progress_detail,
)
from ..hw_status import (
    HardwareProbe,
    describe_hw_status,
    encoder_result_label,
    probe_from_compressor,
    status_summary,
)
from ..ipc import SingleInstanceServer
from ..software_fallback_dialog import (
    bind_main_window_software_fallback,
    declined_software_fallback_notice,
    handle_software_fallback_queue_message,
    with_declined_fallback_summary,
)
from .dialogs import qt_yes_no_ask, show_about
from .encode_form import EncodeForm, output_path_for
from .hw_card import HardwareStatusCard
from .settings_pane import SettingsPane, scroll_wrap
from .theme import DARK_STYLESHEET

logger = logging.getLogger(__name__)


def _window_icon_path() -> Optional[Path]:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    icon_path = base_dir / "assets" / "squishit.ico"
    return icon_path if icon_path.exists() else None


def _polish_flag(widget, name: str, value: bool) -> None:
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


class _FileRow(QFrame):
    def __init__(self, path: Path, on_remove):
        super().__init__()
        self.path = path
        self.setObjectName("card")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        text = QVBoxLayout()
        self._name = QLabel(path.name)
        self._meta = QLabel(str(path.parent))
        self._meta.setObjectName("muted")
        self._meta.setWordWrap(True)
        text.addWidget(self._name)
        text.addWidget(self._meta)
        layout.addLayout(text, 1)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: on_remove(path))
        layout.addWidget(remove)

    def set_info(self, info) -> None:
        if info is None:
            return
        parts = []
        size = getattr(info, "size", None)
        if size:
            parts.append(format_size(size))
        width = getattr(info, "width", None)
        height = getattr(info, "height", None)
        if width and height:
            parts.append(f"{width}×{height}")
        codec = getattr(info, "video_codec", None) or getattr(info, "codec", None)
        if codec:
            parts.append(str(codec))
        if parts:
            self._meta.setText(" · ".join(parts))


class _ProgressRow(QFrame):
    def __init__(self, job_id: str, title: str, on_cancel):
        super().__init__()
        self.job_id = job_id
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        header = QHBoxLayout()
        self._title = QLabel(title)
        header.addWidget(self._title, 1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(lambda: on_cancel(job_id))
        header.addWidget(cancel)
        layout.addLayout(header)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        layout.addWidget(self._bar)
        self._status = QLabel("Waiting in the queue…")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

    def update_job(self, job, *, awaiting: bool) -> None:
        try:
            percent = int(float(getattr(job, "progress", 0) or 0))
        except (TypeError, ValueError):
            percent = 0
        self._bar.setValue(max(0, min(100, percent)))
        self._status.setText(
            "\n".join(
                part
                for part in (
                    human_progress_status(job, awaiting_software_fallback=awaiting),
                    progress_detail(job),
                )
                if part
            )
        )


class _ResultRow(QFrame):
    def __init__(self, result):
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        if getattr(result, "cancelled", False):
            title = "Cancelled"
        elif getattr(result, "skipped", False):
            title = "Skipped"
        elif getattr(result, "success", False):
            title = "Kept original" if getattr(result, "kept_original", False) else "Compressed"
        else:
            title = "Failed"
        name = ""
        output = getattr(result, "output_file", None)
        source = getattr(result, "input_file", None)
        if output:
            name = Path(output).name
        elif source:
            name = Path(source).name
        heading = QLabel(f"{title} — {name}" if name else title)
        layout.addWidget(heading)
        bits = []
        if getattr(result, "success", False) and not getattr(result, "skipped", False):
            original = getattr(result, "original_size", 0) or 0
            compressed = getattr(result, "compressed_size", 0) or 0
            if original and compressed:
                bits.append(f"{format_size(original)} → {format_size(compressed)}")
            reduction = getattr(result, "reduction_percent", None)
            if reduction is not None and not getattr(result, "kept_original", False):
                try:
                    bits.append(f"{float(reduction):.0f}% smaller")
                except (TypeError, ValueError):
                    pass
            elapsed = getattr(result, "encoding_time", 0) or 0
            if elapsed:
                bits.append(format_time(elapsed))
        encoder = encoder_result_label(str(getattr(result, "encoder_name", "") or ""))
        if encoder:
            bits.append(f"Encoder: {encoder}")
        note = getattr(result, "note", None) or getattr(result, "error_message", None)
        if note:
            bits.append(str(note))
        notice = declined_software_fallback_notice(result)
        if notice:
            bits.append(notice)
        body = QLabel(" · ".join(bits) if bits else "")
        body.setObjectName("hint")
        body.setWordWrap(True)
        layout.addWidget(body)


class SquishItWindow(QMainWindow):
    """Full-app shell: queue, profiles, Quick Compress, and encode settings."""

    def __init__(
        self,
        startup_files: Optional[list[Path]] = None,
        auto_start: bool = False,
    ):
        super().__init__()
        self.setStyleSheet(DARK_STYLESHEET)
        self.setWindowTitle(APP_NAME)
        self._apply_icon()
        self._queue: queue.Queue = queue.Queue()
        self._ui_thread = threading.current_thread()
        self._compressor = None
        self._compressor_lock = threading.Lock()
        self._profile_manager = ProfileManager()
        self._history = HistoryManager(max_entries=get_config().history_max_entries)
        self._files: dict[str, dict] = {}
        self._progress_rows: dict[str, _ProgressRow] = {}
        self._results: list = []
        self._pending: list[str] = []
        self._active = 0
        self._running = False
        self._cancel_requested = False
        self._awaiting_fallback: set[str] = set()
        self._hw_probe: Optional[HardwareProbe] = None
        self._full_profile_active = False
        self._quick_buttons: dict[str, QPushButton] = {}
        self._output_folder: Optional[Path] = None
        self._current_profile: Optional[CompressionProfile] = None
        self._selected_profile = get_config().default_profile or "Balanced"
        self.form = EncodeForm(
            target_size_mode=get_config().default_target_size_mode,
            resource_governor=get_config().resource_governor,
            parallel_jobs=max(1, int(get_config().max_parallel_jobs or 1)),
            output_name_template=get_config().output_name_template,
            use_hw_accel=bool(get_config().use_hardware_accel),
        )
        base = self._profile_manager.get_profile(self._selected_profile)
        if base is None:
            base = self._profile_manager.get_profile("Balanced")
            self._selected_profile = "Balanced"
        if base is not None:
            self.form.apply_profile(base)

        self._build()
        saved_quick = normalize_quick_compress_name(
            get_config().quick_compress_profile
        )
        self.select_quick(saved_quick, persist=False)
        self._place_on_screen()
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._pump)
        self._timer.start()
        self._ipc = SingleInstanceServer(self._on_ipc_open)
        self._ipc.start()
        if startup_files:
            self.add_paths([str(path) for path in startup_files])
        self._refresh_preview()
        self._start_hw_probe()
        if auto_start and self._files:
            QTimer.singleShot(0, self.start_compression)

    def _apply_icon(self) -> None:
        icon_path = _window_icon_path()
        if icon_path is None:
            return
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            self.setWindowIcon(icon)

    def _place_on_screen(self) -> None:
        config = get_config()
        width = int(config.window_width or 1180)
        height = int(config.window_height or 760)
        if (width, height) in {(1200, 800), (2240, 1480)} or width < 960 or height < 640:
            width, height = 1180, 760
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            width = min(width, max(960, int(avail.width() * 0.94)))
            height = min(height, max(640, int(avail.height() * 0.9)))
        self.resize(width, height)
        self.setMinimumSize(960, 640)

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)

        header = QHBoxLayout()
        titles = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        titles.addWidget(title)
        tagline = QLabel(HOME_TAGLINE)
        tagline.setObjectName("hint")
        titles.addWidget(tagline)
        header.addLayout(titles, 1)
        self._output_btn = QPushButton("Output folder")
        self._output_btn.clicked.connect(self._pick_output_folder)
        header.addWidget(self._output_btn)
        help_btn = QPushButton("Help")
        help_btn.clicked.connect(self._open_help_menu)
        header.addWidget(help_btn)
        about_btn = QPushButton("About")
        about_btn.clicked.connect(self._open_about)
        header.addWidget(about_btn)
        outer.addLayout(header)

        self._hw_card = HardwareStatusCard()
        outer.addWidget(self._hw_card)
        tray = QLabel(TRAY_HINT)
        tray.setObjectName("trayHint")
        tray.setWordWrap(True)
        outer.addWidget(tray)

        self._queue_host = QWidget()
        queue_layout = QVBoxLayout(self._queue_host)
        queue_layout.setContentsMargins(0, 0, 8, 0)
        queue_layout.setSpacing(8)

        self._drop = QFrame()
        self._drop.setObjectName("dropZone")
        drop_layout = QVBoxLayout(self._drop)
        drop_layout.setContentsMargins(16, 16, 16, 16)
        drop_title = QLabel(DROP_ZONE_TITLE)
        drop_title.setObjectName("title")
        drop_layout.addWidget(drop_title)
        drop_next = QLabel(DROP_ZONE_NEXT)
        drop_next.setObjectName("hint")
        drop_next.setWordWrap(True)
        drop_layout.addWidget(drop_next)
        drop_actions = QHBoxLayout()
        add_files = QPushButton("Add files")
        add_files.clicked.connect(self._pick_files)
        add_folder = QPushButton("Add folder")
        add_folder.clicked.connect(self._pick_folder)
        drop_actions.addWidget(add_files)
        drop_actions.addWidget(add_folder)
        drop_actions.addStretch(1)
        drop_layout.addLayout(drop_actions)
        queue_layout.addWidget(self._drop)

        quick_title = QLabel(QUICK_COMPRESS_TITLE)
        quick_title.setObjectName("section")
        queue_layout.addWidget(quick_title)
        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        presets = build_quick_compress_profiles()
        for preset_name in QUICK_COMPRESS_ORDER:
            button = QPushButton(preset_name)
            button.setObjectName("pill")
            button.setToolTip(
                QUICK_PRESET_BLURBS.get(preset_name, presets[preset_name].description)
            )
            button.clicked.connect(
                lambda _checked=False, name=preset_name: self.select_quick(name)
            )
            self._quick_buttons[preset_name] = button
            quick_row.addWidget(button)
        quick_row.addStretch(1)
        queue_layout.addLayout(quick_row)
        self._quick_blurb = QLabel("")
        self._quick_blurb.setObjectName("hint")
        self._quick_blurb.setWordWrap(True)
        queue_layout.addWidget(self._quick_blurb)

        actions = QHBoxLayout()
        self._compress_btn = QPushButton("Quick Compress")
        self._compress_btn.setMinimumHeight(44)
        self._compress_btn.setObjectName("primary")
        self._compress_btn.clicked.connect(self.start_compression)
        actions.addWidget(self._compress_btn)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.cancel_all)
        self._cancel_btn.setEnabled(False)
        actions.addWidget(self._cancel_btn)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.clicked.connect(self.clear_queue)
        actions.addWidget(self._clear_btn)
        actions.addStretch(1)
        self._queue_label = QLabel(EMPTY_QUEUE)
        self._queue_label.setObjectName("hint")
        actions.addWidget(self._queue_label)
        queue_layout.addLayout(actions)

        self._rows = QVBoxLayout()
        self._rows.setSpacing(8)
        queue_layout.addLayout(self._rows)
        queue_layout.addStretch(1)

        self._settings = SettingsPane(
            self._profile_manager.get_all_profiles(),
            self.form,
            self._on_settings_change,
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(scroll_wrap(self._queue_host, "queueScroll"))
        splitter.addWidget(scroll_wrap(self._settings, "settingsScroll"))
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)
        self.setAcceptDrops(True)
        self._refresh_output_button()

    def _on_settings_change(self, kind: str, value: Optional[str]) -> None:
        if kind == "profile" and value:
            self.select_profile(value)
            return
        if kind == "quick" and value:
            self.select_quick(value)
            return
        if self.form.quick_name:
            self._settings.set_active_quick(self.form.quick_name)
        elif self._full_profile_active:
            self._settings.set_active_profile(self._selected_profile)
        else:
            self._settings.set_active_profile(None)
        self._refresh_preview()

    def select_profile(self, name: str) -> None:
        profile = self._profile_manager.get_profile(name)
        if profile is None:
            return
        self._selected_profile = name
        self._full_profile_active = True
        self.form.apply_profile(profile, quick_name=None)
        self._settings.load(self.form)
        self._settings.set_active_profile(name)
        self._refresh_preview()

    def select_quick(self, name: str, *, persist: bool = True) -> None:
        presets = build_quick_compress_profiles()
        profile = presets.get(name)
        if profile is None:
            return
        self._full_profile_active = False
        self.form.apply_profile(profile, quick_name=name)
        self._settings.load(self.form)
        self._settings.set_active_quick(name)
        if persist:
            try:
                get_config_manager().update_config(quick_compress_profile=name)
            except Exception:
                logger.debug("Could not store the Quick Compress preset", exc_info=True)
        self._refresh_preview()

    def _base_profile(self) -> CompressionProfile:
        if self.form.quick_name:
            preset = build_quick_compress_profiles().get(self.form.quick_name)
            if preset is not None:
                return CompressionProfile.from_dict(preset.to_dict())
        base = self._profile_manager.get_profile(self._selected_profile)
        if base is None:
            profiles = self._profile_manager.get_all_profiles()
            base = profiles[0]
        return base

    def job_profile(self) -> CompressionProfile:
        base = self._base_profile()
        return self.form.build_profile(self._selected_profile or base.name, base)

    def _refresh_preview(self) -> None:
        profile = self.job_profile()
        self._style_quick()
        self._refresh_action_label()
        self._refresh_hw_status()
        self._refresh_size_preview(profile)

    def _style_quick(self) -> None:
        for name, button in self._quick_buttons.items():
            _polish_flag(button, "selected", name == self.form.quick_name)
        self._quick_blurb.setText(
            QUICK_PRESET_BLURBS.get(self.form.quick_name or "", "")
        )

    def _refresh_action_label(self) -> None:
        if self._running:
            return
        if self.form.quick_name:
            self._compress_btn.setText("Quick Compress")
        else:
            self._compress_btn.setText("Compress")

    def _refresh_hw_status(self) -> None:
        profile = self.job_profile()
        action = "Quick Compress" if self.form.quick_name else "This preset"
        status = describe_hw_status(
            self._hw_probe,
            codec=profile.video_codec,
            use_hw=bool(profile.use_hw_accel),
            force_software=self.form.ladder_choice().force_software,
            action=action,
            software_name=profile.video_codec.ffmpeg_encoder,
        )
        self._hw_card.apply(status)
        self._settings.set_detected_hw(status_summary(status))

    def apply_hardware_probe(self, probe: Optional[HardwareProbe]) -> None:
        """Show a probe immediately. Tests use this; the home path uses the worker."""

        self._hw_probe = probe
        self._refresh_hw_status()

    def _start_hw_probe(self) -> None:
        threading.Thread(target=self._hw_probe_worker, daemon=True).start()

    def _hw_probe_worker(self) -> None:
        try:
            probe = probe_from_compressor(self._get_compressor())
        except Exception:
            logger.debug("Hardware probe failed", exc_info=True)
            probe = HardwareProbe(failed=True)
        self._queue.put(("hw_probe", probe))

    def _refresh_size_preview(self, profile: CompressionProfile) -> None:
        if not profile.target_size_mb or profile.output_mode != "video":
            self._settings.set_size_preview("")
            return
        info = self._first_video_info()
        if info is None:
            self._settings.set_size_preview("Preview pending source analysis…")
            return
        if self._compressor is None:
            self._settings.set_size_preview("")
            return
        try:
            plan = self._compressor._resolve_target_size_plan(profile, info)
            if not plan:
                self._settings.set_size_preview("")
                return
            self._settings.set_size_preview(describe_target_size_plan(plan, info.size))
        except Exception:
            self._settings.set_size_preview("Preview unavailable for the current selection.")

    def _first_video_info(self):
        for item in self._files.values():
            info = item.get("info")
            if info is not None and hasattr(info, "video_codec"):
                return info
        return None

    def _get_compressor(self):
        if self._compressor is not None:
            return self._compressor
        with self._compressor_lock:
            if self._compressor is None:
                from ...core.compressor import VideoCompressor

                compressor = VideoCompressor()
                compressor.set_progress_callback(
                    lambda job: self._queue.put(("progress", job))
                )
                bind_main_window_software_fallback(
                    compressor,
                    self,
                    self._queue,
                    ui_thread=self._ui_thread,
                    ask=qt_yes_no_ask,
                )
                self._compressor = compressor
        return self._compressor

    def add_paths(self, paths: list[str]) -> None:
        if self._running:
            return
        added = False
        for path in collect_media_files(paths):
            key = str(path.resolve())
            if key in self._files:
                continue
            self._add_file(path, key)
            added = True
        if added:
            self._refresh_queue_label()
            self._refresh_preview()

    def _add_file(self, path: Path, key: str) -> None:
        row = _FileRow(path, self.remove_file)
        self._files[key] = {"path": path, "row": row, "info": None}
        self._rows.addWidget(row)

        def _analyze() -> None:
            info = None
            try:
                info = self._get_compressor().analyze_media(path)
            except Exception:
                logger.debug("Could not analyze %s", path, exc_info=True)
            self._queue.put(("info", key, info))

        threading.Thread(target=_analyze, daemon=True).start()

    def remove_file(self, path: Path) -> None:
        if self._running:
            return
        key = str(Path(path).resolve())
        item = self._files.pop(key, None)
        if item is None:
            return
        row = item.get("row")
        if row is not None:
            row.setParent(None)
            row.deleteLater()
        self._refresh_queue_label()
        self._refresh_preview()

    def _clear_rows(self) -> None:
        while self._rows.count():
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._progress_rows.clear()

    def clear_queue(self) -> None:
        if self._running:
            return
        self._clear_rows()
        self._files.clear()
        self._results.clear()
        self._refresh_queue_label()
        self._refresh_preview()

    def _refresh_queue_label(self) -> None:
        count = len(self._files)
        if count == 0:
            self._queue_label.setText(EMPTY_QUEUE)
        elif count == 1:
            self._queue_label.setText("1 file queued")
        else:
            self._queue_label.setText(f"{count} files queued")

    def _pick_files(self) -> None:
        paths, _selected = QFileDialog.getOpenFileNames(
            self,
            "Add media",
            str(Path.home()),
            "Media (*.mp4 *.mkv *.mov *.avi *.webm *.wmv *.m4v *.mpg *.mpeg "
            "*.jpg *.jpeg *.png *.webp *.gif *.bmp *.tif *.tiff *.avif)",
        )
        if paths:
            self.add_paths(paths)

    def _pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Add folder", str(Path.home()))
        if folder:
            self.add_paths([folder])

    def _pick_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Output folder",
            str(self._output_folder or Path.home()),
        )
        if not folder:
            return
        self._output_folder = Path(folder)
        self._output_folder.mkdir(parents=True, exist_ok=True)
        self._refresh_output_button()

    def _refresh_output_button(self) -> None:
        if self._output_folder is None:
            self._output_btn.setText("Output: next to source")
        else:
            self._output_btn.setText(f"Output: {self._output_folder.name}")

    def start_compression(self) -> None:
        if self._running or not self._files:
            if not self._files:
                self._queue_label.setText("Add a file, then press Quick Compress.")
            return
        self._cancel_requested = False
        self._results.clear()
        self._awaiting_fallback.clear()
        profile = self.job_profile()
        self._current_profile = profile
        self._running = True
        self._compress_btn.setEnabled(False)
        self._clear_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._persist_run_settings()
        ordered = list(self._files)
        self._clear_rows()
        self._pending = ordered
        self._active = 0
        try:
            self._launch_next()
        except Exception as exc:
            logger.exception("Could not start compression")
            self._running = False
            self._compress_btn.setEnabled(True)
            self._clear_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            QMessageBox.critical(self, APP_NAME, str(exc) or "Could not start compression")

    def _persist_run_settings(self) -> None:
        payload = {
            "max_parallel_jobs": self.form.parallel_jobs,
            "output_name_template": self.form.output_name_template,
            "default_target_size_mode": self.form.target_size_mode,
            "resource_governor": self.form.resource_governor,
            "use_hardware_accel": self.form.effective_hw(),
        }
        if self.form.quick_name:
            payload["quick_compress_profile"] = self.form.quick_name
        elif self._selected_profile:
            payload["default_profile"] = self._selected_profile
        try:
            get_config_manager().update_config(**payload)
        except Exception:
            logger.debug("Could not store encode settings", exc_info=True)

    def _launch_next(self) -> None:
        profile = self._current_profile
        if profile is None:
            return
        compressor = self._get_compressor()
        queued = self._active + len(self._pending)
        try:
            slots = compressor.queue_slots(
                profile,
                requested_parallel=self.form.parallel_jobs,
                queued_files=max(1, queued),
                media_info=self._first_video_info(),
            )
        except Exception:
            slots = 1
        slots = max(1, int(slots or 1))
        while self._pending and self._active < slots:
            key = self._pending.pop(0)
            item = self._files.get(key)
            if item is None:
                continue
            in_path = Path(item["path"])
            job_profile = CompressionProfile.from_dict(profile.to_dict())
            out_path = output_path_for(
                in_path,
                job_profile,
                self._output_folder,
                self.form.output_name_template,
            )
            job_id = compressor.compress_async(
                input_file=in_path,
                output_file=out_path,
                profile=job_profile,
                callback=lambda result: self._queue.put(("result", result)),
                parallel_jobs=slots,
            )
            self._active += 1
            row = _ProgressRow(job_id, in_path.name, self._cancel_one)
            self._progress_rows[job_id] = row
            self._rows.addWidget(row)
            job = compressor.get_job(job_id)
            if job is not None:
                row.update_job(job, awaiting=job_id in self._awaiting_fallback)
        self._queue_label.setText("Compressing…")

    def _cancel_one(self, job_id: str) -> None:
        if self._compressor is not None:
            self._compressor.cancel_job(job_id)

    def cancel_all(self) -> None:
        if not self._running:
            return
        self._cancel_requested = True
        self._pending.clear()
        self._cancel_btn.setEnabled(False)
        compressor = self._compressor
        if compressor is None:
            self._finish()
            return
        for job_id in list(self._progress_rows):
            compressor.cancel_job(job_id)
        try:
            active = compressor.get_active_jobs()
        except Exception:
            active = []
        if not active and self._active == 0:
            self._finish()

    def _pump(self) -> None:
        try:
            while True:
                self._handle(self._queue.get_nowait())
        except queue.Empty:
            pass

    def _handle(self, message) -> None:
        if handle_software_fallback_queue_message(self, message):
            return
        kind = message[0]
        if kind == "hw_probe":
            self.apply_hardware_probe(message[1])
            return
        if kind == "external_open":
            _kind, paths = message
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self.add_paths([str(path) for path in paths])
        elif kind == "info":
            _kind, key, info = message
            item = self._files.get(key)
            if item is None:
                return
            item["info"] = info
            row = item.get("row")
            if row is not None and hasattr(row, "set_info"):
                row.set_info(info)
            self._refresh_preview()
        elif kind == "progress":
            _kind, job = message
            job_id = getattr(job, "id", "")
            row = self._progress_rows.get(job_id)
            if row is None and self._running:
                title = Path(getattr(job, "input_file", "file")).name
                row = _ProgressRow(job_id, title, self._cancel_one)
                self._progress_rows[job_id] = row
                self._rows.addWidget(row)
            if row is not None:
                row.update_job(job, awaiting=job_id in self._awaiting_fallback)
                self._hw_card.set_activity(
                    human_progress_status(
                        job,
                        awaiting_software_fallback=job_id in self._awaiting_fallback,
                    )
                )
        elif kind == "result":
            _kind, result = message
            self._results.append(result)
            self._active = max(0, self._active - 1)
            self._record_history(result)
            self._launch_next()
            self._refresh_preview()
            if not self._pending and self._active == 0:
                self._finish()

    def _record_history(self, result) -> None:
        if not getattr(result, "success", False) or getattr(result, "skipped", False):
            return
        if not getattr(result, "output_file", None):
            return
        try:
            profile_name = self._current_profile.name if self._current_profile else ""
            codec_name = getattr(result, "video_codec", "") or (
                self._current_profile.video_codec.value if self._current_profile else ""
            )
            self._history.add_entry(
                HistoryEntry(
                    input_path=str(result.input_file),
                    output_path=str(result.output_file),
                    original_size_bytes=int(getattr(result, "original_size", 0) or 0),
                    compressed_size_bytes=int(getattr(result, "compressed_size", 0) or 0),
                    reduction_pct=float(getattr(result, "reduction_percent", 0) or 0),
                    codec=str(codec_name),
                    profile_name=profile_name,
                    duration_seconds=round(float(getattr(result, "encoding_time", 0) or 0), 1),
                    timestamp=datetime.now().isoformat(timespec="seconds"),
                )
            )
        except Exception:
            logger.debug("Could not record history", exc_info=True)

    def _set_software_fallback_wait(self, request, waiting: bool) -> None:
        hw = str(getattr(request, "hw_encoder", "") or "")
        matched: list[str] = []
        compressor = self._compressor
        for job_id, row in self._progress_rows.items():
            job = None
            if compressor is not None:
                try:
                    job = compressor.get_job(job_id)
                except Exception:
                    job = None
            encoder = str(getattr(job, "encoder_name", "") or "")
            if not hw or encoder == hw or job is None:
                matched.append(job_id)
                _ = row
        if waiting:
            self._awaiting_fallback.update(matched)
        else:
            for job_id in matched:
                self._awaiting_fallback.discard(job_id)
        for job_id in matched:
            row = self._progress_rows.get(job_id)
            job = compressor.get_job(job_id) if compressor is not None else None
            if row is not None and job is not None:
                row.update_job(job, awaiting=job_id in self._awaiting_fallback)

    def _finish(self) -> None:
        self._running = False
        self._compress_btn.setEnabled(True)
        self._clear_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._current_profile = None
        ok = sum(
            1
            for result in self._results
            if getattr(result, "success", False) and not getattr(result, "skipped", False)
        )
        skipped = sum(1 for result in self._results if getattr(result, "skipped", False))
        cancelled = sum(1 for result in self._results if getattr(result, "cancelled", False))
        failed = len(self._results) - ok - skipped - cancelled
        if self._cancel_requested:
            message = f"Cancelled — {cancelled} stopped"
            if ok:
                message += f", {ok} completed"
        else:
            message = f"Done — {ok} compressed"
            if skipped:
                message += f", {skipped} skipped"
        if failed:
            message += f", {failed} failed"
        summary = with_declined_fallback_summary(message, self._results)
        self._queue_label.setText(summary)
        self._hw_card.set_activity(summary)
        self._clear_rows()
        for result in self._results:
            if getattr(result, "cancelled", False):
                continue
            self._rows.addWidget(_ResultRow(result))
        self._files.clear()
        self._open_output_folder()
        cancelled_run = self._cancel_requested
        self._cancel_requested = False
        if not cancelled_run:
            self._notify_done(ok, len(self._results))

    def _open_output_folder(self) -> None:
        successful = [
            result
            for result in self._results
            if getattr(result, "success", False) and getattr(result, "output_file", None)
        ]
        if not successful:
            return
        if os.name != "nt":
            return
        folder = Path(successful[-1].output_file).parent
        try:
            os.startfile(folder)  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Could not open the output folder", exc_info=True)

    def _notify_done(self, ok: int, total: int) -> None:
        if ok <= 0 or os.name != "nt":
            return
        if not get_config().notify_on_completion:
            return
        try:
            from winotify import Notification
        except ImportError:
            return
        try:
            icon = _window_icon_path()
            toast = Notification(
                app_id=APP_NAME,
                title="Compression Complete",
                msg=f"{ok}/{total} files compressed",
                icon=str(icon) if icon else "",
            )
            toast.show()
        except Exception:
            logger.debug("Toast notification failed", exc_info=True)

    def _open_help_menu(self) -> None:
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        menu.addAction(HELP_TRAY, self._explain_tray)
        menu.addSeparator()
        for label, url in help_menu_items():
            menu.addAction(label, lambda target=url: webbrowser.open(target))
        button = self.sender()
        if button is not None and hasattr(button, "mapToGlobal"):
            menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        else:
            menu.exec(self.mapToGlobal(self.rect().topRight()))

    def _explain_tray(self) -> None:
        QMessageBox.information(self, f"{APP_NAME} tray", TRAY_HELP)

    def _open_about(self) -> None:
        show_about(
            self,
            f"About {APP_NAME}",
            (
                f"{APP_NAME} {APP_VERSION}\n\n"
                "Desktop media compression. Source-available under the "
                "PolyForm Noncommercial License 1.0.0. Commercial rights "
                "are reserved to Rjwolfe44.\n\n"
                f"Downloads: {RELEASES_URL}"
            ),
        )

    def _on_ipc_open(self, paths: list[Path]) -> None:
        self._queue.put(("external_open", list(paths)))

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            _polish_flag(self._drop, "active", True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        _polish_flag(self._drop, "active", False)
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        _polish_flag(self._drop, "active", False)
        paths = []
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                paths.append(local)
        self.add_paths(paths)
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        try:
            self._ipc.stop()
        except Exception:
            pass
        try:
            if get_config().remember_window_size:
                get_config_manager().update_config(
                    window_width=self.width(),
                    window_height=self.height(),
                )
        except Exception:
            logger.debug("Could not store the window size", exc_info=True)
        super().closeEvent(event)


def run_app(startup_files: Optional[list[Path]] = None, auto_start: bool = False) -> int:
    """Start the Qt shell and run its event loop."""

    from PySide6.QtGui import QFont

    from .theme import apply_theme

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Segoe UI", 10))
    apply_theme(app)
    window = SquishItWindow(startup_files=startup_files, auto_start=auto_start)
    window.show()
    return app.exec()
