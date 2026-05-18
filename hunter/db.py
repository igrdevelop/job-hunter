"""
SQLite persistence layer for the job tracker.

Primary data store for all tracker operations. tracker.xlsx is maintained
as a secondary human-readable view via dual-write in tracker.py write functions,
and can be fully regenerated via /export or tracker.export_to_excel().

Circular-import note: db.py imports from tracker.py only inside functions (lazy),
so there is no circular dependency at module load time.
"""
import sqlite3
import threading
import logging
from pathlib import Path

from hunter.config import PROJECT_DIR

DB_PATH: Path = PROJECT_DIR / "hunter.db"
log = logging.getLogger(__name__)

_lock = threading.RLock()   # reentrant: read functions hold _lock while calling _connection()
_conn: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id       TEXT PRIMARY KEY,
    date     TEXT NOT NULL DEFAULT '',
    company  TEXT NOT NULL DEFAULT '',
    title    TEXT NOT NULL DEFAULT '',
    stack    TEXT NOT NULL DEFAULT '',
    ats_pct  TEXT NOT NULL DEFAULT '',
    url      TEXT NOT NULL DEFAULT '',
    folder   TEXT NOT NULL DEFAULT '',
    sent     TEXT NOT NULL DEFAULT '',
    reapply  TEXT NOT NULL DEFAULT '',
    to_learn TEXT NOT NULL DEFAULT '',
    norm_url TEXT NOT NULL DEFAULT '',
    ct_key   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_norm_url ON jobs(norm_url);
