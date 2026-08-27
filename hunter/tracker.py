"""
tracker.py — SQLite-backed job tracker.

All persistent state lives in tracker.db (SQLite, WAL mode).
Public API is identical to the previous openpyxl implementation;
callers need no changes except where row_num (xlsx row) was replaced
by row_id (the 8-char hex PRIMARY KEY from the applications table).

Responsibilities:
  - get_known_urls()     → set of normalised URLs for dedup
  - add_skipped(job)     → append a SKIP row
  - add_applied(...)     → append a successful generated-docs row
  - add_manual_jobleads_pending(...) → MANUAL row for JobLeads paste flow
  - lookup_url(url)      → list of matching rows (by normalised URL)
  - read_all_tracker_rows() → all rows for Google Sheets / Drive sync
  - set_confirmation(row_id, date_str) → write confirmation date
  … (see individual docstrings)

DB path: hunter.config.TRACKER_DB_PATH  (default: project root/tracker.db)
Testable: monkeypatch hunter.tracker.DB_PATH to an isolated tmp path.
"""

import json
import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from hunter.config import TRACKER_DB_PATH, TRACKER_PATH
from hunter.db import get_db

log = logging.getLogger(__name__)
from hunter.models import Job
from hunter.validation import SCOUT_POSTS_URL_MARKER, _LEGACY_SCOUT_POSTS_URL_MARKER

# ── Module-level DB path — override in tests via monkeypatch ──────────────────
DB_PATH: Path = TRACKER_DB_PATH


def _uid() -> str:
    """Active user id for the current process.

    Delegates to config.current_user_id(): JOB_HUNTER_USER_ID (injected into
    per-user apply subprocesses by hunter.users.user_env — Phase B3) wins
    over DEFAULT_USER_ID (env, the owner). In the bot process the env var is
    absent, so every read/write stays owner-scoped exactly as in Phase B1.
    """
    from hunter.config import current_user_id

    return current_user_id()


# ── Schema / header constants (kept for backward compat with tracker_cache, db) ─
TRACKER_HEADERS = [
    "Date",
    "Company",
    "Job Title",
    "Stack",
    "ATS %",
    "URL",
    "Folder",
    "Sent",
    "Re-application",
    "To Learn",
    "ID",
    "Drive URL",
    "Confirmation",
    "Answer",
    "Cost $",
]
# 1-based column indices (mirror xlsx schema; kept for tracker_cache / db imports)
URL_COL_INDEX = 6
COMPANY_COL_INDEX = 2
TITLE_COL_INDEX = 3
ATS_COL_INDEX = 5
SENT_COL_INDEX = 8
ID_COL_INDEX = 11
COL_DRIVE_URL = 12
COL_CONFIRMATION = 13
COL_ANSWER = 14
COL_COST_USD = 15

# Dash markers in the Sent column. Historically written only by react-skips;
# since 2026-08-08 EVERY SKIP/FAIL row gets '—' at insert (add_skipped/
# add_failed) so skipped/failed rows never appear in the owner's Sheets
# "Sent is empty" send-queue filter. On a SKIP row a dash still doubles as
# the "permanently decided" marker (see get_url_status_flags) — a plain
# re-paste is refused, /force regenerates. On a FAIL row it means nothing
# beyond "not awaiting send": retries key on ats_status='FAIL' alone.
REACT_SKIP_SENT_MARKERS = {"—", "–", "-"}
MANUAL_PENDING_ATS = "MANUAL"
PENDING_ATS = "PENDING"
IN_PROGRESS_ATS = "IN_PROGRESS"
# Statuses that must NOT count as "recently applied" for the company/title
# cooldown checks below (is_in_cooldown/company_cooldown_active): a queued-
# but-not-yet-attempted PENDING/IN_PROGRESS row (M1, docs/
# HUNT_APPLY_SPLIT_PLAN.md) is exactly as un-applied as a SKIP/FAIL row.
_COOLDOWN_SKIP_STATUSES = frozenset(
    {"SKIP", "FAIL", "MANUAL", "EXPIRED", PENDING_ATS, IN_PROGRESS_ATS}
)

# ── gsheets column map (used by read_all_tracker_rows + gsheets_sync) ─────────
_GSHEETS_COLS = [
    "Date",
    "Company",
    "Job Title",
    "Stack",
    "ATS %",
    "URL",
    "Folder",
    "Sent",
    "Re-application",
    "To Learn",
    "ID",
]


# ── Pure helpers ──────────────────────────────────────────────────────────────


def normalize_url(url: str) -> str:
    """Canonical form: lowercase host, strip trailing slash, drop tracking params."""
    url = url.strip()
    if not url:
        return url
    p = urlparse(url)
    host = (p.hostname or "").lower()
    path = p.path.rstrip("/") or "/"
    drop_params = {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "utm_term",
        "utm_id",
        "ref",
        "refId",
        "trackingId",
        "trk",
        "fbclid",
        "gclid",
        "campaignid",
        "adgroupid",
        "originToLandingJobPostings",
        "origin",
        "sendid",
        "send_date",
        "sug",
    }
    _path_id_domains = {"www.pracuj.pl", "justjoin.it", "nofluffjobs.com"}
    if host in _path_id_domains:
        query = ""
    else:
        qs = parse_qs(p.query, keep_blank_values=False)
        clean_qs = {k: v for k, v in qs.items() if k not in drop_params}
        query = urlencode(clean_qs, doseq=True) if clean_qs else ""
    if "linkedin.com" in host and "/jobs/view/" in path:
        m = re.search(r"/jobs/view/(\d+)", path)
        if m:
            path = f"/jobs/view/{m.group(1)}"
            query = ""
    if path.endswith("/rss"):
        path = path[:-4] or "/"
    if "justjoin.it" in host:
        path = re.sub(r"^/offers/", "/job-offer/", path)
    return urlunparse((p.scheme, host, path, "", query, ""))


def _strip_diacritics(s: str) -> str:
    import unicodedata

    s = s.replace("ł", "l").replace("Ł", "L")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def _strip_legal_suffixes(s: str) -> str:
    s = re.sub(r"sp\.?\s*z\.?\s*o\.?\s*o\.?", "", s)
    s = re.sub(r"s\.a\.(?![a-z])", "", s)
    s = re.sub(r"\b(ltd|gmbh|inc|llc|sa)\b\.?", "", s)
    s = re.sub(r"spol?ka.*", "", s)
    s = re.sub(r"spzo+", "", s)
    return s


def normalize_company(company: str) -> str:
    s = company.lower()
    s = re.sub(r"_?\d{4}-\d{2}-\d{2}(_\d+)?$", "", s)
    s = re.sub(r"_\d+$", "", s)
    s = _strip_diacritics(s)
    s = _strip_legal_suffixes(s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


_MARKETING_VERBS = (
    r"build|join|help|shape|create|drive|be\s+part|make|lead|scale|"
    r"transform|craft|deliver|grow|work|define|change|redefine"
)
_MARKETING_TAIL_RE = re.compile(
    r"\s+[—–]\s+(?=" + _MARKETING_VERBS + r")"
    r"|\s+\|\s+"
    r"|\s+-\s+(?=" + _MARKETING_VERBS + r")",
    re.IGNORECASE,
)


def _strip_marketing_tail(title: str) -> str:
    m = _MARKETING_TAIL_RE.search(title)
    if m:
        return title[: m.start()].strip()
    return title


def dedup_key(company: str, title: str) -> str:
    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"_?\d{4}-\d{2}-\d{2}(_\d+)?$", "", s)
        s = re.sub(r"_\d+$", "", s)
        s = _strip_diacritics(s)
        s = _strip_legal_suffixes(s)
        s = re.sub(r"[^a-z0-9]", "", s)
        return s

    return _norm(company) + "|" + _norm(_strip_marketing_tail(title))


def _new_row_id() -> str:
    return uuid.uuid4().hex[:8]


def _parse_ats_score(raw: str) -> tuple[str, int | None]:
    value = (raw or "").strip()
    if not value:
        return "", None
    m10 = re.search(r"(\d{1,2}(?:[.,]\d+)?)\s*/\s*10\b", value)
    if m10:
        base = float(m10.group(1).replace(",", "."))
        score = max(0, min(int(round(base * 10)), 100))
        return f"{score}%", score
    m = re.search(r"\d{1,3}", value)
    if not m:
        return value, None
    score = max(0, min(int(m.group(0)), 100))
    return f"{score}%", score


def _company_from_content(content: dict) -> str:
    cn = (content.get("company_name") or "").strip()
    if cn:
        return cn
    folder_name = Path(str(content.get("output_folder") or "")).name
    s = re.sub(r"_(\d+)$", "", folder_name)
    m = re.search(r"^(.+)_[0-9]{4}-[0-9]{2}-[0-9]{2}$", s)
    return (m.group(1) if m else s) or "Unknown"


def _is_unsent(sent: str) -> bool:
    return not sent or sent in REACT_SKIP_SENT_MARKERS


# ── Row → dict helpers ────────────────────────────────────────────────────────


