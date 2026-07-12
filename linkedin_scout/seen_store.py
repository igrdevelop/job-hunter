"""Dedup key + local JSON seen-store for the LinkedIn posts scout.

Completely independent of hunter/tracker.db (task spec §3.3) — this script must
never touch the bot's tracker. Plain JSON, atomic write (write to a temp file in
the same directory, then os.replace) so a crash mid-write can't corrupt the store.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_SNIPPET_CHARS = 200


def dedup_key(author: str, text: str) -> str:
    """md5(author + first 200 chars of post text) — task spec §3.3."""
    snippet = (text or "")[:_SNIPPET_CHARS]
    payload = f"{author or ''}{snippet}".encode("utf-8")
    return hashlib.md5(payload, usedforsecurity=False).hexdigest()


class SeenStore:
    """Lazily-loaded set of dedup keys, backed by a JSON file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._seen: set[str] = set()
        self._loaded = False

    def load(self) -> set[str]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._seen = set(data.get("seen", []))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                self._seen = set()
        else:
            self._seen = set()
        self._loaded = True
        return self._seen

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def is_seen(self, key: str) -> bool:
        self._ensure_loaded()
        return key in self._seen

    def mark_seen(self, key: str) -> None:
        self._ensure_loaded()
        self._seen.add(key)

    def save(self) -> None:
        self._ensure_loaded()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps({"seen": sorted(self._seen)}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, self.path)
