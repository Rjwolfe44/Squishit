"""Scrollable settings for profiles, Quick Compress, and hardware."""

from __future__ import annotations

from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.codecs import AudioCodec, VideoCodec
from ...core.profiles import CompressionProfile
from ...core.quality_ladder import profile_picker_label
from ..copy import (
    EXTRA_SETTINGS_LABEL,
    MORE_MENU_LABEL,
    MORE_OPTIONS_HINT,
    MORE_OPTIONS_TITLE,
    MORE_PROFILES_TOOLTIP,
    PROFILE_FRIENDLY,
    PROFILE_HELPER,
    QUICK_PRESET_BLURBS,
    profile_ui_text,
    secondary_menu_entries,
)
from .encode_form import (
    AUDIO_CODECS,
    CODEC_CHOICES,
    CODEC_LABELS,
    EXACT_AUDIO,
    FRAME_RATE_LABELS,
    GOVERNORS,
    RESOLUTION_LABELS,
    RUNG_LABELS,
    TARGET_MODES,
    EncodeForm,
    containers_for,
)

_PRIMARY = ["Fast", "Balanced", "Max / Archival"]
ChangeCallback = Callable[[str, Optional[str]], None]


def _section(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("section")
    return label


def _hint(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def _retint(button: QPushButton, selected: bool) -> None:
    button.setProperty("selected", selected)
    style = button.style()
    style.unpolish(button)
    style.polish(button)


class SettingsPane(QWidget):
    """Right-hand column. Scrolling lives on the parent scroll area."""

    def __init__(
        self,
        profiles: Sequence[CompressionProfile],
        form: EncodeForm,
        on_change: ChangeCallback,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.form = form
        self._profiles = list(profiles)
        self._on_change = on_change
        self._loading = False
        self._detected_hw = ""
        self._profile_buttons: dict[str, QPushButton] = {}
        self._build()
        self.load(form)

    def set_profiles(self, profiles: Sequence[CompressionProfile]) -> None:
        self._profiles = list(profiles)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 8, 16)
        root.setSpacing(8)

        root.addWidget(_section(MORE_OPTIONS_TITLE))
        root.addWidget(_hint(MORE_OPTIONS_HINT))

        root.addWidget(_section("Other presets"))
        helper = _hint(PROFILE_HELPER)
        root.addWidget(helper)
        pill_row = QHBoxLayout()
        pill_row.setSpacing(6)
        for profile in self._profiles:
            if profile.name not in _PRIMARY:
                continue
            label, blurb = profile_ui_text(profile.name, profile.description)
            button = QPushButton(profile_picker_label(label))
            button.setObjectName("pill")
            button.setToolTip(PROFILE_FRIENDLY.get(profile.name, blurb))
            button.clicked.connect(
                lambda _checked=False, profile_name=profile.name: self._emit_profile(
                    profile_name
                )
            )
            self._profile_buttons[profile.name] = button
            pill_row.addWidget(button)
        pill_row.addStretch(1)
        root.addLayout(pill_row)

        self._more = QComboBox()
        self._more.setToolTip(MORE_PROFILES_TOOLTIP)
        self._rebuild_more()
        self._more.currentIndexChanged.connect(self._on_more)
        root.addWidget(self._more)
        self._profile_blurb = _hint("")
        root.addWidget(self._profile_blurb)

        self._extra = QCheckBox(EXTRA_SETTINGS_LABEL)
        self._extra.setObjectName("extraSettings")
        self._extra.setToolTip("Codec, file size, output, and performance.")
        root.addWidget(self._extra)

        self._advanced = QWidget()
        advanced = QVBoxLayout(self._advanced)
        advanced.setContentsMargins(0, 8, 0, 0)
        advanced.setSpacing(8)
        self._build_advanced(advanced)
        root.addWidget(self._advanced)
        self._advanced.setVisible(False)
        self._extra.toggled.connect(self._advanced.setVisible)
        root.addStretch(1)

    def _build_advanced(self, root: QVBoxLayout) -> None:
        root.addWidget(_section("Video"))
        form = QFormLayout()
        form.setSpacing(8)
        self._codec = QComboBox()
        for codec in CODEC_CHOICES:
            self._codec.addItem(CODEC_LABELS[codec], codec.value)
        self._codec.currentIndexChanged.connect(self._on_codec)
        form.addRow("Codec", self._codec)

        self._rung = QComboBox()
        self._rung.addItems(RUNG_LABELS)
        self._rung.currentIndexChanged.connect(self._on_rung)
        form.addRow("Compression", self._rung)
        root.addLayout(form)
        self._ladder_hint = _hint("")
        root.addWidget(self._ladder_hint)

        self._hw = QCheckBox("Use hardware encoding")
        self._hw.toggled.connect(self._on_hw)
        root.addWidget(self._hw)
        self._hw_hint = _hint("")
        root.addWidget(self._hw_hint)

        root.addWidget(_section("Target size"))
        self._target_on = QCheckBox("Aim for a file size")
        self._target_on.toggled.connect(self._on_target_toggle)
        root.addWidget(self._target_on)
        size_row = QHBoxLayout()
        self._target_mb = QSpinBox()
        self._target_mb.setRange(1, 200000)
        self._target_mb.setSuffix(" MB")
        self._target_mb.valueChanged.connect(self._on_target_mb)
        size_row.addWidget(self._target_mb)
        size_row.addStretch(1)
        root.addLayout(size_row)
        mode_form = QFormLayout()
        self._target_mode = QComboBox()
        self._target_mode.addItems(TARGET_MODES)
        self._target_mode.currentIndexChanged.connect(self._on_target_mode)
        mode_form.addRow("Size mode", self._target_mode)
        self._exact_audio = QComboBox()
        self._exact_audio.addItems(EXACT_AUDIO)
        self._exact_audio.currentIndexChanged.connect(self._on_exact_audio)
        mode_form.addRow("Exact audio", self._exact_audio)
        root.addLayout(mode_form)
        self._two_pass = QCheckBox("Two passes for an exact size")
        self._two_pass.toggled.connect(self._on_two_pass)
        root.addWidget(self._two_pass)
        self._size_preview = _hint("")
        root.addWidget(self._size_preview)

        root.addWidget(_section("Output"))
        out_form = QFormLayout()
        self._container = QComboBox()
        self._container.currentIndexChanged.connect(self._on_container)
        out_form.addRow("Container", self._container)
        self._resolution = QComboBox()
        self._resolution.addItems(RESOLUTION_LABELS)
        self._resolution.currentIndexChanged.connect(self._on_resolution)
        out_form.addRow("Resolution", self._resolution)
        self._fps = QComboBox()
        self._fps.addItems(FRAME_RATE_LABELS)
        self._fps.currentIndexChanged.connect(self._on_fps)
        out_form.addRow("Frame rate", self._fps)
        self._audio = QComboBox()
        for name in AUDIO_CODECS:
            self._audio.addItem(name, name)
        self._audio.currentIndexChanged.connect(self._on_audio)
        out_form.addRow("Audio", self._audio)
        root.addLayout(out_form)
        self._template = QLineEdit()
        self._template.setPlaceholderText("{name}_compressed")
        self._template.editingFinished.connect(self._on_template)
        template_form = QFormLayout()
        template_form.addRow("File name", self._template)
        root.addLayout(template_form)

        root.addWidget(_section("Performance"))
        perf = QFormLayout()
        self._governor = QComboBox()
        self._governor.addItems(GOVERNORS)
        self._governor.currentIndexChanged.connect(self._on_governor)
        perf.addRow("Computer effort", self._governor)
        self._parallel = QComboBox()
        self._parallel.addItems(["1", "2", "3", "4", "6", "8"])
        self._parallel.currentIndexChanged.connect(self._on_parallel)
        perf.addRow("Files at once", self._parallel)
        root.addLayout(perf)

    def _rebuild_more(self) -> None:
        self._more.blockSignals(True)
        self._more.clear()
        self._more.addItem(MORE_MENU_LABEL, None)
        for label, name in secondary_menu_entries(self._profiles, _PRIMARY):
            self._more.addItem(label, name)
        self._more.blockSignals(False)

    def load(self, form: EncodeForm) -> None:
        """Push ``form`` into the widgets without treating it as a user edit."""

        self.form = form
        self._loading = True
        try:
            codec_index = self._codec.findData(form.video_codec.value)
            if codec_index < 0:
                codec_index = 0
            self._codec.setCurrentIndex(codec_index)
            rung_index = self._rung.findText(form.compression_label)
            self._rung.setCurrentIndex(max(0, rung_index))
            self._refresh_containers()
            container_index = self._container.findText(form.video_container)
            if container_index >= 0:
                self._container.setCurrentIndex(container_index)
            res_index = self._resolution.findText(form.resolution_label)
            self._resolution.setCurrentIndex(max(0, res_index))
            fps_index = self._fps.findText(form.frame_rate_label)
            self._fps.setCurrentIndex(max(0, fps_index))
            audio_value = (
                form.audio_codec.value
                if isinstance(form.audio_codec, AudioCodec)
                else str(form.audio_codec)
            )
            audio_index = self._audio.findData(audio_value)
            if audio_index < 0:
                self._audio.addItem(audio_value, audio_value)
                audio_index = self._audio.findData(audio_value)
            self._audio.setCurrentIndex(max(0, audio_index))
            self._hw.setChecked(form.effective_hw())
            self._target_on.setChecked(form.target_size_enabled)
            self._target_mb.setValue(int(form.target_size_mb or 50))
            self._target_mb.setEnabled(form.target_size_enabled)
            mode_index = self._target_mode.findText(form.target_size_mode)
            self._target_mode.setCurrentIndex(max(0, mode_index))
            exact_index = self._exact_audio.findText(form.exact_audio_policy)
            self._exact_audio.setCurrentIndex(max(0, exact_index))
            self._two_pass.setChecked(form.exact_two_pass)
            gov_index = self._governor.findText(form.resource_governor)
            self._governor.setCurrentIndex(max(0, gov_index))
            parallel_index = self._parallel.findText(str(form.parallel_jobs))
            if parallel_index < 0:
                self._parallel.addItem(str(form.parallel_jobs))
                parallel_index = self._parallel.findText(str(form.parallel_jobs))
            self._parallel.setCurrentIndex(max(0, parallel_index))
            self._template.setText(form.output_name_template)
            self._refresh_hints()
            self._refresh_profile_chrome(form.quick_name)
        finally:
            self._loading = False

    def set_size_preview(self, text: str) -> None:
        self._size_preview.setText(text or "")

    def _refresh_containers(self) -> None:
        current = self.form.video_container
        self._container.blockSignals(True)
        self._container.clear()
        for name in containers_for(self.form.video_codec):
            self._container.addItem(name)
        index = self._container.findText(current)
        self._container.setCurrentIndex(max(0, index))
        if self._container.currentText():
            self.form.video_container = self._container.currentText()
        self._container.blockSignals(False)

    def set_detected_hw(self, summary: str) -> None:
        """Real detection text. Does not change the encoder or the checkbox."""

        self._detected_hw = summary or ""
        self._apply_hw_hint_text()

    def _refresh_hints(self) -> None:
        choice = self.form.ladder_choice()
        self._hw.setEnabled(not choice.force_software)
        if choice.force_software:
            self._hw.setChecked(False)
            self.form.use_hw_accel = False
        self._ladder_hint.setText(self.form.ladder_hint())
        self._apply_hw_hint_text()

    def _apply_hw_hint_text(self) -> None:
        detected = (self._detected_hw or "").strip()
        policy = self.form.hw_hint()
        if not detected:
            self._hw_hint.setText(policy)
            return
        choice = self.form.ladder_choice()
        exact = (
            self.form.target_size_enabled
            and self.form.target_size_mode == "exact"
            and self.form.use_hw_accel
            and not choice.force_software
        )
        if exact:
            self._hw_hint.setText(f"{detected}\n\n{policy}")
        else:
            self._hw_hint.setText(detected)

    def _friendly_profile_blurb(self, name: str, description: str) -> str:
        if name in PROFILE_FRIENDLY:
            return PROFILE_FRIENDLY[name]
        _label, blurb = profile_ui_text(name, description)
        return blurb

    def _refresh_profile_chrome(self, quick_name: Optional[str]) -> None:
        for button in self._profile_buttons.values():
            _retint(button, False)
        self._more.blockSignals(True)
        self._more.setCurrentIndex(0)
        self._more.blockSignals(False)
        if quick_name:
            blurb = QUICK_PRESET_BLURBS.get(quick_name, "")
            text = f"Quick preset: {quick_name}."
            if blurb:
                text = f"{text} {blurb}"
            self._profile_blurb.setText(text)
        else:
            self._profile_blurb.setText("")

    def set_active_profile(self, name: Optional[str]) -> None:
        for profile_name, button in self._profile_buttons.items():
            _retint(button, profile_name == name)
        self._more.blockSignals(True)
        if name in self._profile_buttons or not name:
            self._more.setCurrentIndex(0)
        else:
            index = self._more.findData(name)
            self._more.setCurrentIndex(index if index >= 0 else 0)
        self._more.blockSignals(False)
        profile = next((item for item in self._profiles if item.name == name), None)
        if profile is not None:
            self._profile_blurb.setText(
                self._friendly_profile_blurb(profile.name, profile.description)
            )
        else:
            self._profile_blurb.setText("")

    def set_active_quick(self, name: Optional[str]) -> None:
        self._refresh_profile_chrome(name)

    def _emit_profile(self, name: str) -> None:
        if self._loading:
            return
        self._on_change("profile", name)

    def _emit_field(self) -> None:
        if self._loading:
            return
        self._refresh_hints()
        self._on_change("field", None)

    def _on_more(self, index: int) -> None:
        if self._loading:
            return
        name = self._more.itemData(index)
        if name:
            self._emit_profile(str(name))

    def _on_codec(self) -> None:
        if self._loading:
            return
        value = self._codec.currentData()
        try:
            codec = VideoCodec(value)
        except ValueError:
            return
        self.form.set_codec(codec)
        self._loading = True
        try:
            self._refresh_containers()
            if self.form.ladder_choice().force_software:
                self._hw.setChecked(False)
        finally:
            self._loading = False
        self._emit_field()

    def _on_rung(self) -> None:
        if self._loading:
            return
        self.form.set_rung_label(self._rung.currentText())
        self._loading = True
        try:
            if self.form.ladder_choice().force_software:
                self._hw.setChecked(False)
        finally:
            self._loading = False
        self._emit_field()

    def _on_hw(self, checked: bool) -> None:
        if self._loading:
            return
        if self.form.ladder_choice().force_software:
            self.form.use_hw_accel = False
            self._hw.setChecked(False)
        else:
            self.form.use_hw_accel = bool(checked)
            self.form.note_manual_edit()
        self._emit_field()

    def _on_target_toggle(self, checked: bool) -> None:
        if self._loading:
            return
        self.form.target_size_enabled = bool(checked)
        self._target_mb.setEnabled(bool(checked))
        if checked:
            self.form.target_size_mb = int(self._target_mb.value())
        self.form.note_manual_edit()
        self._emit_field()

    def _on_target_mb(self, value: int) -> None:
        if self._loading:
            return
        self.form.target_size_mb = int(value)
        self.form.note_manual_edit()
        self._emit_field()

    def _on_target_mode(self) -> None:
        if self._loading:
            return
        self.form.target_size_mode = self._target_mode.currentText()
        self.form.note_manual_edit()
        self._emit_field()

    def _on_exact_audio(self) -> None:
        if self._loading:
            return
        self.form.exact_audio_policy = self._exact_audio.currentText()
        self.form.note_manual_edit()
        self._emit_field()

    def _on_two_pass(self, checked: bool) -> None:
        if self._loading:
            return
        self.form.exact_two_pass = bool(checked)
        self.form.note_manual_edit()
        self._emit_field()

    def _on_container(self) -> None:
        if self._loading:
            return
        text = self._container.currentText()
        if text:
            self.form.video_container = text
            self.form.note_manual_edit()
            self._emit_field()

    def _on_resolution(self) -> None:
        if self._loading:
            return
        self.form.resolution_label = self._resolution.currentText()
        self.form.note_manual_edit()
        self._emit_field()

    def _on_fps(self) -> None:
        if self._loading:
            return
        self.form.frame_rate_label = self._fps.currentText()
        self.form.note_manual_edit()
        self._emit_field()

    def _on_audio(self) -> None:
        if self._loading:
            return
        value = self._audio.currentData() or self._audio.currentText()
        try:
            self.form.audio_codec = AudioCodec(value)
        except ValueError:
            return
        self.form.note_manual_edit()
        self._emit_field()

    def _on_template(self) -> None:
        if self._loading:
            return
        self.form.output_name_template = self._template.text().strip() or "{name}_compressed"
        self._emit_field()

    def _on_governor(self) -> None:
        if self._loading:
            return
        self.form.resource_governor = self._governor.currentText()
        self._emit_field()

    def _on_parallel(self) -> None:
        if self._loading:
            return
        try:
            self.form.parallel_jobs = int(self._parallel.currentText())
        except ValueError:
            self.form.parallel_jobs = 1
        self._emit_field()


def scroll_wrap(inner: QWidget, object_name: str) -> QScrollArea:
    """Scroll area with a visible bar as soon as the column overflows."""

    area = QScrollArea()
    area.setObjectName(object_name)
    area.setWidgetResizable(True)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setWidget(inner)
    area.verticalScrollBar().setSingleStep(28)
    return area