def _db_row_to_tracker_dict(row) -> dict:
    """Map sqlite3.Row / dict with DB column names to TRACKER_HEADERS keyed dict."""
    return {
        "Date": str(row["date"] or ""),
        "Company": str(row["company"] or ""),
        "Job Title": str(row["title"] or ""),
        "Stack": str(row["stack"] or ""),
        "ATS %": str(row["ats_status"] or ""),
        "URL": str(row["url"] or ""),
        "Folder": str(row["folder"] or ""),
        "Sent": str(row["sent"] or ""),
        "Re-application": str(row["reapplication"] or ""),
        "To Learn": str(row["to_learn"] or ""),
        "ID": str(row["id"] or ""),
        "Drive URL": str(row["drive_url"] or ""),
        "Confirmation": str(row["confirmation"] or ""),
        "Answer": str(row["answer"] or ""),
        # sqlite3.Row's `in` checks VALUES, not column names — must use .keys().
        "Cost $": _format_cost(row["cost_usd"] if "cost_usd" in row.keys() else None),  # noqa: SIM118
    }


def _format_cost(cost_usd) -> str:
    """Render a cost_usd column value for display / Sheets push.

    None → empty string (no measurement, either a pre-tracking row or a CLI
    run). 0 → '$0.0000' (kept distinguishable from missing). Any positive
    number rounded to 4 decimals.
    """
    if cost_usd is None or cost_usd == "":
        return ""
    try:
        return f"${float(cost_usd):.4f}"
    except (TypeError, ValueError):
        return ""


def _parse_cost_cell(value) -> float | None:
    """Inverse of _format_cost: turn a Sheet "Cost $" cell back into a float.

    "$0.4712" → 0.4712, "" / None → None. Any junk silently → None — a
    user edit in the Cost column shouldn't be able to crash the pull.
    Used by insert_pulled_rows when bootstrapping from the Sheet on a
    fresh DB so historical cost data isn't lost on redeploy.
    """
    if value is None:
        return None
    s = str(value).strip().lstrip("$").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── Dedup reads ───────────────────────────────────────────────────────────────


def get_known_urls() -> set[str]:
    """Return all normalised URLs stored in tracker — used for deduplication."""
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT url_norm FROM applications WHERE url_norm != '' AND user_id=?",
            (_uid(),),
        ).fetchall()
        return {r["url_norm"] for r in rows}


def get_known_company_titles(*, exclude_url: str = "", exclude_folder: str = "") -> set[str]:
    """Return dedup_key(company, title) for all rows in tracker.

    `exclude_url` / `exclude_folder` drop THIS run's own row from the set. The
    CLI pipeline needs that and the API pipeline does not: there, the dedup gate
    runs before generate_docs, so no row exists yet. In the CLI pipeline the
    skill has already run generate_docs (without --no-tracker) by the time the
    gate is reached, so the run's own row is in the tracker and the gate matched
    ITSELF -- every CLI apply that got past the stack gate aborted as a
    duplicate from 2026-08-21 (when the gate shipped) until this fix. Confirmed
    on production data: `Ness Solution` and `Grupa Com40` each had exactly ONE
    row carrying their dedup_key, written by the very run their transcript shows
    aborting with "SKIP - company+title dedup match".
    """
    norm = normalize_url(exclude_url) if exclude_url else ""
    folder = str(exclude_folder) if exclude_folder else ""

    with get_db(DB_PATH) as conn:
        # Exclude exactly ONE row -- the newest match, which is the row this run
        # just wrote. Dropping every row that shares the folder string is not
        # safe: `.claude/commands/apply.md` tells the skill to name the folder
        # "{date}/{CompanyName}" with no _2 collision suffix (that logic lives in
        # compute_output_folder, which only the API path uses), so two CLI
        # applications for the same company on the same day carry an IDENTICAL
        # folder. Excluding both would hide the earlier one from the gate and let
        # a genuine duplicate through -- which is the Comarch case the gate was
        # added for on 2026-08-20: one requisition arriving via LinkedIn and via
        # pracuj.pl in the same day's hunts.
        excluded_rowid = None
        if norm or folder:
            clauses, params = [], []
            if norm:
                clauses.append("url_norm=?")
                params.append(norm)
            if folder:
                clauses.append("folder=?")
                params.append(folder)
            params.append(_uid())
            own = conn.execute(
                f"SELECT rowid FROM applications WHERE ({' OR '.join(clauses)}) "  # noqa: S608
                "AND user_id=? ORDER BY rowid DESC LIMIT 1",
                params,
            ).fetchone()
            excluded_rowid = own["rowid"] if own else None

        rows = conn.execute(
            "SELECT rowid, company, title FROM applications "
            "WHERE company != '' AND title != '' AND user_id=?",
            (_uid(),),
        ).fetchall()
    return {dedup_key(r["company"], r["title"]) for r in rows if r["rowid"] != excluded_rowid}


def get_sent_companies() -> set[str]:
    """Return normalised company names for rows that have Sent info."""
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT company FROM applications WHERE sent != '' AND company != ''"
        ).fetchall()
        return {normalize_company(r["company"]) for r in rows}


def company_matches_sent(comp_key: str, sent_companies: set[str]) -> bool:
    """Fuzzy company match: 'scalo' matches 'scalowroclaw' and vice versa."""
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


def is_known(url: str, company: str = "", title: str = "") -> bool:
    """Check if a job is already in tracker (by URL or company+title).

    Status-agnostic by construction — a PENDING or IN_PROGRESS row (M1,
    docs/HUNT_APPLY_SPLIT_PLAN.md) already makes this return True, same as
    SKIP/FAIL/MANUAL/a real ATS score, since the query below never filters
    on ats_status at all. That's exactly the guard the queue needs: a job
    already sitting in the PENDING queue must not be re-queued by the next
    hunt cycle.
    """
    norm = normalize_url(url)
    uid = _uid()
    with get_db(DB_PATH) as conn:
        if (
            norm
            and conn.execute(
                "SELECT 1 FROM applications WHERE url_norm=? AND user_id=? LIMIT 1",
                (norm, uid),
            ).fetchone()
        ):
            return True
        if company and title:
            key = dedup_key(company, title)
            rows = conn.execute(
                "SELECT company, title FROM applications "
                "WHERE company != '' AND title != '' AND user_id=?",
                (uid,),
            ).fetchall()
            for r in rows:
                if dedup_key(r["company"], r["title"]) == key:
                    return True
    return False


def _is_known_terminal(url: str, company: str = "", title: str = "") -> bool:
    """Same contract as is_known(), but a PENDING/IN_PROGRESS placeholder for
    THIS exact job does not count (M1, docs/HUNT_APPLY_SPLIT_PLAN.md).

    **Call this URL-ONLY (omit company/title) from every terminal-write
    function** (add_failed, add_skipped, add_expired, add_react_skipped —
    add_manual_jobleads_pending already does its own URL-only check). Passing
    company/title there is a bug, not a stricter guard: the company+title
    branch matches ANY row anywhere sharing dedup_key, including a
    COMPLETELY DIFFERENT url's terminal row (e.g. the same employer re-posted
    under a new URL). A caller reaching add_skipped/etc. has ALREADY decided
    THIS url needs its own terminal write — a match on some OTHER url must
    not suppress it, or THIS url's own PENDING/IN_PROGRESS placeholder is
    never cleared and the vacancy claims, fails the same company+title dedup
    gate, and gets released back to the queue forever (real incident
    2026-08-27: AVENGA "Senior Angular Developer" re-posted under a new
    nofluffjobs URL matched an unrelated April application by company+title,
    the Step 4.55 dedup gate correctly decided to SKIP it, but add_skipped's
    then-company+title-aware guard silently no-op'd instead of writing the
    SKIP row for the NEW url — so its own placeholder bounced between
    PENDING and IN_PROGRESS every ~60 min, burning a full CLI generation
    attempt each time, never resolving, forever). The URL-only check is
    still exactly what queue mode needs: it lets a write proceed onto THIS
    url's own PENDING/IN_PROGRESS placeholder while still refusing a
    duplicate write when THIS SAME url already carries a genuine terminal
    row.

    Company+title matching is reserved for READ-ONLY "has this vacancy
    already been decided, by any URL" checks (e.g.
    apply_worker._resolve_outcome's "ok" branch, deciding whether a
    successful exit deserves delivery) — never for gating a write.

    With the queue disabled (APPLY_QUEUE_ENABLED=false, the default) no
    PENDING/IN_PROGRESS row is ever created, so the URL branch alone behaves
    identically to is_known() — zero behavior change for the non-queue path.
    """
    norm = normalize_url(url)
    uid = _uid()
    with get_db(DB_PATH) as conn:
        if norm:
            rows = conn.execute(
                "SELECT ats_status FROM applications WHERE url_norm=? AND user_id=?",
                (norm, uid),
            ).fetchall()
            if any((r["ats_status"] or "") not in (PENDING_ATS, IN_PROGRESS_ATS) for r in rows):
                return True
        if company and title:
            key = dedup_key(company, title)
            rows = conn.execute(
                "SELECT company, title, ats_status FROM applications "
                "WHERE company != '' AND title != '' AND user_id=?",
                (uid,),
            ).fetchall()
            for r in rows:
                if dedup_key(r["company"], r["title"]) == key and (r["ats_status"] or "") not in (
                    PENDING_ATS,
                    IN_PROGRESS_ATS,
                ):
                    return True
    return False


def _clear_own_placeholder(conn, url: str) -> None:
    """Delete this job's own PENDING/IN_PROGRESS row, if any, within an
    already-open connection/transaction.

    Called by every terminal-write function right before inserting the real
    row so a queued job's placeholder is replaced in place instead of left
    behind as a stale duplicate. A no-op when queue mode is off (no such row
    ever exists) or `url` is empty.
    """
    norm = normalize_url(url) if url else ""
    if not norm:
        return
    conn.execute(
        f"DELETE FROM applications WHERE url_norm=? AND user_id=? "  # noqa: S608
        f"AND ats_status IN ('{PENDING_ATS}', '{IN_PROGRESS_ATS}')",
        (norm, _uid()),
    )


