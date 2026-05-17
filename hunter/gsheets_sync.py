"""
hunter/gsheets_sync.py — High-level Google Sheets mirror logic.

Responsibilities:
  - Mirror writes (new rows, status updates, EXPIRED stamps) to Sheets best-effort.
  - Mark rows dirty in cache when Sheets write fails.
  - Retry dirty rows via resync_dirty().
  - Validate credentials and sheet reachability on startup.
  - Bootstrap: create or load spreadsheet (Phase 6).

All public mirror_* functions are async and safe to call even when GSHEETS_ENABLED=False
(they become no-ops). The caller never needs to check the flag.

Pull logic (Sheets → Excel) is in pull_full_snapshot().
Bootstrap logic (create/load spreadsheet) is in init_or_load_spreadsheet().
"""

import asyncio
import json
import logging
from typing import Any

from hunter.config import (
    GSHEETS_ENABLED,
    GSHEETS_TRACKER_ID,
    GSHEETS_CREDENTIALS_FILE,
    GSHEETS_TOKEN_FILE,
    GSHEETS_STATE_FILE,
)
from hunter.tracker_cache import cache

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime state (survives process lifetime; persisted to gsheets_state.json)
# ---------------------------------------------------------------------------

_state: dict = {}   # {"sheet_id": "..."}


def _read_state() -> dict:
    """Load gsheets_state.json. Returns {} if missing or malformed."""
    try:
        if GSHEETS_STATE_FILE.exists():
            return json.loads(GSHEETS_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("gsheets_sync: could not read state file: %s", e)
    return {}


def _write_state(data: dict) -> None:
    """Persist runtime state to gsheets_state.json (atomic-ish via write+rename)."""
    try:
        tmp = GSHEETS_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(GSHEETS_STATE_FILE)
    except Exception as e:
        log.warning("gsheets_sync: could not write state file: %s", e)


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
    """Return the active sheet ID: runtime state > env var."""
    return _state.get("sheet_id") or GSHEETS_TRACKER_ID


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
# Pull — Sheets → tracker.xlsx
# ---------------------------------------------------------------------------

async def pull_full_snapshot() -> dict:
    """
    Pull all rows from Google Sheets and merge into cache + tracker.xlsx.

    Conflict matrix (applied in tracker_cache.apply_pull_delta):
      - Sent: EXPIRED beats empty Sheets; Sheets date beats EXPIRED; else trust Sheets.
      - To Learn, Re-application: always trust Sheets (user edits there).

    Returns: {"pulled": int, "updated": int, "errors": list[str]}
    """
    if not _ready():
        return {"pulled": 0, "updated": 0, "errors": []}

    from hunter.gsheets_client import read_all
    from hunter.tracker import apply_pull_updates
    from hunter.config import TRACKER_PATH

    errors: list[str] = []

    try:
        sheets_rows = await asyncio.to_thread(
            read_all, _get_service(), _sheet_id()
        )
    except Exception as e:
        log.error("gsheets pull_full_snapshot: read_all failed: %s", e)
        return {"pulled": 0, "updated": 0, "errors": [str(e)]}

    # Update sheet_row_index for all rows we see in Sheets
    for sheet_row_num, row in sheets_rows:
        row_id = row.get("ID", "").strip()
        if row_id:
            await cache.set_sheet_row_index(row_id, sheet_row_num)

    # Apply conflict matrix — get rows that need Excel update
    to_write = await cache.apply_pull_delta(sheets_rows)

    if to_write:
        try:
            written = await asyncio.to_thread(apply_pull_updates, to_write)
            log.info("gsheets pull_full_snapshot: updated %d/%d rows in Excel", written, len(to_write))
        except Exception as e:
            log.error("gsheets pull_full_snapshot: apply_pull_updates failed: %s", e)
            errors.append(str(e))

    log.info("gsheets pull_full_snapshot: pulled %d rows, %d Excel updates", len(sheets_rows), len(to_write))
    return {"pulled": len(sheets_rows), "updated": len(to_write), "errors": errors}


# ---------------------------------------------------------------------------
# Bootstrap — create or load spreadsheet
# ---------------------------------------------------------------------------

async def init_or_load_spreadsheet(
    notify_cb=None,
) -> dict:
    """
    Determine the active spreadsheet, creating one if needed.

    Resolution order:
      1. gsheets_state.json has "sheet_id" → use it (Docker restart safety).
      2. GSHEETS_TRACKER_ID env var is set → use it, save to state.
      3. Both empty → create a new spreadsheet, save state, call notify_cb.

    notify_cb: async callable(text: str) to send a Telegram message.
    Returns: {"sheet_id": str, "created": bool, "sheet_url": str}
    """
    global _state

    if not GSHEETS_ENABLED:
        return {"sheet_id": "", "created": False, "sheet_url": ""}

    svc = _get_service()
    if svc is None:
        return {"sheet_id": "", "created": False, "sheet_url": "", "error": "no service"}

    # 1. Check state file
    file_state = _read_state()
    if file_state.get("sheet_id"):
        _state = file_state
        log.info("gsheets_sync: loaded sheet_id from state file: %s", _state["sheet_id"])
        return {
            "sheet_id": _state["sheet_id"],
            "created": False,
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{_state['sheet_id']}",
        }

    # 2. Check env var
    if GSHEETS_TRACKER_ID:
        _state = {"sheet_id": GSHEETS_TRACKER_ID}
        _write_state(_state)
        log.info("gsheets_sync: using env GSHEETS_TRACKER_ID, saved to state")
        return {
            "sheet_id": GSHEETS_TRACKER_ID,
            "created": False,
            "sheet_url": f"https://docs.google.com/spreadsheets/d/{GSHEETS_TRACKER_ID}",
        }

    # 3. Create a new spreadsheet
    log.info("gsheets_sync: no sheet_id found — creating new spreadsheet")
    try:
        from hunter.gsheets_client import create_spreadsheet
        sheet_id = await asyncio.to_thread(create_spreadsheet, svc, "Job Tracker")
    except Exception as e:
        log.error("gsheets_sync: create_spreadsheet failed: %s", e)
        return {"sheet_id": "", "created": False, "sheet_url": "", "error": str(e)}

    _state = {"sheet_id": sheet_id}
    _write_state(_state)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}"
    log.info("gsheets_sync: created new spreadsheet: %s", sheet_url)

    if notify_cb:
        try:
            await notify_cb(
                "📊 <b>Google Sheets tracker создан!</b>\n\n"
                f'🔗 <a href="{sheet_url}">Открыть таблицу</a>\n\n'
                "Сохрани ID в .env:\n"
                f"<code>GSHEETS_TRACKER_ID={sheet_id}</code>\n\n"
                "💡 <b>Фильтр-вид для отправки:</b>\n"
                "1. Данные → Создать фильтр-вид\n"
                "2. Столбец «Sent» → Фильтровать: пусто\n"
                "3. Сохрани вид — будет показывать только неотосланные."
            )
        except Exception as e:
            log.warning("gsheets_sync: notify_cb failed: %s", e)

    return {"sheet_id": sheet_id, "created": True, "sheet_url": sheet_url}


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

    sid = _sheet_id()
    if not sid:
        return {
            "ok": True,
            "error": None,
            "sheet_url": None,
            "warning": "GSHEETS_TRACKER_ID not set — will be created on first run",
        }

    # Try a lightweight read to verify the sheet is accessible
    try:
        from hunter.gsheets_client import read_all
        read_all(svc, sid)
        sheet_url = f"https://docs.google.com/spreadsheets/d/{sid}"
        return {"ok": True, "error": None, "sheet_url": sheet_url}
    except Exception as e:
        return {"ok": False, "error": f"Sheet not accessible: {e}", "sheet_url": None}


