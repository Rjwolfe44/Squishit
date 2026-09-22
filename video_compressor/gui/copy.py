"""User-facing GUI copy and progress phrasing.

Encode choices stay in the quality ladder and compressor. This module only
decides what the window says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

from ..core.descriptions import (
    STREAMING_UPLOAD_DESCRIPTION,
    STREAMING_UPLOAD_LABEL,
    YOUTUBE_UPLOAD_DESCRIPTION,
    YOUTUBE_UPLOAD_LABEL,
)
from ..core.utils import format_size, format_time

PROFILE_HELPER = "Full app profiles (incl. Archival Max)."
QUICK_COMPRESS_TITLE = "Quick Compress"
QUICK_COMPRESS_SUBTITLE = (
    "Explorer one-click · Lite / Balanced / HEVC Max — not the full profile list."
)
DROP_ZONE_TITLE = "Drop videos or images"
DROP_ZONE_NEXT = "They queue on the left. Choose a profile, then press Compress."
EMPTY_QUEUE = "Add files to start"
MORE_MENU_LABEL = "More…"
MORE_PROFILES_TOOLTIP = (
    "YouTube / social upload, Mobile, Streaming upload, and custom profiles. "
    "Streaming upload is a video file for Twitch, YouTube, or OBS upload."
)
WAITING_FOR_SOFTWARE_FALLBACK = "Waiting for software fallback answer…"
GITHUB_REPO_URL = "https://github.com/Rjwolfe44/Squishit"
BUG_REPORT_URL = f"{GITHUB_REPO_URL}/issues/new?labels=bug"
FEATURE_REQUEST_URL = f"{GITHUB_REPO_URL}/issues/new?labels=enhancement"
HELP_REPORT_BUG = "Report a bug"
HELP_SUGGEST_FEATURE = "Suggest a feature"
PROGRESS_PHASES: Sequence[str] = ("Queued", "Encoding", "Finishing")
FINISHING_PROGRESS = 92.0

__all__ = [
    "DROP_ZONE_NEXT",
    "DROP_ZONE_TITLE",
    "EMPTY_QUEUE",
    "FINISHING_PROGRESS",
    "MORE_MENU_LABEL",
    "MORE_PROFILES_TOOLTIP",
    "PROFILE_HELPER",
    "PROGRESS_PHASES",
    "QUICK_COMPRESS_SUBTITLE",
    "QUICK_COMPRESS_TITLE",
    "STREAMING_UPLOAD_DESCRIPTION",
    "STREAMING_UPLOAD_LABEL",
    "WAITING_FOR_SOFTWARE_FALLBACK",
    "YOUTUBE_UPLOAD_DESCRIPTION",
    "YOUTUBE_UPLOAD_LABEL",
    "encoder_family_label",
    "help_menu_items",
    "human_progress_status",
    "job_status_value",
    "profile_ui_text",
    "progress_detail",
    "progress_phase",
    "secondary_menu_entries",
]


def help_menu_items() -> list[tuple[str, str]]:
    """Help menu rows: label and the GitHub new-issue URL it opens."""

    return [
        (HELP_REPORT_BUG, BUG_REPORT_URL),
        (HELP_SUGGEST_FEATURE, FEATURE_REQUEST_URL),
    ]


def profile_ui_text(name: str, description: str = "") -> tuple[str, str]:
    """Return the label and tooltip for a profile name.

    YouTube and Streaming keep their stored names. The window shows file-upload
    labels, and those tooltips come from the shared description constants.
    """

    if name == "YouTube Upload":
        return YOUTUBE_UPLOAD_LABEL, YOUTUBE_UPLOAD_DESCRIPTION
    if name == "Streaming":
        return STREAMING_UPLOAD_LABEL, STREAMING_UPLOAD_DESCRIPTION
    return name, description


def secondary_menu_entries(
    profiles: Iterable[Any],
    main_names: Iterable[str],
) -> list[tuple[str, str]]:
    """Menu rows for profiles that are not the three primary pills.

    Each row is ``(label, stored_name)``. A custom profile that collides with
    a built-in label keeps its stored name so the menu stays unique.
    """

    primary = set(main_names)
    entries: list[tuple[str, str]] = []
    used: dict[str, str] = {}
    for profile in profiles:
        name = getattr(profile, "name", "")
        if not name or name in primary:
            continue
        label = profile_ui_text(name, getattr(profile, "description", "") or "")[0]
        if label in used and used[label] != name:
            label = name
        used[label] = name
        entries.append((label, name))
    return entries


def job_status_value(job: Any) -> str:
    status = getattr(job, "status", "")
    return str(getattr(status, "value", status) or "").lower()


def progress_phase(status: str, progress: float) -> str:
    """Map a job onto Queued, Encoding, or Finishing."""

    status = (status or "").lower()
    try:
        amount = float(progress or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if status in {"queued", "analyzing"}:
        return "Queued"
    if status == "paused":
        return "Encoding" if amount > 0 else "Queued"
    if status == "completed":
        return "Finishing"
    finishing = status in {"compressing", "failed", "cancelled"}
    if amount >= FINISHING_PROGRESS and finishing:
        return "Finishing"
    if status == "compressing" or amount > 0:
        return "Encoding"
    return "Queued"


def encoder_family_label(encoder_name: str) -> str:
    """Short hardware family name. Software encoders keep their FFmpeg name."""

    name = encoder_name or ""
    lower = name.lower()
    if "nvenc" in lower:
        return "NVENC"
    if "qsv" in lower:
        return "QSV"
    if "amf" in lower:
        return "AMF"
    return name


def human_progress_status(job: Any, *, awaiting_software_fallback: bool = False) -> str:
    """One sentence for the progress card footer."""

    if awaiting_software_fallback:
        return WAITING_FOR_SOFTWARE_FALLBACK
    status = job_status_value(job)
    if status == "queued":
        return "Waiting in the queue…"
    if status == "analyzing":
        return "Analyzing the file…"
    if status == "paused":
        return "Paused"
    if status == "completed":
        return "Done"
    if status == "failed":
        return "Could not finish this file"
    if status == "cancelled":
        return "Cancelled"
    try:
        amount = float(getattr(job, "progress", 0) or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if status == "compressing" and amount >= FINISHING_PROGRESS:
        return "Finishing…"
    if status == "compressing":
        family = encoder_family_label(str(getattr(job, "encoder_name", "") or ""))
        line = f"Encoding with {family}…" if family else "Encoding…"
        try:
            attempt = int(getattr(job, "attempt_count", 0) or 0)
        except (TypeError, ValueError):
            attempt = 0
        if attempt > 1:
            line = f"{line} · retry {attempt}"
        return line
    return "Waiting in the queue…"


def progress_detail(job: Any) -> str:
    """ETA, size, and speed when the job actually has them."""

    parts: list[str] = []
    profile = getattr(job, "profile", None)
    profile_name = getattr(profile, "name", "") or ""
    if profile_name:
        label, _description = profile_ui_text(
            profile_name,
            getattr(profile, "description", "") or "",
        )
        parts.append(label)
    try:
        eta = float(getattr(job, "eta", 0) or 0)
    except (TypeError, ValueError):
        eta = 0.0
    if eta > 0:
        parts.append(f"ETA {format_time(eta)}")
    size_text = _size_text(job)
    if size_text:
        parts.append(size_text)
    try:
        speed = float(getattr(job, "speed", 0) or 0)
    except (TypeError, ValueError):
        speed = 0.0
    if speed > 0:
        parts.append(f"{speed:.1f} fps")
    return "  ·  ".join(parts)


def _size_text(job: Any) -> str:
    info = getattr(job, "video_info", None)
    try:
        original = int(getattr(info, "size", 0) or 0)
    except (TypeError, ValueError):
        original = 0
    output_size = 0
    output = getattr(job, "output_file", None)
    if output:
        path = Path(output)
        try:
            if path.is_file():
                output_size = int(path.stat().st_size)
        except OSError:
            output_size = 0
    if original > 0 and output_size > 0:
        return f"{format_size(original)} → {format_size(output_size)}"
    if output_size > 0:
        return format_size(output_size)
    if original > 0:
        return format_size(original)
    return ""
