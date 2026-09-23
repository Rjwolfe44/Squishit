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

PROFILE_HELPER = "Other presets are here, including archive quality."
QUICK_COMPRESS_TITLE = "Quick Compress"
QUICK_COMPRESS_SUBTITLE = "Pick a preset, then press Quick Compress."
HOME_TAGLINE = "Shrink a video or image."
MORE_OPTIONS_TITLE = "More options"
MORE_OPTIONS_HINT = "Optional. Quick Compress on the left is enough for most files."
EXTRA_SETTINGS_LABEL = "Show extra settings"
DROP_ZONE_TITLE = "Drop a video or image"
DROP_ZONE_NEXT = "Then pick a preset below and press Quick Compress."
EMPTY_QUEUE = "Nothing here yet"
QUICK_PRESET_BLURBS = {
    "Quick Lite": "Fastest. A smaller file without a long wait.",
    "Balanced": "Recommended. Smaller file that still looks good.",
    "HEVC Max": "Smallest of the quick presets. Takes longer.",
}
PROFILE_FRIENDLY = {
    "Fast": "Faster encode, larger file.",
    "Balanced": "Everyday choice in the full preset list.",
    "Max / Archival": "Smallest archive file. Uses software, not the graphics card.",
}
TRAY_HINT = "Closing or minimizing this window does not put SquishIt in the tray."
TRAY_HELP = (
    "Closing or minimizing this window does not put SquishIt in the tray.\n\n"
    "The tray icon is a separate helper. Turn on Start SquishIt tray with Windows "
    "in the installer, or launch SquishIt with --tray. That icon can open SquishIt "
    "or Quick Compress. Quit on the icon exits the tray."
)
HELP_TRAY = "Tray icon"
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
    "EXTRA_SETTINGS_LABEL",
    "FINISHING_PROGRESS",
    "HELP_TRAY",
    "HOME_TAGLINE",
    "MORE_MENU_LABEL",
    "MORE_OPTIONS_HINT",
    "MORE_OPTIONS_TITLE",
    "MORE_PROFILES_TOOLTIP",
    "PROFILE_FRIENDLY",
    "PROFILE_HELPER",
    "PROGRESS_PHASES",
    "QUICK_COMPRESS_SUBTITLE",
    "QUICK_COMPRESS_TITLE",
    "QUICK_PRESET_BLURBS",
    "TRAY_HELP",
    "TRAY_HINT",
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
