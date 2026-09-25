"""
hunter/hunt_live.py — the step a hunt is on RIGHT NOW, persisted.

Pipeline control plan, PR 1 (c). ``hunter/main.py``'s hunt state lives in
memory (``_hunt_lock``) and its numbers reach the DB only at the end
(``hunt_runs``), so the site's /pipeline page could not show a loader on the
step that is working. This module keeps ONE row per hunt that the loop
updates at each step boundary and after every source in the fetch loop:

    waiting -> fetch -> filter -> dedup -> act -> done | error

``waiting`` is written by ``run_hunt`` BEFORE it acquires ``_hunt_lock`` (a
hunt queued behind another one is visible as such). ``run_retry_failed``
writes a row too (``trigger="retry"``, no sources): waiting -> act -> done.

Columns are the shared contract with job-hunter-api (it reads this table
for ``GET /pipeline/snapshot`` — ``hunt.live``); do not rename. Counts and
source names only — never a job, URL or title. No ``user_id`` by design (the
hunt is one process for every user, like ``hunt_runs``). All timestamps UTC
``%Y-%m-%dT%H:%M:%S+00:00``.

Error contract: every function RAISES on a broken DB; every caller wraps it
in ``best_effort("hunt.live")`` — the page's loader is telemetry and must
never cost a hunt, but repeated failures still alert.

Storage: a ``hunt_live`` table in the same tracker.db, created lazily (the
``hunter.source_health`` / ``hunter.hunt_runs`` pattern — NOT part of
``hunter.db.init_db()``). Pruned to the newest ``HUNT_LIVE_KEEP`` rows each
time a row is started.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from hunter.config import HUNT_LIVE_KEEP, TRACKER_DB_PATH
from hunter.db import get_db

log = logging.getLogger(__name__)

__all__ = [
    "STEPS",
    "TRIGGERS",
    "fail_unfinished",
    "finish",
    "latest",
    "set_step",
    "source_done",
    "source_started",
    "start",
]

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.hunt_runs.DB_PATH).
DB_PATH = TRACKER_DB_PATH

STEPS: tuple[str, ...] = ("waiting", "fetch", "filter", "dedup", "act", "done", "error")
TERMINAL_STEPS: tuple[str, ...] = ("done", "error")
TRIGGERS: tuple[str, ...] = ("scheduled", "manual", "web", "retry")

# "trigger" is an SQLite keyword — quoted everywhere it appears.
_DDL = """
CREATE TABLE IF NOT EXISTS hunt_live (
    hunt_id         TEXT    PRIMARY KEY,
    "trigger"       TEXT    NOT NULL,
    sources         TEXT    NOT NULL DEFAULT '[]',
    started_at      TEXT    NOT NULL,
    step            TEXT    NOT NULL,
    step_started_at TEXT    NOT NULL,
    current_source  TEXT    NOT NULL DEFAULT '',
    sources_done    INTEGER NOT NULL DEFAULT 0,
    sources_total   INTEGER NOT NULL DEFAULT 0,
    found_so_far    INTEGER NOT NULL DEFAULT 0,
    command_id      TEXT    NOT NULL DEFAULT '',
    finished_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_hunt_live_started ON hunt_live(started_at);
"""


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ── Write ─────────────────────────────────────────────────────────────────────


def start(
    *,
    trigger: str,
    sources: Iterable[str],
    command_id: str = "",
    step: str = "waiting",
) -> str:
    """Insert a new row (step ``waiting`` by default) and return its hunt_id.

    An unknown ``trigger``/``step`` is stored as given but logged — a report
    column, never a gate. Prunes to ``HUNT_LIVE_KEEP`` rows.
    """
    if trigger not in TRIGGERS:
        log.warning("hunt_live: unknown trigger %r (stored as-is)", trigger)
    if step not in STEPS:
        log.warning("hunt_live: unknown step %r (stored as-is)", step)
    names = [str(s) for s in sources]
    hunt_id = uuid.uuid4().hex
    now = _now_iso()
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        conn.execute(
            'INSERT INTO hunt_live (hunt_id, "trigger", sources, started_at, step, '
            "step_started_at, sources_total, command_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                hunt_id,
                str(trigger),
                json.dumps(names, ensure_ascii=False),
                now,
                step,
                now,
                len(names),
                str(command_id or ""),
            ),
        )
        conn.execute(
            """
            DELETE FROM hunt_live
            WHERE rowid NOT IN (SELECT rowid FROM hunt_live ORDER BY rowid DESC LIMIT ?)
            """,
            (max(1, int(HUNT_LIVE_KEEP)),),
        )
    return hunt_id


def set_step(hunt_id: str, step: str) -> None:
    """Move an unfinished row to ``step`` and restart its step clock.
    ``current_source`` is cleared when leaving ``fetch``. A finished row is
    never touched (a late write after ``finish`` must not resurrect it)."""
    if step not in STEPS:
        log.warning("hunt_live: unknown step %r (stored as-is)", step)
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE hunt_live SET step=?, step_started_at=?, "
            "current_source=CASE WHEN ?='fetch' THEN current_source ELSE '' END "
            "WHERE hunt_id=? AND finished_at IS NULL",
            (step, _now_iso(), step, hunt_id),
        )


def source_started(hunt_id: str, source: str) -> None:
    """The fetch loop is about to call ``source.search()``."""
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE hunt_live SET current_source=? WHERE hunt_id=? AND finished_at IS NULL",
            (str(source), hunt_id),
        )


def source_done(hunt_id: str, *, sources_done: int, found_so_far: int) -> None:
    """One more source finished (ok or error); running totals so far."""
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        conn.execute(
            "UPDATE hunt_live SET sources_done=?, found_so_far=? "
            "WHERE hunt_id=? AND finished_at IS NULL",
            (max(0, int(sources_done)), max(0, int(found_so_far)), hunt_id),
        )


def finish(hunt_id: str, *, ok: bool = True) -> bool:
    """Stamp the final step (``done`` / ``error``) and ``finished_at``.

    Idempotent: only an unfinished row changes, so the first terminal write
    wins (``_run_hunt_impl`` stamps ``error`` itself on an early bail-out,
    and ``run_hunt``'s outer ``finally`` then leaves it alone). Returns True
    when this call finished the row.
    """
    now = _now_iso()
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE hunt_live SET step=?, step_started_at=?, current_source='', finished_at=? "
            "WHERE hunt_id=? AND finished_at IS NULL",
            ("done" if ok else "error", now, now, hunt_id),
        )
        return cur.rowcount > 0


def fail_unfinished() -> int:
    """Every unfinished row -> ``error``. Called once at bot startup: a row
    still open in a process that has just started belongs to a hunt the
    previous process never finished. Returns the number of rows stamped."""
    now = _now_iso()
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE hunt_live SET step='error', step_started_at=?, current_source='', "
            "finished_at=? WHERE finished_at IS NULL",
            (now, now),
        )
        return cur.rowcount


# ── Read ──────────────────────────────────────────────────────────────────────


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        parsed = json.loads(d.get("sources") or "[]")
    except (TypeError, ValueError):
        parsed = []
    d["sources"] = parsed if isinstance(parsed, list) else []
    return d


def latest(limit: int = 20) -> list[dict[str, Any]]:
    """The newest ``limit`` rows, newest first, ``sources`` decoded."""
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        rows = conn.execute(
            'SELECT hunt_id, "trigger", sources, started_at, step, step_started_at, '
            "current_source, sources_done, sources_total, found_so_far, command_id, "
            "finished_at FROM hunt_live ORDER BY rowid DESC LIMIT ?",
            (max(0, int(limit)),),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]
