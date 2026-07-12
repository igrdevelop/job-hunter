"""Sheets writer for the per-vacancy `Cost $` column.

Why a separate module
---------------------
The bot's main A–K push (hunter.gsheets_client.COLUMNS) writes a contiguous
range every time. Adding cost to COLUMNS would extend that range to A–M and
**overwrite column L** on every push — but column L already holds
``sent_normalizer``'s "Applied Date" cell-values that aren't tracked in the
DB. Clobbering L would silently destroy the user's stats tab.

The fix: cost lives in **column M**, written by this module only. The main
push touches A–K, ``sent_normalizer`` touches L, this module touches M —
three non-overlapping writers, never racing for the same cell.

Operations:
- ``mirror_cost_cell(row_id)`` — async, called from gsheets_sync.mirror_new_row
  right after the A–K append succeeds. Reads ``cost_usd`` from the DB and
  writes M{row}.
- ``write_cost_header(service, sheet_id, tab)`` — one-shot, writes
  ``M1 = "Cost $"`` so any new spreadsheet gets a labelled header.
- ``backfill_all_costs(service, sheet_id, tab)`` — used by /sync_costs to
  push every row's cost_usd in one pass. Read costs+sheets_rows in one DB
  query, then batchUpdate against the Sheets API (one HTTP call regardless
  of row count).

Pull is intentionally not implemented — the user doesn't edit Cost $ in the
Sheet, so there is no "Sheet wins" conflict to merge.
"""

from __future__ import annotations

import logging
from typing import Any

from hunter.config import TRACKER_DB_PATH
from hunter.db import get_db
from hunter.tracker import _format_cost

# Re-bound at import time; tests monkeypatch this attribute to swap in a
# temp DB path (same pattern as hunter.tracker.DB_PATH).
DB_PATH = TRACKER_DB_PATH

log = logging.getLogger(__name__)

# Column for cost lives one cell to the right of `sent_normalizer`'s
# "Applied Date" column (L). Hardcoded to M for now — if the sheet ever
# grows beyond M we'll need to make this a config / discovered value.
COST_COL_LETTER = "M"
COST_HEADER = "Cost $"

# Set once per process after the first successful header write so we don't
# spam M1 on every mirror. Cleared on import / restart — first mirror after
# bot startup rewrites the header, which is cheap and idempotent and ensures
# a freshly-created spreadsheet always ends up labelled.
_header_written: dict[str, bool] = {}


def _row_for(row_id: str) -> tuple[int | None, float | None]:
    """Return (sheet_row_index, cost_usd) for the given application ID."""
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            "SELECT sheets_row, cost_usd FROM applications WHERE id = ?",
            (row_id,),
        ).fetchone()
    if row is None:
        return None, None
    return row["sheets_row"], row["cost_usd"]


def mirror_cost_cell_sync(
    service: Any,
    sheet_id: str,
    row_id: str,
    tab: str = "Tracker",
) -> bool:
    """Blocking variant — used inside asyncio.to_thread by the async wrapper.

    Returns True on success or no-op (nothing to write), False on Sheets error.
    Never raises — gsheets_sync expects best-effort behaviour from mirrors.
    """
    sheet_row, cost_usd = _row_for(row_id)
    if sheet_row is None or cost_usd is None:
        # No sheets_row yet (A–K append hasn't happened) or no measured cost
        # (CLI run, pre-tracking row). Either way, nothing to write.
        return True
    # First write in this process for this sheet: also ensure the header is
    # set. Idempotent — overwriting "Cost $" with "Cost $" is harmless.
    if not _header_written.get(sheet_id):
        if write_cost_header_sync(service, sheet_id, tab):
            _header_written[sheet_id] = True
    cell = f"'{tab}'!{COST_COL_LETTER}{sheet_row}"
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=cell,
            valueInputOption="RAW",
            body={"values": [[_format_cost(cost_usd)]]},
        ).execute()
        return True
    except Exception as e:
        log.error("cost_writer: failed to write M%s for %s: %s", sheet_row, row_id, e)
        return False


def write_cost_header_sync(
    service: Any,
    sheet_id: str,
    tab: str = "Tracker",
) -> bool:
    """Idempotent — writes M1 = "Cost $". Safe to call repeatedly.

    The main bootstrap path doesn't know about column M; we set the header
    lazily on the first cost write (see mirror_cost_cell).
    """
    cell = f"'{tab}'!{COST_COL_LETTER}1"
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=cell,
            valueInputOption="RAW",
            body={"values": [[COST_HEADER]]},
        ).execute()
        return True
    except Exception as e:
        log.error("cost_writer: failed to write header M1: %s", e)
        return False


def backfill_all_costs_sync(
    service: Any,
    sheet_id: str,
    tab: str = "Tracker",
) -> dict:
    """Push every priced row's cost into column M in a single batchUpdate.

    Used by tools/sync_costs.py and the /sync_costs Telegram command for
    one-shot bulk sync (e.g. right after this PR ships, so historical rows
    get their existing cost_usd surfaced).

    Returns {"written": N, "skipped_no_row": N, "skipped_no_cost": N}.
    Skipped tallies cover rows that have no sheets_row yet (never pushed)
    and rows with NULL cost_usd (CLI runs / pre-tracking).
    """
    with get_db(DB_PATH) as conn:
        rows = conn.execute("SELECT id, sheets_row, cost_usd FROM applications").fetchall()

    data = []
    no_row = 0
    no_cost = 0
    for r in rows:
        if r["sheets_row"] is None:
            no_row += 1
            continue
        if r["cost_usd"] is None:
            no_cost += 1
            continue
        data.append(
            {
                "range": f"'{tab}'!{COST_COL_LETTER}{r['sheets_row']}",
                "values": [[_format_cost(r["cost_usd"])]],
            }
        )

    # Header always — pin it before any data write so a fresh sheet gets
    # the column labelled even when there are no priced rows yet.
    data.insert(
        0,
        {
            "range": f"'{tab}'!{COST_COL_LETTER}1",
            "values": [[COST_HEADER]],
        },
    )

    if len(data) == 1:
        # Only the header — still worth writing (one API call), but skip the
        # values.batchUpdate complexity.
        write_cost_header_sync(service, sheet_id, tab)
        return {"written": 0, "skipped_no_row": no_row, "skipped_no_cost": no_cost}

    try:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
    except Exception as e:
        log.error("cost_writer: batchUpdate failed: %s", e)
        raise
    return {"written": len(data) - 1, "skipped_no_row": no_row, "skipped_no_cost": no_cost}
