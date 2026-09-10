"""
hunter/metrics.py — a per-run metrics log for the apply pipeline.

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M1. The pipeline makes several
expensive decisions (5 refine rounds, which model to use, which gate fires)
around numbers whose relationship to the real outcome has never been
measured, because nothing records per-run/per-stage data — the tracker row
only has a day-granularity `date` and a handful of terminal columns.

Two tables in the same tracker.db, created lazily (mirrors
`hunter.source_health`'s own lazy-ensure pattern for `source_runs`, and
`hunter.db.ensure_subsystem_health_table` for `subsystem_health`):

  generation_runs  — one row per apply attempt: pipeline/profile/model,
                     track, posting language, source, manual/force flags,
                     every gate/loop's summary numbers (ATS pre-score, PDF
                     score, verdict before/after refine, judge violations,
                     language-gate hits, cost), and the terminal outcome.
  pipeline_events  — a free-form append log of stage transitions for one run
                     (fetch/gate/generate/judge/lang_gate/render/verdict/
                     refine), with an optional duration and a JSON payload.

Public API
----------
    start_run(**fields) -> run_id            always returns a run_id (str);
                                              the INSERT itself is best-effort
    stage(run_id, stage, event, duration_ms=None, payload=None)
    update_run(run_id, **fields)             fields must be in ALLOWED_RUN_FIELDS
    finish_run(run_id, outcome, exit_code=None, **extra)
    timed_stage(run_id, name)                context manager: records
                                              "ok"/"error" + duration_ms,
                                              re-raises the block's exception

Every public function wraps its own DB access in
``with hunter.best_effort.best_effort("metrics"):`` — a metrics write must
NEVER be the reason an apply fails, and `start_run` always hands back a
run_id (even when the INSERT itself silently failed) so every call site can
call `stage()`/`finish_run()` unconditionally, with no None-check needed.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

from hunter.best_effort import best_effort
from hunter.config import TRACKER_DB_PATH
from hunter.db import get_db

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.source_health.DB_PATH / hunter.best_effort.DB_PATH).
DB_PATH = TRACKER_DB_PATH

_DDL = """
CREATE TABLE IF NOT EXISTS generation_runs (
    run_id            TEXT    PRIMARY KEY,
    user_id           TEXT    NOT NULL DEFAULT '',
    url_norm          TEXT    NOT NULL DEFAULT '',
    row_id            TEXT,
    started_at        TEXT,
    finished_at       TEXT,
    pipeline          TEXT    NOT NULL DEFAULT '',
    profile           TEXT    NOT NULL DEFAULT '',
    gen_model         TEXT    NOT NULL DEFAULT '',
    judge_model       TEXT    NOT NULL DEFAULT '',
    track             TEXT    NOT NULL DEFAULT '',
    posting_lang      TEXT    NOT NULL DEFAULT '',
    source            TEXT    NOT NULL DEFAULT '',
    is_manual         INTEGER NOT NULL DEFAULT 0,
    is_force          INTEGER NOT NULL DEFAULT 0,
    ats_pre_score     REAL,
    ats_pre_keyword   REAL,
    ats_pdf_score     REAL,
    verdict_first     REAL,
    verdict_final     REAL,
    refine_rounds     INTEGER,
    refine_accepted   INTEGER,
    best_round_kind   TEXT,
    judge_violations  INTEGER,
    judge_repaired    INTEGER,
    judge_surviving   INTEGER,
    lang_gate_hits    INTEGER,
    lang_gate_blocked INTEGER,
    scrub_fixes       INTEGER,
    reused_donor      TEXT,
    cost_usd          REAL,
    outcome           TEXT,
    exit_code         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_generation_runs_url_norm ON generation_runs(url_norm);
CREATE INDEX IF NOT EXISTS idx_generation_runs_started_at ON generation_runs(started_at);

CREATE TABLE IF NOT EXISTS pipeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT    NOT NULL,
    ts          TEXT    NOT NULL,
    stage       TEXT    NOT NULL,
    event       TEXT    NOT NULL,
    duration_ms INTEGER,
    payload     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pipeline_events_run_id ON pipeline_events(run_id, id);
"""

# Whitelist of columns update_run()/finish_run() may write — fields come from
# our own pipeline code, never from untrusted input, but a whitelist keeps
# the f-string SQL builder honest and catches a typo'd field name loudly in
# tests rather than silently no-op'ing in prod.
ALLOWED_RUN_FIELDS = frozenset(
    {
        "user_id",
        "url_norm",
        "row_id",
        "started_at",
        "finished_at",
        "pipeline",
        "profile",
        "gen_model",
        "judge_model",
        "track",
        "posting_lang",
        "source",
        "is_manual",
        "is_force",
        "ats_pre_score",
        "ats_pre_keyword",
        "ats_pdf_score",
        "verdict_first",
        "verdict_final",
        "refine_rounds",
        "refine_accepted",
        "best_round_kind",
        "judge_violations",
        "judge_repaired",
        "judge_surviving",
        "lang_gate_hits",
        "lang_gate_blocked",
        "scrub_fixes",
        "reused_donor",
        "cost_usd",
        "outcome",
        "exit_code",
    }
)


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_bool_int(value: Any) -> int:
    return 1 if value else 0


# ── Write: runs ────────────────────────────────────────────────────────────


def start_run(
    *,
    run_id: str | None = None,
    user_id: str = "",
    url_norm: str = "",
    pipeline: str = "",
    profile: str = "",
    gen_model: str = "",
    judge_model: str = "",
    track: str = "",
    posting_lang: str = "",
    source: str = "",
    is_manual: bool = False,
    is_force: bool = False,
    started_at: str | None = None,
) -> str:
    """Insert a new generation_runs row and return its run_id.

    Always returns a usable run_id, generating one with uuid4 when not
    given — even if the INSERT itself fails (best-effort), so callers never
    need to guard `stage()`/`finish_run()` calls with a None check.
    """
    resolved_run_id = run_id or uuid.uuid4().hex
    ts = started_at or _now_iso()
    with best_effort("metrics"), get_db(DB_PATH) as conn:
        _ensure_tables(conn)
        conn.execute(
            """
                INSERT OR IGNORE INTO generation_runs
                    (run_id, user_id, url_norm, started_at, pipeline, profile,
                     gen_model, judge_model, track, posting_lang, source,
                     is_manual, is_force)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
            (
                resolved_run_id,
                user_id or "",
                url_norm or "",
                ts,
                pipeline or "",
                profile or "",
                gen_model or "",
                judge_model or "",
                track or "",
                posting_lang or "",
                source or "",
                _to_bool_int(is_manual),
                _to_bool_int(is_force),
            ),
        )
    return resolved_run_id


