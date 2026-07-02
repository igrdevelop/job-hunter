"""
hunter/gsheets_sync.py — High-level Google Sheets mirror logic.

Responsibilities:
  - Mirror writes (new rows, status updates, EXPIRED stamps) to Sheets best-effort.
  - Mark rows dirty in DB when Sheets write fails; retry via resync_dirty().
  - Validate credentials and sheet reachability on startup.
  - Bootstrap: create or load spreadsheet (Phase 6).

All public mirror_* functions are async and safe to call even when GSHEETS_ENABLED=False
(they become no-ops). The caller never needs to check the flag.

Pull logic (Sheets → DB) is in pull_full_snapshot().
Bootstrap logic (create/load spreadsheet) is in init_or_load_spreadsheet().

Sheets metadata (sheets_row, sheets_dirty) is stored in the SQLite DB via
hunter.tracker.{set_sheets_row, get_sheets_row, mark_sheets_dirty, ...}.
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
from hunter.tracker import (
    set_sheets_row,
    get_sheets_row,
    mark_sheets_dirty,
    mark_sheets_clean,
    get_dirty_rows_for_sheets,
    get_dirty_sheets_count,
    lookup_url,
    read_all_tracker_rows,
    apply_pull_updates,
    insert_pulled_rows,
    mark_orphans_expired,
)

# Reconciliation safety: skip marking orphans if the Sheets read returned fewer
# than this fraction of the DB's ID-bearing rows (guards against a partial/failed
# read wrongly EXPIRING live vacancies).
_RECONCILE_MIN_RATIO = 0.8

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Runtime state (survives process lifetime; persisted to gsheets_state.json)
# ---------------------------------------------------------------------------

_state: dict = {}   # {"sheet_id": "..."}


def _read_state() -> dict:
    """Load gsheets_state.json. Returns {} if missing or malformed."""
    if GSHEETS_STATE_FILE.is_dir():
        log.error(
            "gsheets_sync: %s is a directory, not a file — Docker Volume misconfiguration. "
            "Fix on server: stop container, run `rm -rf %s && echo '{}' > %s`, restart.",
            GSHEETS_STATE_FILE, GSHEETS_STATE_FILE, GSHEETS_STATE_FILE,
        )
        return {}
    try:
        if GSHEETS_STATE_FILE.exists():
            return json.loads(GSHEETS_STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("gsheets_sync: could not read state file: %s", e)
    return {}


def _write_state(data: dict) -> None:
    """Persist runtime state to gsheets_state.json (atomic-ish via write+rename)."""
    if GSHEETS_STATE_FILE.is_dir():
        return  # already logged in _read_state
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
    """Append a new row to Sheets.  On success, stores sheets_row in DB.

    Called after: add_applied, add_skipped, add_manual_jobleads_pending.

    After the A–K append lands, this also pokes column M (`Cost $`) via
    hunter.cost_writer and column N (`ATS Verdict`) via hunter.verdict_writer
    — both columns are outside COLUMNS by design so the bot never overwrites
    them with empty strings on subsequent A–K pushes. Both are best-effort;
    a failure there doesn't dirty the row (neither is workflow state).

    Timing note (verdict): the apply subprocess stamps ats_verdict on the DB
    row (tracker.set_ats_verdict, apply Step 7.7) BEFORE it exits, and this
    mirror runs from the bot process after the subprocess completes — so by
    the time the A–K append assigns sheets_row, the verdict is already in
    the DB and the cell write below lands in the same pass.
    """
    if not _ready():
        return
    row_id = row.get("ID", "").strip()
    if not row_id:
        return

    from hunter.cost_writer import mirror_cost_cell_sync
    from hunter.gsheets_client import append_rows
    from hunter.verdict_writer import mirror_verdict_cell_sync

    try:
        indices = await asyncio.to_thread(
            append_rows, _get_service(), _sheet_id(), [row]
        )
        if indices:
            set_sheets_row(row_id, indices[0])
        mark_sheets_clean(row_id)
        log.debug("gsheets mirror_new_row: %s → row %s", row_id, indices[0] if indices else "?")
    except Exception as e:
        log.error("gsheets mirror_new_row failed for %s: %s", row_id, e)
        mark_sheets_dirty(row_id)
        return

    # Mirror cost into column M. Best-effort: a Sheets hiccup here doesn't
    # roll back the A–K push or mark the row dirty (cost isn't part of any
    # workflow gate). We swallow failures because cost_writer logs them
    # internally and the next /sync_costs backfill will catch up.
    try:
        await asyncio.to_thread(
            mirror_cost_cell_sync, _get_service(), _sheet_id(), row_id
        )
    except Exception as e:
        log.warning("gsheets cost mirror failed for %s (non-fatal): %s", row_id, e)

    # Mirror the independent PDF-verdict score into column N. Same best-effort
    # contract as cost above; tools/sync_verdicts.py backfills any misses.
    try:
        await asyncio.to_thread(
            mirror_verdict_cell_sync, _get_service(), _sheet_id(), row_id
        )
    except Exception as e:
        log.warning("gsheets verdict mirror failed for %s (non-fatal): %s", row_id, e)


# ---------------------------------------------------------------------------
# Mirror — single cell update (status, EXPIRED, etc.)
# ---------------------------------------------------------------------------

async def mirror_cell_update(row_id: str, col: str, value: str) -> None:
    """Update a single cell in Sheets using the sheets_row stored in DB.

    Called after: skip, fail, EXPIRED, manual status changes.
    If sheets_row is unknown, marks dirty for later resync.
    """
    if not _ready() or not row_id:
        return

    from hunter.gsheets_client import update_cell

    sheet_row = get_sheets_row(row_id)
    if sheet_row is None:
        log.debug("gsheets mirror_cell_update: no sheets_row for %s — marking dirty", row_id)
        mark_sheets_dirty(row_id)
        return

    try:
        await asyncio.to_thread(
            update_cell, _get_service(), _sheet_id(), sheet_row, col, value
        )
        mark_sheets_clean(row_id)
    except Exception as e:
        log.error("gsheets mirror_cell_update(%s, %s) failed: %s", row_id, col, e)
        mark_sheets_dirty(row_id)


# ---------------------------------------------------------------------------
# Mirror — batch EXPIRED write
# ---------------------------------------------------------------------------

async def mirror_expired_batch(row_ids: set[str]) -> None:
    """Write EXPIRED to Sheets Sent column for all given row IDs.

    Called by expired_marker.py after tracker DB is updated.
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

    dirty = await asyncio.to_thread(get_dirty_rows_for_sheets)
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
                    set_sheets_row(row_id, indices[0])
            else:
                # Row exists in Sheets — overwrite it
                await asyncio.to_thread(
                    update_row, svc, sheet_id, sheet_row, row
                )
            mark_sheets_clean(row_id)
            synced += 1
            log.debug("gsheets resync_dirty: synced %s", row_id)
        except Exception as e:
            log.warning("gsheets resync_dirty: failed for %s: %s", row_id, e)

    log.info("gsheets resync_dirty: %d/%d rows synced", synced, len(dirty))
    return synced


