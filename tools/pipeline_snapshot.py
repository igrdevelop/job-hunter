"""
tools/pipeline_snapshot.py — one read-only snapshot of the whole vacancy
pipeline, from the tables the bot already writes.

docs/PIPELINE_VIZ_PLAN.md M0. Builds the three tiers the planned site page
shows (hunt → apply → result) plus the event footer, entirely from
tracker.db, and then answers the plan's five decision rules: is the data
already good enough to draw the page from, or does M1 instrumentation
(`start` events, per-refine-round events, a `hunt_runs` table, a
`queued_at` column, orphan-run stamping) have to land first?

Since 2026-09-22 the tool also READS that M1 data where it exists (PRs
#288–#291): the `hunt_runs` funnel is the primary hunt line, PENDING rows
carry their queue wait from `queued_at`, and the in-progress card reads the
`start` event and the latest refine-round event instead of inferring. Every
pre-M1 branch is kept — a backup copy from before the instrumentation still
snapshots, it just says less.

Sections:
  hunt      hunt_runs funnel (found → filtered → dup → new → queued, the
            loop's own counters), source_runs yield in the window,
            postings_seen verdicts (passed / rejected + top reasons), rows
            that entered the tracker, and the next hunt slot computed from
            the same grid the scheduler uses
  apply     PENDING / IN_PROGRESS rows (queue wait from `queued_at`), the
            open generation_runs row behind each IN_PROGRESS one with its
            last pipeline_events stage, the minutes since that stage's
            `start` event and the latest refine round (the "where is it
            right now" card), $0 cut-offs by outcome, FAIL rows and the
            apply_failures.jsonl records in the window
  result    ready-to-send (applied, sent=''), sent in the window, recorded
            outcomes, LLM spend
  events    the last N pipeline_events joined to company/title
  coverage  the five decision rules with PASS / FAIL / UNMEASURED

Read-only: the DB is opened with `mode=ro`, nothing is written, no network,
no LLM. A table that does not exist on the target DB (a pre-M1 checkout, a
fresh dev DB) reports UNMEASURED for the rules that need it — never 0.

Usage:
    docker compose exec -T job-hunter python tools/pipeline_snapshot.py --db tracker.db
    docker compose exec -T job-hunter python tools/pipeline_snapshot.py --db tracker.db --days 7
    docker compose exec -T job-hunter python tools/pipeline_snapshot.py --db tracker.db --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# Force UTF-8 stdout/stderr on Windows (console defaults to cp1252) — same
# guard as tools/render_profile.py / tools/fail_signatures.py.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# ── Defaults from hunter.config (guarded: the tool must still run against a
# bare DB copy on a machine with no .env) ────────────────────────────────────
try:
    from hunter import config as _cfg

    DEFAULT_DB = str(_cfg.TRACKER_DB_PATH)
    DEFAULT_USER = _cfg.current_user_id()
    TIMEZONE = _cfg.TIMEZONE
    SCHEDULE_TIMES = list(_cfg.SCHEDULE_TIMES)
    SCHEDULE_SOURCE_OFFSET_MIN = int(_cfg.SCHEDULE_SOURCE_OFFSET_MIN)
    SCHEDULE_BLACKOUT = _cfg.SCHEDULE_BLACKOUT
    RETRY_FAILED_TIMES = list(_cfg.RETRY_FAILED_TIMES)
    APPLY_CLAIM_TIMEOUT_MIN = int(_cfg.APPLY_CLAIM_TIMEOUT_MIN)
    APPLY_AGENT_CLI_TIMEOUT_SEC = int(_cfg.APPLY_AGENT_CLI_TIMEOUT_SEC)
    APPLY_QUEUE_ENABLED = bool(_cfg.APPLY_QUEUE_ENABLED)
except Exception:  # noqa: BLE001 — no .env, no problem
    DEFAULT_DB = "tracker.db"
    DEFAULT_USER = ""
    TIMEZONE = "Europe/Warsaw"
    SCHEDULE_TIMES = ["02:00", "05:00", "08:00", "13:00"]
    SCHEDULE_SOURCE_OFFSET_MIN = 40
    SCHEDULE_BLACKOUT = "18:00-00:00"
    RETRY_FAILED_TIMES = ["02:45", "07:45"]
    APPLY_CLAIM_TIMEOUT_MIN = 60
    APPLY_AGENT_CLI_TIMEOUT_SEC = 10800
    APPLY_QUEUE_ENABLED = False

try:
    from hunter.tracker import MAX_FAIL_RETRIES
except Exception:  # noqa: BLE001
    MAX_FAIL_RETRIES = 3

# The hunt_runs count columns, in DDL order — a pure constant, imported so the
# tool sums exactly the set `hunter.hunt_runs.sum_window` does. The module's
# writer/reader functions are NOT used: they open the bot's own DB path with a
# writable connection, and this tool owns one read-only connection.
try:
    from hunter.hunt_runs import COUNT_COLUMNS as HUNT_RUN_COUNT_COLUMNS
except ImportError:  # a bare DB copy on a machine without the package — a real
    # error INSIDE hunter.hunt_runs must surface, so only the missing-module
    # case falls back to the pinned list
    HUNT_RUN_COUNT_COLUMNS = (
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

# `generation_runs.outcome` prefix a parent/sweeper stamp carries (never the
# pipeline itself) — hunter.metrics.ORPHAN_PREFIX; a literal so the tool runs
# against a bare DB copy with no package import.
ORPHAN_PREFIX = "orphan:"

# pipeline_events.event values the refine loop writes once per round (the
# `start` event opens the loop and carries no round).
REFINE_ROUND_EVENTS = ("accepted", "rejected", "discarded")

try:
    from hunter.sent_parse import classify as _classify_sent
    from hunter.sent_parse import parse_sent_date as _parse_sent_date
except Exception:  # noqa: BLE001

    def _classify_sent(value: str) -> str:  # type: ignore[misc]
        v = (value or "").strip()
        if not v:
            return "blank"
        if v.upper() == "EXPIRED":
            return "expired"
        return "applied" if v[:4].isdigit() else "other"

    def _parse_sent_date(value: str) -> date | None:  # type: ignore[misc]
        try:
            return date.fromisoformat((value or "").strip()[:10])
        except ValueError:
            return None


PLACEHOLDER_STATUSES = ("PENDING", "IN_PROGRESS")
NON_APPLIED_STATUSES = (
    "SKIP",
    "FAIL",
    "MANUAL",
    "EXPIRED",
    "PENDING",
    "IN_PROGRESS",
    "—",
    "–",
    "-",
)

# generation_runs.outcome values that mean "decided before the first
# generation call" — the $0 cut-off stack on the page.
ZERO_COST_OUTCOMES = (
    "expired",
    "too_short",
    "skip_react_pre_llm",
    "skip_backend_only",
    "skip_doomed_gate",
    "reused_repost",
    "skip_prescreen",
)

# Canonical stage order of one apply run, for inferring "current stage" from
# the last recorded pipeline_events row. Stages that exist as events today
# are marked; the others are what the page shows between two recorded ones.
STAGE_ORDER = [
    "fetch",
    "gates",
    "generate",
    "ats_loop",
    "judge",
    "lang_gate",
    "render",
    "verdict",
    "refine",
    "delivery",
]

TZ = ZoneInfo(TIMEZONE)


# ── Small helpers ─────────────────────────────────────────────────────────────


def _parse_ts(value: Any) -> datetime | None:
    """Tolerant UTC parser for the three timestamp shapes the DB holds:
    `…+00:00` (metrics/source_runs/postings_seen), `…Z` (claimed_at) and a
    naive ISO string (treated as UTC)."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _local_hhmm(value: Any) -> str:
    """Local HH:MM for a timestamp from today, MM-DD HH:MM for an older one."""
    dt = _parse_ts(value)
    if dt is None:
        return "--:--"
    local = dt.astimezone(TZ)
    fmt = "%H:%M" if local.date() == datetime.now(TZ).date() else "%m-%d %H:%M"
    return local.strftime(fmt)