def update_run(run_id: str | None, **fields: Any) -> None:
    """Merge `fields` (must all be in ALLOWED_RUN_FIELDS) onto an existing
    generation_runs row. No-op for an unknown run_id, an empty run_id, or an
    empty `fields`."""
    if not run_id or not fields:
        return
    unknown = set(fields) - ALLOWED_RUN_FIELDS
    if unknown:
        raise ValueError(f"metrics.update_run: unknown field(s): {sorted(unknown)}")
    with best_effort("metrics"), get_db(DB_PATH) as conn:
        _ensure_tables(conn)
        # Every key was checked against _RUN_FIELDS above, so the only
        # thing interpolated here is a known column name; the values stay
        # parameterised.
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE generation_runs SET {set_clause} WHERE run_id = ?",  # noqa: S608
            (*fields.values(), run_id),
        )


def finish_run(
    run_id: str | None, outcome: str, exit_code: int | None = None, **extra: Any
) -> None:
    """Stamp the terminal outcome + finished_at, plus any other known field."""
    fields = dict(extra)
    fields["outcome"] = outcome
    fields["exit_code"] = exit_code
    fields["finished_at"] = _now_iso()
    update_run(run_id, **fields)


# ── Write: events ─────────────────────────────────────────────────────────


def stage(
    run_id: str | None,
    stage: str,
    event: str,
    *,
    duration_ms: int | None = None,
    payload: Any = None,
) -> None:
    """Append one pipeline_events row. No-op for an empty run_id."""
    if not run_id:
        return
    payload_json = ""
    if payload is not None:
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001 — a bad payload must not lose the event
            payload_json = str(payload)
    ts = _now_iso()
    with best_effort("metrics"), get_db(DB_PATH) as conn:
        _ensure_tables(conn)
        conn.execute(
            """
                INSERT INTO pipeline_events (run_id, ts, stage, event, duration_ms, payload)
                VALUES (?,?,?,?,?,?)
                """,
            (run_id, ts, stage, event, duration_ms, payload_json),
        )


@contextmanager
def timed_stage(
    run_id: str | None, name: str, *, payload: Any = None
) -> Generator[None, None, None]:
    """Time a block and record it as one pipeline_events row.

    Records event="ok" with the elapsed duration_ms on a clean exit, or
    event="error" (also timed) and re-raises on an exception — this context
    manager records telemetry, it never swallows the wrapped block's own
    errors (unlike best_effort(), which is for a self-contained side effect).
    """
    t0 = time.monotonic()
    try:
        yield
    except Exception:
        stage(run_id, name, "error", duration_ms=int((time.monotonic() - t0) * 1000))
        raise
    else:
        stage(run_id, name, "ok", duration_ms=int((time.monotonic() - t0) * 1000), payload=payload)


# ── Read (diagnostics / tests / /status) ────────────────────────────────────


def count_runs_since(days: int = 7) -> int:
    """Number of generation_runs rows started in the last `days` days.

    Best-effort: returns 0 on any failure (missing table, DB error) rather
    than raising — this feeds an informational /status line, never a gate.
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    try:
        with get_db(DB_PATH) as conn:
            _ensure_tables(conn)
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM generation_runs WHERE started_at >= ?",
                (cutoff,),
            ).fetchone()
            return int(row["n"]) if row else 0
    except Exception:  # noqa: BLE001 — informational only
        return 0
