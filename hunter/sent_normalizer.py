"""
Normalize the messy ``Sent`` column into a clean date column on the Google Sheet.

The ``Sent`` column mixes application dates (in many formats) with free-text notes
("выгасла", "повторка", "EXPIRED", ...). This module reads every row, extracts a real
application date where one exists (see :mod:`hunter.sent_parse`), and writes it — as a
*real date* — into a dedicated column to the right of the synced area:

    Column L  "Applied Date"   →  2026-05-13   (blank = not applied)

Why column L: the bot only ever syncs columns A–K with the Sheet (see
``gsheets_client.COLUMNS``), so L is never touched by the normal push/pull. The clean
date therefore survives every sync, and a stats tab can simply COUNT / QUERY column L.

The original ``Sent`` column is left completely untouched — this only writes column L.

Used by:
  - ``tools/normalize_sent.py``                 (CLI, dry-run by default)
  - ``hunter/schedules/normalize_sent.py``      (daily auto-refresh)
  - ``hunter/commands/normalize.py``            (/normalize on demand)
"""

import asyncio
from typing import Any

from hunter.gsheets_client import read_all
from hunter.sent_parse import parse_sent_date

# Column to the right of the synced A–K area.
APPLIED_COL = "L"
APPLIED_HEADER = "Applied Date"


def build_column(rows: list[tuple[int, dict]]) -> tuple[list[list[str]], int]:
    """
    Build the L2:L<N> value grid (one cell per data row, in sheet order).

    Returns ``(grid, filled_count)``. Each cell is an ISO date string or "".
    Assumes ``read_all`` returns contiguous sheet rows starting at row 2.
    """
    grid: list[list[str]] = []
    filled = 0
    for _idx, row in rows:
        d = parse_sent_date(row.get("Sent", ""))
        if d is not None:
            grid.append([d.isoformat()])
            filled += 1
        else:
            grid.append([""])
    return grid, filled


def write_column(service: Any, sheet_id: str, grid: list[list[str]], tab: str = "Tracker") -> None:
    """Write the header into L1 and the dates into L2:L<N> in two calls.

    ``valueInputOption=USER_ENTERED`` so the ISO strings land as real Sheets dates
    (enabling COUNT / YEAR() / MONTH() / QUERY on column L).
    """
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!{APPLIED_COL}1",
        valueInputOption="RAW",
        body={"values": [[APPLIED_HEADER]]},
    ).execute()
    if not grid:
        return
    last_row = len(grid) + 1  # +1 for the header row
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!{APPLIED_COL}2:{APPLIED_COL}{last_row}",
        valueInputOption="USER_ENTERED",
        body={"values": grid},
    ).execute()


def normalize_sheet(service: Any, sheet_id: str, tab: str = "Tracker") -> dict:
    """Read the tab, parse Sent → date, write column L. Returns a small summary.

    Synchronous (blocking Sheets I/O); async callers should wrap in
    ``asyncio.to_thread`` (see :func:`normalize_sheet_async`).
    """
    rows = read_all(service, sheet_id, tab=tab)
    grid, filled = build_column(rows)
    write_column(service, sheet_id, grid, tab=tab)
    return {"rows": len(rows), "filled": filled}


async def normalize_sheet_async() -> dict:
    """Async wrapper used by the scheduled callback and /normalize command.

    Returns ``{"enabled": False}`` when Google Sheets is disabled/unconfigured,
    else ``{"enabled": True, "rows": N, "filled": M}``.
    """
    from hunter import gsheets_sync

    if not gsheets_sync._ready():
        return {"enabled": False, "rows": 0, "filled": 0}
    service = gsheets_sync._get_service()
    sheet_id = gsheets_sync._sheet_id()
    result = await asyncio.to_thread(normalize_sheet, service, sheet_id)
    result["enabled"] = True
    return result
