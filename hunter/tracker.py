"""
tracker.py — read/write tracker.xlsx for the hunter.

Responsibilities:
  - get_known_urls()     → set of URLs already in tracker (for dedup)
  - add_skipped(job)     → append a row with status "SKIP"
  - add_applied(...)     → append a successful generated-docs row
  - add_manual_jobleads_pending(...) → MANUAL row when JobLeads text cannot be fetched
"""

import re
import time
import uuid
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
    "ATS %", "URL", "Folder", "Sent", "Re-application", "To Learn", "ID",
]
# Columns: 1 Date, 2 Company, 3 Job Title, 4 Stack, 5 ATS %, 6 URL, 7 Folder, 8 Sent, 9 Re-app, 10 To Learn, 11 ID
URL_COL_INDEX = 6       # "URL" (was wrongly 5 — that is ATS %, broke URL dedup)
COMPANY_COL_INDEX = 2   # "Company"
TITLE_COL_INDEX = 3     # "Job Title"
ATS_COL_INDEX = 5       # "ATS %" - also used for status (FAIL, SKIP)
SENT_COL_INDEX = 8      # "Sent"
ID_COL_INDEX = 11       # "ID" — short uuid4 hex, used as sync key (Google Sheets ↔ tracker)
REACT_SKIP_SENT_MARKERS = {"—", "–", "-"}

# ATS column: JobLeads detail pages are Cloudflare-blocked — user pastes description
# into Applications/.../job_posting.txt then re-runs apply on the same URL.
MANUAL_PENDING_ATS = "MANUAL"


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
    # Strip /rss suffix (solid.jobs RSS feed vs regular URL are the same job)
    if path.endswith("/rss"):
        path = path[:-4] or "/"
    # Normalize JustJoin URL format: /offers/{slug} → /job-offer/{slug}
    # JustJoin changed their URL scheme; both point to the same offer.
    if "justjoin.it" in host:
        path = re.sub(r"^/offers/", "/job-offer/", path)
    return urlunparse((p.scheme, host, path, "", query, ""))


def _strip_diacritics(s: str) -> str:
    """Transliterate Polish/accented chars to ASCII equivalents.

    Note: ł/Ł (U+0142/U+0141) are NOT NFKD-decomposable, so they must be
    replaced explicitly before the standard normalization step.
    """
    import unicodedata
    s = s.replace('\u0142', 'l').replace('\u0141', 'L')   # ł → l, Ł → L
    return unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')


def _strip_legal_suffixes(s: str) -> str:
    """Remove Polish/international legal-form suffixes in all common representations.

    Must be called AFTER lowercasing and diacritic-stripping (so 'ł' is already 'l').

    Handles both properly formatted ("Sp. z o.o.", "S.A.") and squished/CamelCase
    variants that appear in LLM-generated folder names:
      MindboxSpZOo  → mindbox
      UpvantaSpólkaZOgraniczonąOdpowiedzialnoś → upvanta  (after diacritics → spolka...)
    """
    # "sp. z o.o." and variants (spaces/dots optional) — no \b needed, dots are specific enough
    s = re.sub(r'sp\.?\s*z\.?\s*o\.?\s*o\.?', '', s)
    # "S.A." — trailing \b doesn't work after a dot, use (?![a-z]) lookahead
    s = re.sub(r's\.a\.(?![a-z])', '', s)
    # Other international forms — use \b where safe (all-alpha, no trailing dots)
    s = re.sub(r'\b(ltd|gmbh|inc|llc)\b\.?', '', s)
    # "Spółka..." / "Spolka..." — remove from this word to end of string
    # After diacritic-strip: ł→l, ó→o, so "Spółka" → "Spolka"
    s = re.sub(r'spol?ka.*', '', s)
    # Squished forms: SpZOo, SpZoo, spzoo (sp + z + oo)
    s = re.sub(r'spzo+', '', s)
    return s


def normalize_company(company: str) -> str:
    """Normalized company name for dedup.

    Produces the same key for all of these:
      - "Mindbox Sp. z o.o."
      - "MindboxSpZOo"   (LLM folder-safe form)
      - "Upvanta Spółka z o.o."
      - "UpvantaSpółkaZOgraniczonąOdpowiedzialnoś"
    """
    s = company.lower()
    s = re.sub(r'_?\d{4}-\d{2}-\d{2}(_\d+)?$', '', s)
    s = _strip_diacritics(s)
    s = _strip_legal_suffixes(s)
    s = re.sub(r'[^a-z0-9]', '', s)
    return s


def dedup_key(company: str, title: str) -> str:
    """Normalized key for company+title dedup (cross-source, cross-URL)."""
    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r'_?\d{4}-\d{2}-\d{2}(_\d+)?$', '', s)
        s = _strip_diacritics(s)
        s = _strip_legal_suffixes(s)
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


