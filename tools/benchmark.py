"""Basic encoder benchmark helper for SquishIt."""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

SAMPLE_DURATION = 5
CODECS = ["libx264", "libx265", "libsvtav1", "libaom-av1", "libvpx-vp9"]


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found")
        return 1

    out_dir = Path("benchmark_output")
    out_dir.mkdir(exist_ok=True)
    source = out_dir / "benchmark_source.mp4"

    generate = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=1920x1080:rate=30:duration={SAMPLE_DURATION}",
        str(source),
    ]
    if subprocess.run(generate, capture_output=True).returncode != 0:
        print("failed to generate benchmark source")
        return 2

    for codec in CODECS:
        target = out_dir / f"{codec}.mkv"
        start = time.perf_counter()
        result = subprocess.run([
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-c:v",
            codec,
            "-an",
            str(target),
        ], capture_output=True, text=True)
        elapsed = time.perf_counter() - start
        if result.returncode == 0:
            print(f"{codec:<12} {elapsed:>7.2f}s")
        else:
            print(f"{codec:<12} FAILED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
