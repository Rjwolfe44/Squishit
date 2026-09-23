"""Quick Compress window for Explorer and ``--quick-compress``.

Quick Lite, Balanced, and HEVC Max are ladder presets with no target size.
This window does not ask before a software encoder. That confirm stays on
the main window, which is where exact-size jobs are started.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...config import APP_NAME, get_config_manager
from ...core.codecs import VideoCodec
from ...core.compressor import CompressionResult, VideoCompressor
from ...core.profiles import (
    QUICK_COMPRESS_ORDER,
    CompressionProfile,
    build_quick_compress_profiles,
    normalize_quick_compress_name,
    retarget_quick_compress_fallback,
)
from ...core.utils import detect_media_type, get_image_extension, get_video_extension
from ..copy import QUICK_COMPRESS_SUBTITLE, QUICK_COMPRESS_TITLE
from .theme import DARK_STYLESHEET

logger = logging.getLogger(__name__)

_QUICK_PRESETS = build_quick_compress_profiles()
_PRESET_ORDER = list(QUICK_COMPRESS_ORDER)


def _window_icon_path() -> Optional[Path]:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    icon_path = base_dir / "assets" / "squishit.ico"
    return icon_path if icon_path.exists() else None


def _retint(button: QPushButton, selected: bool) -> None:
    button.setProperty("selected", selected)
    style = button.style()
    style.unpolish(button)
    style.polish(button)


class QuickCompressWindow(QWidget):
    """One-file progress window. Presets come from the shared ladder."""

    def __init__(self, input_file: Path):
        super().__init__()
        self.setStyleSheet(DARK_STYLESHEET)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.input_file = Path(input_file)
        self.compressor: Optional[VideoCompressor] = None
        self._config = get_config_manager()
        self._events: queue.Queue = queue.Queue()
        self._job_id: Optional[str] = None
        self._running = False
        self._preset_buttons: dict[str, QPushButton] = {}
        saved = normalize_quick_compress_name(self._config.config.quick_compress_profile)
        self._preset = saved if saved in _QUICK_PRESETS else "Balanced"

        self.setWindowTitle(f"{APP_NAME} {QUICK_COMPRESS_TITLE}")
        self.setMinimumSize(560, 380)
        self.resize(640, 440)
        icon_path = _window_icon_path()
        if icon_path is not None:
            icon = QIcon(str(icon_path))
            if not icon.isNull():
                self.setWindowIcon(icon)

        self._build()
        self._refresh_preview()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._pump)
        self._timer.start()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(8)

        title = QLabel(QUICK_COMPRESS_TITLE)
        title.setObjectName("title")
        outer.addWidget(title)
        subtitle = QLabel(QUICK_COMPRESS_SUBTITLE)
        subtitle.setObjectName("hint")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        name = QLabel(self.input_file.name)
        name.setWordWrap(True)
        outer.addWidget(name)

        row = QHBoxLayout()
        row.setSpacing(6)
        preset_label = QLabel("Preset")
        preset_label.setObjectName("muted")
        row.addWidget(preset_label)
        for preset_name in _PRESET_ORDER:
            button = QPushButton(preset_name)
            button.setObjectName("pill")
            button.clicked.connect(
                lambda _checked=False, name=preset_name: self._set_preset(name)
            )
            self._preset_buttons[preset_name] = button
            row.addWidget(button)
        row.addStretch(1)
        outer.addLayout(row)
        self._style_presets()

        self._hint = QLabel("")
        self._hint.setObjectName("hint")
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint)

        self._status = QLabel("Waiting to start")
        self._status.setObjectName("hint")
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        outer.addWidget(self._bar)

        self._detail = QLabel("0%")
        self._detail.setWordWrap(True)
        outer.addWidget(self._detail)
        self._destination = QLabel("")
        self._destination.setObjectName("hint")
        self._destination.setWordWrap(True)
        outer.addWidget(self._destination)
        outer.addStretch(1)

        actions = QHBoxLayout()
        open_app = QPushButton("Open Full App")
        open_app.clicked.connect(self._open_full_app)
        actions.addWidget(open_app)
        actions.addStretch(1)
        self._start_btn = QPushButton("Start Compression")
        self._start_btn.setObjectName("primary")
        self._start_btn.clicked.connect(self.start_compression)
        actions.addWidget(self._start_btn)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        actions.addWidget(cancel)
        outer.addLayout(actions)

    def _style_presets(self) -> None:
        for name, button in self._preset_buttons.items():
            _retint(button, name == self._preset)

    def _get_compressor(self) -> VideoCompressor:
        if self.compressor is None:
            self.compressor = VideoCompressor()
        return self.compressor

    def _set_preset(self, preset: str) -> None:
        self._preset = preset
        self._style_presets()
        try:
            self._config.update_config(quick_compress_profile=preset)
        except Exception:
            logger.debug("Could not store the Quick Compress preset", exc_info=True)
        if not self._running:
            self._refresh_preview()

    def _profile(self, resolve_fallback: bool = True) -> CompressionProfile:
        profile = CompressionProfile.from_dict(_QUICK_PRESETS[self._preset].to_dict())
        if not resolve_fallback or profile.video_codec != VideoCodec.HEVC:
            return profile
        hevc_missing = not self._get_compressor().codec_manager.is_codec_usable(VideoCodec.HEVC)
        if hevc_missing:
            fallback_codec = self._get_compressor().codec_manager.get_best_codec(
                prefer_efficiency=True
            )
            retarget_quick_compress_fallback(profile, fallback_codec, self._preset)
            profile.video_container = (
                self._get_compressor()
                .codec_manager.get_compatible_container(
                    fallback_codec,
                    profile.video_container,
                )
                .value
            )
        return profile

    def _output_file(self, profile: Optional[CompressionProfile] = None) -> Path:
        config = self._config.config
        folder = self.input_file.parent
        folder.mkdir(parents=True, exist_ok=True)
        profile = profile or self._profile(resolve_fallback=False)
        media_type = detect_media_type(self.input_file)
        if media_type == "image":
            ext = "." + get_image_extension(profile.image_format)
        else:
            ext = "." + get_video_extension(profile.video_codec.value, profile.video_container)
        return folder / f"{self.input_file.stem}{config.output_suffix}{ext}"

    def _refresh_preview(self) -> None:
        profile = self._profile(resolve_fallback=False)
        self._hint.setText(profile.description)
        self._destination.setText(f"Will save to: {self._output_file(profile)}")

    def start_compression(self) -> None:
        if self._running:
            return
        if not self.input_file.exists():
            QMessageBox.critical(self, APP_NAME, f"File not found: {self.input_file}")
            self.close()
            return
        self._running = True
        self._start_btn.setEnabled(False)
        self._start_btn.setText("Compressing…")
        for button in self._preset_buttons.values():
            button.setEnabled(False)
        profile = self._profile(resolve_fallback=True)
        output_file = self._output_file(profile)
        self._destination.setText(f"Saving to: {output_file}")
        compressor = self._get_compressor()
        compressor.set_progress_callback(lambda job: self._events.put(("progress", job)))
        self._job_id = compressor.compress_async(
            input_file=self.input_file,
            output_file=output_file,
            profile=profile,
            callback=lambda result: self._events.put(("result", result)),
        )

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "progress":
                    self._show_progress(payload)
                elif kind == "result":
                    self._complete(payload)
                    return
        except queue.Empty:
            pass

    def _show_progress(self, job) -> None:
        try:
            percent = int(float(job.progress))
        except (TypeError, ValueError):
            percent = 0
        self._bar.setValue(max(0, min(100, percent)))
        status = getattr(getattr(job, "status", None), "value", getattr(job, "status", ""))
        self._status.setText(str(status or "compressing").replace("_", " ").title())
        parts = [f"{percent}%"]
        speed = getattr(job, "speed", 0) or 0
        eta = getattr(job, "eta", 0) or 0
        if speed:
            parts.append(f"{float(speed):.1f} fps")
        if eta:
            parts.append(f"ETA {int(float(eta))}s")
        self._detail.setText("  ·  ".join(parts))

    def _complete(self, result: CompressionResult) -> None:
        self._running = False
        if getattr(result, "success", False):
            self._bar.setValue(100)
            if getattr(result, "skipped", False):
                self._status.setText("Skipped")
            elif getattr(result, "kept_original", False):
                self._status.setText("Kept Original")
            else:
                self._status.setText("Completed")
            self._detail.setText(result.note or "Compression finished")
            if result.output_file:
                self._destination.setText(f"Saved to: {result.output_file}")
            self._start_btn.setText("Close")
            self._start_btn.setEnabled(True)
            self._start_btn.clicked.disconnect()
            self._start_btn.clicked.connect(self.close)
            QTimer.singleShot(1800, self.close)
        else:
            self._status.setText("Failed")
            self._detail.setText(result.error_message or "Compression failed")
            self._start_btn.setText("Try Again")
            self._start_btn.setEnabled(True)
            for button in self._preset_buttons.values():
                button.setEnabled(True)
            QMessageBox.critical(self, APP_NAME, result.error_message or "Compression failed")

    def _open_full_app(self) -> None:
        subprocess.Popen(
            [sys.executable, "-m", "video_compressor", "--open", str(self.input_file)]
        )
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._job_id and self._running and self.compressor is not None:
            try:
                self.compressor.cancel_job(self._job_id)
            except Exception:
                logger.debug("Could not cancel Quick Compress", exc_info=True)
        self._timer.stop()
        super().closeEvent(event)


def run_quick_compress(input_file: Path) -> int:
    """Open the Quick Compress window and run the Qt event loop."""

    from PySide6.QtWidgets import QApplication

    from .theme import apply_theme

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    apply_theme(app)
    window = QuickCompressWindow(Path(input_file))
    window.show()
    return app.exec()
