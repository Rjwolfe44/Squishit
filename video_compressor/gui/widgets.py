"""
Custom widgets for the media compressor GUI.
Clean, neutral dark theme with customtkinter.
"""

import customtkinter as ctk
from tkinter import filedialog
import tkinter as tk
from pathlib import Path
from typing import Optional, List, Callable, Any, ClassVar, Dict, TYPE_CHECKING
import os

from ..core.codecs import AudioCodec, VideoCodec, CodecManager, RateControl, AudioMode, ENCODER_REGISTRY
from ..core.profiles import CompressionProfile
from ..core.utils import format_size, format_time, parse_timecode
from .scaling import UI_SCALE_OPTIONS

if TYPE_CHECKING:
    from ..core.compressor import CompressionJob


# â”€â”€ Color palette â€” neutral, professional dark â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
COLORS = {
    "bg":             "#0d0d0f",
    "surface":        "#18181b",
    "surface_raised": "#27272a",
    "border":         "#3f3f46",
    "accent":         "#3b82f6",
    "accent_hover":   "#60a5fa",
    "accent_dim":     "#1d3461",
    "text":           "#fafafa",
    "text_dim":       "#a1a1aa",
    "text_muted":     "#52525b",
    "success":        "#22c55e",
    "warning":        "#f59e0b",
    "error":          "#ef4444",
    "progress_track": "#27272a",
}



# â”€â”€ Helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _lbl(parent, text: str, size: int = 12, weight: str = "normal",
         color: str = None, anchor: str = "w", **kw) -> ctk.CTkLabel:
    return ctk.CTkLabel(
        parent, text=text,
        font=ctk.CTkFont(size=size, weight=weight),
        text_color=color or COLORS["text"],
        anchor=anchor, **kw,
    )


def _section_hdr(parent, text: str):
    _lbl(parent, text.upper(), size=10, weight="bold",
         color=COLORS["text_muted"]).pack(anchor="w", pady=(0, 8))


def _bind_adaptive_wrap(
    label: ctk.CTkLabel,
    *,
    minimum: int = 180,
    maximum: Optional[int] = None,
    padding: int = 12,
) -> ctk.CTkLabel:
    """Keep explanatory labels readable as their parent width changes."""

    def _update(_event=None):
        try:
            base_width = label.master.winfo_width()
            if base_width <= 1:
                base_width = label.winfo_width()
            if base_width <= 1:
                label.after(20, _update)
                return
            wrap = max(minimum, base_width - padding)
            if maximum is not None:
                wrap = min(maximum, wrap)
            label.configure(wraplength=wrap)
        except Exception:
            pass

    label.after_idle(_update)
    try:
        label.master.bind("<Configure>", lambda _event: label.after_idle(_update), add="+")
    except Exception:
        pass
    return label


# â”€â”€ HardwareBadge â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class HardwareBadge(ctk.CTkFrame):
    """Small badge in the header showing the active hardware encoder."""

    def __init__(self, master, hw_info, **kwargs):
        super().__init__(
            master, fg_color=COLORS["surface_raised"], corner_radius=8, **kwargs
        )
        self._lbl_widget = None
        self.update_info(hw_info)

    def update_info(self, hw_info) -> None:
        """Replace badge text after the window has already rendered."""
        if self._lbl_widget is not None:
            self._lbl_widget.destroy()

        if hw_info is None:
            text = "Detecting hardware..."
        else:
            vendor = hw_info.preferred_hw_encoder or "cpu"
            vendor_labels = {"amd": "AMD AMF", "nvidia": "NVIDIA NVENC", "intel": "Intel QSV"}
            fallback = vendor_labels.get(vendor, "Software Encoder")

            gpu_name = ""
            for gpu in hw_info.gpus:
                if gpu.vendor.value == vendor and (
                    gpu.encoder_support.get("h264") or gpu.encoder_support.get("hevc")
                ):
                    gpu_name = gpu.name
                    break

            text = gpu_name or fallback

        self._lbl_widget = _lbl(self, text, size=11, color=COLORS["text_dim"])
        self._lbl_widget.pack(side="left", padx=(10, 10), pady=5)


# â”€â”€ DropZone â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class DropZone(ctk.CTkFrame):
    """
    Drop zone that shows a large target when the queue is empty
    and a compact "Add more files" button when files are queued.
    """

    _FULL_H = 200

    def __init__(self, master, on_drop: Callable[[List[str]], None], **kwargs):
        super().__init__(
            master, fg_color=COLORS["surface"], corner_radius=12, **kwargs
        )
        self.pack_propagate(False)
        self.on_drop = on_drop
        self._compact = False
        self._build_full()

    # â”€â”€ states â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _build_full(self):
        for w in self.winfo_children():
            w.destroy()
        self._compact = False
        self.configure(height=self._FULL_H)

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")

        _lbl(inner, "📂", size=40, anchor="center").pack()
        _lbl(inner, "Drop files here", size=16, weight="bold", anchor="center").pack(
            pady=(6, 3)
        )
        _lbl(
            inner,
            "MP4 · MKV · MOV · AVI · WebM · JPG · PNG · WebP · GIF",
            size=11, color=COLORS["text_dim"], anchor="center",
        ).pack()
        button_row = ctk.CTkFrame(inner, fg_color="transparent")
        button_row.pack(pady=(16, 0))
        ctk.CTkButton(
            button_row,
            text="Browse Files",
            width=130, height=34, corner_radius=8,
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#ffffff",
            command=self._browse,
        ).pack(side="left")
        ctk.CTkButton(
            button_row,
            text="Browse Folder",
            width=130, height=34, corner_radius=8,
            fg_color=COLORS["surface_raised"], hover_color=COLORS["border"],
            font=ctk.CTkFont(size=13), text_color=COLORS["text"],
            command=self._browse_folder,
        ).pack(side="left", padx=(8, 0))

        self.bind("<Button-1>", lambda e: self._browse() if e.widget is self else None)
        self.bind("<Enter>", lambda e: self.configure(fg_color=COLORS["surface_raised"]))
        self.bind("<Leave>", lambda e: self.configure(fg_color=COLORS["surface"]))

    def _build_compact(self):
        for w in self.winfo_children():
            w.destroy()
        self._compact = True
        self.configure(height=52)
        ctk.CTkButton(
            self,
            text="+  Add More Files",
            height=34, corner_radius=8,
            fg_color="transparent", hover_color=COLORS["surface_raised"],
            text_color=COLORS["accent"],
            border_width=1, border_color=COLORS["accent_dim"],
            font=ctk.CTkFont(size=12),
            command=self._browse,
        ).pack(fill="x", padx=8, pady=8)

    def set_compact(self, compact: bool):
        if compact != self._compact:
            if compact:
                self._build_compact()
            else:
                self._build_full()

    # â”€â”€ internals â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _browse(self):
        files = filedialog.askopenfilenames(
            title="Select Media Files",
            filetypes=[
                (
                    "Media Files",
                    "*.mp4 *.mkv *.mov *.avi *.webm *.wmv *.flv *.m4v "
                    "*.mpeg *.mpg *.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.gif",
                ),
                ("Video Files", "*.mp4 *.mkv *.mov *.avi *.webm *.wmv *.flv *.m4v *.mpeg *.mpg"),
                ("Image Files", "*.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.gif"),
                ("All Files", "*.*"),
            ],
        )
        if files:
            self.on_drop(list(files))

    def _browse_folder(self):
        folder = filedialog.askdirectory(title="Select Folder")
        if folder:
            self.on_drop([folder])


    def _on_dnd_drop(self, event):
        import re
        raw = event.data or ""
        paths = re.findall(r"\{([^}]+)\}", raw)
        remaining = re.sub(r"\{[^}]+\}", "", raw).split()
        paths += [p for p in remaining if p.strip()]
        if paths:
            self.on_drop([p.strip() for p in paths if p.strip()])


