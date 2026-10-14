"""Custom profile editor for the Qt shell.

This is the PySide6 port of the profile dialog the CustomTkinter window had.
It reads and writes profiles through ``ProfileManager`` only, so a profile
saved here is the same YAML file the CLI and the main window load.

Wiring from the main window::

    dialog = ProfileEditorDialog(self, self._profile_manager, on_changed=refresh)
    dialog.exec()

``on_changed()`` runs after every save, delete, or import (and
``profiles_changed`` is emitted at the same moments). The caller shares the
manager, so ``manager.get_all_profiles()`` is already up to date by then.

Choices are limited to what the chosen codec's encoder supports
(``ENCODER_REGISTRY``) and to containers the codec matrix allows. The values
a custom profile carries (codec, CRF, preset, ...) are used exactly as set
when the profile is picked in the main window. Only the built-in Quick,
Balanced, and Max presets are locked to the shared quality ladder.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Optional

import yaml
from PySide6.QtCore import QStandardPaths, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.codecs import (
    ENCODER_REGISTRY,
    AudioCodec,
    AudioMode,
    CodecManager,
    RateControl,
    VideoCodec,
)
from ...core.profiles import (
    CompressionProfile,
    ProfileManager,
    ProfileType,
    normalize_profile_preset,
)
from ...core.quality_ladder import QUICK_COMPRESS_LITE_LABEL, QUICK_COMPRESS_MAX_LABEL
from ...core.utils import sanitize_filename
from ..copy import MORE_MENU_LABEL, profile_ui_text
from .encode_form import (
    CODEC_CHOICES,
    CODEC_LABELS,
    EXACT_AUDIO,
    GOVERNORS,
    TARGET_MODES,
    containers_for,
    default_container,
)
from .theme import DARK_STYLESHEET

__all__ = [
    "EncoderOptions",
    "ProfileEditorDialog",
    "crf_tier",
    "encoder_options",
    "is_builtin_profile",
    "name_format_error",
    "profile_file_stem",
    "reserved_profile_stems",
    "validate_profile_name",
    "validate_profile_values",
]

logger = logging.getLogger(__name__)

_BUILTIN_TYPES = {ProfileType.FAST, ProfileType.BALANCED, ProfileType.MAX}
_NAME_MAX = 60
_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_FILE_FILTER = "YAML files (*.yaml *.yml);;All files (*)"

# "<" is not allowed in a profile name, so the unsaved-draft row can never
# collide with a real profile.
_DRAFT_KEY = "<draft>"
_ROLE_NAME = Qt.ItemDataRole.UserRole
_ROLE_LOCKED = Qt.ItemDataRole.UserRole.value + 1

_NO_TUNE = "none"

_SHORT_CODEC_NAMES = {
    VideoCodec.H264: "H.264",
    VideoCodec.HEVC: "HEVC",
    VideoCodec.VP9: "VP9",
    VideoCodec.SVT_AV1: "SVT-AV1",
    VideoCodec.AV1: "AV1",
}

_RATE_CONTROL_LABELS = {
    RateControl.CRF: "Constant quality (recommended)",
    RateControl.CQP: "Fixed quality level",
    RateControl.CBR: "Constant bitrate",
    RateControl.ABR: "Average bitrate",
    RateControl.VBR: "Variable bitrate",
    RateControl.ICQ: "Smart quality (Intel)",
    RateControl.QVBR: "Quality-based bitrate",
    RateControl.LOSSLESS: "Lossless (very large files)",
}
# Modes where the quality number on the slider is what the encoder uses.
_QUALITY_RATE_CONTROLS = frozenset(
    {RateControl.CRF, RateControl.CQP, RateControl.ICQ, RateControl.QVBR}
)

_TUNE_LABELS = {
    "film": "Live action (film)",
    "animation": "Cartoons and animation",
    "grain": "Keep film grain",
    "stillimage": "Slideshows and still images",
    "psnr": "Quality tests (PSNR)",
    "ssim": "Quality tests (SSIM)",
    "fastdecode": "Easy to play on weak devices",
    "zerolatency": "Live streaming (no delay)",
}

_CONTAINER_LABELS = {
    "mp4": "MP4 (plays almost everywhere)",
    "mkv": "MKV (flexible, good for archives)",
    "webm": "WebM (made for the web)",
    "mov": "MOV (Apple-friendly)",
    "avi": "AVI (old, limited)",
}

_RESOLUTIONS = (
    (0, "Original size"),
    (2160, "4K (2160p)"),
    (1440, "1440p"),
    (1080, "1080p"),
    (720, "720p"),
    (480, "480p"),
)
_FRAME_RATES = (
    (0, "Original"),
    (60, "60 fps"),
    (30, "30 fps"),
    (24, "24 fps"),
)

_AUDIO_MODE_LABELS = (
    (AudioMode.AUTO, "Automatic"),
    (AudioMode.COPY, "Keep the original audio"),
    (AudioMode.REENCODE, "Re-encode the audio"),
    (AudioMode.STRIP, "Remove the audio"),
)
_AUDIO_CODEC_LABELS = {
    AudioCodec.AAC: "AAC",
    AudioCodec.OPUS: "Opus",
    AudioCodec.MP3: "MP3",
    AudioCodec.VORBIS: "Vorbis",
    AudioCodec.AC3: "AC-3",
    AudioCodec.FLAC: "FLAC (lossless)",
    AudioCodec.ALAC: "ALAC (lossless)",
}
_AUDIO_BITRATES = (320_000, 256_000, 192_000, 160_000, 128_000, 96_000, 64_000)

_TARGET_MODE_LABELS = {
    "auto": "Automatic",
    "fast": "Fast (rough match)",
    "balanced": "Balanced",
    "strict": "Strict (close match)",
    "exact": "Exact (slower)",
}
_EXACT_AUDIO_LABELS = {
    "keep": "Keep the audio as it is",
    "reduce": "Lower the audio quality if needed",
    "drop": "Remove the audio if needed",
}
_GOVERNOR_LABELS = {
    "auto": "Automatic",
    "low": "Gentle (keeps the PC responsive)",
    "balanced": "Balanced",
    "max": "Use everything (fastest)",
}

# Thresholds are on the 0-51 x264/x265 scale. Encoders with a 0-63 scale are
# scaled down first so the words stay roughly right for them.
_CRF_TIERS = (
    (0, "Lossless"),
    (15, "Near-lossless"),
    (18, "Very high quality"),
    (22, "High quality"),
    (26, "Good quality"),
    (30, "Medium quality"),
    (36, "Low quality"),
    (45, "Very low quality"),
)

_EXTRA_STYLES = """
QLabel#editorMessage {
    background: transparent;
    font-size: 12px;
    color: #d4d4d8;
}
QLabel#editorMessage[kind="error"] {
    color: #fca5a5;
}
QLabel#editorMessage[kind="ok"] {
    color: #86efac;
}
QLabel#lockedBanner {
    background: #27272a;
    border: 1px solid #52525b;
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
}
QLineEdit[invalid="true"], QSpinBox[invalid="true"] {
    border: 1px solid #ef4444;
}
QListWidget#profileList {
    background: #18181b;
    border: 1px solid #3f3f46;
    border-radius: 10px;
    padding: 4px;
    outline: none;
}
QListWidget#profileList::item {
    padding: 7px 8px;
    border-radius: 6px;
}
QListWidget#profileList::item:selected {
    background: #3b82f6;
    color: #ffffff;
}
QListWidget#profileList::item:hover:!selected {
    background: #27272a;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #3f3f46;
    border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background: #3b82f6;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #fafafa;
    width: 16px;
    margin: -6px 0;
    border-radius: 8px;
}
QSlider::sub-page:horizontal:disabled {
    background: #52525b;
}
QSlider::handle:horizontal:disabled {
    background: #71717a;
}
QComboBox:disabled, QLineEdit:disabled, QSpinBox:disabled {
    color: #a1a1aa;
}
"""


# ---------------------------------------------------------------------------
# Pure helpers (no widgets, safe to test directly)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncoderOptions:
    """What the software encoder behind a codec lets a profile choose."""

    codec: VideoCodec
    presets: tuple[str, ...]
    default_preset: str
    tunes: tuple[str, ...]
    rate_controls: tuple[RateControl, ...]
    crf_min: int
    crf_max: int
    crf_default: int


def encoder_options(codec: VideoCodec) -> EncoderOptions:
    """Preset, tune, rate-control, and CRF choices from ``ENCODER_REGISTRY``.

    Presets that the profile loader would rewrite (such as ``placebo``, which
    ``normalize_profile_preset`` turns into ``slow``) are left out so a saved
    value always reads back the same.
    """

    registry = ENCODER_REGISTRY.get(codec.ffmpeg_encoder, {})
    presets = tuple(
        preset
        for preset in registry.get("presets", ())
        if normalize_profile_preset(preset) == preset
    ) or ("medium",)
    default_preset = registry.get("default_preset")
    if default_preset not in presets:
        default_preset = "medium" if "medium" in presets else presets[0]
    low, high = registry.get("crf_range", (0, 51))
    crf_default = min(max(int(registry.get("crf_default", 28)), low), high)
    return EncoderOptions(
        codec=codec,
        presets=presets,
        default_preset=default_preset,
        tunes=tuple(registry.get("tunes") or ()),
        rate_controls=tuple(registry.get("rate_controls") or (RateControl.CRF,)),
        crf_min=int(low),
        crf_max=int(high),
        crf_default=crf_default,
    )


def crf_tier(crf: int, crf_max: int = 51) -> str:
    """Plain-language word for a CRF number, like the legacy editor showed."""

    scaled = crf * 51 / crf_max if crf_max > 51 else crf
    label = _CRF_TIERS[0][1]
    for threshold, text in _CRF_TIERS:
        if scaled >= threshold:
            label = text
    return label


def profile_file_stem(name: str) -> str:
    """File stem ``ProfileManager`` uses for a profile name.

    Two names with the same stem share one YAML file, so they count as the
    same name here.
    """

    return name.lower().replace(" ", "_")


def is_builtin_profile(profile: CompressionProfile) -> bool:
    """True for the profiles that ship with SquishIt and stay read-only."""

    return profile.created_by == "system" or profile.profile_type in _BUILTIN_TYPES


def reserved_profile_stems() -> set[str]:
    """File stems a new profile may not take (built-in and Quick Compress names)."""

    names = {profile.name for profile in ProfileManager.DEFAULT_PROFILES}
    names.update((QUICK_COMPRESS_LITE_LABEL, QUICK_COMPRESS_MAX_LABEL))
    return {profile_file_stem(name) for name in names}


def name_format_error(name: str) -> str:
    """Why ``name`` cannot be a profile (and file) name, or an empty string."""

    text = (name or "").strip()
    if not text:
        return "Give your profile a name."
    if len(text) > _NAME_MAX:
        return f"Use {_NAME_MAX} characters or fewer."
    if any(char in _ILLEGAL_NAME_CHARS or ord(char) < 32 for char in text):
        shown = " ".join(_ILLEGAL_NAME_CHARS)
        return f"Names can't contain any of these characters: {shown}"
    if text.endswith("."):
        return "Names can't end with a dot."
    if profile_file_stem(text).split(".")[0].upper() in _RESERVED_DEVICE_NAMES:
        return "Windows doesn't allow that name. Try another one."
    return ""


def validate_profile_name(
    name: str,
    *,
    taken: Collection[str] = (),
    reserved: Collection[str] = (),
    own_stem: Optional[str] = None,
) -> str:
    """Name problem as a sentence, or an empty string when the name is fine.

    ``taken`` and ``reserved`` hold file stems. ``own_stem`` is the stem of
    the profile being edited, which may keep its own name.
    """

    text = (name or "").strip()
    problem = name_format_error(text)
    if problem:
        return problem
    stem = profile_file_stem(text)
    if stem == own_stem:
        return ""
    if stem in reserved:
        return "That name belongs to a built-in preset. Pick another one."
    if stem in taken:
        return "You already have a profile with that name."
    return ""


def _is_whole_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return int(value) == value


def validate_profile_values(profile: CompressionProfile) -> str:
    """Quality and size problems as a sentence, or an empty string."""

    options = encoder_options(profile.video_codec)
    codec_name = _SHORT_CODEC_NAMES.get(profile.video_codec, profile.video_codec.value)
    if not _is_whole_number(profile.crf):
        return "The quality level must be a whole number."
    if not options.crf_min <= int(profile.crf) <= options.crf_max:
        return (
            f"{codec_name} quality level must be between "
            f"{options.crf_min} and {options.crf_max}."
        )
    if profile.target_size_mb is not None:
        if not _is_whole_number(profile.target_size_mb):
            return "The file size target must be a whole number of MB."
        if int(profile.target_size_mb) <= 0:
            return "The file size target must be above 0 MB."
    return ""


def _clean_name_base(name: str) -> str:
    """A profile name with characters a file name can't hold swapped for dashes."""

    cleaned = "".join(
        "-" if char in _ILLEGAL_NAME_CHARS or ord(char) < 32 else char
        for char in name
    )
    squeezed = re.sub(r"\s+", " ", cleaned).strip(" .")
    return squeezed[: _NAME_MAX - 14].strip(" .") or "Profile"