# ── Status queries ────────────────────────────────────────────────────────────


def has_successful_entry(url: str) -> bool:
    """True if tracker has a non-FAIL, non-SKIP entry for this URL."""
    return get_url_status_flags(url)["has_success"]


def get_url_status_flags(url: str) -> dict[str, bool]:
    """Return status flags for URL: successful entry and skip-dash marker.

    ``is_react_skip`` is True for any SKIP row whose Sent carries a dash
    marker. Since 2026-08-08 every SKIP row gets the dash at insert (not just
    react-skips), so the flag effectively reads "deliberately skipped —
    don't regenerate on a plain paste"; /force (skip_dedup) bypasses it.
    """
    norm = normalize_url(url)
    if not norm:
        return {"has_success": False, "is_react_skip": False}

    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ats_status, sent FROM applications WHERE url_norm=?", (norm,)
        ).fetchall()

    has_success = False
    is_react_skip = False
    for row in rows:
        ats = (row["ats_status"] or "").strip().upper()
        sent = (row["sent"] or "").strip()
        if ats not in ("FAIL", "SKIP", "?", "", MANUAL_PENDING_ATS, PENDING_ATS, IN_PROGRESS_ATS):
            has_success = True
        elif ats == "SKIP" and sent in REACT_SKIP_SENT_MARKERS:
            is_react_skip = True
        if has_success and is_react_skip:
            break
    return {"has_success": has_success, "is_react_skip": is_react_skip}


def is_react_skipped(url: str) -> bool:
    """True if tracker already has a React-skip row (SKIP + Sent='—') for this URL."""
    return get_url_status_flags(url)["is_react_skip"]


# ── Lookup ────────────────────────────────────────────────────────────────────


def lookup_url(url: str) -> list[dict]:
    """Find all tracker entries matching this URL (normalised).

    Returns list of dicts with keys: id, company, title, ats, folder, sent.
    (``id`` replaces the former xlsx-row-number ``row`` key.)
    """
    norm = normalize_url(url)
    if not norm:
        return []
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, company, title, ats_status, folder, sent "
            "FROM applications WHERE url_norm=?",
            (norm,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "company": r["company"],
            "title": r["title"],
            "ats": r["ats_status"],
            "folder": r["folder"],
            "sent": r["sent"],
        }
        for r in rows
    ]


def lookup_company(company: str) -> list[dict]:
    """Find all tracker entries matching this company (normalised)."""
    norm = normalize_company(company)
    if not norm:
        return []
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, company, title, ats_status, folder, sent FROM applications",
        ).fetchall()
    return [
        {
            "id": r["id"],
            "company": r["company"],
            "title": r["title"],
            "ats": r["ats_status"],
            "folder": r["folder"],
            "sent": r["sent"],
        }
        for r in rows
        if r["company"] and normalize_company(r["company"]) == norm
    ]


# ── FAIL-row management ───────────────────────────────────────────────────────

# Stop retrying a FAIL job after this many consecutive apply failures.
MAX_FAIL_RETRIES: int = 3

_PASTE_NO_URL = "paste://no-url"


def get_failed_jobs() -> list[Job]:
    """Return Job objects for all FAIL rows that are still worth retrying.

    Excluded:
    - paste://no-url rows (no URL → apply_agent can't fetch the posting)
    - LinkedIn Scout relay rows (synthetic dedup-key URL; the retry rebuilds a
      bare Job without raw["post_text"], so fetching the URL raises by design —
      every retry is guaranteed to fail. The scout relays the post again if it
      is still live, so dropping the retry loses nothing.)
    - rows with fail_count >= MAX_FAIL_RETRIES (gave up after N attempts)
    """
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT title, company, url, fail_count FROM applications WHERE ats_status='FAIL'"
        ).fetchall()
    return [
        Job(
            title=r["title"],
            company=r["company"],
            location="",
            salary=None,
            url=r["url"],
            source="retry",
        )
        for r in rows
        if r["url"]
        and r["url"] != _PASTE_NO_URL
        and SCOUT_POSTS_URL_MARKER not in r["url"]
        and _LEGACY_SCOUT_POSTS_URL_MARKER not in r["url"]
        and (r["fail_count"] or 0) < MAX_FAIL_RETRIES
    ]


def increment_fail_count(url: str) -> int:
    """Increment fail_count for the FAIL row with this URL. Returns new count."""
    norm = normalize_url(url)
    with get_db(DB_PATH) as conn:
        conn.execute(
            "UPDATE applications SET fail_count = fail_count + 1 "
            "WHERE ats_status='FAIL' AND url_norm=?",
            (norm,),
        )
        row = conn.execute(
            "SELECT fail_count FROM applications WHERE ats_status='FAIL' AND url_norm=?",
            (norm,),
        ).fetchone()
    return int(row["fail_count"]) if row else 0


def classify_retry_outcome(url: str) -> str:
    """After a retry subprocess exited 0, report what the pipeline actually did.

    Exit code 0 covers four very different endings — a real application,
    EXPIRED, a gate SKIP, and the too-short abort that writes nothing — and
    the parent can't tell them apart from the exit code alone (retry log
    2026-08-10: six dead postings all reported as "✅ Retry OK").
    Returns "applied" | "expired" | "skipped" | "noop".
    """
    norm = normalize_url(url)
    if not norm:
        return "noop"
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ats_status, sent FROM applications WHERE url_norm=?",
            (norm,),
        ).fetchall()
    non_applied = {
        "FAIL",
        "SKIP",
        "?",
        "",
        MANUAL_PENDING_ATS,
        PENDING_ATS,
        IN_PROGRESS_ATS,
    }
    statuses = [
        ((r["ats_status"] or "").strip().upper(), (r["sent"] or "").strip().upper()) for r in rows
    ]
    if any(ats not in non_applied for ats, _sent in statuses):
        return "applied"
    if any(sent == "EXPIRED" for _ats, sent in statuses):
        return "expired"
    if any(ats == "SKIP" for ats, _sent in statuses):
        return "skipped"
    return "noop"


def remove_failed(url: str) -> None:
    """Remove a FAIL row from tracker (so it can be re-added as a proper entry)."""
    norm = normalize_url(url)
    if not norm:
        return
    with get_db(DB_PATH) as conn:
        conn.execute("DELETE FROM applications WHERE ats_status='FAIL' AND url_norm=?", (norm,))


def get_gave_up_failed() -> list[dict]:
    """FAIL rows whose fail_count reached MAX_FAIL_RETRIES.

    These are permanently invisible to get_failed_jobs() — nothing in the
    normal flow ever resets the counter, so without /retry_reset they are
    dead forever (M3, docs/LLM_OUTAGE_RESILIENCE_PLAN.md). Each dict:
    {company, title, url, fail_count}.
    """
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT company, title, url, fail_count FROM applications "
            "WHERE ats_status='FAIL' AND fail_count >= ? ORDER BY id",
            (MAX_FAIL_RETRIES,),
        ).fetchall()
    return [dict(r) for r in rows]


def reset_fail_counts(urls: list[str] | None = None) -> int:
    """Reset fail_count to 0 on FAIL rows so run_retry_failed picks them up again.

    urls=None resets EVERY FAIL row with a non-zero count (the /retry_reset all
    path); otherwise only the rows matching the given URLs. Returns the number
    of rows changed. Deliberately touches fail_count only — the row keeps its
    FAIL status and re-enters the normal retry loop on the next
    RETRY_FAILED_TIMES slot, where today's code may well resolve it differently
    (e.g. a link-rotted posting now comes back as a clean EXPIRED).
    """
    with get_db(DB_PATH) as conn:
        if urls is None:
            cur = conn.execute(
                "UPDATE applications SET fail_count = 0 WHERE ats_status='FAIL' AND fail_count > 0"
            )
        else:
            norms = [n for n in (normalize_url(u) for u in urls if u) if n]
            if not norms:
                return 0
            placeholders = ",".join("?" * len(norms))
            cur = conn.execute(
                "UPDATE applications SET fail_count = 0 "  # noqa: S608 — placeholders only
                f"WHERE ats_status='FAIL' AND fail_count > 0 AND url_norm IN ({placeholders})",
                norms,
            )
        return cur.rowcount


def delete_all_by_url(url: str) -> dict:
    """Delete ALL tracker rows matching this URL (any status).

    Returns: {"deleted": int, "folder": str | None, "drive_url": str | None}
    """
    result: dict = {"deleted": 0, "folder": None, "drive_url": None}
    norm = normalize_url(url)
    if not norm:
        return result

    with get_db(DB_PATH) as conn:
        first = conn.execute(
            "SELECT folder, drive_url FROM applications WHERE url_norm=? ORDER BY rowid LIMIT 1",
            (norm,),
        ).fetchone()
        if first:
            result["folder"] = first["folder"] or None
            result["drive_url"] = first["drive_url"] or None
        cur = conn.execute("DELETE FROM applications WHERE url_norm=?", (norm,))
        result["deleted"] = cur.rowcount
    return result


# ── MANUAL row management ─────────────────────────────────────────────────────