# ---------------------------------------------------------------------------
# Push Sent column — DB → Sheets
# ---------------------------------------------------------------------------

async def push_sent_column() -> dict:
    """Push the Sent column from DB to Sheets for rows that differ.

    Unlike resync_dirty() (which relies on the DB dirty flag and loses
    state on restart), this function reads the DB directly and compares
    against live Sheets data — so it works reliably after any bot restart.

    Useful for:
    - EXPIRED stamps that were written to DB but never reached Sheets
    - Any Sent value mismatch between DB and Sheets

    Returns: {"checked": int, "updated": int, "errors": int}
    """
    if not _ready():
        return {"checked": 0, "updated": 0, "errors": 0}

    from hunter.gsheets_client import read_all, update_cell

    svc = _get_service()
    sid = _sheet_id()

    # Read Sheets: build ID → (sheet_row_index, current_sent) map
    sheets_rows = await asyncio.to_thread(read_all, svc, sid)
    sheets_map: dict[str, tuple[int, str]] = {}
    for row_idx, row_dict in sheets_rows:
        row_id = row_dict.get("ID", "").strip()
        if row_id:
            sheets_map[row_id] = (row_idx, row_dict.get("Sent", "").strip())

    # Read tracker rows that have a non-empty Sent value
    tracker_rows = await asyncio.to_thread(read_all_tracker_rows)

    checked = updated = errors = 0
    for row in tracker_rows:
        tracker_sent = row.get("Sent", "").strip()
        if not tracker_sent:
            continue
        row_id = row.get("ID", "").strip()
        if not row_id or row_id not in sheets_map:
            continue
        sheet_row_idx, sheets_sent = sheets_map[row_id]
        checked += 1
        if tracker_sent == sheets_sent:
            continue  # already in sync
        try:
            await asyncio.to_thread(
                update_cell, svc, sid, sheet_row_idx, "Sent", tracker_sent
            )
            updated += 1
            log.info("push_sent_column: %s → Sent=%s (was %r)", row_id[:8], tracker_sent, sheets_sent)
        except Exception as e:
            errors += 1
            log.warning("push_sent_column: failed for %s: %s", row_id[:8], e)

    log.info("push_sent_column: checked=%d updated=%d errors=%d", checked, updated, errors)
    return {"checked": checked, "updated": updated, "errors": errors}


