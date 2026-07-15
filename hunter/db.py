"""
hunter/db.py — SQLite persistence layer for the job tracker.

Replaces the openpyxl-based tracker.xlsx store with an ACID-safe,
concurrent-read-friendly SQLite database (WAL mode).

Public interface
----------------
  init_db(path)              Create tables + auto-migrate from tracker.xlsx
  get_db(path)               Context manager → sqlite3.Connection
  migrate_from_excel(xlsx, db)  One-time import of existing tracker.xlsx rows
  ensure_subsystem_health_table(conn)  Idempotent CREATE for hunter.best_effort

Column mapping (mirrors tracker.xlsx schema exactly):
  id           — 8-char hex UUID  (PRIMARY KEY)
  date         — application date YYYY-MM-DD
  company      — company name
  title        — job title
  stack        — tech stack (from LLM)
  ats_status   — score "85%" or SKIP/FAIL/MANUAL/EXPIRED/—
  url          — canonical job URL (human-readable, may have query params)
  url_norm     — normalize_url(url) — used for O(1) dedup via index
  folder       — path to Applications/ subfolder
  sent         — date sent, EXPIRED, or blank
  reapplication — '+' flag or blank
  to_learn     — skills gap
  drive_url    — Google Drive folder URL after upload
  confirmation — date ATS acknowledged application
  answer       — company reply (rejection / interview / offer)
  sheets_row   — Google Sheets 1-based row number (NULL if not pushed yet)
  sheets_dirty — 1 = needs Sheets push / resync

Thread safety: WAL mode + check_same_thread=False → concurrent readers OK,
serialised writers via context-manager commit/rollback.
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from hunter.config import TRACKER_DB_PATH, TRACKER_PATH

log = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS applications (
    id            TEXT    PRIMARY KEY,
    date          TEXT    NOT NULL DEFAULT '',
    company       TEXT    NOT NULL DEFAULT '',
    title         TEXT    NOT NULL DEFAULT '',
    stack         TEXT    NOT NULL DEFAULT '',
    ats_status    TEXT    NOT NULL DEFAULT '',
    url           TEXT    NOT NULL DEFAULT '',
    url_norm      TEXT    NOT NULL DEFAULT '',
    folder        TEXT    NOT NULL DEFAULT '',
    sent          TEXT    NOT NULL DEFAULT '',
    reapplication TEXT    NOT NULL DEFAULT '',
    to_learn      TEXT    NOT NULL DEFAULT '',
    drive_url     TEXT    NOT NULL DEFAULT '',
    confirmation  TEXT    NOT NULL DEFAULT '',
    answer        TEXT    NOT NULL DEFAULT '',
    sheets_row    INTEGER,
    sheets_dirty  INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL
);

CREATE INDEX IF NOT EXISTS idx_url_norm
    ON applications(url_norm)
    WHERE url_norm != '';

CREATE INDEX IF NOT EXISTS idx_ats
    ON applications(ats_status);

CREATE INDEX IF NOT EXISTS idx_company
    ON applications(company);
"""

# Backs hunter.best_effort — consecutive-failure counters for best-effort
# subsystems (Sheets mirror, Drive upload, delivery, outreach, dual-shadow,
# cost/verdict writers). One row per subsystem name; `consecutive_failures`
# resets to 0 on the next success. Separate table (not columns on
# `applications`) because this tracks *subsystem* health, not per-row state.
_SUBSYSTEM_HEALTH_DDL = """
CREATE TABLE IF NOT EXISTS subsystem_health (
    subsystem             TEXT    PRIMARY KEY,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_error            TEXT    NOT NULL DEFAULT '',
    last_alert_at          TEXT
);
"""


def ensure_subsystem_health_table(conn: sqlite3.Connection) -> None:
    """Idempotent CREATE for the `subsystem_health` table.

    Called from init_db() at bot startup, and defensively by
    hunter.best_effort itself on every read/write — the apply pipeline runs
    as a subprocess against an already-initialised tracker.db in production,
    but tests and standalone scripts may open a bare temp DB that never went
    through init_db() (mirrors hunter.source_health's own lazy-ensure
    pattern for source_runs).
    """
    conn.executescript(_SUBSYSTEM_HEALTH_DDL)