# â”€â”€ FileQueueCard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class FileQueueCard(ctk.CTkFrame):
    """Card representing a file in the compression queue."""

    _ICON_CACHE: ClassVar[Dict[str, str]] = {}

    def __init__(self, master, filepath: str,
                 on_remove: Optional[Callable] = None,
                 on_compress_single: Optional[Callable] = None,
                 on_compare_sample: Optional[Callable] = None,
                 on_move_top: Optional[Callable] = None,
                 **kwargs):
        super().__init__(
            master, fg_color=COLORS["surface"], corner_radius=10, **kwargs
        )
        self._path = Path(filepath)
        self._on_remove = on_remove
        self._on_compress_single = on_compress_single
        self._on_compare_sample = on_compare_sample
        self._on_move_top = on_move_top
        self._info = None
        self._build()
        self.bind("<Button-3>", self._show_context_menu)
        # Also bind on all child widgets so right-click works anywhere on the card
        self.bind_all_children("<Button-3>", self._show_context_menu)

    def _build(self):
        for w in self.winfo_children():
            w.destroy()

        is_image = getattr(self._info, "image_format", None) is not None
        icon = "🖼" if is_image else "🎬"

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=10)

        _lbl(row, icon, size=20).pack(side="left")

        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", fill="x", expand=True, padx=(10, 0))

        _lbl(col, self._path.name, size=13, weight="bold", anchor="w").pack(fill="x")
        _lbl(col, self._meta(is_image), size=11,
             color=COLORS["text_dim"], anchor="w").pack(fill="x", pady=(1, 0))

        if self._on_remove:
            ctk.CTkButton(
                row,
                text="x", width=26, height=26, corner_radius=6,
                fg_color="transparent", hover_color=COLORS["surface_raised"],
                text_color=COLORS["text_muted"], font=ctk.CTkFont(size=11),
                command=lambda: self._on_remove(self._path),
            ).pack(side="right")

    def _meta(self, is_image: bool) -> str:
        if not self._info:
            return "Analyzing..."
        size_str = format_size(self._info.size)
        if is_image:
            return (
                f"{size_str}  ·  {self._info.width}x{self._info.height}"
                f"  ·  {self._info.image_format.upper()}"
            )
        return (
            f"{size_str}  ·  {self._info.resolution_label}"
            f"  ·  {format_time(self._info.duration)}"
        )

    def update_info(self, info):
        self._info = info
        self._build()

    def bind_all_children(self, event: str, handler):
        """Recursively bind an event to all child widgets."""
        for child in self.winfo_children():
            try:
                child.bind(event, handler)
            except Exception:
                pass
            if hasattr(child, 'winfo_children'):
                for sub in child.winfo_children():
                    try:
                        sub.bind(event, handler)
                    except Exception:
                        pass

    def _show_context_menu(self, event):
        menu = tk.Menu(self, tearoff=0, bg=COLORS["surface_raised"],
                       fg=COLORS["text"], activebackground=COLORS["accent"],
                       activeforeground="#ffffff", relief="flat", bd=0)

        if self._on_compress_single:
            menu.add_command(label="Compress this file only",
                             command=lambda: self._on_compress_single(self._path))
            if self._on_compare_sample:
                menu.add_command(label="Compare sample presets",
                                 command=lambda: self._on_compare_sample(self._path))
            menu.add_separator()

        menu.add_command(label="Open file location",
                         command=lambda: os.startfile(self._path.parent)
                         if os.name == "nt" else None)
        menu.add_command(label="Copy file path",
                         command=self._copy_path)
        menu.add_separator()

        if self._on_remove:
            menu.add_command(label="Remove from queue",
                             command=lambda: self._on_remove(self._path))
        if self._on_move_top:
            menu.add_command(label="Move to top",
                             command=lambda: self._on_move_top(self._path))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_path(self):
        self.clipboard_clear()
        self.clipboard_append(str(self._path))


