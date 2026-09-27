"""
hunter/hunt_jobs.py — which vacancies one hunt passed on, and what it did with each.

docs/HUNT_DRILLDOWN_PLAN.md M1. ``hunt_runs`` keeps a hunt's funnel as counts;
this table keeps the vacancies behind those counts — one row per listing that
PASSED the filter, with its ``fate`` in that hunt:

  dup_url / dup_ct / dup_cooldown   the dedup step dropped it (and why)
  new                               new, but nothing acted on it (outage pause,
                                    apply not ready — it returns next hunt)
  card                              a Telegram Apply/Skip card (manual-only
                                    source, or AUTO_APPLY off)
  capped                            cut by MAX_JOBS_PER_RUN
  queued                            a PENDING row was written
  applied_inline                    handed to the inline apply batch

Filtered-out listings are NOT stored per row (owner decision 2026-09-27: they
are ~10x the volume, and ``hunt_runs.filter_reasons`` already counts them).
The site's /pipeline page opens one hunt by joining these rows to the
vacancy's CURRENT tracker / generation_runs state by ``url_norm`` at read time
— nothing here is ever updated after the hunt writes it.

Invariant (pinned by tests): per hunt, the number of ``dup_*`` rows equals the
matching ``hunt_runs`` counter, and every other row counts toward
``hunt_runs.new``.

Listing metadata only (title, company, source, url) — public employer
advertisements, like ``postings_seen``. No ``user_id`` by design, so
``hunter/erasure.py`` (which discovers tables by that column) skips it.

Error contract: ``record_hunt_jobs`` RAISES on a broken DB; the hunt-loop
caller wraps it in ``best_effort("hunt.jobs")``, same as
``hunt_runs.record_hunt``.

Storage: a ``hunt_jobs`` table in tracker.db, created lazily (the
``hunt_runs`` / ``postings_seen`` pattern, NOT part of ``init_db()``). Pruned
inside every write: rows older than ``HUNT_JOBS_TTL_DAYS`` (default 30, owner
decision 2026-09-27), and rows whose hunt already left the ``hunt_runs`` ring.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from hunter.config import HUNT_JOBS_TTL_DAYS, TRACKER_DB_PATH
from hunter.db import get_db

log = logging.getLogger(__name__)

__all__ = ["DUP_FATES", "FATES", "jobs_for_hunt", "record_hunt_jobs"]

# Module-level so tests can monkeypatch it (mirrors hunter.hunt_runs.DB_PATH).
DB_PATH = TRACKER_DB_PATH

DUP_FATES: tuple[str, ...] = ("dup_url", "dup_ct", "dup_cooldown")
FATES: tuple[str, ...] = (
    *DUP_FATES,
    "new",
    "card",
    "capped",
    "queued",
    "applied_inline",
)

_DDL = """
CREATE TABLE IF NOT EXISTS hunt_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    hunt_id      TEXT NOT NULL,
    ts           TEXT NOT NULL,
    url_norm     TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    company      TEXT NOT NULL DEFAULT '',
    fate         TEXT NOT NULL,
    fate_detail  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hunt_jobs_hunt ON hunt_jobs(hunt_id, id);
CREATE INDEX IF NOT EXISTS idx_hunt_jobs_url ON hunt_jobs(url_norm);
"""

_COLUMNS = "hunt_id, ts, url_norm, url, source, title, company, fate, fate_detail"


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_hunt_jobs(
    hunt_id: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    ts: str | None = None,
) -> int:
    """Insert one row per vacancy of this hunt; return how many were written.

    Each mapping carries ``url_norm``, ``url``, ``source``, ``title``,
    ``company``, ``fate`` and optionally ``fate_detail``. An unknown fate is
    stored as given (a report column, never a gate) but logged. A blank
    ``hunt_id`` writes nothing: without it the rows could never be joined back
    to their hunt. Prunes orphans in the same transaction.

    Raises on a broken DB — see the module docstring.
    """
    if not hunt_id:
        return 0
    stamp = ts or _utc_now_iso()
    values = []
    for r in rows:
        fate = str(r.get("fate") or "")
        if fate not in FATES:
            log.warning("hunt_jobs: unknown fate %r (stored as-is)", fate)
        values.append(
            (
                hunt_id,
                stamp,
                str(r.get("url_norm") or ""),
                str(r.get("url") or ""),
                str(r.get("source") or ""),
                str(r.get("title") or "")[:300],
                str(r.get("company") or "")[:200],
                fate,
                str(r.get("fate_detail") or "")[:300],
            )
        )
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        if values:
            conn.executemany(
                f"INSERT INTO hunt_jobs ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",  # noqa: S608 — constant column list
                values,
            )
        _prune(conn)
    return len(values)


def _prune(conn: sqlite3.Connection) -> None:
    """Drop rows past the TTL, then rows whose hunt left the ``hunt_runs`` ring.

    The TTL (``HUNT_JOBS_TTL_DAYS``) is the retention the owner chose; the
    ring rule only matters when ``HUNT_RUNS_KEEP`` is shorter than the TTL.
    When ``hunt_runs`` does not exist in this database (an isolated test DB,
    or the hunt_runs writer disabled), the ring rule is skipped — never delete
    everything because the reference table is missing.
    """
    ttl_days = max(1, int(HUNT_JOBS_TTL_DAYS))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl_days)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM hunt_jobs WHERE ts < ?", (cutoff,))
    has_runs = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hunt_runs'"
    ).fetchone()
    if not has_runs:
        return
    # The ts guard keeps a hunt whose own hunt_runs write failed (or has not
    # happened yet) — only rows OLDER than the oldest retained hunt go.
    conn.execute(
        """
        DELETE FROM hunt_jobs
        WHERE hunt_id NOT IN (SELECT hunt_id FROM hunt_runs WHERE hunt_id != '')
          AND ts < (SELECT MIN(ts) FROM hunt_runs)
        """
    )


def jobs_for_hunt(hunt_id: str) -> list[dict[str, Any]]:
    """Every row of one hunt, in the order the loop decided them."""
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        rows = conn.execute(
            f"SELECT id, {_COLUMNS} FROM hunt_jobs WHERE hunt_id = ? ORDER BY id",  # noqa: S608 — constant column list
            (hunt_id,),
        ).fetchall()
    return [dict(r) for r in rows]
