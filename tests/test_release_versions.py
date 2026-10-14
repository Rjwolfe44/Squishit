"""The version lives in four places. A release is only valid when they agree.

CI checks three of them before it publishes a tag; ``APP_VERSION`` is the fourth
and it drives the About text and the update check, so a mismatch there would
keep offering an update the user already has.
"""

from __future__ import annotations

import re
from pathlib import Path

from video_compressor import __version__
from video_compressor.config import APP_VERSION

_ROOT = Path(__file__).resolve().parents[1]


def _first_match(relative: str, pattern: str) -> str:
    text = (_ROOT / relative).read_text(encoding="utf-8")
    match = re.search(pattern, text, re.MULTILINE)
    assert match, f"no version found in {relative}"
    return match.group(1)


def test_every_version_string_agrees():
    pyproject = _first_match("pyproject.toml", r'^version\s*=\s*"([^"]+)"')
    installer = _first_match("tools/squishit.iss", r'^#define MyAppVersion\s+"([^"]+)"')
    assert APP_VERSION == __version__ == pyproject == installer


def test_version_looks_like_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