def has_manual_pending(url: str) -> bool:
    """True if tracker already has a MANUAL row for this URL."""
    norm = normalize_url(url)
    if not norm:
        return False
    with get_db(DB_PATH) as conn:
        return bool(
            conn.execute(
                "SELECT 1 FROM applications WHERE url_norm=? AND ats_status=? LIMIT 1",
                (norm, MANUAL_PENDING_ATS),
            ).fetchone()
        )


def remove_manual_pending_rows(url: str) -> int:
    """Delete tracker rows for this URL where ATS is MANUAL."""
    norm = normalize_url(url)
    if not norm:
        return 0
    with get_db(DB_PATH) as conn:
        cur = conn.execute(
            "DELETE FROM applications WHERE url_norm=? AND ats_status=?",
            (norm, MANUAL_PENDING_ATS),
        )
        return cur.rowcount


def manual_jobleads_job_posting_path(url: str) -> Path | None:
    """Return path to job_posting.txt for a MANUAL JobLeads row, or None."""
    norm = normalize_url(url)
    if not norm:
        return None
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            "SELECT folder FROM applications WHERE url_norm=? AND ats_status=? LIMIT 1",
            (norm, MANUAL_PENDING_ATS),
        ).fetchone()
    if not row or not row["folder"]:
        return None
    p = Path(row["folder"])
    if not p.is_absolute():
        p = Path(TRACKER_PATH).parent / row["folder"]
    return p / "job_posting.txt"


def get_all_manual_pending() -> list[dict]:
    """Return all MANUAL-pending rows as dicts: url, folder, company, title, id."""
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, url, folder, company, title FROM applications "
            "WHERE ats_status=? AND url != ''",
            (MANUAL_PENDING_ATS,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "url": r["url"],
            "folder": r["folder"],
            "company": r["company"],
            "title": r["title"],
        }
        for r in rows
    ]


def latest_manual_pending() -> dict[str, str] | None:
    """Return latest MANUAL row info (url, folder) or None."""
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            "SELECT url, folder FROM applications "
            "WHERE ats_status=? AND url != '' ORDER BY rowid DESC LIMIT 1",
            (MANUAL_PENDING_ATS,),
        ).fetchone()
    if not row:
        return None
    return {"url": row["url"], "folder": row["folder"]}


# ── Write operations ──────────────────────────────────────────────────────────


def add_manual_jobleads_pending(
    *,
    url: str,
    company: str,
    title: str,
    folder_abs: Path,
) -> bool:
    """Append MANUAL row for JobLeads when description cannot be fetched.

    Returns False when URL already has any tracker row (dedup / FAIL / MANUAL / success).
    A PENDING/IN_PROGRESS placeholder for this same URL (M1, queue mode) does
    not count — it's replaced in place, same as every other terminal write.
    """
    if has_successful_entry(url) or any(
        r["ats"] not in (PENDING_ATS, IN_PROGRESS_ATS) for r in lookup_url(url)
    ):
        return False

    row_id = _new_row_id()
    norm = normalize_url(url) if url else ""
    today = date.today().strftime("%Y-%m-%d")
    folder_str = str(folder_abs).replace("\\", "/")

    with get_db(DB_PATH) as conn:
        _clear_own_placeholder(conn, url)
        conn.execute(
            """
            INSERT INTO applications
            (id, date, user_id, company, title, ats_status, url, url_norm, folder, to_learn)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                today,
                _uid(),
                company.strip() or "Unknown",
                title.strip() or "Unknown",
                MANUAL_PENDING_ATS,
                url,
                norm,
                folder_str,
                "Paste job text into job_posting.txt in Folder, then re-run apply (same URL).",
            ),
        )

    return True


def get_recent_applied_for_repost(window_days: int) -> list[dict]:
    """Applied rows from the last `window_days` days that have a docs folder —
    the donor candidates for the re-post gate (hunter.repost_gate). Excludes
    non-apply statuses (SKIP/FAIL/EXPIRED/MANUAL): only a row whose CV was
    actually generated can donate its documents. Newest first."""
    cutoff = (date.today() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT url, company, title, folder, date
              FROM applications
             WHERE folder != '' AND folder IS NOT NULL
               AND date >= ?
               AND upper(coalesce(ats_status, '')) NOT IN ('SKIP', 'FAIL', 'EXPIRED', ?)
               AND user_id = ?
             ORDER BY date DESC, id DESC
            """,
            (cutoff, MANUAL_PENDING_ATS, _uid()),
        ).fetchall()
    return [
        {
            "url": r["url"] or "",
            "company": r["company"] or "",
            "title": r["title"] or "",
            "folder": r["folder"],
            "date": r["date"] or "",
        }
        for r in rows
    ]


def add_applied(content: dict, force: bool = False, reapplication: bool = False) -> bool:
    """Append a successful apply row. Returns True when a row was written.

    `reapplication=True` forces the Re-application flag regardless of URL
    history — used by the re-post gate, where the URL is NEW by definition
    (that's why URL dedup missed it) but the vacancy itself is a repeat."""
    company = _company_from_content(content)
    job_title = str(content.get("job_title", "") or "")
    stack = str(content.get("stack", "") or "")
    apply_url = str(content.get("apply_url", "") or "")
    if apply_url:
        remove_manual_pending_rows(apply_url)
    folder = str(content.get("output_folder", "") or "")
    to_learn = str(content.get("to_learn", "") or "")
    ats_raw = str(content.get("ats_score", "") or "")
    ats_display, _ = _parse_ats_score(ats_raw)
    today = date.today().strftime("%Y-%m-%d")
    norm_url = normalize_url(apply_url) if apply_url else ""
    # Per-vacancy USD spent on LLM calls — recorded by apply_api into
    # content["cost"] = {"total_usd": 0.47, "by_model": ..., ...}. CLI runs
    # stamp {"mode": "cli", "total_usd": None}. We persist only the scalar
    # total; the full breakdown stays on content.json next to the docs.
    cost_payload = content.get("cost") if isinstance(content.get("cost"), dict) else None
    cost_usd = cost_payload.get("total_usd") if cost_payload else None

    with get_db(DB_PATH) as conn:
        # Atomic dedup: check + insert inside ONE transaction so concurrent
        # processes cannot both pass the check before either writes.
        if norm_url and not force:
            existing = conn.execute(
                "SELECT ats_status, sent FROM applications WHERE url_norm=?",
                (norm_url,),
            ).fetchall()
            for row in existing:
                ats = (row["ats_status"] or "").strip().upper()
                # PENDING/IN_PROGRESS (M1, queue mode) is this job's OWN
                # placeholder, not a prior successful entry — never blocks.
                if ats not in (
                    "FAIL",
                    "SKIP",
                    "?",
                    "",
                    MANUAL_PENDING_ATS,
                    PENDING_ATS,
                    IN_PROGRESS_ATS,
                ):
                    return False  # already has a successful entry

        # Is this a re-application (same URL exists with any status)?
        # Check BEFORE the force-mode delete below so the flag is accurate.
        # A PENDING/IN_PROGRESS placeholder doesn't count — every normal
        # queued apply would otherwise be misflagged as a re-application.
        is_reapply = bool(
            norm_url
            and conn.execute(
                f"SELECT 1 FROM applications WHERE url_norm=? "  # noqa: S608
                f"AND ats_status NOT IN ('{PENDING_ATS}', '{IN_PROGRESS_ATS}') LIMIT 1",
                (norm_url,),
            ).fetchone()
        )

        # Force mode: delete any stale rows before inserting the fresh one.
        # _force_cleanup should have already done this, but if it missed the row
        # (e.g. URL normalisation mismatch) we'd end up with duplicate rows and
        # get_folder_by_url would return the OLD folder path (yesterday's date).
        if force and norm_url:
            conn.execute("DELETE FROM applications WHERE url_norm=?", (norm_url,))
        elif norm_url:
            # Replace this job's own PENDING/IN_PROGRESS placeholder in place
            # (M1) — leaving it would duplicate the row once the real one
            # below is inserted.
            _clear_own_placeholder(conn, apply_url)
            # Same for this user's own FAIL/SKIP/blank row: the dedup check
            # above deliberately lets a successful apply through past those
            # statuses, but the unique (user_id, url_norm) index (B1) allows
            # only ONE row per URL — without this delete the INSERT below
            # raises IntegrityError, which is how a genuinely successful
            # RETRY crashed at the tracker write (the retry loop removes the
            # FAIL row only AFTER the subprocess exits — too late).
            conn.execute(
                "DELETE FROM applications WHERE url_norm=? AND user_id=? "
                "AND ats_status IN ('FAIL', 'SKIP', '?', '')",
                (norm_url, _uid()),
            )

        conn.execute(
            """
            INSERT INTO applications
            (id, date, user_id, company, title, stack, ats_status, url, url_norm,
             folder, sent, reapplication, to_learn, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)
            """,
            (
                _new_row_id(),
                today,
                _uid(),
                company,
                job_title,
                stack,
                ats_display,
                norm_url or apply_url,
                norm_url,
                folder,
                "+" if (is_reapply or reapplication) else "",
                to_learn,
                cost_usd,
            ),
        )
    return True


