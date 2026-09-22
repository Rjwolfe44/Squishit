"""
tools/ai_publish.py — Non-interactive release publisher for AI use.

GitHub Copilot calls this directly from the terminal. No prompts, no editors.
All args are required. Exits 0 on success, 1 on any failure.

Usage:
    python tools/ai_publish.py --version X.Y.Z --notes "Markdown release notes"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OWNER = "Rjwolfe44"
REPO  = "squishit-releases"
API   = "https://api.github.com"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _die(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _load_token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        for p in (PROJECT_ROOT / ".github_token",
                  PROJECT_ROOT / "tools" / ".github_token"):
            if p.exists():
                token = p.read_text(encoding="utf-8").strip()
                break
    if not token:
        _die("No GitHub token. Set GH_TOKEN env var or create tools/.github_token")
    return token


def _read_package_version() -> str:
    init_path = PROJECT_ROOT / "video_compressor" / "__init__.py"
    for line in init_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    _die("Could not read __version__ from video_compressor/__init__.py")


def _read_pyproject_version() -> str:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = str(data.get("project", {}).get("version", "")).strip()
    if not version:
        _die("Could not read version from pyproject.toml")
    return version


def _read_installer_version() -> str:
    iss_path = PROJECT_ROOT / "tools" / "squishit.iss"
    match = re.search(r'#define\s+MyAppVersion\s+"([^"]+)"', iss_path.read_text(encoding="utf-8"))
    if not match:
        _die("Could not read MyAppVersion from tools/squishit.iss")
    return match.group(1).strip()


def _validate_version_alignment(version: str) -> None:
    versions = {
        "video_compressor/__init__.py": _read_package_version(),
        "pyproject.toml": _read_pyproject_version(),
        "tools/squishit.iss": _read_installer_version(),
    }
    mismatched = {path: value for path, value in versions.items() if value != version}
    if mismatched:
        details = "\n".join(f"  {path}: {value}" for path, value in versions.items())
        _die(
            f"Version mismatch detected. Refusing to publish {version}.\n\n{details}\n\n"
            "Update the mismatched files first so the app, package metadata, and installer stay in sync."
        )


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SquishIt-AI-Publisher/1.0",
    }


def _get(url: str, token: str):
    req = request.Request(url, headers=_headers(token))
    try:
        with request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except HTTPError as e:
        if e.code == 404:
            return {}
        _die(f"GET {url} → {e.code}: {e.read().decode(errors='replace')[:300]}")
    except URLError as e:
        _die(f"Network error: {e.reason}")


def _post_json(url: str, token: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = request.Request(url, data=data,
                          headers={**_headers(token), "Content-Type": "application/json"},
                          method="POST")
    try:
        with request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except HTTPError as e:
        _die(f"POST {url} → {e.code}: {e.read().decode(errors='replace')[:400]}")
    except URLError as e:
        _die(f"Network error: {e.reason}")


def _post_binary(url: str, token: str, data: bytes) -> dict:
    req = request.Request(url, data=data,
                          headers={**_headers(token), "Content-Type": "application/octet-stream"},
                          method="POST")
    try:
        with request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except HTTPError as e:
        _die(f"Asset upload → {e.code}: {e.read().decode(errors='replace')[:400]}")
    except URLError as e:
        _die(f"Upload network error: {e.reason}")


def _put_json(url: str, token: str, payload: dict) -> None:
    data = json.dumps(payload).encode()
    req = request.Request(url, data=data,
                          headers={**_headers(token), "Content-Type": "application/json"},
                          method="PUT")
    try:
        with request.urlopen(req, timeout=30) as r:
            r.read()
    except HTTPError as e:
        _die(f"PUT {url} → {e.code}: {e.read().decode(errors='replace')[:300]}")
    except URLError as e:
        _die(f"Network error: {e.reason}")


# ── README builder ────────────────────────────────────────────────────────────

def _exe_asset(rel: dict):
    return next((a for a in rel.get("assets", [])
                 if a["name"].lower().endswith(".exe")), None)


def _build_readme(releases: list) -> str:
    lines = [
        "# SquishIt — Releases",
        "",
        "Power-user desktop media compression for Windows.  ",
        "Modern codecs (AV1, HEVC, H.264) · Hardware acceleration · Explorer right-click integration",
        "",
        "---",
        "",
    ]

    if releases:
        latest = releases[0]
        exe = _exe_asset(latest)
        dl_url  = exe["browser_download_url"] if exe else latest["html_url"]
        dl_name = exe["name"] if exe else latest["tag_name"]
        pub     = (latest.get("published_at") or "")[:10]
        body    = (latest.get("body") or "").strip()

        lines += [
            "## ⬇ Latest Download",
            "",
            f"**[{dl_name}]({dl_url})**"
            f" &nbsp;·&nbsp; {latest['tag_name']}"
            f" &nbsp;·&nbsp; Released {pub}",
            "",
        ]
        if body:
            preview = [ln for ln in body.splitlines() if ln.strip()][:4]
            lines.append("> " + "  \n> ".join(preview))
            lines.append("")

    lines += ["---", "", "## All Releases", ""]
    lines.append("| Version | Released | Download |")
    lines.append("|---------|----------|----------|")
    for rel in releases:
        tag  = rel["tag_name"]
        pub  = (rel.get("published_at") or "")[:10]
        exe  = _exe_asset(rel)
        link = (f"[{exe['name']}]({exe['browser_download_url']})"
                if exe else f"[Release page]({rel['html_url']})")
        lines.append(f"| **{tag}** | {pub} | {link} |")

    lines += ["", "---", "", "## Release Notes", ""]
    for rel in releases:
        tag   = rel["tag_name"]
        pub   = (rel.get("published_at") or "")[:10]
        notes = (rel.get("body") or "").strip()
        lines += [
            f"### {tag} — {pub}",
            "",
            notes or "*(No release notes provided.)*",
            "",
        ]

    lines += [
        "---", "",
        "## System Requirements", "",
        "- Windows 10 / 11 (64-bit)",
        "- ~50 MB disk space",
        "- No additional runtime or dependencies required",
        "",
        "## Installation", "",
        "1. Download the setup installer above.",
        "2. Run it — no elevated privileges required for a per-user install.",
        "3. During setup, optionally enable the Explorer right-click context menu.",
        "",
        "---", "",
        "*This repository contains only release builds. Source code is private.*",
    ]
    return "\n".join(lines) + "\n"


def _update_readme(token: str) -> None:
    all_releases = _get(f"{API}/repos/{OWNER}/{REPO}/releases?per_page=100", token)
    if not isinstance(all_releases, list):
        all_releases = []
    content = _build_readme(all_releases)
    url      = f"{API}/repos/{OWNER}/{REPO}/contents/README.md"
    existing = _get(url, token)
    payload  = {
        "message": "docs: regenerate release list",
        "content": base64.b64encode(content.encode()).decode(),
        "branch":  "main",
    }
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]
    _put_json(url, token, payload)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version",   required=True, metavar="X.Y.Z")
    ap.add_argument("--notes",     required=True, metavar="TEXT",
                    help="Release notes (Markdown)")
    ap.add_argument("--installer", metavar="PATH",
                    help="Path to .exe installer (default: dist/SquishIt-Setup-vX.Y.Z.exe)")
    ap.add_argument("--draft",     action="store_true")
    args = ap.parse_args()

    token   = _load_token()
    version = args.version.lstrip("v")
    _validate_version_alignment(version)
    tag     = f"v{version}"

    installer = Path(args.installer) if args.installer else (
        PROJECT_ROOT / "dist" / f"SquishIt-Setup-v{version}.exe"
    )
    if not installer.exists():
        _die(f"Installer not found: {installer}\nRun tools\\make_installer.bat first.")

    size_mb = installer.stat().st_size / 1_048_576
    print(f"Publishing {tag}  ({installer.name}, {size_mb:.1f} MB)")

    # Guard: no duplicate tags
    existing = _get(f"{API}/repos/{OWNER}/{REPO}/releases/tags/{tag}", token)
    if existing:
        _die(f"Release {tag} already exists: {existing.get('html_url', '')}")

    # Create release
    release = _post_json(f"{API}/repos/{OWNER}/{REPO}/releases", token, {
        "tag_name":   tag,
        "name":       f"SquishIt {tag}",
        "body":       args.notes,
        "draft":      args.draft,
        "prerelease": False,
    })
    print(f"RELEASE_URL={release.get('html_url', '')}")

    # Upload asset
    upload_url = release["upload_url"].split("{")[0] + f"?name={quote(installer.name)}"
    asset = _post_binary(upload_url, token, installer.read_bytes())
    print(f"DOWNLOAD_URL={asset.get('browser_download_url', '')}")

    # Regenerate README
    _update_readme(token)
    print("README_UPDATED=true")
    print("OK")


if __name__ == "__main__":
    main()
