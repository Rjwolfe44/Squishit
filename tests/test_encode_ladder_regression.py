"""Encode-time, size, and SSIM regression for Quick Lite and both Max lanes.

Software paths only. CI has no GPU, and these copies turn hardware off so a
machine that does have one still measures libx264 / libx265 / libsvtav1.
Product presets are not edited here. Quick Lite and HEVC Max still prefer
hardware (NVENC, then QSV, then AMF). Max / Archival stays SVT-AV1 with
hardware off. The two Max lanes are encoded separately.

Missing ``ffmpeg`` skips the module locally and fails it in CI, same as the
other smoke encodes. VMAF is not used. ``ffmpeg``'s ``ssim`` filter on this
short clip is the quality proxy.

Fixture clip
------------
``testsrc2=size=320x180:rate=12:duration=2`` with mild temporal noise, stored
as raw yuv420p and no audio. About two seconds, 24 frames. The pattern is
deterministic on a given FFmpeg build (byte-identical across repeats here).
Audio is disabled on the encode copies so the byte counts follow the video
ladder instead of AAC versus Opus.

Thresholds
----------
Measured with FFmpeg 6.1.1 on 4 cores, then widened. They are soft bounds,
not exact bytes or exact seconds.

================  ========  =========  ======  =================================
Lane              Wall      Size       SSIM    Role of the bound
================  ========  =========  ======  =================================
Quick Lite        ~0.18 s   61_057 B   0.835   <= 8 s; 20k–250k B; SSIM >= 0.75
HEVC Max          ~0.26 s   25_061 B   0.787   <= 12 s; 8k–120k B; SSIM >= 0.68
Max / Archival    ~0.73 s   25_299 B   0.792   <= 25 s; 8k–120k B; SSIM >= 0.68
================  ========  =========  ======  =================================

Ordering, also soft:

* Archival wall time is at least 1.5× Lite, and slower than HEVC Max. That
  catches a collapsed preset (Archival dropped onto a fast preset, or Lite
  switched onto a slow AV1 preset) without pinning a stopwatch.
* Lite's file is at least 1.25× either Max. Quick stays the larger file on
  this clip. The two Max sizes may sit close together; they stay distinct by
  codec, CRF, and preset (HEVC CRF 30 slow versus SVT-AV1 CRF 38 preset 6),
  not by an exact byte gap.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from video_compressor.core.codecs import VideoCodec
from video_compressor.core.compressor import VideoCompressor
from video_compressor.core.profiles import (
    CompressionProfile,
    ProfileManager,
    build_quick_compress_profiles,
)
from video_compressor.core.quality_ladder import ARCHIVAL_PROFILE_NAME

# Two seconds at 320x180 is enough for size and SSIM to settle, and the three
# software encodes together stay under a few seconds on a CI-class machine.
_CLIP_SIZE = "320x180"
_CLIP_RATE = "12"
_CLIP_DURATION = "2"
_CLIP_LAVFI = (
    f"testsrc2=size={_CLIP_SIZE}:rate={_CLIP_RATE}:duration={_CLIP_DURATION},"
    "noise=alls=8:allf=t"
)
_TIMEOUT_SECONDS = 30

# Ceilings are many times the measured ~0.2–0.8 s so a 2-core runner still
# passes, and a hung or accidentally slow preset still fails.
_TIME_CEILING_SECONDS = {
    "lite": 8.0,
    "hevc_max": 12.0,
    "archival": 25.0,
}
_SIZE_BOUNDS_BYTES = {
    "lite": (20_000, 250_000),
    "hevc_max": (8_000, 120_000),
    "archival": (8_000, 120_000),
}
_SSIM_FLOOR = {
    "lite": 0.75,
    "hevc_max": 0.68,
    "archival": 0.68,
}
# Archival preset 6 versus Lite's fast H.264. Measured ratio was about 4×.
_ARCHIVAL_VS_LITE_MIN_TIME_RATIO = 1.5
# Lite versus either Max. Measured ratio was about 2.4×.
_LITE_VS_MAX_MIN_SIZE_RATIO = 1.25

_SSIM_ALL = re.compile(r"All:([0-9]+(?:\.[0-9]+)?)")


def _require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    if os.environ.get("CI"):
        pytest.fail("ffmpeg is required in CI but was not found on PATH")
    pytest.skip("ffmpeg is not on PATH; install it to run the smoke encode")


def _run_ffmpeg(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _require_ffprobe() -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    if os.environ.get("CI"):
        pytest.fail("ffprobe is required in CI but was not found on PATH")
    pytest.skip("ffprobe is not on PATH; install it to run the smoke encode")


def _probe_video_codec(path: Path) -> str:
    ffprobe = _require_ffprobe()
    result = _run_ffmpeg(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    return result.stdout.strip()


def _probe_audio_codecs(path: Path) -> str:
    ffprobe = _require_ffprobe()
    result = _run_ffmpeg(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    return result.stdout.strip()


def _ssim_all(ffmpeg: str, encoded: Path, source: Path) -> float:
    """Mean SSIM from ffmpeg's ssim filter. The score is printed on stderr."""

    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-i",
            str(encoded),
            "-i",
            str(source),
            "-lavfi",
            "ssim",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    matches = _SSIM_ALL.findall(result.stderr)
    assert matches, result.stderr
    return float(matches[-1])


