"""Validate FFmpeg binaries and required encoders for SquishIt."""

from __future__ import annotations

import shutil
import subprocess
import sys

REQUIRED_ENCODERS = [
    "libx264",
    "libx265",
    "libaom-av1",
    "libsvtav1",
    "libvpx-vp9",
    "aac",
    "libopus",
]


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("FFmpeg or FFprobe is missing from PATH")
        return 1

    result = subprocess.run([ffmpeg, "-encoders", "-hide_banner"], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr.strip() or "Failed to query FFmpeg encoders")
        return result.returncode

    output = result.stdout
    missing = [encoder for encoder in REQUIRED_ENCODERS if encoder not in output]
    print(f"ffmpeg: {ffmpeg}")
    print(f"ffprobe: {ffprobe}")
    if missing:
        print("Missing encoders:")
        for encoder in missing:
            print(f"  - {encoder}")
        return 2

    print("All required encoders are available.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
