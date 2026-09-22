#!/usr/bin/env python3
"""
tools/publish_release.py — Publish a new SquishIt release to GitHub.

What it does
────────────
  1. Reads the current version from video_compressor/__init__.py
  2. Finds the built installer in dist/
  3. Creates a new release on Rjwolfe44/squishit-releases with your release notes
  4. Uploads the installer .exe as a downloadable asset
  5. Regenerates the releases-repo README.md with a full version history table

Usage
─────
  python tools/publish_release.py
  python tools/publish_release.py -n "What changed in this release"
  python tools/publish_release.py -f CHANGELOG.md
  python tools/publish_release.py --draft

  If you don't pass -n or -f, Notepad opens so you can write release notes.

Authentication
──────────────
  Option 1 (recommended): set environment variable
      GH_TOKEN=ghp_xxxxxxxxxxxxxxxx

  Option 2: create a file named  .github_token  in the project root
      containing only your Personal Access Token on one line.

  Create a token at: https://github.com/settings/tokens/new?scopes=public_repo
  (Only "public_repo" scope is needed — the releases repo is public.)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, NoReturn, Optional
from urllib import request
from urllib.error import HTTPError, URLError
from urllib.parse import quote

# ── Constants ────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OWNER = "Rjwolfe44"
REPO = "squishit-releases"
API = "https://api.github.com"

# ── Rich (optional, already a project dep) ───────────────────────────────────

try:
    from rich.console import Console
    from rich.panel import Panel

    _con = Console()
    _RICH = True
except ImportError:
    _con = None  # type: ignore[assignment]
    _RICH = False


# ── Terminal helpers ──────────────────────────────────────────────────────────


def _die(msg: str, code: int = 1) -> NoReturn:
    if _RICH:
        _con.print(f"\n[bold red]✗  Error:[/bold red] {msg}\n")
    else:
        print(f"\nError: {msg}\n", file=sys.stderr)
    sys.exit(code)


def _info(msg: str) -> None:
    if _RICH:
        _con.print(f"  [dim cyan]→[/dim cyan] {msg}")
    else:
        print(f"  → {msg}")


def _ok(msg: str) -> None:
    if _RICH:
        _con.print(f"  [bold green]✓[/bold green] {msg}")
    else:
        print(f"  ✓ {msg}")


# ── Token loading ─────────────────────────────────────────────────────────────


def _load_token(cli_token: Optional[str]) -> str:
    token = (
        cli_token
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if not token:
        # Check both the project root and the tools/ subfolder
        for candidate in (
            PROJECT_ROOT / ".github_token",
            PROJECT_ROOT / "tools" / ".github_token",
        ):
            if candidate.exists():
                token = candidate.read_text(encoding="utf-8").strip()
                break
    if not token:
        _die(
            "No GitHub token found.\n\n"
            "  Option 1 (recommended): set the GH_TOKEN environment variable\n"
            "    GH_TOKEN=ghp_...\n\n"
            "  Option 2: create a file  .github_token  in the project root\n"
            "    containing only your Personal Access Token on one line.\n\n"
            "  Create a token:  https://github.com/settings/tokens/new?scopes=public_repo"
        )
    return token  # type: ignore[return-value]


# ── HTTP helpers ──────────────────────────────────────────────────────────────


def _base_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "SquishIt-Publisher/1.0",
    }


def _gh_get(url: str, token: str) -> Any:
    """GET a GitHub API URL. Returns parsed JSON (dict or list), or {} on 404."""
    req = request.Request(url, headers=_base_headers(token))
    try:
        with request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        if exc.code == 404:
            return {}
        body = exc.read().decode(errors="replace")
        _die(f"GET {url}\n  → HTTP {exc.code}: {body[:400]}")
    except URLError as exc:
        _die(f"Network error: {exc.reason}")


def _gh_post_json(url: str, token: str, payload: dict) -> dict:
    """POST JSON to a GitHub API URL and return the parsed response."""
    data = json.dumps(payload).encode()
    headers = {**_base_headers(token), "Content-Type": "application/json"}
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")
        _die(f"POST {url}\n  → HTTP {exc.code}: {body[:500]}")
    except URLError as exc:
        _die(f"Network error: {exc.reason}")


def _gh_post_binary(url: str, token: str, data: bytes) -> dict:
    """Upload raw bytes (asset upload) and return the parsed response."""
    headers = {
        **_base_headers(token),
        "Content-Type": "application/octet-stream",
    }
    req = request.Request(url, data=data, headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")
        _die(f"Asset upload failed → HTTP {exc.code}: {body[:400]}")
    except URLError as exc:
        _die(f"Network error during upload: {exc.reason}")


def _gh_put_json(url: str, token: str, payload: dict) -> None:
    """PUT JSON to a GitHub API URL (used for file updates)."""
    data = json.dumps(payload).encode()
    headers = {**_base_headers(token), "Content-Type": "application/json"}
    req = request.Request(url, data=data, headers=headers, method="PUT")
    try:
        with request.urlopen(req, timeout=30) as resp:
            resp.read()
    except HTTPError as exc:
        body = exc.read().decode(errors="replace")
        _die(f"PUT {url}\n  → HTTP {exc.code}: {body[:500]}")
    except URLError as exc:
        _die(f"Network error: {exc.reason}")


# ── Version / installer auto-detection ──────────────────────────────────────


def _detect_version() -> str:
    init = PROJECT_ROOT / "video_compressor" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
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
        "video_compressor/__init__.py": _detect_version(),
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


def _detect_installer(version: str) -> Path:
    path = PROJECT_ROOT / "dist" / f"SquishIt-Setup-v{version}.exe"
    if not path.exists():
        _die(
            f"Installer not found at:\n  {path}\n\n"
            "Run  tools\\make_installer.bat  first, or pass  --installer PATH."
        )
    return path


# ── Release notes via Notepad ────────────────────────────────────────────────


def _notes_from_editor(tag: str) -> str:
    """Open Notepad so the user can type release notes. Returns stripped content."""
    template = (
        f"# Release Notes — SquishIt {tag}\n"
        "# Lines starting with '#' are stripped.\n"
        "# Save and close Notepad when you're done.\n\n"
        "## What's New\n\n"
        "- \n\n"
        "## Bug Fixes\n\n"
        "- \n"
    )
    fd, tmp = tempfile.mkstemp(suffix=".md", prefix="squishit_notes_")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(template)
        subprocess.run(["notepad.exe", str(tmp_path)], check=True)
        raw = tmp_path.read_text(encoding="utf-8")
        lines = [ln for ln in raw.splitlines() if not ln.startswith("#")]
        return "\n".join(lines).strip()
    finally:
        tmp_path.unlink(missing_ok=True)


# ── README generator ──────────────────────────────────────────────────────────


def _exe_asset(rel: dict) -> Optional[dict]:
    return next(
        (a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")),
        None,
    )


def _build_readme(releases: list) -> str:
    lines: list[str] = [
        "# SquishIt — Releases",
        "",
        "Power-user desktop media compression for Windows.  ",
        "Modern codecs (AV1, HEVC, H.264) · Hardware acceleration · Explorer right-click integration",
        "",
        "---",
        "",
    ]

    if releases:
        latest = releases[0]  # GitHub returns newest-first
        exe = _exe_asset(latest)
        dl_url = exe["browser_download_url"] if exe else latest["html_url"]
        dl_name = exe["name"] if exe else latest["tag_name"]
        pub_date = (latest.get("published_at") or "")[:10]
        notes_preview = (latest.get("body") or "").strip()

        lines += [
            "## ⬇ Latest Download",
            "",
            f"**[{dl_name}]({dl_url})**"
            f" &nbsp;·&nbsp; {latest['tag_name']}"
            f" &nbsp;·&nbsp; Released {pub_date}",
            "",
        ]
        if notes_preview:
            # Show first 4 non-empty lines as a block-quote preview
            preview = [ln for ln in notes_preview.splitlines() if ln.strip()][:4]
            lines.append("> " + "  \n> ".join(preview))
            lines.append("")

    # ── All releases table ────────────────────────────────────────────────────
    lines += ["---", "", "## All Releases", ""]
    lines.append("| Version | Released | Download |")
    lines.append("|---------|----------|----------|")
    for rel in releases:
        tag = rel["tag_name"]
        pub = (rel.get("published_at") or "")[:10]
        exe = _exe_asset(rel)
        if exe:
            link = f"[{exe['name']}]({exe['browser_download_url']})"
        else:
            link = f"[Release page]({rel['html_url']})"
        draft_flag = " *(draft)*" if rel.get("draft") else ""
        lines.append(f"| **{tag}**{draft_flag} | {pub} | {link} |")

    # ── Per-release notes ─────────────────────────────────────────────────────
    lines += ["", "---", "", "## Release Notes", ""]
    for rel in releases:
        tag = rel["tag_name"]
        pub = (rel.get("published_at") or "")[:10]
        notes = (rel.get("body") or "").strip()
        lines += [
            f"### {tag} — {pub}",
            "",
            notes if notes else "*(No release notes provided.)*",
            "",
        ]

    # ── Footer ────────────────────────────────────────────────────────────────
    lines += [
        "---",
        "",
        "## System Requirements",
        "",
        "- Windows 10 / 11 (64-bit)",
        "- ~50 MB disk space",
        "- No additional runtime or dependencies required",
        "",
        "## Installation",
        "",
        "1. Download the setup installer above.",
        "2. Run it — no elevated privileges required for a per-user install.",
        "3. During setup, optionally enable the Explorer right-click context menu.",
        "",
        "---",
        "",
        "*This repository contains only release builds. Source code is private.*",
    ]

    return "\n".join(lines) + "\n"


# ── README update ─────────────────────────────────────────────────────────────


def _update_readme(token: str, content: str) -> None:
    """Create or update README.md on the releases repo."""
    url = f"{API}/repos/{OWNER}/{REPO}/contents/README.md"
    existing = _gh_get(url, token)
    payload: dict = {
        "message": "docs: regenerate release list",
        "content": base64.b64encode(content.encode("utf-8")).decode(),
    }
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]
    _gh_put_json(url, token, payload)


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", metavar="X.Y.Z",
                    help="Version to release (default: auto-detected from __init__.py)")
    ap.add_argument("-n", "--notes", metavar="TEXT",
                    help="Release notes as a string (Markdown supported)")
    ap.add_argument("-f", "--notes-file", metavar="PATH",
                    help="Read release notes from a Markdown file")
    ap.add_argument("--installer", metavar="PATH",
                    help="Path to the .exe installer (default: dist/SquishIt-Setup-vX.Y.Z.exe)")
    ap.add_argument("--draft", action="store_true",
                    help="Create release as a draft (not publicly visible yet)")
    ap.add_argument("--token", metavar="TOKEN",
                    help="GitHub PAT (default: GH_TOKEN env var or .github_token file)")
    args = ap.parse_args()

    if _RICH:
        _con.print("\n[bold]SquishIt Release Publisher[/bold]\n")
    else:
        print("\nSquishIt Release Publisher\n")

    # ── Auth ──────────────────────────────────────────────────────────────────
    token = _load_token(args.token)

    # ── Version + installer ───────────────────────────────────────────────────
    version = args.version or _detect_version()
    _validate_version_alignment(version)
    tag = f"v{version}"
    installer = Path(args.installer) if args.installer else _detect_installer(version)
    size_mb = installer.stat().st_size / 1_048_576

    _info(f"Version    : {tag}")
    _info(f"Installer  : {installer.name}  ({size_mb:.1f} MB)")
    if args.draft:
        _info("Mode       : DRAFT (not publicly visible until you publish it on GitHub)")

    # ── Release notes ─────────────────────────────────────────────────────────
    if args.notes_file:
        notes = Path(args.notes_file).read_text(encoding="utf-8").strip()
        _info(f"Notes from : {args.notes_file}")
    elif args.notes:
        notes = args.notes.strip()
    else:
        _info("No --notes provided — opening Notepad for release notes…")
        notes = _notes_from_editor(tag)

    if not notes:
        _die("Release notes are empty. Aborting.")

    # ── Check for duplicate tag ───────────────────────────────────────────────
    _info(f"Checking {OWNER}/{REPO} for existing tag {tag}…")
    existing_release = _gh_get(
        f"{API}/repos/{OWNER}/{REPO}/releases/tags/{tag}", token
    )
    if existing_release:
        _die(
            f"A release tagged {tag!r} already exists:\n"
            f"  {existing_release.get('html_url', '')}\n\n"
            "Delete it on GitHub first, or bump the version number."
        )
    _ok("No duplicate found.")

    # ── Create release ────────────────────────────────────────────────────────
    _info(f"Creating release  SquishIt {tag}…")
    release = _gh_post_json(
        f"{API}/repos/{OWNER}/{REPO}/releases",
        token,
        {
            "tag_name": tag,
            "name": f"SquishIt {tag}",
            "body": notes,
            "draft": args.draft,
            "prerelease": False,
        },
    )
    release_url = release.get("html_url", "")
    _ok(f"Release created  →  {release_url}")

    # ── Upload installer asset ────────────────────────────────────────────────
    upload_url = release["upload_url"].split("{")[0] + f"?name={quote(installer.name)}"
    _info(f"Uploading {installer.name}  ({size_mb:.1f} MB)…")
    asset = _gh_post_binary(upload_url, token, installer.read_bytes())
    download_url = asset.get("browser_download_url", "")
    _ok(f"Asset uploaded  →  {download_url}")

    # ── Regenerate README ─────────────────────────────────────────────────────
    _info(f"Fetching all releases from {OWNER}/{REPO}…")
    all_releases = _gh_get(
        f"{API}/repos/{OWNER}/{REPO}/releases?per_page=100", token
    )
    if not isinstance(all_releases, list):
        all_releases = [release]  # fallback: at least include the one we just created

    _info("Building README.md…")
    readme_content = _build_readme(all_releases)

    _info("Updating README.md on releases repo…")
    _update_readme(token, readme_content)
    _ok("README updated.")

    # ── Done ──────────────────────────────────────────────────────────────────
    if _RICH:
        _con.print()
        _con.print(
            Panel.fit(
                f"[bold green]Done![/bold green]\n\n"
                f"  Tag       [cyan]{tag}[/cyan]\n"
                f"  Release   {release_url}\n"
                f"  Download  {download_url}",
                title="✓ Published",
                border_style="green",
            )
        )
    else:
        print(f"\n✓  SquishIt {tag} published successfully.")
        print(f"   Release:   {release_url}")
        print(f"   Download:  {download_url}")


if __name__ == "__main__":
    main()
