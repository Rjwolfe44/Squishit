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
    MORE_MENU_LABEL,
    MORE_PROFILES_TOOLTIP,
    PROFILE_HELPER,
    QUICK_COMPRESS_SUBTITLE,
    QUICK_COMPRESS_TITLE,
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
        self._profile_buttons: dict[str, QPushButton] = {}
        self._quick_buttons: dict[str, QPushButton] = {}
        self._build()
        self.load(form)

    def set_profiles(self, profiles: Sequence[CompressionProfile]) -> None:
        self._profiles = list(profiles)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 8, 16)
        root.setSpacing(8)

        root.addWidget(_section(QUICK_COMPRESS_TITLE))
        subtitle = _hint(QUICK_COMPRESS_SUBTITLE)
        root.addWidget(subtitle)
        quick_row = QHBoxLayout()
        quick_row.setSpacing(6)
        from ...core.profiles import QUICK_COMPRESS_ORDER, build_quick_compress_profiles

        presets = build_quick_compress_profiles()
        for name in QUICK_COMPRESS_ORDER:
            button = QPushButton(name)
            button.setObjectName("pill")
            button.setToolTip(presets[name].description)
            button.clicked.connect(lambda _checked=False, preset=name: self._emit_quick(preset))
            self._quick_buttons[name] = button
            quick_row.addWidget(button)
        quick_row.addStretch(1)
        root.addLayout(quick_row)

        root.addWidget(_section("Profiles"))
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
            button.setToolTip(blurb)
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

        self._hw = QCheckBox("Hardware acceleration")
        self._hw.toggled.connect(self._on_hw)
        root.addWidget(self._hw)
        self._hw_hint = _hint("")
        root.addWidget(self._hw_hint)

        root.addWidget(_section("Target size"))
        self._target_on = QCheckBox("Use target size")
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
        mode_form.addRow("Target mode", self._target_mode)
        self._exact_audio = QComboBox()
        self._exact_audio.addItems(EXACT_AUDIO)
        self._exact_audio.currentIndexChanged.connect(self._on_exact_audio)
        mode_form.addRow("Exact audio", self._exact_audio)
        root.addLayout(mode_form)
        self._two_pass = QCheckBox("Two-pass exact size")
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
        template_form.addRow("Name template", self._template)
        root.addLayout(template_form)

        root.addWidget(_section("Performance"))
        perf = QFormLayout()
        self._governor = QComboBox()
        self._governor.addItems(GOVERNORS)
        self._governor.currentIndexChanged.connect(self._on_governor)
        perf.addRow("Resource use", self._governor)
        self._parallel = QComboBox()
        self._parallel.addItems(["1", "2", "3", "4", "6", "8"])
        self._parallel.currentIndexChanged.connect(self._on_parallel)
        perf.addRow("Parallel jobs", self._parallel)
        root.addLayout(perf)
        root.addStretch(1)

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

    def _refresh_hints(self) -> None:
        choice = self.form.ladder_choice()
        self._hw.setEnabled(not choice.force_software)
        if choice.force_software:
            self._hw.setChecked(False)
            self.form.use_hw_accel = False
        self._ladder_hint.setText(self.form.ladder_hint())
        self._hw_hint.setText(self.form.hw_hint())

    def _refresh_profile_chrome(self, quick_name: Optional[str]) -> None:
        for name, button in self._quick_buttons.items():
            _retint(button, name == quick_name)
        if quick_name:
            for button in self._profile_buttons.values():
                _retint(button, False)
            self._more.blockSignals(True)
            self._more.setCurrentIndex(0)
            self._more.blockSignals(False)
            preset = next(
                (item for item in self._profiles if item.name == quick_name),
                None,
            )
            self._profile_blurb.setText("")
            if preset is None:
                from ...core.profiles import build_quick_compress_profiles

                qc = build_quick_compress_profiles().get(quick_name)
                if qc is not None:
                    self._profile_blurb.setText(qc.description)

    def set_active_profile(self, name: Optional[str]) -> None:
        for profile_name, button in self._profile_buttons.items():
            _retint(button, profile_name == name)
        for button in self._quick_buttons.values():
            _retint(button, False)
        self._more.blockSignals(True)
        if name in self._profile_buttons or not name:
            self._more.setCurrentIndex(0)
        else:
            index = self._more.findData(name)
            self._more.setCurrentIndex(index if index >= 0 else 0)
        self._more.blockSignals(False)
        profile = next((item for item in self._profiles if item.name == name), None)
        if profile is not None:
            _label, blurb = profile_ui_text(profile.name, profile.description)
            self._profile_blurb.setText(blurb)
        else:
            self._profile_blurb.setText("")

    def set_active_quick(self, name: Optional[str]) -> None:
        self._refresh_profile_chrome(name)
        for preset, button in self._quick_buttons.items():
            _retint(button, preset == name)

    def _emit_profile(self, name: str) -> None:
        if self._loading:
            return
        self._on_change("profile", name)

    def _emit_quick(self, name: str) -> None:
        if self._loading:
            return
        self._on_change("quick", name)

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