def add_skipped(job: Job) -> dict | None:
    """Append a SKIP row to tracker. Returns the row dict (with ID) or None if already known.

    Sent is stamped '—' so the row never shows up in the owner's Sheets
    "Sent is empty" send-queue filter view — a skipped vacancy is a decision,
    not a pending application. Side effect (accepted): '—' on a SKIP row is
    the react-skip marker, so a plain re-paste of the URL is refused by
    ``_already_processed``; ``/force`` still regenerates (and for doomed-gate
    skips a plain paste would just re-hit the gate anyway).

    An existing FAIL row for the same URL is converted to SKIP in place
    (returns None — the row is already mirrored, resync pushes the change);
    see _convert_own_fail_row.
    """
    if _convert_own_fail_row(job.url, sent="—"):
        return None
    if _is_known_terminal(job.url):
        return None

    row_id = _new_row_id()
    norm = normalize_url(job.url)
    today = date.today().strftime("%Y-%m-%d")

    with get_db(DB_PATH) as conn:
        _clear_own_placeholder(conn, job.url)
        conn.execute(
            """
            INSERT INTO applications
            (id, date, user_id, company, title, ats_status, url, url_norm, sent)
            VALUES (?, ?, ?, ?, ?, 'SKIP', ?, ?, '—')
            """,
            (row_id, today, _uid(), job.company, job.title, norm, norm),
        )

    return {
        "Date": today,
        "Company": job.company,
        "Job Title": job.title,
        "Stack": "",
        "ATS %": "SKIP",
        "URL": norm,
        "Folder": "",
        "Sent": "—",
        "Re-application": "",
        "To Learn": "",
        "ID": row_id,
        "Drive URL": "",
        "Confirmation": "",
        "Answer": "",
    }


def add_react_skipped(content: dict, url: str) -> None:
    """Write a SKIP row for a React-only job. Sent='—' marks it as stack-filtered."""
    company = (content.get("company_name") or "").strip()
    title = (content.get("job_title") or "").strip()
    if _is_known_terminal(url):
        return

    norm = normalize_url(url) if url else ""
    today = date.today().strftime("%Y-%m-%d")

    with get_db(DB_PATH) as conn:
        _clear_own_placeholder(conn, url)
        conn.execute(
            """
            INSERT INTO applications
            (id, date, user_id, company, title, stack, ats_status, url, url_norm, sent)
            VALUES (?, ?, ?, ?, ?, ?, 'SKIP', ?, ?, '—')
            """,
            (
                _new_row_id(),
                today,
                _uid(),
                company,
                title,
                content.get("stack", ""),
                norm,
                norm,
            ),
        )


def _convert_own_fail_row(url: str, sent: str) -> bool:
    """Convert this user's FAIL row for `url` into a terminal SKIP row in place.

    Retry context: the vacancy died (or hit a gate) between the original FAIL
    and the retry. Updating the existing row keeps its id/sheets_row and marks
    it sheets_dirty, so the next resync pushes the new status onto the
    already-mirrored Sheet row instead of appending a duplicate. Returns True
    when a row was converted. Without this, _is_known_terminal() saw the FAIL
    row and silently dropped the terminal write, so a retried-then-expired
    vacancy left no marker at all (retry log 2026-08-10).
    """
    norm = normalize_url(url) if url else ""
    if not norm:
        return False
    with get_db(DB_PATH) as conn:
        cur = conn.execute(
            "UPDATE applications SET ats_status='SKIP', sent=?, sheets_dirty=1 "
            "WHERE url_norm=? AND user_id=? AND ats_status='FAIL'",
            (sent, norm, _uid()),
        )
        return bool(cur.rowcount)


def convert_own_applied_row(
    url: str = "",
    *,
    folder: str = "",
    status: str = "SKIP",
    sent: str = "—",
) -> bool:
    """Convert this user's APPLIED row into a terminal SKIP row, in place.

    The CLI pipeline's abort stages (React-only stack, company+title dedup,
    judge block, language-gate block) all run after the CLI skill has already
    rendered the documents and written the tracker row through generate_docs
    (`.claude/commands/apply.md` calls it without --no-tracker). Their own
    terminal writes then no-op on `_is_known_terminal`, and `apply_worker.
    _resolve_outcome` sees `has_successful_entry` on exit 0 and DELIVERS the
    package the stage just decided to throw away (docs/STACK_PRESCREEN_PLAN.md
    M1; measured 2026-08-24: 6 of 14 main_cli runs shipped a row carrying the
    skill's self-reported ATS score with no independent verdict at all).

    Updating in place -- rather than delete + insert -- keeps the row's id and
    `sheets_row` and marks it `sheets_dirty`, so a row already mirrored to the
    Sheet is CORRECTED there instead of gaining a duplicate.

    **Identity must be exact.** `url` and `folder` should come from the
    content.json the row was WRITTEN from (`apply_url` / `output_folder`) --
    those are the literal values `add_applied` stored. The pipeline's own idea
    of the URL is not a safe substitute: paste mode never hands the skill a URL
    (the row lands with `url_norm=''`) and `.claude/commands/apply.md` lets the
    skill record the apply-button URL instead of the input one.

    Matching is exact equality only. An earlier version also matched a
    `<date>/<Company>` suffix to survive relative-vs-absolute path spellings;
    a review reproduced it converting a SECOND, unrelated row -- two runs for
    the same company on the same day under different roots share that suffix,
    and `_` in a sanitised company name is a LIKE wildcard. A destructive write
    does not get to guess: when more than one row matches, nothing is converted
    and a warning is logged, because ambiguity means the identity is wrong and
    the caller needs to know.

    `folder` is left in place on the converted row: the abort deletes the
    rendered documents but keeps job_posting.txt for diagnostics, and a SKIP row
    can never become a re-post donor (get_recent_applied_for_repost excludes
    SKIP). The Drive backfill knows about it -- see
    gdrive_sync.upload_missing_folders, which skips non-applied rows.

    Returns True only when exactly one row was converted.
    """
    norm = normalize_url(url) if url else ""
    folders = []
    if folder:
        folders.append(str(folder))
        try:
            resolved = str(Path(folder).resolve())
            if resolved != str(folder):
                folders.append(resolved)
        except OSError:  # pragma: no cover - resolve() on a hostile path
            pass
    if not norm and not folders:
        return False

    where = []
    params: list = []
    if norm:
        where.append("url_norm=?")
        params.append(norm)
    if folders:
        where.append(f"folder IN ({','.join('?' * len(folders))})")
        params.extend(folders)
    params.append(_uid())

    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT rowid, url, folder FROM applications "  # noqa: S608
            f"WHERE ({' OR '.join(where)}) AND user_id=? "
            "AND upper(coalesce(ats_status, '')) NOT IN ('SKIP', 'FAIL', 'EXPIRED', ?, ?, ?)",
            [*params, MANUAL_PENDING_ATS, PENDING_ATS, IN_PROGRESS_ATS],
        ).fetchall()
        if not rows:
            return False
        if len(rows) > 1:
            log.warning(
                "convert_own_applied_row: %d rows match url=%r folder=%r - refusing to convert "
                "any of them (%s)",
                len(rows),
                url,
                folder,
                ", ".join(r["folder"] or r["url"] or "?" for r in rows),
            )
            return False
        conn.execute(
            "UPDATE applications SET ats_status=?, sent=?, sheets_dirty=1 WHERE rowid=?",
            (status, sent, rows[0]["rowid"]),
        )
    return True


def add_expired(url: str, company: str = "", title: str = "") -> None:
    """Write an expired row — offer was no longer active when fetched.

    Convention (matches expired_marker / mark_orphans_expired): the EXPIRED
    marker lives in the ``Sent`` column; the ATS column gets ``SKIP`` (no CV was
    generated). Both ``SKIP`` and a non-blank ``sent`` keep the row out of future
    hunts via the dedup/cooldown path.

    An existing FAIL row for the same URL is converted in place instead of
    blocking the write — see _convert_own_fail_row.
    """
    if _convert_own_fail_row(url, sent="EXPIRED"):
        return
    if _is_known_terminal(url):
        return

    norm = normalize_url(url) if url else ""
    today = date.today().strftime("%Y-%m-%d")

    with get_db(DB_PATH) as conn:
        _clear_own_placeholder(conn, url)
        conn.execute(
            """
            INSERT INTO applications
            (id, date, user_id, company, title, ats_status, url, url_norm, sent)
            VALUES (?, ?, ?, ?, ?, 'SKIP', ?, ?, 'EXPIRED')
            """,
            (_new_row_id(), today, _uid(), company, title, norm, norm),
        )


def add_failed(job: Job) -> None:
    """Append a FAIL row so the job is not retried on next hunt.

    A PENDING/IN_PROGRESS placeholder for this same URL (M1, queue mode) is
    replaced in place rather than blocking the write — see _is_known_terminal.

    Sent is stamped '—' to keep the row out of the owner's Sheets send-queue
    filter (blank Sent = "waiting to be sent", which a FAIL row never is).
    Harmless for retries: the retry loop selects by ats_status='FAIL' only,
    and the react-skip check requires ats_status='SKIP', so a dash on a FAIL
    row blocks nothing. On a successful retry the row is deleted and replaced
    by a normal applied row anyway (delete_failed_row).
    """
    if _is_known_terminal(job.url):
        return

    norm = normalize_url(job.url)
    today = date.today().strftime("%Y-%m-%d")

    with get_db(DB_PATH) as conn:
        _clear_own_placeholder(conn, job.url)
        conn.execute(
            """
            INSERT INTO applications
            (id, date, user_id, company, title, ats_status, url, url_norm, sent)
            VALUES (?, ?, ?, ?, ?, 'FAIL', ?, ?, '—')
            """,
            (_new_row_id(), today, _uid(), job.company, job.title, norm, norm),
        )


