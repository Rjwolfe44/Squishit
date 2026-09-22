"""Updater and publish targets stay on this repository's GitHub Releases."""

import re
from pathlib import Path

from video_compressor.core.updater import GITHUB_API_URL, ISSUES_URL, RELEASES_URL

ROOT = Path(__file__).resolve().parents[1]
SCAN_SUFFIXES = {".py", ".md", ".bat", ".yml", ".yaml", ".iss", ".toml", ".sh", ".txt", ".spec"}
# Negative assertions name the retired releases repo and AI publisher.
# Other files must not.
ALLOWED_MENTIONS = {
    "squishit-releases": {
        "tests/test_release_targets.py",
        "tests/test_ui_copy.py",
    },
    "ai_publish": {
        "tests/test_release_targets.py",
    },
}


def test_updater_points_at_this_repository():
    assert GITHUB_API_URL == "https://api.github.com/repos/Rjwolfe44/Squishit/releases/latest"
    assert RELEASES_URL == "https://github.com/Rjwolfe44/Squishit/releases"
    assert ISSUES_URL == "https://github.com/Rjwolfe44/Squishit/issues"


def test_publish_script_targets_this_repository():
    assert not (ROOT / "tools" / "ai_publish.py").exists()
    text = (ROOT / "tools" / "publish_release.py").read_text(encoding="utf-8")
    assert 'OWNER = "Rjwolfe44"' in text
    assert re.search(r'^REPO\s*=\s*"Squishit"\s*$', text, re.M)
    assert "squishit-releases" not in text
    bat = (ROOT / "tools" / "publish_release.bat").read_text(encoding="utf-8")
    assert "publish_release.py" in bat
    assert "ai_publish" not in bat
    about = (ROOT / "video_compressor/gui/about_dialog.py").read_text(encoding="utf-8")
    assert "from ..core.updater import ISSUES_URL, RELEASES_URL" in about


def test_tracked_sources_do_not_name_retired_publish_targets():
    hits = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SCAN_SUFFIXES:
            continue
        relative = path.relative_to(ROOT).as_posix()
        if any(part in {".git", ".venv", "venv", "__pycache__", "dist", "build"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle, allowed in ALLOWED_MENTIONS.items():
            if needle in text and relative not in allowed:
                hits.append(f"{relative}: {needle}")
    assert hits == []
