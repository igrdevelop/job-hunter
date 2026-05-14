"""
In-memory cache of tracker.xlsx contents.

Speeds up per-job dedup from O(disk read) to O(1), and provides the row-index
mapping needed for O(1) Google Sheets writes.

All mutations and is_known_* reads are protected by asyncio.Lock to prevent
race conditions between the hunt loop and the apply pipeline, which run as
concurrent asyncio tasks.

Lifecycle:
  1. At bot startup: cache.load_from_excel()
  2. Every hunt/apply write goes through cache.add() or cache.update_*()
  3. Every 30 min gsheets pull calls cache.apply_pull_delta() to reflect user edits
"""

import asyncio
import logging
from pathlib import Path

import openpyxl

from hunter.tracker import (
    TRACKER_PATH,
    TRACKER_HEADERS,
    normalize_url,
    dedup_key,
    URL_COL_INDEX,
    COMPANY_COL_INDEX,
    TITLE_COL_INDEX,
    ATS_COL_INDEX,
    SENT_COL_INDEX,
    ID_COL_INDEX,
)

log = logging.getLogger(__name__)

# Angular keyword match for /unsent angular% stat
_ANGULAR_KEYWORDS = ("angular", "ng ")


class TrackerCache:
    """Thread-safe (asyncio) in-memory view of tracker.xlsx."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}           # ID -> row dict
        self.by_url: dict[str, str] = {}          # normalized_url -> ID (latest)
        self.by_ctkey: dict[str, str] = {}        # dedup_key -> ID (latest)
        self.sheet_row_index: dict[str, int] = {} # ID -> Sheets 1-based row index
        self.dirty_ids: set[str] = set()          # IDs that failed Sheets push
        self._lock = asyncio.Lock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    async def load_from_excel(self, path: Path = TRACKER_PATH) -> None:
        """Populate cache from tracker.xlsx. Safe to call again to reload."""
        async with self._lock:
            self._load_locked(path)

    def _load_locked(self, path: Path) -> None:
        """Must be called while holding _lock."""
        self.rows.clear()
        self.by_url.clear()
        self.by_ctkey.clear()
        # sheet_row_index and dirty_ids are Sheets-only state not stored in Excel —
        # preserve them across hot reloads so mirror_cell_update keeps working.

        if not path.exists():
            log.warning("tracker_cache: %s not found, starting empty", path)
            self._loaded = True
            return

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        for sheet_row, row_values in enumerate(
            ws.iter_rows(min_row=2, values_only=True), start=2
        ):
            if not row_values or not any(row_values):
                continue
            row_dict = self._values_to_dict(row_values)
            row_id = row_dict.get("ID", "").strip()
            if not row_id:
                continue
            self._index_row(row_id, row_dict, sheet_row)
        wb.close()
        self._loaded = True
        log.info("tracker_cache: loaded %d rows", len(self.rows))

    def _values_to_dict(self, values: tuple) -> dict:
        padded = list(values) + [""] * (len(TRACKER_HEADERS) - len(values))
        return {
            col: (str(padded[i]) if padded[i] is not None else "")
            for i, col in enumerate(TRACKER_HEADERS)
        }

    def _index_row(self, row_id: str, row_dict: dict, sheet_row: int | None = None) -> None:
        """Insert/overwrite row in all indexes. Caller must hold lock."""
        self.rows[row_id] = row_dict
        url = row_dict.get("URL", "")
        if url:
            self.by_url[normalize_url(url)] = row_id
        company = row_dict.get("Company", "")
        title = row_dict.get("Job Title", "")
        if company and title:
            self.by_ctkey[dedup_key(company, title)] = row_id
        if sheet_row is not None:
            self.sheet_row_index[row_id] = sheet_row

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    async def add(self, row: dict, sheet_row: int | None = None) -> None:
        """Add a new row. row must contain 'ID'. sheet_row is optional Sheets index."""
        async with self._lock:
            row_id = row.get("ID", "").strip()
            if not row_id:
                log.warning("tracker_cache.add: row missing ID, skipping")
                return
            self._index_row(row_id, dict(row), sheet_row)

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

    async def mark_dirty(self, row_id: str) -> None:
        """Flag row for Sheets resync retry."""
        async with self._lock:
            self.dirty_ids.add(row_id)

    async def mark_clean(self, row_id: str) -> None:
        """Clear dirty flag after successful Sheets write."""
        async with self._lock:
            self.dirty_ids.discard(row_id)

    async def set_sheet_row_index(self, row_id: str, sheet_row: int) -> None:
        """Store the Sheets row index after a successful append."""
        async with self._lock:
            self.sheet_row_index[row_id] = sheet_row

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

    async def dirty_rows(self) -> list[tuple[str, dict, int | None]]:
        """Return (id, row, sheet_row_index) for all dirty rows."""
        async with self._lock:
            result = []
            for row_id in list(self.dirty_ids):
                if row_id in self.rows:
                    result.append((
                        row_id,
                        dict(self.rows[row_id]),
                        self.sheet_row_index.get(row_id),
                    ))
            return result

    # ------------------------------------------------------------------
    # Pull delta (Sheets → cache)
    # ------------------------------------------------------------------

    async def apply_pull_delta(self, sheets_rows: list[tuple[int, dict]]) -> list[dict]:
        """
        Merge a fresh Sheets snapshot into the cache.

        For each Sheets row matched by ID:
        - Update user-editable fields (Sent, To Learn, Re-application) per conflict matrix.
        - Update sheet_row_index.

        Returns list of row dicts that need to be written back to Excel
        (i.e., rows where Sheets had a newer value).
        """
        to_write_excel: list[dict] = []

        async with self._lock:
            sheets_by_id = {r.get("ID", ""): (idx, r) for idx, r in sheets_rows if r.get("ID")}

            for row_id, cached in list(self.rows.items()):
                if row_id not in sheets_by_id:
                    # Row missing from Sheets — will be restored by gsheets_sync
                    continue

                sheet_idx, sheet_row = sheets_by_id[row_id]
                self.sheet_row_index[row_id] = sheet_idx

                changed = False
                excel_sent = cached.get("Sent", "").strip()
                sheet_sent = sheet_row.get("Sent", "").strip()

                # Conflict matrix for Sent (§9)
                if excel_sent != sheet_sent:
                    if excel_sent == "EXPIRED" and not sheet_sent:
                        # Bot's EXPIRED wins — Sheets will be fixed by resync
                        pass
                    else:
                        # Trust Sheets (user's date, user erased, different date)
                        cached["Sent"] = sheet_sent
                        changed = True

                # User-editable columns: always trust Sheets if different
                for field in ("To Learn", "Re-application"):
                    sheet_val = sheet_row.get(field, "").strip()
                    if cached.get(field, "").strip() != sheet_val:
                        cached[field] = sheet_val
                        changed = True

                if changed:
                    to_write_excel.append(dict(cached))

        return to_write_excel

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
        return (
            f"<TrackerCache rows={len(self.rows)} "
            f"dirty={len(self.dirty_ids)} loaded={self._loaded}>"
        )


# Singleton — one cache per process
cache = TrackerCache()