# ── Connection factory ────────────────────────────────────────────────────────


@contextmanager
def get_db(path: Path = TRACKER_DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Open a connection to the SQLite tracker DB.

    Usage::

        with get_db() as conn:
            conn.execute("INSERT INTO applications ...")

    The connection uses WAL journal mode and returns rows as ``sqlite3.Row``
    objects (accessible by column name).  Commits on clean exit; rolls back on
    exception.
    """
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL, faster than FULL
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Schema init ───────────────────────────────────────────────────────────────


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add any missing columns (incremental schema migrations for existing DBs).

    Called during init_db() after CREATE TABLE IF NOT EXISTS so that
    existing databases gain new columns without needing a full rebuild.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
    migrations = [
        ("sheets_row", "INTEGER"),
        ("sheets_dirty", "INTEGER NOT NULL DEFAULT 0"),
        ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
        # cost_usd is the per-vacancy total USD spent on LLM calls (rounded
        # to 4 decimals). NULL means "not measured" — either a pre-cost-
        # tracking row, or a CLI-mode run (Pro subscription, no per-token
        # visibility). The Sheets mirror renders NULL as an empty cell.
        ("cost_usd", "REAL"),
        # ats_verdict is the independent PDF-verdict score (0-100): one cheap
        # judge-model call scoring the text extracted from the rendered EN CV
        # PDF. NULL means "no verdict" (feature disabled, no judge key, PDF
        # unreadable, or a pre-verdict row). Mirrored to Sheet column N by
        # hunter.verdict_writer (parallel to cost_usd -> column M).
        ("ats_verdict", "REAL"),
    ]
    for col, definition in migrations:
        if col not in existing:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {definition}")
            log.info("db: added missing column '%s' to applications", col)


def _dedup_url_norm(conn: sqlite3.Connection) -> int:
    """Remove duplicate url_norm rows keeping the best entry per URL.

    Priority for 'best': successful ATS score > MANUAL > FAIL > SKIP > empty.
    Returns number of rows deleted.
    """
    _STATUS_RANK = {
        "fail": 0,
        "skip": 0,
        "": 0,
        "?": 0,
        "manual": 1,
        "expired": 1,
    }

    def _rank(ats: str) -> int:
        key = (ats or "").lower().strip()
        # Real ATS scores (e.g. "97%") rank highest
        if key.endswith("%"):
            return 10
        return _STATUS_RANK.get(key, 5)

    dups = conn.execute(
        """
        SELECT url_norm, COUNT(*) as cnt
        FROM applications
        WHERE url_norm != ''
        GROUP BY url_norm
        HAVING cnt > 1
        """
    ).fetchall()

    deleted = 0
    for dup in dups:
        url_norm = dup["url_norm"]
        rows = conn.execute(
            "SELECT id, ats_status, date FROM applications WHERE url_norm=?",
            (url_norm,),
        ).fetchall()
        # Sort: best status first, then latest date
        rows_sorted = sorted(
            rows,
            key=lambda r: (_rank(r["ats_status"] or ""), r["date"] or ""),
            reverse=True,
        )
        keep_id = rows_sorted[0]["id"]
        ids_to_delete = [r["id"] for r in rows_sorted[1:]]
        if ids_to_delete:
            conn.execute(
                # f-string only expands the placeholder count; values are bound.
                f"DELETE FROM applications WHERE id IN ({','.join('?' * len(ids_to_delete))})",  # noqa: S608
                ids_to_delete,
            )
            deleted += len(ids_to_delete)
            log.info(
                "db.dedup: removed %d duplicate(s) for url_norm=%s (kept %s)",
                len(ids_to_delete),
                url_norm[:60],
                keep_id,
            )

    return deleted


def init_db(
    path: Path = TRACKER_DB_PATH,
    *,
    xlsx_path: Path | None = None,
) -> None:
    """Create tables if they do not exist and apply incremental migrations.

    Args:
        path: Path to the SQLite database file to initialise.
        xlsx_path: Path to an existing tracker.xlsx to migrate from.
            When *None* (default), uses the global ``TRACKER_PATH`` from
            ``hunter.config``.  Pass an explicit path (or a non-existent path)
            in tests to suppress auto-migration.

    If ``path`` does not exist yet **and** the resolved xlsx file is present,
    all existing Excel rows are automatically imported so no data is lost.
    """
    _xlsx = xlsx_path if xlsx_path is not None else TRACKER_PATH
    need_migration = not path.exists() and _xlsx.exists()

    with get_db(path) as conn:
        conn.executescript(_DDL)
        _ensure_columns(conn)
        ensure_subsystem_health_table(conn)
        # Deduplicate existing rows before applying any unique constraints
        n_dedup = _dedup_url_norm(conn)
        if n_dedup:
            log.info("db.init_db: removed %d duplicate url_norm rows", n_dedup)

    if need_migration:
        n = migrate_from_excel(_xlsx, path)
        log.info("db.init_db: migrated %d rows from %s → %s", n, _xlsx.name, path)
    else:
        log.debug("db.init_db: ready at %s", path)


# ── Migration ─────────────────────────────────────────────────────────────────


def migrate_from_excel(
    xlsx_path: Path = TRACKER_PATH,
    db_path: Path = TRACKER_DB_PATH,
) -> int:
    """Import all rows from an existing tracker.xlsx into the SQLite DB.

    Skips rows that already exist (by ID) to make the function idempotent.
    Returns the number of new rows inserted.
    """
    try:
        import openpyxl  # only needed for migration
    except ImportError:
        log.error("db.migrate_from_excel: openpyxl not installed")
        return 0

    # Import here to avoid circular import (tracker imports from db)
    from hunter.tracker import (
        normalize_url,
        URL_COL_INDEX,
        COMPANY_COL_INDEX,
        TITLE_COL_INDEX,
        ATS_COL_INDEX,
        SENT_COL_INDEX,
        ID_COL_INDEX,
        COL_DRIVE_URL,
        COL_CONFIRMATION,
        COL_ANSWER,
    )

    if not xlsx_path.exists():
        log.warning("db.migrate_from_excel: %s not found, nothing to migrate", xlsx_path)
        return 0

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active

    inserted = 0
    rows_to_insert = []

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not any(row):
            continue
        # Pad row to max column we care about
        padded = list(row) + [""] * max(0, COL_ANSWER - len(row))

        def cell(idx: int, padded: list = padded) -> str:  # 1-based column index
            v = padded[idx - 1]
            return str(v).strip() if v is not None else ""

        row_id = cell(ID_COL_INDEX)
        if not row_id:
            continue  # rows without ID cannot be synced — skip

        raw_url = cell(URL_COL_INDEX)
        rows_to_insert.append(
            {
                "id": row_id,
                "date": cell(1),
                "company": cell(COMPANY_COL_INDEX),
                "title": cell(TITLE_COL_INDEX),
                "stack": cell(4),
                "ats_status": cell(ATS_COL_INDEX),
                "url": raw_url,
                "url_norm": normalize_url(raw_url) if raw_url else "",
                "folder": cell(7),
                "sent": cell(SENT_COL_INDEX),
                "reapplication": cell(9),
                "to_learn": cell(10),
                "drive_url": cell(COL_DRIVE_URL),
                "confirmation": cell(COL_CONFIRMATION),
                "answer": cell(COL_ANSWER) if len(padded) >= COL_ANSWER else "",
                "sheets_row": None,
                "sheets_dirty": 0,
            }
        )

    wb.close()

    with get_db(db_path) as conn:
        for r in rows_to_insert:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO applications
                    (id, date, company, title, stack, ats_status, url, url_norm,
                     folder, sent, reapplication, to_learn, drive_url,
                     confirmation, answer, sheets_row, sheets_dirty)
                    VALUES
                    (:id, :date, :company, :title, :stack, :ats_status, :url, :url_norm,
                     :folder, :sent, :reapplication, :to_learn, :drive_url,
                     :confirmation, :answer, :sheets_row, :sheets_dirty)
                    """,
                    r,
                )
                if conn.execute("SELECT changes()").fetchone()[0]:
                    inserted += 1
            except Exception as e:
                log.warning("db.migrate_from_excel: skipping row %s: %s", r.get("id"), e)

    log.info("db.migrate_from_excel: inserted %d / %d rows", inserted, len(rows_to_insert))
    return inserted


# ── Convenience helpers ───────────────────────────────────────────────────────


def row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain Python dict."""
    return dict(row)
