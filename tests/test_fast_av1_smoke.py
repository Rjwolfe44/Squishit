"""Real FFmpeg smoke for every Fast AV1 path this FFmpeg has.

Each test encodes about a second of generated video through
``VideoCompressor`` and checks the file is AV1 (or HEVC for the no-AV1
fallback). A path skips when its encoder is missing: the repo's
``vendor\\ffmpeg`` "essentials" build has libaom and the hardware encoders but
no libsvtav1, so point ``SQUISHIT_SMOKE_FFMPEG`` at a full build (the
installed app's ``_internal\\vendor\\ffmpeg\\ffmpeg.exe``) to run the SVT-AV1
test locally. CI's apt FFmpeg usually has libsvtav1 and libaom. The hardware
test runs the real AV1 check and skips when no GPU encoder passes it.

Without ``SQUISHIT_SMOKE_FFMPEG`` the FFmpeg is the one ``VideoCompressor``
finds on its own. A missing FFmpeg skips locally and fails in CI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from video_compressor.core import fast_av1
from video_compressor.core.codecs import CodecManager, VideoCodec
from video_compressor.core.compressor import VideoCompressor
from video_compressor.core.profiles import CompressionProfile, ProfileType
from video_compressor.core.quality_ladder import QualityRung, get_step

pytestmark = pytest.mark.smoke

_TIMEOUT_SECONDS = 60


def _app_ffmpeg() -> str:
    override = os.environ.get("SQUISHIT_SMOKE_FFMPEG", "").strip()
    if override:
        if not Path(override).is_file():
            pytest.fail(f"SQUISHIT_SMOKE_FFMPEG points at a missing file: {override}")
        return override
    found = VideoCompressor._find_ffmpeg(None)  # the app's own search: vendor, then PATH
    if Path(found).is_file() or shutil.which(found):
        return found
    if os.environ.get("CI"):
        pytest.fail("ffmpeg is required in CI but was not found")
    pytest.skip("ffmpeg was not found; install it to run the Fast AV1 smoke encodes")


def _ffprobe_next_to(ffmpeg: str) -> str:
    path = Path(ffmpeg)
    sibling = path.with_name(path.name.replace("ffmpeg", "ffprobe"))
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ffprobe") or pytest.skip("ffprobe was not found")


@pytest.fixture(scope="module")
def smoke_ffmpeg() -> str:
    return _app_ffmpeg()


@pytest.fixture(scope="module")
def smoke_encoders(smoke_ffmpeg) -> set:
    return set(CodecManager(smoke_ffmpeg).available_encoders)


@pytest.fixture(scope="module")
def smoke_source(smoke_ffmpeg, tmp_path_factory) -> Path:
    """One second of 640x360 noise, raw, so any AV1 encode is smaller."""

    source = tmp_path_factory.mktemp("fast-av1-smoke") / "source.avi"
    result = subprocess.run(
        [
            smoke_ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=15:duration=1,noise=alls=20:allf=t",
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return source


def _compressor(monkeypatch, ffmpeg: str, *, encoders=None, probe=None) -> VideoCompressor:
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: ffmpeg)
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: _ffprobe_next_to(ffmpeg))
    compressor = VideoCompressor(av1_probe=probe)
    if encoders is not None:
        compressor.codec_manager._available_encoders = set(encoders)
    return compressor


def _profile(*, use_hw: bool) -> CompressionProfile:
    step = get_step(VideoCodec.AV1, QualityRung.QUICK)
    return CompressionProfile(
        name="Fast AV1 smoke",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.AV1,
        crf=step.crf,
        preset=step.preset,
        video_container="mkv",
        use_hw_accel=use_hw,
        disable_audio=True,
    )


def _probe_codec(ffmpeg: str, path: Path) -> str:
    result = subprocess.run(
        [
            _ffprobe_next_to(ffmpeg),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _encode(compressor, source: Path, output: Path, profile: CompressionProfile):
    result = compressor.compress(source, output, profile, job_id=output.stem)
    assert result.success, result.error_message or result.note
    assert result.output_file is not None and result.output_file.is_file()
    assert result.output_file.stat().st_size > 0
    assert result.kept_original is False, result.note
    return result


def test_fast_av1_on_svt_av1(monkeypatch, tmp_path, smoke_ffmpeg, smoke_encoders, smoke_source):
    if "libsvtav1" not in smoke_encoders:
        pytest.skip("this FFmpeg has no libsvtav1 (set SQUISHIT_SMOKE_FFMPEG to a full build)")
    compressor = _compressor(monkeypatch, smoke_ffmpeg)
    result = _encode(compressor, smoke_source, tmp_path / "svt.mkv", _profile(use_hw=False))
    assert result.encoder_name == "libsvtav1"
    assert result.video_codec == "av1"
    assert _probe_codec(smoke_ffmpeg, result.output_file) == "av1"


def test_fast_av1_on_libaom_when_svt_is_missing(monkeypatch, tmp_path, smoke_ffmpeg, smoke_encoders, smoke_source):
    if "libaom-av1" not in smoke_encoders:
        pytest.skip("this FFmpeg has no libaom-av1")
    compressor = _compressor(monkeypatch, smoke_ffmpeg, encoders=smoke_encoders - {"libsvtav1"})
    result = _encode(compressor, smoke_source, tmp_path / "aom.mkv", _profile(use_hw=False))
    assert result.encoder_name == "libaom-av1"
    assert result.video_codec == "av1"
    assert _probe_codec(smoke_ffmpeg, result.output_file) == "av1"


def test_fast_av1_without_any_av1_encoder_uses_hevc(monkeypatch, tmp_path, smoke_ffmpeg, smoke_encoders, smoke_source):
    if "libx265" not in smoke_encoders:
        pytest.skip("this FFmpeg has no libx265")
    no_av1 = {name for name in smoke_encoders if "av1" not in name}
    compressor = _compressor(monkeypatch, smoke_ffmpeg, encoders=no_av1)
    # Hardware off so a PC with hevc_amf/hevc_nvenc still lands on libx265.
    result = _encode(compressor, smoke_source, tmp_path / "fallback.mkv", _profile(use_hw=False))
    assert result.encoder_name == "libx265"
    assert result.video_codec == "hevc"
    assert "no AV1 encoder" in result.note
    assert _probe_codec(smoke_ffmpeg, result.output_file) == "hevc"


def test_fast_av1_on_verified_hardware(monkeypatch, tmp_path, smoke_ffmpeg, smoke_encoders, smoke_source):
    listed = [name for name in fast_av1.HW_AV1_ENCODERS if name in smoke_encoders]
    if not listed:
        pytest.skip("this FFmpeg lists no hardware AV1 encoder")
    probe = fast_av1.HardwareAv1Probe(smoke_ffmpeg, runner=fast_av1.subprocess_runner, cache_path=None)
    working = probe.first_working(listed)
    for name in listed:
        result = probe.peek(name)
        if result is not None:
            # A failing encoder must fail fast, not ride out the timeout.
            assert result.reason in (fast_av1.REASON_OK, fast_av1.REASON_FAILED), result
    if working is None:
        pytest.skip("no hardware AV1 encoder passed the test encode on this PC")
    compressor = _compressor(monkeypatch, smoke_ffmpeg, probe=probe)
    result = _encode(compressor, smoke_source, tmp_path / "hw.mkv", _profile(use_hw=True))
    assert result.encoder_name == working
    assert _probe_codec(smoke_ffmpeg, result.output_file) == "av1"


def test_real_probe_rejects_an_encoder_quickly(smoke_ffmpeg):
    probe = fast_av1.HardwareAv1Probe(smoke_ffmpeg, runner=fast_av1.subprocess_runner, cache_path=None)
    result = probe.check("av1_does_not_exist")
    assert result.ok is False
    assert result.reason == fast_av1.REASON_FAILED
    assert result.seconds < fast_av1.PROBE_TIMEOUT_SECONDS
