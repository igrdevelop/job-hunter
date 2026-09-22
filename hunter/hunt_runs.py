"""
hunter/hunt_runs.py — one row per hunt with the funnel the loop already computed.

docs/PIPELINE_VIZ_PLAN.md M1 (``hunt_runs`` table). ``hunter/main.py::
_run_hunt_impl`` computes the whole per-hunt funnel in local variables —
raw listings found, how many the filter cut and why, URL / company+title /
cooldown duplicates, how many were genuinely new, how many the
``MAX_JOBS_PER_RUN`` cap dropped, and whether the survivors were queued as
PENDING rows or applied inline — and sends them to ONE Telegram message.
M0 on prod (2026-09-22) showed the funnel cannot be derived afterwards from
the tables that do exist: ``source_runs`` counts a listing once per sweep
(49,356 over 7 days) while ``postings_seen`` counts a ``url_norm`` once ever
(2,825), a 94% gap. So the numbers are persisted at the moment the loop has
them, once per hunt, $0.

What it stores
--------------
Counts only, plus two small JSON columns: ``sources`` (the names that ran in
this hunt — one per staggered slot in prod) and ``filter_reasons`` (reason →
count, non-zero entries only, the same dict the Telegram report renders).
Never a job, a URL or a title — ``postings_seen`` and ``applications`` own
those. No ``user_id`` by design (the hunt is one process for every user;
``hunter/erasure.py`` discovers tables by that column and correctly skips
this one).

Error contract
--------------
``record_hunt`` MAY raise on a broken DB and deliberately does NOT swallow:
the hunt-loop caller wraps it in ``best_effort("hunt.record")``, which needs
the exception to count consecutive failures and alert at the threshold —
the same contract as ``hunter.postings_seen.record_listings``. The read
side (``recent_hunts`` / ``sum_window``) raises too; its callers are reports.

Storage: a ``hunt_runs`` table in the same tracker.db, created lazily (same
self-contained lazy-ensure pattern as ``hunter.source_health`` /
``hunter.postings_seen`` — NOT part of ``hunter.db.init_db()``). Pruned to
the newest ``HUNT_RUNS_KEEP`` rows inside every write, like
``source_health._prune``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from hunter.config import HUNT_RUNS_KEEP, TRACKER_DB_PATH
from hunter.db import get_db

log = logging.getLogger(__name__)

__all__ = [
    "COUNT_COLUMNS",
    "TRIGGERS",
    "record_hunt",
    "recent_hunts",
    "sum_window",
]

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.source_health.DB_PATH / hunter.postings_seen.DB_PATH).
DB_PATH = TRACKER_DB_PATH

# What started the hunt. The loop can tell a manual /hunt from a scheduled
# slot; "force" is reserved for a future caller that runs the loop with dedup
# disabled — today /force applies to one URL and never runs the hunt.
TRIGGERS: tuple[str, ...] = ("scheduled", "manual", "force")

# "trigger" and "new" are SQLite keywords — quoted everywhere they appear.
_DDL = """
CREATE TABLE IF NOT EXISTS hunt_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    "trigger"       TEXT    NOT NULL,
    sources         TEXT    NOT NULL,
    found           INTEGER NOT NULL DEFAULT 0,
    filtered_out    INTEGER NOT NULL DEFAULT 0,
    filter_reasons  TEXT    NOT NULL DEFAULT '{}',
    dup_url         INTEGER NOT NULL DEFAULT 0,
    dup_ct          INTEGER NOT NULL DEFAULT 0,
    dup_cooldown    INTEGER NOT NULL DEFAULT 0,
    "new"           INTEGER NOT NULL DEFAULT 0,
    capped          INTEGER NOT NULL DEFAULT 0,
    queued          INTEGER NOT NULL DEFAULT 0,
    applied_inline  INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_hunt_runs_ts ON hunt_runs(ts);
"""

# Every integer column, in DDL order — the set sum_window() totals.
COUNT_COLUMNS: tuple[str, ...] = (
    "found",
    "filtered_out",
    "dup_url",
    "dup_ct",
    "dup_cooldown",
    "new",
    "capped",
    "queued",
    "applied_inline",
    "duration_ms",
)

_INSERT_SQL = (
    'INSERT INTO hunt_runs (ts, "trigger", sources, found, filtered_out, filter_reasons, '
    'dup_url, dup_ct, dup_cooldown, "new", capped, queued, applied_inline, duration_ms) '
    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

_SELECT_COLUMNS = (
    'id, ts, "trigger", sources, found, filtered_out, filter_reasons, dup_url, dup_ct, '
    'dup_cooldown, "new", capped, queued, applied_inline, duration_ms'
)


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _nonneg(value: Any) -> int:
    """Clamp a counter to a non-negative int; None counts as 0."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _reasons_json(reasons: Mapping[str, Any] | None) -> str:
    """reason -> count, non-zero entries only, keys sorted for a stable text."""
    cleaned = {str(k): _nonneg(v) for k, v in (reasons or {}).items()}
    return json.dumps({k: v for k, v in sorted(cleaned.items()) if v > 0}, ensure_ascii=False)


