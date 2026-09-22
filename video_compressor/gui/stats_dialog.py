"""Stats dashboard for lifetime compression history."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

import customtkinter as ctk

from ..config import APP_NAME
from ..core.history import HistoryManager
from ..core.utils import format_size, format_time
from .scaling import apply_dialog_geometry
from .widgets import COLORS, _lbl


class StatsDialog(ctk.CTkToplevel):
    """Modal dashboard summarizing overall compression stats."""

    def __init__(self, master, history_manager: HistoryManager, **kwargs):
        super().__init__(master, fg_color=COLORS["bg"], **kwargs)
        self._history = history_manager
        self.title(f"{APP_NAME} Stats")
        self.transient(master)
        self.grab_set()

        self._build()
        apply_dialog_geometry(self, master, base_width=900, base_height=620, min_width=760, min_height=520)
        self._populate()

    def _build(self):
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=18, pady=18)

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.pack(fill="x", pady=(0, 12))
        _lbl(header, "Stats Dashboard", size=20, weight="bold").pack(side="left")
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
        ).pack(side="right")

        self._scroll = ctk.CTkScrollableFrame(
            root,
            fg_color=COLORS["surface"],
            corner_radius=12,
            scrollbar_button_color=COLORS["surface_raised"],
            scrollbar_button_hover_color=COLORS["border"],
        )
        self._scroll.pack(fill="both", expand=True)

    def _card(self, title: str, body_lines: list[str]):
        card = ctk.CTkFrame(self._scroll, fg_color=COLORS["surface_raised"], corner_radius=10)
        card.pack(fill="x", padx=10, pady=(10, 0))
        _lbl(card, title, size=14, weight="bold").pack(anchor="w", padx=12, pady=(12, 6))
        for line in body_lines:
            _lbl(card, line, size=12, color=COLORS["text_dim"]).pack(anchor="w", padx=12, pady=(0, 4))
        ctk.CTkFrame(card, fg_color="transparent", height=8).pack()

    def _populate(self):
        for child in self._scroll.winfo_children():
            child.destroy()

        entries = self._history.get_entries()
        if not entries:
            self._card("No stats yet", ["Run a few compressions and your dashboard will show totals here."])
            return

        total_input = sum(entry.original_size_bytes for entry in entries)
        total_output = sum(entry.compressed_size_bytes for entry in entries)
        total_saved = max(0, total_input - total_output)
        avg_reduction = sum(entry.reduction_pct for entry in entries) / len(entries)
        total_runtime = sum(entry.duration_seconds for entry in entries)
        largest_save = max(entries, key=lambda entry: entry.original_size_bytes - entry.compressed_size_bytes)

        self._card(
            "Lifetime Totals",
            [
                f"Files processed: {len(entries)}",
                f"Original size total: {format_size(total_input)}",
                f"Compressed size total: {format_size(total_output)}",
                f"Space saved: {format_size(total_saved)}",
                f"Average reduction: {avg_reduction:.1f}%",
                f"Encoding time recorded: {format_time(total_runtime)}",
            ],
        )

        self._card(
            "Best Single Result",
            [
                f"File: {largest_save.input_path}",
                f"Saved: {format_size(max(0, largest_save.original_size_bytes - largest_save.compressed_size_bytes))}",
                f"Profile: {largest_save.profile_name}",
                f"Codec: {largest_save.codec.upper()}",
            ],
        )

        profile_counts = Counter(entry.profile_name for entry in entries)
        codec_counts = Counter(entry.codec.upper() for entry in entries)
        self._card(
            "Top Profiles",
            [f"{name}: {count}" for name, count in profile_counts.most_common(5)],
        )
        self._card(
            "Top Codecs",
            [f"{name}: {count}" for name, count in codec_counts.most_common(5)],
        )

        now = datetime.now()
        last_week = now - timedelta(days=7)
        recent = []
        for entry in entries:
            try:
                if datetime.fromisoformat(entry.timestamp) >= last_week:
                    recent.append(entry)
            except ValueError:
                continue
        recent_saved = max(0, sum(entry.original_size_bytes - entry.compressed_size_bytes for entry in recent))
        self._card(
            "Last 7 Days",
            [
                f"Files: {len(recent)}",
                f"Saved: {format_size(recent_saved)}",
            ],
        )