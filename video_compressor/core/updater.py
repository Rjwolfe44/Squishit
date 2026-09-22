"""Auto-update support via GitHub Releases.

Checks https://github.com/Rjwolfe44/squishit-releases for the latest release,
downloads the setup installer, and runs it silently to update the app.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib import request
from urllib.error import URLError

logger = logging.getLogger(__name__)

GITHUB_API_URL = (
    "https://api.github.com/repos/Rjwolfe44/squishit-releases/releases/latest"
)
_REQUEST_TIMEOUT = 8  # seconds


@dataclass
class UpdateInfo:
    """Describes an available update."""

    latest_version: str
    download_url: str
    release_notes: str


def _version_gt(a: str, b: str) -> bool:
    """Return True if version string *a* is newer than *b* (semver, numeric only)."""
    def _parts(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.lstrip("v").split("."))
        except ValueError:
            return (0,)

    return _parts(a) > _parts(b)


def check_for_update(current_version: str, force: bool = False) -> Optional[UpdateInfo]:
    """
    Query GitHub Releases and return an UpdateInfo if a newer version exists.

    When *force* is True the 24-hour throttle is skipped (manual check).
    Returns None on network errors or when already up-to-date.
    """
    if not force:
        # Honour the 24-hour throttle for background checks.
        from ..config import get_config_manager
        from datetime import datetime, timezone, timedelta

        cfg = get_config_manager().config
        if cfg.last_update_check:
            try:
                last = datetime.fromisoformat(cfg.last_update_check)
                if datetime.now(timezone.utc) - last < timedelta(hours=24):
                    return None
            except ValueError:
                pass

    try:
        req = request.Request(
            GITHUB_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "SquishIt-Updater",
            },
        )
        with request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data: dict = json.loads(resp.read().decode())
    except (URLError, OSError, json.JSONDecodeError) as exc:
        logger.debug("Update check failed: %s", exc)
        return None

    tag: str = data.get("tag_name", "").lstrip("v")
    if not tag:
        return None

    if not _version_gt(tag, current_version):
        logger.debug("Already up-to-date (%s >= %s)", current_version, tag)
        return None

    # Find the first .exe asset (the Inno Setup installer)
    assets: list[dict] = data.get("assets", [])
    exe_asset = next(
        (a for a in assets if a.get("name", "").lower().endswith(".exe")), None
    )
    if not exe_asset:
        logger.debug("Release %s has no .exe asset", tag)
        return None

    # Record the check timestamp so the throttle works for the next call.
    try:
        from datetime import datetime, timezone
        from ..config import get_config_manager
        get_config_manager().update_config(
            last_update_check=datetime.now(timezone.utc).isoformat()
        )
    except Exception:
        pass

    return UpdateInfo(
        latest_version=tag,
        download_url=exe_asset["browser_download_url"],
        release_notes=data.get("body", ""),
    )


def download_update(
    url: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> Optional[Path]:
    """
    Download the installer to a temp file and return its path.

    *progress_cb* is called with (bytes_done, total_bytes).
    Returns None if the download fails.
    """
    try:
        req = request.Request(url, headers={"User-Agent": "SquishIt-Updater"})
        with request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            suffix = Path(url).suffix or ".exe"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="squishit_update_")
            done = 0
            chunk = 65536
            try:
                with os.fdopen(fd, "wb") as fh:
                    while True:
                        buf = resp.read(chunk)
                        if not buf:
                            break
                        fh.write(buf)
                        done += len(buf)
                        if progress_cb and total:
                            progress_cb(done, total)
            except Exception:
                os.unlink(tmp_path)
                raise
        return Path(tmp_path)
    except (URLError, OSError) as exc:
        logger.error("Update download failed: %s", exc)
        return None