def _new_row_id() -> str:
    """Generate a short unique ID for a tracker row (8-char hex)."""
    return uuid.uuid4().hex[:8]


def _ensure_ids(ws: openpyxl.worksheet.worksheet.Worksheet) -> bool:
    """Assign IDs to any existing rows that lack one. Returns True if any were added."""
    changed = False
    for row_num in range(2, ws.max_row + 1):
        cell = ws.cell(row=row_num, column=ID_COL_INDEX)
        if not cell.value:
            cell.value = _new_row_id()
            changed = True
    return changed


def _load_or_create() -> tuple[openpyxl.Workbook, openpyxl.worksheet.worksheet.Worksheet]:
    if TRACKER_PATH.exists():
        wb = openpyxl.load_workbook(TRACKER_PATH)
        ws = wb.active
        # Ensure header row has ID column (migration for existing files)
        if ws.max_column < ID_COL_INDEX:
            header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
            header_fill = PatternFill("solid", fgColor="2B579A")
            cell = ws.cell(row=1, column=ID_COL_INDEX, value="ID")
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[get_column_letter(ID_COL_INDEX)].width = 12
        if _ensure_ids(ws):
            _save_with_retry(wb)
        return wb, ws

    # Create fresh tracker with header row
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Applications"

    header_fill = PatternFill("solid", fgColor="2B579A")
    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    widths = [12, 20, 30, 12, 8, 50, 40, 8, 16, 35, 12]

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

        if ats.upper() not in ("FAIL", "SKIP", "?", "", MANUAL_PENDING_ATS):
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


def remove_manual_pending_rows(url: str) -> int:
    """Delete tracker rows for this URL where ATS is MANUAL (successful apply supersedes)."""
    if not TRACKER_PATH.exists():
        return 0
    norm = normalize_url(url)
    wb = openpyxl.load_workbook(TRACKER_PATH)
    ws = wb.active
    deleted = 0
    for row_num in range(ws.max_row, 1, -1):
        row_url = str(ws.cell(row=row_num, column=URL_COL_INDEX).value or "").strip()
        if not row_url or normalize_url(row_url) != norm:
            continue
        ats = str(ws.cell(row=row_num, column=ATS_COL_INDEX).value or "").strip().upper()
        if ats == MANUAL_PENDING_ATS:
            ws.delete_rows(row_num)
            deleted += 1
    if deleted:
        _save_with_retry(wb)
    else:
        wb.close()
    return deleted


def manual_jobleads_job_posting_path(url: str) -> Path | None:
    """Return path to job_posting.txt for a MANUAL JobLeads row, or None."""
    if not TRACKER_PATH.exists():
        return None
    norm = normalize_url(url)
    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    try:
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < URL_COL_INDEX:
                continue
            row_url = str(row[URL_COL_INDEX - 1] or "").strip()
            if not row_url or normalize_url(row_url) != norm:
                continue
            ats = str(row[ATS_COL_INDEX - 1] or "").strip().upper()
            if ats != MANUAL_PENDING_ATS:
                continue
            if len(row) <= URL_COL_INDEX:
                return None
            folder_raw = str(row[URL_COL_INDEX] or "").strip()
            if not folder_raw:
                return None
            p = Path(folder_raw)
            if not p.is_absolute():
                p = Path(TRACKER_PATH).parent / folder_raw
            return p / "job_posting.txt"
    finally:
        wb.close()
    return None


def has_manual_pending(url: str) -> bool:
    """True if tracker already has a MANUAL row for this URL."""
    return any(
        (r.get("ats") or "").strip().upper() == MANUAL_PENDING_ATS
        for r in lookup_url(url)
    )


def get_all_manual_pending() -> list[dict]:
    """Return all MANUAL-pending rows as dicts with keys: url, folder, company, title, row."""
    if not TRACKER_PATH.exists():
        return []
    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    results: list[dict] = []
    try:
        for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if not row or len(row) < ATS_COL_INDEX:
                continue
            ats = str(row[ATS_COL_INDEX - 1] or "").strip().upper()
            if ats != MANUAL_PENDING_ATS:
                continue
            url = str(row[URL_COL_INDEX - 1] or "").strip() if len(row) >= URL_COL_INDEX else ""
            folder = str(row[URL_COL_INDEX] or "").strip() if len(row) > URL_COL_INDEX else ""
            company = str(row[COMPANY_COL_INDEX - 1] or "").strip() if len(row) >= COMPANY_COL_INDEX else ""
            title = str(row[TITLE_COL_INDEX - 1] or "").strip() if len(row) >= TITLE_COL_INDEX else ""
            if not url:
                continue
            results.append({"url": url, "folder": folder, "company": company, "title": title, "row": i})
    finally:
        wb.close()
    return results