def _preset_label(preset: str, options: EncoderOptions) -> str:
    if preset.isdigit():
        numbers = [int(item) for item in options.presets if item.isdigit()]
        if numbers and int(preset) == min(numbers):
            return f"{preset} (slowest, smallest file)"
        if numbers and int(preset) == max(numbers):
            return f"{preset} (fastest, larger file)"
        if preset == options.default_preset:
            return f"{preset} (default)"
        return preset
    return {
        "ultrafast": "ultrafast (fastest, larger file)",
        "medium": "medium (balanced)",
        "veryslow": "veryslow (slowest, smallest file)",
    }.get(preset, preset)


def _make_lock_icon(color: str = "#d4d4d8") -> QIcon:
    """Small padlock drawn in code, so the list needs no image files."""

    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(1.8)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawArc(4, 1, 8, 9, 0, 180 * 16)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(3, 7, 10, 8, 2, 2)
    painter.end()
    return QIcon(pixmap)


def _section(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("section")
    return label


def _hint(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def _set_flag(widget: QWidget, name: str, value: Any) -> None:
    """Set a dynamic property and re-run the stylesheet for that widget."""

    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _fill_combo(
    combo: QComboBox,
    entries: list[tuple[str, Any]],
    wanted: Any,
    default: Any,
    *,
    keep_unknown: bool = False,
    unknown_label: Optional[str] = None,
) -> None:
    """Replace a combo's items and select ``wanted``.

    A ``wanted`` value that is not in ``entries`` is added as an extra row
    when ``keep_unknown`` is set (so a profile written by hand loads without
    being changed). Otherwise ``default`` is selected.
    """

    combo.clear()
    for label, data in entries:
        combo.addItem(label, data)
    index = combo.findData(wanted) if wanted is not None else -1
    if index < 0 and keep_unknown and wanted not in (None, ""):
        combo.addItem(unknown_label or str(wanted), wanted)
        index = combo.count() - 1
    if index < 0:
        index = max(0, combo.findData(default))
    combo.setCurrentIndex(index)


def _select_data(
    combo: QComboBox, value: Any, label: Optional[str] = None
) -> None:
    """Select the row holding ``value``, adding it first if it is missing."""

    index = combo.findData(value)
    if index < 0:
        combo.addItem(label if label is not None else str(value), value)
        index = combo.count() - 1
    combo.setCurrentIndex(index)


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


class ProfileEditorDialog(QDialog):
    """List and form for creating, editing, and sharing custom profiles.

    Built-in profiles show a lock and stay read-only (Duplicate makes an
    editable copy). New and Duplicate start an unsaved draft that only
    becomes a file when Save succeeds.
    """

    profiles_changed = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        profile_manager: Optional[ProfileManager] = None,
        on_changed: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self.setObjectName("profileEditor")
        self.setWindowTitle("Custom profiles")
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setStyleSheet(DARK_STYLESHEET + _EXTRA_STYLES)
        self.setMinimumSize(780, 540)
        self.resize(960, 700)

        self._pm = profile_manager if profile_manager is not None else ProfileManager()
        self._on_changed = on_changed
        self._codec_manager = CodecManager()
        self._lock_icon = _make_lock_icon()
        self._last_dir: Optional[Path] = None

        self._loading = True  # stays on until the first profile is loaded
        self._suppress_list = 0
        self._current: Optional[str] = None
        self._draft: Optional[CompressionProfile] = None
        self._base: Optional[CompressionProfile] = None
        self._locked = False
        self._dirty = False
        self._hw_pref = True

        self._build()
        self._rebuild_list()
        first = self._default_selection()
        if first is not None:
            self._open(first)
        self._loading = False
        self._update_buttons()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        title = QLabel("Custom profiles")
        title.setObjectName("title")
        root.addWidget(title)
        root.addWidget(
            _hint(
                "Make your own presets. Saved profiles show up under "
                f"{MORE_MENU_LABEL} on the main screen."
            )
        )

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(self._build_list_column())
        body.addLayout(self._build_form_column(), 1)
        root.addLayout(body, 1)

    def _build_list_column(self) -> QWidget:
        column = QWidget()
        column.setFixedWidth(250)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._list = QListWidget()
        self._list.setObjectName("profileList")
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._list.currentItemChanged.connect(self._on_current_item_changed)
        layout.addWidget(self._list, 1)

        first_row = QHBoxLayout()
        first_row.setSpacing(6)
        self._new_btn = self._tool_button("New", "Start a new profile", self.new_profile)
        self._duplicate_btn = self._tool_button(
            "Duplicate", "Make an editable copy of this profile", self.duplicate_current
        )
        self._delete_btn = self._tool_button(
            "Delete", "Delete this profile", self.delete_current
        )
        for button in (self._new_btn, self._duplicate_btn, self._delete_btn):
            first_row.addWidget(button)
        layout.addLayout(first_row)

        second_row = QHBoxLayout()
        second_row.setSpacing(6)
        self._import_btn = self._tool_button(
            "Import…", "Add a profile from a YAML file", self.import_profile
        )
        self._export_btn = self._tool_button(
            "Export…", "Save this profile as a YAML file", self.export_current
        )
        second_row.addWidget(self._import_btn)
        second_row.addWidget(self._export_btn)
        layout.addLayout(second_row)
        return column

    def _tool_button(self, text: str, tip: str, slot: Callable[[], Any]) -> QPushButton:
        button = QPushButton(text)
        button.setToolTip(tip)
        button.setAutoDefault(False)
        button.clicked.connect(lambda _checked=False: slot())
        return button

    def _build_form_column(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setSpacing(8)

        self._banner = QLabel(
            "This is a built-in profile, so it can't be changed. "
            "Use Duplicate to make your own version."
        )
        self._banner.setObjectName("lockedBanner")
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        column.addWidget(self._banner)

        self._body = QWidget()
        self._build_form(self._body)
        scroll = QScrollArea()
        scroll.setObjectName("profileFormScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(self._body)
        scroll.verticalScrollBar().setSingleStep(28)
        column.addWidget(scroll, 1)

        self._message = QLabel("")
        self._message.setObjectName("editorMessage")
        self._message.setWordWrap(True)
        self._message.setProperty("kind", "info")
        self._message.setVisible(False)
        column.addWidget(self._message)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._save_btn = QPushButton("Save")
        self._save_btn.setObjectName("primary")
        self._save_btn.setAutoDefault(False)
        self._save_btn.clicked.connect(lambda _checked=False: self.save())
        self._close_btn = QPushButton("Close")
        self._close_btn.setAutoDefault(False)
        self._close_btn.clicked.connect(self.reject)
        buttons.addWidget(self._save_btn)
        buttons.addWidget(self._close_btn)
        column.addLayout(buttons)
        return column

    def _build_form(self, body: QWidget) -> None:
        root = QVBoxLayout(body)
        root.setContentsMargins(0, 0, 10, 8)
        root.setSpacing(8)

        def new_form() -> QFormLayout:
            form = QFormLayout()
            form.setSpacing(8)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            return form

        # About
        root.addWidget(_section("Profile"))
        about = new_form()
        self._name = QLineEdit()
        self._name.setObjectName("profileName")
        self._name.setPlaceholderText("For example: Phone clips")
        self._name.textChanged.connect(self._on_text_edited)
        about.addRow("Name", self._name)
        self._desc = QLineEdit()
        self._desc.setObjectName("profileDescription")
        self._desc.setPlaceholderText("Optional note about when to use it")
        self._desc.textChanged.connect(self._on_text_edited)
        about.addRow("Description", self._desc)
        root.addLayout(about)

        # Video
        root.addSpacing(6)
        root.addWidget(_section("Video"))
        root.addWidget(
            _hint(
                "These values are used exactly as set when you pick this profile "
                f"under {MORE_MENU_LABEL}. The Quick Compress presets keep "
                "their own fixed settings."
            )
        )
        video = new_form()
        self._codec = QComboBox()
        self._codec.setObjectName("profileCodec")
        for codec in CODEC_CHOICES:
            self._codec.addItem(CODEC_LABELS[codec], codec.value)
        self._codec.currentIndexChanged.connect(self._on_codec_changed)
        video.addRow("Codec", self._codec)

        self._preset = QComboBox()
        self._preset.setObjectName("profilePreset")
        self._preset.setToolTip(
            "Slower settings take longer but usually make a smaller file. "
            "Hardware encoders choose their own speed."
        )
        self._preset.currentIndexChanged.connect(self._on_combo_edited)
        video.addRow("Encoding speed", self._preset)
        self._preset_hint = _hint("")
        video.addRow(self._preset_hint)

        self._rate = QComboBox()
        self._rate.setObjectName("profileRate")
        self._rate.currentIndexChanged.connect(self._on_rate_changed)
        video.addRow("Quality mode", self._rate)
        self._rate_hint = _hint("")
        self._rate_hint.setVisible(False)
        video.addRow(self._rate_hint)

        crf_row = QHBoxLayout()
        crf_row.setSpacing(10)
        self._crf = QSlider(Qt.Orientation.Horizontal)
        self._crf.setObjectName("profileCrf")
        self._crf.setRange(0, 51)
        self._crf.valueChanged.connect(self._on_crf_changed)
        crf_row.addWidget(self._crf, 1)
        self._crf_label = QLabel("")
        self._crf_label.setObjectName("muted")
        self._crf_label.setMinimumWidth(170)
        crf_row.addWidget(self._crf_label)
        video.addRow("Quality level", crf_row)
        self._crf_hint = _hint("")
        video.addRow(self._crf_hint)

        self._tune = QComboBox()
        self._tune.setObjectName("profileTune")
        self._tune.currentIndexChanged.connect(self._on_combo_edited)
        video.addRow("Optimize for", self._tune)

        self._container = QComboBox()
        self._container.setObjectName("profileContainer")
        self._container.currentIndexChanged.connect(self._on_container_changed)
        video.addRow("File type", self._container)

        self._resolution = QComboBox()
        self._resolution.setObjectName("profileResolution")
        self._resolution.setToolTip("Bigger videos are scaled down to this height.")
        for height, label in _RESOLUTIONS:
            self._resolution.addItem(label, height)
        self._resolution.currentIndexChanged.connect(self._on_combo_edited)
        video.addRow("Largest size", self._resolution)

        self._fps = QComboBox()
        self._fps.setObjectName("profileFps")
        self._fps.setToolTip("Faster videos are slowed to this frame rate.")
        for rate, label in _FRAME_RATES:
            self._fps.addItem(label, rate)
        self._fps.currentIndexChanged.connect(self._on_combo_edited)
        video.addRow("Highest frame rate", self._fps)
        root.addLayout(video)

        # Audio
        root.addSpacing(6)
        root.addWidget(_section("Audio"))
        audio = new_form()
        self._audio_mode = QComboBox()
        self._audio_mode.setObjectName("profileAudioMode")
        for mode, label in _AUDIO_MODE_LABELS:
            self._audio_mode.addItem(label, mode.value)
        self._audio_mode.currentIndexChanged.connect(self._on_audio_changed)
        audio.addRow("Audio", self._audio_mode)

        self._audio_codec = QComboBox()
        self._audio_codec.setObjectName("profileAudioCodec")
        for codec, label in _AUDIO_CODEC_LABELS.items():
            self._audio_codec.addItem(label, codec.value)
        self._audio_codec.currentIndexChanged.connect(self._on_audio_changed)
        audio.addRow("Audio format", self._audio_codec)

        self._audio_kbps = QComboBox()
        self._audio_kbps.setObjectName("profileAudioBitrate")
        for bitrate in _AUDIO_BITRATES:
            self._audio_kbps.addItem(f"{bitrate // 1000} kbps", bitrate)
        self._audio_kbps.currentIndexChanged.connect(self._on_audio_changed)
        audio.addRow("Audio quality", self._audio_kbps)
        self._audio_hint = _hint("")
        audio.addRow(self._audio_hint)
        root.addLayout(audio)

        # Size target
        root.addSpacing(6)
        root.addWidget(_section("Size target"))
        self._target_on = QCheckBox("Aim for a file size")
        self._target_on.setObjectName("profileTargetOn")
        self._target_on.toggled.connect(self._on_target_changed)
        root.addWidget(self._target_on)
        target = new_form()
        self._target_mb = QSpinBox()
        self._target_mb.setObjectName("profileTargetMb")
        self._target_mb.setRange(0, 200000)
        self._target_mb.setSuffix(" MB")
        self._target_mb.valueChanged.connect(self._on_target_changed)
        target.addRow("Target size", self._target_mb)
        self._target_mode = QComboBox()
        self._target_mode.setObjectName("profileTargetMode")
        self._target_mode.currentIndexChanged.connect(self._on_target_changed)
        target.addRow("How close", self._target_mode)
        self._exact_audio = QComboBox()
        self._exact_audio.setObjectName("profileExactAudio")
        self._exact_audio.currentIndexChanged.connect(self._on_target_changed)
        target.addRow("If audio must shrink", self._exact_audio)
        root.addLayout(target)
        self._two_pass = QCheckBox("Two passes for an exact size")
        self._two_pass.setObjectName("profileTwoPass")
        self._two_pass.toggled.connect(self._on_target_changed)
        root.addWidget(self._two_pass)
        root.addWidget(
            _hint(
                "Optional. When on, SquishIt picks the quality that lands near "
                "this size. The two settings under it matter for Exact only."
            )
        )
        for value in TARGET_MODES:
            self._target_mode.addItem(_TARGET_MODE_LABELS.get(value, value), value)
        for value in EXACT_AUDIO:
            self._exact_audio.addItem(_EXACT_AUDIO_LABELS.get(value, value), value)

        # Hardware
        root.addSpacing(6)
        root.addWidget(_section("Hardware"))
        self._hw = QCheckBox("Use hardware encoding when available")
        self._hw.setObjectName("profileHardware")
        self._hw.toggled.connect(self._on_hw_toggled)
        root.addWidget(self._hw)
        self._hw_hint = _hint("")
        root.addWidget(self._hw_hint)
        hardware = new_form()
        self._governor = QComboBox()
        self._governor.setObjectName("profileGovernor")
        for value in GOVERNORS:
            self._governor.addItem(_GOVERNOR_LABELS.get(value, value), value)
        self._governor.currentIndexChanged.connect(self._on_combo_edited)
        hardware.addRow("Computer effort", self._governor)
        root.addLayout(hardware)
        root.addStretch(1)

    # -- list ---------------------------------------------------------------

    def _ordered_profiles(self) -> list[CompressionProfile]:
        profiles = self._pm.get_all_profiles()
        built_in = [item for item in profiles if is_builtin_profile(item)]
        custom = sorted(
            (item for item in profiles if not is_builtin_profile(item)),
            key=lambda item: item.name.casefold(),
        )
        return built_in + custom

    def _default_selection(self) -> Optional[str]:
        ordered = self._ordered_profiles()
        for profile in ordered:
            if not is_builtin_profile(profile):
                return profile.name
        return ordered[0].name if ordered else None

    def _make_item(self, profile: CompressionProfile) -> QListWidgetItem:
        label = profile_ui_text(profile.name, profile.description)[0]
        locked = is_builtin_profile(profile)
        item = QListWidgetItem(label)
        item.setData(_ROLE_NAME, profile.name)
        item.setData(_ROLE_LOCKED, locked)
        if locked:
            item.setIcon(self._lock_icon)
            item.setToolTip(
                f"{label} is built in and can't be changed. "
                "Use Duplicate to make your own version."
            )
        elif profile.description:
            item.setToolTip(profile.description)
        return item

    def _draft_label(self) -> str:
        name = self._name.text().strip() if self._draft is not None else ""
        return f"{name or 'Untitled'}  (not saved)"

    def _rebuild_list(self, select: Optional[str] = None) -> None:
        self._suppress_list += 1
        try:
            self._list.clear()
            for profile in self._ordered_profiles():
                self._list.addItem(self._make_item(profile))
            if self._draft is not None:
                item = QListWidgetItem(self._draft_label())
                item.setData(_ROLE_NAME, _DRAFT_KEY)
                item.setData(_ROLE_LOCKED, False)
                self._list.addItem(item)
            self._select_row(select)
        finally:
            self._suppress_list -= 1

    def _select_row(self, key: Optional[str]) -> None:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(_ROLE_NAME) == key:
                self._list.setCurrentItem(item)
                return
        self._list.setCurrentRow(-1)

    def _restore_selection(self) -> None:
        self._suppress_list += 1
        try:
            self._select_row(self._open_key())
        finally:
            self._suppress_list -= 1

    def _open_key(self) -> Optional[str]:
        return _DRAFT_KEY if self._draft is not None else self._current

    def _on_current_item_changed(
        self, current: Optional[QListWidgetItem], _previous: Optional[QListWidgetItem]
    ) -> None:
        if self._suppress_list or current is None:
            return
        key = current.data(_ROLE_NAME)
        if key == self._open_key():
            return
        if not self._resolve_unsaved():
            self._restore_selection()
            return
        self._open(str(key))

    # -- opening profiles ---------------------------------------------------

    def select_profile(self, name: str) -> bool:
        """Open a saved profile by its stored name.

        Returns False when no such profile exists or the user cancels the
        unsaved-changes prompt.
        """

        if self._pm.get_profile(name) is None:
            return False
        if name == self._open_key():
            return True
        if not self._resolve_unsaved():
            return False
        self._open(name)
        return True

    def _open(self, name: str) -> None:
        profile = self._pm.get_profile(name)
        self._draft = None
        if profile is None:
            self._current = None
            self._rebuild_list()
            self._base = None
            self._locked = False
            self._dirty = False
            self._update_buttons()
            return
        self._current = name
        self._rebuild_list(select=name)
        self._load_form(profile, locked=is_builtin_profile(profile))

    def _start_draft(self, profile: CompressionProfile) -> None:
        self._draft = profile
        self._current = None
        self._rebuild_list(select=_DRAFT_KEY)
        self._load_form(profile, locked=False)
        self._dirty = True
        self._update_buttons()
        self._name.setFocus()
        self._name.selectAll()

    # -- unsaved changes ----------------------------------------------------

    def has_unsaved_changes(self) -> bool:
        """True while a draft or edited profile has not been saved."""

        return self._draft is not None or (self._dirty and not self._locked)

    def _resolve_unsaved(self) -> bool:
        """Ask what to do with unsaved work. False means stay where you are."""

        if not self.has_unsaved_changes():
            return True
        name = self._name.text().strip() or "this profile"
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f'Save your changes to "{name}" first?',
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Save:
            return self.save()
        return answer == QMessageBox.StandardButton.Discard

    def _reopen_clean(self) -> None:
        """Throw away unsaved work and reload what is on disk."""

        keep = self._current if self._draft is None else None
        self._draft = None
        self._dirty = False
        name = keep if keep and self._pm.get_profile(keep) else self._default_selection()
        if name is not None:
            self._open(name)

    def done(self, result: int) -> None:
        """Closing by any route asks about unsaved work first."""

        if not self._resolve_unsaved():
            return
        if self.has_unsaved_changes():
            # Only reachable after Discard. Start clean if the dialog is reused.
            self._reopen_clean()
        super().done(result)

    # -- form <-> profile ---------------------------------------------------

    def _load_form(self, profile: CompressionProfile, *, locked: bool) -> None:
        clamp_note = ""
        self._loading = True
        try:
            self._base = profile
            self._locked = locked
            self._name.setText(profile.name)
            self._desc.setText(profile.description or "")
            codec = profile.video_codec
            # SVT-AV1 is always saved with hardware off. That is forced, not a
            # preference, so it must not carry over to the next codec.
            self._hw_pref = (
                True if codec == VideoCodec.SVT_AV1 else bool(profile.use_hw_accel)
            )
            self._codec.setCurrentIndex(max(0, self._codec.findData(codec.value)))
            options = encoder_options(codec)
            crf = profile.crf if _is_whole_number(profile.crf) else options.crf_default
            self._fill_codec_choices(
                codec,
                preset=profile.preset,
                tune=profile.tune or _NO_TUNE,
                rate=profile.rate_control,
                container=profile.video_container,
                crf=int(crf),
                keep_unknown=True,
            )
            if int(crf) != self._crf.value():
                clamp_note = (
                    f"The saved quality level ({int(crf)}) was outside what "
                    f"{_SHORT_CODEC_NAMES[codec]} allows, so it is shown as "
                    f"{self._crf.value()}."
                )

            _select_data(
                self._resolution,
                int(profile.max_resolution or 0),
                f"{profile.max_resolution}p",
            )
            rate_value = profile.frame_rate or 0
            _select_data(self._fps, rate_value, f"{rate_value:g} fps")

            try:
                mode = AudioMode(profile.audio_mode).value
            except ValueError:
                mode = AudioMode.AUTO.value
            self._audio_mode.setCurrentIndex(self._audio_mode.findData(mode))
            _select_data(
                self._audio_codec,
                profile.audio_codec.value,
                profile.audio_codec.display_name,
            )
            bitrate = int(profile.audio_bitrate or 0)
            _select_data(
                self._audio_kbps,
                bitrate,
                f"{bitrate // 1000} kbps" if bitrate else "Automatic",
            )

            self._target_on.setChecked(bool(profile.target_size_mb))
            self._target_mb.setValue(int(profile.target_size_mb or 50))
            _select_data(
                self._target_mode, profile.target_size_mode or "auto",
            )
            _select_data(
                self._exact_audio, profile.exact_audio_policy or "reduce",
            )
            self._two_pass.setChecked(bool(profile.exact_two_pass))
            _select_data(self._governor, profile.resource_governor or "auto")
            self._sync_all()
        finally:
            self._loading = False
        self._dirty = False
        self._set_flag_name(False)
        self._set_flag_target(False)
        if clamp_note:
            self._show_message(clamp_note, "info")
        else:
            self._clear_message()
        self._update_buttons()

    def _fill_codec_choices(
        self,
        codec: VideoCodec,
        *,
        preset: Optional[str],
        tune: str,
        rate: Optional[str],
        container: Optional[str],
        crf: Optional[int],
        keep_unknown: bool = False,
    ) -> None:
        """Rebuild every choice that depends on the codec."""

        options = encoder_options(codec)
        _fill_combo(
            self._preset,
            [(_preset_label(item, options), item) for item in options.presets],
            preset,
            options.default_preset,
            keep_unknown=keep_unknown,
        )
        _fill_combo(
            self._tune,
            [("None", _NO_TUNE)]
            + [(_TUNE_LABELS.get(item, item), item) for item in options.tunes],
            tune,
            _NO_TUNE,
            keep_unknown=keep_unknown,
        )
        _fill_combo(
            self._rate,
            [
                (_RATE_CONTROL_LABELS.get(item, item.value), item.value)
                for item in options.rate_controls
            ],
            rate,
            RateControl.CRF.value
            if RateControl.CRF in options.rate_controls
            else options.rate_controls[0].value,
            keep_unknown=keep_unknown and _is_rate_control(rate),
            unknown_label=_rate_label(rate),
        )
        allowed = containers_for(codec)
        _fill_combo(
            self._container,
            [(_CONTAINER_LABELS.get(item, item), item) for item in allowed],
            container,
            default_container(codec),
        )
        self._crf.setRange(options.crf_min, options.crf_max)
        self._crf.setTickInterval(5)
        self._crf.setValue(
            crf if crf is not None else options.crf_default
        )

    def _collect_profile(self) -> CompressionProfile:
        """Build a profile from the form on top of the profile being edited.

        Fields the form does not show (bitrate, trim, extra FFmpeg arguments,
        and so on) come from the loaded profile, so editing never drops them.
        """

        if self._base is not None:
            data = dict(self._base.to_dict())
        else:
            data = CompressionProfile(name="", profile_type=ProfileType.CUSTOM).to_dict()
        codec = VideoCodec(self._codec.currentData())
        created_by = str(data.get("created_by") or "user")
        if created_by == "system":
            created_by = "user"
        tune = self._tune.currentData()
        target_on = self._target_on.isChecked()
        data.update(
            {
                "name": self._name.text().strip(),
                "profile_type": ProfileType.CUSTOM.value,
                "description": self._desc.text().strip(),
                "video_codec": codec.value,
                "crf": self._crf.value(),
                "preset": self._preset.currentData(),
                "rate_control": self._rate.currentData(),
                "tune": None if tune in (None, _NO_TUNE) else tune,
                "video_container": self._container.currentData(),
                "max_resolution": self._resolution.currentData() or None,
                "frame_rate": self._fps.currentData() or None,
                "audio_mode": self._audio_mode.currentData(),
                "audio_codec": self._audio_codec.currentData(),
                "audio_bitrate": int(self._audio_kbps.currentData() or 0),
                "use_hw_accel": self._hardware_value(codec),
                "target_size_mb": self._target_mb.value() if target_on else None,
                "target_size_mode": self._target_mode.currentData(),
                "exact_audio_policy": self._exact_audio.currentData(),
                "exact_two_pass": self._two_pass.isChecked(),
                "resource_governor": self._governor.currentData(),
                "created_by": created_by,
            }
        )
        return CompressionProfile.from_dict(data)

    def _hardware_value(self, codec: VideoCodec) -> bool:
        if codec == VideoCodec.SVT_AV1:
            return False
        return self._hw.isChecked()

    # -- reacting to edits --------------------------------------------------

    def _touch(self) -> None:
        """A field changed: mark dirty, recheck, and refresh the buttons."""

        self._dirty = True
        if self._draft is not None:
            item = self._draft_item()
            if item is not None:
                item.setText(self._draft_label())
        self._refresh_validation()
        self._update_buttons()

    def _draft_item(self) -> Optional[QListWidgetItem]:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(_ROLE_NAME) == _DRAFT_KEY:
                return item
        return None

    def _on_text_edited(self, _text: str = "") -> None:
        if self._loading:
            return
        self._touch()

    def _on_combo_edited(self, _index: int = 0) -> None:
        if self._loading:
            return
        self._touch()

    def _on_codec_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        codec = VideoCodec(self._codec.currentData())
        self._loading = True
        try:
            self._fill_codec_choices(
                codec,
                preset=self._preset.currentData(),
                tune=self._tune.currentData() or _NO_TUNE,
                rate=self._rate.currentData(),
                container=self._container.currentData(),
                crf=self._crf.value(),
            )
            self._sync_all()
        finally:
            self._loading = False
        self._touch()

    def _on_rate_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            self._sync_rate()
        finally:
            self._loading = False
        self._touch()

    def _on_crf_changed(self, value: int) -> None:
        self._update_crf_label(value)
        if self._loading:
            return
        self._touch()

    def _on_container_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        self._sync_audio()
        self._touch()

    def _on_audio_changed(self, _index: int = 0) -> None:
        if self._loading:
            return
        self._sync_audio()
        self._touch()

    def _on_target_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        self._sync_target()
        self._touch()

    def _on_hw_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self._hw_pref = bool(checked)
        self._touch()

    # -- keeping dependent controls in step ---------------------------------

    def _sync_all(self) -> None:
        codec = VideoCodec(self._codec.currentData())
        self._sync_hardware(codec)
        self._sync_rate()
        self._sync_audio()
        self._sync_target()
        self._update_crf_label(self._crf.value())

    def _sync_hardware(self, codec: VideoCodec) -> None:
        if codec == VideoCodec.SVT_AV1:
            self._hw.setEnabled(False)
            self._hw.setChecked(False)
            self._hw_hint.setText(
                "SVT-AV1 always runs on your processor. It gives the smallest "
                "files but takes the longest."
            )
        else:
            self._hw.setEnabled(True)
            self._hw.setChecked(self._hw_pref)
            if codec == VideoCodec.AV1:
                self._hw_hint.setText(
                    "Fast hardware AV1 uses your graphics card when it can "
                    "(NVIDIA, Intel, or AMD) and your processor otherwise."
                )
            else:
                self._hw_hint.setText(
                    "Your graphics card does the work when it can (NVIDIA, "
                    "Intel, or AMD). Otherwise your processor does."
                )
        options = encoder_options(codec)
        self._crf_hint.setText(
            "Lower numbers look better but make bigger files. A good starting "
            f"point for {_SHORT_CODEC_NAMES[codec]} is {options.crf_default}."
        )
        if options.presets and options.presets[0].isdigit():
            self._preset_hint.setText(
                "Lower numbers are slower and usually make smaller files."
            )
        else:
            self._preset_hint.setText(
                "Slower speeds take longer and usually make smaller files."
            )

    def _sync_rate(self) -> None:
        try:
            rate = RateControl(self._rate.currentData())
        except ValueError:
            rate = RateControl.CRF
        uses_quality = rate in _QUALITY_RATE_CONTROLS
        self._crf.setEnabled(uses_quality)
        if uses_quality:
            text = ""
        elif rate == RateControl.LOSSLESS:
            text = "Lossless keeps every detail, so files get very large."
        else:
            text = (
                "This mode aims for a steady data rate, so the quality level "
                "does not apply. Set a file size below to control how big the "
                "result is."
            )
        self._rate_hint.setText(text)
        self._rate_hint.setVisible(bool(text))

    def _sync_audio(self) -> None:
        try:
            mode = AudioMode(self._audio_mode.currentData())
        except ValueError:
            mode = AudioMode.AUTO
        try:
            codec = AudioCodec(self._audio_codec.currentData())
        except ValueError:
            codec = AudioCodec.AAC
        encodes = mode in (AudioMode.AUTO, AudioMode.REENCODE)
        self._audio_codec.setEnabled(encodes)
        self._audio_kbps.setEnabled(encodes and not codec.is_lossless)
        if mode == AudioMode.COPY:
            text = "The audio is copied exactly as it is."
        elif mode == AudioMode.STRIP:
            text = "The result has no sound."
        else:
            text = "The audio is re-encoded with the format above."
            container = self._container.currentData()
            if container:
                try:
                    usable = self._codec_manager.coerce_audio_codec(codec, container)
                except ValueError:
                    usable = codec
                if usable != codec:
                    text = (
                        f"{_AUDIO_CODEC_LABELS.get(codec, codec.value)} can't go "
                        f"in a {str(container).upper()} file, so SquishIt will "
                        f"use {_AUDIO_CODEC_LABELS.get(usable, usable.value)} "
                        "instead."
                    )
        self._audio_hint.setText(text)

    def _sync_target(self) -> None:
        on = self._target_on.isChecked()
        exact = self._target_mode.currentData() == "exact"
        self._target_mb.setEnabled(on)
        self._target_mode.setEnabled(on)
        self._exact_audio.setEnabled(on and exact)
        self._two_pass.setEnabled(on and exact)

    def _update_crf_label(self, value: int) -> None:
        self._crf_label.setText(f"{value} — {crf_tier(value, self._crf.maximum())}")

    # -- validation and messages --------------------------------------------

    def _taken_stems(self) -> set[str]:
        own = profile_file_stem(self._current) if self._current else None
        return {
            profile_file_stem(profile.name)
            for profile in self._pm.get_all_profiles()
            if profile_file_stem(profile.name) != own
        }

    def _validate(self, profile: CompressionProfile) -> list[tuple[str, str]]:
        """Problems with ``profile`` as ``(field, message)`` pairs."""

        problems: list[tuple[str, str]] = []
        own = profile_file_stem(self._current) if self._current else None
        message = validate_profile_name(
            profile.name,
            taken=self._taken_stems(),
            reserved=reserved_profile_stems(),
            own_stem=own,
        )
        if message:
            problems.append(("name", message))
        if self._target_on.isChecked() and self._target_mb.value() <= 0:
            problems.append(("target", "Enter a file size above 0 MB."))
        else:
            values = validate_profile_values(profile)
            if values:
                problems.append(("values", values))
        return problems

    def _refresh_validation(self) -> None:
        if self._locked or self._loading:
            return
        problems = self._validate(self._collect_profile())
        fields = {field for field, _message in problems}
        self._set_flag_name("name" in fields)
        self._set_flag_target("target" in fields)
        if problems:
            self._show_message(problems[0][1], "error")
        else:
            self._clear_message()

    def _set_flag_name(self, invalid: bool) -> None:
        _set_flag(self._name, "invalid", invalid)

    def _set_flag_target(self, invalid: bool) -> None:
        _set_flag(self._target_mb, "invalid", invalid)

    def _show_message(self, text: str, kind: str = "info") -> None:
        self._message.setText(text)
        _set_flag(self._message, "kind", kind)
        self._message.setVisible(bool(text))

    def _clear_message(self) -> None:
        self._show_message("", "info")

    def message_text(self) -> str:
        """The note or error currently shown under the form."""

        return self._message.text()

    def _update_buttons(self) -> None:
        has_open = self._open_key() is not None
        saved_open = self._current is not None and self._draft is None
        self._save_btn.setEnabled(
            has_open and not self._locked and (self._dirty or self._draft is not None)
        )
        self._delete_btn.setEnabled(has_open and not self._locked)
        self._duplicate_btn.setEnabled(saved_open)
        self._export_btn.setEnabled(saved_open)
        self._banner.setVisible(self._locked)
        self._body.setEnabled(has_open and not self._locked)

    def _notify_changed(self) -> None:
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:  # a broken refresh callback must not break the editor
                logger.exception("Profile list refresh failed")
        self.profiles_changed.emit()

    # -- actions ------------------------------------------------------------

    def _unique_name(self, first: str, numbered: Callable[[int], str]) -> str:
        taken = {
            profile_file_stem(profile.name) for profile in self._pm.get_all_profiles()
        } | reserved_profile_stems()
        candidate = first
        index = 1
        while profile_file_stem(candidate) in taken:
            index += 1
            candidate = numbered(index)
        return candidate

    def new_profile(self) -> bool:
        """Start an unsaved profile with sensible starting values."""

        if not self._resolve_unsaved():
            return False
        options = encoder_options(VideoCodec.HEVC)
        name = self._unique_name("New profile", lambda index: f"New profile {index}")
        self._start_draft(
            CompressionProfile(
                name=name,
                profile_type=ProfileType.CUSTOM,
                description="",
                video_codec=VideoCodec.HEVC,
                crf=options.crf_default,
                preset=options.default_preset,
                audio_codec=AudioCodec.AAC,
                audio_bitrate=128_000,
                video_container="mp4",
                use_hw_accel=True,
                created_by="user",
            )
        )
        return True

    def duplicate_current(self) -> bool:
        """Start an unsaved copy of the open profile under a new name."""

        if self._draft is not None:
            return False
        if not self._resolve_unsaved():
            return False
        source = self._pm.get_profile(self._current) if self._current else None
        if source is None:
            return False
        base = _clean_name_base(source.name)
        data = source.to_dict()
        data["name"] = self._unique_name(
            f"{base} (copy)", lambda index: f"{base} (copy {index})"
        )
        data["profile_type"] = ProfileType.CUSTOM.value
        data["created_by"] = "user"
        self._start_draft(CompressionProfile.from_dict(data))
        return True

    def save(self) -> bool:
        """Check the form and write the profile. False leaves everything as is."""

        if self._locked:
            self._show_message(
                "Built-in profiles can't be changed. Use Duplicate to make your own.",
                "error",
            )
            return False
        if self._open_key() is None:
            return False
        candidate = self._collect_profile()
        problems = self._validate(candidate)
        if problems:
            fields = {field for field, _message in problems}
            self._set_flag_name("name" in fields)
            self._set_flag_target("target" in fields)
            self._show_message(problems[0][1], "error")
            return False

        previous = self._pm.get_profile(self._current) if self._current else None
        if previous is not None and previous.name != candidate.name:
            if not self._pm.delete_profile(previous.name):
                self._show_message(
                    "SquishIt couldn't rename the profile. Try again.", "error"
                )
                return False
            if not self._pm.save_profile(candidate):
                self._pm.save_profile(previous)
                self._show_message(
                    "SquishIt couldn't save the profile, so the old name was kept. "
                    "Check that the profiles folder can be written to.",
                    "error",
                )
                return False
        elif not self._pm.save_profile(candidate):
            self._show_message(
                "SquishIt couldn't save the profile. "
                "Check that the profiles folder can be written to.",
                "error",
            )
            return False

        saved = self._pm.get_profile(candidate.name) or candidate
        self._draft = None
        self._current = saved.name
        self._rebuild_list(select=saved.name)
        self._load_form(saved, locked=False)
        self._show_message(
            f'Saved "{saved.name}". You can pick it under {MORE_MENU_LABEL} '
            "on the main screen.",
            "ok",
        )
        self._notify_changed()
        return True

    def delete_current(self) -> bool:
        """Delete the open profile (or drop the unsaved draft) after asking."""

        if self._locked:
            return False
        if self._draft is not None:
            answer = QMessageBox.question(
                self,
                "Discard profile",
                "Discard this new profile? It hasn't been saved yet.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
            self._draft = None
            self._dirty = False
            first = self._default_selection()
            if first is not None:
                self._open(first)
            return True
        name = self._current
        if not name or self._pm.get_profile(name) is None:
            return False
        answer = QMessageBox.question(
            self,
            "Delete profile",
            f'Delete "{name}"? This removes its file and can\'t be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        if not self._pm.delete_profile(name):
            QMessageBox.warning(
                self,
                "Delete profile",
                f'SquishIt couldn\'t delete "{name}". The file may be in use.',
            )
            return False
        self._dirty = False
        self._current = None
        first = self._default_selection()
        if first is not None:
            self._open(first)
        self._show_message(f'Deleted "{name}".', "ok")
        self._notify_changed()
        return True

    # -- import / export ----------------------------------------------------

    def _start_dir(self) -> Path:
        if self._last_dir is not None and self._last_dir.exists():
            return self._last_dir
        documents = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        return Path(documents) if documents else Path.home()

    def export_current(self) -> bool:
        """Write the open profile to a YAML file chosen by the user."""

        name = self._current
        profile = self._pm.get_profile(name) if name and self._draft is None else None
        if profile is None:
            self._show_message("Save this profile first, then export it.", "error")
            return False
        if self.has_unsaved_changes():
            answer = QMessageBox.question(
                self,
                "Export profile",
                f'Save your changes to "{profile.name}" first? '
                "The exported file only has saved changes.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Yes:
                if not self.save():
                    return False
                profile = self._pm.get_profile(self._current or "") or profile
        suggested = sanitize_filename(profile_file_stem(profile.name)) + ".yaml"
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "Export profile",
            str(self._start_dir() / suggested),
            _FILE_FILTER,
        )
        if not path:
            return False
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(".yaml")
        try:
            with open(target, "w", encoding="utf-8") as handle:
                yaml.safe_dump(profile.to_dict(), handle, sort_keys=False)
        except (OSError, yaml.YAMLError) as exc:
            QMessageBox.warning(
                self, "Export profile", f"SquishIt couldn't write that file.\n\n{exc}"
            )
            return False
        self._last_dir = target.parent
        self._show_message(f'Exported "{profile.name}" to {target.name}.', "ok")
        return True

    def _read_profile_file(self, path: Path) -> CompressionProfile:
        """Load and check a profile file. Raises ``ValueError`` with a sentence."""

        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                data = yaml.safe_load(handle)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValueError(f"SquishIt couldn't read that file.\n\n{exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("name"), str):
            raise ValueError(
                "That file doesn't look like a SquishIt profile. "
                "A profile file needs a name."
            )
        try:
            profile = CompressionProfile.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"That profile has a setting SquishIt doesn't understand ({exc})."
            ) from exc
        profile.name = profile.name.strip()
        problem = name_format_error(profile.name) or validate_profile_values(profile)
        if problem:
            raise ValueError(problem)
        # Imports are always editable custom profiles, even when the file came
        # from a built-in one.
        profile.profile_type = ProfileType.CUSTOM
        if profile.created_by in (None, "", "system"):
            profile.created_by = "import"
        return profile

    def import_profile(self) -> bool:
        """Add a profile from a YAML file chosen by the user."""

        path, _selected = QFileDialog.getOpenFileName(
            self, "Import profile", str(self._start_dir()), _FILE_FILTER
        )
        if not path:
            return False
        source = Path(path)
        try:
            profile = self._read_profile_file(source)
        except ValueError as exc:
            QMessageBox.warning(self, "Import profile", str(exc))
            return False
        self._last_dir = source.parent
        if not self._resolve_unsaved():
            return False

        stem = profile_file_stem(profile.name)
        existing = next(
            (
                item
                for item in self._pm.get_all_profiles()
                if profile_file_stem(item.name) == stem
            ),
            None,
        )
        renamed = False
        if stem in reserved_profile_stems() or (
            existing is not None and is_builtin_profile(existing)
        ):
            renamed = True
        elif existing is not None:
            answer = QMessageBox.question(
                self,
                "Import profile",
                f'You already have a profile called "{existing.name}". '
                "Replace it with the imported one?\n\nChoose No to keep both.",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Yes:
                if not self._pm.delete_profile(existing.name):
                    QMessageBox.warning(
                        self,
                        "Import profile",
                        f'SquishIt couldn\'t replace "{existing.name}".',
                    )
                    return False
            else:
                renamed = True
        if renamed:
            base = _clean_name_base(profile.name)
            profile.name = self._unique_name(
                f"{base} (imported)", lambda index: f"{base} (imported {index})"
            )
        if not self._pm.save_profile(profile):
            QMessageBox.warning(
                self,
                "Import profile",
                "SquishIt couldn't save the imported profile. "
                "Check that the profiles folder can be written to.",
            )
            return False
        self._open(profile.name)
        note = f'Imported "{profile.name}".'
        if renamed:
            note += " The name was already taken, so a new one was chosen."
        self._show_message(note, "ok")
        self._notify_changed()
        return True


def _is_rate_control(value: Optional[str]) -> bool:
    try:
        RateControl(value)
    except ValueError:
        return False
    return True


def _rate_label(value: Optional[str]) -> Optional[str]:
    try:
        return _RATE_CONTROL_LABELS.get(RateControl(value), str(value))
    except ValueError:
        return None