# ---------------------------------------------------------------------------
# Pull delta helper (Sheets → DB conflict matrix)
# ---------------------------------------------------------------------------

def _apply_pull_delta_db(sheets_rows: list[tuple[int, dict]]) -> list[dict]:
    """Synchronous: apply Sheets conflict matrix against DB; persist sheets_row.

    For each Sheets row that matches a DB row by ID:
    - Stores the Sheets row index in the DB (sheets_row column).
    - Applies the conflict matrix for Sent, To Learn, Re-application.

    Returns list of row dicts (with 'ID') that need to be written back to DB.
    Intended to be called via asyncio.to_thread.

    Conflict matrix:
      Sent:              EXPIRED (DB) + empty (Sheets)  → keep EXPIRED (Sheets will be fixed by resync)
                         anything else differs           → trust Sheets
      To Learn:          always trust Sheets
      Re-application:    always trust Sheets
    """
    tracker_rows = {r["ID"]: r for r in read_all_tracker_rows() if r.get("ID")}
    sheets_by_id = {r.get("ID", ""): (idx, r) for idx, r in sheets_rows if r.get("ID")}

    to_write: list[dict] = []
    for row_id, db_row in tracker_rows.items():
        if row_id not in sheets_by_id:
            continue

        sheet_idx, sheet_row = sheets_by_id[row_id]
        # Persist Sheets row index to DB
        set_sheets_row(row_id, sheet_idx)

        changed = False
        updated = dict(db_row)

        db_sent = db_row.get("Sent", "").strip()
        sheet_sent = sheet_row.get("Sent", "").strip()

        if db_sent != sheet_sent:
            if not (db_sent == "EXPIRED" and not sheet_sent):
                updated["Sent"] = sheet_sent
                changed = True

        for field in ("To Learn", "Re-application"):
            sv = sheet_row.get(field, "").strip()
            if db_row.get(field, "").strip() != sv:
                updated[field] = sv
                changed = True

        if changed:
            to_write.append(updated)

    return to_write


# ---------------------------------------------------------------------------
# Pull — Sheets → DB
# ---------------------------------------------------------------------------

