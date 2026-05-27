"""
In-memory cache of the SQLite tracker DB.

Speeds up per-job dedup from O(disk read) to O(1).

All mutations and is_known_* reads are protected by asyncio.Lock to prevent
race conditions between the hunt loop and the apply pipeline, which run as
concurrent asyncio tasks.

Lifecycle:
  1. At bot startup: cache.load_from_db()
  2. Every hunt/apply write goes through cache.add() or cache.update_*()
  3. Sheets metadata (sheets_row, sheets_dirty) is stored directly in DB —
     see hunter.tracker.set_sheets_row / mark_sheets_dirty / mark_sheets_clean.
"""

import asyncio
import logging
from pathlib import Path

from hunter.tracker import (
    TRACKER_HEADERS,
    normalize_url,
    normalize_company,
    dedup_key,
    _strip_marketing_tail,
    _title_similarity,
)

log = logging.getLogger(__name__)

# Angular keyword match for /unsent angular% stat
_ANGULAR_KEYWORDS = ("angular", "ng ")


class TrackerCache:
    """Thread-safe (asyncio) in-memory view of the tracker SQLite DB."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}    # ID -> row dict
        self.by_url: dict[str, str] = {}   # normalized_url -> ID (latest)
        self.by_ctkey: dict[str, str] = {} # dedup_key -> ID (latest)
        self._lock = asyncio.Lock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    async def load_from_db(self) -> None:
        """Populate cache from the SQLite tracker DB.  Safe to call again to reload."""
        from hunter.tracker import read_all_tracker_rows
        rows = await asyncio.to_thread(read_all_tracker_rows)
        async with self._lock:
            self._load_rows_locked(rows)

    async def load_from_excel(self, path: Path | None = None) -> None:
        """Deprecated wrapper — calls load_from_db() and ignores *path*.

        Kept for backward compatibility; prefer load_from_db() for new code.
        """
        await self.load_from_db()

    def _load_rows_locked(self, rows: list[dict]) -> None:
        """Rebuild all indexes from a fresh list of row dicts.  Must hold _lock."""
        self.rows.clear()
        self.by_url.clear()
        self.by_ctkey.clear()
        for row_dict in rows:
            row_id = row_dict.get("ID", "").strip()
            if not row_id:
                continue
            self._index_row(row_id, row_dict)
        self._loaded = True
        log.info("tracker_cache: loaded %d rows from DB", len(self.rows))

    def _index_row(self, row_id: str, row_dict: dict) -> None:
        """Insert/overwrite row in all indexes. Caller must hold lock."""
        self.rows[row_id] = row_dict
        url = row_dict.get("URL", "")
        if url:
            self.by_url[normalize_url(url)] = row_id
        company = row_dict.get("Company", "")
        title = row_dict.get("Job Title", "")
        if company and title:
            self.by_ctkey[dedup_key(company, title)] = row_id

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def add(self, row: dict) -> None:
        """Add a new row. row must contain 'ID'."""
        async with self._lock:
            row_id = row.get("ID", "").strip()
            if not row_id:
                log.warning("tracker_cache.add: row missing ID, skipping")
                return
            self._index_row(row_id, dict(row))

    async def update_status(self, row_id: str, ats_value: str) -> None:
        """Update ATS % / status field (SKIP, FAIL, EXPIRED, score…)."""
        async with self._lock:
            if row_id not in self.rows:
                log.warning("tracker_cache.update_status: unknown ID %s", row_id)
                return
            self.rows[row_id]["ATS %"] = ats_value

    async def update_sent(self, row_id: str, sent_value: str) -> None:
        """Update the Sent column (date string or EXPIRED)."""
        async with self._lock:
            if row_id not in self.rows:
                log.warning("tracker_cache.update_sent: unknown ID %s", row_id)
                return
            self.rows[row_id]["Sent"] = sent_value

    async def update_field(self, row_id: str, field: str, value: str) -> None:
        """Update any single column by header name."""
        async with self._lock:
            if row_id not in self.rows:
                log.warning("tracker_cache.update_field: unknown ID %s", row_id)
                return
            if field not in TRACKER_HEADERS:
                raise ValueError(f"Unknown field {field!r}")
            self.rows[row_id][field] = value

    async def invalidate_url(self, url: str) -> None:
        """Remove all cache entries for this URL (by_url, by_ctkey, rows).

        Call after delete_all_by_url() so the next hunt/apply sees a clean slate.
        """
        norm = normalize_url(url)
        async with self._lock:
            row_id = self.by_url.pop(norm, None)
            if row_id is None:
                return
            row = self.rows.pop(row_id, None)
            if row:
                company = row.get("Company", "")
                title = row.get("Job Title", "")
                if company and title:
                    self.by_ctkey.pop(dedup_key(company, title), None)

    # ------------------------------------------------------------------
    # Dedup reads
    # ------------------------------------------------------------------

    async def is_known_url(self, url: str) -> bool:
        """Return True if a normalized form of url is already in tracker."""
        async with self._lock:
            return normalize_url(url) in self.by_url

    async def is_known_ct(self, company: str, title: str) -> bool:
        """Return True if company+title dedup key is already in tracker."""
        async with self._lock:
            return dedup_key(company, title) in self.by_ctkey

    async def is_fuzzy_ct(
        self,
        company: str,
        title: str,
        threshold: float = 0.6,
    ) -> bool:
        """Return True if a same-company row with a similar title already exists.

        Used as a soft dedup when URL and exact company+title checks both miss —
        e.g. Gmail enriches "Angular Developer" → "Remote Angular Developer —
        Build great UIs", which П-1.1 reduces to "Remote Angular Developer" but
        still doesn't exactly match the stored "Angular Developer".

        The check is O(n) in rows-per-company (typically 1-5) so it is only
        worth calling after both fast checks have already failed.
        """
        norm_company = normalize_company(company)
        if not norm_company:
            return False
        # Strip marketing tail from the incoming title before comparison so
        # "Angular Dev — Build great UIs" becomes "Angular Dev" before scoring.
        clean_title = _strip_marketing_tail(title)
        async with self._lock:
            for row in self.rows.values():
                if normalize_company(row.get("Company", "")) != norm_company:
                    continue
                stored_title = row.get("Job Title", "")
                if _title_similarity(clean_title, stored_title) >= threshold:
                    return True
        return False

    async def get_row_by_url(self, url: str) -> dict | None:
        """Return the row dict for a URL, or None if not found."""
        async with self._lock:
            row_id = self.by_url.get(normalize_url(url))
            if row_id is None:
                return None
            return dict(self.rows[row_id])

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    async def unsent_count(self) -> int:
        """Count rows where Sent is blank."""
        async with self._lock:
            return sum(
                1 for r in self.rows.values()
                if not r.get("Sent", "").strip()
            )

    async def unsent_angular_count(self) -> int:
        """Count unsent rows where title or stack contains Angular."""
        async with self._lock:
            count = 0
            for r in self.rows.values():
                if r.get("Sent", "").strip():
                    continue
                title = r.get("Job Title", "").lower()
                stack = r.get("Stack", "").lower()
                if any(kw in title or kw in stack for kw in _ANGULAR_KEYWORDS):
                    count += 1
            return count

    async def all_unsent(self) -> list[dict]:
        """Return copies of all rows where Sent is blank."""
        async with self._lock:
            return [
                dict(r) for r in self.rows.values()
                if not r.get("Sent", "").strip()
            ]

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        return len(self.rows)

    @property
    def loaded(self) -> bool:
        return self._loaded

    def __repr__(self) -> str:
        return f"<TrackerCache rows={len(self.rows)} loaded={self._loaded}>"


# Singleton — one cache per process
cache = TrackerCache()
