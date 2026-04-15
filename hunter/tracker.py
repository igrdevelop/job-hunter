"""
tracker.py — read/write tracker.xlsx for the hunter.

Responsibilities:
  - get_known_urls()     → set of URLs already in tracker (for dedup)
  - add_skipped(job)     → append a row with status "SKIP"
  - add_applied(...)     → append a successful generated-docs row
"""

import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

from hunter.config import TRACKER_PATH
from hunter.models import Job

TRACKER_HEADERS = [
    "Date", "Company", "Job Title", "Stack",
    "ATS %", "URL", "Folder", "Sent", "Re-application", "To Learn",
]
# Columns: 1 Date, 2 Company, 3 Job Title, 4 Stack, 5 ATS %, 6 URL, 7 Folder, 8 Sent, ...
URL_COL_INDEX = 6       # "URL" (was wrongly 5 — that is ATS %, broke URL dedup)
COMPANY_COL_INDEX = 2   # "Company"
TITLE_COL_INDEX = 3     # "Job Title"
ATS_COL_INDEX = 5       # "ATS %" - also used for status (FAIL, SKIP)
REACT_SKIP_SENT_MARKERS = {"—", "–", "-"}


def normalize_url(url: str) -> str:
    """Canonical form: lowercase host, strip trailing slash, drop tracking params."""
    url = url.strip()
    if not url:
        return url
    p = urlparse(url)
    host = (p.hostname or "").lower()
    path = p.path.rstrip("/") or "/"
    # Drop common tracking query params
    drop_params = {
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "ref", "refId", "trackingId", "trk", "fbclid", "gclid",
        "originToLandingJobPostings", "origin",
        # Pracuj.pl tracking params (email alerts, suggested jobs)
        "sendid", "send_date", "sug",
    }
    qs = parse_qs(p.query, keep_blank_values=False)
    clean_qs = {k: v for k, v in qs.items() if k not in drop_params}
    query = urlencode(clean_qs, doseq=True) if clean_qs else ""
    # For LinkedIn /jobs/view/{id}/ — keep only path
    if "linkedin.com" in host and "/jobs/view/" in path:
        m = re.search(r"/jobs/view/(\d+)", path)
        if m:
            path = f"/jobs/view/{m.group(1)}"
            query = ""
    return urlunparse((p.scheme, host, path, "", query, ""))


def normalize_company(company: str) -> str:
    """Normalized company name for dedup."""
    s = company.lower()
    s = re.sub(r'_?\d{4}-\d{2}-\d{2}(_\d+)?$', '', s)
    s = re.sub(r'\b(sp\.?\s*z\.?\s*o\.?\s*o\.?|s\.a\.|ltd\.?|gmbh|inc\.?)\b', '', s)
    s = re.sub(r'[^a-z0-9]', '', s)
    return s


def dedup_key(company: str, title: str) -> str:
    """Normalized key for company+title dedup (cross-source, cross-URL)."""
    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r'_?\d{4}-\d{2}-\d{2}(_\d+)?$', '', s)
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
    """Return all normalized URLs stored in tracker — used for deduplication."""
    if not TRACKER_PATH.exists():
        return set()

    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    urls = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and len(row) >= URL_COL_INDEX:
            val = row[URL_COL_INDEX - 1]  # 0-based
            if val:
                urls.add(normalize_url(str(val)))
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


SENT_COL_INDEX = 8  # "Sent" column (1-based)


def get_sent_companies() -> set[str]:
    """Return normalized company names for rows that have Sent info."""
    if not TRACKER_PATH.exists():
        return set()

    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    companies = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < SENT_COL_INDEX:
            continue
        sent = str(row[SENT_COL_INDEX - 1] or "").strip()
        company = str(row[COMPANY_COL_INDEX - 1] or "").strip()
        if sent and company:
            companies.add(normalize_company(company))
    wb.close()
    return companies


