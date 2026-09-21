"""
tools/fail_signatures.py — which apply failures are really ONE defect?

docs/APPLY_FAILURE_QUEUES_PLAN.md M0. Groups every record in
`logs/apply_failures.jsonl` (plus its rotated `.1`..`.5` siblings) by a
normalised error signature (`hunter.failure_signature.signature`, the same
function the M2 classifier will use), then reports per signature: records,
DISTINCT vacancies, first/last seen, the peak number of distinct vacancies in
any 6-hour window, cli_mode share, exit codes, and source-domain spread.

With `--db`, joins the affected URLs against tracker.db (opened read-only)
and reports what became of them: still FAIL and retryable, FAIL and given up
(fail_count >= MAX_FAIL_RETRIES, i.e. out of the retry loop forever), queued,
applied, skipped/expired. For the 2026-09-10 CLI-argv incident that is the
damage count.

Then prints the plan's four decision rules with the numbers filled in.

Read-only, zero LLM calls, zero network, zero writes.

Usage:
    docker compose exec -T job-hunter python tools/fail_signatures.py --db tracker.db
    docker compose exec -T job-hunter python tools/fail_signatures.py --db tracker.db --days 30
    docker compose exec -T job-hunter python tools/fail_signatures.py --db tracker.db --json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Force UTF-8 output on Windows (console defaults to cp1252 -> emoji crash).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter.failure_signature import UNINFORMATIVE, signature, signature_id  # noqa: E402

# Decision-rule thresholds, fixed BEFORE the run (docs/APPLY_FAILURE_QUEUES_PLAN.md M0).
RULE1_MIN_DISTINCT = 5
PEAK_WINDOW = timedelta(hours=6)
RULE1_MIN_PEAK = 3
RULE3_SOURCE_SCOPED_SHARE = 0.8
RULE4_MIN_SPAN_DAYS = 7
_ROTATED_BACKUPS = 5

# The known 2026-09-10 incident (argv prompt swallowed by --disallowedTools).
# Rule 1 is evaluated WITHOUT it — the question is whether systemic failures
# recur beyond the one we already know about.
INCIDENT_RE = re.compile(r"matches no known tool", re.IGNORECASE)

_QUEUED_STATUSES = {"PENDING", "IN_PROGRESS"}
_OTHER_TERMINAL = {"SKIP": "skipped", "EXPIRED": "expired", "MANUAL": "manual"}


@dataclass
class FailRecord:
    ts: datetime
    url: str
    url_norm: str
    outcome: str
    exit_code: int | None
    cli_mode: bool
    signature: str


@dataclass
class SignatureGroup:
    signature: str
    records: list[FailRecord] = field(default_factory=list)

    @property
    def sig_id(self) -> str:
        return signature_id(self.signature)

    @property
    def distinct_urls(self) -> set[str]:
        return {r.url_norm for r in self.records if r.url_norm}

    @property
    def is_incident(self) -> bool:
        return bool(INCIDENT_RE.search(self.signature))

    def peak_distinct(self, window: timedelta = PEAK_WINDOW) -> int:
        """Max distinct vacancies whose failures fall inside any `window`."""
        recs = sorted((r for r in self.records if r.url_norm), key=lambda r: r.ts)
        best = 0
        left = 0
        in_window: Counter[str] = Counter()
        for rec in recs:
            in_window[rec.url_norm] += 1
            while rec.ts - recs[left].ts > window:
                old = recs[left].url_norm
                in_window[old] -= 1
                if not in_window[old]:
                    del in_window[old]
                left += 1
            best = max(best, len(in_window))
        return best

    def domains(self) -> Counter[str]:
        """Distinct vacancies per source domain."""
        out: Counter[str] = Counter()
        for url in self.distinct_urls:
            host = (urlparse(url).hostname or "").lower()
            out[host.removeprefix("www.") or "(no host)"] += 1
        return out

    def top_domain_share(self) -> float:
        doms = self.domains()
        total = sum(doms.values())
        return (doms.most_common(1)[0][1] / total) if total else 0.0


# ── Loading ──────────────────────────────────────────────────────────────────


def _normalize_url(url: str) -> str:
    try:
        from hunter.tracker import normalize_url

        return normalize_url(url)
    except Exception:  # noqa: BLE001 — a report must not die on an import problem
        return url.strip().rstrip("/").lower()


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def log_files(base: Path) -> list[Path]:
    """The live log plus its RotatingFileHandler backups, oldest first."""
    candidates = [base.with_name(f"{base.name}.{i}") for i in range(_ROTATED_BACKUPS, 0, -1)]
    candidates.append(base)
    return [p for p in candidates if p.is_file()]


def load_records(paths: list[Path], since: datetime | None = None) -> tuple[list[FailRecord], int]:
    """Parse every JSONL line; returns (records, unparseable_line_count)."""
    records: list[FailRecord] = []
    bad = 0
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            ts = _parse_ts(raw.get("ts"))
            if ts is None:
                bad += 1
                continue
            if since is not None and ts < since:
                continue
            url = str(raw.get("url") or "")
            records.append(
                FailRecord(
                    ts=ts,
                    url=url,
                    url_norm=_normalize_url(url) if url else "",
                    outcome=str(raw.get("outcome") or ""),
                    exit_code=raw.get("exit_code"),
                    cli_mode=bool(raw.get("cli_mode")),
                    signature=signature(raw.get("error")),
                )
            )
    records.sort(key=lambda r: r.ts)
    return records, bad


def group_records(records: list[FailRecord]) -> list[SignatureGroup]:
    groups: dict[str, SignatureGroup] = {}
    for rec in records:
        groups.setdefault(rec.signature, SignatureGroup(rec.signature)).records.append(rec)
    return sorted(groups.values(), key=lambda g: (-len(g.distinct_urls), -len(g.records)))


# ── tracker.db join (read-only) ──────────────────────────────────────────────


def _max_fail_retries() -> int:
    try:
        from hunter.tracker import MAX_FAIL_RETRIES

        return int(MAX_FAIL_RETRIES)
    except Exception:  # noqa: BLE001
        return 3


def tracker_states(db_path: Path, url_norms: set[str]) -> dict[str, str]:
    """url_norm -> fate in tracker.db. Read-only URI connection; never writes."""
    if not url_norms or not db_path.is_file():
        return {}
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
        if not cols:
            return {}
        fail_col = "fail_count" if "fail_count" in cols else "0"
        max_retries = _max_fail_retries()
        rows: list[tuple[str, str, int]] = []
        norms = sorted(url_norms)
        for i in range(0, len(norms), 500):
            chunk = norms[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows.extend(
                conn.execute(
                    f"SELECT url_norm, COALESCE(ats_status,''), COALESCE({fail_col},0) "  # noqa: S608 — column name from PRAGMA, values bound
                    f"FROM applications WHERE url_norm IN ({placeholders})",
                    chunk,
                ).fetchall()
            )
    finally:
        conn.close()

    # Several rows per url_norm are possible (multi-user); the best fate wins.
    rank = {
        "applied": 6,
        "queued": 5,
        "fail_retryable": 4,
        "given_up": 3,
        "skipped": 2,
        "expired": 2,
        "manual": 2,
    }
    states: dict[str, str] = {}
    for url_norm, status, fail_count in rows:
        status = str(status).strip().upper()
        if status == "FAIL":
            fate = "given_up" if int(fail_count or 0) >= max_retries else "fail_retryable"
        elif status in _QUEUED_STATUSES:
            fate = "queued"
        elif status in _OTHER_TERMINAL:
            fate = _OTHER_TERMINAL[status]
        else:
            fate = "applied"
        prev = states.get(url_norm)
        if prev is None or rank[fate] > rank[prev]:
            states[url_norm] = fate
    return states


# ── Decision rules ───────────────────────────────────────────────────────────


def evaluate(groups: list[SignatureGroup], records: list[FailRecord]) -> dict[str, Any]:
    span_days = (records[-1].ts - records[0].ts).total_seconds() / 86400 if records else 0.0
    uninformative = sum(1 for r in records if r.signature == UNINFORMATIVE)

    recurring = [
        g
        for g in groups
        if not g.is_incident
        and g.signature != UNINFORMATIVE
        and len(g.distinct_urls) >= RULE1_MIN_DISTINCT
        and g.peak_distinct() >= RULE1_MIN_PEAK
    ]
    over_threshold = [
        g for g in groups if g.signature != UNINFORMATIVE and g.peak_distinct() >= RULE1_MIN_PEAK
    ]
    source_scoped = [g for g in recurring if g.top_domain_share() >= RULE3_SOURCE_SCOPED_SHARE]

    if span_days < RULE4_MIN_SPAN_DAYS:
        verdict = (
            f"UNMEASURABLE — the log covers {span_days:.1f} days (< {RULE4_MIN_SPAN_DAYS}). "
            "Fall back to logs/apply_stdout/ transcripts before deciding (rule 4)."
        )
    elif recurring:
        verdict = (
            f"BUILD M2+M3 — {len(recurring)} non-incident signature(s) with >= "
            f"{RULE1_MIN_DISTINCT} distinct vacancies and a 6h peak >= {RULE1_MIN_PEAK} (rule 1)."
        )
    else:
        verdict = (
            "SHIP M1 + M4 ONLY — no recurring systemic signature beyond the known "
            "2026-09-10 incident (rule 1). Close M2/M3."
        )
    return {
        "span_days": round(span_days, 2),
        "records": len(records),
        "uninformative": uninformative,
        "uninformative_share": round(uninformative / len(records), 3) if records else 0.0,
        "rule1_recurring": [g.sig_id for g in recurring],
        "rule2_over_threshold": [g.sig_id for g in over_threshold],
        "rule3_source_scoped": [g.sig_id for g in source_scoped],
        "verdict": verdict,
    }


# ── Output ───────────────────────────────────────────────────────────────────


def group_to_dict(g: SignatureGroup, states: dict[str, str]) -> dict[str, Any]:
    fates = Counter(states.get(u, "absent") for u in g.distinct_urls) if states else Counter()
    return {
        "sig_id": g.sig_id,
        "signature": g.signature,
        "incident_2026_09_10": g.is_incident,
        "records": len(g.records),
        "distinct_vacancies": len(g.distinct_urls),
        "peak_distinct_6h": g.peak_distinct(),
        "first_seen": g.records[0].ts.strftime("%Y-%m-%d %H:%M"),
        "last_seen": g.records[-1].ts.strftime("%Y-%m-%d %H:%M"),
        "outcomes": dict(Counter(r.outcome for r in g.records)),
        "exit_codes": dict(Counter(str(r.exit_code) for r in g.records)),
        "cli_mode_share": round(sum(r.cli_mode for r in g.records) / len(g.records), 2),
        "top_domains": dict(g.domains().most_common(3)),
        "top_domain_share": round(g.top_domain_share(), 2),
        "tracker_fates": dict(fates),
        "example_url": next((r.url for r in g.records if r.url), ""),
    }


def render_text(
    groups: list[dict[str, Any]], summary: dict[str, Any], files: list[Path], bad: int
) -> str:
    out: list[str] = []
    out.append("Apply failure signatures — docs/APPLY_FAILURE_QUEUES_PLAN.md M0")
    out.append(f"Files: {', '.join(str(p) for p in files) or '(none found)'}")
    out.append(
        f"Records: {summary['records']} over {summary['span_days']} days"
        f" · unparseable lines: {bad}"
        f" · uninformative: {summary['uninformative']} ({summary['uninformative_share']:.0%})"
    )
    out.append("")
    for g in groups:
        tag = "  [2026-09-10 incident]" if g["incident_2026_09_10"] else ""
        out.append(f"[{g['sig_id']}] {g['signature']}{tag}")
        out.append(
            f"    vacancies {g['distinct_vacancies']} (records {g['records']})"
            f" · 6h peak {g['peak_distinct_6h']}"
            f" · {g['first_seen']} → {g['last_seen']}"
            f" · cli {g['cli_mode_share']:.0%}"
        )
        out.append(
            f"    outcomes {g['outcomes']} · exit {g['exit_codes']}"
            f" · domains {g['top_domains']} (top share {g['top_domain_share']:.0%})"
        )
        if g["tracker_fates"]:
            out.append(f"    tracker now: {g['tracker_fates']}")
        if g["example_url"]:
            out.append(f"    e.g. {g['example_url']}")
    out.append("")
    out.append("Decision rules (fixed before the run):")
    out.append(
        f"  1. recurring systemic signatures (excl. the incident): "
        f"{summary['rule1_recurring'] or 'none'}"
    )
    out.append(
        f"  2. signatures over the {RULE1_MIN_PEAK}-in-6h threshold — LABEL EACH BY HAND "
        f"(system/vacancy); any vacancy-class one raises the M2 threshold: "
        f"{summary['rule2_over_threshold'] or 'none'}"
    )
    out.append(
        f"  3. of rule-1 signatures, source-scoped (top domain >= "
        f"{RULE3_SOURCE_SCOPED_SHARE:.0%}): {summary['rule3_source_scoped'] or 'none'}"
    )
    out.append(f"  4. window span: {summary['span_days']} days (need >= {RULE4_MIN_SPAN_DAYS})")
    out.append("")
    out.append(f"VERDICT: {summary['verdict']}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--log",
        type=Path,
        default=PROJECT_DIR / "logs" / "apply_failures.jsonl",
        help="live apply_failures.jsonl; rotated .1-.5 siblings are read too",
    )
    parser.add_argument("--db", type=Path, help="tracker.db to join against (read-only)")
    parser.add_argument("--days", type=int, help="only records from the last N days")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    since = datetime.now(timezone.utc) - timedelta(days=args.days) if args.days else None
    files = log_files(args.log)
    records, bad = load_records(files, since)
    groups = group_records(records)

    states: dict[str, str] = {}
    if args.db:
        all_urls: set[str] = set()
        for g in groups:
            all_urls |= g.distinct_urls
        states = tracker_states(args.db, all_urls)

    summary = evaluate(groups, records)
    group_dicts = [group_to_dict(g, states) for g in groups]

    if args.json:
        print(
            json.dumps(
                {
                    "files": [str(p) for p in files],
                    "unparseable": bad,
                    "summary": summary,
                    "signatures": group_dicts,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render_text(group_dicts, summary, files, bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