# ── Write ─────────────────────────────────────────────────────────────────────


def record_hunt(
    *,
    trigger: str,
    sources: Iterable[str],
    found: int = 0,
    filtered_out: int = 0,
    filter_reasons: Mapping[str, Any] | None = None,
    dup_url: int = 0,
    dup_ct: int = 0,
    dup_cooldown: int = 0,
    new: int = 0,
    capped: int = 0,
    queued: int = 0,
    applied_inline: int = 0,
    duration_ms: int = 0,
    ts: str | None = None,
) -> int:
    """Insert ONE ``hunt_runs`` row and return its id.

    ``ts`` defaults to now (UTC, ISO seconds); the loop passes the hunt's own
    start time so the row is stamped when the sweep began, not when the ACT
    step finished deciding. An unknown ``trigger`` is stored as given (a
    report column, never a gate) but logged. Prunes to ``HUNT_RUNS_KEEP``
    rows in the same transaction.

    Raises on a broken DB — see the module docstring.
    """
    if trigger not in TRIGGERS:
        log.warning("hunt_runs: unknown trigger %r (stored as-is)", trigger)
    row = (
        ts or _utc_now_iso(),
        str(trigger),
        json.dumps([str(s) for s in sources], ensure_ascii=False),
        _nonneg(found),
        _nonneg(filtered_out),
        _reasons_json(filter_reasons),
        _nonneg(dup_url),
        _nonneg(dup_ct),
        _nonneg(dup_cooldown),
        _nonneg(new),
        _nonneg(capped),
        _nonneg(queued),
        _nonneg(applied_inline),
        _nonneg(duration_ms),
    )
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        cur = conn.execute(_INSERT_SQL, row)
        row_id = int(cur.lastrowid or 0)
        _prune(conn)
    log.info(
        "hunt_runs: recorded id=%d trigger=%s found=%d new=%d queued=%d inline=%d",
        row_id,
        trigger,
        row[3],
        row[9],
        row[11],
        row[12],
    )
    return row_id


def _prune(conn: sqlite3.Connection) -> None:
    """Keep only the newest HUNT_RUNS_KEEP rows (ring buffer, like source_runs)."""
    conn.execute(
        """
        DELETE FROM hunt_runs
        WHERE id NOT IN (SELECT id FROM hunt_runs ORDER BY id DESC LIMIT ?)
        """,
        (max(1, int(HUNT_RUNS_KEEP)),),
    )


# ── Read ──────────────────────────────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["sources"] = json.loads(d.get("sources") or "[]")
    except (TypeError, ValueError):
        d["sources"] = []
    try:
        d["filter_reasons"] = json.loads(d.get("filter_reasons") or "{}")
    except (TypeError, ValueError):
        d["filter_reasons"] = {}
    return d


def recent_hunts(limit: int = 20) -> list[dict[str, Any]]:
    """The newest ``limit`` rows, newest first, JSON columns decoded."""
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM hunt_runs ORDER BY id DESC LIMIT ?",  # noqa: S608 — constant column list
            (max(0, int(limit)),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def sum_window(since_iso: str) -> dict[str, Any]:
    """Totals over every row with ``ts >= since_iso``.

    Returns ``{"hunts": n, <every COUNT_COLUMNS name>: total, "filter_reasons":
    {reason: total}}``. ``since_iso`` is compared as text, so pass the same
    UTC ISO-seconds shape ``ts`` is written in. An empty window is all zeros.
    """
    totals: dict[str, Any] = {"hunts": 0, **dict.fromkeys(COUNT_COLUMNS, 0)}
    sums = ", ".join(f'COALESCE(SUM("{c}"), 0) AS "{c}"' for c in COUNT_COLUMNS)
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        agg = conn.execute(
            f"SELECT COUNT(*) AS hunts, {sums} FROM hunt_runs WHERE ts >= ?",  # noqa: S608 — constant column list
            (since_iso,),
        ).fetchone()
        reason_rows = conn.execute(
            "SELECT filter_reasons FROM hunt_runs WHERE ts >= ?", (since_iso,)
        ).fetchall()
    if agg:
        totals["hunts"] = int(agg["hunts"] or 0)
        for c in COUNT_COLUMNS:
            totals[c] = int(agg[c] or 0)
    merged: dict[str, int] = {}
    for r in reason_rows:
        try:
            reasons = json.loads(r["filter_reasons"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(reasons, dict):
            continue
        for k, v in reasons.items():
            merged[str(k)] = merged.get(str(k), 0) + _nonneg(v)
    totals["filter_reasons"] = dict(sorted(merged.items(), key=lambda kv: (-kv[1], kv[0])))
    return totals