def company_matches_sent(comp_key: str, sent_companies: set[str]) -> bool:
    """Fuzzy company match: 'scalo' matches 'scalowroclaw' and vice versa.

    True if comp_key is a substring of any sent company, or any sent company
    is a substring of comp_key. Minimum 3 chars to avoid false positives.
    """
    if not comp_key or len(comp_key) < 3:
        return False
    if comp_key in sent_companies:
        return True
    for sc in sent_companies:
        if len(sc) < 3:
            continue
        if comp_key in sc or sc in comp_key:
            return True
    return False


def get_failed_jobs() -> list[Job]:
    """Return Job objects for all rows with ATS status 'FAIL'."""
    if not TRACKER_PATH.exists():
        return []

    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    jobs = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < URL_COL_INDEX:
            continue
        ats = str(row[ATS_COL_INDEX - 1] or "").strip()
        if ats != "FAIL":
            continue
        company = str(row[COMPANY_COL_INDEX - 1] or "").strip()
        title = str(row[TITLE_COL_INDEX - 1] or "").strip()
        url = str(row[URL_COL_INDEX - 1] or "").strip()
        if url:
            jobs.append(Job(
                title=title,
                company=company,
                location="",
                salary=None,
                url=url,
                source="retry",
            ))
    wb.close()
    return jobs


def remove_failed(url: str) -> None:
    """Remove a FAIL row from tracker (so it can be re-added as a proper entry)."""
    if not TRACKER_PATH.exists():
        return

    wb = openpyxl.load_workbook(TRACKER_PATH)
    ws = wb.active

    norm = normalize_url(url)
    rows_to_delete = []
    for row_num in range(2, ws.max_row + 1):
        ats = str(ws.cell(row=row_num, column=ATS_COL_INDEX).value or "").strip()
        row_url = str(ws.cell(row=row_num, column=URL_COL_INDEX).value or "").strip()
        if ats == "FAIL" and normalize_url(row_url) == norm:
            rows_to_delete.append(row_num)

    for row_num in reversed(rows_to_delete):
        ws.delete_rows(row_num)

    if rows_to_delete:
        _save_with_retry(wb)
    else:
        wb.close()


def has_successful_entry(url: str) -> bool:
    """True if tracker has a non-FAIL, non-SKIP entry for this URL (= docs were generated)."""
    return get_url_status_flags(url)["has_success"]


def get_url_status_flags(url: str) -> dict[str, bool]:
    """Return status flags for URL: successful entry and React-only skip marker."""
    if not TRACKER_PATH.exists():
        return {"has_success": False, "is_react_skip": False}
    norm = normalize_url(url)
    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active

    has_success = False
    is_react_skip = False

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < URL_COL_INDEX:
            continue
        row_url = str(row[URL_COL_INDEX - 1] or "").strip()
        if not row_url or normalize_url(row_url) != norm:
            continue

        ats = str(row[ATS_COL_INDEX - 1] or "").strip().upper()
        sent = str(row[SENT_COL_INDEX - 1] or "").strip() if len(row) >= SENT_COL_INDEX else ""

        if ats not in ("FAIL", "SKIP", "?", ""):
            has_success = True
        elif ats == "SKIP" and sent in REACT_SKIP_SENT_MARKERS:
            is_react_skip = True

        if has_success and is_react_skip:
            break

    wb.close()
    return {"has_success": has_success, "is_react_skip": is_react_skip}


def lookup_url(url: str) -> list[dict]:
    """Find all tracker entries matching this URL (normalized). Returns row details."""
    if not TRACKER_PATH.exists():
        return []
    norm = normalize_url(url)
    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    results = []
    for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or len(row) < URL_COL_INDEX:
            continue
        row_url = str(row[URL_COL_INDEX - 1] or "").strip()
        if row_url and normalize_url(row_url) == norm:
            results.append({
                "row": row_num,
                "company": str(row[COMPANY_COL_INDEX - 1] or "").strip(),
                "title": str(row[TITLE_COL_INDEX - 1] or "").strip(),
                "ats": str(row[ATS_COL_INDEX - 1] or "").strip(),
                "folder": str(row[URL_COL_INDEX] or "").strip() if len(row) > URL_COL_INDEX else "",
                "sent": str(row[SENT_COL_INDEX - 1] or "").strip() if len(row) >= SENT_COL_INDEX else "",
            })
    wb.close()
    return results


