"""
Low-level Google Sheets API v4 wrapper.

All public functions accept a built `service` object (from build_service()) so
callers can inject mocks in tests. No global state.

Column order matches tracker.xlsx schema (11 columns, A-K):
  A=Date, B=Company, C=Job Title, D=Stack, E=ATS%, F=URL,
  G=Folder, H=Sent, I=Re-application, J=To Learn, K=ID
"""

import logging
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

COLUMNS = ["Date", "Company", "Job Title", "Stack", "ATS %", "URL",
           "Folder", "Sent", "Re-application", "To Learn", "ID"]
COL_COUNT = len(COLUMNS)  # 11


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def build_service(credentials_file: Path, token_file: Path) -> Any:
    """Load credentials and return a Sheets API service object."""
    creds = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json())
        else:
            raise RuntimeError(
                f"gsheets_token.json is missing or invalid. "
                f"Run: python tools/gsheets_auth.py"
            )

    return build("sheets", "v4", credentials=creds)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _range(tab: str, start_row: int | None = None, end_row: int | None = None) -> str:
    """Return an A1 range string for the full tab or a specific row range."""
    col_end = chr(ord("A") + COL_COUNT - 1)  # "K"
    if start_row is None:
        return f"'{tab}'!A:{col_end}"
    if end_row is None:
        return f"'{tab}'!A{start_row}:{col_end}{start_row}"
    return f"'{tab}'!A{start_row}:{col_end}{end_row}"


def _row_to_list(row: dict) -> list[str]:
    """Convert a row dict (column names as keys) to an ordered list of strings."""
    return [str(row.get(col, "") or "") for col in COLUMNS]


def _list_to_row(values: list[str]) -> dict:
    """Convert an API row (list of strings) to a named dict."""
    padded = values + [""] * (COL_COUNT - len(values))
    return {col: padded[i] for i, col in enumerate(COLUMNS)}


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def read_all(service: Any, sheet_id: str, tab: str = "Tracker") -> list[tuple[int, dict]]:
    """
    Read all data rows (excluding header).

    Returns list of (sheet_row_index, row_dict) where sheet_row_index is
    1-based (row 1 = header, row 2 = first data row).
    """
    try:
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=_range(tab))
            .execute()
        )
    except HttpError as e:
        log.error("gsheets read_all failed: %s", e)
        raise

    raw_rows: list[list[str]] = result.get("values", [])
    if not raw_rows:
        return []

    # Skip header row (index 0 in raw_rows = sheet row 1)
    data: list[tuple[int, dict]] = []
    for i, values in enumerate(raw_rows[1:], start=2):  # sheet rows start at 1
        data.append((i, _list_to_row(values)))
    return data


# ---------------------------------------------------------------------------
# Write — append
# ---------------------------------------------------------------------------

def append_rows(
    service: Any,
    sheet_id: str,
    rows: list[dict],
    tab: str = "Tracker",
) -> list[int]:
    """
    Append rows to the end of the sheet.

    Returns list of 1-based sheet row indices for the appended rows.
    """
    if not rows:
        return []

    values = [_row_to_list(r) for r in rows]
    try:
        result = (
            service.spreadsheets()
            .values()
            .append(
                spreadsheetId=sheet_id,
                range=f"'{tab}'!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            )
            .execute()
        )
    except HttpError as e:
        log.error("gsheets append_rows failed: %s", e)
        raise

    # Parse inserted range e.g. "Tracker!A5:K7"
    updated_range: str = result.get("updates", {}).get("updatedRange", "")
    start_row = _parse_start_row(updated_range)
    return list(range(start_row, start_row + len(rows)))


def _parse_start_row(range_str: str) -> int:
    """Extract the starting row number from a range like "Tracker!A5:K7"."""
    try:
        cell_part = range_str.split("!")[1]  # "A5:K7"
        start_cell = cell_part.split(":")[0]  # "A5"
        return int("".join(c for c in start_cell if c.isdigit()))
    except Exception:
        log.warning("Could not parse row from range %r", range_str)
        return -1


# ---------------------------------------------------------------------------
# Write — update existing rows/cells
# ---------------------------------------------------------------------------

def update_row(
    service: Any,
    sheet_id: str,
    row_idx: int,
    row: dict,
    tab: str = "Tracker",
) -> None:
    """Overwrite an entire row (all columns) at the given 1-based row index."""
    values = [_row_to_list(row)]
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=_range(tab, row_idx),
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
    except HttpError as e:
        log.error("gsheets update_row(%d) failed: %s", row_idx, e)
        raise


def update_cell(
    service: Any,
    sheet_id: str,
    row_idx: int,
    col_name: str,
    value: str,
    tab: str = "Tracker",
) -> None:
    """Update a single cell identified by 1-based row index and column name."""
    if col_name not in COLUMNS:
        raise ValueError(f"Unknown column: {col_name!r}. Valid: {COLUMNS}")
    col_letter = chr(ord("A") + COLUMNS.index(col_name))
    cell = f"'{tab}'!{col_letter}{row_idx}"
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=cell,
            valueInputOption="RAW",
            body={"values": [[str(value)]]},
        ).execute()
    except HttpError as e:
        log.error("gsheets update_cell(%d, %s) failed: %s", row_idx, col_name, e)
        raise


# ---------------------------------------------------------------------------
# Create spreadsheet
# ---------------------------------------------------------------------------

def create_spreadsheet(service: Any, title: str) -> str:
    """Create a new spreadsheet with a 'Tracker' tab and a bold frozen header.

    Returns the spreadsheet ID.
    """
    try:
        spreadsheet = (
            service.spreadsheets()
            .create(
                body={
                    "properties": {"title": title},
                    "sheets": [{"properties": {"title": "Tracker"}}],
                }
            )
            .execute()
        )
    except HttpError as e:
        log.error("gsheets create_spreadsheet failed: %s", e)
        raise

    sheet_id = spreadsheet["spreadsheetId"]
    tab_sheet_id = spreadsheet["sheets"][0]["properties"]["sheetId"]

    # Write header row
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range="'Tracker'!A1:K1",
        valueInputOption="RAW",
        body={"values": [COLUMNS]},
    ).execute()

    # Format: bold + freeze row 1
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={
            "requests": [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": tab_sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"bold": True}
                            }
                        },
                        "fields": "userEnteredFormat.textFormat.bold",
                    }
                },
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": tab_sheet_id,
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                },
            ]
        },
    ).execute()

    log.info("Created spreadsheet %r id=%s", title, sheet_id)
    return sheet_id


# ---------------------------------------------------------------------------
# Batch write (migration / full upload)
# ---------------------------------------------------------------------------

def batch_write_all(
    service: Any,
    sheet_id: str,
    rows: list[dict],
    tab: str = "Tracker",
) -> None:
    """Write all rows in one API call (used for initial migration).

    Overwrites A2:K<N> — does not touch the header in row 1.
    """
    if not rows:
        return
    values = [_row_to_list(r) for r in rows]
    end_row = len(rows) + 1  # +1 because row 1 is header
    range_str = f"'{tab}'!A2:K{end_row}"
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=range_str,
            valueInputOption="RAW",
            body={"values": values},
        ).execute()
    except HttpError as e:
        log.error("gsheets batch_write_all failed: %s", e)
        raise
