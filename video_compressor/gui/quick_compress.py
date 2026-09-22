"""Minimal quick-compress window for Explorer context-menu launches."""

from __future__ import annotations

import queue
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from typing import Optional

from ..core.compressor import CompressionResult, VideoCompressor
from ..core.profiles import CompressionProfile, ProfileType
from ..core.codecs import VideoCodec, AudioCodec
from ..config import APP_NAME, get_config_manager
from .scaling import apply_tk_scaling, center_window, resolve_ui_scale, scaled

# Quick compress presets — lightweight standalone definitions, no ProfileManager needed.
_QUICK_PRESETS: dict[str, CompressionProfile] = {
    "Lite": CompressionProfile(
        name="Lite",
        profile_type=ProfileType.FAST,
        description="Fastest option. Smaller files with minimal CPU use.",
        video_codec=VideoCodec.H264,
        crf=28,
        preset="fast",
        audio_codec=AudioCodec.AAC,
        audio_bitrate=128_000,
        video_container="mp4",
    ),
    "Balanced": CompressionProfile(
        name="Balanced",
        profile_type=ProfileType.BALANCED,
        description="Recommended. Better compression without slowing to a crawl.",
        video_codec=VideoCodec.HEVC,
        crf=28,
        preset="medium",
        audio_codec=AudioCodec.AAC,
        audio_bitrate=128_000,
        video_container="mp4",
    ),
    "Max": CompressionProfile(
        name="Max",
        profile_type=ProfileType.MAX,
        description="Smallest practical files. Uses stronger HEVC compression.",
        video_codec=VideoCodec.HEVC,
        crf=29,
        preset="slow",
        audio_codec=AudioCodec.OPUS,
        audio_bitrate=96_000,
        video_container="mkv",
        use_hw_accel=True,
    ),
}

_PRESET_ORDER = ["Lite", "Balanced", "Max"]


def _window_icon_path() -> Optional[Path]:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    icon_path = base_dir / "assets" / "squishit.ico"
    return icon_path if icon_path.exists() else None