def _minutes_ago(value: Any, now: datetime) -> int | None:
    dt = _parse_ts(value)
    if dt is None:
        return None
    return max(0, int((now - dt).total_seconds() // 60))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _pct(part: float, whole: float) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


class Window:
    """Calendar-day window in the schedule's own timezone (Warsaw), so
    "today" means what the owner's clock and `/schedule` mean."""

    def __init__(self, days: int, now: datetime | None = None) -> None:
        self.now = now or datetime.now(timezone.utc)
        local_now = self.now.astimezone(TZ)
        start_local = (local_now - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self.days = days
        self.start = start_local.astimezone(timezone.utc)
        self.start_iso = _iso_utc(self.start)
        # applications.date is a local YYYY-MM-DD string (date.today() at write)
        self.dates = [(start_local + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        self.label = "today" if days == 1 else f"last {days} days"

    def date_placeholders(self) -> str:
        return ",".join("?" for _ in self.dates)

    def contains(self, dt: datetime | None) -> bool:
        return dt is not None and dt >= self.start


# ── Hunt tier ─────────────────────────────────────────────────────────────────


def hunt_tier(conn: sqlite3.Connection, win: Window, user_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"window": win.label}

    # hunt_runs (M1) — the loop's own funnel, one row per hunt. The PRIMARY
    # funnel line: these are the numbers `_run_hunt_impl` held when it
    # decided, so found → filtered → dup → new → queued adds up by
    # construction. source_runs / postings_seen below stay as the secondary,
    # per-source and per-listing views (and as the pre-M1 comparison).
    # A table that exists but predates a column (a DB written by an older
    # bot, a hand-created fixture) must report UNMEASURED with the reason —
    # never raise out of a read-only snapshot.
    out["hunt_runs"], out["hunt_runs_unmeasured"] = None, None
    if _table_exists(conn, "hunt_runs"):
        missing = sorted(set(HUNT_RUN_REQUIRED_COLUMNS) - _columns(conn, "hunt_runs"))
        if missing:
            out["hunt_runs_unmeasured"] = f"hunt_runs table lacks columns: {', '.join(missing)}"
        else:
            out["hunt_runs"] = _hunt_runs_window(conn, win)
    else:
        out["hunt_runs_unmeasured"] = "hunt_runs table missing"

    if _table_exists(conn, "source_runs"):
        rows = conn.execute(
            "SELECT source, ts, yield, ok, error FROM source_runs WHERE ts >= ? ORDER BY id",
            (win.start_iso,),
        ).fetchall()
        per_source: dict[str, dict[str, Any]] = {}
        for r in rows:
            s = per_source.setdefault(
                r["source"], {"runs": 0, "found": 0, "errors": 0, "last_ok": None}
            )
            s["runs"] += 1
            s["found"] += int(r["yield"] or 0)
            if not r["ok"]:
                s["errors"] += 1
            s["last_ok"] = bool(r["ok"])
        out["source_runs"] = {
            "runs": len(rows),
            "found_raw": sum(int(r["yield"] or 0) for r in rows),
            "sources_ran": len(per_source),
            "sources_last_run_ok": sum(1 for s in per_source.values() if s["last_ok"]),
            "errors": sum(1 for r in rows if not r["ok"]),
            "per_source": dict(sorted(per_source.items(), key=lambda kv: -kv[1]["found"])),
        }
    else:
        out["source_runs"] = None

    if _table_exists(conn, "postings_seen"):
        seen = conn.execute(
            "SELECT filter_verdict_last, first_seen, source FROM postings_seen WHERE last_seen >= ?",
            (win.start_iso,),
        ).fetchall()
        reasons = Counter(
            (r["filter_verdict_last"] or "").split(":")[0] or "(blank)"
            for r in seen
            if (r["filter_verdict_last"] or "") != "passed"
        )
        out["postings_seen"] = {
            "unique_seen": len(seen),
            "new_this_window": sum(1 for r in seen if (r["first_seen"] or "") >= win.start_iso),
            "passed": sum(1 for r in seen if (r["filter_verdict_last"] or "") == "passed"),
            "rejected": sum(1 for r in seen if (r["filter_verdict_last"] or "") != "passed"),
            "top_reasons": reasons.most_common(8),
        }
    else:
        out["postings_seen"] = None

    cols = _columns(conn, "applications")
    source_col = "source" if "source" in cols else "''"
    ph = win.date_placeholders()
    rows = conn.execute(
        f"SELECT ats_status, {source_col} AS source "  # noqa: S608 — column name is a literal
        f"FROM applications WHERE user_id = ? AND date IN ({ph})",
        (user_id, *win.dates),
    ).fetchall()
    by_status = Counter(_bucket_status(r["ats_status"]) for r in rows)
    by_source = Counter((r["source"] or "(blank)") for r in rows)
    out["entered_tracker"] = {
        "rows": len(rows),
        "by_status": dict(by_status),
        "by_source": by_source.most_common(10),
    }

    out["next_slot"] = _next_hunt_slot(win.now)
    return out


# Every column _hunt_runs_window reads; checked against PRAGMA table_info
# before the query so a partial schema degrades to UNMEASURED.
HUNT_RUN_REQUIRED_COLUMNS = ("ts", "trigger", "sources", "filter_reasons", *HUNT_RUN_COUNT_COLUMNS)


def _hunt_runs_window(conn: sqlite3.Connection, win: Window) -> dict[str, Any]:
    """Totals over the hunt_runs rows in the window — the same arithmetic as
    `hunter.hunt_runs.sum_window`, run over this tool's own read-only
    connection. An empty window is all zeros with `last=None`."""
    count_cols = ", ".join(f'"{c}"' for c in HUNT_RUN_COUNT_COLUMNS)
    rows = conn.execute(
        f'SELECT ts, "trigger", sources, filter_reasons, {count_cols} '  # noqa: S608 — constant column list
        "FROM hunt_runs WHERE ts >= ? ORDER BY id",
        (win.start_iso,),
    ).fetchall()
    totals: dict[str, Any] = {"hunts": len(rows), **dict.fromkeys(HUNT_RUN_COUNT_COLUMNS, 0)}
    reasons: Counter[str] = Counter()
    by_trigger: Counter[str] = Counter()
    for r in rows:
        for c in HUNT_RUN_COUNT_COLUMNS:
            totals[c] += int(r[c] or 0)
        by_trigger[r["trigger"] or "?"] += 1
        try:
            parsed = json.loads(r["filter_reasons"] or "{}")
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                try:
                    reasons[str(k)] += max(0, int(v or 0))
                except (TypeError, ValueError):
                    continue
    last = rows[-1] if rows else None
    if last is not None:
        try:
            last_sources = json.loads(last["sources"] or "[]")
        except (TypeError, ValueError):
            last_sources = []
        totals["last"] = {
            "ts": last["ts"],
            "at": _local_hhmm(last["ts"]),
            "trigger": last["trigger"],
            "sources": last_sources if isinstance(last_sources, list) else [],
            "found": int(last["found"] or 0),
            "new": int(last["new"] or 0),
        }
    else:
        totals["last"] = None
    totals["by_trigger"] = dict(by_trigger)
    totals["top_filter_reasons"] = reasons.most_common(8)
    return totals


def _bucket_status(ats: str | None) -> str:
    a = (ats or "").strip().upper()
    if a in ("PENDING", "IN_PROGRESS", "SKIP", "FAIL", "MANUAL", "EXPIRED"):
        return a
    if a in ("", "—", "–", "-"):
        return "(blank)"
    return "APPLIED"


def _next_hunt_slot(now: datetime) -> dict[str, Any] | None:
    """Next (time, source) the scheduler grid will fire — the same arithmetic
    `hunter/schedules/__init__.py::register` uses, so the page and the
    scheduler can never disagree."""
    try:
        from hunter.schedules import grid
        from hunter.sources import ALL_SOURCES
    except Exception as e:  # noqa: BLE001 — a scraper import failure must not kill the snapshot
        return {"error": f"unavailable: {e}"[:160]}

    names = [getattr(s, "name", type(s).__name__) for s in ALL_SOURCES]
    segments = grid.parse_blackout(SCHEDULE_BLACKOUT)
    local_now = now.astimezone(TZ)
    now_min = local_now.hour * 60 + local_now.minute
    candidates: list[tuple[int, int, str]] = []  # (minutes from now, minute-of-day, source)
    for base in SCHEDULE_TIMES:
        base_min = grid.parse_hhmm(base)
        if base_min is None:
            continue
        for idx, name in enumerate(names):
            fire = grid.fire_minute(base_min, idx, SCHEDULE_SOURCE_OFFSET_MIN, segments)
            delta = (fire - now_min) % grid.MINUTES_PER_DAY
            if delta == 0:
                delta = grid.MINUTES_PER_DAY
            candidates.append((delta, fire, name))
    if not candidates:
        return None
    delta, fire, name = min(candidates)
    return {
        "at": f"{fire // 60:02d}:{fire % 60:02d}",
        "in_min": delta,
        "source": name,
        "sources_total": len(names),
    }


def _next_hhmm(times: list[str], now: datetime) -> dict[str, Any] | None:
    local_now = now.astimezone(TZ)
    now_min = local_now.hour * 60 + local_now.minute
    best: tuple[int, str] | None = None
    for t in times:
        try:
            hh, mm = (int(p) for p in t.split(":"))
        except ValueError:
            continue
        delta = (hh * 60 + mm - now_min) % (24 * 60) or 24 * 60
        if best is None or delta < best[0]:
            best = (delta, t)
    return {"at": best[1], "in_min": best[0]} if best else None


# ── Apply tier ────────────────────────────────────────────────────────────────


def apply_tier(
    conn: sqlite3.Connection, win: Window, user_id: str, failures_log: Path
) -> dict[str, Any]:
    cols = _columns(conn, "applications")
    # Queue mode is read from the DATA, not from this machine's .env: an
    # off-host run (a backup copy on a laptop) would otherwise label prod's
    # queue mode with the laptop's APPLY_QUEUE_ENABLED. A row that ever
    # carried claimed_at went through claim_pending, which only the worker
    # calls.
    queue_seen = bool(
        "claimed_at" in cols
        and _scalar(conn, "SELECT 1 FROM applications WHERE claimed_at IS NOT NULL LIMIT 1")
    )
    out: dict[str, Any] = {
        "queue_mode_observed": queue_seen,
        "queue_enabled_local_config": APPLY_QUEUE_ENABLED,
    }
    has_metrics = _table_exists(conn, "generation_runs") and _table_exists(conn, "pipeline_events")

    # PENDING — the queue, in the order claim_pending drains it (rowid).
    # `queued_at` (M1) is the placeholder's UTC insertion time; NULL on a row
    # written before the column existed. `oldest_wait_min` follows the same
    # rule as tracker.oldest_pending_wait_min: it is the wait of the HEAD row
    # (queue order), and a NULL there reports None rather than skipping ahead
    # to a younger stamped row. (That function is not called here — it opens
    # the bot's DB path with its own user scope; this tool owns one ro
    # connection and an explicit --user.)
    source_col = "source" if "source" in cols else "''"
    claimed_by_col = "claimed_by" if "claimed_by" in cols else "''"
    queued_at_col = "queued_at" if "queued_at" in cols else "NULL"
    pend = conn.execute(
        f"SELECT company, title, date, rowid, {source_col} AS source, "  # noqa: S608 — literal column names
        f"{queued_at_col} AS queued_at "
        "FROM applications WHERE user_id = ? AND ats_status = 'PENDING' ORDER BY rowid",
        (user_id,),
    ).fetchall()
    out["pending"] = {
        "count": len(pend),
        "oldest_date": pend[0]["date"] if pend else None,
        "oldest_wait_min": _minutes_ago(pend[0]["queued_at"], win.now) if pend else None,
        "head": [
            {
                "company": r["company"],
                "title": r["title"],
                "source": r["source"],
                "wait_min": _minutes_ago(r["queued_at"], win.now),
            }
            for r in pend[:5]
        ],
    }

    # IN_PROGRESS — the card. Join the open generation_runs row (finished_at
    # NULL, same url_norm) and its last event to say where the run is.
    prog = conn.execute(
        f"SELECT company, title, url_norm, claimed_at, {claimed_by_col} AS claimed_by, "  # noqa: S608
        f"{source_col} AS source "
        "FROM applications WHERE user_id = ? AND ats_status = 'IN_PROGRESS' ORDER BY claimed_at",
        (user_id,),
    ).fetchall()
    cards = []
    for r in prog:
        card: dict[str, Any] = {
            "company": r["company"],
            "title": r["title"],
            "source": r["source"],
            "claimed_by": r["claimed_by"],
            "claimed_min_ago": _minutes_ago(r["claimed_at"], win.now),
            "stale": False,
            "run": None,
        }
        mins = card["claimed_min_ago"]
        card["stale"] = mins is not None and mins > APPLY_CLAIM_TIMEOUT_MIN
        if has_metrics and r["url_norm"]:
            card["run"] = _open_run_for(conn, r["url_norm"], win.now)
        cards.append(card)
    out["in_progress"] = {"count": len(prog), "cards": cards}

    # $0 cut-offs and the full outcome mix, from generation_runs.
    if _table_exists(conn, "generation_runs"):
        runs = conn.execute(
            "SELECT outcome FROM generation_runs "
            "WHERE started_at >= ? AND pipeline != 'backfill' AND (user_id = ? OR user_id = '')",
            (win.start_iso, user_id),
        ).fetchall()
        outcomes = Counter((r["outcome"] or "(open)") for r in runs)
        out["runs"] = {
            "started": len(runs),
            "outcomes": outcomes.most_common(),
            "cut_zero_cost": {k: outcomes[k] for k in ZERO_COST_OUTCOMES if outcomes[k]},
            "cut_zero_cost_total": sum(outcomes[k] for k in ZERO_COST_OUTCOMES),
        }
    else:
        out["runs"] = None

    # SKIP / EXPIRED tracker rows in the window, by skip_reason prefix.
    ph = win.date_placeholders()
    if "skip_reason" in cols:
        sk = conn.execute(
            f"SELECT ats_status, skip_reason FROM applications "  # noqa: S608 — ph is '?' placeholders
            f"WHERE user_id = ? AND date IN ({ph}) AND ats_status IN ('SKIP','EXPIRED')",
            (user_id, *win.dates),
        ).fetchall()
        prefixes = Counter(
            (
                r["ats_status"]
                if r["ats_status"] == "EXPIRED"
                else (r["skip_reason"] or "").split(":")[0] or "(untagged)"
            )
            for r in sk
        )
        out["skipped_rows"] = {"count": len(sk), "by_reason": prefixes.most_common()}
    else:
        out["skipped_rows"] = None

    # FAIL rows: in-window count, retryable vs given up (all time).
    fails = conn.execute(
        "SELECT date, fail_count FROM applications WHERE user_id = ? AND ats_status = 'FAIL'",
        (user_id,),
    ).fetchall()
    fc = "fail_count" in cols
    out["failures"] = {
        "in_window": sum(1 for r in fails if r["date"] in win.dates),
        "retryable_total": sum(
            1 for r in fails if not fc or (r["fail_count"] or 0) < MAX_FAIL_RETRIES
        ),
        "gave_up_total": sum(1 for r in fails if fc and (r["fail_count"] or 0) >= MAX_FAIL_RETRIES),
        "next_retry": _next_hhmm(RETRY_FAILED_TIMES, win.now),
        "log_records": _failure_log_records(failures_log, win),
    }

    out["llm_outage"] = _llm_outage(conn, win.now)
    return out


def _open_run_for(conn: sqlite3.Connection, url_norm: str, now: datetime) -> dict[str, Any] | None:
    run = conn.execute(
        "SELECT run_id, pipeline, profile, gen_model, started_at, verdict_first, verdict_final, "
        "refine_rounds, refine_accepted FROM generation_runs "
        "WHERE url_norm = ? AND finished_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (url_norm,),
    ).fetchone()
    if run is None:
        return None
    events = conn.execute(
        "SELECT ts, stage, event, duration_ms, payload FROM pipeline_events "
        "WHERE run_id = ? ORDER BY id",
        (run["run_id"],),
    ).fetchall()
    last = events[-1] if events else None
    current = _infer_stage(events)
    return {
        "run_id": run["run_id"],
        "pipeline": run["pipeline"],
        "profile": run["profile"] or run["gen_model"],
        "elapsed_min": _minutes_ago(run["started_at"], now),
        "events": len(events),
        "last_event": (
            {"stage": last["stage"], "event": last["event"], "at": _local_hhmm(last["ts"])}
            if last
            else None
        ),
        "current_stage": current,
        "stage_started_min_ago": _stage_started_min_ago(events, current["stage"], now),
        "refine_progress": _refine_progress(events),
        "verdict_first": run["verdict_first"],
        "verdict_final": run["verdict_final"],
        "refine_rounds": run["refine_rounds"],
        "refine_accepted": run["refine_accepted"],
    }


def _stage_started_min_ago(events: list[sqlite3.Row], stage: str, now: datetime) -> int | None:
    """Minutes since the `start` event of the CURRENT stage (M1), or None when
    the run has no `start` row for it — a pre-M1 run, or a stage that was
    inferred rather than observed."""
    for e in reversed(events):
        if e["event"] == "start":
            return _minutes_ago(e["ts"], now) if e["stage"] == stage else None
    return None


def _refine_progress(events: list[sqlite3.Row]) -> dict[str, Any] | None:
    """The latest refine ROUND (M1: one event per round from
    `verdict_refine.refine_loop`, payload `{round, kind, score, best,
    reason}`), or None before the first round / on a pre-M1 run."""
    for e in reversed(events):
        if e["stage"] != "refine" or e["event"] not in REFINE_ROUND_EVENTS:
            continue
        try:
            payload = json.loads(e["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "round": payload.get("round"),
            "kind": payload.get("kind"),
            "score": payload.get("score"),
            "best": payload.get("best"),
            "outcome": e["event"],
            "at": _local_hhmm(e["ts"]),
        }
    return None


def _infer_stage(events: list[sqlite3.Row]) -> dict[str, Any]:
    """The stage a run is in RIGHT NOW. Observed from a `start` event or a
    refine-round event when the run has them (M1); otherwise the pre-M1
    guess — "the stage after the last `ok`" — with a basis that says so, so
    the page can still say "probably" for a run that predates the events."""
    if not events:
        return {"stage": "fetch", "basis": "no events yet"}
    last = events[-1]
    if last["event"] == "start":
        return {"stage": last["stage"], "basis": "start event"}
    if last["stage"] == "refine" and last["event"] in REFINE_ROUND_EVENTS:
        return {"stage": "refine", "basis": f"refine round {last['event']}"}
    if last["event"] in ("error", "blocked"):
        return {"stage": last["stage"], "basis": f"last event was {last['event']}"}
    try:
        i = STAGE_ORDER.index(last["stage"])
        nxt = STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else STAGE_ORDER[-1]
    except ValueError:
        nxt = "?"
    return {"stage": nxt, "basis": f"inferred: after '{last['stage']}' ok"}


def _failure_log_records(path: Path, win: Window) -> dict[str, Any] | None:
    if not path.exists():
        return None
    by_outcome: Counter[str] = Counter()
    total = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not win.contains(_parse_ts(rec.get("ts"))):
                    continue
                total += 1
                by_outcome[rec.get("outcome") or "?"] += 1
    except OSError:
        return None
    return {"in_window": total, "by_outcome": by_outcome.most_common()}


def _llm_outage(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    if not _table_exists(conn, "config"):
        return {"paused": False, "remaining_min": 0}
    raw = _scalar(conn, "SELECT value FROM config WHERE key = 'llm_outage_until'")
    try:
        until = int(float(raw)) if raw else 0
    except (TypeError, ValueError):
        until = 0
    left = max(0, int(until - now.timestamp()))
    return {"paused": left > 0, "remaining_min": left // 60}


# ── Result tier ───────────────────────────────────────────────────────────────


def result_tier(conn: sqlite3.Connection, win: Window, user_id: str) -> dict[str, Any]:
    cols = _columns(conn, "applications")
    sel = ["date", "ats_status", "sent", "cost_usd"]
    for opt in ("ats_verdict", "outcome_label", "outcome_at"):
        sel.append(opt if opt in cols else f"NULL AS {opt}")
    rows = conn.execute(
        f"SELECT {', '.join(sel)} FROM applications WHERE user_id = ?",  # noqa: S608 — column names are literals
        (user_id,),
    ).fetchall()

    applied = [r for r in rows if _bucket_status(r["ats_status"]) == "APPLIED"]
    ready = [r for r in applied if (r["sent"] or "").strip() == ""]
    ready_verdicts = [float(r["ats_verdict"]) for r in ready if r["ats_verdict"] is not None]

    sent_in_window = []
    for r in applied:
        if _classify_sent(r["sent"] or "") != "applied":
            continue
        d = _parse_sent_date(r["sent"] or "")
        if d and d.strftime("%Y-%m-%d") in win.dates:
            sent_in_window.append(r)

    outcomes = Counter(
        (r["outcome_label"] or "")
        for r in rows
        if (r["outcome_label"] or "") and win.contains(_parse_ts(r["outcome_at"]))
    )

    produced = [r for r in applied if r["date"] in win.dates]
    # cost_usd is 0.0 (not NULL) for a CLI-served run since the M4b fallback
    # — 42 of 51 prod rows in the 30-day window on 2026-09-22. Zero is
    # "unpriced", not "free": counting it as priced printed "$0.0 each".
    cost_rows = [r for r in produced if r["cost_usd"] is not None and float(r["cost_usd"]) > 0]
    total_cost = round(sum(float(r["cost_usd"]) for r in cost_rows), 2)

    return {
        "ready": {
            "count": len(ready),
            "produced_in_window": len(produced),
            "mean_verdict": round(sum(ready_verdicts) / len(ready_verdicts), 1)
            if ready_verdicts
            else None,
        },
        "sent_in_window": len(sent_in_window),
        "outcomes_in_window": outcomes.most_common(),
        "cost": {
            "total_usd": total_cost,
            "priced_rows": len(cost_rows),
            "unpriced_rows": len(produced) - len(cost_rows),
            "per_priced_row_usd": round(total_cost / len(cost_rows), 2) if cost_rows else None,
        },
    }


# ── Events footer ─────────────────────────────────────────────────────────────


def recent_events(conn: sqlite3.Connection, limit: int) -> list[dict[str, Any]] | None:
    if not (_table_exists(conn, "pipeline_events") and _table_exists(conn, "generation_runs")):
        return None
    rows = conn.execute(
        """
        SELECT e.ts, e.stage, e.event, e.duration_ms, e.payload, r.pipeline, r.url_norm,
               (SELECT company FROM applications a WHERE a.url_norm = r.url_norm LIMIT 1) AS company
        FROM pipeline_events e
        JOIN generation_runs r ON r.run_id = e.run_id
        ORDER BY e.ts DESC, e.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        payload = r["payload"] or ""
        out.append(
            {
                "at": _local_hhmm(r["ts"]),
                "ts": r["ts"],
                "stage": r["stage"],
                "event": r["event"],
                "duration_ms": r["duration_ms"],
                "company": r["company"] or "",
                "pipeline": r["pipeline"],
                "payload": payload[:80],
            }
        )
    return out


# ── Coverage — the plan's decision rules ──────────────────────────────────────


def coverage(
    conn: sqlite3.Connection, win: Window, user_id: str, hunt: dict[str, Any]
) -> dict[str, Any]:
    rules: dict[str, Any] = {}
    cols = _columns(conn, "applications")
    has_runs = _table_exists(conn, "generation_runs")
    has_events = _table_exists(conn, "pipeline_events")
    ph = win.date_placeholders()

    # Rule 1 — run coverage: produced rows ↔ generation_runs rows.
    if has_runs and "source" in cols:
        produced = conn.execute(
            f"SELECT url_norm, source FROM applications WHERE user_id = ? AND date IN ({ph}) "  # noqa: S608
            "AND ats_status NOT IN ('PENDING','IN_PROGRESS') AND url_norm != ''",
            (user_id, *win.dates),
        ).fetchall()
        # Rows with a blank `source` predate MARKET_MEMORY M3 (2026-09-13) —
        # and metrics itself (2026-09-10) — so they can't be expected to have
        # a run; they are reported, not counted.
        urls = {r["url_norm"] for r in produced if (r["source"] or "") != ""}
        excluded_blank_source = sum(1 for r in produced if (r["source"] or "") == "")
        covered = 0
        if urls:
            q = ",".join("?" for _ in urls)
            covered = int(
                _scalar(
                    conn,
                    f"SELECT COUNT(DISTINCT url_norm) FROM generation_runs "  # noqa: S608
                    f"WHERE pipeline != 'backfill' AND url_norm IN ({q})",
                    tuple(urls),
                )
                or 0
            )
        share = _pct(covered, len(urls))
        rules["1_run_coverage"] = {
            "rows_produced": len(urls),
            "excluded_blank_source": excluded_blank_source,
            "with_generation_run": covered,
            "share_pct": share,
            "threshold": ">= 90",
            "verdict": "UNMEASURED" if share is None else ("PASS" if share >= 90 else "FAIL"),
            "consequence_if_fail": "M1 must fix metrics wiring before any page",
        }
    else:
        rules["1_run_coverage"] = {
            "verdict": "UNMEASURED",
            "why": "generation_runs or applications.source missing",
        }

    # Rule 2 — stage resolution: longest event gap as a share of run wall time.
    if has_runs and has_events:
        runs = conn.execute(
            "SELECT run_id, started_at, finished_at FROM generation_runs "
            "WHERE started_at >= ? AND finished_at IS NOT NULL AND pipeline != 'backfill'",
            (win.start_iso,),
        ).fetchall()
        shares: list[float] = []
        start_events = 0
        refine_events = 0
        for run in runs:
            s, f = _parse_ts(run["started_at"]), _parse_ts(run["finished_at"])
            if not s or not f or f <= s:
                continue
            evs = conn.execute(
                "SELECT ts, event, stage FROM pipeline_events WHERE run_id = ? ORDER BY id",
                (run["run_id"],),
            ).fetchall()
            start_events += sum(1 for e in evs if e["event"] == "start")
            refine_events += sum(1 for e in evs if e["stage"] == "refine")
            points = [s] + [t for t in (_parse_ts(e["ts"]) for e in evs) if t] + [f]
            gaps = [(b - a).total_seconds() for a, b in zip(points, points[1:], strict=False)]
            wall = (f - s).total_seconds()
            if wall > 60:  # a sub-minute run ($0 skip) has no stage question
                shares.append(100.0 * max(gaps) / wall)
        med = round(sorted(shares)[len(shares) // 2], 1) if shares else None
        rules["2_stage_resolution"] = {
            "finished_runs_over_1min": len(shares),
            "median_longest_gap_share_pct": med,
            "runs_with_gap_over_50pct": sum(1 for x in shares if x > 50),
            "start_events_seen": start_events,
            "refine_events_seen": refine_events,
            "threshold": "median <= 50",
            "verdict": "UNMEASURED" if med is None else ("PASS" if med <= 50 else "FAIL"),
            "consequence_if_fail": "M1 adds start events + one event per refine round",
        }
    else:
        rules["2_stage_resolution"] = {"verdict": "UNMEASURED", "why": "metrics tables missing"}

    # Rule 3 — hunt funnel consistency: raw yield vs unique postings_seen.
    sr, ps = hunt.get("source_runs"), hunt.get("postings_seen")
    if sr and ps and sr["found_raw"]:
        unique = ps["passed"] + ps["rejected"]
        gap = _pct(sr["found_raw"] - unique, sr["found_raw"])
        rules["3_hunt_funnel"] = {
            "found_raw": sr["found_raw"],
            "unique_seen": unique,
            "gap_pct": gap,
            "threshold": "<= 30",
            "verdict": "PASS" if gap is not None and gap <= 30 else "FAIL",
            "consequence_if_fail": "M1 adds a hunt_runs table written from hunter/main.py's own counters",
        }
    else:
        rules["3_hunt_funnel"] = {
            "verdict": "UNMEASURED",
            "why": "no source_runs/postings_seen rows in window",
        }
    # Rule 3b — the M1 answer to rule 3: hunt_runs holds the loop's own
    # funnel, so the derived gap above stops mattering once rows exist. Kept
    # as a separate key so the pre-M1 comparison still prints next to it.
    hr = hunt.get("hunt_runs")
    if hr is None:
        rules["3b_hunt_runs_present"] = {
            "verdict": "UNMEASURED",
            "why": hunt.get("hunt_runs_unmeasured") or "hunt_runs table missing",
        }
    else:
        rules["3b_hunt_runs_present"] = {
            "hunts_in_window": hr["hunts"],
            "found": hr["found"],
            "new": hr["new"],
            "queued": hr["queued"],
            "verdict": "PASS" if hr["hunts"] > 0 else "UNMEASURED",
            **({} if hr["hunts"] > 0 else {"why": "table present, no hunt rows in window"}),
        }

    # Rule 4 — ready stack == /unsent minus its non-applied entries.
    ready = int(
        _scalar(
            conn,
            f"SELECT COUNT(*) FROM applications WHERE user_id = ? AND sent = '' AND ats_status "  # noqa: S608 — '?' placeholders only
            f"NOT IN ({','.join('?' for _ in NON_APPLIED_STATUSES)}) AND ats_status != ''",
            (user_id, *NON_APPLIED_STATUSES),
        )
        or 0
    )
    # iter_unsent_rows()'s WHERE, verbatim. It also accepts a DASH in `sent`,
    # which on an APPLIED row means the owner declined it by hand (web-UI
    # "Filter miss"/"Skipped", or an old manual dash) — those are not "ready
    # to send" (M0 on prod, 2026-09-22: 19 vs 24, the 5 were exactly these).
    # The page's "ready" is sent='' only; declined rows get their own count.
    unsent_rows = conn.execute(
        "SELECT ats_status, sent FROM applications WHERE ats_status != 'SKIP' "
        "AND ats_status NOT IN ('PENDING','IN_PROGRESS') AND id != '' "
        "AND (sent = '' OR sent IN ('—', '–', '-')) AND user_id = ?",
        (user_id,),
    ).fetchall()
    unsent_applied_all = [r for r in unsent_rows if _bucket_status(r["ats_status"]) == "APPLIED"]
    declined = sum(1 for r in unsent_applied_all if (r["sent"] or "").strip() != "")
    unsent_applied = len(unsent_applied_all) - declined
    rules["4_ready_stack"] = {
        "ready_by_snapshot": ready,
        "unsent_applied_by_tracker_sql": unsent_applied,
        "declined_by_owner_dash": declined,
        "verdict": "PASS" if ready == unsent_applied else "FAIL",
        "consequence_if_fail": "fix the 'ready' definition before anything else",
    }

    # Rule 5 — leaked open runs.
    if has_runs:
        cutoff = _iso_utc(win.now - timedelta(seconds=APPLY_AGENT_CLI_TIMEOUT_SEC))
        leaked = int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM generation_runs WHERE finished_at IS NULL "
                "AND pipeline != 'backfill' AND started_at < ?",
                (cutoff,),
            )
            or 0
        )
        open_total = int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM generation_runs WHERE finished_at IS NULL AND pipeline != 'backfill'",
            )
            or 0
        )
        # Informational (M1): runs in the window whose outcome was stamped by
        # the parent/sweeper (`orphan:<outcome>` / `orphan:stale`) rather than
        # written by the pipeline itself. Not part of the verdict — a stamped
        # run is a CLOSED run, which is what the rule wants.
        orphan_stamped = int(
            _scalar(
                conn,
                "SELECT COUNT(*) FROM generation_runs WHERE started_at >= ? "
                "AND pipeline != 'backfill' AND outcome LIKE ?",
                (win.start_iso, ORPHAN_PREFIX + "%"),
            )
            or 0
        )
        rules["5_leaked_open_runs"] = {
            "open_runs": open_total,
            "older_than_timeout": leaked,
            "orphan_stamped": orphan_stamped,
            "timeout_sec": APPLY_AGENT_CLI_TIMEOUT_SEC,
            "verdict": "PASS" if leaked == 0 else "FAIL",
            "consequence_if_fail": "M1 stamps orphan runs from apply_worker._resolve_outcome",
        }
        rules["growth"] = {
            "pipeline_events_total": int(_scalar(conn, "SELECT COUNT(*) FROM pipeline_events") or 0)
            if has_events
            else None,
            "pipeline_events_last_7d": int(
                _scalar(
                    conn,
                    "SELECT COUNT(*) FROM pipeline_events WHERE ts >= ?",
                    (_iso_utc(win.now - timedelta(days=7)),),
                )
                or 0
            )
            if has_events
            else None,
            "generation_runs_total": int(
                _scalar(conn, "SELECT COUNT(*) FROM generation_runs") or 0
            ),
        }
    else:
        rules["5_leaked_open_runs"] = {"verdict": "UNMEASURED", "why": "generation_runs missing"}

    return rules


# ── Snapshot ──────────────────────────────────────────────────────────────────


def build_snapshot(
    db_path: Path, *, days: int, user_id: str, failures_log: Path, events_limit: int
) -> dict[str, Any]:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "applications"):
            raise SystemExit(f"{db_path}: no applications table — not a tracker.db")
        win = Window(days)
        hunt = hunt_tier(conn, win, user_id)
        snap = {
            "generated_at": _iso_utc(win.now),
            "window": {
                "label": win.label,
                "days": days,
                "start_utc": win.start_iso,
                "tz": TIMEZONE,
            },
            "user_id": user_id or "(unscoped: empty user_id)",
            "hunt": hunt,
            "apply": apply_tier(conn, win, user_id, failures_log),
            "result": result_tier(conn, win, user_id),
            "events": recent_events(conn, events_limit),
            "coverage": coverage(conn, win, user_id, hunt),
        }
    finally:
        conn.close()
    return snap


# ── Text report ───────────────────────────────────────────────────────────────


def _fmt_pairs(pairs: list[tuple[str, int]], n: int = 6) -> str:
    return " · ".join(f"{k} {v}" for k, v in pairs[:n]) or "—"


def print_report(snap: dict[str, Any]) -> None:
    w = snap["window"]
    print(f"Pipeline snapshot — {w['label']} ({w['tz']}), user {snap['user_id']}")
    print(f"generated {snap['generated_at']}\n")

    h = snap["hunt"]
    print("HUNT")
    hr = h["hunt_runs"]
    if hr is None:
        print(
            f"  hunts              —   ({h.get('hunt_runs_unmeasured') or 'no hunt_runs table'} — funnel below is derived)"
        )
    elif not hr["hunts"]:
        print("  hunts              0   (hunt_runs present, no hunt in window)")
    else:
        last = hr["last"]
        print(
            f"  hunts              {hr['hunts']:>6}   {_fmt_pairs(list(hr['by_trigger'].items()))}; "
            f"last {last['at']} ({last['trigger']}, {', '.join(last['sources'][:3]) or '?'}"
            f"{', …' if len(last['sources']) > 3 else ''}: found {last['found']}, new {last['new']})"
        )
        print(
            f"  funnel             found {hr['found']} → filtered {hr['filtered_out']} → "
            f"dup {hr['dup_url']} url / {hr['dup_ct']} company+title / {hr['dup_cooldown']} cooldown → "
            f"new {hr['new']} → capped {hr['capped']} → queued {hr['queued']}"
            + (f" / inline {hr['applied_inline']}" if hr["applied_inline"] else "")
        )
        print(f"    top reasons      {_fmt_pairs(hr['top_filter_reasons'])}")
    sr = h["source_runs"]
    if sr:
        print(
            f"  found (raw)        {sr['found_raw']:>6}   {sr['runs']} runs, "
            f"{sr['sources_ran']} sources ran, {sr['sources_last_run_ok']} last-run ok, {sr['errors']} errors"
        )
    else:
        print("  found (raw)        —   (no source_runs table)")
    ps = h["postings_seen"]
    if ps:
        print(
            f"  unique seen        {ps['unique_seen']:>6}   new {ps['new_this_window']}, "
            f"passed {ps['passed']}, rejected {ps['rejected']}"
        )
        print(f"    top reasons      {_fmt_pairs(ps['top_reasons'])}")
    else:
        print("  unique seen        —   (no postings_seen table)")
    et = h["entered_tracker"]
    print(f"  entered tracker    {et['rows']:>6}   {_fmt_pairs(list(et['by_status'].items()))}")
    print(f"    by source        {_fmt_pairs(et['by_source'])}")
    ns = h["next_slot"]
    if ns and "error" not in ns:
        print(
            f"  next slot          {ns['at']} ({ns['source']}, in {ns['in_min']} min, {ns['sources_total']} sources)"
        )
    elif ns:
        print(f"  next slot          {ns['error']}")

    a = snap["apply"]
    print(
        "\nAPPLY"
        + (
            ""
            if a["queue_mode_observed"]
            else "   (no row ever claimed — inline mode, or the queue was never used)"
        )
    )
    p = a["pending"]
    wait = f"{p['oldest_wait_min']} min" if p["oldest_wait_min"] is not None else "—"
    print(
        f"  pending            {p['count']:>6}   oldest date {p['oldest_date'] or '—'}, oldest waits {wait}"
    )
    for r in p["head"]:
        w = f"  waiting {r['wait_min']} min" if r["wait_min"] is not None else ""
        print(f"      · {r['company']} — {r['title']}  [{r['source'] or '?'}]{w}")
    ip = a["in_progress"]
    print(f"  in progress        {ip['count']:>6}")
    for c in ip["cards"]:
        stale = "  STALE (> claim timeout)" if c["stale"] else ""
        print(
            f"      · {c['company']} — {c['title']}  [{c['source'] or '?'}] claimed {c['claimed_min_ago']} min ago{stale}"
        )
        run = c["run"]
        if run:
            le = run["last_event"]
            print(
                f"        run {run['pipeline']}/{run['profile']} {run['elapsed_min']} min, {run['events']} events; "
                f"last: {le['stage']} {le['event']} @ {le['at']}"
                if le
                else f"        run {run['pipeline']}/{run['profile']} {run['elapsed_min']} min, no events yet"
            )
            cs = run["current_stage"]
            v = (
                f"verdict {run['verdict_first']} → {run['verdict_final']}"
                if run["verdict_first"] is not None
                else "no verdict yet"
            )
            since = (
                f", for {run['stage_started_min_ago']} min"
                if run["stage_started_min_ago"] is not None
                else ""
            )
            print(
                f"        now: {cs['stage']} ({cs['basis']}{since}); {v}; refine {run['refine_rounds'] or 0} rounds"
            )
            rp = run["refine_progress"]
            if rp:
                print(
                    f"        refine: round {rp['round']} {rp['kind'] or '?'} {rp['outcome']} @ {rp['at']}, "
                    f"score {rp['score']}, best {rp['best']}"
                )
        else:
            print("        no open generation_runs row for this url (metrics gap or pre-M1 DB)")
    rn = a["runs"]
    if rn:
        print(f"  runs started       {rn['started']:>6}   {_fmt_pairs(rn['outcomes'], 10)}")
        print(
            f"  cut at $0          {rn['cut_zero_cost_total']:>6}   {_fmt_pairs(list(rn['cut_zero_cost'].items()))}"
        )
    else:
        print("  runs started       —   (no generation_runs table)")
    sk = a["skipped_rows"]
    if sk:
        print(f"  SKIP/EXPIRED rows  {sk['count']:>6}   {_fmt_pairs(sk['by_reason'])}")
    f = a["failures"]
    nr = f["next_retry"]
    print(
        f"  FAIL rows          {f['in_window']:>6}   all-time: retryable {f['retryable_total']}, gave up {f['gave_up_total']}"
        + (f", next retry {nr['at']} (in {nr['in_min']} min)" if nr else "")
    )
    lr = f["log_records"]
    if lr:
        print(f"    failures.jsonl   {lr['in_window']:>6}   {_fmt_pairs(lr['by_outcome'])}")
    lo = a["llm_outage"]
    print(
        f"  LLM outage         {'PAUSED, ' + str(lo['remaining_min']) + ' min left' if lo['paused'] else 'none'}"
    )

    r = snap["result"]
    print("\nRESULT")
    rd = r["ready"]
    print(
        f"  ready (unsent)     {rd['count']:>6}   produced in window {rd['produced_in_window']}, mean verdict {rd['mean_verdict'] if rd['mean_verdict'] is not None else '—'}"
    )
    print(f"  sent               {r['sent_in_window']:>6}")
    print(
        f"  outcomes           {sum(v for _, v in r['outcomes_in_window']):>6}   {_fmt_pairs(r['outcomes_in_window'])}"
    )
    c = r["cost"]
    print(
        f"  LLM spend          ${c['total_usd']:>5}   {c['priced_rows']} priced rows"
        + (f" (${c['per_priced_row_usd']} each)" if c["per_priced_row_usd"] is not None else "")
        + (f", {c['unpriced_rows']} unpriced (CLI-served / no cost)" if c["unpriced_rows"] else "")
    )

    ev = snap["events"]
    print("\nEVENTS (newest first)")
    if ev is None:
        print("  (no pipeline_events table)")
    elif not ev:
        print("  (none)")
    for e in ev or []:
        dur = f" {e['duration_ms'] // 1000}s" if e["duration_ms"] else ""
        print(
            f"  {e['at']}  {e['stage']:<10} {e['event']:<8}{dur:>6}  {e['company'][:28]:<28} {e['payload']}"
        )

    print("\nCOVERAGE — docs/PIPELINE_VIZ_PLAN.md M0 decision rules")
    cov = snap["coverage"]
    for key in (
        "1_run_coverage",
        "2_stage_resolution",
        "3_hunt_funnel",
        "3b_hunt_runs_present",
        "4_ready_stack",
        "5_leaked_open_runs",
    ):
        rule = cov.get(key, {})
        verdict = rule.get("verdict", "UNMEASURED")
        detail = {
            k: v
            for k, v in rule.items()
            if k not in ("verdict", "consequence_if_fail", "threshold")
        }
        thr = f" (rule: {rule['threshold']})" if rule.get("threshold") else ""
        print(f"  [{verdict:>10}] {key}{thr}")
        print(f"               {json.dumps(detail, ensure_ascii=False)}")
        if verdict == "FAIL" and rule.get("consequence_if_fail"):
            print(f"               → {rule['consequence_if_fail']}")
    g = cov.get("growth")
    if g:
        print(f"  growth: {json.dumps(g)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", default=DEFAULT_DB, help="tracker.db path (opened read-only)")
    ap.add_argument("--days", type=int, default=1, help="calendar-day window, 1 = today (default)")
    ap.add_argument(
        "--user", default=DEFAULT_USER, help="user_id to scope applications rows (default: config)"
    )
    ap.add_argument(
        "--failures-log",
        default=str(PROJECT_DIR / "logs" / "apply_failures.jsonl"),
        help="apply_failures.jsonl path",
    )
    ap.add_argument("--events", type=int, default=15, help="how many recent events to show")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: {db_path} not found", file=sys.stderr)
        return 1
    if args.days < 1:
        print("ERROR: --days must be >= 1", file=sys.stderr)
        return 1

    snap = build_snapshot(
        db_path,
        days=args.days,
        user_id=args.user or "",
        failures_log=Path(args.failures_log),
        events_limit=max(0, args.events),
    )
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
    else:
        print_report(snap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