# ---------------------------------------------------------------------------
# Push missing rows (tracker.xlsx → Sheets, skipping rows already there)
# ---------------------------------------------------------------------------

async def push_missing_rows() -> dict:
    """Append tracker.xlsx rows that are absent from Google Sheets (by ID).

    Returns: {"pushed": int, "already_present": int, "errors": list[str]}
    Used by /gsheets_push_missing command.
    """
    if not _ready():
        return {"pushed": 0, "already_present": 0, "errors": ["Sheets not ready"]}

    from hunter.gsheets_client import read_all, append_rows
    from hunter.tracker import read_all_tracker_rows

    errors: list[str] = []

    # 1. Fetch IDs already in Sheets
    try:
        sheets_rows = await asyncio.to_thread(read_all, _get_service(), _sheet_id())
    except Exception as e:
        log.error("push_missing_rows: read_all failed: %s", e)
        return {"pushed": 0, "already_present": 0, "errors": [str(e)]}

    sheets_ids: set[str] = {
        r.get("ID", "").strip()
        for _, r in sheets_rows
        if r.get("ID", "").strip()
    }
    # Update sheet_row_index cache while we have the data
    for sheet_row_num, row in sheets_rows:
        row_id = row.get("ID", "").strip()
        if row_id:
            await cache.set_sheet_row_index(row_id, sheet_row_num)

    # 2. Find tracker rows absent from Sheets
    try:
        tracker_rows = await asyncio.to_thread(read_all_tracker_rows)
    except Exception as e:
        log.error("push_missing_rows: read_all_tracker_rows failed: %s", e)
        return {"pushed": 0, "already_present": 0, "errors": [str(e)]}

    already = sum(1 for r in tracker_rows if r.get("ID", "").strip() in sheets_ids)
    missing = [r for r in tracker_rows if r.get("ID", "").strip() not in sheets_ids]

    if not missing:
        log.info("push_missing_rows: nothing to push (%d rows already in Sheets)", already)
        return {"pushed": 0, "already_present": already, "errors": []}

    # 3. Append missing rows in one batch
    try:
        indices = await asyncio.to_thread(
            append_rows, _get_service(), _sheet_id(), missing
        )
        for row_dict, sheet_row in zip(missing, indices):
            row_id = row_dict.get("ID", "").strip()
            if row_id and sheet_row > 0:
                await cache.set_sheet_row_index(row_id, sheet_row)
                await cache.mark_clean(row_id)
        log.info("push_missing_rows: pushed %d rows", len(missing))
    except Exception as e:
        log.error("push_missing_rows: append_rows failed: %s", e)
        errors.append(str(e))
        return {"pushed": 0, "already_present": already, "errors": errors}

    return {"pushed": len(missing), "already_present": already, "errors": errors}


# ---------------------------------------------------------------------------
# Status report (for /gsheets_status command)
# ---------------------------------------------------------------------------

async def status_report() -> dict:
    """Return a dict summarising gsheets integration state for the status command."""
    dirty_rows = await cache.dirty_rows()
    sid = _sheet_id()
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sid}" if sid else None
    return {
        "enabled": GSHEETS_ENABLED,
        "sheet_id": sid or None,
        "sheet_url": sheet_url,
        "dirty_count": len(dirty_rows),
        "service_ok": _service is not None,
    }