CREATE INDEX IF NOT EXISTS idx_ct_key   ON jobs(ct_key);
CREATE INDEX IF NOT EXISTS idx_date     ON jobs(date);
CREATE INDEX IF NOT EXISTS idx_ats      ON jobs(ats_pct);
"""


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        with _lock:
            if _conn is None:
                DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA journal_mode=WAL")
                c.execute("PRAGMA synchronous=NORMAL")
                c.executescript(_SCHEMA)
                c.commit()
                _conn = c
    return _conn


def init_db() -> None:
    """Ensure schema exists. Safe to call multiple times."""
    _connection()
    log.info("db: initialized at %s", DB_PATH)


def row_count() -> int:
    with _lock:
        return _connection().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def is_empty() -> bool:
    return row_count() == 0


# ── Write ─────────────────────────────────────────────────────────────────────

def insert_job(row: dict, *, replace: bool = False) -> None:
    """Insert a row keyed by TRACKER_HEADERS names. Silently ignores missing ID.

    Uses lazy import of normalize_url/dedup_key to avoid circular dependency with tracker.py.
    """
    row_id = (row.get("ID") or "").strip()
    if not row_id:
        return

    from hunter.tracker import normalize_url, dedup_key  # lazy — breaks circular dep

    url = str(row.get("URL") or "")
    company = str(row.get("Company") or "")
    title = str(row.get("Job Title") or "")
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"

    with _lock:
        conn = _connection()
        conn.execute(
            f"{verb} INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                str(row.get("Date") or ""),
                company,
                title,
                str(row.get("Stack") or ""),
                str(row.get("ATS %") or ""),
                url,
                str(row.get("Folder") or ""),
                str(row.get("Sent") or ""),
                str(row.get("Re-application") or ""),
                str(row.get("To Learn") or ""),
                normalize_url(url) if url else "",
                dedup_key(company, title) if company and title else "",
            ),
        )
        conn.commit()


def update_sent(row_id: str, sent_value: str) -> int:
    """Update Sent column for a row. Returns 1 if updated, 0 if not found."""
    with _lock:
        conn = _connection()
        cur = conn.execute("UPDATE jobs SET sent=? WHERE id=?", (sent_value, row_id))
        conn.commit()
    return cur.rowcount


def update_user_fields(row_id: str, sent: str, reapply: str, to_learn: str) -> int:
    """Update Sent + Re-application + To Learn (Sheets pull sync). Returns 1 if updated."""
    with _lock:
        conn = _connection()
        cur = conn.execute(
            "UPDATE jobs SET sent=?,reapply=?,to_learn=? WHERE id=?",
            (sent, reapply, to_learn, row_id),
        )
        conn.commit()
    return cur.rowcount


def delete_where(norm_url: str, ats_pct: str) -> int:
    """Delete rows matching norm_url AND ats_pct. Returns count deleted."""
    with _lock:
        conn = _connection()
        cur = conn.execute(
            "DELETE FROM jobs WHERE norm_url=? AND ats_pct=?", (norm_url, ats_pct)
        )
        conn.commit()
    return cur.rowcount


# ── Read ──────────────────────────────────────────────────────────────────────

def is_known(norm_url: str, ct_key: str = "") -> bool:
    """Return True if norm_url or ct_key already exists in jobs table."""
    with _lock:
        conn = _connection()
        if conn.execute("SELECT 1 FROM jobs WHERE norm_url=?", (norm_url,)).fetchone():
            return True
        if ct_key and conn.execute("SELECT 1 FROM jobs WHERE ct_key=?", (ct_key,)).fetchone():
            return True
    return False


def get_known_norm_urls() -> set[str]:
    with _lock:
        return {r[0] for r in _connection().execute(
            "SELECT norm_url FROM jobs WHERE norm_url != ''"
        ).fetchall()}


def get_known_ct_keys() -> set[str]:
    with _lock:
        return {r[0] for r in _connection().execute(
            "SELECT ct_key FROM jobs WHERE ct_key != ''"
        ).fetchall()}


def get_all_rows() -> list[dict]:
    """All rows as TRACKER_HEADERS-keyed dicts, ordered by date then insertion order."""
    with _lock:
        rows = _connection().execute(
            "SELECT id,date,company,title,stack,ats_pct,url,folder,sent,reapply,to_learn"
            " FROM jobs ORDER BY date,rowid"
        ).fetchall()
    return [_to_headers(r) for r in rows]


def get_by_norm_url(norm_url: str) -> list[dict]:
    with _lock:
        rows = _connection().execute(
            "SELECT id,date,company,title,stack,ats_pct,url,folder,sent,reapply,to_learn"
            " FROM jobs WHERE norm_url=?",
            (norm_url,),
        ).fetchall()
    return [_to_detail(r) for r in rows]


def get_by_ats(ats_pct: str) -> list[dict]:
    """Return rows for a given ATS status (FAIL, SKIP, MANUAL, EXPIRED…)."""
    with _lock:
        rows = _connection().execute(
            "SELECT id,date,company,title,stack,ats_pct,url,folder,sent,reapply,to_learn,rowid"
            " FROM jobs WHERE ats_pct=? ORDER BY date,rowid",
            (ats_pct,),
        ).fetchall()
    return [
        {
            "id": r[0], "date": r[1], "company": r[2], "title": r[3],
            "stack": r[4], "ats": r[5], "url": r[6], "folder": r[7],
            "sent": r[8], "reapply": r[9], "to_learn": r[10],
            "row": r[11],
        }
        for r in rows
    ]


def get_unsent_rows() -> list[dict]:
    """Rows with empty Sent and ats_pct != 'SKIP', ordered by date."""
    with _lock:
        rows = _connection().execute(
            "SELECT id,date,company,title,stack,ats_pct,url,folder,sent,reapply,to_learn,rowid"
            " FROM jobs"
            " WHERE (sent='' OR sent IS NULL)"
            "   AND ats_pct != 'SKIP'"
            "   AND id != ''"
            " ORDER BY date,rowid"
        ).fetchall()
    return [
        {
            "id": r[0], "date": r[1], "company": r[2], "title": r[3],
            "stack": r[4], "ats": r[5], "url": r[6], "folder": r[7],
            "sent": r[8], "reapp": r[9], "to_learn": r[10],
            "row_num": r[11],
        }
        for r in rows
    ]


def get_sent_company_names() -> list[str]:
    """Company names for rows that have a non-empty Sent value."""
    with _lock:
        rows = _connection().execute(
            "SELECT company FROM jobs WHERE sent != '' AND company != ''"
        ).fetchall()
    return [r[0] for r in rows]


# ── Migration ─────────────────────────────────────────────────────────────────

def migrate_from_excel(path: Path) -> int:
    """Import all rows from tracker.xlsx into SQLite via INSERT OR IGNORE (idempotent).

    Returns total rows processed (duplicates silently skipped).
    Uses lazy imports to avoid circular dependency at module load time.
    """
    if not path.exists():
        return 0

    import openpyxl
    from hunter.tracker import TRACKER_HEADERS, ID_COL_INDEX  # lazy

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    ws = wb.active
    processed = 0
    for values in ws.iter_rows(min_row=2, values_only=True):
        if not values or not any(values):
            continue
        padded = list(values) + [""] * max(0, len(TRACKER_HEADERS) - len(values))
        row_id = str(padded[ID_COL_INDEX - 1] or "").strip()
        if not row_id:
            continue
        row_dict = {
            col: str(padded[i] or "").strip()
            for i, col in enumerate(TRACKER_HEADERS)
        }
        insert_job(row_dict, replace=False)
        processed += 1
    wb.close()
    log.info("db.migrate_from_excel: %d rows from %s", processed, path)
    return processed


# ── Row format helpers ────────────────────────────────────────────────────────

def _to_headers(r) -> dict:
    """Map a SQLite row to TRACKER_HEADERS-keyed dict (for gsheets, export)."""
    return {
        "ID": r[0], "Date": r[1], "Company": r[2], "Job Title": r[3],
        "Stack": r[4], "ATS %": r[5], "URL": r[6], "Folder": r[7],
        "Sent": r[8], "Re-application": r[9], "To Learn": r[10],
    }


def _to_detail(r) -> dict:
    """Map a SQLite row to lowercase-key detail dict (for lookup functions)."""
    return {
        "id": r[0], "date": r[1], "company": r[2], "title": r[3],
        "stack": r[4], "ats": r[5], "url": r[6], "folder": r[7],
        "sent": r[8], "reapply": r[9], "to_learn": r[10],
    }
