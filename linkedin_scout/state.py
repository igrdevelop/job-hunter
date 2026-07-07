"""Persisted run state for the LinkedIn posts scout: circuit-breaker trip flag
+ round-robin keyword rotation index (task spec §3.5 / M2).

Pure, no Playwright import — a plain JSON file, atomic write, fully independent
of tracker.db / hunter's DB-backed config store (this script is standalone).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _StateData:
    tripped_at: str | None = None
    trip_reason: str = ""
    keyword_index: int = 0


class ScoutState:
    """Loads/saves linkedin_scout run state to a JSON file (atomic write)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data = _StateData()
        self._loaded = False

    def load(self) -> _StateData:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = _StateData(
                    tripped_at=raw.get("tripped_at"),
                    trip_reason=raw.get("trip_reason", ""),
                    keyword_index=int(raw.get("keyword_index", 0)),
                )
            except (json.JSONDecodeError, OSError, ValueError, UnicodeDecodeError):
                self._data = _StateData()
        else:
            self._data = _StateData()
        self._loaded = True
        return self._data

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def save(self) -> None:
        self._ensure_loaded()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {
            "tripped_at": self._data.tripped_at,
            "trip_reason": self._data.trip_reason,
            "keyword_index": self._data.keyword_index,
        }
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp_path, self.path)

    def is_tripped(self) -> bool:
        self._ensure_loaded()
        return self._data.tripped_at is not None

    def trip_reason(self) -> str:
        self._ensure_loaded()
        return self._data.trip_reason

    def trip(self, reason: str) -> bool:
        """Mark the circuit breaker tripped.

        Returns True the FIRST time this fires (the caller should send exactly
        one Telegram alert then); returns False if already tripped, so a caller
        that calls trip() again on a later no-op run never re-alerts. This is
        the mechanism behind the spec's "send exactly ONE Telegram alert" rule.
        """
        self._ensure_loaded()
        if self._data.tripped_at is not None:
            return False
        self._data.tripped_at = _now_iso()
        self._data.trip_reason = reason
        self.save()
        return True

    def reset(self) -> None:
        """Clear the circuit-breaker trip (owner action: `run.py --reset`)."""
        self._ensure_loaded()
        self._data.tripped_at = None
        self._data.trip_reason = ""
        self.save()

    def next_keyword(self, keywords: list[str]) -> str:
        """Round-robin: return the next keyword and persist the advanced index.

        Exactly one keyword per call, by design (task spec §3.5 point 3) — the
        caller must not loop this to exhaust the whole list in one run.
        """
        if not keywords:
            raise ValueError("keywords must be a non-empty list")
        self._ensure_loaded()
        idx = self._data.keyword_index % len(keywords)
        keyword = keywords[idx]
        self._data.keyword_index = (idx + 1) % len(keywords)
        self.save()
        return keyword
