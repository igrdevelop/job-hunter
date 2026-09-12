"""Sheets writer for the per-application `Outcome` column (column O).

Why a separate module
---------------------
Same reasoning as hunter.cost_writer (M) and hunter.verdict_writer (N): the main
A–K push (hunter.gsheets_client.COLUMNS) overwrites a contiguous range on every
dirty-row resync, so a column outside it must have its own writer or the push
would blank it. Five non-overlapping writers: A–K main push, L sent_normalizer,
M cost_writer, N verdict_writer, O outcome_writer.

What is different from M and N
------------------------------
The outcome is WORKFLOW state the owner edits in the Sheet, so unlike the cost
and the verdict it round-trips:

- Push: ``gsheets_sync.resync_dirty`` writes O for every dirty row that carries
  a label, and ``gsheets_sync.mirror_outcome`` writes it straight after
  /outcome. A failed O write keeps the row dirty (the A–K push alone is not
  enough to call the row synced).
- Pull: ``gsheets_sync._merge_outcomes`` reads O through
  :func:`parse_outcome_cell` — a blank cell never clears the DB, a cell that is
  not one of ``tracker.OUTCOME_LABELS`` is logged and ignored.

Functions here only talk to the Sheets API; the callers own the DB reads, so the
dirty-row resync needs no extra query per row.
"""

from __future__ import annotations

import logging
from typing import Any

from hunter.gsheets_client import (
    COLUMNS,
    OUTCOME_COL_INDEX,
    OUTCOME_COL_LETTER,
    get_tab_sheet_id,
)
from hunter.tracker import OUTCOME_LABELS

log = logging.getLogger(__name__)

OUTCOME_HEADER = "Outcome"

# Set once per process after the header + dropdown landed (see
# cost_writer._header_written for the rationale).
_column_ready: dict[str, bool] = {}


def parse_outcome_cell(value: object) -> str | None:
    """Normalise a column-O cell read from the Sheet.

    Returns the label for a valid cell (case/whitespace-insensitive), "" for a
    blank cell, and None for anything else — the caller logs and skips it.
    """
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text in OUTCOME_LABELS else None


def ensure_outcome_column_sync(service: Any, sheet_id: str, tab: str = "Tracker") -> bool:
    """Write the O1 header and a dropdown of OUTCOME_LABELS on O2:O. Idempotent.

    Runs once per process per spreadsheet. The header matters more than the
    dropdown: a failed dropdown is logged and does not block cell writes.
    """
    if _column_ready.get(sheet_id):
        return True
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!{OUTCOME_COL_LETTER}1",
            valueInputOption="RAW",
            body={"values": [[OUTCOME_HEADER]]},
        ).execute()
    except Exception as e:
        log.error("outcome_writer: failed to write header O1: %s", e)
        return False

    try:
        tab_sheet_id = get_tab_sheet_id(service, sheet_id, tab)
        if tab_sheet_id is not None:
            service.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id,
                body={"requests": [_dropdown_request(tab_sheet_id)]},
            ).execute()
    except Exception as e:
        log.warning("outcome_writer: could not set the O dropdown (non-fatal): %s", e)

    _column_ready[sheet_id] = True
    return True


def _dropdown_request(tab_sheet_id: int) -> dict:
    """setDataValidation for O2:O — a ONE_OF_LIST dropdown, blank allowed."""
    return {
        "setDataValidation": {
            "range": {
                "sheetId": tab_sheet_id,
                "startRowIndex": 1,  # skip the header
                "startColumnIndex": OUTCOME_COL_INDEX,
                "endColumnIndex": OUTCOME_COL_INDEX + 1,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": label} for label in OUTCOME_LABELS],
                },
                "strict": True,
                "showCustomUi": True,
            },
        }
    }


def write_outcome_cell_sync(
    service: Any,
    sheet_id: str,
    sheet_row: int | None,
    label: str,
    tab: str = "Tracker",
    *,
    allow_blank: bool = False,
    expect_id: str | None = None,
) -> bool:
    """Write O{sheet_row} = label. Returns True when a cell was written.

    ``expect_id``: read K{sheet_row} first and write only if it still holds this
    ID. The DB's cached sheets_row goes stale when rows above it are deleted in
    the Sheet (until the next pull re-reads positions); a blind write would put
    the label next to a DIFFERENT application, and the next pull would copy it
    into that application's DB row. The resync skips this check because it
    rewrites the whole A–K row at that position in the same pass.

    Raises on a Sheets error: the dirty-row resync relies on that to keep the
    row dirty. A blank label is skipped unless ``allow_blank`` — during a resync
    an empty DB value most likely means "never recorded", and writing "" would
    wipe a label the owner just typed into the Sheet before the pull saw it.
    Only an explicit ``/outcome <id> clear`` blanks the cell.
    """
    label = (label or "").strip()
    if sheet_row is None or sheet_row < 2:
        return False
    if not label and not allow_blank:
        return False
    if expect_id is not None:
        id_col = chr(ord("A") + COLUMNS.index("ID"))
        got = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=f"'{tab}'!{id_col}{sheet_row}")
            .execute()
        )
        cells = got.get("values") or [[""]]
        found = str((cells[0] or [""])[0]).strip()
        if found != expect_id:
            log.warning(
                "outcome_writer: row %d holds id %r, not %r — sheets_row is stale, "
                "leaving O to the next resync",
                sheet_row,
                found,
                expect_id,
            )
            return False
    ensure_outcome_column_sync(service, sheet_id, tab)
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!{OUTCOME_COL_LETTER}{sheet_row}",
        valueInputOption="RAW",
        body={"values": [[label]]},
    ).execute()
    return True
