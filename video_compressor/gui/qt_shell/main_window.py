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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...config import APP_NAME, get_config, get_config_manager
from ...core.clips import ClipPlan, expand_clip_jobs
from ...core.history import HistoryEntry, HistoryManager
from ...core.profiles import (
    QUICK_COMPRESS_ORDER,
    CompressionProfile,
    ProfileManager,
    build_quick_compress_profiles,
    describe_target_size_plan,
    normalize_quick_compress_name,
)
from ...core.utils import (
    SUPPORTED_MEDIA_EXTENSIONS,
    collect_media_files,
)
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
    TRIM_HINT,
    help_menu_items,
    human_progress_status,
)
from ..hw_status import (
    HardwareProbe,
    describe_hw_status,
    probe_from_compressor,
    status_summary,
)
from ..ipc import SingleInstanceServer
from ..software_fallback_dialog import (
    bind_main_window_software_fallback,
    handle_software_fallback_queue_message,
    with_declined_fallback_summary,
)
from .about_dialog import AboutDialog, UpdateBanner, start_update_check
from .dialogs import qt_yes_no_ask
from .encode_form import EncodeForm, output_path_for
from .history_dialog import HistoryDialog
from .hw_card import HardwareStatusCard
from .rows import (
    FileRow,
    ProgressRow,
    ResultRow,
    RowActions,
    clip_summary,
    pixmap_from_rgb,
    reveal_in_folder,
    section_header,
)
from .settings_pane import SettingsPane, scroll_wrap
from .stats_dialog import StatsDialog
from .theme import DARK_STYLESHEET

logger = logging.getLogger(__name__)

_MEDIA_FILTER = "Media (" + " ".join(
    f"*{ext}" for ext in sorted(SUPPORTED_MEDIA_EXTENSIONS)
) + ")"


@dataclass
class _JobSpec:
    """One encode to start: a file, plus the clip plan for this encode (if any)."""

    key: str
    path: Path
    plan: Optional[ClipPlan] = None
    token: str = ""
    title: str = ""


def _window_icon_path() -> Optional[Path]:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    icon_path = base_dir / "assets" / "squishit.ico"
    return icon_path if icon_path.exists() else None