class QuickCompressWindow(tk.Tk):
    """Fast-loading minimal progress window for one-click compression."""

    def __init__(self, input_file: Path):
        super().__init__()
        self.input_file = Path(input_file)
        self.compressor: Optional[VideoCompressor] = None
        self.config_manager = get_config_manager()
        self._queue: queue.Queue = queue.Queue()
        self._job_id: Optional[str] = None
        self._result: Optional[CompressionResult] = None
        self._output_path: Optional[Path] = None
        self._is_running = False
        self._preset_buttons: dict[str, tk.Button] = {}
        self._pump_after_id: Optional[str] = None
        self._ui_scale = resolve_ui_scale(
            getattr(self.config_manager.config, "ui_scale_mode", "auto"),
            self.winfo_screenheight(),
        )

        apply_tk_scaling(self, self._ui_scale)
        self.option_add("*Font", ("Segoe UI", max(9, int(round(10 * self._ui_scale)))))

        # Restore last-used preset (default to Balanced)
        saved = self.config_manager.config.quick_compress_profile
        if saved not in _QUICK_PRESETS:
            saved = "Balanced"
        self._preset_var = tk.StringVar(value=saved)

        self.title(f"{APP_NAME} Quick Compress")
        width, height = self._scaled_window_size()
        self.geometry(f"{width}x{height}")
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._apply_icon()

        self._build_ui()
        center_window(self, width=width, height=height)
        self._refresh_preview()
        self.bind("<Configure>", lambda _event: self._update_wraplengths())
        self._pump_after_id = self.after(100, self._pump)

    def _build_ui(self) -> None:
        outer = tk.Frame(self, padx=16, pady=14)
        outer.pack(fill="both", expand=True)

        # File name
        tk.Label(outer, text=self.input_file.name, font=("Segoe UI", max(11, int(round(11 * self._ui_scale))), "bold"), anchor="w").pack(fill="x")

        # Preset selector row
        preset_row = tk.Frame(outer)
        preset_row.pack(fill="x", pady=(8, 0))
        tk.Label(preset_row, text="Preset:", font=("Segoe UI", max(9, int(round(9 * self._ui_scale)))), fg="#555555").pack(side="left")
        for name in _PRESET_ORDER:
            btn = tk.Button(
                preset_row,
                text=name,
                font=("Segoe UI", max(9, int(round(9 * self._ui_scale)))),
                width=10,
                relief="flat",
                bd=0,
                padx=10,
                pady=5,
                cursor="hand2",
                command=lambda preset=name: self._set_preset(preset),
            )
            btn.pack(side="left", padx=(8, 0))
            self._preset_buttons[name] = btn

        self.preset_hint_var = tk.StringVar(value="Choose a preset, then click Start Compression.")
        self._hint_label = tk.Label(
            outer,
            textvariable=self.preset_hint_var,
            anchor="w",
            justify="left",
            fg="#555555",
        )
        self._hint_label.pack(fill="x", pady=(8, 0))

        self.status_var = tk.StringVar(value="Waiting to start")
        tk.Label(outer, textvariable=self.status_var, anchor="w", fg="#555555").pack(fill="x", pady=(8, 10))

        self.progress = tk.DoubleVar(value=0.0)
        self.progress_bar = tk.Canvas(outer, height=12, bg="#dadada", highlightthickness=0)
        self.progress_bar.pack(fill="x")
        self.progress_bar.bind("<Configure>", lambda _event: self._draw_progress())

        self.detail_var = tk.StringVar(value="0%")
        tk.Label(outer, textvariable=self.detail_var, anchor="w", fg="#333333").pack(fill="x", pady=(10, 0))
        self.destination_var = tk.StringVar(value="")
        self._destination_label = tk.Label(
            outer,
            textvariable=self.destination_var,
            anchor="w",
            justify="left",
            fg="#555555",
        )
        self._destination_label.pack(fill="x", pady=(8, 0))

        actions = tk.Frame(outer)
        actions.pack(fill="x", pady=(12, 0))
        tk.Button(actions, text="Open Full App", command=self._open_full_app).pack(side="left")
        self._start_btn = tk.Button(actions, text="Start Compression", command=self._start)
        self._start_btn.pack(side="right", padx=(0, 8))
        tk.Button(actions, text="Cancel", command=self._cancel).pack(side="right")

        self._update_preset_buttons()

    def _scaled_window_size(self) -> tuple[int, int]:
        screen_w = max(1, self.winfo_screenwidth())
        screen_h = max(1, self.winfo_screenheight())
        scale = max(0.95, self._ui_scale)
        width = max(560, min(scaled(620, scale), screen_w - 80))
        height = max(300, min(scaled(320, scale), screen_h - 80))
        return width, height

    def _update_wraplengths(self) -> None:
        wraplength = max(320, self.winfo_width() - 48)
        try:
            self._hint_label.configure(wraplength=wraplength)
            self._destination_label.configure(wraplength=wraplength)
        except Exception:
            pass

    def _get_compressor(self) -> VideoCompressor:
        if self.compressor is None:
            self.compressor = VideoCompressor()
        return self.compressor

    def _set_preset(self, preset: str) -> None:
        self._preset_var.set(preset)
        self._on_preset_change()

    def _update_preset_buttons(self) -> None:
        active = self._preset_var.get()
        for name, button in self._preset_buttons.items():
            if name == active:
                button.configure(bg="#2f80ed", fg="#ffffff", activebackground="#1f6fda", activeforeground="#ffffff")
            else:
                button.configure(bg="#e7ebf0", fg="#1f2933", activebackground="#d7dde5", activeforeground="#1f2933")

    def _on_preset_change(self) -> None:
        """Persist the newly selected preset to config."""
        selected = self._preset_var.get()
        self.config_manager.update_config(quick_compress_profile=selected)
        self._update_preset_buttons()
        if not self._is_running:
            self._refresh_preview()

    def _apply_icon(self) -> None:
        icon_path = _window_icon_path()
        if not icon_path:
            return
        try:
            self.iconbitmap(str(icon_path))
        except Exception:
            pass

    def _draw_progress(self) -> None:
        self.progress_bar.delete("all")
        width = max(1, self.progress_bar.winfo_width())
        height = max(1, self.progress_bar.winfo_height())
        self.progress_bar.create_rectangle(0, 0, width, height, fill="#dadada", outline="")
        self.progress_bar.create_rectangle(0, 0, width * (self.progress.get() / 100.0), height, fill="#2f80ed", outline="")

    def _get_profile(self, resolve_fallback: bool = True) -> CompressionProfile:
        selected = self._preset_var.get()
        profile = CompressionProfile.from_dict(_QUICK_PRESETS[selected].to_dict())
        if resolve_fallback and selected == "Max" and not self._get_compressor().codec_manager.is_codec_usable(VideoCodec.HEVC):
            fallback_codec = self._get_compressor().codec_manager.get_best_codec(prefer_efficiency=True)
            profile.video_codec = fallback_codec
            profile.video_container = self._get_compressor().codec_manager.get_compatible_container(
                fallback_codec,
                profile.video_container,
            ).value
        return profile

    def _refresh_preview(self) -> None:
        profile = self._get_profile(resolve_fallback=False)
        self.preset_hint_var.set(profile.description)
        self.destination_var.set(f"Will save to: {self._output_file()}")

    def _output_file(self, profile: Optional[CompressionProfile] = None) -> Path:
        config = self.config_manager.config
        folder = self.input_file.parent
        folder.mkdir(parents=True, exist_ok=True)
        profile = profile or self._get_profile(resolve_fallback=False)
        from ..core.utils import detect_media_type, get_image_extension, get_video_extension

        media_type = detect_media_type(self.input_file)
        if media_type == "image":
            ext = "." + get_image_extension(profile.image_format)
        else:
            ext = "." + get_video_extension(profile.video_codec.value, profile.video_container)
        return folder / f"{self.input_file.stem}{config.output_suffix}{ext}"

    def _start(self) -> None:
        if self._is_running:
            return
        if not self.input_file.exists():
            messagebox.showerror(APP_NAME, f"File not found: {self.input_file}")
            self.destroy()
            return

        self._is_running = True
        self._start_btn.configure(state="disabled", text="Compressing...")
        profile = self._get_profile(resolve_fallback=True)
        output_file = self._output_file(profile)
        self._output_path = output_file
        self.destination_var.set(f"Saving to: {output_file}")
        compressor = self._get_compressor()
        compressor.set_progress_callback(lambda job: self._queue.put(("progress", job)))
        self._job_id = compressor.compress_async(
            input_file=self.input_file,
            output_file=output_file,
            profile=profile,
            callback=lambda result: self._queue.put(("result", result)),
        )

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "progress":
                    job = payload
                    self.progress.set(job.progress)
                    self.status_var.set(job.status.value.title())
                    parts = [f"{job.progress:.0f}%"]
                    if job.speed > 0:
                        parts.append(f"{job.speed:.1f} fps")
                    if job.eta > 0:
                        parts.append(f"ETA {int(job.eta)}s")
                    self.detail_var.set("  •  ".join(parts))
                    self._draw_progress()
                elif kind == "result":
                    self._result = payload
                    self._complete(payload)
                    return
        except queue.Empty:
            pass
        if self.winfo_exists():
            self._pump_after_id = self.after(100, self._pump)

    def _complete(self, result: CompressionResult) -> None:
        self._is_running = False
        if result.success:
            self.progress.set(100)
            self._draw_progress()
            if result.skipped:
                self.status_var.set("Skipped")
            else:
                self.status_var.set("Kept Original" if result.kept_original else "Completed")
            self.detail_var.set(result.note or "Compression finished")
            if result.output_file:
                self.destination_var.set(f"Saved to: {result.output_file}")
            self._start_btn.configure(text="Close", state="normal", command=self.destroy)
            self.after(1800, self.destroy)
        else:
            self.status_var.set("Failed")
            self.detail_var.set(result.error_message or "Compression failed")
            self._start_btn.configure(text="Try Again", state="normal")
            messagebox.showerror(APP_NAME, result.error_message or "Compression failed")

    def _cancel(self) -> None:
        if self._job_id and self._is_running:
            self._get_compressor().cancel_job(self._job_id)
        if self._pump_after_id:
            try:
                self.after_cancel(self._pump_after_id)
            except Exception:
                pass
            self._pump_after_id = None
        self.destroy()

    def _open_full_app(self) -> None:
        subprocess.Popen([sys.executable, "-m", "video_compressor", "--open", str(self.input_file)])
        self.after(0, self.destroy)


def run_quick_compress(input_file: Path) -> None:
    app = QuickCompressWindow(input_file)
    app.mainloop()
