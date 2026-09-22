"""Real FFmpeg encode smoke.

The rest of the suite builds argv and mocks the encoder process. This test
runs a short software encode so CI proves the installed ``ffmpeg`` binary
actually works. It skips when ``ffmpeg`` is missing locally. In CI the binary
is required, and a missing install fails the test instead of skipping.
"""

import os
import shutil
import subprocess

import pytest

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
