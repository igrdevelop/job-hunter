"""
tools/funnel_sources.py — which sources actually feed the funnel?

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M0.2. Reuses
`hunter.funnel.compute_funnel(days)` for the tracked/generated/sent/
confirmed/answered counts (so the per-source definitions can never drift
from the real /funnel command), then adds what compute_funnel doesn't carry:
a Wilson 90% confidence interval on the sent-rate, per-source LLM spend
(`sum(cost_usd)/sent`; a CLI-mode sent row has no cost_usd and is reported
separately as "unpriced"), FAIL/SKIP counts, and scraper-liveness status
from `hunter.source_health.health_report()`.

Then applies the plan's decision table:
  - tracked >= 30 in the window and sent == 0                -> "ballast"
    (recommend the *_ENABLED toggle off; prints up to 10 filtered URLs so
    the owner can eyeball them first)
  - health status in {BROKEN?, ERROR} for >= 14 days         -> "broken"
    (fix the scraper or disable it)
  - sent >= 5 and answered == 0 with a >= 21-day window       -> "watch"
    (n too small to act on; flag, don't disable)

Read-only, zero LLM calls, zero writes.

Usage:
    docker compose exec -T job-hunter python tools/funnel_sources.py --days 90
    docker compose exec -T job-hunter python tools/funnel_sources.py --days 90 --json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Force UTF-8 output on Windows (console defaults to cp1252 -> emoji crash).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

_MIN_TRACKED_FOR_BALLAST = 30
_MIN_SENT_FOR_WATCH = 5
_MIN_WINDOW_DAYS_FOR_WATCH = 21
_MIN_BROKEN_DAYS = 14
_BROKEN_STATUSES = ("BROKEN?", "ERROR")


# ── Wilson score interval (pure) ────────────────────────────────────────────


def wilson_ci(successes: int, total: int, z: float = 1.645) -> tuple[float, float]:
    """Wilson score interval for a proportion, default z=1.645 (90% two-sided).

    `successes` is clamped into [0, total] before the interval math. On this
    repo's real data the sent-rate call site legitimately passes more
    successes than trials: `generated` counts rows whose `ats_status` holds a
    numeric score, `sent` counts rows whose Sent column is not a non-sent
    marker, and those are independent columns — a row added by hand through
    the Sheet (`tracker.insert_pulled_rows`) is sent without ever carrying a
    score. Without the clamp `phat > 1` makes `phat * (1 - phat)` negative and
    the square root below raises `ValueError: math domain error`, which is
    exactly how this tool died on its first real run (2026-09-12, prod).
    A ratio above 1 is not a proportion, so there is no meaningful interval
    for it; the caller reports the raw counts and flags the row instead.
    """
    if total <= 0:
        return (0.0, 0.0)
    successes = min(max(successes, 0), total)
    phat = successes / total
    denom = 1 + z * z / total
    center = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    lo = max(0.0, (center - margin) / denom)
    hi = min(1.0, (center + margin) / denom)
    return (lo, hi)


# ── Extra per-source aggregation (pure over pre-fetched rows) ──────────────


@dataclass
class SourceExtra:
    fail: int = 0
    skip: int = 0
    cost_total: float = 0.0
    cost_priced_sent: int = 0
    cost_unpriced_sent: int = 0
    filtered_urls: list[str] = field(default_factory=list)

    @property
    def cost_per_sent(self) -> float | None:
        return (self.cost_total / self.cost_priced_sent) if self.cost_priced_sent else None


def aggregate_extra(rows: list[dict], days: int | None = None) -> dict[str, SourceExtra]:
    """rows: dicts with date/url/ats_status/sent/cost_usd. Applies the SAME
    date-window filter as hunter.funnel.compute_funnel so this and the
    reused funnel counts describe the identical row set."""
    from hunter.funnel import _cutoff, _is_sent, source_for_url

    cutoff = _cutoff(days)
    out: dict[str, SourceExtra] = {}
    for r in rows:
        d = (r.get("date") or "").strip()
        if cutoff is not None and (not re.match(r"^\d{4}-\d{2}-\d{2}", d) or d < cutoff):
            continue

        src = source_for_url(r.get("url") or "")
        extra = out.setdefault(src, SourceExtra())
        status = (r.get("ats_status") or "").strip().upper()
        if status == "FAIL":
            extra.fail += 1
        elif status == "SKIP":
            extra.skip += 1
            if len(extra.filtered_urls) < 10:
                extra.filtered_urls.append(r.get("url") or "")

        if _is_sent(r.get("sent") or ""):
            cost = r.get("cost_usd")
            if cost is not None:
                extra.cost_total += float(cost)
                extra.cost_priced_sent += 1
            else:
                extra.cost_unpriced_sent += 1
    return out


def fetch_rows(conn) -> list[dict]:
    cur = conn.execute("SELECT date, url, ats_status, sent, cost_usd FROM applications")
    return [dict(r) for r in cur.fetchall()]


# ── Decision table (pure) ───────────────────────────────────────────────────


def broken_since_days(source: str, zero_streak: int) -> int | None:
    """Best-effort: how many days ago did the current zero/error streak
    start? None when it can't be determined (no runs, unparsable ts)."""
    if zero_streak <= 0:
        return None
    from hunter.source_health import recent_runs

    runs = recent_runs(source, limit=zero_streak)
    if not runs:
        return None
    oldest = runs[-1]  # recent_runs is newest-first; the streak's oldest run
    try:
        ts = datetime.fromisoformat(oldest.ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).days
    except (ValueError, TypeError):
        return None