def latest_manual_pending() -> dict[str, str] | None:
    """Return latest MANUAL row info (url, folder) or None.

    Used by Telegram paste flow when the user pastes a posting without URL.
    """
    if not TRACKER_PATH.exists():
        return None
    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    latest: dict[str, str] | None = None
    try:
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < ATS_COL_INDEX:
                continue
            ats = str(row[ATS_COL_INDEX - 1] or "").strip().upper()
            if ats != MANUAL_PENDING_ATS:
                continue
            url = str(row[URL_COL_INDEX - 1] or "").strip() if len(row) >= URL_COL_INDEX else ""
            folder = str(row[URL_COL_INDEX] or "").strip() if len(row) > URL_COL_INDEX else ""
            if not url:
                continue
            latest = {"url": url, "folder": folder}
    finally:
        wb.close()
    return latest


def add_manual_jobleads_pending(
    *,
    url: str,
    company: str,
    title: str,
    folder_abs: Path,
) -> bool:
    """Append MANUAL row for JobLeads when description cannot be fetched.

    Skips when URL already has a success row or any tracker row (dedup / FAIL / MANUAL).
    """
    if has_successful_entry(url) or lookup_url(url):
        return False
    wb, ws = _load_or_create()
    today = date.today().strftime("%Y-%m-%d")
    next_row = ws.max_row + 1
    folder_str = str(folder_abs).replace("\\", "/")
    values = [
        today,
        company.strip() or "Unknown",
        title.strip() or "Unknown",
        "",
        MANUAL_PENDING_ATS,
        url,
        folder_str,
        "",
        "",
        "Paste job text into job_posting.txt in Folder, then re-run apply (same URL).",
        _new_row_id(),
    ]
    row_font = Font(name="Calibri", size=11)
    manual_fill = PatternFill("solid", fgColor="FFF2CC")  # light yellow

    for col, val in enumerate(values, 1):
        cell = ws.cell(row=next_row, column=col, value=val)
        cell.font = row_font
        cell.fill = manual_fill
        if col == URL_COL_INDEX and val:
            cell.hyperlink = str(val)
            cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")

    _save_with_retry(wb)
    return True


def add_applied(content: dict, force: bool = False) -> bool:
    """Append a successful apply row. Returns True when a row was written."""
    company = _company_from_content(content)
    job_title = str(content.get("job_title", "") or "")
    stack = str(content.get("stack", "") or "")
    apply_url = str(content.get("apply_url", "") or "")
    if apply_url:
        remove_manual_pending_rows(apply_url)
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
        _new_row_id(),
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


def add_skipped(job: Job) -> dict | None:
    """Append a SKIP row to tracker. Returns the row dict (with ID) or None if already known."""
    if is_known(job.url, job.company, job.title):
        return None
    wb, ws = _load_or_create()
    today = date.today().strftime("%Y-%m-%d")
    next_row = ws.max_row + 1
    row_id = _new_row_id()

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
        row_id,          # ID
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
    return dict(zip(TRACKER_HEADERS, values))


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
        _new_row_id(),                  # ID
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


def add_expired(url: str, company: str = "", title: str = "") -> None:
    """Write an EXPIRED row — offer was no longer active when fetched.

    Orange fill distinguishes these from SKIP (grey) and React-skip (yellow).
    URL is recorded so the job is never re-fetched.
    """
    if is_known(url, company, title):
        return
    wb, ws = _load_or_create()
    today = date.today().strftime("%Y-%m-%d")
    next_row = ws.max_row + 1

    values = [
        today,           # Date
        company,         # Company
        title,           # Job Title
        "",              # Stack
        "EXPIRED",       # ATS %
        url,             # URL
        "",              # Folder
        "",              # Sent
        "",              # Re-application
        "",              # To Learn
        _new_row_id(),   # ID
    ]

    row_font = Font(name="Calibri", size=11)
    row_fill = PatternFill("solid", fgColor="FCE4D6")  # light orange

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
        _new_row_id(),   # ID
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


def iter_unsent_rows() -> list[dict]:
    """Return unsent tracker rows (no Sent value, excluding SKIP).

    Each dict has keys: id, date, company, title, stack, ats, url, folder,
    sent, reapp, to_learn, row_num.
    """
    if not TRACKER_PATH.exists():
        return []

    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    results = []
    try:
        for row_num, row in enumerate(ws.iter_rows(min_row=2, values_only=True), 2):
            if not row:
                continue
            row = tuple(row) + ("",) * max(0, ID_COL_INDEX - len(row))

            ats = str(row[ATS_COL_INDEX - 1] or "").strip()
            sent = str(row[SENT_COL_INDEX - 1] or "").strip()
            row_id = str(row[ID_COL_INDEX - 1] or "").strip()

            if ats == "SKIP":
                continue
            if sent:
                continue
            if not row_id:
                continue

            results.append({
                "id": row_id,
                "row_num": row_num,
                "date": str(row[0] or "").strip(),
                "company": str(row[COMPANY_COL_INDEX - 1] or "").strip(),
                "title": str(row[TITLE_COL_INDEX - 1] or "").strip(),
                "stack": str(row[3] or "").strip(),
                "ats": ats,
                "url": str(row[URL_COL_INDEX - 1] or "").strip(),
                "folder": str(row[6] or "").strip(),
                "sent": sent,
                "reapp": str(row[8] or "").strip(),
                "to_learn": str(row[9] or "").strip(),
            })
    finally:
        wb.close()
    return results


