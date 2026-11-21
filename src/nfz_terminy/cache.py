"""Prosty plikowy cache słowników (np. miejscowości województwa).

Słowniki zmieniają się rzadko, a ich pobranie to kilka-kilkanaście zapytań, więc trzymamy
je lokalnie przez kilka dni. Cache jest tylko optymalizacją: każdy błąd odczytu/zapisu
jest ignorowany, a dane są wtedy po prostu pobierane ponownie.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_TTL = timedelta(days=7)
APP_NAME = "nfz-terminy"


def default_cache_dir() -> Path:
    """Katalog cache: $NFZ_TERMINY_CACHE_DIR albo standardowe miejsce dla systemu."""
    override = os.environ.get("NFZ_TERMINY_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / APP_NAME
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP_NAME


class LocalityCache:
    """Nazwy miejscowości per województwo, zapisane w jednym pliku JSON."""

    def __init__(self, directory: Path | None = None, ttl: timedelta = DEFAULT_TTL) -> None:
        self._path = (directory or default_cache_dir()) / "localities.json"
        self._ttl = ttl

    def get(self, province: str) -> list[str] | None:
        entry = self._load().get(province)
        if not isinstance(entry, dict):
            return None
        try:
            fetched = datetime.fromisoformat(entry["fetched"])
            names = [str(n) for n in entry["names"]]
        except (KeyError, TypeError, ValueError):
            return None
        if datetime.now(UTC) - fetched > self._ttl:
            return None
        return names

    def put(self, province: str, names: list[str]) -> None:
        data = self._load()
        data[province] = {"fetched": datetime.now(UTC).isoformat(), "names": names}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _load(self) -> dict[str, object]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