def decide(
    *,
    tracked: int,
    sent: int,
    answered: int,
    outcome_recorded: int = 0,
    health_status: str | None,
    health_source: str | None,
    zero_streak: int,
    days: int | None,
) -> str:
    if tracked >= _MIN_TRACKED_FOR_BALLAST and sent == 0:
        return "ballast"
    if health_status in _BROKEN_STATUSES:
        broken_days = broken_since_days(health_source or "", zero_streak) if health_source else None
        if broken_days is not None and broken_days >= _MIN_BROKEN_DAYS:
            return "broken"
        return "watch-broken"
    # "No replies" and "nobody recorded anything" read identically as
    # answered == 0. The first prod run (2026-09-12) put 20 sources on "watch"
    # while not a single outcome had ever been written — every one of those
    # verdicts was noise. With no outcome data for the source there is nothing
    # to judge, so say that instead of implying a missing reply.
    if sent >= _MIN_SENT_FOR_WATCH and outcome_recorded == 0:
        return "unmeasured"
    if sent >= _MIN_SENT_FOR_WATCH and answered == 0 and (days or 0) >= _MIN_WINDOW_DAYS_FOR_WATCH:
        return "watch"
    return "ok"


# ── Report ────────────────────────────────────────────────────────────────────


def build_report(days: int | None, db_path: Path | None = None) -> dict[str, Any]:
    import hunter.funnel as funnel_mod
    import hunter.source_health as source_health_mod
    from hunter.config import TRACKER_DB_PATH
    from hunter.db import get_db

    resolved_db = db_path or TRACKER_DB_PATH
    # hunter.funnel / hunter.source_health each own their own module-level
    # DB_PATH constant rather than taking a path argument (mirrors
    # hunter.tracker.DB_PATH). Normally both already resolve to
    # TRACKER_DB_PATH; temporarily repointing them here is what lets --db
    # (a downloaded VPS snapshot, or a test's isolated tmp DB) describe the
    # SAME database across compute_funnel/health_report/our own query.
    prev_funnel_db, prev_health_db = funnel_mod.DB_PATH, source_health_mod.DB_PATH
    funnel_mod.DB_PATH = resolved_db
    source_health_mod.DB_PATH = resolved_db
    try:
        funnel_report = funnel_mod.compute_funnel(days)
        health_rows = source_health_mod.health_report()

        with get_db(resolved_db) as conn:
            raw_rows = fetch_rows(conn)
        extra_by_source = aggregate_extra(raw_rows, days=days)

        health_by_source = {h.source: h for h in health_rows}

        sources: dict[str, Any] = {}
        all_names = set(funnel_report.by_source) | set(extra_by_source)
        for name in all_names:
            counts = funnel_report.by_source.get(name)
            extra = extra_by_source.get(name, SourceExtra())
            health = health_by_source.get(name)

            tracked = counts.tracked if counts else 0
            generated = counts.generated if counts else 0
            sent = counts.sent if counts else 0
            confirmed = counts.confirmed if counts else 0
            answered = counts.answered if counts else 0
            outcome_recorded = counts.outcome_recorded if counts else 0

            ci_lo, ci_hi = wilson_ci(sent, generated)
            # decide() -> broken_since_days() -> source_health.recent_runs()
            # still reads source_health_mod.DB_PATH, so this must run inside
            # the same monkeypatched window as compute_funnel/health_report.
            decision = decide(
                tracked=tracked,
                sent=sent,
                answered=answered,
                outcome_recorded=outcome_recorded,
                health_status=health.status if health else None,
                health_source=name,
                zero_streak=health.zero_streak if health else 0,
                days=days,
            )

            sources[name] = {
                "tracked": tracked,
                "generated": generated,
                "sent": sent,
                "confirmed": confirmed,
                "answered": answered,
                "outcome_recorded": outcome_recorded,
                "sent_rate_ci90": {"low": ci_lo, "high": ci_hi},
                # More sent than generated means hand-made applications for
                # this source (a Sheet row with a Sent date and no score).
                # The interval above is computed on the clamped value, so the
                # flag is what keeps the reader from trusting it as a rate.
                "sent_exceeds_generated": sent > generated,
                "cost_per_sent": extra.cost_per_sent,
                "cost_priced_sent": extra.cost_priced_sent,
                "cost_unpriced_sent": extra.cost_unpriced_sent,
                "fail": extra.fail,
                "skip": extra.skip,
                "health_status": health.status if health else "NODATA",
                "decision": decision,
                "filtered_urls_sample": extra.filtered_urls if decision == "ballast" else [],
            }
    finally:
        funnel_mod.DB_PATH = prev_funnel_db
        source_health_mod.DB_PATH = prev_health_db

    return {"days": days, "sources": sources}