def apply_sent_updates(updates: dict[str, str]) -> int:
    """Write Sent values (e.g. EXPIRED) back into tracker.xlsx.

    updates: {row_id: sent_value} — only non-empty sent_value entries.
    Returns number of rows updated.
    """
    if not updates or not TRACKER_PATH.exists():
        return 0

    wb = openpyxl.load_workbook(TRACKER_PATH)
    ws = wb.active
    updated = 0

    for row_num in range(2, ws.max_row + 1):
        row_id = str(ws.cell(row=row_num, column=ID_COL_INDEX).value or "").strip()
        if row_id and row_id in updates:
            sent_val = updates[row_id]
            if sent_val:
                ws.cell(row=row_num, column=SENT_COL_INDEX).value = sent_val
                updated += 1

    if updated:
        _save_with_retry(wb)
    else:
        wb.close()
    return updated


_REAPP_COL_INDEX = 9    # "Re-application"
_TO_LEARN_COL_INDEX = 10  # "To Learn"


def apply_pull_updates(rows: list[dict]) -> int:
    """Write Sheets-sourced field changes back to tracker.xlsx (pull sync).

    rows: list of full row dicts (with 'ID') where Sheets had a newer value.
    Updates Sent, Re-application, To Learn columns. Returns count of rows updated.
    """
    if not rows or not TRACKER_PATH.exists():
        return 0

    wb = openpyxl.load_workbook(TRACKER_PATH)
    ws = wb.active
    updated = 0

    for row_num in range(2, ws.max_row + 1):
        row_id = str(ws.cell(row=row_num, column=ID_COL_INDEX).value or "").strip()
        if not row_id:
            continue
        for row_dict in rows:
            if row_dict.get("ID", "").strip() != row_id:
                continue
            ws.cell(row=row_num, column=SENT_COL_INDEX).value = row_dict.get("Sent", "")
            ws.cell(row=row_num, column=_REAPP_COL_INDEX).value = row_dict.get("Re-application", "")
            ws.cell(row=row_num, column=_TO_LEARN_COL_INDEX).value = row_dict.get("To Learn", "")
            updated += 1
            break

    if updated:
        _save_with_retry(wb)
    else:
        wb.close()
    return updated


def get_folder_by_url(url: str) -> str | None:
    """Return the Folder value for a given job URL, or None if not found.

    Normalizes the URL before comparing (strip trailing slash, lowercase scheme).
    Returns the raw string from the Folder column (e.g. 'Applications/2026-05-11/PeopleMore_3').
    """
    if not TRACKER_PATH.exists():
        return None

    target = normalize_url(url)
    if not target:
        return None

    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    try:
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            cell_url = str(row[URL_COL_INDEX - 1] or "").strip()
            if normalize_url(cell_url) == target:
                folder_val = row[URL_COL_INDEX]  # column 7 = index 6 = URL_COL_INDEX
                return str(folder_val).strip() if folder_val else None
    finally:
        wb.close()
    return None


# gsheets COLUMNS order: Date, Company, Job Title, Stack, ATS %, URL,
#                        Folder, Sent, Re-application, To Learn, ID
_GSHEETS_COLS = [
    "Date", "Company", "Job Title", "Stack", "ATS %", "URL",
    "Folder", "Sent", "Re-application", "To Learn", "ID",
]

def read_all_tracker_rows() -> list[dict]:
    """Read every data row from tracker.xlsx as a dict keyed by gsheets COLUMNS names.

    Rows with no ID are skipped (they can't be synced). Empty cells become "".
    Used by gsheets_sync.push_missing_rows() to detect what's absent from Sheets.
    """
    if not TRACKER_PATH.exists():
        return []

    wb = openpyxl.load_workbook(TRACKER_PATH, read_only=True, data_only=True)
    ws = wb.active
    results: list[dict] = []
    try:
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row:
                continue
            # Pad to at least ID_COL_INDEX columns
            padded = tuple(row) + ("",) * max(0, ID_COL_INDEX - len(row))
            row_id = str(padded[ID_COL_INDEX - 1] or "").strip()
            if not row_id:
                continue
            results.append({
                col: str(padded[i] or "").strip()
                for i, col in enumerate(_GSHEETS_COLS)
            })
    finally:
        wb.close()
    return results
