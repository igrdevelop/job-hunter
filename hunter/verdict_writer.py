"""Sheets writer for the per-vacancy `ATS Verdict` column.

Why a separate module
---------------------
Same reasoning as hunter.cost_writer (column M): the bot's main A–K push
(hunter.gsheets_client.COLUMNS) writes a contiguous range every time, and
column L is owned by ``sent_normalizer``. The independent PDF-verdict score
therefore lives in **column N**, written by this module only. Four
non-overlapping writers: A–K main push, L sent_normalizer, M cost_writer,
N verdict_writer — never racing for the same cell.

What the value is
-----------------
``applications.ats_verdict`` — the score from ONE judge-model (Haiku) call
over the text extracted from the rendered EN CV PDF (see
ats_pdf_roundtrip.run_llm_verdict). Stamped on the row post-hoc by
tracker.set_ats_verdict inside the apply subprocess; mirrored here from the
bot process when gsheets_sync.mirror_new_row runs (by then the value is
already in the DB). NULL means "no verdict" and renders as an empty cell.

Operations:
- ``mirror_verdict_cell_sync(service, sheet_id, row_id)`` — writes N{row}.
- ``write_verdict_header_sync(service, sheet_id)`` — one-shot N1 header.
- ``backfill_all_verdicts_sync(service, sheet_id)`` — bulk push for
  tools/sync_verdicts.py (one batchUpdate regardless of row count).

Pull is intentionally not implemented — the user doesn't edit the verdict in
the Sheet, so there is no "Sheet wins" conflict to merge.
"""

from __future__ import annotations

import logging
from typing import Any

from hunter.best_effort import best_effort
from hunter.config import TRACKER_DB_PATH
from hunter.db import get_db

# Re-bound at import time; tests monkeypatch this attribute to swap in a
# temp DB path (same pattern as hunter.cost_writer.DB_PATH).
DB_PATH = TRACKER_DB_PATH

log = logging.getLogger(__name__)

# One cell to the right of cost_writer's "Cost $" column (M). Hardcoded like
# its siblings — if the sheet ever grows beyond N, make these discovered.
VERDICT_COL_LETTER = "N"
VERDICT_HEADER = "ATS Verdict"

# Set once per process after the first successful header write (see
# cost_writer._header_written for the rationale).
_header_written: dict[str, bool] = {}


def _format_verdict(score: float) -> float | int:
    """Render 91.0 as 91 (int) and 88.5 as 88.5 — cleaner Sheet cells."""
    rounded = round(float(score), 1)
    return int(rounded) if rounded == int(rounded) else rounded


def _row_for(row_id: str) -> tuple[int | None, float | None]:
    """Return (sheet_row_index, ats_verdict) for the given application ID."""
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            "SELECT sheets_row, ats_verdict FROM applications WHERE id = ?",
            (row_id,),
        ).fetchone()
    if row is None:
        return None, None
    return row["sheets_row"], row["ats_verdict"]


def mirror_verdict_cell_sync(
    service: Any,
    sheet_id: str,
    row_id: str,
    tab: str = "Tracker",
) -> bool:
    """Blocking single-cell write of N{row} for one application.

    Returns True on success or no-op (nothing to write), False on Sheets
    error. Never raises — gsheets_sync expects best-effort mirrors.
    """
    sheet_row, verdict = _row_for(row_id)
    if sheet_row is None or verdict is None:
        # No sheets_row yet (A–K append hasn't happened) or no verdict
        # (feature disabled / no judge key / PDF unreadable). Nothing to write.
        return True
    if not _header_written.get(sheet_id) and write_verdict_header_sync(service, sheet_id, tab):
        _header_written[sheet_id] = True
    cell = f"'{tab}'!{VERDICT_COL_LETTER}{sheet_row}"
    ok = True
    with best_effort("verdict_writer.mirror_verdict_cell"):
        try:
            service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=cell,
                valueInputOption="RAW",
                body={"values": [[_format_verdict(verdict)]]},
            ).execute()
        except Exception as e:
            log.error("verdict_writer: failed to write N%s for %s: %s", sheet_row, row_id, e)
            ok = False
            raise
    return ok


def write_verdict_header_sync(
    service: Any,
    sheet_id: str,
    tab: str = "Tracker",
) -> bool:
    """Idempotent — writes N1 = "ATS Verdict". Safe to call repeatedly."""
    cell = f"'{tab}'!{VERDICT_COL_LETTER}1"
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=cell,
            valueInputOption="RAW",
            body={"values": [[VERDICT_HEADER]]},
        ).execute()
        return True
    except Exception as e:
        log.error("verdict_writer: failed to write header N1: %s", e)
        return False


def backfill_all_verdicts_sync(
    service: Any,
    sheet_id: str,
    tab: str = "Tracker",
) -> dict:
    """Push every judged row's verdict into column N in a single batchUpdate.

    Used by tools/sync_verdicts.py for one-shot bulk sync (rows whose live
    mirror failed, e.g. Sheets token down at apply time, heal here).

    Returns {"written": N, "skipped_no_row": N, "skipped_no_verdict": N}.
    """
    with get_db(DB_PATH) as conn:
        rows = conn.execute("SELECT id, sheets_row, ats_verdict FROM applications").fetchall()

    data = []
    no_row = 0
    no_verdict = 0
    for r in rows:
        if r["sheets_row"] is None:
            no_row += 1
            continue
        if r["ats_verdict"] is None:
            no_verdict += 1
            continue
        data.append(
            {
                "range": f"'{tab}'!{VERDICT_COL_LETTER}{r['sheets_row']}",
                "values": [[_format_verdict(r["ats_verdict"])]],
            }
        )

    # Header always — pin it before any data write so a fresh sheet gets the
    # column labelled even when there are no judged rows yet.
    data.insert(
        0,
        {
            "range": f"'{tab}'!{VERDICT_COL_LETTER}1",
            "values": [[VERDICT_HEADER]],
        },
    )

    if len(data) == 1:
        write_verdict_header_sync(service, sheet_id, tab)
        return {"written": 0, "skipped_no_row": no_row, "skipped_no_verdict": no_verdict}

    try:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "RAW", "data": data},
        ).execute()
    except Exception as e:
        log.error("verdict_writer: batchUpdate failed: %s", e)
        raise
    return {
        "written": len(data) - 1,
        "skipped_no_row": no_row,
        "skipped_no_verdict": no_verdict,
    }