# â”€â”€ ProgressCard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class ProgressCard(ctk.CTkFrame):
    """Card showing compression progress for a single file."""

    _ICONS = {
        "queued":      "⏳",
        "analyzing":   "🔬",
        "compressing": "⚡",
        "paused":      "⏸",
        "completed":   "✓",
        "failed":      "✗",
        "cancelled":   "o",
    }

    def __init__(self, master, job: "CompressionJob",
                 on_cancel: Optional[Callable] = None, **kwargs):
        super().__init__(
            master, fg_color=COLORS["surface"], corner_radius=10, **kwargs
        )
        self.job = job
        self._on_cancel = on_cancel
        self._build()

    def _build(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="x", padx=12, pady=10)

        # Header row
        hdr = ctk.CTkFrame(body, fg_color="transparent")
        hdr.pack(fill="x")

        self._icon_lbl = _lbl(
            hdr, self._ICONS.get(getattr(self.job.status, "value", self.job.status), "·"),
            size=14, color=COLORS["accent"], width=20,
        )
        self._icon_lbl.pack(side="left")

        _lbl(hdr, self.job.input_file.name, size=13, weight="bold").pack(
            side="left", fill="x", expand=True, padx=(8, 0)
        )

        self._pct_lbl = _lbl(
            hdr, "0%", size=12, color=COLORS["text_dim"], width=38, anchor="e"
        )
        self._pct_lbl.pack(side="right")

        if self._on_cancel:
            ctk.CTkButton(
                hdr,
                text="x", width=26, height=26, corner_radius=6,
                fg_color="transparent", hover_color=COLORS["surface_raised"],
                text_color=COLORS["text_muted"], font=ctk.CTkFont(size=11),
                command=lambda: self._on_cancel(self.job.id),
            ).pack(side="right", padx=(0, 4))

        # Progress bar
        self._bar = ctk.CTkProgressBar(
            body, height=4, corner_radius=2,
            fg_color=COLORS["progress_track"],
            progress_color=COLORS["accent"],
        )
        self._bar.pack(fill="x", pady=(8, 5))
        self._bar.set(0)

        # Footer row
        foot = ctk.CTkFrame(body, fg_color="transparent")
        foot.pack(fill="x")
        self._status_lbl = _lbl(foot, "Queued", size=11, color=COLORS["text_dim"])
        self._status_lbl.pack(side="left")
        self._detail_lbl = _lbl(foot, "", size=11, color=COLORS["text_muted"], anchor="e")
        self._detail_lbl.pack(side="right")

    def update(self, job: "CompressionJob"):
        self.job = job
        status_value = getattr(job.status, "value", job.status)
        self._bar.set(job.progress / 100)
        self._pct_lbl.configure(text=f"{job.progress:.0f}%")
        self._icon_lbl.configure(text=self._ICONS.get(status_value, "·"))

        status_text = str(status_value).title()
        if getattr(job, "attempt_count", 0) > 1 and status_value == "compressing":
            status_text = f"Retry {job.attempt_count}"
        self._status_lbl.configure(text=status_text)

        parts = []
        profile_name = getattr(getattr(job, "profile", None), "name", "")
        if profile_name:
            parts.append(profile_name)
        if getattr(job, "encoder_name", ""):
            parts.append(job.encoder_name)
        if getattr(job, "threads_used", 0):
            parts.append(f"{job.threads_used}T")
        if job.speed and job.speed > 0:
            parts.append(f"{job.speed:.1f} fps")
        if job.throughput_mb_s and job.throughput_mb_s > 0:
            parts.append(f"{job.throughput_mb_s:.2f} MB/s")
        if job.eta and job.eta > 0:
            parts.append(f"ETA {format_time(job.eta)}")
        self._detail_lbl.configure(text="  ·  ".join(parts))

        if status_value == "completed":
            self._bar.configure(progress_color=COLORS["success"])
        elif status_value == "failed":
            self._bar.configure(progress_color=COLORS["error"])
        elif status_value == "cancelled":
            self._bar.configure(progress_color=COLORS["text_muted"])


# â”€â”€ ResultCard â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class ResultCard(ctk.CTkFrame):
    """Card showing a compression result with an Open File button."""

    def __init__(self, master, result: Any, **kwargs):
        super().__init__(
            master, fg_color=COLORS["surface"], corner_radius=10, **kwargs
        )
        self.result = result
        self._build()

    def _build(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="x", padx=12, pady=12)

        ok = self.result.success

        # Header
        hdr = ctk.CTkFrame(body, fg_color="transparent")
        hdr.pack(fill="x")

        _lbl(
            hdr, "OK" if ok else "X", size=14, weight="bold",
            color=COLORS["success"] if ok else COLORS["error"], width=20,
        ).pack(side="left")

        title_wrap = ctk.CTkFrame(hdr, fg_color="transparent")
        title_wrap.pack(side="left", fill="x", expand=True, padx=(8, 8))

        title_label = _lbl(
            title_wrap,
            self.result.input_file.name,
            size=13,
            weight="bold",
            anchor="w",
            justify="left",
        )
        title_label.pack(fill="x")
        _bind_adaptive_wrap(title_label, minimum=220, padding=8)

        if ok and self.result.output_file:
            ctk.CTkButton(
                hdr,
                text="Open",
                width=58, height=26, corner_radius=6,
                fg_color=COLORS["surface_raised"], hover_color=COLORS["border"],
                text_color=COLORS["accent"], font=ctk.CTkFont(size=11),
                command=self._open_file,
            ).pack(side="right")

        if ok:
            # Savings progress bar
            pct = self.result.reduction_percent
            bar = ctk.CTkProgressBar(
                body, height=3, corner_radius=2,
                fg_color=COLORS["progress_track"],
                progress_color=COLORS["success"],
            )
            bar.pack(fill="x", pady=(10, 8))
            bar.set(max(0.0, min(1.0, pct / 100)))

            # Stats row
            stats = ctk.CTkFrame(body, fg_color="transparent")
            stats.pack(fill="x")
            for label, value, color in [
                ("Before", format_size(self.result.original_size), COLORS["text_dim"]),
                ("After",  format_size(self.result.compressed_size), COLORS["text"]),
                ("Saved",  self.result.reduction, COLORS["success"]),
                ("Time",   format_time(self.result.encoding_time), COLORS["text_dim"]),
            ]:
                col = ctk.CTkFrame(stats, fg_color="transparent")
                col.pack(side="left", expand=True)
                _lbl(col, label, size=10, color=COLORS["text_muted"], anchor="center").pack()
                _lbl(col, value, size=12, weight="bold", color=color, anchor="center").pack()

            # Output path
            if self.result.output_file:
                path_label = _lbl(
                    body, str(self.result.output_file),
                    size=10, color=COLORS["text_muted"], anchor="w", justify="left",
                )
                path_label.pack(fill="x", pady=(8, 0))
                _bind_adaptive_wrap(path_label, minimum=220, padding=24)
            if getattr(self.result, "note", ""):
                note_label = _lbl(
                    body,
                    self.result.note,
                    size=11, color=COLORS["text_dim"], anchor="w", justify="left",
                )
                note_label.pack(fill="x", pady=(8, 0))
                _bind_adaptive_wrap(note_label, minimum=220, padding=24)
            if getattr(self.result, "encoder_name", ""):
                thread_text = f"  ·  {self.result.threads_used} threads" if getattr(self.result, "threads_used", 0) else ""
                encoder_label = _lbl(
                    body,
                    f"Encoder: {self.result.encoder_name}{thread_text}",
                    size=10, color=COLORS["text_muted"], anchor="w", justify="left",
                )
                encoder_label.pack(fill="x", pady=(6, 0))
                _bind_adaptive_wrap(encoder_label, minimum=220, padding=24)
        else:
            error_label = _lbl(
                body,
                self.result.error_message or "Unknown error",
                size=12, color=COLORS["error"], anchor="w",
            )
            error_label.pack(fill="x", pady=(8, 0))
            _bind_adaptive_wrap(error_label, minimum=240, padding=24)

    def _open_file(self):
        try:
            if os.name == "nt":
                os.startfile(self.result.output_file)
            else:
                import subprocess
                subprocess.Popen(["xdg-open", str(self.result.output_file)])
        except Exception:
            pass