def _reconcile_deleted_rows(sheets_rows: list[tuple[int, dict]]) -> int:
    """Synchronous: mark DB rows whose ID is gone from Sheets as EXPIRED.

    The pull conflict matrix only inserts + updates rows matched by ID; it never
    reacts to *deletions*. When the user (or tools/dedup_sheet.py) removes a row
    from the Sheet, the DB keeps an orphan with a blank Sent that pollutes the
    unsent count forever. This closes that gap.

    Only rows with a non-blank ID and a *blank* Sent are touched — rows the user
    already annotated keep their value, and dedup is preserved (row is kept, just
    stamped EXPIRED). See tracker.mark_orphans_expired.

    Safety: if the Sheets read looks partial (fewer IDs than _RECONCILE_MIN_RATIO
    of the DB's ID-bearing rows), skip entirely rather than mass-EXPIRE live rows.
    Intended to be called via asyncio.to_thread. Returns count marked.
    """
    sheet_ids = {
        (r.get("ID") or "").strip()
        for _, r in sheets_rows
        if (r.get("ID") or "").strip()
    }
    if not sheet_ids:
        return 0

    db_rows = [r for r in read_all_tracker_rows() if (r.get("ID") or "").strip()]
    if not db_rows:
        return 0

    if len(sheet_ids) < _RECONCILE_MIN_RATIO * len(db_rows):
        log.warning(
            "gsheets reconcile: Sheets returned %d IDs vs %d DB rows (<%.0f%%) — "
            "skipping orphan reconcile (looks partial)",
            len(sheet_ids), len(db_rows), _RECONCILE_MIN_RATIO * 100,
        )
        return 0

    orphan_ids = [
        r["ID"].strip()
        for r in db_rows
        if r["ID"].strip() not in sheet_ids and not (r.get("Sent") or "").strip()
    ]
    if not orphan_ids:
        return 0

    marked = mark_orphans_expired(orphan_ids)
    if marked:
        log.info("gsheets reconcile: marked %d orphan row(s) EXPIRED (deleted from Sheets)", marked)
    return marked


async def pull_full_snapshot() -> dict:
    """
    Pull all rows from Google Sheets and merge into DB.

    Conflict matrix (applied in _apply_pull_delta_db):
      - Sent: EXPIRED beats empty Sheets; Sheets date beats EXPIRED; else trust Sheets.
      - To Learn, Re-application: always trust Sheets (user edits there).

    Also persists sheets_row in DB for all matched rows, and inserts Sheets rows
    that are absent from the DB (dedup self-heal after a fresh/empty tracker.db).

    Returns: {"pulled": int, "inserted": int, "updated": int, "errors": list[str]}
    """
    if not _ready():
        return {"pulled": 0, "inserted": 0, "updated": 0, "errors": []}

    from hunter.gsheets_client import read_all

    errors: list[str] = []

    try:
        sheets_rows = await asyncio.to_thread(
            read_all, _get_service(), _sheet_id()
        )
    except Exception as e:
        log.error("gsheets pull_full_snapshot: read_all failed: %s", e)
        return {"pulled": 0, "inserted": 0, "updated": 0, "errors": [str(e)]}

    # Self-heal dedup: insert Sheets rows missing from the DB (must run before the
    # conflict matrix so freshly inserted rows also get their Sent/To Learn applied).
    inserted = 0
    try:
        inserted = await asyncio.to_thread(insert_pulled_rows, sheets_rows)
        if inserted:
            log.info("gsheets pull_full_snapshot: inserted %d missing rows into DB", inserted)
    except Exception as e:
        log.error("gsheets pull_full_snapshot: insert_pulled_rows failed: %s", e)
        errors.append(str(e))

    # Apply conflict matrix + persist sheets_row indices in DB
    try:
        to_write = await asyncio.to_thread(_apply_pull_delta_db, sheets_rows)
    except Exception as e:
        log.error("gsheets pull_full_snapshot: _apply_pull_delta_db failed: %s", e)
        return {"pulled": len(sheets_rows), "inserted": inserted, "updated": 0, "errors": [str(e)]}

    if to_write:
        try:
            written = await asyncio.to_thread(apply_pull_updates, to_write)
            log.info("gsheets pull_full_snapshot: updated %d/%d rows in DB", written, len(to_write))
        except Exception as e:
            log.error("gsheets pull_full_snapshot: apply_pull_updates failed: %s", e)
            errors.append(str(e))

    # Reconcile deletions: rows removed from the Sheet but still lingering in the DB
    # with a blank Sent (guarded against partial reads). Runs last so inserts above
    # are already in the DB and not mistaken for orphans.
    reconciled = 0
    try:
        reconciled = await asyncio.to_thread(_reconcile_deleted_rows, sheets_rows)
    except Exception as e:
        log.error("gsheets pull_full_snapshot: _reconcile_deleted_rows failed: %s", e)
        errors.append(str(e))

    log.info(
        "gsheets pull_full_snapshot: pulled %d rows, %d inserted, %d DB updates, %d reconciled",
        len(sheets_rows), inserted, len(to_write), reconciled,
    )
    return {
        "pulled": len(sheets_rows),
        "inserted": inserted,
        "updated": len(to_write),
        "reconciled": reconciled,
        "errors": errors,
    }


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
                "📊 <b>Google Sheets tracker created!</b>\n\n"
                f'🔗 <a href="{sheet_url}">Open spreadsheet</a>\n\n'
                "Save the ID in .env:\n"
                f"<code>GSHEETS_TRACKER_ID={sheet_id}</code>\n\n"
                "💡 <b>Filter view for sending:</b>\n"
                "1. Data → Create filter view\n"
                "2. Column «Sent» → Filter: empty\n"
                "3. Save the view — shows only unsent applications."
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
                "gsheets_token.json not found. "
                "Run: python tools/gsheets_auth.py"
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
# Delete row by URL (used by /force cleanup)
# ---------------------------------------------------------------------------