def lookup_company(company: str) -> list[dict]:
    """Find all tracker entries matching this company (normalized). Returns row details."""
    if not TRACKER_PATH.exists():
        return []
    norm = normalize_company(company)
    if not norm:
        return []
    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    results = []
    for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
        if not row or len(row) < URL_COL_INDEX:
            continue
        row_company = str(row[COMPANY_COL_INDEX - 1] or "").strip()
        if row_company and normalize_company(row_company) == norm:
            results.append({
                "row": row_num,
                "company": row_company,
                "title": str(row[TITLE_COL_INDEX - 1] or "").strip(),
                "ats": str(row[ATS_COL_INDEX - 1] or "").strip(),
                "folder": str(row[URL_COL_INDEX] or "").strip() if len(row) > URL_COL_INDEX else "",
                "sent": str(row[SENT_COL_INDEX - 1] or "").strip() if len(row) >= SENT_COL_INDEX else "",
            })
    wb.close()
    return results


def is_known(url: str, company: str = "", title: str = "") -> bool:
    """Check if a job is already in tracker (by URL or company+title)."""
    known_urls = get_known_urls()
    if normalize_url(url) in known_urls:
        return True
    if company and title:
        known_ct = get_known_company_titles()
        if dedup_key(company, title) in known_ct:
            return True
    return False


def _company_from_content(content: dict) -> str:
    """Prefer explicit company_name; fall back to output folder name."""
    cn = (content.get("company_name") or "").strip()
    if cn:
        return cn
    folder_name = Path(str(content.get("output_folder") or "")).name
    # Remove collision suffixes: Company_2, Company_3...
    s = re.sub(r"_(\d+)$", "", folder_name)
    # Legacy flat structure: Company_YYYY-MM-DD
    m = re.search(r"^(.+)_[0-9]{4}-[0-9]{2}-[0-9]{2}$", s)
    return (m.group(1) if m else s) or "Unknown"


def _parse_ats_score(raw: str) -> tuple[str, int | None]:
    """Normalize ATS score for tracker display and optional color coding."""
    value = (raw or "").strip()
    if not value:
        return "", None

    # Support 10-point scales from LLM output: "8/10", "8.5 / 10".
    m10 = re.search(r"(\d{1,2}(?:[.,]\d+)?)\s*/\s*10\b", value)
    if m10:
        base = float(m10.group(1).replace(",", "."))
        score = max(0, min(int(round(base * 10)), 100))
        return f"{score}%", score

    # Support common LLM variants: "85", "85%", "score: 85/100"
    m = re.search(r"\d{1,3}", value)
    if not m:
        return value, None

    score = max(0, min(int(m.group(0)), 100))
    return f"{score}%", score