def _software_copy(profile: CompressionProfile) -> CompressionProfile:
    """Copy a product profile onto the software encoder with audio removed.

    Audio stays out of the measurement so a later audio-default change is not
    this test's job. Hardware is off so the sample is the CI software path.
    """

    copy = CompressionProfile.from_dict(profile.to_dict())
    copy.use_hw_accel = False
    copy.disable_audio = True
    return copy


@dataclass(frozen=True)
class LadderCase:
    """One generated clip plus the product profiles and their software copies."""

    ffmpeg: str
    source: Path
    product: dict[str, CompressionProfile]
    software: dict[str, CompressionProfile]


@dataclass(frozen=True)
class EncodeSample:
    """One software encode of the shared clip."""

    key: str
    wall_seconds: float
    size_bytes: int
    ssim: float
    encoder: str
    codec: str
    probed_codec: str
    crf: int
    preset: str


@pytest.fixture(scope="module")
def ladder_case(tmp_path_factory) -> LadderCase:
    """Build the short clip and the Lite / HEVC Max / Archival profile copies."""

    ffmpeg = _require_ffmpeg()
    work = tmp_path_factory.mktemp("encode-ladder")
    source = work / "source.avi"
    _run_ffmpeg(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            _CLIP_LAVFI,
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(source),
        ]
    )

    quick = build_quick_compress_profiles()
    archival = ProfileManager(config_dir=work / "profiles").get_profile(
        ARCHIVAL_PROFILE_NAME
    )
    product = {
        "lite": quick["Quick Lite"],
        "hevc_max": quick["HEVC Max"],
        "archival": archival,
    }
    software = {key: _software_copy(profile) for key, profile in product.items()}
    return LadderCase(
        ffmpeg=ffmpeg,
        source=source,
        product=product,
        software=software,
    )


def _encode(case: LadderCase, key: str, output: Path) -> EncodeSample:
    profile = case.software[key]
    compressor = VideoCompressor()
    started = time.perf_counter()
    result = compressor.compress(case.source, output, profile, job_id=f"ladder-{key}")
    wall_seconds = time.perf_counter() - started

    assert result.success, result.error_message or result.note
    assert result.kept_original is False, result.note
    assert result.attempt_count == 1
    assert result.output_file is not None
    assert result.output_file.is_file()
    assert result.compressed_size == result.output_file.stat().st_size
    assert result.compressed_size < result.original_size
    assert _probe_audio_codecs(result.output_file) == ""

    return EncodeSample(
        key=key,
        wall_seconds=wall_seconds,
        size_bytes=result.compressed_size,
        ssim=_ssim_all(case.ffmpeg, result.output_file, case.source),
        encoder=result.encoder_name or "",
        codec=result.video_codec or "",
        probed_codec=_probe_video_codec(result.output_file),
        crf=int(profile.crf or 0),
        preset=profile.preset,
    )


