"""Plain-language hardware status for the Qt home and Quick Compress paths.

What was found comes from the hardware detector. What Quick Compress will
use comes from ``VideoCompressor._select_hw_encoder`` (NVENC, then QSV, then
AMF, then software). This module does not pick a different encoder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from ..core.codecs import VideoCodec
from ..core.quality_ladder import ordered_hw_vendors

logger = logging.getLogger(__name__)

_VENDOR_FAMILY = {
    "nvidia": "NVENC",
    "intel": "QSV",
    "amd": "AMF",
    "apple": "VideoToolbox",
}

FAMILY_PLAIN = {
    "NVENC": "NVIDIA hardware (NVENC)",
    "QSV": "Intel hardware (Quick Sync)",
    "AMF": "AMD hardware (AMF)",
    "VideoToolbox": "Apple hardware (VideoToolbox)",
}


@dataclass(frozen=True)
class FoundHardware:
    """One vendor the detector listed, in product preference order."""

    family: str
    gpu_name: str = ""


@dataclass(frozen=True)
class HardwareProbe:
    """Cached detector output plus the encoder the real picker chose."""

    found: tuple[FoundHardware, ...] = ()
    encoders: Mapping[str, Optional[str]] = field(default_factory=dict)
    failed: bool = False

    def encoder_for(self, codec: VideoCodec) -> Optional[str]:
        return self.encoders.get(codec.value)


@dataclass(frozen=True)
class HwStatusText:
    """Copy the home card shows. ``state`` is pending, ready, software, or failed."""

    badge: str
    found: str
    using: str
    state: str
    tooltip: str = ""


def family_of(encoder_name: str) -> Optional[str]:
    """Map an FFmpeg encoder name to NVENC, QSV, AMF, or VideoToolbox."""

    lower = (encoder_name or "").lower()
    if "nvenc" in lower:
        return "NVENC"
    if "qsv" in lower:
        return "QSV"
    if "amf" in lower:
        return "AMF"
    if "videotoolbox" in lower:
        return "VideoToolbox"
    return None


def findings_from_info(info) -> tuple[FoundHardware, ...]:
    """Vendors that advertise an encoder, in NVENC → QSV → AMF order.

    ``preferred_hw_encoder`` is ignored. GPU probe order must not decide
    which name is listed first.
    """

    chosen: dict[str, str] = {}
    order: list[str] = []
    for gpu in getattr(info, "gpus", ()) or ():
        vendor = getattr(getattr(gpu, "vendor", None), "value", "")
        if not vendor or vendor in chosen:
            continue
        support = getattr(gpu, "encoder_support", {}) or {}
        if not any(bool(ok) for ok in support.values()):
            continue
        chosen[vendor] = str(getattr(gpu, "name", "") or "")
        order.append(vendor)
    found: list[FoundHardware] = []
    for vendor in ordered_hw_vendors(order):
        family = _VENDOR_FAMILY.get(vendor)
        if not family:
            continue
        found.append(FoundHardware(family, chosen.get(vendor, "")))
    return tuple(found)


def probe_from_compressor(compressor) -> HardwareProbe:
    """Read the detector and the real encoder picker. Does not encode."""

    detector = getattr(compressor, "hw_detector", None)
    select = getattr(compressor, "_select_hw_encoder", None)
    if detector is None or select is None:
        return HardwareProbe(failed=True)
    try:
        info = detector.info
    except Exception:
        logger.debug("Hardware detection failed", exc_info=True)
        return HardwareProbe(failed=True)
    found = findings_from_info(info)
    encoders: dict[str, Optional[str]] = {}
    try:
        for codec in VideoCodec:
            chosen = select(codec)
            encoders[codec.value] = str(chosen) if chosen else None
    except Exception:
        logger.debug("Hardware encoder selection failed", exc_info=True)
        return HardwareProbe(found=found, failed=True)
    return HardwareProbe(found=found, encoders=encoders, failed=False)


def describe_hw_status(
    probe: Optional[HardwareProbe],
    *,
    codec: VideoCodec,
    use_hw: bool,
    force_software: bool,
    action: str = "Quick Compress",
    software_name: str = "",
) -> HwStatusText:
    """Two sentences: what was found, and what this action will encode with."""

    if probe is None:
        return HwStatusText(
            badge="Checking…",
            found="Checking this PC for a hardware encoder.",
            using=f"{action} will show its encoder here when the check finishes.",
            state="pending",
        )
    if probe.failed:
        return HwStatusText(
            badge="Software",
            found="Hardware detection failed.",
            using=f"{action} will use software encoding.",
            state="failed",
            tooltip=software_name,
        )

    found_line = _found_line(probe.found)
    tooltip = software_name
    if force_software:
        return HwStatusText(
            badge="Software",
            found=found_line,
            using=(f"{action} keeps hardware off and will use software encoding."),
            state="software",
            tooltip=tooltip,
        )
    if not use_hw:
        return HwStatusText(
            badge="Software",
            found=found_line,
            using=(
                "Hardware encoding is turned off. "
                f"{action} will use software encoding."
            ),
            state="software",
            tooltip=tooltip,
        )

    encoder = probe.encoder_for(codec)
    family = family_of(encoder or "")
    if encoder and family and not probe.found:
        found_line = f"Found {FAMILY_PLAIN.get(family, family)}."
    if encoder and family:
        using = _hardware_using(action, family, probe.found)
        return HwStatusText(
            badge=family if family != "VideoToolbox" else "Apple",
            found=found_line,
            using=using,
            state="ready",
            tooltip=encoder,
        )
    if probe.found:
        qualifier = (
            "Those hardware encoders are not available for this preset."
            if len(probe.found) > 1
            else "That hardware encoder is not available for this preset."
        )
        return HwStatusText(
            badge="Software",
            found=found_line,
            using=f"{qualifier} {action} will use software encoding.",
            state="software",
            tooltip=tooltip,
        )
    return HwStatusText(
        badge="Software",
        found="No hardware encoder found.",
        using=f"{action} will use software encoding.",
        state="software",
        tooltip=tooltip,
    )


def status_summary(status: HwStatusText) -> str:
    """One block for the settings hint."""

    parts = [status.found.strip(), status.using.strip()]
    return " ".join(part for part in parts if part)


def encoder_result_label(encoder_name: str) -> str:
    """Short result-card label. Software keeps the FFmpeg name in parentheses."""

    family = family_of(encoder_name)
    if family and family in FAMILY_PLAIN:
        return FAMILY_PLAIN[family]
    if encoder_name:
        return f"software ({encoder_name})"
    return ""


def _found_line(found: Sequence[FoundHardware]) -> str:
    if not found:
        return "No hardware encoder found."
    phrases = [_found_phrase(item) for item in found]
    return f"Found {_english_join(phrases)}."


def _found_phrase(item: FoundHardware) -> str:
    name = (item.gpu_name or "").strip()
    if name:
        return f"{name} ({item.family})"
    return FAMILY_PLAIN.get(item.family, item.family)


def _hardware_using(
    action: str,
    family: str,
    found: Sequence[FoundHardware],
) -> str:
    plain = FAMILY_PLAIN.get(family, family)
    sentence = f"{action} will use {plain}."
    if found and found[0].family != family:
        skipped = FAMILY_PLAIN.get(found[0].family, found[0].family)
        sentence = f"{action} will use {plain} instead of {skipped}."
    return sentence


def _english_join(parts: Sequence[str]) -> str:
    items = [part for part in parts if part]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"
