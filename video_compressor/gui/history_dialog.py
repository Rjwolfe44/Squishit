"""History dialog for past compression jobs."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import customtkinter as ctk

from ..config import APP_NAME
from ..core.history import HistoryManager, HistoryEntry
from ..core.utils import format_size, format_time
from .scaling import apply_dialog_geometry
from .widgets import COLORS, _lbl


class HistoryDialog(ctk.CTkToplevel):
    """Modal dialog showing saved compression history."""

    def __init__(self, master, history_manager: HistoryManager, **kwargs):
        super().__init__(master, fg_color=COLORS["bg"], **kwargs)
        self._history = history_manager
        self.title(f"{APP_NAME} History")
        self.transient(master)
        self.grab_set()

        self._scroll = None
        self._stats_label = None
        self._build()
        apply_dialog_geometry(self, master, base_width=820, base_height=560, min_width=720, min_height=460)
        self._refresh()

    def _build(self):
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=18, pady=18)

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.pack(fill="x", pady=(0, 12))

        _lbl(header, "History", size=20, weight="bold").pack(side="left")
        self._stats_label = _lbl(header, "", size=11, color=COLORS["text_dim"])
        self._stats_label.pack(side="left", padx=(12, 0))

        ctk.CTkButton(
            header,
            text="Clear History",
            width=100,
            height=30,
            corner_radius=8,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["error"],
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=12),
            command=self._clear_history,
        ).pack(side="right")

        ctk.CTkButton(
            header,
            text="Close",
            width=80,
            height=30,
            corner_radius=8,
            fg_color=COLORS["surface_raised"],
            hover_color=COLORS["border"],
            text_color=COLORS["text_dim"],
            font=ctk.CTkFont(size=12),
            command=self.destroy,
        ).pack(side="right", padx=(0, 8))

        self._scroll = ctk.CTkScrollableFrame(
            root,
            fg_color=COLORS["surface"],
            corner_radius=12,
            scrollbar_button_color=COLORS["surface_raised"],
            scrollbar_button_hover_color=COLORS["border"],
        )
        self._scroll.pack(fill="both", expand=True)

    def _refresh(self):
        for child in self._scroll.winfo_children():
            child.destroy()

        entries = self._history.get_entries()
        stats = self._history.get_stats()
        self._stats_label.configure(
            text=f"{stats['count']} entries - {format_size(stats['total_saved_bytes'])} saved total"
        )

        if not entries:
            empty = ctk.CTkFrame(self._scroll, fg_color="transparent")
            empty.pack(fill="both", expand=True, pady=24)
            _lbl(empty, "No history yet", size=16, weight="bold", anchor="center").pack()
            _lbl(
                empty,
                "Completed compressions will appear here.",
                size=11,
                color=COLORS["text_dim"],
                anchor="center",
            ).pack(pady=(6, 0))
            return

        for entry in entries:
            self._entry_card(entry)

    def _entry_card(self, entry: HistoryEntry):
        card = ctk.CTkFrame(self._scroll, fg_color=COLORS["surface_raised"], corner_radius=10)
        card.pack(fill="x", padx=10, pady=(10, 0))

        body = ctk.CTkFrame(card, fg_color="transparent")
        body.pack(fill="x", padx=12, pady=12)

        header = ctk.CTkFrame(body, fg_color="transparent")
        header.pack(fill="x")

        input_name = Path(entry.input_path).name
        _lbl(header, input_name, size=13, weight="bold").pack(side="left", fill="x", expand=True)

        try:
            stamp = datetime.fromisoformat(entry.timestamp).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            stamp = entry.timestamp
        _lbl(header, stamp, size=10, color=COLORS["text_muted"]).pack(side="right")

        stats = ctk.CTkFrame(body, fg_color="transparent")
        stats.pack(fill="x", pady=(10, 0))
        for label, value, color in [
            ("Before", format_size(entry.original_size_bytes), COLORS["text_dim"]),
            ("After", format_size(entry.compressed_size_bytes), COLORS["text"]),
            ("Saved", f"{entry.reduction_pct:.1f}%", COLORS["success"]),
            ("Time", format_time(entry.duration_seconds), COLORS["text_dim"]),
            ("Codec", entry.codec.upper(), COLORS["text_dim"]),
            ("Profile", entry.profile_name, COLORS["text_dim"]),
        ]:
            col = ctk.CTkFrame(stats, fg_color="transparent")
            col.pack(side="left", expand=True)
            _lbl(col, label, size=10, color=COLORS["text_muted"], anchor="center").pack()
            _lbl(col, value, size=12, weight="bold", color=color, anchor="center").pack()

        path_row = ctk.CTkFrame(body, fg_color="transparent")
        path_row.pack(fill="x", pady=(10, 0))
        _lbl(path_row, entry.output_path, size=10, color=COLORS["text_muted"]).pack(side="left", fill="x", expand=True)

        if Path(entry.output_path).exists():
            ctk.CTkButton(
                path_row,
                text="Open File",
                width=72,
                height=26,
                corner_radius=6,
                fg_color=COLORS["surface"],
                hover_color=COLORS["border"],
                text_color=COLORS["accent"],
                font=ctk.CTkFont(size=11),
                command=lambda p=entry.output_path: self._open_path(Path(p)),
            ).pack(side="right")

    def _clear_history(self):
        self._history.clear()
        self._refresh()

    def _open_path(self, path: Path):
        try:
            if os.name == "nt":
                os.startfile(path)
        except Exception:
            pass