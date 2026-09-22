"""Main application window for SquishIt."""

import customtkinter as ctk
import tkinterdnd2
from tkinter import filedialog, messagebox, font as tkfont
from pathlib import Path
from typing import Optional, List, Dict, Any
import os
import subprocess
import sys
import threading
import queue
import logging
import time
from datetime import datetime, timezone, timedelta

from .widgets import (
    DropZone, FileQueueCard, ProgressCard,
    SettingsPanel, ProfileBar, ResultCard, COLORS, HardwareBadge,
)
from .ipc import SingleInstanceServer
from .about_dialog import AboutDialog
from .history_dialog import HistoryDialog
from .profile_editor import ProfileEditorDialog
from .stats_dialog import StatsDialog
from .scaling import (
    apply_customtkinter_scale,
    center_window,
    resolve_main_window_layout,
    resolve_ui_scale,
    scaled,
)
from ..core.profiles import CompressionProfile, ProfileManager, describe_target_size_plan
from ..core.hardware import get_hardware_detector
from ..core.codecs import VideoCodec
from ..core.utils import collect_media_files, detect_media_type, format_size, format_time, get_audio_extension, get_image_extension, get_video_extension, sanitize_filename
from ..core.history import HistoryManager, HistoryEntry
from ..config import APP_NAME, APP_VERSION, get_config, get_config_manager

try:
    from winotify import Notification
    _HAS_WINOTIFY = True
except ImportError:
    _HAS_WINOTIFY = False

logger = logging.getLogger(__name__)

_SPLASH_MIN_SECONDS = 0.45
_SCALE_TRANSITION_MIN_SECONDS = 0.2


def _window_icon_path() -> Optional[Path]:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    icon_path = base_dir / "assets" / "squishit.ico"
    return icon_path if icon_path.exists() else None


class _StartupSplash(ctk.CTkToplevel):
    """Small splash shown while the main window builds off-screen."""

    def __init__(
        self,
        master,
        ui_scale: float,
        *,
        title_text: str = APP_NAME,
        status_text: str = "Loading profiles, hardware, and codec support…",
        footer_text: Optional[str] = None,
    ):
        super().__init__(master, fg_color=COLORS["bg"])
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except Exception:
            pass

        width = scaled(420, ui_scale)
        height = scaled(180, ui_scale)

        shell = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=18)
        shell.pack(fill="both", expand=True, padx=2, pady=2)

        content = ctk.CTkFrame(shell, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=24, pady=22)

        ctk.CTkLabel(
            content,
            text=title_text,
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=COLORS["text"],
        ).pack(anchor="w")

        ctk.CTkLabel(
            content,
            text=status_text,
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_dim"],
            anchor="w",
        ).pack(anchor="w", pady=(6, 16))

        self._bar = ctk.CTkProgressBar(
            content,
            mode="indeterminate",
            height=scaled(10, ui_scale),
            corner_radius=scaled(8, ui_scale),
            fg_color=COLORS["progress_track"],
            progress_color=COLORS["accent"],
        )
        self._bar.pack(fill="x")
        self._bar.start()

        if footer_text is None:
            footer_text = f"v{APP_VERSION}"

        if footer_text:
            ctk.CTkLabel(
                content,
                text=footer_text,
                font=ctk.CTkFont(size=11),
                text_color=COLORS["text_muted"],
                anchor="w",
            ).pack(anchor="w", pady=(14, 0))

        center_window(self, width=width, height=height)

    def close(self) -> None:
        try:
            self._bar.stop()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass


class MainWindow(tkinterdnd2.Tk):
    """Main application window."""

    def __init__(self, startup_files: Optional[List[Path]] = None, auto_start: bool = False):
        super().__init__()

        # Core components
        self.compressor: Optional[Any] = None
        self.profile_manager: Optional[ProfileManager] = None
        self.hw_detector = None
        self.app_config = get_config()
        if self.app_config.history_max_entries > 100:
            get_config_manager().update_config(history_max_entries=100)
            self.app_config.history_max_entries = 100

        # App state
        self.video_files: Dict[str, dict] = {}
        self.active_jobs: Dict[str, ProgressCard] = {}
        self.results: List[Any] = []
        self.is_compressing = False
        self._compare_mode = False
        self._q: queue.Queue = queue.Queue()
        self.output_folder: Optional[Path] = None
        self.startup_files = startup_files or []
        self.auto_start = auto_start
        self._ipc_server = SingleInstanceServer(self._handle_ipc_open)
        self._update_banner: Optional[ctk.CTkFrame] = None
        self._pump_after_id: Optional[str] = None
        self._cancel_requested = False
        self._splash: Optional[_StartupSplash] = None
        self._splash_started_at: float = 0.0
        self._scale_transition_active = False
        self._ui_scale_mode = getattr(self.app_config, "ui_scale_mode", "auto")
        self._ui_scale = resolve_ui_scale(self._ui_scale_mode, self.winfo_screenheight())

        # Parallel compression state
        self._pending_files: List[str] = []
        self._active_job_count: int = 0
        self._current_profile: Optional[CompressionProfile] = None

        # Hardware info for About dialog
        self._hw_info: Optional[Any] = None

        # History
        self._history = HistoryManager(max_entries=self.app_config.history_max_entries)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        apply_customtkinter_scale(self._ui_scale)

        configured_width = self.app_config.window_width or None
        configured_height = self.app_config.window_height or None
        if (configured_width, configured_height) in {(1200, 800), (2240, 1480)}:
            configured_width = None
            configured_height = None

        layout = resolve_main_window_layout(
            self.winfo_screenwidth(),
            self.winfo_screenheight(),
            self._ui_scale,
            configured_width=configured_width,
            configured_height=configured_height,
        )

        self.title(APP_NAME)
        self.geometry(f"{layout.width}x{layout.height}")
        self.minsize(layout.min_width, layout.min_height)
        self.tk_setPalette(background=COLORS["bg"])
        self._apply_icon()
        self.withdraw()
        self._splash = _StartupSplash(self, self._ui_scale)
        self._splash_started_at = time.perf_counter()
        try:
            self._splash.lift()
            self._splash.update_idletasks()
            self._splash.update()
        except Exception:
            pass

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(10, self._bootstrap_startup)

    def _apply_icon(self):
        icon_path = _window_icon_path()
        if not icon_path:
            return
        try:
            self.iconbitmap(str(icon_path))
        except Exception:
            pass

    def _get_compressor(self):
        if self.compressor is None:
            from ..core.compressor import VideoCompressor

            self.compressor = VideoCompressor()
            self.compressor.set_progress_callback(
                lambda job: self._q.put(("progress", job))
            )
        return self.compressor

    def _detect_hw_async(self) -> None:
        detector = self._get_hw_detector()
        info = detector.info
        self._q.put(("hw_ready", info))

    def _get_hw_detector(self):
        if self.hw_detector is None:
            self.hw_detector = get_hardware_detector()
        return self.hw_detector

    def _get_profile_manager(self) -> ProfileManager:
        if self.profile_manager is None:
            self.profile_manager = ProfileManager()
        return self.profile_manager

    def _check_update_async(self) -> None:
        """Run in a background thread — throttle is handled inside check_for_update."""
        cfg = get_config_manager().config
        if not cfg.check_updates_on_startup:
            return
        from ..core.updater import check_for_update
        info = check_for_update(APP_VERSION)
        if info:
            self._q.put(("update_available", info))

    def _handle_ipc_open(self, paths: list[Path]) -> None:
        if paths:
            self._q.put(("external_open", [str(path) for path in paths]))

    def _show_update_banner(self, info) -> None:
        """Attach a slim banner at the top of the window advertising the new release."""
        if self._update_banner:
            return  # already showing

        banner = ctk.CTkFrame(
            self,
            fg_color="#1a3a5c",
            corner_radius=0,
            height=44,
        )
        banner.pack(side="top", fill="x", before=self.winfo_children()[0])
        banner.pack_propagate(False)
        self._update_banner = banner

        ctk.CTkLabel(
            banner,
            text=f"\u2B06  SquishIt v{info.latest_version} is available!",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#ffffff",
        ).pack(side="left", padx=16)

        def _dismiss():
            banner.destroy()
            self._update_banner = None

        def _download_and_install():
            dl_btn.configure(text="Downloading…", state="disabled")
            dismiss_btn.configure(state="disabled")

            def _run():
                from ..core.updater import download_update

                def _progress(done: int, total: int):
                    pct = int(done / total * 100) if total else 0
                    try:
                        dl_btn.configure(text=f"Downloading… {pct}%")
                    except Exception:
                        pass

                installer = download_update(info.download_url, progress_cb=_progress)
                if installer:
                    try:
                        dl_btn.configure(text="Installing…")
                    except Exception:
                        pass
                    subprocess.Popen(
                        [str(installer), "/VERYSILENT", "/NORESTART"],
                        creationflags=subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP,
                    )
                    self.after(1500, self.destroy)
                else:
                    try:
                        dl_btn.configure(text="Download failed", state="normal")
                        dismiss_btn.configure(state="normal")
                    except Exception:
                        pass

            threading.Thread(target=_run, daemon=True).start()

        dismiss_btn = ctk.CTkButton(
            banner,
            text="✕",
            width=28, height=28, corner_radius=6,
            fg_color="transparent",
            hover_color="#2a4a6c",
            text_color="#ffffff",
            font=ctk.CTkFont(size=13),
            command=_dismiss,
        )
        dismiss_btn.pack(side="right", padx=(0, 8))

        dl_btn = ctk.CTkButton(
            banner,
            text="Download & Install",
            width=160, height=28, corner_radius=6,
            fg_color="#2f80ed",
            hover_color="#1a6bd6",
            text_color="#ffffff",
            font=ctk.CTkFont(size=12),
            command=_download_and_install,
        )
        dl_btn.pack(side="right", padx=(0, 6))

    #  Layout 

    def _build_ui(self):
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=20, pady=20)

        self._build_header(root)

        body = ctk.CTkFrame(root, fg_color="transparent")
        body.pack(fill="both", expand=True, pady=(10, 0))

        self._build_left(body)
        self._build_right(body)

    def _bootstrap_startup(self):
        self._build_ui()
        self._setup_dnd()
        self._pump_ui()
        self._ipc_server.start()
        threading.Thread(target=self._detect_hw_async, daemon=True).start()
        threading.Thread(target=self._check_update_async, daemon=True).start()
        self.after(20, self._finish_startup)

    def _finish_startup(self):
        if self._splash is not None:
            elapsed = time.perf_counter() - self._splash_started_at
            remaining = _SPLASH_MIN_SECONDS - elapsed
            if remaining > 0:
                self.after(max(1, int(remaining * 1000)), self._finish_startup)
                return

        self.update_idletasks()
        self._center()
        self.deiconify()
        self.lift()
        self.focus_force()
        if self._splash is not None:
            self._splash.close()
            self._splash = None

        if self.startup_files:
            self.after(80, lambda: self._on_files_dropped([str(path) for path in self.startup_files]))
            if self.auto_start:
                self.after(550, self._start_compression)

    def _setup_dnd(self):
        """Register drag-and-drop handlers on the root window."""
        try:
            from tkinterdnd2 import DND_FILES
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_dnd_drop)
        except Exception:
            # Silently fail if tkinterdnd2 isn't available
            pass

    def _on_dnd_drop(self, event):
        """Handle files dropped onto the window."""
        import re
        raw = event.data or ""
        paths = re.findall(r"\{([^}]+)\}", raw)
        remaining = re.sub(r"\{[^}]+\}", "", raw).split()
        paths += [p for p in remaining if p.strip()]
        if paths:
            self._on_files_dropped([p.strip() for p in paths if p.strip()])

    def _build_header(self, parent):
        hdr = ctk.CTkFrame(
            parent,
            fg_color=COLORS["surface"],
            corner_radius=18,
            border_width=1,
            border_color=COLORS["border"],
        )
        hdr.pack(fill="x")

        shell = ctk.CTkFrame(hdr, fg_color="transparent")
        shell.pack(fill="x", padx=16, pady=12)

        top_row = ctk.CTkFrame(shell, fg_color="transparent", height=44)
        top_row.pack(fill="x")
        top_row.pack_propagate(False)

        brand_row = ctk.CTkFrame(top_row, fg_color="transparent")
        brand_row.pack(side="left", fill="y")

        ctk.CTkLabel(
            brand_row,
            text=APP_NAME,
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLORS["text"],
        ).pack(side="left")

        self._hw_badge = HardwareBadge(brand_row, None)
        self._hw_badge.pack(side="left", padx=(12, 0))

        actions_row = ctk.CTkFrame(top_row, fg_color="transparent")
        actions_row.pack(side="right")

        ctk.CTkButton(
            actions_row,
            text="Output Folder",
            width=120, height=30, corner_radius=8,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["border"],
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=12),
            command=self._pick_output_folder,
        ).pack(side="left")

        ctk.CTkButton(
            actions_row,
            text="History",
            width=76, height=30, corner_radius=8,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["border"],
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=12),
            command=self._open_history,
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            actions_row,
            text="About",
            width=70, height=30, corner_radius=8,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["border"],
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=12),
            command=self._open_about,
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            actions_row,
            text="Stats",
            width=64, height=30, corner_radius=8,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["border"],
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=12),
            command=self._open_stats,
        ).pack(side="left", padx=(8, 0))

        ctk.CTkFrame(shell, height=1, fg_color=COLORS["border"]).pack(fill="x", pady=(8, 8))

        meta_row = ctk.CTkFrame(shell, fg_color="transparent")
        meta_row.pack(fill="x")

        encoder_card = ctk.CTkFrame(
            meta_row,
            fg_color=COLORS["surface_raised"],
            corner_radius=12,
            height=66,
        )
        encoder_card.pack(side="left", fill="x", expand=True)
        encoder_card.pack_propagate(False)

        encoder_inner = ctk.CTkFrame(encoder_card, fg_color="transparent")
        encoder_inner.pack(fill="both", expand=True, padx=12, pady=8)

        ctk.CTkLabel(
            encoder_inner,
            text="ENCODER PLAN",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w")

        self._encoder_preview_lbl = ctk.CTkLabel(
            encoder_inner,
            text="Encoder: detecting…",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
            anchor="w",
            justify="left",
        )
        self._encoder_preview_lbl.pack(anchor="w", fill="x", pady=(4, 0))

        output_card = ctk.CTkFrame(
            meta_row,
            fg_color=COLORS["surface_raised"],
            corner_radius=12,
            width=250,
            height=66,
        )
        output_card.pack(side="left", padx=(12, 0))
        output_card.pack_propagate(False)

        output_inner = ctk.CTkFrame(output_card, fg_color="transparent")
        output_inner.pack(fill="both", expand=True, padx=12, pady=8)

        ctk.CTkLabel(
            output_inner,
            text="OUTPUT",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w")

        self._output_lbl = ctk.CTkLabel(
            output_inner,
            text="\u2192 Same folder as source",
            font=ctk.CTkFont(size=11),
            text_color=COLORS["text_dim"],
            anchor="w",
            justify="left",
        )
        self._output_lbl.pack(anchor="w", fill="x", pady=(4, 0))

        encoder_card.bind("<Configure>", lambda _event: self.after_idle(self._refresh_encoder_preview), add="+")

    def _build_left(self, parent):
        left = ctk.CTkFrame(parent, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True)

        bar = ctk.CTkFrame(left, fg_color="transparent", height=52)
        bar.pack(side="bottom", fill="x", pady=(12, 0))
        bar.pack_propagate(False)

        self._compress_btn = ctk.CTkButton(
            bar,
            text="Compress",
            height=42, corner_radius=10,
            fg_color=COLORS["accent"],
            hover_color=COLORS["accent_hover"],
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#ffffff",
            command=self._start_compression,
        )
        self._compress_btn.pack(side="left", fill="x", expand=True)

        self._compare_btn = ctk.CTkButton(
            bar,
            text="Compare Sample",
            width=130, height=42, corner_radius=10,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["border"],
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._compare_first,
        )
        self._compare_btn.pack(side="left", padx=(10, 0))

        self._cancel_btn = ctk.CTkButton(
            bar,
            text="Cancel",
            width=80, height=42, corner_radius=10,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["error"],
            text_color=COLORS["error"],
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._cancel_all,
        )
        # hidden until compression starts

        self._clear_btn = ctk.CTkButton(
            bar,
            text="Clear",
            width=80, height=42, corner_radius=10,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["error"],
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=13),
            command=self._clear_all,
        )
        self._clear_btn.pack(side="right", padx=(10, 0))

        self._drop_zone = DropZone(left, on_drop=self._on_files_dropped)
        self._drop_zone.pack(fill="x")

        self._queue_lbl = ctk.CTkLabel(
            left,
            text="No files added",
            font=ctk.CTkFont(size=12),
            text_color=COLORS["text_muted"],
        )
        self._queue_lbl.pack(anchor="w", pady=(12, 6))

        self._list = ctk.CTkScrollableFrame(
            left, fg_color="transparent",
            scrollbar_button_color=COLORS["surface_raised"],
            scrollbar_button_hover_color=COLORS["border"],
        )
        self._list.pack(fill="both", expand=True)

    def _build_right(self, parent):
        right = ctk.CTkFrame(
            parent,
            fg_color=COLORS["surface"],
            corner_radius=14,
            width=320,
        )
        right.pack(side="right", fill="y", padx=(16, 0))
        right.pack_propagate(False)

        inner = ctk.CTkFrame(right, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=16, pady=16)

        ctk.CTkLabel(
            inner,
            text="PROFILE",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w")

        self._profile_bar = ProfileBar(
            inner,
            profiles=self._get_profile_manager().get_all_profiles(),
            on_select=self._on_profile_select,
        )
        self._profile_bar.pack(fill="x", pady=(8, 0))

        ctk.CTkButton(
            inner,
            text="✎ Custom Profiles",
            height=26, corner_radius=6,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["border"],
            text_color=COLORS["accent"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._open_profile_editor,
        ).pack(fill="x", pady=(6, 0))

        ctk.CTkFrame(inner, height=1, fg_color=COLORS["border"]).pack(
            fill="x", pady=14
        )

        ctk.CTkLabel(
            inner,
            text="SETTINGS",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=COLORS["text_muted"],
        ).pack(anchor="w")

        self._settings = SettingsPanel(
            inner,
            profiles=self._get_profile_manager().get_all_profiles(),
            on_change=self._on_settings_change,
            codec_manager=self._get_compressor().codec_manager,
            hw_vendor=self._get_hw_detector().info.preferred_hw_encoder,
        )
        self._settings.pack(fill="both", expand=True, pady=(8, 0))

        # Apply default profile
        self._profile_bar.set_selected("Balanced")
        self._refresh_runtime_previews()

    def _center(self):
        center_window(self)

    def _on_settings_change(self, settings: dict) -> None:
        scale_mode = settings.get("ui_scale_mode", self._ui_scale_mode)
        if scale_mode != self._ui_scale_mode:
            self._apply_ui_scale_mode(scale_mode)
        self._refresh_runtime_previews()

    def _apply_ui_scale_mode(self, scale_mode: str) -> None:
        if self._scale_transition_active or scale_mode == self._ui_scale_mode:
            return

        self._scale_transition_active = True
        previous_scale = self._ui_scale
        next_scale = resolve_ui_scale(scale_mode, self.winfo_screenheight())
        overlay = _StartupSplash(
            self,
            next_scale,
            title_text="Applying Scale",
            status_text="Refreshing the interface for the new UI scale…",
            footer_text="This should only take a moment.",
        )
        transition_started_at = time.perf_counter()
        try:
            overlay.lift()
            overlay.update_idletasks()
            overlay.update()
        except Exception:
            pass

        self.withdraw()
        self._ui_scale_mode = scale_mode
        self._ui_scale = next_scale
        apply_customtkinter_scale(self._ui_scale)

        scale_ratio = self._ui_scale / previous_scale if previous_scale else 1.0
        layout = resolve_main_window_layout(
            self.winfo_screenwidth(),
            self.winfo_screenheight(),
            self._ui_scale,
            configured_width=int(self.winfo_width() * scale_ratio),
            configured_height=int(self.winfo_height() * scale_ratio),
        )
        self.minsize(layout.min_width, layout.min_height)
        self.geometry(f"{layout.width}x{layout.height}")
        self.update_idletasks()
        self._center()

        def _finish_scale_transition():
            self.deiconify()
            self.lift()
            self.focus_force()
            overlay.close()
            self._scale_transition_active = False

        remaining = _SCALE_TRANSITION_MIN_SECONDS - (time.perf_counter() - transition_started_at)
        if remaining > 0:
            self.after(max(1, int(remaining * 1000)), _finish_scale_transition)
        else:
            _finish_scale_transition()

        get_config_manager().update_config(
            ui_scale_mode=scale_mode,
            window_width=layout.width,
            window_height=layout.height,
        )

    #  File management 

    def _on_files_dropped(self, files: List[str]):
        for path in collect_media_files(files):
            if str(path) not in self.video_files:
                self._add_file(path)
        self._refresh_label()

    def _add_file(self, path: Path):
        card = FileQueueCard(
            self._list, filepath=str(path), on_remove=self._remove_file,
            on_compress_single=self._compress_single,
            on_compare_sample=self._compare_sample,
            on_move_top=self._move_to_top,
        )
        card.pack(fill="x", pady=(0, 8))
        self.video_files[str(path)] = {"card": card, "info": None}

        def _analyze():
            info = self._get_compressor().analyze_media(path)
            if info:
                self._q.put(("info", str(path), info))

        threading.Thread(target=_analyze, daemon=True).start()

    def _remove_file(self, path: Path):
        key = str(path)
        if key in self.video_files:
            self.video_files[key]["card"].destroy()
            del self.video_files[key]
        self._refresh_label()

    def _refresh_label(self):
        n = len(self.video_files)
        if n == 0:
            self._queue_lbl.configure(text="No files added")
            self._drop_zone.set_compact(False)
        elif n == 1:
            self._queue_lbl.configure(text="1 file queued")
            self._drop_zone.set_compact(True)
        else:
            self._queue_lbl.configure(text=f"{n} files queued")
            self._drop_zone.set_compact(True)

        if hasattr(self, "_encoder_preview_lbl"):
            self._refresh_encoder_preview()

    #  Profile / settings 

    def _on_profile_select(self, name: str):
        profile = self.profile_manager.get_profile(name)
        if self.profile_manager is None:
            profile = self._get_profile_manager().get_profile(name)
        if profile:
            self._settings.apply_profile(profile)
            self._refresh_runtime_previews()

    def _refresh_runtime_previews(self) -> None:
        self._refresh_encoder_preview()
        self._refresh_size_preview()
        self._refresh_recommendation()

    def _effective_parallel_jobs(self) -> int:
        settings = self._settings.get_settings() if hasattr(self, "_settings") else {}
        configured_jobs = max(
            1,
            int(settings.get("parallel_jobs", getattr(get_config(), "max_parallel_jobs", 1)) or 1),
        )

        if self.is_compressing:
            queued_jobs = self._active_job_count + len(getattr(self, "_pending_files", []))
            if queued_jobs > 0:
                return min(configured_jobs, queued_jobs)

        queued_files = len(self.video_files)
        if queued_files > 0:
            return min(configured_jobs, queued_files)
        return 1

    def _planned_parallel_jobs(self) -> int:
        configured_jobs = max(1, int(getattr(self, "_max_parallel", 1) or 1))
        queued_jobs = self._active_job_count + 1 + len(self._pending_files)
        return min(configured_jobs, max(1, queued_jobs))

    def _fit_encoder_preview_text(self, candidates: List[str]) -> str:
        label = self._encoder_preview_lbl
        available_width = label.winfo_width()
        if available_width <= 1:
            available_width = label.master.winfo_width() - 12
        if available_width <= 1:
            return candidates[-1]

        try:
            text_font = tkfont.Font(font=label.cget("font"))
            measure = text_font.measure
        except Exception:
            measure = lambda text: len(text) * 7

        for candidate in candidates:
            if measure(candidate) <= available_width:
                return candidate

        trimmed = candidates[-1]
        ellipsis = "…"
        while trimmed and measure(trimmed + ellipsis) > available_width:
            trimmed = trimmed[:-1]

        trimmed = trimmed.rstrip(" ./@-|")
        return (trimmed + ellipsis) if trimmed else ellipsis

    @staticmethod
    def _short_governor_label(governor: str) -> str:
        return {
            "balanced": "bal",
            "low": "low",
            "max": "max",
            "auto": "auto",
        }.get(governor, governor)

    def _first_analyzed_video_info(self):
        for item in self.video_files.values():
            info = item.get("info")
            if info and hasattr(info, "video_codec"):
                return info
        return None

    def _refresh_encoder_preview(self) -> None:
        if not hasattr(self, "_encoder_preview_lbl") or not hasattr(self, "_settings"):
            return

        settings = self._settings.get_settings()
        first_video_info = self._first_analyzed_video_info()

        try:
            profile = self._get_profile()
            optimized = self._get_compressor()._optimize_video_profile(profile, first_video_info)

            if optimized.output_mode == "audio":
                text = self._fit_encoder_preview_text([
                    f"Audio · {optimized.audio_codec.value}",
                    optimized.audio_codec.value,
                ])
            else:
                hw_encoder = None
                if optimized.use_hw_accel:
                    hw_encoder = self._get_compressor()._select_hw_encoder(optimized.video_codec)
                encoder_name = hw_encoder or optimized.video_codec.ffmpeg_encoder
                effective_parallel_jobs = self._effective_parallel_jobs()
                threads = self._get_compressor()._get_thread_count(
                    optimized,
                    optimized.video_codec,
                    hw_encoder,
                    parallel_jobs=effective_parallel_jobs,
                    media_info=first_video_info,
                )
                governor = settings.get("resource_governor", "auto")
                if governor == "auto":
                    governor = self._get_hw_detector().resolve_resource_governor(
                        governor="auto",
                        n_parallel=effective_parallel_jobs,
                        codec=optimized.video_codec.value,
                        hw_encoder=hw_encoder,
                        frame_height=getattr(first_video_info, "height", optimized.max_resolution or 0) or 0,
                    )
                short_governor = self._short_governor_label(governor)
                text = self._fit_encoder_preview_text([
                    f"{encoder_name} · {threads}T @ {effective_parallel_jobs}J · {governor}",
                    f"{threads}T @ {effective_parallel_jobs}J · {encoder_name} · {short_governor}",
                    f"{threads}T/{effective_parallel_jobs}J · {encoder_name} · {short_governor}",
                    f"{threads}T/{effective_parallel_jobs}J · {encoder_name}",
                ])

            self._encoder_preview_lbl.configure(text=text)
        except Exception:
            self._encoder_preview_lbl.configure(text="Encoder: preview unavailable")

    def _refresh_size_preview(self) -> None:
        if not hasattr(self, "_settings"):
            return

        settings = self._settings.get_settings()
        if not settings.get("target_size_mb") or settings.get("output_mode") != "video":
            self._settings.set_target_size_preview("")
            return

        first_video_info = self._first_analyzed_video_info()

        if first_video_info is None:
            self._settings.set_target_size_preview("Preview pending source analysis…")
            return

        try:
            profile = self._get_profile()
            profile.video_container = self._get_compressor().codec_manager.get_compatible_container(
                profile.video_codec,
                profile.video_container,
            ).value
            plan = self._get_compressor()._resolve_target_size_plan(profile, first_video_info)
            if not plan:
                self._settings.set_target_size_preview("")
                return

            if plan.target_size_bytes >= first_video_info.size:
                preview = (
                    f"Target {plan.target_size_mb} MB is not smaller than the source "
                    f"({first_video_info.size / 1_000_000:.1f} MB) and will be skipped."
                )
            else:
                preview = describe_target_size_plan(plan, first_video_info.size)
                if len(self.video_files) > 1:
                    preview += "  ·  Preview based on the first analyzed video."
                if settings.get("target_size_mode") == "exact":
                    exact_bits = []
                    if settings.get("exact_audio_policy") and settings.get("exact_audio_policy") != "reduce":
                        exact_bits.append(f"audio {settings['exact_audio_policy']}")
                    if settings.get("exact_two_pass"):
                        exact_bits.append("two-pass")
                    if exact_bits:
                        preview += f"  ·  Exact settings: {', '.join(exact_bits)}."

            self._settings.set_target_size_preview(preview)
        except Exception:
            self._settings.set_target_size_preview("Preview unavailable for the current selection.")

    def _refresh_recommendation(self) -> None:
        if not hasattr(self, "_settings"):
            return

        if not self.video_files:
            self._settings.set_recommendation("")
            return

        first_video_info = self._first_analyzed_video_info()
        if first_video_info is None:
            self._settings.set_recommendation("Recommendation pending source analysis…")
            return

        try:
            recommendation = self._get_compressor().recommend_profile_adjustments(
                self._get_profile(),
                first_video_info,
            )
        except Exception:
            recommendation = ""
        self._settings.set_recommendation(recommendation)

    def _get_profile(self) -> CompressionProfile:
        settings = self._settings.get_settings()
        name = self._profile_bar.get_selected() or "Balanced"
        base = (
            self._get_profile_manager().get_profile(name)
            or self._get_profile_manager().get_all_profiles()[0]
        )
        return CompressionProfile(
            name=name,
            profile_type=base.profile_type,
            description="Custom",
            video_codec=settings.get("video_codec", base.video_codec),
            crf=settings.get("crf", base.crf),
            preset=settings.get("preset", base.preset),
            audio_codec=settings.get("audio_codec", base.audio_codec),
            audio_bitrate=settings.get("audio_bitrate", base.audio_bitrate),
            output_mode=settings.get("output_mode", "video"),
            max_resolution=settings.get("max_resolution", base.max_resolution),
            frame_rate=settings.get("frame_rate", base.frame_rate),
            use_hw_accel=settings.get("use_hw_accel", True),
            target_size_mb=settings.get("target_size_mb"),
            target_size_mode=settings.get("target_size_mode", base.target_size_mode),
            exact_audio_policy=settings.get("exact_audio_policy", getattr(base, "exact_audio_policy", "reduce")),
            exact_two_pass=settings.get("exact_two_pass", getattr(base, "exact_two_pass", False)),
            video_container=settings.get("video_container", base.video_container),
            image_format=settings.get("image_format", base.image_format),
            threads=settings.get("threads"),
            resource_governor=settings.get("resource_governor", base.resource_governor),
            trim_enabled=settings.get("trim_enabled", False),
            trim_start=settings.get("trim_start"),
            trim_end=settings.get("trim_end"),
            extra_ffmpeg_args=settings.get("extra_ffmpeg_args", ""),
            rate_control=settings.get("rate_control", base.rate_control),
            audio_mode=settings.get("audio_mode", base.audio_mode),
            tune=settings.get("tune", base.tune),
        )

    def _output_path(self, input_path: Path, profile: CompressionProfile) -> Path:
        media_type = detect_media_type(input_path)
        if profile.output_mode == "audio":
            ext = "." + get_audio_extension(profile.audio_codec.value)
        elif media_type == "image":
            ext = "." + get_image_extension(profile.image_format)
        else:
            ext = "." + get_video_extension(profile.video_codec.value, profile.video_container)
        folder = self.output_folder or input_path.parent
        folder.mkdir(parents=True, exist_ok=True)

        settings = self._settings.get_settings()
        template = settings.get("output_name_template", "{name}_compressed")
        now = datetime.now()
        stem = template.format(
            name=input_path.stem,
            profile=profile.name,
            codec=profile.audio_codec.value if profile.output_mode == "audio" else profile.video_codec.value,
            crf=profile.crf,
            date=now.strftime("%Y-%m-%d"),
            timestamp=now.strftime("%H%M%S"),
        )
        return folder / (sanitize_filename(stem) + ext)

    #  Compression 

    def _start_compression(self):
        if not self.video_files or self.is_compressing:
            return

        self._cancel_requested = False
        self.is_compressing = True
        self._compress_btn.configure(text="Compressing\u2026", state="disabled")
        self._compare_btn.configure(text="Compare Sample", state="disabled")
        self._cancel_btn.pack(side="right", padx=(10, 0))
        self._cancel_btn.configure(text="Cancel", state="normal")
        self._clear_btn.pack_forget()
        self._current_profile = self._get_profile()

        for w in self._list.winfo_children():
            w.destroy()
        self.active_jobs.clear()
        self.results.clear()

        settings = self._settings.get_settings()
        max_parallel = settings.get("parallel_jobs", 1)
        self._max_parallel = max_parallel
        get_config_manager().update_config(
            max_parallel_jobs=max_parallel,
            output_name_template=settings.get("output_name_template", "{name}_compressed"),
            default_target_size_mode=settings.get("target_size_mode", "auto"),
            resource_governor=settings.get("resource_governor", "auto"),
            batch_queue_order=settings.get("batch_queue_order", "manual"),
            ui_scale_mode=settings.get("ui_scale_mode", self._ui_scale_mode),
        )

        self._pending_files = self._sorted_pending_files(settings.get("batch_queue_order", "manual"))
        self._active_job_count = 0
        self._refresh_encoder_preview()
        self._launch_next_jobs()

    def _launch_next_jobs(self):
        """Launch jobs up to the parallel limit."""
        import time
        while self._pending_files and self._active_job_count < self._max_parallel:
            fp = self._pending_files.pop(0)
            in_path = Path(fp)
            profile = self._current_profile
            if profile is None:
                break

            planned_parallel_jobs = self._planned_parallel_jobs()

            job_profile = CompressionProfile.from_dict(profile.to_dict())

            out_path = self._output_path(in_path, job_profile)
            compressor = self._get_compressor()
            job_id = compressor.compress_async(
                input_file=in_path,
                output_file=out_path,
                profile=job_profile,
                callback=lambda r: self._q.put(("result", r)),
                parallel_jobs=planned_parallel_jobs,
            )
            self._active_job_count += 1
            job = compressor.get_job(job_id)
            if job:
                card = ProgressCard(
                    self._list, job=job,
                    on_cancel=lambda jid: self._get_compressor().cancel_job(jid),
                )
                card.pack(fill="x", pady=(0, 8))
                self.active_jobs[job_id] = card

    def _compress_single(self, path: Path):
        """Context-menu handler — compress one file immediately."""
        if self.is_compressing:
            return
        # Keep only this file
        keys_to_remove = [k for k in self.video_files if k != str(path)]
        for k in keys_to_remove:
            if k in self.video_files:
                self.video_files[k]["card"].destroy()
                del self.video_files[k]
        self._refresh_label()
        self._start_compression()

    def _move_to_top(self, path: Path):
        """Context-menu handler — move file card to top of queue."""
        key = str(path)
        if key not in self.video_files:
            return
        item = self.video_files.pop(key)
        self.video_files = {key: item, **self.video_files}
        card = self.video_files[key]["card"]
        card.pack_forget()
        card.pack(fill="x", pady=(0, 8), before=self._list.winfo_children()[0] if self._list.winfo_children() else None)

    def _render_queue_cards(self):
        """Rebuild queue cards from the current in-memory file list."""

        for w in self._list.winfo_children():
            w.destroy()

        rebuilt: Dict[str, dict] = {}
        for key, item in self.video_files.items():
            card = FileQueueCard(
                self._list,
                filepath=key,
                on_remove=self._remove_file,
                on_compress_single=self._compress_single,
                on_compare_sample=self._compare_sample,
                on_move_top=self._move_to_top,
            )
            card.pack(fill="x", pady=(0, 8))
            info = item.get("info")
            if info:
                card.update_info(info)
            rebuilt[key] = {"card": card, "info": info}
        self.video_files = rebuilt
        self._refresh_label()

    def _sorted_pending_files(self, queue_order: str) -> List[str]:
        """Sort queued files according to the selected batch strategy."""

        items = list(self.video_files.keys())
        if queue_order == "manual":
            return items

        def _size_for(path_key: str) -> int:
            info = self.video_files[path_key].get("info")
            if info and getattr(info, "size", 0):
                return int(info.size)
            path = Path(path_key)
            return path.stat().st_size if path.exists() else 0

        def _duration_for(path_key: str) -> float:
            info = self.video_files[path_key].get("info")
            return float(getattr(info, "duration", 0.0) or 0.0) if info else 0.0

        reverse = queue_order in {"largest-first", "longest-first"}
        if queue_order in {"largest-first", "smallest-first"}:
            return sorted(items, key=_size_for, reverse=reverse)
        if queue_order in {"longest-first", "shortest-first"}:
            return sorted(items, key=_duration_for, reverse=reverse)
        return items

    def _build_compare_profiles(self, base_profile: CompressionProfile) -> List[CompressionProfile]:
        """Create a small comparison matrix around the currently selected settings."""

        compressor = self._get_compressor()
        codec_manager = compressor.codec_manager
        candidates: List[CompressionProfile] = []
        seen: set[tuple[str, str, bool]] = set()

        def _candidate(name: str, codec: VideoCodec, preset: str, use_hw: bool) -> CompressionProfile:
            profile = CompressionProfile.from_dict(base_profile.to_dict())
            profile.name = name
            profile.video_codec = codec
            profile.preset = preset
            profile.use_hw_accel = use_hw
            profile.target_size_mb = None
            profile.target_reduction_percent = None
            profile.video_container = codec_manager.get_compatible_container(codec, base_profile.video_container).value
            return profile

        candidate_specs = [
            (f"Current · {base_profile.video_codec.value.upper()}", base_profile.video_codec, base_profile.preset, base_profile.use_hw_accel),
            ("HEVC Balanced", VideoCodec.HEVC, "medium", True),
            ("HEVC Smaller", VideoCodec.HEVC, "slow", True),
            ("H264 Fast", VideoCodec.H264, "fast", True),
        ]

        for name, codec, preset, use_hw in candidate_specs:
            key = (codec.value, preset, bool(use_hw))
            if key in seen:
                continue
            seen.add(key)
            candidates.append(_candidate(name, codec, preset, use_hw))
            if len(candidates) == 3:
                break

        return candidates

    def _compare_output_dir(self, input_path: Path) -> Path:
        """Return the folder used for sample-compare output."""

        base_folder = self.output_folder or input_path.parent
        compare_dir = base_folder / "_compare_samples"
        compare_dir.mkdir(parents=True, exist_ok=True)
        return compare_dir

    def _compare_first(self):
        if not self.video_files or self.is_compressing:
            return
        first_path = Path(next(iter(self.video_files.keys())))
        self._compare_sample(first_path)

    def _compare_sample(self, path: Path):
        """Run short comparison encodes for one queued video."""

        if self.is_compressing:
            return

        info = self.video_files.get(str(path), {}).get("info")
        if info and not hasattr(info, "video_codec"):
            messagebox.showinfo(APP_NAME, "Compare mode currently supports videos only.", parent=self)
            return

        self._compare_mode = True
        self._cancel_requested = False
        self.is_compressing = True
        self._compress_btn.configure(state="disabled")
        self._compare_btn.configure(text="Comparing…", state="disabled")
        self._cancel_btn.pack(side="right", padx=(10, 0))
        self._cancel_btn.configure(text="Cancel", state="normal")
        self._clear_btn.pack_forget()

        for w in self._list.winfo_children():
            w.destroy()
        self.active_jobs.clear()
        self.results.clear()
        self._pending_files = []
        self._active_job_count = 0
        self._current_profile = self._get_profile()
        compare_profiles = self._build_compare_profiles(self._current_profile)
        self._queue_lbl.configure(text=f"Comparing {len(compare_profiles)} sample encodes for {path.name}")
        output_dir = self._compare_output_dir(path)
        compressor = self._get_compressor()

        def _worker():
            try:
                results = compressor.compare_sample(
                    input_file=path,
                    output_dir=output_dir,
                    profiles=compare_profiles,
                    sample_seconds=20.0,
                )
                for result in results:
                    self._q.put(("result", result))
            except Exception as exc:
                self._q.put(("compare_failed", str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_compare_mode(self, title: str, body: str, is_error: bool = False):
        """Restore the queued-file view after compare mode finishes."""

        self.is_compressing = False
        self._compare_mode = False
        self._compress_btn.configure(text="Compress", state="normal")
        self._compare_btn.configure(text="Compare Sample", state="normal")
        self._cancel_btn.pack_forget()
        self._clear_btn.pack(side="right", padx=(10, 0))
        self._current_profile = None
        self.active_jobs.clear()
        self._render_queue_cards()
        if is_error:
            messagebox.showerror(title, body, parent=self)
        else:
            messagebox.showinfo(title, body, parent=self)

    #  UI pump 

    def _pump_ui(self):
        try:
            while True:
                self._handle(self._q.get_nowait())
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._pump_after_id = self.after(80, self._pump_ui)

    def _handle(self, msg: tuple):
        kind = msg[0]
        if kind == "update_available":
            _, info = msg
            self._show_update_banner(info)
        elif kind == "hw_ready":
            _, info = msg
            self._hw_info = info
            self._hw_badge.update_info(info)
            self._refresh_encoder_preview()
        elif kind == "external_open":
            _, files = msg
            self.deiconify()
            self.lift()
            try:
                self.focus_force()
            except Exception:
                pass
            self._on_files_dropped(files)
        elif kind == "info":
            _, fp, info = msg
            if fp in self.video_files:
                self.video_files[fp]["info"] = info
                self.video_files[fp]["card"].update_info(info)
                self._refresh_size_preview()
        elif kind == "progress":
            _, job = msg
            if job.id not in self.active_jobs and self.is_compressing:
                card = ProgressCard(
                    self._list,
                    job=job,
                    on_cancel=lambda jid: self._get_compressor().cancel_job(jid),
                )
                card.pack(fill="x", pady=(0, 8))
                self.active_jobs[job.id] = card
            if job.id in self.active_jobs:
                self.active_jobs[job.id].update(job)
        elif kind == "result":
            _, result = msg
            self.results.append(result)
            self._active_job_count = max(0, self._active_job_count - 1)

            # Record to history
            if result.success and not getattr(result, "skipped", False) and result.output_file and not self._compare_mode:
                try:
                    orig_size = result.original_size
                    comp_size = result.compressed_size
                    reduction = result.reduction_percent
                    profile_name = self._current_profile.name if self._current_profile else "Unknown"
                    codec_name = result.video_codec or (self._current_profile.video_codec.value if self._current_profile else "")
                    entry = HistoryEntry(
                        input_path=str(result.input_file),
                        output_path=str(result.output_file),
                        original_size_bytes=orig_size,
                        compressed_size_bytes=comp_size,
                        reduction_pct=reduction,
                        codec=codec_name,
                        profile_name=profile_name,
                        duration_seconds=round(result.encoding_time, 1),
                        timestamp=datetime.now().isoformat(timespec="seconds"),
                    )
                    self._history.add_entry(entry)
                except Exception:
                    pass

            # Launch next parallel job if queue has more
            self._launch_next_jobs()
            self._refresh_encoder_preview()

            compressor = self._get_compressor()
            pending = [
                j for j in compressor.get_all_jobs()
                if getattr(j.status, "value", j.status) not in ("completed", "failed", "cancelled")
            ]
            if not pending and not self._pending_files:
                if self._compare_mode:
                    summary_lines = ["Sample compare finished.", ""]
                    for item in self.results:
                        label = item.note or item.video_codec.upper()
                        if item.success:
                            summary_lines.append(
                                f"{label} -> {format_size(item.compressed_size)} in {format_time(item.encoding_time)}"
                            )
                        else:
                            summary_lines.append(f"{label} -> failed: {item.error_message}")
                    compare_outputs = [r.output_file for r in self.results if r.success and r.output_file]
                    if compare_outputs:
                        compare_dir = compare_outputs[-1].parent
                        summary_lines.extend(["", f"Saved samples to: {compare_dir}"])
                        try:
                            if os.name == "nt":
                                os.startfile(compare_dir)
                        except Exception:
                            pass
                    self._finish_compare_mode(APP_NAME, "\n".join(summary_lines))
                else:
                    self._all_done()
        elif kind == "compare_failed":
            _, error_text = msg
            self._finish_compare_mode(APP_NAME, error_text or "Compare mode failed.", is_error=True)

    def _cancel_all(self):
        """Cancel every active and pending compression job."""
        if not self.is_compressing:
            return
        self._cancel_requested = True
        self._pending_files.clear()
        self._cancel_btn.configure(text="Cancelling\u2026", state="disabled")
        compressor = self._get_compressor()
        for jid in list(self.active_jobs):
            compressor.cancel_job(jid)
        if not compressor.get_active_jobs():
            if self._compare_mode:
                self._finish_compare_mode(APP_NAME, "Compare mode cancelled.")
            else:
                self._all_done()

    def _all_done(self):
        self.is_compressing = False
        self._compress_btn.configure(text="Compress", state="normal")
        self._compare_btn.configure(text="Compare Sample", state="normal")
        self._cancel_btn.pack_forget()
        self._clear_btn.pack(side="right", padx=(10, 0))
        self._current_profile = None
        self._refresh_encoder_preview()
        if not self._cancel_requested and any(r.success for r in self.results):
            self._send_toast()
        self._show_results()

    def _send_toast(self):
        """Send a Windows toast notification on batch completion."""
        if not _HAS_WINOTIFY or not get_config().notify_on_completion:
            return
        ok = sum(1 for r in self.results if r.success and not getattr(r, "skipped", False))
        total = len(self.results)
        saved_bytes = 0
        for r in self.results:
            if r.success and r.input_file and r.output_file:
                try:
                    saved_bytes += Path(r.input_file).stat().st_size - Path(r.output_file).stat().st_size
                except Exception:
                    pass
        saved_mb = saved_bytes / (1024 * 1024)
        body = f"{ok}/{total} files compressed"
        if saved_mb > 0:
            body += f" — saved {saved_mb:.1f} MB"
        try:
            icon = _window_icon_path()
            toast = Notification(
                app_id=APP_NAME,
                title="Compression Complete",
                msg=body,
                icon=str(icon) if icon else "",
            )
            toast.show()
        except Exception:
            pass

    def _show_results(self):
        for w in self._list.winfo_children():
            w.destroy()

        ok = sum(1 for r in self.results if r.success and not getattr(r, "skipped", False))
        skipped = sum(1 for r in self.results if getattr(r, "skipped", False))
        cancelled = sum(1 for r in self.results if getattr(r, "cancelled", False))
        fail = len(self.results) - ok - skipped - cancelled
        msg = f"Done \u2014 {ok} compressed"
        if self._cancel_requested:
            msg = f"Cancelled \u2014 {cancelled} stopped"
            if ok:
                msg += f", {ok} completed"
        elif skipped:
            msg += f", {skipped} skipped"
        if fail:
            msg += f", {fail} failed"
        self._queue_lbl.configure(text=msg)

        for result in self.results:
            if getattr(result, "cancelled", False):
                continue
            ResultCard(self._list, result=result).pack(fill="x", pady=(0, 8))

        self._open_output_folder()
        self.video_files.clear()
        self.active_jobs.clear()
        self._drop_zone.set_compact(True)
        self._cancel_requested = False

    def _open_output_folder(self):
        successful = [r for r in self.results if r.success and r.output_file]
        if not successful:
            return
        folder = successful[-1].output_file.parent
        try:
            if os.name == "nt":
                os.startfile(folder)
        except Exception as exc:
            logger.debug(f"Could not open folder: {exc}")

    #  Misc 

    def _clear_all(self):
        for w in self._list.winfo_children():
            w.destroy()
        self.video_files.clear()
        self.active_jobs.clear()
        self.results.clear()
        self._queue_lbl.configure(text="No files added")
        self._drop_zone.set_compact(False)

    def _pick_output_folder(self):
        folder = filedialog.askdirectory(
            title="Select Output Folder",
            initialdir=str(self.output_folder or Path.home()),
        )
        if folder:
            self.output_folder = Path(folder)
            self.output_folder.mkdir(parents=True, exist_ok=True)
            self._output_lbl.configure(text=f"\u2192 {self.output_folder.name}/")

    def _open_about(self):
        AboutDialog(self, hw_info=self._hw_info)

    def _open_history(self):
        HistoryDialog(self, self._history)

    def _open_stats(self):
        StatsDialog(self, self._history)

    def _open_profile_editor(self):
        def _on_editor_close():
            profiles = self._get_profile_manager().get_all_profiles()
            self._profile_bar.refresh(profiles)
            self._settings.refresh_profiles(profiles)
        ProfileEditorDialog(self, self._get_profile_manager(), on_close=_on_editor_close)

    def _on_close(self):
        self._ipc_server.stop()
        if self._pump_after_id:
            try:
                self.after_cancel(self._pump_after_id)
            except Exception:
                pass
            self._pump_after_id = None
        if self._splash is not None:
            self._splash.close()
            self._splash = None
        if self.app_config.remember_window_size:
            get_config_manager().update_config(
                window_width=self.winfo_width(),
                window_height=self.winfo_height(),
                ui_scale_mode=self._ui_scale_mode,
            )
        self.destroy()

    def run(self):
        self.mainloop()


def run_app(startup_files: Optional[List[Path]] = None, auto_start: bool = False):
    """Entry point for the GUI application."""
    app = MainWindow(startup_files=startup_files, auto_start=auto_start)
    app.run()


if __name__ == "__main__":
    run_app()