DECISION_RULE = """
Decision rule (docs/improvement-2026-09/08-DATA_EVAL_PLAN.md, M0.2):
  - tracked >= 30 in the window and sent == 0  -> "ballast": turn off the
    source's *_ENABLED toggle, but eyeball the printed filtered URLs first.
  - health status in {BROKEN?, ERROR} for >= 14 days -> "broken": fix the
    scraper or disable it.
  - sent >= 5 and NO outcome recorded (the `out` column is 0) -> "unmeasured":
    there is nothing to judge yet — record outcomes with /outcome first.
  - sent >= 5 and answered == 0 with a >= 21-day window -> "watch": n is too
    small to act on yet, flag it, don't disable it.
""".strip()


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"[funnel_sources] window: last {report['days']} day(s)"
        if report["days"]
        else "[funnel_sources] window: all time",
        "",
    ]
    header = (
        f"{'Source':<22} {'trk':>4} {'gen':>4} {'sent':>4} {'conf':>4} {'ans':>4} {'out':>4} "
        f"{'sent%CI90':>16} {'$/sent':>8} {'fail':>4} {'skip':>4} {'health':<8} decision"
    )
    lines.append(header)
    lines.append("-" * len(header))

    items = sorted(
        report["sources"].items(), key=lambda kv: (-kv[1]["tracked"], -kv[1]["sent"], kv[0])
    )
    for name, s in items:
        ci = s["sent_rate_ci90"]
        ci_text = f"[{ci['low']:.0%},{ci['high']:.0%}]"
        cost_text = f"{s['cost_per_sent']:.2f}" if s["cost_per_sent"] is not None else "—"
        lines.append(
            f"{name:<22} {s['tracked']:>4} {s['generated']:>4} {s['sent']:>4} "
            f"{s['confirmed']:>4} {s['answered']:>4} {s.get('outcome_recorded', 0):>4} "
            f"{ci_text:>16} {cost_text:>8} "
            f"{s['fail']:>4} {s['skip']:>4} {s['health_status']:<8} {s['decision']}"
        )
        if s.get("sent_exceeds_generated"):
            lines.append(
                f"{'':<22}   (sent {s['sent']} > generated {s['generated']} — rows sent by "
                f"hand, no score; the CI above is not a rate)"
            )
        if s["cost_unpriced_sent"]:
            lines.append(f"{'':<22}   ({s['cost_unpriced_sent']} sent row(s) unpriced — CLI mode)")

    lines.append("")
    lines.append(DECISION_RULE)

    ballast = {n: s for n, s in report["sources"].items() if s["decision"] == "ballast"}
    if ballast:
        lines.append("")
        lines.append("Ballast sources — sample filtered URLs to eyeball before disabling:")
        for name, s in ballast.items():
            lines.append(f"  {name}:")
            for url in s["filtered_urls_sample"]:
                lines.append(f"    - {url}")
            if not s["filtered_urls_sample"]:
                lines.append("    (no SKIP rows sampled in this window)")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--days", type=int, default=90, help="window in days (default 90)")
    parser.add_argument("--db", type=Path, default=None, help="tracker.db path override")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    report = build_report(args.days, db_path=args.db)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