def add_applied(content: dict, force: bool = False) -> bool:
    """Append a successful apply row. Returns True when a row was written."""
    company = _company_from_content(content)
    job_title = str(content.get("job_title", "") or "")
    stack = str(content.get("stack", "") or "")
    apply_url = str(content.get("apply_url", "") or "")
    folder = str(content.get("output_folder", "") or "")
    to_learn = str(content.get("to_learn", "") or "")
    ats_raw = str(content.get("ats_score", "") or "")
    ats_display, ats_numeric = _parse_ats_score(ats_raw)
    today = date.today().strftime("%Y-%m-%d")
    norm_url = normalize_url(apply_url) if apply_url else ""

    # Keep historical behavior: do not duplicate successful rows unless force mode.
    if norm_url and has_successful_entry(apply_url) and not force:
        return False

    wb, ws = _load_or_create()

    is_reapply = any(
        normalize_url(str(row[URL_COL_INDEX - 1] or "")) == norm_url
        for row in ws.iter_rows(min_row=2, values_only=True)
        if norm_url and row and len(row) >= URL_COL_INDEX and row[URL_COL_INDEX - 1]
    )

    next_row = ws.max_row + 1
    row_font = Font(name="Calibri", size=11)
    values = [
        today,
        company,
        job_title,
        stack,
        ats_display,
        apply_url,
        folder,
        "",
        "+" if is_reapply else "",
        to_learn,
    ]

    for col, val in enumerate(values, 1):
        cell = ws.cell(row=next_row, column=col, value=val)
        cell.font = row_font
        if col == URL_COL_INDEX and val:
            cell.hyperlink = str(val)
            cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")
        if col == ATS_COL_INDEX and ats_display:
            cell.alignment = Alignment(horizontal="center")
            if ats_numeric is not None:
                if ats_numeric >= 80:
                    cell.fill = PatternFill("solid", fgColor="C6EFCE")
                    cell.font = Font(name="Calibri", size=11, color="276221", bold=True)
                elif ats_numeric >= 60:
                    cell.fill = PatternFill("solid", fgColor="FFEB9C")
                    cell.font = Font(name="Calibri", size=11, color="9C6500", bold=True)
                else:
                    cell.fill = PatternFill("solid", fgColor="FFC7CE")
                    cell.font = Font(name="Calibri", size=11, color="9C0006", bold=True)
        if col in (SENT_COL_INDEX, 9):
            cell.alignment = Alignment(horizontal="center")

    # Alternate row color for readability.
    if next_row % 2 == 0:
        fill = PatternFill("solid", fgColor="EEF2FA")
        for col in range(1, len(TRACKER_HEADERS) + 1):
            ws.cell(row=next_row, column=col).fill = fill

    _save_with_retry(wb)
    return True


def add_skipped(job: Job) -> None:
    """Append a SKIP row to tracker so the job is never shown again."""
    if is_known(job.url, job.company, job.title):
        return
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


def is_react_skipped(url: str) -> bool:
    """True if tracker already has a React-skip row (SKIP + Sent='—') for this URL."""
    return get_url_status_flags(url)["is_react_skip"]


def add_react_skipped(content: dict, url: str) -> None:
    """Write a SKIP row for a React-only job. Sent='—' marks it as stack-filtered.

    Light yellow fill distinguishes these rows from geo/tech SKIP rows (grey).
    The URL is recorded so the job is never re-surfaced by the hunter or apply_agent.
    """
    company = (content.get("company_name") or "").strip()
    title   = (content.get("job_title")    or "").strip()
    if is_known(url, company, title):
        return
    wb, ws = _load_or_create()
    today = date.today().strftime("%Y-%m-%d")
    next_row = ws.max_row + 1

    values = [
        today,                          # Date
        company,                        # Company
        title,                          # Job Title
        content.get("stack", ""),       # Stack
        "SKIP",                         # ATS %
        url,                            # URL
        "",                             # Folder
        "—",                            # Sent  ← marks React-skip
        "",                             # Re-application
        "",                             # To Learn
    ]

    row_font = Font(name="Calibri", size=11)
    row_fill = PatternFill("solid", fgColor="FFF2CC")  # light yellow

    for col, val in enumerate(values, 1):
        cell = ws.cell(row=next_row, column=col, value=val)
        cell.font = row_font
        cell.fill = row_fill
        if col == URL_COL_INDEX:
            cell.hyperlink = url
            cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")

    _save_with_retry(wb)


def add_failed(job: Job) -> None:
    """Append a FAIL row so the job is not retried on next hunt.
    User can delete the row from Excel to retry manually."""
    if is_known(job.url, job.company, job.title):
        return
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
