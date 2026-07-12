"""
hunter/source_health.py — per-source yield tracking + breakage detection.

A scraper that returns 0 jobs is indistinguishable from "no new vacancies"
unless you know its baseline. This module records every source's raw yield per
hunt run in SQLite and flags a source that *used to* produce jobs but has gone
dry for several consecutive runs — the signature of a broken selector / renamed
API field, not a quiet day.

Public API
----------
    record_run(source, yield_count, ok=True, error="")   persist one run
    recent_runs(source, limit=20)                         list[RunRow] newest-first
    source_health(source)                                 HealthRow for one source
    health_report(source_names=None)                      list[HealthRow]
    newly_broken(source)                                  True at the exact run a
                                                          working source crosses the
                                                          zero-streak threshold

Storage: a `source_runs` table in the same tracker.db (created lazily). Old rows
are pruned to the last `SOURCE_HEALTH_KEEP` per source so the table stays small.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from hunter.config import (
    SOURCE_HEALTH_ALERT_STREAK,
    SOURCE_HEALTH_KEEP,
    TRACKER_DB_PATH,
)
from hunter.db import get_db

log = logging.getLogger(__name__)

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.tracker.DB_PATH).
DB_PATH = TRACKER_DB_PATH

_DDL = """
CREATE TABLE IF NOT EXISTS source_runs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source  TEXT    NOT NULL,
    ts      TEXT    NOT NULL,
    yield   INTEGER NOT NULL DEFAULT 0,
    ok      INTEGER NOT NULL DEFAULT 1,
    error   TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source, id);
"""


def _ensure_table(conn) -> None:
    conn.executescript(_DDL)


# ── Data rows ─────────────────────────────────────────────────────────────────


@dataclass
class RunRow:
    source: str
    ts: str
    yield_count: int
    ok: bool
    error: str


@dataclass
class HealthRow:
    source: str
    last_ts: str | None
    last_yield: int | None
    last_ok: bool
    avg_yield: float  # mean yield over recorded OK runs (0.0 if none)
    runs: int  # number of recorded runs
    zero_streak: int  # leading consecutive runs with 0 yield or error
    ever_positive: bool  # any recorded run had yield > 0
    status: str  # OK | IDLE | BROKEN? | ERROR | NODATA

    @property
    def icon(self) -> str:
        return {
            "OK": "✅",
            "IDLE": "💤",
            "BROKEN?": "⚠️",
            "ERROR": "❌",
            "NODATA": "—",
        }.get(self.status, "—")


# ── Write ─────────────────────────────────────────────────────────────────────


def record_run(source: str, yield_count: int, ok: bool = True, error: str = "") -> None:
    """Persist one source run. Best-effort: never raises into the hunt loop."""
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with get_db(DB_PATH) as conn:
            _ensure_table(conn)
            conn.execute(
                "INSERT INTO source_runs (source, ts, yield, ok, error) VALUES (?,?,?,?,?)",
                (source, ts, max(0, int(yield_count)), 1 if ok else 0, (error or "")[:300]),
            )
            _prune(conn, source)
    except Exception as e:  # noqa: BLE001 — telemetry must never break a hunt
        log.warning("source_health.record_run failed for %s: %s", source, e)


def _prune(conn, source: str) -> None:
    """Keep only the newest SOURCE_HEALTH_KEEP rows for `source`."""
    conn.execute(
        """
        DELETE FROM source_runs
        WHERE source = ?
          AND id NOT IN (
              SELECT id FROM source_runs WHERE source = ?
              ORDER BY id DESC LIMIT ?
          )
        """,
        (source, source, SOURCE_HEALTH_KEEP),
    )


# ── Read ──────────────────────────────────────────────────────────────────────


def recent_runs(source: str, limit: int = 20) -> list[RunRow]:
    """Return up to `limit` most recent runs for `source`, newest first."""
    try:
        with get_db(DB_PATH) as conn:
            _ensure_table(conn)
            rows = conn.execute(
                "SELECT source, ts, yield, ok, error FROM source_runs "
                "WHERE source = ? ORDER BY id DESC LIMIT ?",
                (source, limit),
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("source_health.recent_runs failed for %s: %s", source, e)
        return []
    return [RunRow(r["source"], r["ts"], r["yield"], bool(r["ok"]), r["error"]) for r in rows]


def _classify(runs: list[RunRow], source: str = "") -> HealthRow:
    """Build a HealthRow from a source's recent runs (newest first)."""
    source = source or (runs[0].source if runs else "")
    if not runs:
        return HealthRow(source, None, None, False, 0.0, 0, 0, False, "NODATA")

    last = runs[0]
    ok_yields = [r.yield_count for r in runs if r.ok]
    avg_yield = sum(ok_yields) / len(ok_yields) if ok_yields else 0.0
    ever_positive = any(r.yield_count > 0 for r in runs)

    zero_streak = 0
    for r in runs:
        if (not r.ok) or r.yield_count == 0:
            zero_streak += 1
        else:
            break

    if not last.ok:
        status = "ERROR"
    elif ever_positive and zero_streak >= SOURCE_HEALTH_ALERT_STREAK:
        status = "BROKEN?"
    elif last.yield_count == 0:
        status = "IDLE"
    else:
        status = "OK"

    return HealthRow(
        source=source,
        last_ts=last.ts,
        last_yield=last.yield_count,
        last_ok=last.ok,
        avg_yield=round(avg_yield, 1),
        runs=len(runs),
        zero_streak=zero_streak,
        ever_positive=ever_positive,
        status=status,
    )


def source_health(source: str, window: int = 20) -> HealthRow:
    """Health summary for one source over its last `window` runs."""
    return _classify(recent_runs(source, limit=window), source=source)


def health_report(source_names: list[str] | None = None, window: int = 20) -> list[HealthRow]:
    """Health summary per source.

    If `source_names` is given, report exactly those (a source with no recorded
    runs yields a NODATA row). Otherwise report every source that has any rows,
    ordered by status severity then name.
    """
    if source_names is None:
        try:
            with get_db(DB_PATH) as conn:
                _ensure_table(conn)
                names = [
                    r["source"]
                    for r in conn.execute(
                        "SELECT DISTINCT source FROM source_runs ORDER BY source"
                    ).fetchall()
                ]
        except Exception as e:  # noqa: BLE001
            log.warning("source_health.health_report failed: %s", e)
            names = []
    else:
        names = list(source_names)

    rows = [source_health(n, window=window) for n in names]
    severity = {"ERROR": 0, "BROKEN?": 1, "IDLE": 2, "NODATA": 3, "OK": 4}
    rows.sort(key=lambda h: (severity.get(h.status, 9), h.source))
    return rows


def newly_broken(source: str) -> bool:
    """True only at the run that *first* pushes a working source over the
    zero-streak threshold — so a breakage alerts exactly once per episode.

    Requires the source to have produced jobs before (ever_positive), so a board
    that is simply always-empty for our filters never alerts.
    """
    h = source_health(source)
    return h.status == "BROKEN?" and h.ever_positive and h.zero_streak == SOURCE_HEALTH_ALERT_STREAK
