"""
tracker.py — read/write tracker.xlsx for the hunter.

Responsibilities:
  - get_known_urls()     → set of URLs already in tracker (for dedup)
  - add_skipped(job)     → append a row with status "SKIP"
  - add_applied(...)     → generate_docs.py handles the "applied" row,
                           but this module owns the skip path
"""

import re
import time
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

from hunter.config import TRACKER_PATH
from hunter.models import Job

TRACKER_HEADERS = [
    "Date", "Company", "Job Title", "Stack",
    "ATS %", "URL", "Folder", "Sent", "Re-application", "To Learn",
]
URL_COL_INDEX = 5       # 1-based column index for "URL" in tracker (column F)
COMPANY_COL_INDEX = 2   # "Company"
TITLE_COL_INDEX = 3     # "Job Title"


def dedup_key(company: str, title: str) -> str:
    """Normalized key for company+title dedup (cross-source, cross-URL)."""
    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r'\b(sp\.?\s*z\.?\s*o\.?\s*o\.?|s\.a\.|ltd\.?|gmbh|inc\.?)\b', '', s)
        s = re.sub(r'[^a-z0-9]', '', s)
        return s
    return _norm(company) + "|" + _norm(title)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _save_with_retry(wb: openpyxl.Workbook, retries: int = 5, delay: float = 3.0) -> None:
    """Save workbook, retrying on PermissionError (file open in Excel)."""
    for attempt in range(1, retries + 1):
        try:
            wb.save(TRACKER_PATH)
            return
        except PermissionError:
            if attempt == retries:
                raise
            print(
                f"[tracker] tracker.xlsx is locked (Excel open?). "
                f"Retry {attempt}/{retries} in {delay}s..."
            )
            time.sleep(delay)


def _load_or_create() -> tuple[openpyxl.Workbook, openpyxl.worksheet.worksheet.Worksheet]:
    if TRACKER_PATH.exists():
        wb = openpyxl.load_workbook(TRACKER_PATH)
        ws = wb.active
        return wb, ws

    # Create fresh tracker with header row
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Applications"

    header_fill = PatternFill("solid", fgColor="2B579A")
    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    widths = [12, 20, 30, 12, 8, 50, 40, 8, 16, 35]

    for col, (header, width) in enumerate(zip(TRACKER_HEADERS, widths), 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = width

    ws.row_dimensions[1].height = 20
    ws.freeze_panes = "A2"
    _save_with_retry(wb)
    return wb, ws


# ── Public API ────────────────────────────────────────────────────────────────

def get_known_urls() -> set[str]:
    """Return all URLs stored in tracker — used for deduplication."""
    if not TRACKER_PATH.exists():
        return set()

    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    urls = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and len(row) >= URL_COL_INDEX:
            val = row[URL_COL_INDEX - 1]  # 0-based
            if val:
                urls.add(str(val).strip())
    wb.close()
    return urls


def get_known_company_titles() -> set[str]:
    """Return dedup_key(company, title) for all rows in tracker."""
    if not TRACKER_PATH.exists():
        return set()

    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    keys = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and len(row) >= TITLE_COL_INDEX:
            company = str(row[COMPANY_COL_INDEX - 1] or "").strip()
            title = str(row[TITLE_COL_INDEX - 1] or "").strip()
            if company and title:
                keys.add(dedup_key(company, title))
    wb.close()
    return keys


def add_skipped(job: Job) -> None:
    """Append a SKIP row to tracker so the job is never shown again."""
    wb, ws = _load_or_create()
    today = date.today().strftime("%Y-%m-%d")
    next_row = ws.max_row + 1

    values = [
        today,           # Date
        job.company,     # Company
        job.title,       # Job Title
        "",              # Stack  (unknown at this point)
        "SKIP",          # ATS %  (repurposed for status)
        job.url,         # URL
        "",              # Folder
        "",              # Sent
        "",              # Re-application
        "",              # To Learn
    ]

    row_font = Font(name="Calibri", size=11)
    skip_fill = PatternFill("solid", fgColor="D9D9D9")

    for col, val in enumerate(values, 1):
        cell = ws.cell(row=next_row, column=col, value=val)
        cell.font = row_font
        cell.fill = skip_fill
        if col == URL_COL_INDEX:
            cell.hyperlink = job.url
            cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")

    _save_with_retry(wb)


def add_failed(job: Job) -> None:
    """Append a FAIL row so the job is not retried on next hunt.
    User can delete the row from Excel to retry manually."""
    wb, ws = _load_or_create()
    today = date.today().strftime("%Y-%m-%d")
    next_row = ws.max_row + 1

    values = [
        today,           # Date
        job.company,     # Company
        job.title,       # Job Title
        "",              # Stack
        "FAIL",          # ATS %  (repurposed for status)
        job.url,         # URL
        "",              # Folder
        "",              # Sent
        "",              # Re-application
        "",              # To Learn
    ]

    row_font = Font(name="Calibri", size=11)
    fail_fill = PatternFill("solid", fgColor="F4CCCC")  # light red

    for col, val in enumerate(values, 1):
        cell = ws.cell(row=next_row, column=col, value=val)
        cell.font = row_font
        cell.fill = fail_fill
        if col == URL_COL_INDEX:
            cell.hyperlink = job.url
            cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")

    _save_with_retry(wb)
