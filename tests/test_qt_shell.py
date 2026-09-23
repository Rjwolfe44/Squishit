"""Qt shell: ladder settings, launch default, and an offscreen window smoke."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from video_compressor.core.codecs import VideoCodec
from video_compressor.core.profiles import (
    ProfileManager,
    build_quick_compress_profiles,
)
from video_compressor.core.quality_ladder import QualityRung, get_step
from video_compressor.gui.launch import ui_backend
from video_compressor.gui.qt_shell.encode_form import CODEC_CHOICES, EncodeForm
from video_compressor.gui.qt_shell.theme import DARK_STYLESHEET

_ROOT = Path(__file__).resolve().parents[1]


def test_default_ui_backend_is_qt(monkeypatch):
    monkeypatch.delenv("SQUISHIT_UI", raising=False)
    assert ui_backend() == "qt"
    monkeypatch.setenv("SQUISHIT_UI", "ctk")
    assert ui_backend() == "ctk"
    monkeypatch.setenv("SQUISHIT_UI", "qt")
    assert ui_backend() == "qt"


def test_scrollbars_are_visible_in_the_theme():
    assert "QScrollBar::handle:vertical" in DARK_STYLESHEET
    assert "background: #d4d4d8" in DARK_STYLESHEET
    assert "width: 14px" in DARK_STYLESHEET


def test_av1_stays_in_the_codec_list():
    assert VideoCodec.AV1 in CODEC_CHOICES
    assert VideoCodec.SVT_AV1 in CODEC_CHOICES
    assert VideoCodec.H264 in CODEC_CHOICES


def test_quick_lite_form_keeps_h264_hardware_and_no_target():
    form = EncodeForm()
    preset = build_quick_compress_profiles()["Quick Lite"]
    form.apply_profile(preset, quick_name="Quick Lite")
    profile = form.build_profile("Fast", preset)
    step = get_step(VideoCodec.H264, QualityRung.QUICK)
    assert profile.name == "Quick Lite"
    assert profile.video_codec == VideoCodec.H264
    assert profile.crf == step.crf
    assert profile.preset == step.preset
    assert profile.use_hw_accel is True
    assert profile.target_size_mb is None


def test_hevc_max_form_is_not_archival():
    form = EncodeForm()
    preset = build_quick_compress_profiles()["HEVC Max"]
    form.apply_profile(preset, quick_name="HEVC Max")
    profile = form.build_profile("Max / Archival", preset)
    step = get_step(VideoCodec.HEVC, QualityRung.MAX)
    assert profile.name == "HEVC Max"
    assert profile.video_codec == VideoCodec.HEVC
    assert profile.crf == step.crf
    assert profile.preset == step.preset
    assert profile.use_hw_accel is True


def test_archival_profile_forces_software():
    form = EncodeForm()
    archival = next(
        profile
        for profile in ProfileManager.DEFAULT_PROFILES
        if profile.name == "Max / Archival"
    )
    form.apply_profile(archival)
    profile = form.build_profile(archival.name, archival)
    step = get_step(VideoCodec.SVT_AV1, QualityRung.MAX)
    assert profile.video_codec == VideoCodec.SVT_AV1
    assert profile.crf == step.crf
    assert profile.preset == step.preset
    assert profile.use_hw_accel is False
    assert form.effective_hw() is False


def test_exact_size_keeps_hardware_preference_for_the_ask():
    form = EncodeForm()
    balanced = next(
        profile
        for profile in ProfileManager.DEFAULT_PROFILES
        if profile.name == "Balanced"
    )
    form.apply_profile(balanced)
    form.target_size_enabled = True
    form.target_size_mb = 8
    form.target_size_mode = "exact"
    profile = form.build_profile(balanced.name, balanced)
    assert profile.video_codec == VideoCodec.HEVC
    assert profile.use_hw_accel is True
    assert profile.target_size_mb == 8
    assert profile.target_size_mode == "exact"
    assert "asks before" in form.hw_hint()


def test_qt_sources_keep_the_fallback_split():
    main = (_ROOT / "video_compressor/gui/qt_shell/main_window.py").read_text(
        encoding="utf-8"
    )
    quick = (_ROOT / "video_compressor/gui/qt_shell/quick_window.py").read_text(
        encoding="utf-8"
    )
    launch = (_ROOT / "video_compressor/gui/launch.py").read_text(encoding="utf-8")
    assert "bind_main_window_software_fallback" in main
    assert "handle_software_fallback_queue_message" in main
    assert "help_menu_items" in main
    assert "webbrowser.open" in main
    assert "bind_main_window_software_fallback" not in quick
    assert "set_software_fallback_callback" not in quick
    assert 'os.environ.get("SQUISHIT_UI", "qt")' in launch


class _FakeCompressor:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.fallback = None
        self.cancelled = None

    def set_progress_callback(self, callback):
        self.progress = callback

    def set_software_fallback_callback(self, callback):
        self.fallback = callback

    def analyze_media(self, path):
        return None

    def compress_async(self, **kwargs):
        self.calls.append(kwargs)
        return "job-1"

    def get_job(self, job_id):
        return None

    def queue_slots(self, *args, **kwargs):
        return 1

    def cancel_job(self, job_id):
        self.cancelled = job_id

    def get_active_jobs(self):
        return []

    def _select_hw_encoder(self, codec):
        return None


@pytest.fixture
def quiet_config(monkeypatch):
    monkeypatch.setattr(
        "video_compressor.config.ConfigManager.update_config",
        lambda self, **kwargs: None,
    )
    monkeypatch.setattr(
        "video_compressor.config.ConfigManager.save_config",
        lambda self: None,
    )


def test_main_window_quick_lite_starts_the_existing_compressor(
    monkeypatch, quiet_config, tmp_path
):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QScrollArea

    from video_compressor.gui.qt_shell.main_window import SquishItWindow

    monkeypatch.setattr(
        "video_compressor.core.compressor.VideoCompressor",
        _FakeCompressor,
    )
    monkeypatch.setattr(SquishItWindow, "_start_hw_probe", lambda self: None)
    app = QApplication.instance() or QApplication([])
    window = SquishItWindow()
    try:
        window.show()
        app.processEvents()
        assert window.isVisible()
        assert window.findChild(QScrollArea, "queueScroll") is not None
        assert window.findChild(QScrollArea, "settingsScroll") is not None
        codec = window._settings._codec
        assert codec.findData(VideoCodec.AV1.value) >= 0
        clip = tmp_path / "clip.mp4"
        clip.write_bytes(b"not-a-real-video")
        window.select_profile("Max / Archival")
        archival = window.job_profile()
        assert archival.video_codec == VideoCodec.SVT_AV1
        assert archival.use_hw_accel is False
        assert window._settings._hw.isEnabled() is False
        window.select_quick("HEVC Max")
        hevc_max = window.job_profile()
        assert hevc_max.name == "HEVC Max"
        assert hevc_max.video_codec == VideoCodec.HEVC
        assert hevc_max.use_hw_accel is True
        assert hevc_max.target_size_mb is None
        window.select_quick("Quick Lite")
        window.add_paths([str(clip)])
        app.processEvents()
        window.start_compression()
        app.processEvents()
        compressor = window._compressor
        assert isinstance(compressor, _FakeCompressor)
        assert compressor.fallback is not None
        assert len(compressor.calls) == 1
        profile = compressor.calls[0]["profile"]
        assert profile.video_codec == VideoCodec.H264
        assert profile.use_hw_accel is True
        assert profile.target_size_mb is None
        assert profile.crf == get_step(VideoCodec.H264, QualityRung.QUICK).crf
        assert compressor.calls[0]["input_file"] == clip
    finally:
        window.close()
        app.processEvents()


def test_home_path_shows_real_encoder_and_plain_quick_copy(
    monkeypatch, quiet_config
):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton

    from video_compressor.gui.hw_status import FoundHardware, HardwareProbe
    from video_compressor.gui.qt_shell.main_window import SquishItWindow

    monkeypatch.setattr(SquishItWindow, "_start_hw_probe", lambda self: None)
    app = QApplication.instance() or QApplication([])
    window = SquishItWindow()
    try:
        window.show()
        app.processEvents()
        assert window.form.quick_name
        assert window._quick_buttons[window.form.quick_name].property("selected") is True
        assert not any(
            button.property("selected")
            for button in window._settings._profile_buttons.values()
        )
        assert window._compress_btn.text() == "Quick Compress"
        assert window._settings._advanced.isVisible() is False
        tray = window.findChild(QLabel, "trayHint")
        assert tray is not None
        assert "does not put SquishIt in the tray" in tray.text()
        button_text = [button.text() for button in window.findChildren(QPushButton)]
        assert "Send to tray" not in button_text
        assert "CRF" not in window._hw_card.using.text()

        probe = HardwareProbe(
            found=(
                FoundHardware("NVENC", "NVIDIA GeForce RTX 4070"),
                FoundHardware("QSV", "Intel UHD 770"),
            ),
            encoders={
                "h264": "h264_nvenc",
                "hevc": "hevc_nvenc",
                "svt-av1": None,
            },
        )
        window.apply_hardware_probe(probe)
        app.processEvents()
        assert window._hw_card.badge.text() == "NVENC"
        assert "RTX 4070" in window._hw_card.found.text()
        assert window._hw_card.using.text() == (
            "Quick Compress will use NVIDIA hardware (NVENC)."
        )
        assert "NVENC" in window._settings._hw_hint.text()

        window.select_profile("Max / Archival")
        assert window._compress_btn.text() == "Compress"
        assert "software encoding" in window._hw_card.using.text()
        assert "keeps hardware off" in window._hw_card.using.text()
        assert "RTX 4070" in window._hw_card.found.text()

        window.select_quick("Quick Lite")
        window.form.target_size_enabled = True
        window.form.target_size_mode = "exact"
        window.form.target_size_mb = 8
        window._settings.load(window.form)
        window._refresh_hw_status()
        hint = window._settings._hw_hint.text()
        assert "asks before" in hint
        assert "NVENC" in hint
        assert window._hw_card.badge.text() == "NVENC"
    finally:
        window.close()
        app.processEvents()


def test_quick_window_shows_the_encoder_it_will_use(
    monkeypatch, quiet_config, tmp_path
):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from video_compressor.gui.hw_status import FoundHardware, HardwareProbe
    from video_compressor.gui.qt_shell.quick_window import QuickCompressWindow

    monkeypatch.setattr(QuickCompressWindow, "_start_hw_probe", lambda self: None)
    app = QApplication.instance() or QApplication([])
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not-a-real-video")
    window = QuickCompressWindow(clip)
    try:
        window.show()
        app.processEvents()
        window.apply_hardware_probe(
            HardwareProbe(
                found=(FoundHardware("AMF", "AMD Radeon RX 7800 XT"),),
                encoders={"h264": "h264_amf", "hevc": None},
            )
        )
        window._set_preset("Quick Lite")
        app.processEvents()
        assert window._start_btn.text() == "Quick Compress"
        assert window._hw_card.badge.text() == "AMF"
        assert "RX 7800 XT" in window._hw_card.found.text()
        assert window._hw_card.using.text() == (
            "Quick Compress will use AMD hardware (AMF)."
        )
        assert "CRF" not in window._hint.text()
        assert "long wait" in window._hint.text()
    finally:
        window.close()
        app.processEvents()


def test_quick_window_starts_without_asking(monkeypatch, quiet_config, tmp_path):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from video_compressor.gui.qt_shell import quick_window

    monkeypatch.setattr(quick_window, "VideoCompressor", _FakeCompressor)
    monkeypatch.setattr(
        quick_window.QuickCompressWindow, "_start_hw_probe", lambda self: None
    )
    QuickCompressWindow = quick_window.QuickCompressWindow
    app = QApplication.instance() or QApplication([])
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"not-a-real-video")
    window = QuickCompressWindow(clip)
    try:
        window.show()
        app.processEvents()
        window._set_preset("Quick Lite")
        window.start_compression()
        app.processEvents()
        assert isinstance(window.compressor, _FakeCompressor)
        assert window.compressor.fallback is None
        profile = window.compressor.calls[0]["profile"]
        assert profile.video_codec == VideoCodec.H264
        assert profile.use_hw_accel is True
        assert profile.target_size_mb is None
    finally:
        window.close()
        app.processEvents()