async def delete_row_by_url(url: str) -> bool:
    """Delete the Sheets row that corresponds to this URL (best-effort).

    Looks up the row in the tracker DB, reads sheets_row from DB.

    Returns True if a row was deleted, False if not found or Sheets is disabled.
    """
    if not _ready():
        return False

    from hunter.gsheets_client import delete_sheet_row

    # Find row_id via DB lookup
    candidates = await asyncio.to_thread(lookup_url, url)
    if not candidates:
        log.debug("gsheets delete_row_by_url: URL not in tracker: %s", url)
        return False

    row_id = candidates[0]["id"]
    sheet_row = get_sheets_row(row_id)
    if sheet_row is None:
        log.debug("gsheets delete_row_by_url: no sheets_row for %s — row may never have been pushed", row_id[:8])
        return False

    try:
        await asyncio.to_thread(delete_sheet_row, _get_service(), _sheet_id(), sheet_row)
        log.info("gsheets delete_row_by_url: deleted sheet row %d for id=%s", sheet_row, row_id[:8])
        return True
    except Exception as e:
        log.warning("gsheets delete_row_by_url: failed for id=%s: %s", row_id[:8], e)
        return False


# ---------------------------------------------------------------------------
# Push missing rows (DB → Sheets, skipping rows already there)
# ---------------------------------------------------------------------------

async def push_missing_rows() -> dict:
    """Append DB rows that are absent from Google Sheets (by ID).

    Returns: {"pushed": int, "already_present": int, "errors": list[str]}
    Used by /gsheets_push_missing command.
    """
    if not _ready():
        return {"pushed": 0, "already_present": 0, "errors": ["Sheets not ready"]}

    from hunter.gsheets_client import read_all, append_rows

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
    # Persist sheets_row for rows we already know about
    for sheet_row_num, row in sheets_rows:
        row_id = row.get("ID", "").strip()
        if row_id:
            set_sheets_row(row_id, sheet_row_num)

    # 2. Find DB rows absent from Sheets
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
                set_sheets_row(row_id, sheet_row)
                mark_sheets_clean(row_id)
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
    dirty_count = await asyncio.to_thread(get_dirty_sheets_count)
    sid = _sheet_id()
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sid}" if sid else None
    return {
        "enabled": GSHEETS_ENABLED,
        "sheet_id": sid or None,
        "sheet_url": sheet_url,
        "dirty_count": dirty_count,
        "service_ok": _service is not None,
    }
