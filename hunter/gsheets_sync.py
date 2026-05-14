"""
hunter/gsheets_sync.py — High-level Google Sheets mirror logic.

Responsibilities:
  - Mirror writes (new rows, status updates, EXPIRED stamps) to Sheets best-effort.
  - Mark rows dirty in cache when Sheets write fails.
  - Retry dirty rows via resync_dirty().
  - Validate credentials and sheet reachability on startup.

All public mirror_* functions are async and safe to call even when GSHEETS_ENABLED=False
(they become no-ops). The caller never needs to check the flag.

Pull logic (Sheets → Excel) is in Phase 5.
Bootstrap logic (create/load spreadsheet) is in Phase 6.
"""

import asyncio
import logging
from typing import Any

from hunter.config import (
    GSHEETS_ENABLED,
    GSHEETS_TRACKER_ID,
    GSHEETS_CREDENTIALS_FILE,
    GSHEETS_TOKEN_FILE,
)
from hunter.tracker_cache import cache

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy service singleton
# ---------------------------------------------------------------------------

_service: Any = None


def _get_service() -> Any | None:
    """Build and cache the Sheets API service. Returns None if disabled or on error."""
    if not GSHEETS_ENABLED:
        return None
    global _service
    if _service is None:
        try:
            from hunter.gsheets_client import build_service
            _service = build_service(GSHEETS_CREDENTIALS_FILE, GSHEETS_TOKEN_FILE)
        except Exception as e:
            log.error("gsheets_sync: failed to build service: %s", e)
    return _service


def _sheet_id() -> str:
    return GSHEETS_TRACKER_ID


def _ready() -> bool:
    return bool(GSHEETS_ENABLED and _sheet_id() and _get_service() is not None)


# ---------------------------------------------------------------------------
# Mirror — append new row
# ---------------------------------------------------------------------------

async def mirror_new_row(row: dict) -> None:
    """Append a new row to Sheets. On success, store sheet_row_index in cache.

    Called after: add_applied, add_skipped, add_manual_jobleads_pending.
    """
    if not _ready():
        return
    row_id = row.get("ID", "").strip()
    if not row_id:
        return

    from hunter.gsheets_client import append_rows

    try:
        indices = await asyncio.to_thread(
            append_rows, _get_service(), _sheet_id(), [row]
        )
        if indices:
            await cache.set_sheet_row_index(row_id, indices[0])
        await cache.mark_clean(row_id)
        log.debug("gsheets mirror_new_row: %s → row %s", row_id, indices[0] if indices else "?")
    except Exception as e:
        log.error("gsheets mirror_new_row failed for %s: %s", row_id, e)
        await cache.mark_dirty(row_id)


# ---------------------------------------------------------------------------
# Mirror — single cell update (status, EXPIRED, etc.)
# ---------------------------------------------------------------------------

async def mirror_cell_update(row_id: str, col: str, value: str) -> None:
    """Update a single cell in Sheets using cached sheet_row_index.

    Called after: skip, fail, EXPIRED, manual status changes.
    If sheet_row_index is unknown, marks dirty for later resync.
    """
    if not _ready() or not row_id:
        return

    from hunter.gsheets_client import update_cell

    sheet_row = cache.sheet_row_index.get(row_id)
    if sheet_row is None:
        log.debug("gsheets mirror_cell_update: no sheet_row for %s — marking dirty", row_id)
        await cache.mark_dirty(row_id)
        return

    try:
        await asyncio.to_thread(
            update_cell, _get_service(), _sheet_id(), sheet_row, col, value
        )
        await cache.mark_clean(row_id)
    except Exception as e:
        log.error("gsheets mirror_cell_update(%s, %s) failed: %s", row_id, col, e)
        await cache.mark_dirty(row_id)


# ---------------------------------------------------------------------------
# Mirror — batch EXPIRED write
# ---------------------------------------------------------------------------