# â”€â”€ ProfileBar â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class ProfileBar(ctk.CTkFrame):
    """
    Profile selector: pill buttons for the three main profiles,
    plus a "More…" dropdown for the rest.
    """

    _MAIN = ["Fast", "Balanced", "Max / Archival"]

    def __init__(self, master, profiles: List[CompressionProfile],
                 on_select: Optional[Callable[[str], None]] = None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.profiles = profiles
        self.on_select = on_select
        self._selected: Optional[str] = None
        self._buttons: dict = {}
        self._more_var: Optional[ctk.StringVar] = None
        self._build()

    def _build(self):
        pill_row = ctk.CTkFrame(self, fg_color="transparent")
        pill_row.pack(fill="x")

        for profile in [p for p in self.profiles if p.name in self._MAIN]:
            short = profile.name.replace(" / Archival", "")
            btn = ctk.CTkButton(
                pill_row,
                text=short, height=30, width=0, corner_radius=8,
                fg_color=COLORS["surface_raised"], hover_color=COLORS["border"],
                text_color=COLORS["text_dim"], font=ctk.CTkFont(size=12),
                command=lambda n=profile.name: self._select(n),
            )
            btn.pack(side="left", padx=(0, 5))
            self._buttons[profile.name] = btn

        other = [p for p in self.profiles if p.name not in self._MAIN]
        if other:
            self._more_var = ctk.StringVar(value="More…")
            ctk.CTkOptionMenu(
                pill_row,
                variable=self._more_var,
                values=["More…"] + [p.name for p in other],
                height=30, width=80, corner_radius=8,
                fg_color=COLORS["surface_raised"],
                button_color=COLORS["border"],
                button_hover_color=COLORS["accent"],
                text_color=COLORS["text_dim"], font=ctk.CTkFont(size=12),
                command=self._on_more,
            ).pack(side="left")

        self._desc_lbl = _lbl(
            self,
            "",
            size=11,
            color=COLORS["text_dim"],
            anchor="w",
            justify="left",
            wraplength=236,
        )
        self._desc_lbl.pack(anchor="w", fill="x", pady=(6, 0))
        _bind_adaptive_wrap(self._desc_lbl, minimum=220, maximum=236, padding=32)

    def _on_more(self, name: str):
        if name != "More…":
            self._select(name)
            if self._more_var:
                self._more_var.set("More…")

    def _select(self, name: str):
        for n, btn in self._buttons.items():
            if n == name:
                btn.configure(fg_color=COLORS["accent"], text_color="#ffffff")
            else:
                btn.configure(
                    fg_color=COLORS["surface_raised"], text_color=COLORS["text_dim"]
                )
        self._selected = name
        profile = next((p for p in self.profiles if p.name == name), None)
        if profile:
            self._desc_lbl.configure(text=profile.description)
        if self.on_select:
            self.on_select(name)

    def get_selected(self) -> Optional[str]:
        return self._selected

    def set_selected(self, name: str):
        self._select(name)

    def refresh(self, profiles: List[CompressionProfile]):
        """Rebuild the profile bar with updated profiles."""
        self.profiles = profiles
        old_selected = self._selected
        for w in self.winfo_children():
            w.destroy()
        self._buttons.clear()
        self._more_var = None
        self._build()
        if old_selected and any(p.name == old_selected for p in profiles):
            self._select(old_selected)


# â”€â”€ SettingsPanel â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class SettingsPanel(ctk.CTkFrame):
    """
    Grouped settings panel with clear sections for target size, media,
    performance, and expert controls.
    """

    _PRESETS = [
        "ultrafast", "superfast", "veryfast", "fast",
        "medium", "slow", "slower", "veryslow",
    ]
    _COMPRESSION_LEVELS = ["Light", "Balanced", "Strong", "Extreme"]
    _CRF_BY_CODEC = {
        VideoCodec.H264: {"Light": 23, "Balanced": 27, "Strong": 31, "Extreme": 35},
        VideoCodec.HEVC: {"Light": 21, "Balanced": 26, "Strong": 30, "Extreme": 34},
        VideoCodec.AV1: {"Light": 24, "Balanced": 28, "Strong": 32, "Extreme": 36},
        VideoCodec.SVT_AV1: {"Light": 24, "Balanced": 28, "Strong": 32, "Extreme": 36},
        VideoCodec.VP9: {"Light": 28, "Balanced": 32, "Strong": 36, "Extreme": 40},
    }
    _CODEC_ORDER = [VideoCodec.H264, VideoCodec.HEVC, VideoCodec.VP9, VideoCodec.SVT_AV1, VideoCodec.AV1]

    def __init__(self, master, profiles: List[CompressionProfile],
                 on_change: Optional[Callable] = None,
                 codec_manager: Optional[CodecManager] = None,
                 hw_vendor: Optional[str] = None,
                 **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.profiles = profiles
        self.on_change = on_change
        self.codec_manager = codec_manager
        self.hw_vendor = hw_vendor
        self._size_mode_on = False
        self._size_entry_row: Optional[ctk.CTkFrame] = None
        self._recommendation_label: Optional[ctk.CTkLabel] = None
        self._build()

    # â”€â”€ helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _menu_row(self, parent, label: str, var: ctk.StringVar,
                  values: list, width: int = 130,
                  command: Optional[Callable[[str], None]] = None) -> ctk.CTkOptionMenu:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, 10))
        _lbl(row, label, size=12, color=COLORS["text_dim"],
             width=95, anchor="w").pack(side="left")
        menu = ctk.CTkOptionMenu(
            row, variable=var, values=values,
            width=width, height=28, corner_radius=7,
            fg_color=COLORS["surface_raised"],
            button_color=COLORS["border"],
            button_hover_color=COLORS["accent"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=12),
            command=(command or (lambda _: self._changed())),
        )
        menu.pack(side="left")
        menu._row_frame = row
        return menu

    def _available_codec_values(self) -> List[str]:
        if not self.codec_manager:
            return ["h264", "hevc", "vp9"]
        codecs = self.codec_manager.get_supported_codecs(hw_vendor=self.hw_vendor)
        ordered = [codec.value for codec in self._CODEC_ORDER if codec in codecs]
        return ordered or ["h264"]

    def _selected_codec(self) -> VideoCodec:
        try:
            return VideoCodec(self.codec_var.get())
        except ValueError:
            return VideoCodec.H264

    def _crf_for_level(self, codec: VideoCodec, level: str) -> int:
        values = self._CRF_BY_CODEC.get(codec, self._CRF_BY_CODEC[VideoCodec.HEVC])
        return values.get(level, values["Balanced"])

    def _level_for_crf(self, codec: VideoCodec, crf: int) -> str:
        values = self._CRF_BY_CODEC.get(codec, self._CRF_BY_CODEC[VideoCodec.HEVC])
        return min(values, key=lambda level: abs(values[level] - crf))

    def _refresh_container_options(self):
        if not hasattr(self, "container_menu"):
            return
        codec = self._selected_codec()
        if self.codec_manager:
            containers = [c.value for c in self.codec_manager.get_supported_containers(codec)]
            default_container = self.codec_manager.get_default_container(codec).value
        else:
            containers = ["mp4", "mkv", "webm", "mov", "avi"]
            default_container = containers[0]
        self.container_menu.configure(values=containers)
        if self.container_var.get() not in containers:
            self.container_var.set(default_container)

    def _refresh_hw_hint(self):
        if not hasattr(self, "_hw_hint"):
            return
        codec = self._selected_codec()
        target_mode = self.target_mode_var.get() if hasattr(self, "target_mode_var") else "balanced"
        if not self.hw_var.get():
            self._hw_hint.configure(text="Hardware acceleration off. SquishIt will use a software encoder.")
            return
        if target_mode == "exact":
            self._hw_hint.configure(text="Exact target mode uses a software exact-size path and may lower audio, FPS, or resolution before padding the final file.")
            return
        if not self.codec_manager or not self.hw_vendor:
            self._hw_hint.configure(text="No supported GPU encoder detected for this system.")
            return
        encoder = self.codec_manager.get_hw_encoder(codec, self.hw_vendor)
        if encoder:
            self._hw_hint.configure(text=f"Hardware acceleration ready: {encoder}. Auto threads still reserve CPU for filters and audio.")
        else:
            self._hw_hint.configure(text=f"{codec.value.upper()} will encode on the CPU on this system.")

    def _on_codec_change(self, value: str):
        self.codec_var.set(value)
        self._refresh_container_options()
        self._refresh_hw_hint()
        self._changed()

    def _on_compression_level_change(self, value: str):
        self.compression_var.set(value)
        self._refresh_hw_hint()
        self._changed()

    def _on_hw_toggle(self):
        self._refresh_hw_hint()
        self._changed()

    def _on_target_mode_change(self, value: str):
        self.target_mode_var.set(value)
        self._refresh_hw_hint()
        self._changed()

    def _set_target_size_row_visibility(self, visible: bool):
        if not self._size_entry_row:
            return

        self._size_entry_row.pack_forget()
        if not visible:
            return

        target_mode_row = getattr(getattr(self, "_target_mode_menu", None), "_row_frame", None)
        if target_mode_row is not None:
            self._size_entry_row.pack(fill="x", pady=(0, 10), before=target_mode_row)
        else:
            self._size_entry_row.pack(fill="x", pady=(0, 10))

    def _sep(self, parent):
        ctk.CTkFrame(parent, height=1, fg_color=COLORS["border"]).pack(
            fill="x", pady=(4, 12)
        )

    # â”€â”€ build â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _build(self):
        scroll = ctk.CTkScrollableFrame(
            self, fg_color="transparent",
            scrollbar_button_color=COLORS["surface_raised"],
            scrollbar_button_hover_color=COLORS["border"],
        )
        scroll.pack(fill="both", expand=True)
        self._scroll = scroll

        self._build_target_size(scroll)
        self._sep(scroll)
        self._build_video(scroll)
        self._sep(scroll)
        self._build_audio(scroll)
        self._sep(scroll)
        self._build_performance(scroll)
        self._sep(scroll)
        self._build_expert(scroll)
        self._sep(scroll)
        self._build_interface(scroll)
        self._sep(scroll)
        self._build_output_naming(scroll)

    def _build_target_size(self, parent):
        _section_hdr(parent, "Target size")

        from ..config import get_config
        cfg = get_config()

        self._size_mode_var = ctk.BooleanVar(value=False)
        sw_row = ctk.CTkFrame(parent, fg_color="transparent")
        sw_row.pack(fill="x", pady=(0, 6))
        ctk.CTkSwitch(
            sw_row, text="  Use target size",
            variable=self._size_mode_var,
            font=ctk.CTkFont(size=12), text_color=COLORS["text_dim"],
            button_color=COLORS["accent"], button_hover_color=COLORS["accent_hover"],
            progress_color=COLORS["accent_dim"],
            command=self._toggle_size,
        ).pack(side="left")

        self._size_entry_row = ctk.CTkFrame(parent, fg_color="transparent")
        _lbl(self._size_entry_row, "Target MB", size=12,
             color=COLORS["text_dim"], width=95, anchor="w").pack(side="left")
        self.target_size_var = ctk.StringVar(value="")
        self.target_size_entry = ctk.CTkEntry(
            self._size_entry_row,
            textvariable=self.target_size_var,
            width=80, height=28, corner_radius=7,
            fg_color=COLORS["surface_raised"], border_color=COLORS["border"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=12),
        )
        self.target_size_entry.pack(side="left")
        self.target_size_var.trace_add("write", lambda *_: self._changed())

        self.target_mode_var = ctk.StringVar(value=cfg.default_target_size_mode)
        self._target_mode_menu = self._menu_row(parent, "Target mode", self.target_mode_var,
                            ["auto", "fast", "balanced", "strict", "exact"], width=110,
                            command=self._on_target_mode_change)

        self._size_preview_label = _lbl(parent, "", size=10, color=COLORS["text_muted"], anchor="w")
        self._size_preview_label.pack(fill="x", pady=(0, 10))
        _bind_adaptive_wrap(self._size_preview_label, minimum=180, padding=18)

    def _build_video(self, parent):
        _section_hdr(parent, "Video")

        codec_values = self._available_codec_values()
        default_codec = "hevc" if "hevc" in codec_values else codec_values[0]
        self.codec_var = ctk.StringVar(value=default_codec)
        self.codec_menu = self._menu_row(parent, "Codec", self.codec_var, codec_values, command=self._on_codec_change)

        self.compression_var = ctk.StringVar(value="Balanced")
        self._menu_row(parent, "Compression", self.compression_var, self._COMPRESSION_LEVELS, command=self._on_compression_level_change)

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, 10))
        _lbl(row, "Encode speed", size=12, color=COLORS["text_dim"],
             width=95, anchor="w").pack(side="left")
        self.preset_var = ctk.StringVar(value="medium")
        self.preset_slider = ctk.CTkSlider(
            row,
            from_=0, to=len(self._PRESETS) - 1,
            number_of_steps=len(self._PRESETS) - 1,
            width=88, height=14,
            fg_color=COLORS["progress_track"],
            progress_color=COLORS["accent"],
            command=self._on_preset,
        )
        self.preset_slider.set(4)
        self.preset_slider.pack(side="left")
        self.preset_lbl = _lbl(row, "medium", size=11,
                                color=COLORS["text_dim"], width=58)
        self.preset_lbl.pack(side="left", padx=(6, 0))

        self.container_var = ctk.StringVar(value="mp4")
        self.container_menu = self._menu_row(parent, "Container", self.container_var, ["mp4", "mkv", "webm", "mov", "avi"])
        self._refresh_container_options()

        self.res_var = ctk.StringVar(value="original")
        self._menu_row(
            parent, "Resolution", self.res_var,
            ["original", "4K", "1440p", "1080p", "720p", "480p"],
        )

        self.fps_var = ctk.StringVar(value="original")
        self._menu_row(parent, "Frame rate", self.fps_var,
                       ["original", "60", "30", "24"])

        self.image_format_var = ctk.StringVar(value="webp")
        self._menu_row(parent, "Image format", self.image_format_var, ["webp", "avif", "jpg", "png", "jxl"])

        self.hw_var = ctk.BooleanVar(value=True)
        hw_row = ctk.CTkFrame(parent, fg_color="transparent")
        hw_row.pack(fill="x", pady=(0, 10))
        ctk.CTkSwitch(
            hw_row, text="  Hardware acceleration",
            variable=self.hw_var,
            font=ctk.CTkFont(size=12), text_color=COLORS["text_dim"],
            button_color=COLORS["accent"], button_hover_color=COLORS["accent_hover"],
            progress_color=COLORS["accent_dim"],
            command=self._on_hw_toggle,
        ).pack(side="left")

        self._hw_hint = _lbl(parent, "", size=10, color=COLORS["text_muted"], anchor="w")
        self._hw_hint.pack(fill="x", pady=(0, 8))
        _bind_adaptive_wrap(self._hw_hint, minimum=180, padding=18)
        self._refresh_hw_hint()

        self._recommendation_label = _lbl(
            parent,
            "",
            size=10,
            color=COLORS["accent_hover"],
            anchor="w",
        )
        self._recommendation_label.pack(fill="x", pady=(0, 6))
        _bind_adaptive_wrap(self._recommendation_label, minimum=180, padding=18)

    def _build_audio(self, parent):
        _section_hdr(parent, "Audio")

        self.output_mode_var = ctk.StringVar(value="video")
        self._menu_row(parent, "Output type", self.output_mode_var,
                       ["video", "audio"], width=110)

        self.audio_codec_var = ctk.StringVar(value="opus")
        self._menu_row(parent, "Codec", self.audio_codec_var,
                       ["aac", "opus", "mp3", "vorbis", "flac", "alac", "ac3"])
        self.audio_var = ctk.StringVar(value="128")
        self._menu_row(parent, "Bitrate", self.audio_var,
                       ["320", "256", "192", "128", "96"])

    def _build_performance(self, parent):
        _section_hdr(parent, "Performance")

        from ..config import get_config
        cfg = get_config()

        self.thread_var = ctk.StringVar(value="auto")
        self._menu_row(parent, "Threads", self.thread_var, ["auto", "2", "4", "6", "8", "12", "16", "24", "32"])

        self.resource_governor_var = ctk.StringVar(value=cfg.resource_governor)
        self._menu_row(parent, "Resource use", self.resource_governor_var,
                       ["auto", "low", "balanced", "max"], width=110)

        self.parallel_var = ctk.StringVar(value=str(cfg.max_parallel_jobs))
        self._menu_row(parent, "Parallel jobs", self.parallel_var,
                       ["1", "2", "3", "4", "6", "8"])

        self.batch_queue_order_var = ctk.StringVar(value=cfg.batch_queue_order)
        self._menu_row(parent, "Queue order", self.batch_queue_order_var,
                   ["manual", "largest-first", "smallest-first", "longest-first", "shortest-first"], width=130)

    def _build_expert(self, parent):
        _section_hdr(parent, "Expert")

        self.exact_audio_policy_var = ctk.StringVar(value="reduce")
        self._menu_row(parent, "Exact audio", self.exact_audio_policy_var,
                       ["keep", "reduce", "drop"], width=110)

        self.exact_two_pass_var = ctk.BooleanVar(value=False)
        exact_row = ctk.CTkFrame(parent, fg_color="transparent")
        exact_row.pack(fill="x", pady=(0, 8))
        ctk.CTkSwitch(
            exact_row,
            text="  Two-pass exact size",
            variable=self.exact_two_pass_var,
            font=ctk.CTkFont(size=12), text_color=COLORS["text_dim"],
            button_color=COLORS["accent"], button_hover_color=COLORS["accent_hover"],
            progress_color=COLORS["accent_dim"],
            command=self._changed,
        ).pack(side="left")

        self._exact_hint_label = _lbl(
            parent,
            "Exact audio only affects exact target mode. Keep preserves audio, reduce lowers bitrate, and drop removes audio only if the target is impossible. Two-pass is slower but lands closer before padding.",
            size=10,
            color=COLORS["text_muted"],
            anchor="w",
        )
        self._exact_hint_label.pack(fill="x", pady=(0, 10))
        _bind_adaptive_wrap(self._exact_hint_label, minimum=180, padding=18)

        self._trim_mode_var = ctk.BooleanVar(value=False)
        trim_switch_row = ctk.CTkFrame(parent, fg_color="transparent")
        trim_switch_row.pack(fill="x", pady=(0, 6))
        ctk.CTkSwitch(
            trim_switch_row, text="  Trim before compress",
            variable=self._trim_mode_var,
            font=ctk.CTkFont(size=12), text_color=COLORS["text_dim"],
            button_color=COLORS["accent"], button_hover_color=COLORS["accent_hover"],
            progress_color=COLORS["accent_dim"],
            command=self._toggle_trim,
        ).pack(side="left")

        self._trim_row = ctk.CTkFrame(parent, fg_color="transparent")
        _lbl(self._trim_row, "Trim", size=12, color=COLORS["text_dim"], width=95, anchor="w").pack(side="left")
        self.trim_start_var = ctk.StringVar(value="")
        self.trim_end_var = ctk.StringVar(value="")
        self.trim_start_entry = ctk.CTkEntry(
            self._trim_row,
            textvariable=self.trim_start_var,
            width=54, height=28, corner_radius=7,
            fg_color=COLORS["surface_raised"], border_color=COLORS["border"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=11),
            placeholder_text="Start",
        )
        self.trim_start_entry.pack(side="left")
        _lbl(self._trim_row, "to", size=11, color=COLORS["text_dim"], width=18, anchor="center").pack(side="left", padx=(4, 4))
        self.trim_end_entry = ctk.CTkEntry(
            self._trim_row,
            textvariable=self.trim_end_var,
            width=54, height=28, corner_radius=7,
            fg_color=COLORS["surface_raised"], border_color=COLORS["border"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=11),
            placeholder_text="End",
        )
        self.trim_end_entry.pack(side="left")

        args_row = ctk.CTkFrame(parent, fg_color="transparent")
        args_row.pack(fill="x", pady=(0, 10))
        _lbl(args_row, "FFmpeg args", size=12, color=COLORS["text_dim"], width=95, anchor="w").pack(side="left")
        self.extra_args_var = ctk.StringVar(value="")
        self._args_entry = ctk.CTkEntry(
            args_row,
            textvariable=self.extra_args_var,
            width=130, height=28, corner_radius=7,
            fg_color=COLORS["surface_raised"], border_color=COLORS["border"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=11),
            placeholder_text="Optional extra flags",
        )
        self._args_entry.pack(side="left")

    def _build_interface(self, parent):
        _section_hdr(parent, "Interface")

        from ..config import get_config
        cfg = get_config()

        self.ui_scale_mode_var = ctk.StringVar(value=getattr(cfg, "ui_scale_mode", "auto"))
        self._menu_row(parent, "UI scale", self.ui_scale_mode_var, UI_SCALE_OPTIONS, width=110)

    def _build_output_naming(self, parent):
        _section_hdr(parent, "Output naming")

        from ..config import get_config
        cfg = get_config()

        tmpl_row = ctk.CTkFrame(parent, fg_color="transparent")
        tmpl_row.pack(fill="x", pady=(0, 6))
        _lbl(tmpl_row, "Template", size=12, color=COLORS["text_dim"],
             width=95, anchor="w").pack(side="left")
        self.template_var = ctk.StringVar(value=cfg.output_name_template)
        self._template_entry = ctk.CTkEntry(
            tmpl_row,
            textvariable=self.template_var,
            width=130, height=28, corner_radius=7,
            fg_color=COLORS["surface_raised"], border_color=COLORS["border"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=11),
        )
        self._template_entry.pack(side="left")

        help_btn = ctk.CTkButton(
            tmpl_row, text="?", width=24, height=24, corner_radius=6,
            fg_color=COLORS["surface_raised"], hover_color=COLORS["border"],
            text_color=COLORS["text_muted"], font=ctk.CTkFont(size=11),
            command=self._show_template_help,
        )
        help_btn.pack(side="left", padx=(4, 0))

        self._template_help_label = _lbl(parent, "", size=10, color=COLORS["text_muted"], anchor="w")
        _bind_adaptive_wrap(self._template_help_label, minimum=180, padding=18)
        # hidden until ? clicked

    # â”€â”€ events â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def _on_preset(self, val):
        idx = int(round(float(val)))
        name = self._PRESETS[idx]
        self.preset_var.set(name)
        self.preset_lbl.configure(text=name)
        self._changed()

    def _toggle_size(self):
        self._size_mode_on = self._size_mode_var.get()
        self._set_target_size_row_visibility(self._size_mode_on)
        self._changed()

    def _toggle_trim(self):
        if self._trim_mode_var.get():
            self._trim_row.pack(fill="x", pady=(0, 10))
        else:
            self._trim_row.pack_forget()
        self._changed()

    def _show_template_help(self):
        txt = (
            "Available tokens:\n"
            "  {name}  — original filename\n"
            "  {profile}  — profile name\n"
            "  {codec}  — video codec\n"
            "  {crf}  — CRF value\n"
            "  {date}  — YYYY-MM-DD\n"
            "  {timestamp}  — HHMMSS"
        )
        if self._template_help_label.cget("text"):
            self._template_help_label.configure(text="")
            self._template_help_label.pack_forget()
        else:
            self._template_help_label.configure(text=txt)
            self._template_help_label.pack(fill="x", pady=(0, 6))

    def _changed(self, *_):
        if self.on_change:
            self.on_change(self.get_settings())

    # â”€â”€ public API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def get_settings(self) -> dict:
        res_map = {
            "original": None, "4K": 2160, "1440p": 1440,
            "1080p": 1080, "720p": 720, "480p": 480,
        }
        fps_map = {"original": None, "60": 60, "30": 30, "24": 24}
        codec_map = {
            "hevc": VideoCodec.HEVC,
            "h264": VideoCodec.H264,
            "vp9": VideoCodec.VP9,
            "svt-av1": VideoCodec.SVT_AV1,
            "av1": VideoCodec.AV1,
        }
        target_size = None
        threads = None
        if self._size_mode_on:
            try:
                target_size = int(self.target_size_entry.get())
            except ValueError:
                pass
        if self.thread_var.get() != "auto":
            try:
                threads = int(self.thread_var.get())
            except ValueError:
                threads = None
        return {
            "max_resolution": res_map.get(self.res_var.get()),
            "frame_rate": fps_map.get(self.fps_var.get()),
            "video_codec": codec_map.get(self.codec_var.get(), VideoCodec.HEVC),
            "crf": self._crf_for_level(codec_map.get(self.codec_var.get(), VideoCodec.HEVC), self.compression_var.get()),
            "video_container": self.container_var.get(),
            "image_format": self.image_format_var.get(),
            "preset": self.preset_var.get(),
            "use_hw_accel": self.hw_var.get(),
            "audio_codec": AudioCodec(self.audio_codec_var.get()),
            "audio_bitrate": int(self.audio_var.get()) * 1000,
            "target_size_mb": target_size,
            "target_size_mode": self.target_mode_var.get(),
            "exact_audio_policy": self.exact_audio_policy_var.get(),
            "exact_two_pass": self.exact_two_pass_var.get(),
            "threads": threads,
            "resource_governor": self.resource_governor_var.get(),
            "parallel_jobs": int(self.parallel_var.get()),
            "batch_queue_order": self.batch_queue_order_var.get(),
            "ui_scale_mode": self.ui_scale_mode_var.get(),
            "output_name_template": self.template_var.get().strip() or "{name}_compressed",
            "output_mode": self.output_mode_var.get(),
            "trim_enabled": self._trim_mode_var.get(),
            "trim_start": parse_timecode(self.trim_start_var.get()),
            "trim_end": parse_timecode(self.trim_end_var.get()),
            "extra_ffmpeg_args": self.extra_args_var.get().strip(),
        }

    def apply_profile(self, profile: CompressionProfile):
        rev_res = {2160: "4K", 1440: "1440p", 1080: "1080p", 720: "720p", 480: "480p"}
        self.res_var.set(rev_res.get(profile.max_resolution, "original"))
        self.fps_var.set(
            str(int(profile.frame_rate)) if profile.frame_rate else "original"
        )
        codec_str = {
            VideoCodec.AV1: "av1",
            VideoCodec.SVT_AV1: "svt-av1",
            VideoCodec.HEVC: "hevc",
            VideoCodec.H264: "h264",
            VideoCodec.VP9: "vp9",
        }
        codec_values = self._available_codec_values()
        codec_value = codec_str.get(profile.video_codec, "hevc")
        if codec_value not in codec_values:
            codec_value = codec_values[0]
        self.codec_menu.configure(values=codec_values)
        self.codec_var.set(codec_value)
        self.compression_var.set(self._level_for_crf(profile.video_codec, profile.crf))
        self._refresh_container_options()
        self.container_var.set(profile.video_container or "mp4")
        self.image_format_var.set(profile.image_format or "webp")
        p = profile.preset if profile.preset in self._PRESETS else "medium"
        self.preset_var.set(p)
        self.preset_slider.set(self._PRESETS.index(p))
        self.preset_lbl.configure(text=p)
        self.hw_var.set(profile.use_hw_accel)
        self.thread_var.set(str(profile.threads) if profile.threads else "auto")
        kbps = str(profile.audio_bitrate // 1000)
        self.audio_var.set(kbps if kbps in ["320", "256", "192", "128", "96"] else "192")
        self.audio_codec_var.set(profile.audio_codec.value)
        self.output_mode_var.set(profile.output_mode or "video")
        self.target_mode_var.set(profile.target_size_mode or "auto")
        self.exact_audio_policy_var.set(getattr(profile, "exact_audio_policy", "reduce") or "reduce")
        self.exact_two_pass_var.set(bool(getattr(profile, "exact_two_pass", False)))
        self.resource_governor_var.set(profile.resource_governor or "auto")
        self.target_size_var.set(str(profile.target_size_mb or ""))
        self._size_mode_var.set(bool(profile.target_size_mb))
        self._size_mode_on = bool(profile.target_size_mb)
        self._set_target_size_row_visibility(self._size_mode_on)
        self._trim_mode_var.set(bool(profile.trim_enabled))
        self.trim_start_var.set(str(profile.trim_start or ""))
        self.trim_end_var.set(str(profile.trim_end or ""))
        self.extra_args_var.set(profile.extra_ffmpeg_args or "")
        self.set_target_size_preview("")
        self._refresh_hw_hint()
        if self._trim_mode_var.get():
            self._trim_row.pack(fill="x", pady=(0, 10))
        else:
            self._trim_row.pack_forget()

    def set_target_size_preview(self, text: str):
        """Update the target-size preview text shown in the settings panel."""

        if hasattr(self, "_size_preview_label"):
            self._size_preview_label.configure(text=text or "")

    def set_recommendation(self, text: str):
        """Update the current source recommendation text in the sidebar."""

        if self._recommendation_label is not None:
            self._recommendation_label.configure(text=text or "")

    def refresh_profiles(self, profiles: List[CompressionProfile]):
        """Update internal profile list after editor changes."""
        self.profiles = profiles


# â”€â”€ Keep VideoCard as an alias so existing imports don't break â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
VideoCard = FileQueueCard