def _format_samples(samples: dict[str, EncodeSample]) -> str:
    lines = []
    for key in ("lite", "hevc_max", "archival"):
        sample = samples[key]
        lines.append(
            f"{key}: wall={sample.wall_seconds:.3f}s "
            f"size={sample.size_bytes} ssim={sample.ssim:.4f} "
            f"encoder={sample.encoder} codec={sample.probed_codec} "
            f"crf={sample.crf} preset={sample.preset}"
        )
    return "\n".join(lines)


@pytest.mark.smoke
def test_lite_and_dual_max_software_time_size_ssim(ladder_case, tmp_path):
    """Lock soft time, size, and SSIM bounds for Lite and the two Max lanes.

    See the module docstring for the measured numbers and the widened bounds.
    """

    product = ladder_case.product
    assert product["lite"].video_codec is VideoCodec.H264
    assert (product["lite"].crf, product["lite"].preset) == (26, "fast")
    assert product["lite"].use_hw_accel is True

    assert product["hevc_max"].video_codec is VideoCodec.HEVC
    assert (product["hevc_max"].crf, product["hevc_max"].preset) == (30, "slow")
    assert product["hevc_max"].use_hw_accel is True

    assert product["archival"].video_codec is VideoCodec.SVT_AV1
    assert (product["archival"].crf, product["archival"].preset) == (38, "6")
    assert product["archival"].use_hw_accel is False
    assert product["archival"].name != product["hevc_max"].name

    samples = {
        "lite": _encode(ladder_case, "lite", tmp_path / "lite.mp4"),
        "hevc_max": _encode(ladder_case, "hevc_max", tmp_path / "hevc-max.mkv"),
        "archival": _encode(ladder_case, "archival", tmp_path / "archival.mkv"),
    }
    detail = _format_samples(samples)

    assert samples["lite"].encoder == "libx264", detail
    assert samples["lite"].codec == "h264", detail
    assert samples["lite"].probed_codec == "h264", detail
    assert (samples["lite"].crf, samples["lite"].preset) == (26, "fast"), detail

    assert samples["hevc_max"].encoder == "libx265", detail
    assert samples["hevc_max"].codec == "hevc", detail
    assert samples["hevc_max"].probed_codec == "hevc", detail
    assert (samples["hevc_max"].crf, samples["hevc_max"].preset) == (30, "slow"), detail

    assert samples["archival"].encoder == "libsvtav1", detail
    assert samples["archival"].codec == "svt-av1", detail
    assert samples["archival"].probed_codec == "av1", detail
    assert (samples["archival"].crf, samples["archival"].preset) == (38, "6"), detail

    assert samples["hevc_max"].encoder != samples["archival"].encoder, detail
    assert samples["hevc_max"].probed_codec != samples["archival"].probed_codec, detail
    assert (samples["hevc_max"].crf, samples["hevc_max"].preset) != (
        samples["archival"].crf,
        samples["archival"].preset,
    ), detail

    for key, sample in samples.items():
        ceiling = _TIME_CEILING_SECONDS[key]
        low, high = _SIZE_BOUNDS_BYTES[key]
        floor = _SSIM_FLOOR[key]
        assert 0 < sample.wall_seconds <= ceiling, detail
        assert low <= sample.size_bytes <= high, detail
        assert floor <= sample.ssim <= 1.0, detail

    lite = samples["lite"]
    hevc_max = samples["hevc_max"]
    archival = samples["archival"]
    assert (
        archival.wall_seconds >= lite.wall_seconds * _ARCHIVAL_VS_LITE_MIN_TIME_RATIO
    ), detail
    assert archival.wall_seconds > hevc_max.wall_seconds, detail
    assert lite.size_bytes >= int(hevc_max.size_bytes * _LITE_VS_MAX_MIN_SIZE_RATIO), (
        detail
    )
    assert lite.size_bytes >= int(archival.size_bytes * _LITE_VS_MAX_MIN_SIZE_RATIO), (
        detail
    )