# ── PENDING queue (M1, docs/HUNT_APPLY_SPLIT_PLAN.md) ─────────────────────────
#
# Decouples "hunt found this job" from "apply worker processed it": the hunt
# loop calls add_pending() for every new job and moves on immediately instead
# of running the apply subprocess inline under _hunt_lock; a separate
# apply_worker_loop drains the queue on its own schedule. Feature-gated by
# APPLY_QUEUE_ENABLED — with the flag off nothing in this section is called.


def _serialize_pending_meta(job: Job) -> str:
    """JSON-encode everything apply_worker needs to rebuild this Job.

    `default=str` covers non-JSON-native values that can end up in `raw`/
    `email_meta` (e.g. Gmail's `email_meta["date"]` datetime) — the queue
    only needs these fields for display/logging on the worker side, not for
    round-tripping the exact Python type.
    """
    return json.dumps(
        {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "salary": job.salary,
            "url": job.url,
            "source": job.source,
            "raw": job.raw or {},
            "email_meta": job.email_meta or {},
        },
        ensure_ascii=False,
        default=str,
    )


def add_pending(job: Job) -> str:
    """INSERT a PENDING row for a job the hunt loop found but hasn't applied to yet.

    Stores the full Job as JSON in `pending_meta` (see `_serialize_pending_meta`)
    so `job_from_pending_row` can reconstruct it later without re-fetching or
    re-filtering. Returns the new row id.

    Mirrors add_skipped/add_failed's contract: callers must check
    is_known()/should_skip_url() themselves first — this function does not
    dedup on its own.
    """
    row_id = _new_row_id()
    norm = normalize_url(job.url)
    today = date.today().strftime("%Y-%m-%d")

    with get_db(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, user_id, company, title, ats_status, url, url_norm, pending_meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                today,
                _uid(),
                job.company,
                job.title,
                PENDING_ATS,
                job.url,
                norm,
                _serialize_pending_meta(job),
            ),
        )
    return row_id


def job_from_pending_row(row: dict) -> Job:
    """Reconstruct a Job from a claimed PENDING/IN_PROGRESS row's `pending_meta`.

    Falls back to the row's own company/title/url columns when `pending_meta`
    is missing or unparsable (defensive — should not happen for a row written
    by add_pending, but a hand-edited/legacy row must not crash the worker).
    """
    meta: dict = {}
    raw_meta = row.get("pending_meta")
    if raw_meta:
        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    return Job(
        title=meta.get("title") or row.get("title") or "",
        company=meta.get("company") or row.get("company") or "",
        location=meta.get("location") or "",
        salary=meta.get("salary"),
        url=meta.get("url") or row.get("url") or "",
        source=meta.get("source") or "pending",
        raw=meta.get("raw") or {},
        email_meta=meta.get("email_meta") or {},
    )


def claim_pending() -> dict | None:
    """Atomically claim the oldest PENDING row: ats_status -> IN_PROGRESS + claimed_at.

    A single UPDATE...RETURNING statement (SQLite >= 3.35) — the row selection
    and the status flip happen in one atomic write, so two workers can never
    both claim the same row (M2/parallel workers is deferred, but this makes
    the primitive correct now instead of needing a rewrite later). Ordered by
    SQLite's implicit `rowid` (true insertion order) rather than `id` — the
    primary key is a random UUID hex fragment (`_new_row_id`), not a
    sequential one, so sorting by it would NOT give FIFO. Returns the full
    row as a dict (including `pending_meta`), or None when the queue is
    empty.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            f"""
            UPDATE applications
            SET ats_status='{IN_PROGRESS_ATS}', claimed_at=?
            WHERE rowid = (
                SELECT rowid FROM applications
                WHERE ats_status='{PENDING_ATS}'
                ORDER BY rowid
                LIMIT 1
            )
            RETURNING *
            """,  # noqa: S608 — no interpolated user input, both constants are module-level literals
            (now,),
        ).fetchone()
    return dict(row) if row else None


def release_claim(url: str) -> None:
    """IN_PROGRESS -> PENDING, clearing claimed_at.

    Used for outcomes that are infrastructure hiccups, not the vacancy's
    fault (cli_timeout, llm_outage) — the row goes back to the front of the
    queue instead of being marked FAIL.
    """
    norm = normalize_url(url)
    if not norm:
        return
    with get_db(DB_PATH) as conn:
        conn.execute(
            f"UPDATE applications SET ats_status='{PENDING_ATS}', claimed_at=NULL "  # noqa: S608
            f"WHERE ats_status='{IN_PROGRESS_ATS}' AND url_norm=?",
            (norm,),
        )


def reset_stale_claims(timeout_min: int) -> int:
    """IN_PROGRESS -> PENDING for rows claimed more than `timeout_min` minutes ago.

    A worker that crashed or was killed mid-apply leaves its row stuck
    IN_PROGRESS forever; the periodic sweep (hunter/schedules) calls this so
    it re-enters the queue instead of being lost. Returns the number of rows
    reset. A row with a NULL `claimed_at` (should not happen via
    claim_pending, but defensive) is left alone — there's no age to compare.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=timeout_min)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with get_db(DB_PATH) as conn:
        cur = conn.execute(
            f"UPDATE applications SET ats_status='{PENDING_ATS}', claimed_at=NULL "  # noqa: S608
            f"WHERE ats_status='{IN_PROGRESS_ATS}' AND claimed_at IS NOT NULL AND claimed_at < ?",
            (cutoff,),
        )
        return cur.rowcount


def count_pending() -> int:
    """Number of PENDING rows currently queued (for /status, /queue)."""
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM applications WHERE ats_status='{PENDING_ATS}'"  # noqa: S608
        ).fetchone()
    return int(row["n"]) if row else 0


def count_in_progress() -> int:
    """Number of IN_PROGRESS rows currently claimed by a worker (for /status)."""
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM applications WHERE ats_status='{IN_PROGRESS_ATS}'"  # noqa: S608
        ).fetchone()
    return int(row["n"]) if row else 0


def list_pending(limit: int = 20) -> list[dict]:
    """PENDING rows in queue order (oldest first) for /queue. Each: {company, title, url, date}."""
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT company, title, url, date FROM applications "  # noqa: S608
            f"WHERE ats_status='{PENDING_ATS}' ORDER BY rowid LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_pending_row(url: str) -> bool:
    """Delete a PENDING/IN_PROGRESS row for this URL outright.

    Used by `apply_worker._resolve_outcome` when apply_agent exits 0 without
    writing a terminal row (soft abort — too-short text, lang/judge block,
    bogus company): drop the placeholder so the URL isn't stranded
    IN_PROGRESS / deduped until the stale-claim sweep. Also the manual
    escape hatch for a future `/queue cancel <url>`. Returns True if a row
    was deleted.
    """
    norm = normalize_url(url)
    if not norm:
        return False
    with get_db(DB_PATH) as conn:
        cur = conn.execute(
            f"DELETE FROM applications WHERE url_norm=? AND ats_status IN ('{PENDING_ATS}', '{IN_PROGRESS_ATS}')",  # noqa: S608
            (norm,),
        )
        return cur.rowcount > 0


# ── Unsent rows ───────────────────────────────────────────────────────────────


def iter_unsent_rows() -> list[dict]:
    """Return unsent tracker rows (no Sent value, excluding SKIP).

    Each dict has keys: id, date, company, title, stack, ats, url, folder,
    sent, reapp, to_learn.

    PENDING/IN_PROGRESS rows (M1, queue mode) are excluded — they haven't
    been attempted yet, so an expiry check here would be premature (the
    apply pipeline runs its own expiry check when it eventually processes
    the row) and a stray EXPIRED stamp on a still-queued row would leave it
    in a confusing half-state (ats_status='PENDING', sent='EXPIRED').
    """
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            f"""
            SELECT id, date, company, title, stack, ats_status, url, folder,
                   sent, reapplication, to_learn
            FROM applications
            WHERE ats_status != 'SKIP'
              AND ats_status NOT IN ('{PENDING_ATS}', '{IN_PROGRESS_ATS}')
              AND id != ''
              AND (sent = '' OR sent IN ('—', '–', '-'))
              AND user_id = ?
            """,  # noqa: S608 — no interpolated user input, both constants are module-level literals
            (_uid(),),
        ).fetchall()

    return [
        {
            "id": r["id"],
            "date": r["date"],
            "company": r["company"],
            "title": r["title"],
            "stack": r["stack"],
            "ats": r["ats_status"],
            "url": r["url"],
            "folder": r["folder"],
            "sent": r["sent"],
            "reapp": r["reapplication"],
            "to_learn": r["to_learn"],
        }
        for r in rows
    ]


# ── Pull-sync updates ─────────────────────────────────────────────────────────


def apply_sent_updates(updates: dict[str, str]) -> int:
    """Write Sent values (e.g. EXPIRED) back into tracker.

    updates: {row_id: sent_value} — only non-empty sent_value entries.
    Returns number of rows updated.
    """
    if not updates:
        return 0
    updated = 0
    with get_db(DB_PATH) as conn:
        for row_id, sent_val in updates.items():
            if sent_val:
                cur = conn.execute("UPDATE applications SET sent=? WHERE id=?", (sent_val, row_id))
                updated += cur.rowcount
    return updated


