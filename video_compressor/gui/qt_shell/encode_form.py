"""Encode choices for the Qt shell.

This builds the same ``CompressionProfile`` the CustomTkinter settings panel
handed to ``VideoCompressor``. CRF, preset, and hardware locks come from the
shared quality ladder. Nothing here assembles an FFmpeg command.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from ...core.codecs import AudioCodec, CodecManager, VideoCodec, VideoContainer
from ...core.profiles import CompressionProfile
from ...core.quality_ladder import (
    QualityRung,
    coerce_preset_override,
    matching_rung,
    resolve_encoder_choice,
)
from ...core.utils import (
    detect_media_type,
    get_audio_extension,
    get_image_extension,
    get_video_extension,
    sanitize_filename,
)

CODEC_CHOICES = (
    VideoCodec.H264,
    VideoCodec.HEVC,
    VideoCodec.VP9,
    VideoCodec.SVT_AV1,
    VideoCodec.AV1,
)
CODEC_LABELS = {
    VideoCodec.H264: "H.264",
    VideoCodec.HEVC: "HEVC",
    VideoCodec.VP9: "VP9",
    VideoCodec.SVT_AV1: "SVT-AV1",
    VideoCodec.AV1: "AV1 (libaom)",
}
RUNG_LABELS = [rung.label for rung in QualityRung]
TARGET_MODES = ["auto", "fast", "balanced", "strict", "exact"]
RESOLUTION_LABELS = ["original", "4K", "1440p", "1080p", "720p", "480p"]
FRAME_RATE_LABELS = ["original", "60", "30", "24"]
GOVERNORS = ["auto", "low", "balanced", "max"]
EXACT_AUDIO = ["keep", "reduce", "drop"]
AUDIO_CODECS = ["aac", "opus", "mp3", "vorbis", "flac", "ac3"]

_RES_TO_HEIGHT = {
    "original": None,
    "4K": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
}
_HEIGHT_TO_RES = {value: key for key, value in _RES_TO_HEIGHT.items() if value}
_FPS = {"original": None, "60": 60, "30": 30, "24": 24}

_EXACT_HW_HINT = (
    "Exact target tries a hardware encoder first when one is available. "
    "If that encode misses the size, fails, or is short enough that "
    "padding would hit the exact target, SquishIt asks before a "
    "software retry. No keeps the hardware file and does not pad it."
)


def containers_for(codec: VideoCodec) -> list[str]:
    """Containers the codec matrix allows. Does not probe FFmpeg."""

    manager = CodecManager()
    found = [item.value for item in manager.get_supported_containers(codec)]
    return found or [manager.get_default_container(codec).value]


def default_container(codec: VideoCodec) -> str:
    return CodecManager().get_default_container(codec).value


@dataclass
class EncodeForm:
    """Mutable encode settings. The window copies these into a profile."""

    video_codec: VideoCodec = VideoCodec.HEVC
    compression_label: str = "Balanced"
    use_hw_accel: bool = True
    target_size_enabled: bool = False
    target_size_mb: Optional[int] = None
    target_size_mode: str = "auto"
    exact_audio_policy: str = "reduce"
    exact_two_pass: bool = False
    video_container: str = "mp4"
    resolution_label: str = "original"
    frame_rate_label: str = "original"
    audio_codec: AudioCodec = AudioCodec.AAC
    audio_kbps: int = 128
    resource_governor: str = "auto"
    parallel_jobs: int = 1
    output_name_template: str = "{name}_compressed"
    image_format: str = "webp"
    output_mode: str = "video"
    threads: Optional[int] = None
    crf_override: Optional[int] = None
    preset_override: Optional[str] = None
    quick_name: Optional[str] = None
    trim_enabled: bool = False
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None
    extra_ffmpeg_args: str = ""

    def rung(self) -> QualityRung:
        try:
            return QualityRung.from_label(self.compression_label)
        except KeyError:
            return QualityRung.BALANCED

    def ladder_choice(self):
        return resolve_encoder_choice(
            self.video_codec,
            self.rung(),
            crf_override=self.crf_override,
            preset_override=self.preset_override,
        )

    def effective_hw(self) -> bool:
        choice = self.ladder_choice()
        if choice.force_software:
            return False
        return bool(self.use_hw_accel)

    def ladder_hint(self) -> str:
        choice = self.ladder_choice()
        if self.crf_override is not None or self.preset_override is not None:
            return (
                f"Custom encoder settings (CRF {choice.crf}, preset {choice.preset})."
            )
        return choice.hint

    def hw_hint(self) -> str:
        choice = self.ladder_choice()
        if choice.force_software:
            return (
                "Hardware acceleration stays off for this codec. "
                "SquishIt uses the software encoder."
            )
        if not self.use_hw_accel:
            return "Hardware acceleration off. SquishIt will use a software encoder."
        if self.target_size_enabled and self.target_size_mode == "exact":
            return _EXACT_HW_HINT
        return (
            "Hardware preference is NVENC, then QSV, then AMF, then software. "
            "Quick Compress presets do not ask before a software encoder."
        )

    def note_manual_edit(self) -> None:
        """A hand edit leaves the Quick Compress preset label behind."""

        self.quick_name = None

    def set_codec(self, codec: VideoCodec) -> None:
        self.video_codec = codec
        self.crf_override = None
        self.preset_override = None
        self.note_manual_edit()
        allowed = containers_for(codec)
        if self.video_container not in allowed:
            self.video_container = default_container(codec)
        if self.ladder_choice().force_software:
            self.use_hw_accel = False

    def set_rung_label(self, label: str) -> None:
        self.compression_label = label
        self.crf_override = None
        self.preset_override = None
        self.note_manual_edit()
        if self.ladder_choice().force_software:
            self.use_hw_accel = False

    def apply_profile(
        self,
        profile: CompressionProfile,
        *,
        quick_name: Optional[str] = None,
    ) -> None:
        """Copy a saved profile into the form. Ladder locks still win later."""

        self.quick_name = quick_name
        self.video_codec = profile.video_codec
        matched = matching_rung(profile.video_codec, profile.crf, profile.preset)
        if matched is None:
            self.crf_override = profile.crf
            self.preset_override = coerce_preset_override(
                profile.video_codec,
                profile.preset,
            )
            self.compression_label = QualityRung.BALANCED.label
        else:
            self.crf_override = None
            self.preset_override = None
            self.compression_label = matched.label
        self.use_hw_accel = bool(profile.use_hw_accel)
        if self.ladder_choice().force_software:
            self.use_hw_accel = False
        self.video_container = profile.video_container or default_container(
            self.video_codec
        )
        allowed = containers_for(self.video_codec)
        if self.video_container not in allowed:
            self.video_container = default_container(self.video_codec)
        self.image_format = profile.image_format or "webp"
        self.audio_codec = profile.audio_codec
        try:
            self.audio_kbps = max(1, int(profile.audio_bitrate) // 1000)
        except (TypeError, ValueError):
            self.audio_kbps = 128
        self.resolution_label = _HEIGHT_TO_RES.get(profile.max_resolution, "original")
        self.frame_rate_label = (
            str(int(profile.frame_rate)) if profile.frame_rate else "original"
        )
        self.target_size_enabled = bool(profile.target_size_mb)
        self.target_size_mb = profile.target_size_mb
        self.target_size_mode = profile.target_size_mode or self.target_size_mode
        self.exact_audio_policy = (
            getattr(profile, "exact_audio_policy", "reduce") or "reduce"
        )
        self.exact_two_pass = bool(getattr(profile, "exact_two_pass", False))
        self.resource_governor = profile.resource_governor or "auto"
        self.output_mode = profile.output_mode or "video"
        self.threads = profile.threads
        self.trim_enabled = bool(profile.trim_enabled)
        self.trim_start = profile.trim_start
        self.trim_end = profile.trim_end
        self.extra_ffmpeg_args = profile.extra_ffmpeg_args or ""

    def build_profile(self, name: str, base: CompressionProfile) -> CompressionProfile:
        """Profile passed to ``compress_async``. Encoder args stay in the backend."""

        choice = self.ladder_choice()
        audio = self.audio_codec
        if isinstance(audio, str):
            audio = AudioCodec(audio)
        target = self.target_size_mb if self.target_size_enabled else None
        try:
            container = VideoContainer(self.video_container).value
        except ValueError:
            container = default_container(choice.codec)
        return CompressionProfile(
            name=self.quick_name or name,
            profile_type=base.profile_type,
            description=base.description or choice.hint,
            video_codec=choice.codec,
            crf=choice.crf,
            preset=choice.preset,
            audio_codec=audio,
            audio_bitrate=int(self.audio_kbps) * 1000,
            output_mode=self.output_mode or base.output_mode,
            max_resolution=_RES_TO_HEIGHT.get(self.resolution_label),
            frame_rate=_FPS.get(self.frame_rate_label),
            use_hw_accel=False if choice.force_software else bool(self.use_hw_accel),
            target_size_mb=target,
            target_size_mode=self.target_size_mode or base.target_size_mode,
            exact_audio_policy=self.exact_audio_policy,
            exact_two_pass=self.exact_two_pass,
            video_container=container,
            image_format=self.image_format or base.image_format,
            threads=self.threads,
            resource_governor=self.resource_governor or base.resource_governor,
            trim_enabled=self.trim_enabled,
            trim_start=self.trim_start,
            trim_end=self.trim_end,
            extra_ffmpeg_args=self.extra_ffmpeg_args,
            rate_control=base.rate_control,
            audio_mode=base.audio_mode,
            tune=base.tune,
        )


def output_path_for(
    input_path: Path,
    profile: CompressionProfile,
    folder: Optional[Path],
    template: str,
) -> Path:
    """Destination path. Container and suffix come from the profile and template."""

    media_type = detect_media_type(input_path)
    if profile.output_mode == "audio":
        ext = "." + get_audio_extension(profile.audio_codec.value)
    elif media_type == "image":
        ext = "." + get_image_extension(profile.image_format)
    else:
        ext = "." + get_video_extension(
            profile.video_codec.value,
            profile.video_container,
        )
    dest = folder or input_path.parent
    dest.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    pattern = (template or "").strip() or "{name}_compressed"
    try:
        stem = pattern.format(
            name=input_path.stem,
            profile=profile.name,
            codec=(
                profile.audio_codec.value
                if profile.output_mode == "audio"
                else profile.video_codec.value
            ),
            crf=profile.crf,
            date=now.strftime("%Y-%m-%d"),
            timestamp=now.strftime("%H%M%S"),
        )
    except Exception:
        stem = f"{input_path.stem}_compressed"
    return dest / (sanitize_filename(stem) + ext)