async def mirror_expired_batch(row_ids: set[str]) -> None:
    """Write EXPIRED to Sheets Sent column for all given row IDs.

    Called by expired_marker.py after tracker.xlsx is updated.
    """
    if not _ready() or not row_ids:
        return
    for row_id in row_ids:
        await mirror_cell_update(row_id, "Sent", "EXPIRED")


# ---------------------------------------------------------------------------
# Resync dirty rows
# ---------------------------------------------------------------------------

async def resync_dirty() -> int:
    """Retry all dirty rows. Returns number successfully pushed to Sheets."""
    if not _ready():
        return 0

    from hunter.gsheets_client import append_rows, update_row

    dirty = await cache.dirty_rows()
    if not dirty:
        return 0

    synced = 0
    svc = _get_service()
    sheet_id = _sheet_id()

    for row_id, row, sheet_row in dirty:
        try:
            if sheet_row is None:
                # Row was never pushed — append it
                indices = await asyncio.to_thread(
                    append_rows, svc, sheet_id, [row]
                )
                if indices:
                    await cache.set_sheet_row_index(row_id, indices[0])
            else:
                # Row exists in Sheets — overwrite it
                await asyncio.to_thread(
                    update_row, svc, sheet_id, sheet_row, row
                )
            await cache.mark_clean(row_id)
            synced += 1
            log.debug("gsheets resync_dirty: synced %s", row_id)
        except Exception as e:
            log.warning("gsheets resync_dirty: failed for %s: %s", row_id, e)

    log.info("gsheets resync_dirty: %d/%d rows synced", synced, len(dirty))
    return synced


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def validate_startup() -> dict:
    """
    Check credentials, token, and sheet reachability.

    Returns: {"ok": bool, "error": str | None, "sheet_url": str | None}
    """
    if not GSHEETS_ENABLED:
        return {"ok": True, "error": None, "sheet_url": None}

    if not GSHEETS_CREDENTIALS_FILE.exists():
        return {
            "ok": False,
            "error": f"gsheets_credentials.json not found at {GSHEETS_CREDENTIALS_FILE}",
            "sheet_url": None,
        }
    if not GSHEETS_TOKEN_FILE.exists():
        return {
            "ok": False,
            "error": (
                f"gsheets_token.json not found. "
                f"Run: python tools/gsheets_auth.py"
            ),
            "sheet_url": None,
        }

    svc = _get_service()
    if svc is None:
        return {"ok": False, "error": "Failed to build Sheets service (check token)", "sheet_url": None}

    if not GSHEETS_TRACKER_ID:
        return {
            "ok": True,
            "error": None,
            "sheet_url": None,
            "warning": "GSHEETS_TRACKER_ID not set — will be created on first run (Phase 6)",
        }

    # Try a lightweight read to verify the sheet is accessible
    try:
        from hunter.gsheets_client import read_all
        read_all(svc, GSHEETS_TRACKER_ID)
        sheet_url = f"https://docs.google.com/spreadsheets/d/{GSHEETS_TRACKER_ID}"
        return {"ok": True, "error": None, "sheet_url": sheet_url}
    except Exception as e:
        return {"ok": False, "error": f"Sheet not accessible: {e}", "sheet_url": None}


# ---------------------------------------------------------------------------
# Status report (for /gsheets_status command)
# ---------------------------------------------------------------------------

async def status_report() -> dict:
    """Return a dict summarising gsheets integration state for the status command."""
    dirty_rows = await cache.dirty_rows()
    sheet_url = (
        f"https://docs.google.com/spreadsheets/d/{GSHEETS_TRACKER_ID}"
        if GSHEETS_TRACKER_ID else None
    )
    return {
        "enabled": GSHEETS_ENABLED,
        "sheet_id": GSHEETS_TRACKER_ID or None,
        "sheet_url": sheet_url,
        "dirty_count": len(dirty_rows),
        "service_ok": _service is not None,
    }