def apply_pull_updates(rows: list[dict]) -> int:
    """Write Sheets-sourced field changes back to tracker (pull sync).

    rows: list of row dicts (with 'ID') where Sheets had a newer value.
    Updates Sent, Re-application, To Learn columns. Returns count updated.
    """
    if not rows:
        return 0
    updated = 0
    with get_db(DB_PATH) as conn:
        for row_dict in rows:
            row_id = row_dict.get("ID", "").strip()
            if not row_id:
                continue
            cur = conn.execute(
                "UPDATE applications SET sent=?, reapplication=?, to_learn=? WHERE id=?",
                (
                    row_dict.get("Sent", ""),
                    row_dict.get("Re-application", ""),
                    row_dict.get("To Learn", ""),
                    row_id,
                ),
            )
            updated += cur.rowcount
    return updated


def mark_orphans_expired(row_ids: list[str]) -> int:
    """Mark rows whose ID vanished from Sheets as EXPIRED (orphan reconcile).

    Called by gsheets_sync when the user (or a cleanup tool) deletes rows from the
    Sheet that still linger in the DB with a blank Sent. Sets Sent='EXPIRED' so the
    row drops out of unsent/active counts, and clears sheets_dirty + the now-stale
    sheets_row so the bot never tries to push it back to a wrong Sheet position.

    The row itself is kept (not deleted) so URL/company+title dedup still protects
    against re-applying. Returns count actually changed.

    Guards (a row is expired only when BOTH hold):
    - ``TRIM(sent)=''`` — never overwrite an existing Sent value.
    - ``sheets_row IS NOT NULL`` — the row must have been mirrored to the Sheet
      before (``set_sheets_row`` only runs on a successful append/match). This
      distinguishes a genuine *deletion from the Sheet* from a row that was simply
      *never pushed* — e.g. while the Sheets token was down, ``mirror_new_row``
      returned early and left ``sheets_row`` NULL. Without this guard a
      never-mirrored row looks identical to a user deletion ("ID in DB, absent
      from the Sheet") and gets falsely stamped EXPIRED on the next pull.
    """
    if not row_ids:
        return 0
    updated = 0
    with get_db(DB_PATH) as conn:
        for row_id in row_ids:
            rid = (row_id or "").strip()
            if not rid:
                continue
            cur = conn.execute(
                "UPDATE applications SET sent='EXPIRED', sheets_dirty=0, sheets_row=NULL "
                "WHERE id=? AND TRIM(COALESCE(sent,''))='' "
                "AND sheets_row IS NOT NULL",
                (rid,),
            )
            updated += cur.rowcount
    return updated


def insert_pulled_rows(rows: list[tuple[int, dict]]) -> int:
    """Insert Sheets rows that are absent from the DB (dedup self-heal).

    rows: list of (sheet_row_index, row_dict) as returned by gsheets_client.read_all.
    For each row whose ID is non-empty AND neither its ID nor its url_norm already
    exists in the DB, insert a fresh applications row (sheets_row set, sheets_dirty=0).
    Returns count inserted.

    Existing rows (matched by id OR url_norm) are never touched — field updates are
    the job of the conflict matrix in gsheets_sync._apply_pull_delta_db. Rows sharing
    the same url_norm within the batch collapse to the first one (historical dupes).
    """
    if not rows:
        return 0

    inserted = 0
    with get_db(DB_PATH) as conn:
        existing = conn.execute("SELECT id, url_norm FROM applications").fetchall()
        existing_ids = {r["id"] for r in existing}
        existing_norms = {r["url_norm"] for r in existing if r["url_norm"]}

        for sheet_idx, row in rows:
            row_id = (row.get("ID") or "").strip()
            if not row_id:
                continue  # non-syncable row — skip (counted by caller via len delta)
            if not re.fullmatch(r"[0-9a-zA-Z]{8}", row_id):
                # Garbage sheet row (e.g. a URL or bare number in the ID column
                # from an old column-shift incident) — never resurrect it into
                # the DB. Real IDs are 8-char uuid hex.
                continue
            if row_id in existing_ids:
                continue
            raw_url = (row.get("URL") or "").strip()
            url_norm = normalize_url(raw_url) if raw_url else ""
            if url_norm and url_norm in existing_norms:
                continue

            conn.execute(
                """
                INSERT OR IGNORE INTO applications
                (id, date, user_id, company, title, stack, ats_status, url, url_norm,
                 folder, sent, reapplication, to_learn, drive_url,
                 confirmation, answer, sheets_row, sheets_dirty, cost_usd)
                VALUES
                (:id, :date, :user_id, :company, :title, :stack, :ats_status, :url, :url_norm,
                 :folder, :sent, :reapplication, :to_learn, :drive_url,
                 :confirmation, :answer, :sheets_row, :sheets_dirty, :cost_usd)
                """,
                {
                    "id": row_id,
                    "date": row.get("Date", ""),
                    "user_id": _uid(),
                    "company": row.get("Company", ""),
                    "title": row.get("Job Title", ""),
                    "stack": row.get("Stack", ""),
                    "ats_status": row.get("ATS %", ""),
                    "url": raw_url,
                    "url_norm": url_norm,
                    "folder": row.get("Folder", ""),
                    "sent": row.get("Sent", ""),
                    "reapplication": row.get("Re-application", ""),
                    "to_learn": row.get("To Learn", ""),
                    "drive_url": row.get("Drive URL", ""),
                    "confirmation": row.get("Confirmation", ""),
                    "answer": row.get("Answer", ""),
                    "sheets_row": sheet_idx,
                    "sheets_dirty": 0,
                    "cost_usd": _parse_cost_cell(row.get("Cost $", "")),
                },
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
                existing_ids.add(row_id)
                if url_norm:
                    existing_norms.add(url_norm)

    return inserted


# ── Folder / Drive URL ────────────────────────────────────────────────────────


def get_folder_by_url(url: str) -> str | None:
    """Return the Folder value for a given job URL, or None if not found."""
    norm = normalize_url(url)
    if not norm:
        return None
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            "SELECT folder FROM applications WHERE url_norm=? ORDER BY rowid DESC LIMIT 1", (norm,)
        ).fetchone()
    if row and row["folder"]:
        return row["folder"]
    return None


def get_drive_url_by_url(url: str) -> str | None:
    """Return the Drive URL stored for this job URL, or None."""
    norm = normalize_url(url)
    if not norm:
        return None
    with get_db(DB_PATH) as conn:
        row = conn.execute(
            "SELECT drive_url FROM applications WHERE url_norm=? LIMIT 1", (norm,)
        ).fetchone()
    if row and row["drive_url"]:
        return row["drive_url"]
    return None


def set_drive_url(url: str, drive_url: str) -> None:
    """Write drive_url for the first tracker row matching this job URL."""
    if not url or not drive_url:
        return
    norm = normalize_url(url)
    if not norm:
        return
    with get_db(DB_PATH) as conn:
        conn.execute("UPDATE applications SET drive_url=? WHERE url_norm=?", (drive_url, norm))


def set_ats_verdict(url: str, score: float) -> bool:
    """Stamp the independent PDF-verdict score on the row matching `url`.

    The verdict is computed AFTER the tracker row exists (apply Step 7.7,
    while the row is written by generate_docs in Step 7), so this is a
    post-hoc UPDATE by normalized URL — same shape as set_drive_url.

    Also overwrites `ats_status` (the "ATS %" column) with the same score,
    formatted like the self-score it replaces (e.g. "92%"). The owner asked
    for a single number across every interface — the tracker/Sheet "ATS %"
    column and the Telegram card should both show the independent verdict,
    not the generator's own self-assessment (see docs/VERDICT_REFINE_PLAN.md
    M4). Returns True if a row was updated. Never raises (best-effort caller).
    """
    if not url:
        return False
    try:
        norm = normalize_url(url)
        if not norm:
            return False
        ats_display = f"{int(round(float(score)))}%"
        with get_db(DB_PATH) as conn:
            cur = conn.execute(
                "UPDATE applications SET ats_verdict=?, ats_status=? WHERE url_norm=?",
                (float(score), ats_display, norm),
            )
            return cur.rowcount > 0
    except Exception as e:
        print(f"[tracker] set_ats_verdict failed (continuing): {e}")
        return False


def set_cost(url: str, cost_usd: float) -> bool:
    """Re-stamp the per-vacancy LLM cost on the row matching `url`.

    The row is created in apply Step 7 (generate_docs -> add_applied) with
    the Step 6.5 figure — priced BEFORE the independent PDF verdict and the
    verdict refine loop, which can more than double the real spend (rewrite
    rounds, judge re-runs, re-verdicts, PL mirror). The pipeline re-prices
    the full usage log after the verdict block and re-stamps here — post-hoc
    UPDATE by normalized URL, same shape as set_ats_verdict. The Sheets
    column-M mirror (hunter.cost_writer) reads cost_usd from the DB when the
    bot process mirrors the row, so it picks this value up automatically.
    Returns True if a row was updated. Never raises (best-effort caller).
    """
    if not url:
        return False
    try:
        norm = normalize_url(url)
        if not norm:
            return False
        with get_db(DB_PATH) as conn:
            cur = conn.execute(
                "UPDATE applications SET cost_usd=? WHERE url_norm=?",
                (float(cost_usd), norm),
            )
            return cur.rowcount > 0
    except Exception as e:
        print(f"[tracker] set_cost failed (continuing): {e}")
        return False


