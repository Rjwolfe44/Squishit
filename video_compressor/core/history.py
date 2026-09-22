"""Compression history — persistent log of past jobs."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ..config import CONFIG_DIR, get_config

logger = logging.getLogger(__name__)

_HISTORY_FILE = CONFIG_DIR / "history.json"


@dataclass
class HistoryEntry:
    """One completed compression run."""

    input_path: str
    output_path: str
    original_size_bytes: int
    compressed_size_bytes: int
    reduction_pct: float
    codec: str
    profile_name: str
    duration_seconds: float
    timestamp: str  # ISO-8601


class HistoryManager:
    """Read/write the on-disk history log (JSON)."""

    def __init__(self, path: Optional[Path] = None, max_entries: Optional[int] = None):
        self._path = path or _HISTORY_FILE
        self._max_entries = max_entries

    # ── public API ─────────────────────────────────────────────────

    def add_entry(self, entry: HistoryEntry) -> None:
        entries = self._load()
        entries.append(asdict(entry))
        max_entries = self._max_entries or get_config().history_max_entries
        if len(entries) > max_entries:
            entries = entries[-max_entries:]
        self._save(entries)

    def get_entries(self) -> List[HistoryEntry]:
        raw = self._load()
        result: List[HistoryEntry] = []
        for d in reversed(raw):
            try:
                result.append(HistoryEntry(**d))
            except (TypeError, KeyError):
                continue
        return result

    def clear(self) -> None:
        self._save([])

    def get_stats(self) -> dict:
        entries = self._load()
        total_saved = sum(
            e.get("original_size_bytes", 0) - e.get("compressed_size_bytes", 0)
            for e in entries
        )
        return {"count": len(entries), "total_saved_bytes": max(0, total_saved)}

    # ── internals ──────────────────────────────────────────────────

    def _load(self) -> list:
        if not self._path.exists():
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Could not load history: %s", exc)
            return []

    def _save(self, entries: list) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, ensure_ascii=False)
        except OSError as exc:
            logger.error("Could not save history: %s", exc)
