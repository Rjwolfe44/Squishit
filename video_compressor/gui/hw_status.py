"""Plain-language hardware status for the Qt home and Quick Compress paths.

What was found comes from the hardware detector. What Quick Compress will
use comes from ``VideoCompressor._select_hw_encoder`` (NVENC, then QSV, then
AMF, then software). For Fast AV1 that picker only returns an encoder that
passed a real test encode, and ``VideoCompressor.fast_av1_route`` says what
runs without one (SVT-AV1, libaom, or HEVC). This module does not pick a
different encoder. ``probe_from_compressor`` can run those test encodes, so
callers run it on a worker thread.
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

# "Fast AV1 will use your ..." for a graphics encoder that passed the AV1 check.
FAMILY_CARD = {
    "NVENC": "NVIDIA graphics card",
    "QSV": "Intel graphics",
    "AMF": "AMD graphics card",
    "VideoToolbox": "Apple graphics",
}

_CPU_AV1_NAMES = {
    "libsvtav1": "SVT-AV1",
    "libaom-av1": "libaom, which is slower than SVT-AV1",
}
_FALLBACK_NAMES = {"hevc": "HEVC", "h264": "H.264"}


@dataclass(frozen=True)
class FoundHardware:
    """One vendor the detector listed, in product preference order."""

    family: str
    gpu_name: str = ""


@dataclass(frozen=True)
class HardwareProbe:
    """Cached detector output plus the encoder the real picker chose.

    ``av1_software`` is the CPU encoder Fast AV1 falls back to (``libsvtav1``
    or ``libaom-av1``), ``""`` when this FFmpeg has no AV1 encoder (then
    ``av1_fallback`` names the codec used instead), or None when unknown.
    """

    found: tuple[FoundHardware, ...] = ()
    encoders: Mapping[str, Optional[str]] = field(default_factory=dict)
    failed: bool = False
    av1_software: Optional[str] = None
    av1_fallback: str = ""

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
    """Read the detector and the real encoder picker.

    The AV1 entry runs Fast AV1's hardware test encodes the first time (a
    fraction of a second each, read from the saved result afterwards), so
    call this off the UI thread.
    """

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
    av1_software: Optional[str] = None
    av1_fallback = ""
    route_for = getattr(compressor, "fast_av1_route", None)
    if route_for is not None:
        try:
            route = route_for(use_hw_accel=False)
            av1_software = route.encoder if route.kind == "software" else ""
            av1_fallback = route.fallback_codec
        except Exception:
            logger.debug("Fast AV1 route lookup failed", exc_info=True)
    return HardwareProbe(
        found=found,
        encoders=encoders,
        failed=False,
        av1_software=av1_software,
        av1_fallback=av1_fallback,
    )


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
        using = f"{action} keeps hardware off and will use software encoding."
        if codec == VideoCodec.SVT_AV1:
            using += " SVT-AV1 is best compression on the CPU, not Fast AV1."
        return HwStatusText(
            badge="Software",
            found=found_line,
            using=using,
            state="software",
            tooltip=tooltip,
        )
    if codec == VideoCodec.AV1:
        return _describe_fast_av1(probe, found_line, use_hw=use_hw, software_name=software_name)
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


def _describe_fast_av1(
    probe: HardwareProbe,
    found_line: str,
    *,
    use_hw: bool,
    software_name: str,
) -> HwStatusText:
    """What Fast AV1 will do on this PC, in one plain sentence."""

    encoder = probe.encoder_for(VideoCodec.AV1) if use_hw else None
    family = family_of(encoder or "")
    if encoder and family:
        if not probe.found:
            found_line = f"Found {FAMILY_PLAIN.get(family, family)}."
        return HwStatusText(
            badge=family if family != "VideoToolbox" else "Apple",
            found=found_line,
            using=f"Fast AV1 will use your {FAMILY_CARD.get(family, 'graphics card')}.",
            state="ready",
            tooltip=encoder,
        )
    software = probe.av1_software
    if software is None:
        # A probe from before the route was known: assume the named encoder.
        software = software_name if software_name in _CPU_AV1_NAMES else "libsvtav1"
    if not software:
        fallback = _FALLBACK_NAMES.get(probe.av1_fallback, "HEVC")
        return HwStatusText(
            badge=fallback,
            found=found_line,
            using=f"This copy of FFmpeg has no AV1 encoder; {fallback} will be used instead.",
            state="software",
            tooltip="",
        )
    cpu = _CPU_AV1_NAMES.get(software, software)
    if use_hw and probe.found:
        # The card was found (it may still encode H.264/HEVC), but its AV1
        # test encode did not pass on this PC.
        card = (
            FAMILY_CARD.get(probe.found[0].family, "graphics card")
            if len(probe.found) == 1
            else "graphics hardware"
        )
        using = (
            f"Your {card} did not pass the AV1 test, so Fast AV1 will use your "
            f"processor ({cpu})."
        )
    elif use_hw:
        using = (
            "No AV1 graphics hardware was found, so Fast AV1 will use your "
            f"processor ({cpu})."
        )
    else:
        using = (
            "Hardware encoding is turned off, so Fast AV1 will use your "
            f"processor ({cpu})."
        )
    return HwStatusText(
        badge="Software",
        found=found_line,
        using=using,
        state="software",
        tooltip=software,
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
