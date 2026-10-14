"""Qt profile editor: locks, save, duplicate, delete, validation, import/export.

Everything runs offscreen against a temporary profiles folder. QMessageBox and
QFileDialog are replaced with a recorder, so a prompt the test did not expect
fails the test instead of blocking on a dialog nobody can answer.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from video_compressor.core.codecs import (
    ENCODER_REGISTRY,
    AudioCodec,
    AudioMode,
    RateControl,
    VideoCodec,
)
from video_compressor.core.profiles import (
    CompressionProfile,
    ProfileManager,
    ProfileType,
)
from video_compressor.gui.qt_shell.encode_form import (
    CODEC_CHOICES,
    EncodeForm,
    containers_for,
)
from video_compressor.gui.qt_shell.profile_editor import (
    ProfileEditorDialog,
    crf_tier,
    encoder_options,
    is_builtin_profile,
    name_format_error,
    profile_file_stem,
    validate_profile_name,
    validate_profile_values,
)
from video_compressor.gui.qt_shell.settings_pane import SettingsPane

_ROLE_NAME = Qt.ItemDataRole.UserRole
_ROLE_LOCKED = Qt.ItemDataRole.UserRole.value + 1
_BUTTON = QMessageBox.StandardButton
_BUILT_IN_NAMES = [
    "Fast",
    "Balanced",
    "Max / Archival",
    "YouTube Upload",
    "Mobile",
    "Streaming",
]


class _Prompts:
    """Records and answers QMessageBox and QFileDialog calls."""

    def __init__(self) -> None:
        self.questions: list[tuple[str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.answers: list[QMessageBox.StandardButton] = []
        self.open_paths: list[Path | None] = []
        self.save_paths: list[Path | None] = []

    def question(self, parent, title, text, buttons=None, default=None):
        self.questions.append((title, text))
        if not self.answers:
            raise AssertionError(f"Unexpected question: {title}: {text}")
        return self.answers.pop(0)

    def warning(self, parent, title, text, *args, **kwargs):
        self.warnings.append((title, text))
        return _BUTTON.Ok

    def get_open(self, parent=None, caption="", directory="", filter="", *a, **k):
        if not self.open_paths:
            raise AssertionError("Unexpected open-file dialog")
        path = self.open_paths.pop(0)
        return ("" if path is None else str(path)), ""

    def get_save(self, parent=None, caption="", directory="", filter="", *a, **k):
        if not self.save_paths:
            raise AssertionError("Unexpected save-file dialog")
        path = self.save_paths.pop(0)
        return ("" if path is None else str(path)), ""


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def prompts(monkeypatch):
    stub = _Prompts()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(stub.question))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(stub.warning))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(stub.warning))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(stub.get_open))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(stub.get_save))
    return stub


@pytest.fixture
def profiles_dir(tmp_path):
    return tmp_path / "profiles"


@pytest.fixture
def manager(profiles_dir):
    return ProfileManager(config_dir=profiles_dir)


@pytest.fixture
def changes():
    return []


def _build(qapp, manager, changes):
    return ProfileEditorDialog(
        None, manager, on_changed=lambda: changes.append("changed")
    )


@pytest.fixture
def editor(qapp, prompts, manager, changes):
    dialog = _build(qapp, manager, changes)
    yield dialog
    _close(qapp, dialog)


def _close(qapp, dialog):
    dialog._dirty = False
    dialog._draft = None
    dialog.close()
    dialog.deleteLater()
    qapp.processEvents()


def _yaml_files(folder: Path) -> list[str]:
    if not folder.exists():
        return []
    return sorted(path.name for path in folder.glob("*.yaml"))


def _set(combo, data):
    index = combo.findData(data)
    assert index >= 0, f"{data!r} is not offered"
    combo.setCurrentIndex(index)


def _data(combo) -> list:
    return [combo.itemData(row) for row in range(combo.count())]


def _row_names(dialog) -> list[str]:
    return [
        dialog._list.item(row).data(_ROLE_NAME) for row in range(dialog._list.count())
    ]


def _fill_phone_clips(dialog) -> None:
    """Fill a fresh draft with values that differ from every default."""

    dialog._name.setText("Phone clips")
    dialog._desc.setText("Small clips for chat")
    _set(dialog._codec, "vp9")
    _set(dialog._preset, "2")
    _set(dialog._rate, "vbr")
    dialog._crf.setValue(33)
    _set(dialog._container, "webm")
    _set(dialog._resolution, 720)
    _set(dialog._fps, 30)
    _set(dialog._audio_mode, AudioMode.REENCODE.value)
    _set(dialog._audio_codec, AudioCodec.OPUS.value)
    _set(dialog._audio_kbps, 96_000)
    dialog._target_on.setChecked(True)
    dialog._target_mb.setValue(25)
    _set(dialog._target_mode, "strict")
    _set(dialog._governor, "low")
    dialog._hw.setChecked(False)


def _save_custom(manager, name="Saved one", **overrides) -> CompressionProfile:
    profile = manager.create_custom_profile(name, base_profile="Balanced", **overrides)
    assert manager.save_profile(profile)
    return profile


# -- built-ins ---------------------------------------------------------------


def test_builtins_are_listed_with_a_lock_and_stay_read_only(editor, manager, prompts):
    assert _row_names(editor) == _BUILT_IN_NAMES
    for row in range(editor._list.count()):
        item = editor._list.item(row)
        assert item.data(_ROLE_LOCKED) is True
        assert item.icon().isNull() is False
    assert editor._locked is True
    assert editor._body.isEnabled() is False
    assert editor._save_btn.isEnabled() is False
    assert editor._delete_btn.isEnabled() is False
    assert editor._duplicate_btn.isEnabled() is True
    assert editor._export_btn.isEnabled() is True
    assert editor._banner.isHidden() is False

    assert editor.save() is False
    assert "can't be changed" in editor.message_text()
    assert editor.delete_current() is False
    assert _yaml_files(manager.config_dir) == []
    assert prompts.questions == []


def test_system_profiles_with_custom_type_are_locked_too(editor, manager):
    youtube = manager.get_profile("YouTube Upload")
    assert youtube.profile_type is ProfileType.CUSTOM
    assert is_builtin_profile(youtube)
    assert editor.select_profile("YouTube Upload")
    assert editor._locked is True
    assert editor._delete_btn.isEnabled() is False


def test_custom_profiles_are_unlocked_and_open_first(qapp, prompts, manager, changes):
    _save_custom(manager, "Zebra")
    _save_custom(manager, "Apple")
    dialog = _build(qapp, manager, changes)
    try:
        assert _row_names(dialog) == _BUILT_IN_NAMES + ["Apple", "Zebra"]
        assert dialog._current == "Apple"
        assert dialog._locked is False
        assert dialog._body.isEnabled() is True
        apple = dialog._list.item(dialog._list.count() - 2)
        assert apple.data(_ROLE_LOCKED) is False
        assert apple.icon().isNull()
        assert dialog._delete_btn.isEnabled() is True
        assert dialog._save_btn.isEnabled() is False  # nothing edited yet
    finally:
        _close(qapp, dialog)


def test_select_profile_reports_unknown_names(editor):
    assert editor.select_profile("Nope") is False
    assert editor.select_profile("Balanced") is True
    assert editor._current == "Balanced"


# -- new, edit, save ---------------------------------------------------------


def test_new_edit_save_writes_yaml_that_loads_back_equal(
    editor, manager, profiles_dir, changes, qapp
):
    seen = []
    editor.profiles_changed.connect(lambda: seen.append("signal"))

    assert editor.new_profile() is True
    assert editor._name.text() == "New profile"
    assert _row_names(editor)[-1] == "<draft>"
    assert editor._list.currentItem().text().endswith("(not saved)")
    assert _yaml_files(profiles_dir) == []
    assert changes == []

    _fill_phone_clips(editor)
    assert editor._list.currentItem().text().startswith("Phone clips")
    assert editor.save() is True

    assert _yaml_files(profiles_dir) == ["phone_clips.yaml"]
    assert changes == ["changed"]
    assert seen == ["signal"]
    assert "Saved" in editor.message_text()
    assert "More" in editor.message_text()
    assert editor._draft is None
    assert editor._current == "Phone clips"
    assert "<draft>" not in _row_names(editor)
    assert editor._list.currentItem().data(_ROLE_LOCKED) is False

    saved = manager.get_profile("Phone clips")
    reloaded = ProfileManager(config_dir=profiles_dir).get_profile("Phone clips")
    assert reloaded is not None
    assert reloaded.to_dict() == saved.to_dict()
    assert reloaded.profile_type is ProfileType.CUSTOM
    assert reloaded.created_by == "user"
    assert reloaded.description == "Small clips for chat"
    assert reloaded.video_codec is VideoCodec.VP9
    assert reloaded.preset == "2"
    assert reloaded.rate_control == "vbr"
    assert reloaded.crf == 33
    assert reloaded.video_container == "webm"
    assert reloaded.max_resolution == 720
    assert reloaded.frame_rate == 30
    assert reloaded.audio_mode == "reencode"
    assert reloaded.audio_codec is AudioCodec.OPUS
    assert reloaded.audio_bitrate == 96_000
    assert reloaded.target_size_mb == 25
    assert reloaded.target_size_mode == "strict"
    assert reloaded.resource_governor == "low"
    assert reloaded.use_hw_accel is False
    assert reloaded.tune is None
    # Saving is not an edit: the dialog goes back to a clean state.
    assert editor._save_btn.isEnabled() is False


def test_editing_keeps_fields_the_form_does_not_show(qapp, prompts, manager, changes, profiles_dir):
    hidden = _save_custom(
        manager,
        "Hidden extras",
        video_bitrate=4_000_000,
        max_bitrate=6_000_000,
        buffer_size=8_000_000,
        pixel_format="yuv420p10le",
        threads=3,
        image_format="avif",
        trim_enabled=True,
        trim_start=1.5,
        trim_end=9.0,
        extra_ffmpeg_args="-movflags +faststart",
        custom_encoder_opts="-x265-params limit-refs=1",
        keyframe_interval_sec=4,
        film_grain=7,
    )
    dialog = _build(qapp, manager, changes)
    try:
        assert dialog._current == "Hidden extras"
        dialog._desc.setText("Edited note")
        assert dialog.save() is True
    finally:
        _close(qapp, dialog)
    expected = hidden.to_dict()
    expected["description"] = "Edited note"
    reloaded = ProfileManager(config_dir=profiles_dir).get_profile("Hidden extras")
    assert reloaded.to_dict() == expected


def test_values_outside_the_offered_choices_load_without_changes(
    qapp, prompts, manager, changes, profiles_dir
):
    odd = _save_custom(
        manager,
        "Odd values",
        video_codec="vp9",
        video_container="webm",
        preset="veryslow",
        max_resolution=540,
        frame_rate=29.97,
        audio_bitrate=96_500,
    )
    dialog = _build(qapp, manager, changes)
    try:
        assert dialog._current == "Odd values"
        assert dialog._preset.currentData() == "veryslow"
        assert dialog._resolution.currentData() == 540
        assert dialog._audio_kbps.currentData() == 96_500
        dialog._desc.setText("x")
        assert dialog.save() is True
    finally:
        _close(qapp, dialog)
    reloaded = ProfileManager(config_dir=profiles_dir).get_profile("Odd values")
    assert reloaded.preset == "veryslow"
    assert reloaded.max_resolution == 540
    assert reloaded.frame_rate == 29.97
    assert reloaded.audio_bitrate == 96_500
    assert odd.video_codec is VideoCodec.VP9


def test_rename_moves_the_file_and_keeps_one_copy(editor, manager, profiles_dir):
    editor.new_profile()
    editor._name.setText("Old name")
    assert editor.save()
    assert _yaml_files(profiles_dir) == ["old_name.yaml"]

    editor._name.setText("New name")
    assert editor.save()
    assert _yaml_files(profiles_dir) == ["new_name.yaml"]
    assert manager.get_profile("Old name") is None
    assert manager.get_profile("New name") is not None
    assert editor._current == "New name"

    # Only the capitals change: same file, so the file must survive.
    editor._name.setText("NEW Name")
    assert editor.save()
    assert _yaml_files(profiles_dir) == ["new_name.yaml"]
    assert manager.get_profile("New name") is None
    assert manager.get_profile("NEW Name") is not None


# -- duplicate and delete ----------------------------------------------------


def test_duplicate_gets_a_unique_name_each_time(editor, manager, profiles_dir, changes):
    editor.select_profile("Balanced")
    assert editor.duplicate_current() is True
    assert editor._name.text() == "Balanced (copy)"
    assert _yaml_files(profiles_dir) == []  # still a draft
    assert editor.save() is True
    assert manager.get_profile("Balanced (copy)").profile_type is ProfileType.CUSTOM
    assert manager.get_profile("Balanced (copy)").created_by == "user"
    assert manager.get_profile("Balanced (copy)").crf == manager.get_profile("Balanced").crf

    editor.select_profile("Balanced")
    assert editor.duplicate_current() is True
    assert editor._name.text() == "Balanced (copy 2)"
    assert editor.save() is True

    # Copying a custom profile copies its settings, not its name.
    assert editor.duplicate_current() is True
    assert editor._name.text() == "Balanced (copy 2) (copy)"
    assert editor.save() is True
    assert len(changes) == 3
    assert _yaml_files(profiles_dir) == [
        "balanced_(copy).yaml",
        "balanced_(copy_2).yaml",
        "balanced_(copy_2)_(copy).yaml",
    ]


def test_duplicating_a_built_in_with_a_slash_in_its_name_is_savable(editor, manager):
    editor.select_profile("Max / Archival")
    assert editor.duplicate_current() is True
    assert "/" not in editor._name.text()
    assert editor.save() is True
    copy = manager.get_profile(editor._current)
    assert copy.video_codec is VideoCodec.SVT_AV1
    assert copy.profile_type is ProfileType.CUSTOM


def test_delete_asks_first_and_removes_the_file(editor, manager, profiles_dir, changes, prompts):
    editor.new_profile()
    editor._name.setText("Doomed")
    assert editor.save()
    assert _yaml_files(profiles_dir) == ["doomed.yaml"]
    changes.clear()

    prompts.answers.append(_BUTTON.No)
    assert editor.delete_current() is False
    assert _yaml_files(profiles_dir) == ["doomed.yaml"]
    assert manager.get_profile("Doomed") is not None
    assert changes == []

    prompts.answers.append(_BUTTON.Yes)
    assert editor.delete_current() is True
    assert prompts.questions[-1][0] == "Delete profile"
    assert "Doomed" in prompts.questions[-1][1]
    assert _yaml_files(profiles_dir) == []
    assert manager.get_profile("Doomed") is None
    assert "Doomed" not in _row_names(editor)
    assert changes == ["changed"]
    assert editor._current == "Fast"  # nothing custom left, falls back to the first row


def test_discarding_a_draft_writes_nothing_and_does_not_notify(
    editor, profiles_dir, changes, prompts
):
    editor.new_profile()
    prompts.answers.append(_BUTTON.Yes)
    assert editor.delete_current() is True
    assert "<draft>" not in _row_names(editor)
    assert editor.has_unsaved_changes() is False
    assert _yaml_files(profiles_dir) == []
    assert changes == []


# -- validation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "fragment"),
    [
        ("", "name"),
        ("   ", "name"),
        ("bad/name", "can't contain"),
        ("back\\slash", "can't contain"),
        ("who?", "can't contain"),
        ('say "hi"', "can't contain"),
        ("a:b", "can't contain"),
        ("x" * 61, "60 characters"),
        ("CON", "Windows"),
        ("lpt1", "Windows"),
        ("ends with dot.", "dot"),
        ("Fast", "built-in"),
        ("balanced", "built-in"),
        ("Quick Lite", "built-in"),
        ("youtube upload", "built-in"),
    ],
)
def test_invalid_names_are_rejected_with_a_message(
    editor, profiles_dir, changes, name, fragment
):
    editor.new_profile()
    editor._name.setText(name)
    assert editor.message_text() != ""  # shown live while typing
    assert editor._message.property("kind") == "error"
    assert editor._name.property("invalid") is True
    assert editor.save() is False
    assert fragment in editor.message_text()
    assert _yaml_files(profiles_dir) == []
    assert changes == []
    assert editor._draft is not None  # still there to fix


def test_duplicate_names_are_rejected_even_when_only_the_case_differs(
    editor, manager, profiles_dir, changes
):
    editor.new_profile()
    editor._name.setText("My Profile")
    assert editor.save()
    changes.clear()

    editor.new_profile()
    editor._name.setText("my profile")
    assert editor.save() is False
    assert "already have" in editor.message_text()
    editor._name.setText("my_profile")
    assert editor.save() is False  # same file name, so same profile
    assert _yaml_files(profiles_dir) == ["my_profile.yaml"]
    assert changes == []

    editor._name.setText("My Profile 2")
    assert editor.save() is True
    assert editor._name.property("invalid") is False


def test_renaming_onto_another_profile_is_rejected(editor, manager, profiles_dir):
    for name in ("First", "Second"):
        editor.new_profile()
        editor._name.setText(name)
        assert editor.save()
    editor._name.setText("first")
    assert editor.save() is False
    assert "already have" in editor.message_text()
    assert manager.get_profile("Second") is not None
    assert _yaml_files(profiles_dir) == ["first.yaml", "second.yaml"]


def test_target_size_must_be_above_zero(editor, profiles_dir):
    editor.new_profile()
    editor._name.setText("Sized")
    editor._target_on.setChecked(True)
    editor._target_mb.setValue(0)
    assert editor._target_mb.property("invalid") is True
    assert editor.save() is False
    assert "above 0" in editor.message_text()
    assert _yaml_files(profiles_dir) == []

    editor._target_mb.setValue(20)
    assert editor._target_mb.property("invalid") is False
    assert editor.save() is True


def test_target_size_is_left_out_while_the_option_is_off(editor, manager):
    editor.new_profile()
    editor._name.setText("No size")
    editor._target_mb.setValue(0)  # ignored while the checkbox is off
    assert editor.save() is True
    assert manager.get_profile("No size").target_size_mb is None


def test_value_checks_cover_crf_range_and_target_size():
    hevc = CompressionProfile(name="x", profile_type=ProfileType.CUSTOM, crf=60)
    assert "between 0 and 51" in validate_profile_values(hevc)
    vp9 = CompressionProfile(
        name="x", profile_type=ProfileType.CUSTOM, video_codec=VideoCodec.VP9, crf=60
    )
    assert validate_profile_values(vp9) == ""
    vp9.crf = 64
    assert "between 0 and 63" in validate_profile_values(vp9)
    hevc.crf = 28
    hevc.target_size_mb = 0
    assert "above 0" in validate_profile_values(hevc)
    hevc.target_size_mb = -4
    assert "above 0" in validate_profile_values(hevc)
    hevc.target_size_mb = 10
    assert validate_profile_values(hevc) == ""
    hevc.crf = "high"
    assert "whole number" in validate_profile_values(hevc)


def test_name_helpers_follow_the_manager_file_names():
    assert profile_file_stem("My Profile") == "my_profile"
    assert name_format_error("Fine name") == ""
    assert name_format_error("a|b") != ""
    assert validate_profile_name("Mine", taken={"mine"}) != ""
    assert validate_profile_name("Mine", taken={"mine"}, own_stem="mine") == ""
    assert validate_profile_name("Fast", reserved={"fast"}) != ""
    assert validate_profile_name("Fast", reserved={"fast"}, own_stem="fast") == ""


# -- codec rules -------------------------------------------------------------


def test_slider_range_and_tier_label_follow_the_encoder(editor):
    editor.new_profile()
    for codec in CODEC_CHOICES:
        _set(editor._codec, codec.value)
        low, high = ENCODER_REGISTRY[codec.ffmpeg_encoder]["crf_range"]
        assert (editor._crf.minimum(), editor._crf.maximum()) == (low, high)
    _set(editor._codec, "vp9")
    editor._crf.setValue(60)
    _set(editor._codec, "hevc")
    assert editor._crf.value() == 51  # clamped into the new range

    editor._crf.setValue(22)
    assert editor._crf_label.text().startswith("22")
    assert "High quality" in editor._crf_label.text()
    editor._crf.setValue(0)
    assert "Lossless" in editor._crf_label.text()
    assert crf_tier(15) == "Near-lossless"
    assert crf_tier(28) == "Good quality"
    assert crf_tier(50) == "Very low quality"
    assert crf_tier(35, 63) == "Good quality"  # 0-63 scale is scaled to 0-51


def test_codec_change_constrains_preset_tune_rate_and_container(editor):
    editor.new_profile()
    _set(editor._codec, "hevc")
    assert "medium" in _data(editor._preset)
    assert "placebo" not in _data(editor._preset)  # the loader would rewrite it
    assert _data(editor._tune) == ["none", "grain", "animation", "psnr", "ssim", "fastdecode", "zerolatency"]
    assert _data(editor._container) == containers_for(VideoCodec.HEVC)
    assert RateControl.CQP.value in _data(editor._rate)
    _set(editor._preset, "slow")
    _set(editor._tune, "grain")
    _set(editor._rate, "cqp")
    _set(editor._container, "mov")

    _set(editor._codec, "vp9")
    assert _data(editor._preset) == [str(number) for number in range(9)]
    assert editor._preset.currentData() == "4"  # slow is not a VP9 speed: codec default
    assert _data(editor._tune) == ["none"]
    assert editor._tune.currentData() == "none"
    assert _data(editor._rate) == ["crf", "cbr", "vbr"]
    assert editor._rate.currentData() == "crf"
    assert _data(editor._container) == containers_for(VideoCodec.VP9)
    assert "mp4" not in _data(editor._container)
    assert editor._container.currentData() == "webm"

    _set(editor._codec, "h264")
    assert "veryslow" in _data(editor._preset)
    assert _data(editor._container) == containers_for(VideoCodec.H264)
    assert "avi" in _data(editor._container)
    assert editor._container.currentData() == "mp4"  # webm is not allowed for H.264

    _set(editor._codec, "svt-av1")
    assert _data(editor._preset) == [str(number) for number in range(14)]
    _set(editor._codec, "av1")
    # Fast AV1's software encoder is SVT-AV1 now (presets 0-13), not libaom (0-10).
    assert _data(editor._preset) == [str(number) for number in range(14)]
    assert "webm" in _data(editor._container)
    for codec in CODEC_CHOICES:
        options = encoder_options(codec)
        assert options.default_preset in options.presets


def test_codec_change_keeps_choices_that_still_fit(editor):
    editor.new_profile()
    _set(editor._codec, "h264")
    _set(editor._preset, "slow")
    _set(editor._tune, "film")
    _set(editor._container, "mkv")
    _set(editor._codec, "hevc")
    assert editor._preset.currentData() == "slow"
    assert editor._tune.currentData() == "none"  # film is x264 only
    assert editor._container.currentData() == "mkv"


def test_hardware_switch_is_off_for_svt_av1_and_comes_back(editor, manager):
    editor.new_profile()
    editor._name.setText("Hardware check")
    assert editor._hw.isChecked() is True
    _set(editor._codec, "svt-av1")
    assert editor._hw.isEnabled() is False
    assert editor._hw.isChecked() is False
    assert "processor" in editor._hw_hint.text()
    assert editor.save()
    assert manager.get_profile("Hardware check").use_hw_accel is False

    _set(editor._codec, "hevc")
    assert editor._hw.isEnabled() is True
    assert editor._hw.isChecked() is True  # the earlier choice is remembered
    editor._hw.setChecked(False)
    _set(editor._codec, "svt-av1")
    _set(editor._codec, "hevc")
    assert editor._hw.isChecked() is False


def test_quality_slider_only_applies_to_quality_based_modes(editor):
    editor.new_profile()
    _set(editor._rate, "cbr")
    assert editor._crf.isEnabled() is False
    assert "data rate" in editor._rate_hint.text()
    _set(editor._rate, "lossless")
    assert "Lossless" in editor._rate_hint.text()
    _set(editor._rate, "cqp")
    assert editor._crf.isEnabled() is True
    assert editor._rate_hint.text() == ""


def test_audio_controls_follow_the_audio_mode_and_container(editor):
    editor.new_profile()
    _set(editor._audio_mode, "strip")
    assert editor._audio_codec.isEnabled() is False
    assert editor._audio_kbps.isEnabled() is False
    assert "no sound" in editor._audio_hint.text()
    _set(editor._audio_mode, "copy")
    assert "copied" in editor._audio_hint.text()
    _set(editor._audio_mode, "reencode")
    assert editor._audio_codec.isEnabled() is True
    _set(editor._audio_codec, "flac")
    assert editor._audio_kbps.isEnabled() is False  # lossless has no bitrate
    _set(editor._audio_codec, "aac")
    assert editor._audio_kbps.isEnabled() is True

    _set(editor._codec, "vp9")  # WebM cannot hold AAC
    assert editor._container.currentData() == "webm"
    assert "Opus" in editor._audio_hint.text()
    _set(editor._audio_codec, "opus")
    assert "can't go" not in editor._audio_hint.text()


def test_size_target_controls_follow_the_checkbox_and_mode(editor):
    editor.new_profile()
    assert editor._target_mb.isEnabled() is False
    assert editor._target_mode.isEnabled() is False
    editor._target_on.setChecked(True)
    assert editor._target_mb.isEnabled() is True
    assert editor._exact_audio.isEnabled() is False
    assert editor._two_pass.isEnabled() is False
    _set(editor._target_mode, "exact")
    assert editor._exact_audio.isEnabled() is True
    assert editor._two_pass.isEnabled() is True


# -- unsaved changes ---------------------------------------------------------


def test_switching_with_unsaved_edits_asks_and_can_cancel(editor, manager, prompts, profiles_dir):
    editor.new_profile()
    editor._name.setText("Editing")
    assert editor.save()
    assert editor._save_btn.isEnabled() is False
    editor._desc.setText("A change")
    assert editor._save_btn.isEnabled() is True
    assert editor.has_unsaved_changes() is True

    prompts.answers.append(_BUTTON.Cancel)
    assert editor.select_profile("Fast") is False
    assert editor._current == "Editing"
    assert editor._list.currentItem().data(_ROLE_NAME) == "Editing"
    assert editor._desc.text() == "A change"

    # Clicking another row asks the same question and puts the selection back.
    prompts.answers.append(_BUTTON.Cancel)
    editor._list.setCurrentRow(0)
    assert editor._current == "Editing"
    assert editor._list.currentItem().data(_ROLE_NAME) == "Editing"
    assert prompts.questions[-1][0] == "Unsaved changes"
    assert "Editing" in prompts.questions[-1][1]

    prompts.answers.append(_BUTTON.Discard)
    assert editor.select_profile("Fast") is True
    assert editor._current == "Fast"
    assert manager.get_profile("Editing").description == ""
    assert editor.has_unsaved_changes() is False
    assert editor.select_profile("Balanced") is True  # no prompt: nothing to ask


def test_switching_with_unsaved_edits_can_save_them(editor, manager, prompts, profiles_dir, changes):
    editor.new_profile()
    editor._name.setText("Keeper")
    assert editor.save()
    changes.clear()
    editor._desc.setText("Saved on the way out")
    prompts.answers.append(_BUTTON.Save)
    assert editor.select_profile("Balanced") is True
    assert editor._current == "Balanced"
    assert manager.get_profile("Keeper").description == "Saved on the way out"
    assert ProfileManager(config_dir=profiles_dir).get_profile("Keeper").description == (
        "Saved on the way out"
    )
    assert changes == ["changed"]


def test_a_failed_save_while_switching_keeps_you_on_the_profile(editor, manager, prompts):
    editor.new_profile()
    editor._name.setText("Stays")
    assert editor.save()
    editor._name.setText("bad/name")
    prompts.answers.append(_BUTTON.Save)
    assert editor.select_profile("Balanced") is False
    assert editor._current == "Stays"
    assert "can't contain" in editor.message_text()
    assert manager.get_profile("Stays") is not None


def test_switching_away_from_a_draft_asks_and_discard_drops_it(editor, prompts, profiles_dir, changes):
    editor.new_profile()
    editor._name.setText("Throwaway")
    prompts.answers.append(_BUTTON.Discard)
    assert editor.select_profile("Balanced") is True
    assert "<draft>" not in _row_names(editor)
    assert editor._current == "Balanced"
    assert _yaml_files(profiles_dir) == []
    assert changes == []


def test_starting_something_new_asks_about_the_open_draft(editor, prompts):
    editor.new_profile()
    editor._name.setText("First draft")
    prompts.answers.append(_BUTTON.Cancel)
    assert editor.new_profile() is False
    assert editor._name.text() == "First draft"
    prompts.answers.append(_BUTTON.Discard)
    assert editor.new_profile() is True
    assert editor._name.text() == "New profile"
    assert _row_names(editor).count("<draft>") == 1


def test_closing_asks_about_unsaved_changes(qapp, prompts, manager, changes, profiles_dir):
    dialog = _build(qapp, manager, changes)
    try:
        dialog.show()
        qapp.processEvents()
        assert dialog.isVisible()
        dialog.new_profile()
        dialog._name.setText("Unfinished")

        prompts.answers.append(_BUTTON.Cancel)
        dialog.reject()
        assert dialog.isVisible()
        assert prompts.questions[-1][0] == "Unsaved changes"

        prompts.answers.append(_BUTTON.Discard)
        dialog.reject()
        assert not dialog.isVisible()
        assert _yaml_files(profiles_dir) == []
        assert changes == []

        dialog.show()
        qapp.processEvents()
        asked = len(prompts.questions)
        dialog.reject()  # nothing unsaved now, so no prompt
        assert len(prompts.questions) == asked
        assert not dialog.isVisible()
    finally:
        _close(qapp, dialog)


def test_closing_can_save_first(qapp, prompts, manager, changes, profiles_dir):
    dialog = _build(qapp, manager, changes)
    try:
        dialog.show()
        dialog.new_profile()
        dialog._name.setText("Saved on close")
        prompts.answers.append(_BUTTON.Save)
        dialog.close()
        assert not dialog.isVisible()
        assert _yaml_files(profiles_dir) == ["saved_on_close.yaml"]
        assert changes == ["changed"]
    finally:
        _close(qapp, dialog)


def test_the_close_button_uses_the_same_check(qapp, prompts, manager, changes):
    dialog = _build(qapp, manager, changes)
    try:
        dialog.show()
        dialog.new_profile()
        prompts.answers.append(_BUTTON.Cancel)
        dialog._close_btn.click()
        assert dialog.isVisible()
        prompts.answers.append(_BUTTON.Discard)
        dialog._close_btn.click()
        assert not dialog.isVisible()
    finally:
        _close(qapp, dialog)


# -- import and export -------------------------------------------------------


def test_export_then_import_round_trips_every_field(
    editor, manager, profiles_dir, changes, prompts, tmp_path
):
    editor.new_profile()
    _fill_phone_clips(editor)
    editor._name.setText("Round trip")
    assert editor.save()
    # Fields the form does not show must travel too.
    extra = manager.get_profile("Round trip")
    extra.video_bitrate = 2_500_000
    extra.extra_ffmpeg_args = "-movflags +faststart"
    extra.film_grain = 5
    assert manager.save_profile(extra)
    original = dict(manager.get_profile("Round trip").to_dict())

    target = tmp_path / "shared" / "round_trip.yaml"
    target.parent.mkdir()
    prompts.save_paths.append(target)
    assert editor.select_profile("Round trip")
    changes.clear()
    assert editor.export_current() is True
    assert changes == []  # exporting changes nothing in the list
    assert yaml.safe_load(target.read_text(encoding="utf-8")) == original

    prompts.answers.append(_BUTTON.Yes)
    assert editor.delete_current() is True
    assert manager.get_profile("Round trip") is None

    prompts.open_paths.append(target)
    assert editor.import_profile() is True
    imported = manager.get_profile("Round trip")
    assert imported is not None
    assert imported.to_dict() == original
    assert editor._current == "Round trip"
    assert editor._list.currentItem().data(_ROLE_LOCKED) is False
    assert "Imported" in editor.message_text()
    assert changes == ["changed", "changed"]  # delete + import
    assert ProfileManager(config_dir=profiles_dir).get_profile("Round trip").to_dict() == original


def test_export_adds_a_yaml_suffix_and_refuses_unsaved_drafts(editor, prompts, tmp_path):
    editor.new_profile()
    assert editor._export_btn.isEnabled() is False
    assert editor.export_current() is False
    assert "Save this profile first" in editor.message_text()

    editor._name.setText("Suffix check")
    assert editor.save()
    prompts.save_paths.append(tmp_path / "noextension")
    assert editor.export_current() is True
    assert (tmp_path / "noextension.yaml").exists()

    prompts.save_paths.append(None)  # the user cancelled the file box
    assert editor.export_current() is False


def test_export_offers_to_save_pending_edits(editor, manager, prompts, tmp_path):
    editor.new_profile()
    editor._name.setText("Pending")
    assert editor.save()
    editor._desc.setText("Not saved yet")

    prompts.answers.append(_BUTTON.No)
    prompts.save_paths.append(tmp_path / "saved_version.yaml")
    assert editor.export_current() is True
    assert yaml.safe_load((tmp_path / "saved_version.yaml").read_text())["description"] == ""
    assert editor.has_unsaved_changes() is True  # edits stay in the form

    prompts.answers.append(_BUTTON.Yes)
    prompts.save_paths.append(tmp_path / "with_edits.yaml")
    assert editor.export_current() is True
    assert yaml.safe_load((tmp_path / "with_edits.yaml").read_text())["description"] == (
        "Not saved yet"
    )
    assert manager.get_profile("Pending").description == "Not saved yet"

    editor._desc.setText("Again")
    prompts.answers.append(_BUTTON.Cancel)
    assert editor.export_current() is False


def test_exporting_a_built_in_imports_back_as_an_editable_copy(
    editor, manager, prompts, tmp_path, changes
):
    editor.select_profile("Balanced")
    target = tmp_path / "balanced.yaml"
    prompts.save_paths.append(target)
    assert editor.export_current() is True
    assert yaml.safe_load(target.read_text())["profile_type"] == "balanced"

    prompts.open_paths.append(target)
    assert editor.import_profile() is True
    assert changes == ["changed"]
    copy = manager.get_profile("Balanced (imported)")
    assert copy is not None
    assert copy.profile_type is ProfileType.CUSTOM
    assert copy.created_by == "import"
    assert is_builtin_profile(copy) is False
    assert editor._locked is False
    assert "name was already taken" in editor.message_text()
    # The built-in stays as it was.
    assert manager.get_profile("Balanced").profile_type is ProfileType.BALANCED

    prompts.open_paths.append(target)
    assert editor.import_profile() is True
    assert manager.get_profile("Balanced (imported 2)") is not None


def test_importing_over_an_existing_profile_asks_what_to_do(
    editor, manager, prompts, tmp_path, changes, profiles_dir
):
    editor.new_profile()
    editor._name.setText("Shared")
    editor._crf.setValue(20)
    assert editor.save()
    incoming = dict(manager.get_profile("Shared").to_dict())
    incoming["crf"] = 31
    incoming["description"] = "From a friend"
    path = tmp_path / "shared.yaml"
    path.write_text(yaml.safe_dump(incoming, sort_keys=False), encoding="utf-8")
    changes.clear()

    prompts.open_paths.append(path)
    prompts.answers.append(_BUTTON.Cancel)
    assert editor.import_profile() is False
    assert manager.get_profile("Shared").crf == 20
    assert changes == []

    prompts.open_paths.append(path)
    prompts.answers.append(_BUTTON.No)
    assert editor.import_profile() is True
    assert manager.get_profile("Shared").crf == 20
    assert manager.get_profile("Shared (imported)").crf == 31
    assert _yaml_files(profiles_dir) == ["shared.yaml", "shared_(imported).yaml"]

    prompts.open_paths.append(path)
    prompts.answers.append(_BUTTON.Yes)
    assert editor.import_profile() is True
    assert manager.get_profile("Shared").crf == 31
    assert manager.get_profile("Shared").description == "From a friend"
    assert len(_yaml_files(profiles_dir)) == 2
    assert changes == ["changed", "changed"]


def test_importing_asks_about_unsaved_edits_before_switching(
    editor, manager, prompts, tmp_path
):
    editor.new_profile()
    editor._name.setText("Pending work")
    path = tmp_path / "incoming.yaml"
    path.write_text(
        yaml.safe_dump(
            CompressionProfile(name="Incoming", profile_type=ProfileType.CUSTOM).to_dict()
        ),
        encoding="utf-8",
    )
    prompts.open_paths.append(path)
    prompts.answers.append(_BUTTON.Cancel)
    assert editor.import_profile() is False
    assert manager.get_profile("Incoming") is None
    assert editor._name.text() == "Pending work"

    prompts.open_paths.append(path)
    prompts.answers.append(_BUTTON.Discard)
    assert editor.import_profile() is True
    assert editor._current == "Incoming"
    assert "<draft>" not in _row_names(editor)


def test_cancelling_the_file_box_does_nothing(editor, prompts, changes):
    prompts.open_paths.append(None)  # the user cancelled the file box
    assert editor.import_profile() is False
    assert changes == []
    assert prompts.warnings == []


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("::: [not yaml", "couldn't read"),
        ("- just\n- a list\n", "doesn't look like"),
        ("crf: 20\n", "doesn't look like"),
        ("name: 12345\n", "doesn't look like"),
        ("name: Bad codec\naudio_codec: xyz\n", "doesn't understand"),
        ("name: Bad type\nprofile_type: superfast\n", "doesn't understand"),
        ("name: Too strong\ncrf: 99\n", "between 0 and 51"),
        ("name: Vp9 too strong\nvideo_codec: vp9\ncrf: 64\n", "between 0 and 63"),
        ("name: Negative size\ntarget_size_mb: -5\n", "above 0"),
        ("name: Zero size\ntarget_size_mb: 0\n", "above 0"),
        ("name: Text quality\ncrf: high\n", "whole number"),
        ("name: has/slash\n", "can't contain"),
        ('name: ""\n', "Give your profile a name"),
        ("name: CON\n", "Windows"),
    ],
)
def test_bad_import_files_are_rejected_and_nothing_is_written(
    editor, prompts, tmp_path, profiles_dir, changes, body, fragment
):
    path = tmp_path / "bad.yaml"
    path.write_text(body, encoding="utf-8")
    prompts.open_paths.append(path)
    assert editor.import_profile() is False
    assert len(prompts.warnings) == 1
    assert prompts.warnings[0][0] == "Import profile"
    assert fragment in prompts.warnings[0][1]
    assert _yaml_files(profiles_dir) == []
    assert changes == []


def test_missing_import_file_is_reported(editor, prompts, tmp_path, changes):
    prompts.open_paths.append(tmp_path / "gone.yaml")
    assert editor.import_profile() is False
    assert "couldn't read" in prompts.warnings[0][1]
    assert changes == []


# -- the main window consumes what the editor saves ---------------------------


def test_saved_profile_shows_in_the_more_menu_and_is_applied_by_the_form(
    editor, manager
):
    editor.new_profile()
    _fill_phone_clips(editor)
    assert editor.save()
    saved = manager.get_profile("Phone clips")

    pane = SettingsPane(manager.get_all_profiles(), EncodeForm(), lambda kind, value: None)
    index = pane._more.findData("Phone clips")
    assert index > 0
    assert pane._more.itemText(index) == "Phone clips"

    form = EncodeForm()
    form.apply_profile(saved)
    built = form.build_profile(saved.name, saved)
    assert built.video_codec is VideoCodec.VP9
    assert built.crf == 33  # not snapped back to a ladder rung
    assert built.preset == "2"
    assert built.video_container == "webm"
    assert built.max_resolution == 720
    assert built.frame_rate == 30
    assert built.audio_codec is AudioCodec.OPUS
    assert built.audio_bitrate == 96_000
    assert built.target_size_mb == 25
    assert built.target_size_mode == "strict"
    assert built.resource_governor == "low"
    assert built.use_hw_accel is False
    assert built.rate_control == "vbr"
    assert built.audio_mode == "reencode"
    assert built.tune is None


def test_custom_tune_and_rate_control_reach_the_job_profile(editor, manager):
    editor.new_profile()
    editor._name.setText("Grainy")
    _set(editor._codec, "hevc")
    _set(editor._tune, "grain")
    _set(editor._rate, "cqp")
    editor._crf.setValue(24)
    _set(editor._preset, "slower")
    assert editor.save()
    saved = manager.get_profile("Grainy")
    form = EncodeForm()
    form.apply_profile(saved)
    built = form.build_profile(saved.name, saved)
    assert (built.video_codec, built.crf, built.preset) == (VideoCodec.HEVC, 24, "slower")
    assert built.tune == "grain"
    assert built.rate_control == "cqp"
    settings = built.to_codec_settings()
    assert settings.rate_control is RateControl.CQP
    assert settings.tune == "grain"


def test_a_custom_profile_that_matches_a_rung_keeps_the_ladder_numbers(editor, manager):
    editor.new_profile()
    editor._name.setText("Same as balanced")
    # HEVC, CRF 28, medium: exactly the Balanced rung.
    assert editor._crf.value() == 28
    assert editor._preset.currentData() == "medium"
    assert editor.save()
    saved = manager.get_profile("Same as balanced")
    form = EncodeForm()
    form.apply_profile(saved)
    built = form.build_profile(saved.name, saved)
    assert (built.crf, built.preset) == (28, "medium")
