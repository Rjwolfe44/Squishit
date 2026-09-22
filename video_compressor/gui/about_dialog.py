"""About dialog — version info, hardware summary, manual update check."""

from __future__ import annotations

import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Optional

import customtkinter as ctk

from .scaling import apply_dialog_geometry
from .widgets import COLORS, _lbl
from ..config import APP_NAME, APP_VERSION

RELEASES_URL = "https://github.com/Rjwolfe44/squishit-releases/releases"
ISSUES_URL = "https://github.com/Rjwolfe44/squishit-releases/issues"


class AboutDialog(ctk.CTkToplevel):
    """Modal dialog showing app version, hardware info, and update check."""

    def __init__(self, master: Any, hw_info: Any = None, **kwargs):
        super().__init__(master, **kwargs)
        self.title(f"About {APP_NAME}")
        self.configure(fg_color=COLORS["bg"])
        self.transient(master)
        self.grab_set()

        self._hw_info = hw_info
        self._update_info: Optional[Any] = None

        self._build()
        apply_dialog_geometry(self, master, base_width=420, base_height=480, resizable=False)

    # ── layout ───────────────────────────────────────────────────

    def _build(self):
        pad = ctk.CTkFrame(self, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=24, pady=24)

        # App title + version
        _lbl(pad, APP_NAME, size=22, weight="bold").pack(anchor="w")
        _lbl(pad, f"Version {APP_VERSION}", size=13, color=COLORS["text_dim"]).pack(
            anchor="w", pady=(2, 0)
        )

        ctk.CTkFrame(pad, height=1, fg_color=COLORS["border"]).pack(
            fill="x", pady=14
        )

        # Hardware section
        _lbl(pad, "HARDWARE", size=10, weight="bold", color=COLORS["text_muted"]).pack(
            anchor="w"
        )
        hw_frame = ctk.CTkFrame(pad, fg_color=COLORS["surface"], corner_radius=10)
        hw_frame.pack(fill="x", pady=(6, 0))

        if self._hw_info:
            info = self._hw_info
            lines = [
                f"CPU: {info.cpu_name}  ({info.cpu_threads} threads)",
                f"RAM: {info.total_ram_gb:.1f} GB",
            ]
            for gpu in info.gpus:
                codecs = [c for c, ok in gpu.encoder_support.items() if ok]
                codec_str = ", ".join(codecs).upper() if codecs else "none"
                lines.append(f"GPU: {gpu.name}  ({codec_str})")
            if not info.gpus:
                lines.append("GPU: not detected")
        else:
            lines = ["Detecting hardware…"]

        for line in lines:
            _lbl(hw_frame, line, size=12, color=COLORS["text_dim"]).pack(
                anchor="w", padx=12, pady=(4, 0)
            )
        # Bottom padding
        ctk.CTkFrame(hw_frame, fg_color="transparent", height=6).pack()

        ctk.CTkFrame(pad, height=1, fg_color=COLORS["border"]).pack(
            fill="x", pady=14
        )

        # Update section
        _lbl(pad, "UPDATES", size=10, weight="bold", color=COLORS["text_muted"]).pack(
            anchor="w"
        )

        update_row = ctk.CTkFrame(pad, fg_color="transparent")
        update_row.pack(fill="x", pady=(6, 0))

        self._update_status = _lbl(
            update_row, f"Current version: {APP_VERSION}",
            size=12, color=COLORS["text_dim"],
        )
        self._update_status.pack(side="left")

        self._check_btn = ctk.CTkButton(
            update_row,
            text="Check for Updates",
            width=140, height=30, corner_radius=8,
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            text_color="#ffffff", font=ctk.CTkFont(size=12),
            command=self._check_for_updates,
        )
        self._check_btn.pack(side="right")

        # Download button (hidden until update found)
        self._dl_btn = ctk.CTkButton(
            pad,
            text="Download & Install",
            height=34, corner_radius=8,
            fg_color="#22c55e", hover_color="#16a34a",
            text_color="#ffffff", font=ctk.CTkFont(size=13, weight="bold"),
            command=self._download_and_install,
        )

        # Spacer
        ctk.CTkFrame(pad, fg_color="transparent", height=1).pack(fill="x", expand=True)

        ctk.CTkFrame(pad, height=1, fg_color=COLORS["border"]).pack(
            fill="x", pady=(0, 10)
        )

        # Footer links
        link_row = ctk.CTkFrame(pad, fg_color="transparent")
        link_row.pack(fill="x")

        ctk.CTkButton(
            link_row, text="View Releases", width=120, height=28, corner_radius=6,
            fg_color="transparent", hover_color=COLORS["surface_raised"],
            text_color=COLORS["accent"], font=ctk.CTkFont(size=12),
            command=lambda: webbrowser.open(RELEASES_URL),
        ).pack(side="left")

        ctk.CTkButton(
            link_row, text="Report Issue", width=110, height=28, corner_radius=6,
            fg_color="transparent", hover_color=COLORS["surface_raised"],
            text_color=COLORS["accent"], font=ctk.CTkFont(size=12),
            command=lambda: webbrowser.open(ISSUES_URL),
        ).pack(side="left", padx=(6, 0))

        ctk.CTkButton(
            link_row, text="Close", width=80, height=28, corner_radius=6,
            fg_color=COLORS["surface_raised"], hover_color=COLORS["border"],
            text_color=COLORS["text_dim"], font=ctk.CTkFont(size=12),
            command=self.destroy,
        ).pack(side="right")

    # ── update logic ─────────────────────────────────────────────

    def _check_for_updates(self):
        self._check_btn.configure(text="Checking…", state="disabled")
        self._update_status.configure(text="Contacting server…")

        def _run():
            from ..core.updater import check_for_update
            info = check_for_update(APP_VERSION, force=True)
            self.after(0, lambda: self._on_check_result(info))

        threading.Thread(target=_run, daemon=True).start()

    def _on_check_result(self, info):
        if info:
            self._update_info = info
            self._update_status.configure(
                text=f"v{info.latest_version} available!",
                text_color=COLORS["success"],
            )
            self._check_btn.pack_forget()
            self._dl_btn.pack(fill="x", pady=(8, 0))
        else:
            self._update_status.configure(
                text="You're up to date  ✓",
                text_color=COLORS["success"],
            )
            self._check_btn.configure(text="Check for Updates", state="normal")

    def _download_and_install(self):
        if not self._update_info:
            return
        self._dl_btn.configure(text="Downloading…", state="disabled")

        def _run():
            from ..core.updater import download_update

            def _progress(done: int, total: int):
                pct = int(done / total * 100) if total else 0
                try:
                    self._dl_btn.configure(text=f"Downloading… {pct}%")
                except Exception:
                    pass

            installer = download_update(
                self._update_info.download_url, progress_cb=_progress
            )
            if installer:
                try:
                    self._dl_btn.configure(text="Installing…")
                except Exception:
                    pass
                subprocess.Popen(
                    [str(installer), "/VERYSILENT", "/NORESTART"],
                    creationflags=subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP,
                )
                self.after(1500, lambda: (self.master.destroy() if self.master else None))
            else:
                try:
                    self._dl_btn.configure(text="Download failed", state="normal")
                except Exception:
                    pass

        threading.Thread(target=_run, daemon=True).start()