def set_to_learn(url: str, to_learn: str) -> bool:
    """Overwrite the "To Learn" column for the row matching `url`.

    The verdict refine loop (hunter.verdict_refine) may append stretch-round
    tech additions to content["to_learn"] AFTER the tracker row
    already exists (it's created in Step 7, generate_docs -> add_applied,
    with the PRE-loop value) — so this is a post-hoc UPDATE by normalized
    URL, same shape as set_ats_verdict. Returns True if a row was updated.
    Never raises (best-effort caller).
    """
    if not url:
        return False
    try:
        norm = normalize_url(url)
        if not norm:
            return False
        with get_db(DB_PATH) as conn:
            cur = conn.execute(
                "UPDATE applications SET to_learn=? WHERE url_norm=?",
                (str(to_learn or ""), norm),
            )
            return cur.rowcount > 0
    except Exception as e:
        print(f"[tracker] set_to_learn failed (continuing): {e}")
        return False


# ── Google Sheets sync helpers ────────────────────────────────────────────────


def read_all_tracker_rows() -> list[dict]:
    """Read every data row as a dict keyed by column names.

    Keys: all _GSHEETS_COLS names + "Drive URL", "Confirmation", "Answer".
    Rows with no ID are skipped. Used by gsheets_sync and gdrive_sync.

    PENDING/IN_PROGRESS rows (M1, queue mode) are excluded — Sheets/Drive
    stay exactly as blind to the internal queue as they were before it
    existed; pushing a placeholder with no folder/score would be noise at
    best and a premature Drive-upload attempt at worst.
    """
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            f"""
            SELECT id, date, company, title, stack, ats_status, url, folder,
                   sent, reapplication, to_learn, drive_url, confirmation, answer
            FROM applications
            WHERE id != ''
              AND ats_status NOT IN ('{PENDING_ATS}', '{IN_PROGRESS_ATS}')
              AND user_id = ?
            """,  # noqa: S608 — no interpolated user input, both constants are module-level literals
            (_uid(),),
        ).fetchall()

    result = []
    for r in rows:
        d = {
            "Date": r["date"],
            "Company": r["company"],
            "Job Title": r["title"],
            "Stack": r["stack"],
            "ATS %": r["ats_status"],
            "URL": r["url"],
            "Folder": r["folder"],
            "Sent": r["sent"],
            "Re-application": r["reapplication"],
            "To Learn": r["to_learn"],
            "ID": r["id"],
            "Drive URL": r["drive_url"],
            "Confirmation": r["confirmation"],
            "Answer": r["answer"],
        }
        result.append(d)
    return result


# ── Cooldown ──────────────────────────────────────────────────────────────────


def is_in_cooldown(company: str, title: str, cooldown_days: int = 12) -> bool:
    """Return True if company+title was applied to within the last cooldown_days."""
    import datetime as _dt

    target_key = dedup_key(company, title)
    today = date.today()
    placeholders = ",".join("?" * len(_COOLDOWN_SKIP_STATUSES))

    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, company, title FROM applications "  # noqa: S608 — placeholders only
            f"WHERE ats_status NOT IN ({placeholders}) "
            "AND ats_status != ''",
            tuple(_COOLDOWN_SKIP_STATUSES),
        ).fetchall()

    most_recent: date | None = None
    for row in rows:
        if not row["company"] and not row["title"]:
            continue
        if dedup_key(row["company"], row["title"]) != target_key:
            continue
        row_date_str = str(row["date"] or "").strip()
        try:
            row_date = _dt.date.fromisoformat(row_date_str)
        except ValueError:
            continue
        if most_recent is None or row_date > most_recent:
            most_recent = row_date

    if most_recent is None:
        return False
    return (today - most_recent).days < cooldown_days


def company_cooldown_active(company: str, days: int = 180) -> bool:
    """Return True if ANY application to this company was made in the last *days*."""
    import datetime as _dt

    norm = normalize_company(company)
    if not norm:
        return False

    today = date.today()
    placeholders = ",".join("?" * len(_COOLDOWN_SKIP_STATUSES))

    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, company FROM applications "  # noqa: S608 — placeholders only
            f"WHERE ats_status NOT IN ({placeholders}) "
            "AND ats_status != '' AND company != ''",
            tuple(_COOLDOWN_SKIP_STATUSES),
        ).fetchall()

    most_recent: date | None = None
    for row in rows:
        if normalize_company(row["company"]) != norm:
            continue
        row_date_str = str(row["date"] or "").strip()
        try:
            row_date = _dt.date.fromisoformat(row_date_str)
        except ValueError:
            continue
        if most_recent is None or row_date > most_recent:
            most_recent = row_date

    if most_recent is None:
        return False
    return (today - most_recent).days < days


# ── Email response / confirmation ─────────────────────────────────────────────

_TITLE_STOP = frozenset(
    {
        "senior",
        "junior",
        "mid",
        "lead",
        "principal",
        "staff",
        "head",
        "the",
        "and",
        "for",
        "with",
        "of",
        "a",
        "an",
        "in",
        "at",
    }
)


def _title_tokens(title: str) -> set[str]:
    tokens = re.findall(r"[a-z]+", _strip_diacritics(title.lower()))
    return {t for t in tokens if len(t) >= 3 and t not in _TITLE_STOP}


def _title_similarity(a: str, b: str) -> float:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def lookup_by_company_and_title(
    company: str,
    title: str,
    title_min_score: float = 0.5,
) -> list[dict]:
    """Find tracker rows where company normalises to the same key and title
    similarity is at or above *title_min_score*.

    Returns list of dicts with keys: id, company, title, ats, sent, url,
    confirmation, title_score. Sorted by score descending.
    """
    norm_company = normalize_company(company)
    if not norm_company:
        return []

    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, company, title, ats_status, sent, url, confirmation "
            "FROM applications WHERE company != ''"
        ).fetchall()

    results = []
    for row in rows:
        if normalize_company(row["company"]) != norm_company:
            continue
        score = _title_similarity(title, row["title"])
        if score < title_min_score:
            continue
        results.append(
            {
                "id": row["id"],
                "company": row["company"],
                "title": row["title"],
                "ats": row["ats_status"],
                "sent": row["sent"],
                "url": row["url"],
                "confirmation": row["confirmation"],
                "title_score": score,
            }
        )

    results.sort(key=lambda r: r["title_score"], reverse=True)
    return results


def set_confirmation(row_id: str, date_str: str) -> None:
    """Write *date_str* to the Confirmation column for the given row_id.

    No-op if row_id is empty or not found.
    (Replaces the former xlsx row_num-based version.)
    """
    if not row_id or not date_str:
        return
    with get_db(DB_PATH) as conn:
        conn.execute(
            "UPDATE applications SET confirmation=? WHERE id=?",
            (date_str, row_id),
        )


def get_applications_on_date(date_str: str) -> list[dict]:
    """Return all tracker rows where Date == *date_str* (format 'YYYY-MM-DD')."""
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, company, title, ats_status, url FROM applications "
            "WHERE date=? AND company != ''",
            (date_str,),
        ).fetchall()
    return [
        {
            "date": r["date"],
            "company": r["company"],
            "title": r["title"],
            "ats": r["ats_status"],
            "url": r["url"],
        }
        for r in rows
    ]


# ── Google Sheets metadata ────────────────────────────────────────────────────


def set_sheets_row(row_id: str, sheets_row: int) -> None:
    """Store the Google Sheets 1-based row index for a tracker row."""
    with get_db(DB_PATH) as conn:
        conn.execute(
            "UPDATE applications SET sheets_row=? WHERE id=?",
            (sheets_row, row_id),
        )


def get_sheets_row(row_id: str) -> int | None:
    """Return the Google Sheets row index for a row, or None if not yet pushed."""
    with get_db(DB_PATH) as conn:
        row = conn.execute("SELECT sheets_row FROM applications WHERE id=?", (row_id,)).fetchone()
    return int(row["sheets_row"]) if row and row["sheets_row"] is not None else None


def mark_sheets_dirty(row_id: str) -> None:
    """Flag a tracker row as needing a Sheets resync."""
    with get_db(DB_PATH) as conn:
        conn.execute("UPDATE applications SET sheets_dirty=1 WHERE id=?", (row_id,))


def mark_sheets_clean(row_id: str) -> None:
    """Clear the sheets_dirty flag after a successful Sheets write."""
    with get_db(DB_PATH) as conn:
        conn.execute("UPDATE applications SET sheets_dirty=0 WHERE id=?", (row_id,))


def get_dirty_rows_for_sheets() -> list[tuple[str, dict, int | None]]:
    """Return (row_id, row_dict, sheets_row) for all rows that need Sheets sync."""
    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, date, company, title, stack, ats_status, url, folder,
                   sent, reapplication, to_learn, drive_url, confirmation, answer,
                   sheets_row
            FROM applications
            WHERE sheets_dirty=1
            """
        ).fetchall()
    result = []
    for r in rows:
        row_dict = {
            "Date": r["date"],
            "Company": r["company"],
            "Job Title": r["title"],
            "Stack": r["stack"],
            "ATS %": r["ats_status"],
            "URL": r["url"],
            "Folder": r["folder"],
            "Sent": r["sent"],
            "Re-application": r["reapplication"],
            "To Learn": r["to_learn"],
            "ID": r["id"],
            "Drive URL": r["drive_url"],
            "Confirmation": r["confirmation"],
            "Answer": r["answer"],
        }
        result.append((r["id"], row_dict, r["sheets_row"]))
    return result


def get_dirty_sheets_count() -> int:
    """Return the number of rows flagged for Sheets resync."""
    with get_db(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM applications WHERE sheets_dirty=1").fetchone()
    return row["n"] if row else 0