def _polish_flag(widget, name: str, value: bool) -> None:
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


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
        self._progress_rows: dict[str, ProgressRow] = {}
        self._job_titles: dict[str, str] = {}
        self._results: list = []
        self._pending: list[_JobSpec] = []
        self._active = 0
        self._running = False
        self._cancel_requested = False
        self._awaiting_fallback: set[str] = set()
        self._hw_probe: Optional[HardwareProbe] = None
        self._full_profile_active = False
        self._quick_buttons: dict[str, QPushButton] = {}
        self._output_folder: Optional[Path] = self._saved_output_folder()
        self._preview = None
        self._update_info = None
        self._update_check = None
        self._current_profile: Optional[CompressionProfile] = None
        self._selected_profile = get_config().default_profile or "Balanced"
        self.form = EncodeForm(
            target_size_mode=get_config().default_target_size_mode,
            resource_governor=get_config().resource_governor,
            parallel_jobs=max(1, int(get_config().max_parallel_jobs or 1)),
            output_name_template=get_config().output_name_template,
            use_hw_accel=bool(get_config().use_hardware_accel),
            queue_order=get_config().batch_queue_order or "manual",
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
        history_btn = QPushButton("History")
        history_btn.clicked.connect(self._open_history)
        header.addWidget(history_btn)
        more_btn = QPushButton("More \u25be")
        more_btn.clicked.connect(self._open_help_menu)
        header.addWidget(more_btn)
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

        self._update_banner = UpdateBanner()
        queue_layout.addWidget(self._update_banner)

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
        trim_hint = QLabel(TRIM_HINT)
        trim_hint.setObjectName("hint")
        trim_hint.setWordWrap(True)
        drop_layout.addWidget(trim_hint)
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

        # Finished jobs, then running jobs, then the files themselves. The files
        # stay put through a run so trims and settings survive it.
        self._results_box = QVBoxLayout()
        self._results_box.setSpacing(8)
        queue_layout.addLayout(self._results_box)
        self._activity = QVBoxLayout()
        self._activity.setSpacing(8)
        queue_layout.addLayout(self._activity)
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
        self._install_shortcuts()
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

    def _row_actions(self) -> RowActions:
        return RowActions(
            remove=self.remove_file,
            trim=self.open_trim_editor,
            clear_trim=self.clear_trim,
            move_top=self.move_to_top,
            compress_only=self.compress_only,
        )

    def _add_file(self, path: Path, key: str) -> None:
        row = FileRow(path, self._row_actions())
        self._files[key] = {"path": path, "row": row, "info": None, "clips": None}
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

    def move_to_top(self, path: Path) -> None:
        if self._running:
            return
        key = str(Path(path).resolve())
        item = self._files.pop(key, None)
        if item is None:
            return
        self._files = {key: item, **self._files}
        row = item.get("row")
        if row is not None:
            self._rows.removeWidget(row)
            self._rows.insertWidget(0, row)

    def compress_only(self, path: Path) -> None:
        """Run just this file (the others stay queued for next time)."""

        if self._running:
            return
        key = str(Path(path).resolve())
        if key not in self._files:
            return
        self._start([key])

    # -- trimming -------------------------------------------------------

    def open_trim_editor(self, path: Path) -> None:
        """Open the clip editor for one video and keep what the user saves."""

        if self._running:
            return
        key = str(Path(path).resolve())
        item = self._files.get(key)
        info = item.get("info") if item else None
        if item is None or info is None or not hasattr(info, "video_codec"):
            return
        from .trim_editor import TrimEditorDialog

        dialog = TrimEditorDialog(
            self,
            Path(item["path"]),
            info,
            item.get("clips"),
            self._get_preview(),
            target_note=self._target_note(),
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.set_clips(key, dialog.result_plan())

    def set_clips(self, key: str, plan: Optional[ClipPlan]) -> None:
        item = self._files.get(key)
        if item is None:
            return
        item["clips"] = plan
        row = item.get("row")
        if row is not None:
            row.set_clip_summary(clip_summary(plan))
        self._refresh_preview()

    def clear_trim(self, path: Path) -> None:
        if self._running:
            return
        self.set_clips(str(Path(path).resolve()), None)

    def _target_note(self) -> str:
        """Plain-words reminder of a size target, shown in the trim editor."""

        if not self.form.target_size_enabled or not self.form.target_size_mb:
            return ""
        return f"Target size: {self.form.target_size_mb} MB per file"

    def _get_preview(self):
        """Frame/preview helper shared by the trim editor and the row thumbnails."""

        if self._preview is None:
            from ...core.preview import PreviewService

            compressor = self._get_compressor()
            ffmpeg = getattr(compressor, "ffmpeg_path", None) or "ffmpeg"
            ffprobe = getattr(compressor, "ffprobe_path", None)
            self._preview = PreviewService(str(ffmpeg), str(ffprobe) if ffprobe else None)
        return self._preview

    def _on_file_info(self, key: str, item: dict, info) -> None:
        """A file finished analysis: ask for its row thumbnail."""

        if info is None or not hasattr(info, "video_codec"):
            return
        try:
            from ...core.preview import display_size

            width, height = display_size(
                int(info.width or 0) or 16,
                int(info.height or 0) or 9,
                int(getattr(info, "rotation", 0) or 0),
                str(getattr(info, "sar", "1:1") or "1:1"),
                224,
                126,
            )
            moment = min(1.0, max(0.0, float(info.duration or 0) * 0.1))
            self._get_preview().request_poster(
                Path(item["path"]),
                moment,
                (width, height),
                lambda _path, w, h, rgb, _key=key: self._queue.put(
                    ("thumb", _key, w, h, rgb)
                ),
            )
        except Exception:
            logger.debug("Could not request a thumbnail", exc_info=True)

    # -- small helpers --------------------------------------------------

    @staticmethod
    def _saved_output_folder() -> Optional[Path]:
        saved = (get_config().last_output_folder or "").strip()
        if saved and Path(saved).is_dir():
            return Path(saved)
        return None

    def _install_shortcuts(self) -> None:
        for sequence, handler in (
            ("Ctrl+O", self._pick_files),
            ("Ctrl+Shift+O", self._pick_folder),
            ("Ctrl+Return", self.start_compression),
            ("Ctrl+Enter", self.start_compression),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _clear_rows(self) -> None:
        """Remove file rows, running jobs and results (everything in the queue)."""

        self._clear_layout(self._rows)
        self._clear_activity()
        self._clear_results()

    def _clear_activity(self) -> None:
        self._clear_layout(self._activity)
        self._progress_rows.clear()

    def _clear_results(self) -> None:
        self._clear_layout(self._results_box)

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
            _MEDIA_FILTER,
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
        try:
            get_config_manager().update_config(last_output_folder=str(folder))
        except Exception:
            logger.debug("Could not store the output folder", exc_info=True)

    def _refresh_output_button(self) -> None:
        if self._output_folder is None:
            self._output_btn.setText("Output: next to source")
        else:
            self._output_btn.setText(f"Output: {self._output_folder.name}")

    def _ordered_keys(self) -> list[str]:
        """File keys in run order: as listed, or sorted by size/length if asked."""

        keys = list(self._files)
        order = str(
            getattr(self.form, "queue_order", None) or get_config().batch_queue_order
        )
        if order == "manual" or len(keys) < 2:
            return keys

        def _size(key: str) -> int:
            info = self._files[key].get("info")
            if info is not None and getattr(info, "size", 0):
                return int(info.size)
            path = Path(self._files[key]["path"])
            try:
                return path.stat().st_size
            except OSError:
                return 0

        def _length(key: str) -> float:
            info = self._files[key].get("info")
            return float(getattr(info, "duration", 0.0) or 0.0) if info else 0.0

        if order in {"largest-first", "smallest-first"}:
            return sorted(keys, key=_size, reverse=order == "largest-first")
        if order in {"longest-first", "shortest-first"}:
            return sorted(keys, key=_length, reverse=order == "longest-first")
        return keys

    def _build_job_specs(self, keys: list[str]) -> list[_JobSpec]:
        """One spec per encode: a file becomes N specs when it has N separate clips."""

        specs: list[_JobSpec] = []
        for key in keys:
            item = self._files.get(key)
            if item is None:
                continue
            path = Path(item["path"])
            info = item.get("info")
            plan = item.get("clips")
            if info is not None and not hasattr(info, "video_codec"):
                plan = None  # images have no timeline
            for job in expand_clip_jobs(plan):
                title = f"{path.name} · {job.title}" if job.title else path.name
                specs.append(_JobSpec(key, path, job.plan, job.token, title))
        return specs

    def _set_running(self, running: bool) -> None:
        """One place for the busy/idle look of the buttons and file rows."""

        self._running = running
        self._compress_btn.setEnabled(not running)
        self._clear_btn.setEnabled(not running)
        self._cancel_btn.setEnabled(running)
        for item in self._files.values():
            row = item.get("row")
            if row is not None:
                row.set_busy(running)
        if not running:
            self._refresh_action_label()

    def start_compression(self) -> None:
        self._start(self._ordered_keys())

    def _start(self, keys: list[str]) -> None:
        if self._running or not self._files or not keys:
            if not self._files:
                self._queue_label.setText("Add a file, then press Quick Compress.")
            return
        self._cancel_requested = False
        self._results.clear()
        self._awaiting_fallback.clear()
        profile = self.job_profile()
        self._current_profile = profile
        self._set_running(True)
        self._persist_run_settings()
        self._clear_results()
        self._clear_activity()
        self._pending = self._build_job_specs(keys)
        self._active = 0
        try:
            self._launch_next()
        except Exception as exc:
            logger.exception("Could not start compression")
            self._set_running(False)
            QMessageBox.critical(self, APP_NAME, str(exc) or "Could not start compression")

    def _persist_run_settings(self) -> None:
        payload = {
            "max_parallel_jobs": self.form.parallel_jobs,
            "output_name_template": self.form.output_name_template,
            "default_target_size_mode": self.form.target_size_mode,
            "resource_governor": self.form.resource_governor,
            "use_hardware_accel": self.form.effective_hw(),
            "batch_queue_order": self.form.queue_order,
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
            spec = self._pending.pop(0)
            if spec.key not in self._files:
                continue
            in_path = spec.path
            job_profile = CompressionProfile.from_dict(profile.to_dict())
            out_path = output_path_for(
                in_path,
                job_profile,
                self._output_folder,
                self.form.output_name_template,
                clip_token=spec.token,
            )
            kwargs = dict(
                input_file=in_path,
                output_file=out_path,
                profile=job_profile,
                callback=lambda result: self._queue.put(("result", result)),
                parallel_jobs=slots,
                # Never replace a file that is already there; add " (2)" instead.
                overwrite=False,
            )
            if spec.plan is not None:
                kwargs["clips"] = spec.plan
            job_id = compressor.compress_async(**kwargs)
            self._active += 1
            self._job_titles[job_id] = spec.title or in_path.name
            row = ProgressRow(job_id, self._job_titles[job_id], self._cancel_one)
            self._progress_rows[job_id] = row
            self._activity.addWidget(row)
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
            if row is not None:
                try:
                    row.set_info(info)
                except RuntimeError:
                    # The row was removed while the file was still being read.
                    logger.debug("Row for %s is gone", key)
            self._on_file_info(key, item, info)
            self._refresh_preview()
        elif kind == "thumb":
            _kind, key, width, height, rgb = message
            item = self._files.get(key)
            row = item.get("row") if item else None
            if row is not None and rgb:
                try:
                    row.set_thumbnail(pixmap_from_rgb(rgb, width, height))
                except RuntimeError:
                    pass
        elif kind == "progress":
            _kind, job = message
            job_id = getattr(job, "id", "")
            row = self._progress_rows.get(job_id)
            if row is None and self._running:
                title = self._job_titles.get(job_id) or Path(
                    getattr(job, "input_file", "file")
                ).name
                row = ProgressRow(job_id, title, self._cancel_one)
                self._progress_rows[job_id] = row
                self._activity.addWidget(row)
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
        self._set_running(False)
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
        # Running jobs turn into a results list; the files stay queued so their
        # trims survive and the same set can be run again with other settings.
        self._clear_activity()
        self._clear_results()
        shown = [r for r in self._results if not getattr(r, "cancelled", False)]
        if shown:
            open_btn = QPushButton("Show in folder")
            open_btn.clicked.connect(self._open_output_folder)
            self._results_box.addWidget(section_header("Results", extra=open_btn))
            for result in shown:
                self._results_box.addWidget(ResultRow(result))
        if get_config().open_folder_when_done:
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
        reveal_in_folder(Path(successful[-1].output_file))

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
        menu = QMenu(self)
        menu.addAction("Statistics\u2026", self._open_stats)
        menu.addAction("Custom profiles\u2026", self._open_profile_editor)
        menu.addSeparator()
        menu.addAction(HELP_TRAY, self._explain_tray)
        for label, url in help_menu_items():
            menu.addAction(label, lambda target=url: webbrowser.open(target))
        menu.addSeparator()
        menu.addAction("Check for updates\u2026", self._open_about)
        menu.addAction(f"About {APP_NAME}", self._open_about)
        button = self.sender()
        if button is not None and hasattr(button, "mapToGlobal"):
            menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        else:
            menu.exec(self.mapToGlobal(self.rect().topRight()))

    def _explain_tray(self) -> None:
        QMessageBox.information(self, f"{APP_NAME} tray", TRAY_HELP)

    def _open_about(self) -> None:
        AboutDialog(
            self,
            hardware_summary=self._hw_card.found.text(),
            update_info=self._update_info,
        ).exec()

    def _open_history(self) -> None:
        HistoryDialog(self, history=self._history).exec()

    def _open_stats(self) -> None:
        StatsDialog(self, history=self._history).exec()

    def _open_profile_editor(self) -> None:
        from .profile_editor import ProfileEditorDialog

        ProfileEditorDialog(
            self,
            profile_manager=self._profile_manager,
            on_changed=self._profiles_changed,
        ).exec()

    def _profiles_changed(self) -> None:
        """Custom profiles were saved, deleted or imported: refresh the menu."""

        self._settings.set_profiles(self._profile_manager.get_all_profiles())
        if self._full_profile_active:
            # The profile in use may have been edited or removed.
            if self._profile_manager.get_profile(self._selected_profile) is not None:
                self.select_profile(self._selected_profile)
            else:
                self.select_quick(get_config().quick_compress_profile or "Quick Lite")
        else:
            self._settings.set_active_quick(self.form.quick_name)

    def check_for_updates_on_startup(self) -> None:
        """Quietly look for a newer version; a banner appears only if there is one."""

        if not get_config().check_updates_on_startup:
            return
        self._update_check = start_update_check(self, self._on_update_result)

    def _on_update_result(self, info) -> None:
        self._update_info = info
        self._update_banner.set_update(info)

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
        compressor = self._compressor
        if self._running and compressor is not None:
            try:
                encoding = bool(compressor.get_active_jobs())
            except Exception:
                encoding = False
            if encoding:
                answer = QMessageBox.question(
                    self,
                    APP_NAME,
                    "Compression is still running. Stop it and close?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
            # Never leave an ffmpeg process behind when the window goes away.
            self._pending.clear()
            for job_id in list(self._progress_rows):
                try:
                    compressor.cancel_job(job_id)
                except Exception:
                    logger.debug("Could not cancel %s", job_id, exc_info=True)
        if self._preview is not None:
            try:
                self._preview.shutdown()
            except Exception:
                logger.debug("Preview shutdown failed", exc_info=True)
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
    window.check_for_updates_on_startup()
    return app.exec()
