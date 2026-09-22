"""Real FFmpeg encode smoke.

Unit tests build argv and mock the encoder process. These tests run short
software encodes so CI proves two things: the installed ``ffmpeg`` binary
works, and ``VideoCompressor`` actually emits the Quick Lite software path
(``libx264``, not HEVC). They skip when ``ffmpeg`` is missing locally. In CI
the binary is required, and a missing install fails the test instead of
skipping. No GPU is required.
"""

import os
import shutil
import subprocess

import pytest

from video_compressor.core.compressor import VideoCompressor
from video_compressor.core.profiles import (
    CompressionProfile,
    build_quick_compress_profiles,
)

# Tiny generated clip: a few frames, software H.264, no GPU.
_ENCODER = "libx264"
_SIZE = "160x120"
_RATE = "15"
_DURATION = "1"
_TIMEOUT_SECONDS = 30


def _require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    if os.environ.get("CI"):
        pytest.fail("ffmpeg is required in CI but was not found on PATH")
    pytest.skip("ffmpeg is not on PATH; install it to run the smoke encode")


@pytest.mark.smoke
def test_short_libx264_encode_writes_nonempty_mp4(tmp_path):
    ffmpeg = _require_ffmpeg()
    output = tmp_path / "smoke.mp4"
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={_SIZE}:rate={_RATE}:duration={_DURATION}",
        "-c:v",
        _ENCODER,
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(output),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.is_file()
    payload = output.read_bytes()
    assert len(payload) > 0
    assert b"ftyp" in payload[:32]


def _run_ffmpeg(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _probe_video_codec(path) -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None and os.environ.get("CI"):
        pytest.fail("ffprobe is required in CI but was not found on PATH")
    if ffprobe is None:
        pytest.skip("ffprobe is not on PATH; install it to run the smoke encode")
    result = subprocess.run(
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
        ],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.smoke
def test_video_compressor_quick_lite_software_encode(tmp_path):
    """Encode a short clip through VideoCompressor on the Quick Lite software path.

    Hardware is turned off so CI does not need a GPU. The profile is otherwise
    the Quick Lite rung: H.264, CRF 26, preset fast, not HEVC.
    """

    ffmpeg = _require_ffmpeg()
    source = tmp_path / "source.avi"
    # Uncompressed noise stays larger than a CRF 26 re-encode, so compress()
    # keeps the new file instead of copying the source back.
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
            f"nullsrc=size={_SIZE}:rate={_RATE}:duration={_DURATION},noise=alls=30:allf=t",
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(source),
        ],
    )

    lite = CompressionProfile.from_dict(
        build_quick_compress_profiles()["Quick Lite"].to_dict()
    )
    lite.use_hw_accel = False
    lite.disable_audio = True

    compressor = VideoCompressor()
    output = tmp_path / "quick-lite.mp4"
    result = compressor.compress(source, output, lite, job_id="quick-lite-smoke")

    assert result.success, result.error_message or result.note
    assert result.kept_original is False, result.note
    assert result.video_codec == "h264"
    assert result.encoder_name == "libx264"
    assert result.output_file is not None
    assert result.output_file.is_file()
    payload = result.output_file.read_bytes()
    assert len(payload) > 0
    assert b"ftyp" in payload[:32]
    assert _probe_video_codec(result.output_file) == "h264"
