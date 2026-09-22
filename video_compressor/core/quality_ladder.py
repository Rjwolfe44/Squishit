"""Shared Quick / Balanced / Max encoder ladder.

GUI settings, Quick Compress, and the CLI all read this table so a rung emits
one FFmpeg argument set. Two Max labels stay separate:

* Max / Archival is the SVT-AV1 lane. Hardware stays off.
* Quick Compress Max is the HEVC lane (``HEVC Max``). Hardware may stay on.

Quick Lite, the fastest Quick Compress button, is the H.264 Quick rung.
It prefers hardware in ``HW_VENDOR_PREFERENCE`` order (NVENC, then QSV, then
AMF) and otherwise uses libx264. AV1 is not that rung.

Quick is the fast, larger-file end of a lane (faster preset, lower CRF).
Balanced is the default tradeoff. Max is the best compression that lane
exposes: a slower preset and a CRF higher than Balanced.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence

from .codecs import CodecSettings, VideoCodec


class QualityRung(Enum):
    """Speed/quality rung shared by the GUI and the CLI."""

    QUICK = "quick"
    BALANCED = "balanced"
    MAX = "max"

    @property
    def label(self) -> str:
        return _RUNG_LABELS[self]

    @classmethod
    def from_label(cls, label: str) -> "QualityRung":
        key = str(label or "").strip().lower()
        try:
            return _LABEL_TO_RUNG[key]
        except KeyError as exc:
            known = ", ".join(rung.label for rung in cls)
            raise KeyError(
                f"Unknown quality rung {label!r}. Choose one of: {known}"
            ) from exc


_RUNG_LABELS = {
    QualityRung.QUICK: "Quick",
    QualityRung.BALANCED: "Balanced",
    QualityRung.MAX: "Max",
}

_LABEL_TO_RUNG = {
    "quick": QualityRung.QUICK,
    "fast": QualityRung.QUICK,
    "lite": QualityRung.QUICK,
    "balanced": QualityRung.BALANCED,
    "max": QualityRung.MAX,
}

# CLI --profile names that select a ladder rung. youtube/mobile/streaming do not.
PROFILE_FLAG_RUNGS = {
    "fast": QualityRung.QUICK,
    "balanced": QualityRung.BALANCED,
    "max": QualityRung.MAX,
}

# Hardware selector order. Software is the caller's fallback when none match.
HW_VENDOR_PREFERENCE = ("nvidia", "intel", "amd")

_HW_PRESETS = {
    QualityRung.QUICK: {"nvidia": "p3", "intel": "veryfast", "amd": "speed"},
    QualityRung.BALANCED: {"nvidia": "p5", "intel": "medium", "amd": "balanced"},
    QualityRung.MAX: {"nvidia": "p7", "intel": "slow", "amd": "quality"},
}

ARCHIVAL_PROFILE_NAME = "Max / Archival"
QUICK_COMPRESS_LITE_LABEL = "Quick Lite"
QUICK_COMPRESS_MAX_LABEL = "HEVC Max"

# SVT-AV1 and libaom take a numeric preset. x264 names such as "medium"
# must not be forwarded onto those encoders.
_NUMERIC_PRESET_CODECS = {VideoCodec.SVT_AV1, VideoCodec.AV1}


@dataclass(frozen=True)
class LadderStep:
    """Locked software CRF/preset plus hardware CQ placeholders."""

    rung: QualityRung
    codec: VideoCodec
    crf: int
    preset: str
    allow_hw_accel: bool
    force_software: bool
    hw_cq: int
    hw_presets: Mapping[str, str]
    hint: str


@dataclass(frozen=True)
class EncoderChoice:
    """CRF and preset a GUI control should emit for a codec and rung."""

    codec: VideoCodec
    rung: QualityRung
    crf: int
    preset: str
    force_software: bool
    allow_hw_accel: bool
    hint: str


def _step(
    codec: VideoCodec,
    rung: QualityRung,
    crf: int,
    preset: str,
    hint: str,
    *,
    force_software: bool = False,
) -> LadderStep:
    return LadderStep(
        rung=rung,
        codec=codec,
        crf=crf,
        preset=preset,
        allow_hw_accel=not force_software,
        force_software=force_software,
        hw_cq=crf,
        hw_presets=_HW_PRESETS[rung],
        hint=hint,
    )


# CRF moves with the rung. Quick uses a faster preset and a quality target
# that keeps files larger than Max. Max is the slowest, most compact setting
# shipped for that codec.
_LADDER: Dict[VideoCodec, Dict[QualityRung, LadderStep]] = {
    VideoCodec.HEVC: {
        QualityRung.QUICK: _step(
            VideoCodec.HEVC,
            QualityRung.QUICK,
            24,
            "veryfast",
            "HEVC Quick: faster HEVC encode, larger files (CRF 24, veryfast). "
            "Quick Lite, the fastest context-menu rung, stays on H.264.",
        ),
        QualityRung.BALANCED: _step(
            VideoCodec.HEVC,
            QualityRung.BALANCED,
            28,
            "medium",
            "Balanced: default HEVC tradeoff (CRF 28, medium).",
        ),
        QualityRung.MAX: _step(
            VideoCodec.HEVC,
            QualityRung.MAX,
            30,
            "slow",
            "Quick Compress Max: best HEVC compression (CRF 30, slow). "
            "Not Max / Archival (SVT-AV1).",
        ),
    },
    VideoCodec.SVT_AV1: {
        QualityRung.QUICK: _step(
            VideoCodec.SVT_AV1,
            QualityRung.QUICK,
            32,
            "10",
            "SVT-AV1 Quick: faster archival encode, larger files "
            "(CRF 32, preset 10). Hardware stays off.",
            force_software=True,
        ),
        QualityRung.BALANCED: _step(
            VideoCodec.SVT_AV1,
            QualityRung.BALANCED,
            35,
            "8",
            "SVT-AV1 Balanced: default archival tradeoff "
            "(CRF 35, preset 8). Hardware stays off.",
            force_software=True,
        ),
        QualityRung.MAX: _step(
            VideoCodec.SVT_AV1,
            QualityRung.MAX,
            38,
            "6",
            "Max / Archival: best SVT-AV1 compression (CRF 38, preset 6). "
            "Hardware stays off. Not Quick Compress Max (HEVC).",
            force_software=True,
        ),
    },
    VideoCodec.AV1: {
        QualityRung.QUICK: _step(
            VideoCodec.AV1,
            QualityRung.QUICK,
            26,
            "8",
            "AV1 Quick: faster libaom encode, larger files (CRF 26, cpu-used 8).",
        ),
        QualityRung.BALANCED: _step(
            VideoCodec.AV1,
            QualityRung.BALANCED,
            30,
            "6",
            "AV1 Balanced: default libaom tradeoff (CRF 30, cpu-used 6).",
        ),
        QualityRung.MAX: _step(
            VideoCodec.AV1,
            QualityRung.MAX,
            34,
            "4",
            "AV1 Max: best libaom compression (CRF 34, cpu-used 4). "
            "Max / Archival is SVT-AV1, not libaom.",
        ),
    },
    VideoCodec.H264: {
        QualityRung.QUICK: _step(
            VideoCodec.H264,
            QualityRung.QUICK,
            26,
            "fast",
            "Quick Lite: fastest Quick Compress rung on H.264 "
            "(CRF 26, fast). Prefers NVENC, then QSV, then AMF, then libx264.",
        ),
        QualityRung.BALANCED: _step(
            VideoCodec.H264,
            QualityRung.BALANCED,
            28,
            "medium",
            "H.264 Balanced: default tradeoff (CRF 28, medium).",
        ),
        QualityRung.MAX: _step(
            VideoCodec.H264,
            QualityRung.MAX,
            32,
            "slow",
            "H.264 Max: best H.264 compression (CRF 32, slow). "
            "Not Max / Archival (SVT-AV1).",
        ),
    },
    VideoCodec.VP9: {
        QualityRung.QUICK: _step(
            VideoCodec.VP9,
            QualityRung.QUICK,
            28,
            "veryfast",
            "VP9 Quick: faster encode, larger files (CRF 28, veryfast).",
        ),
        QualityRung.BALANCED: _step(
            VideoCodec.VP9,
            QualityRung.BALANCED,
            31,
            "medium",
            "VP9 Balanced: default tradeoff (CRF 31, medium).",
        ),
        QualityRung.MAX: _step(
            VideoCodec.VP9,
            QualityRung.MAX,
            34,
            "slow",
            "VP9 Max: best VP9 compression (CRF 34, slow). "
            "Not Max / Archival (SVT-AV1).",
        ),
    },
}


def ordered_hw_vendors(available: Sequence[str]) -> list[str]:
    """Return vendors in NVENC → QSV → AMF order, then any others.

    Software is not a vendor. Callers that miss every name fall back to the
    software encoder themselves.
    """

    available_set = set(available)
    preferred = [vendor for vendor in HW_VENDOR_PREFERENCE if vendor in available_set]
    rest = [vendor for vendor in available if vendor not in HW_VENDOR_PREFERENCE]
    return preferred + rest


def get_step(codec: VideoCodec, rung: QualityRung) -> LadderStep:
    """Return the locked step for a codec lane and rung."""

    try:
        return _LADDER[codec][rung]
    except KeyError as exc:
        known = ", ".join(codec.value for codec in _LADDER)
        raise KeyError(
            f"No quality ladder step for {codec.value} / {rung.value}. "
            f"Known codecs: {known}"
        ) from exc


def uses_numeric_preset(codec: VideoCodec) -> bool:
    """True when the encoder preset is a number, not an x264 name."""

    return codec in _NUMERIC_PRESET_CODECS


def coerce_preset_override(
    codec: VideoCodec, preset_override: Optional[str]
) -> Optional[str]:
    """Drop x264-style names for SVT-AV1 and libaom.

    A numeric override such as ``"4"`` still wins. ``"medium"`` or ``"slow"``
    does not, so the ladder preset stays in place.
    """

    if preset_override is None:
        return None
    text = str(preset_override).strip()
    if not text:
        return None
    if uses_numeric_preset(codec) and not text.isdigit():
        return None
    return text


def matching_rung(codec: VideoCodec, crf: int, preset: str) -> Optional[QualityRung]:
    """Return the rung whose locked CRF and preset match, if any."""

    steps = _LADDER.get(codec)
    if not steps:
        return None
    for rung, step in steps.items():
        if step.crf == crf and step.preset == preset:
            return rung
    return None


def resolve_encoder_choice(
    codec: VideoCodec,
    rung: QualityRung,
    *,
    crf_override: Optional[int] = None,
    preset_override: Optional[str] = None,
) -> EncoderChoice:
    """Resolve the CRF and preset the GUI should emit.

    Overrides exist for an explicit slider or saved custom profile. With no
    override, the result is the locked ladder step.
    """

    step = get_step(codec, rung)
    preset = coerce_preset_override(codec, preset_override)
    return EncoderChoice(
        codec=codec,
        rung=rung,
        crf=step.crf if crf_override is None else crf_override,
        preset=step.preset if preset is None else preset,
        force_software=step.force_software,
        allow_hw_accel=step.allow_hw_accel,
        hint=step.hint,
    )


def profile_picker_label(name: str) -> str:
    """Profile pill text. Max / Archival is never shortened to Max."""

    return name


def _vendor_for_encoder(encoder_name: str) -> str:
    if "nvenc" in encoder_name:
        return "nvidia"
    if "qsv" in encoder_name:
        return "intel"
    if "amf" in encoder_name:
        return "amd"
    raise ValueError(f"No ladder hardware vendor for encoder {encoder_name!r}")


def ladder_codec_settings(
    codec: VideoCodec,
    rung: QualityRung,
    *,
    hw_encoder: Optional[str] = None,
) -> CodecSettings:
    """Build CodecSettings for a rung.

    SVT-AV1 always stays on libsvtav1. Other lanes use ``hw_cq`` and the
    vendor preset placeholder when ``hw_encoder`` is set.
    """

    step = get_step(codec, rung)
    use_hw = bool(hw_encoder) and not step.force_software
    if use_hw:
        vendor = _vendor_for_encoder(hw_encoder or "")
        try:
            preset = step.hw_presets[vendor]
        except KeyError as exc:
            raise KeyError(
                f"No hardware preset placeholder for {codec.value} on {vendor}"
            ) from exc
        crf = step.hw_cq
    else:
        preset = step.preset
        crf = step.crf
    return CodecSettings(
        video_codec=codec,
        crf=crf,
        preset=preset,
        disable_audio=True,
        pixel_format="yuv420p",
    )


def apply_hardware_ladder(settings: CodecSettings, hw_encoder: Optional[str]) -> None:
    """Swap a matched software rung for that rung's hardware CQ and preset.

    Unmatched CRF/preset pairs are left alone. SVT-AV1 stays on its numeric
    software preset. Encoders without a placeholder (VideoToolbox) are left
    alone too.
    """

    if not hw_encoder or settings.crf is None:
        return
    rung = matching_rung(settings.video_codec, settings.crf, settings.preset)
    if rung is None:
        return
    step = get_step(settings.video_codec, rung)
    if step.force_software:
        return
    try:
        vendor = _vendor_for_encoder(hw_encoder)
        preset = step.hw_presets[vendor]
    except (ValueError, KeyError):
        return
    settings.preset = preset
    settings.crf = step.hw_cq


def ladder_video_ffmpeg_args(
    codec: VideoCodec,
    rung: QualityRung,
    *,
    hw_encoder: Optional[str] = None,
) -> list[str]:
    """Video-only FFmpeg args for a rung. Audio is omitted on purpose."""

    step = get_step(codec, rung)
    encoder = None if step.force_software else hw_encoder
    settings = ladder_codec_settings(codec, rung, hw_encoder=encoder)
    args = settings.to_ffmpeg_args(hw_encoder=encoder)
    if args[-1:] == ["-an"]:
        args = args[:-1]
    return args
