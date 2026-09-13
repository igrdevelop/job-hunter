"""
tools/market_memory_m0.py — what does one hunt sweep actually SEE?

docs/MARKET_MEMORY_PLAN.md M0.a. Runs every enabled source's ``search()``
once, sequentially, exactly like ``hunter/main.py``'s Step 1 (each source in
its own try/except, so one broken scraper never kills the probe), then
reports — per source and in total — what the listings carry BEFORE the plan
builds a ``postings_seen`` table to keep it:

  - raw jobs, unique ``url_norm`` (hunter.tracker.normalize_url), how many
    are already known to tracker.db (``get_known_urls``), and ``new`` =
    unique - known — i.e. how many NEW listings a single sweep sees;
  - salary: share with a non-empty ``salary``, share that
    ``hunter.salary_parse.parse_salary`` reads (min or max + currency), and
    the currency / contract split of the parsed ones;
  - location: share that ``hunter.location_parse.classify_location`` puts
    into remote / hybrid / onsite (not "unknown"), the mode split, and the
    share that carries a recognised city;
  - the filter verdict distribution (``hunter.filters.classify_job`` reason
    or "passed"), against the same profile the hunt loop loads
    (``hunter.filter_profile.load_profile()``);
  - whether the listing payload (``job.raw``) carries a skills list under
    any of the known per-source keys (JustJoin ``requiredSkills`` /
    ``niceToHaveSkills`` / ``skills``, NoFluffJobs ``technology`` /
    ``requirements``, theprotocol/pracuj ``technologies``, Bulldogjob
    ``technologyTags`` / ``mainTechnology``, SolidJobs ``technology``,
    4dayweek ``stack`` / ``tools``, Himalayas ``categories`` /
    ``parentCategories``, plus the generic ``tags`` / ``keywords`` /
    ``mustHaveSkills``), and WHICH keys each source really has.

Then prints the plan's decision rules with the numbers filled in — PASS /
FAIL / UNMEASURED with the value and the threshold:

  1. new listings per full sweep, all sources          >= 30   else close M1
  2. salary_parsed / raw                                >= 25%  else drop the
     salary column set + the pay section of the digest
  3. location_classified / raw                          >= 60%  else keep
     location_raw only, remote_mode best-effort
  4. expected inserts/day (M0.b x new-share)            <= 2000 — CANNOT be
     evaluated here: M0.b is one SQL query over the prod ``source_runs``
     table; the query and the formula are printed verbatim instead.

The script computes; it decides nothing. Exit 0 whenever it ran, 2 only on a
usage error.

Read-only: NO DB writes, NO LLM, NO Telegram. Network only (``--from-dump``
skips even that).

NOTE on the plan text: the plan sketches ``--offline`` over
``Applications/**/content.json``. That corpus carries no ``salary`` and no
listing ``location``, so it cannot feed these metrics — ``--dump`` /
``--from-dump`` replace it: a live run on the deploy host is dumped to JSON
(title / company / location / salary / url / non-empty raw KEYS — never the
raw payload, which can be large and holds nothing the probe needs) and
re-analysed offline with the same ``summarise``. The one thing a dump cannot
reproduce is the ``react_no_angular`` verdict for jobs whose React signal
lives only in ``raw`` skills (``filters._is_react_without_angular``) — the
live verdict is stored per job in the dump for that reason.

Usage:
    docker compose exec -T job-hunter python tools/market_memory_m0.py
    docker compose exec -T job-hunter python tools/market_memory_m0.py --json
    python tools/market_memory_m0.py --sources justjoin,nofluffjobs --dump probe.json
    python tools/market_memory_m0.py --from-dump probe.json --unparsed-salaries 40
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Force UTF-8 output on Windows (console defaults to cp1252 -> emoji crash).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter.filters import classify_job  # noqa: E402
from hunter.location_parse import UNKNOWN, classify_location  # noqa: E402
from hunter.models import Job  # noqa: E402
from hunter.salary_parse import parse_salary  # noqa: E402
from hunter.tracker import normalize_url  # noqa: E402

# ── Decision thresholds (docs/MARKET_MEMORY_PLAN.md "Decision rules") ───────

MIN_NEW_PER_SWEEP = 30
MIN_SALARY_PARSED_SHARE = 0.25
MIN_LOCATION_CLASSIFIED_SHARE = 0.60
MAX_INSERTS_PER_DAY = 2000

# The M0.b half of the plan — one query over the prod DB, printed verbatim.
M0B_SQL = """\
-- raw listings per day, last 14 days, all sources (ring buffer permitting)
SELECT substr(ts,1,10) AS day, SUM(yield) AS raw_listings, COUNT(*) AS runs
FROM source_runs WHERE ts >= date('now','-14 days') GROUP BY day ORDER BY day;"""
M0B_HOWTO = 'docker compose exec job-hunter sqlite3 /app/db/tracker.db "<query>"'
M0B_FORMULA = "expected_inserts_per_day = raw_listings_per_day(M0.b) x new/raw(this probe)"

# Listing-payload keys that carry a skills / technology list, per source shape
# (checked against each source's ``raw=`` payload and against the keys
# ``hunter.filters._is_react_without_angular`` already reads):
#   JustJoin      requiredSkills / niceToHaveSkills (+ the old flat ``skills``)
#   NoFluffJobs   technology (str); requirements (detail API only)
#   theprotocol   technologies
#   pracuj        technologies / expectedTechnologies (detail only)
#   Bulldogjob    technologyTags / mainTechnology
#   SolidJobs     technology (list of {name})
#   4dayweek      stack / tools
#   Himalayas     categories / parentCategories
#   generic       tags / keywords / mustHaveSkills
SKILL_KEYS: tuple[str, ...] = (
    "requiredSkills",
    "niceToHaveSkills",
    "skills",
    "mustHaveSkills",
    "requirements",
    "technologies",
    "technology",
    "technologyTags",
    "mainTechnology",
    "tags",
    "keywords",
    "stack",
    "tools",
    "categories",
    "parentCategories",
)

# A job rebuilt from ``--from-dump`` carries no payload; the dump's non-empty
# raw KEYS ride under this private marker so ``skills_listing_present`` can
# still be computed. ``classify_job`` reads only named keys, so the marker
# never changes a verdict.
_RAW_KEYS_MARKER = "_m0_raw_keys"

PASSED = "passed"


# ── Report dataclasses (pure data, JSON-serialisable via asdict) ─────────────


@dataclass
class SourceRow:
    source: str
    raw: int = 0
    unique: int = 0
    no_url: int = 0
    known: int = 0
    new: int = 0
    salary_present: int = 0
    salary_parsed: int = 0
    location_classified: int = 0
    city_present: int = 0
    skills_listing_present: int = 0
    passed: int = 0
    error: str = ""
    skill_keys_found: dict[str, int] = field(default_factory=dict)

    def share(self, attr: str) -> float | None:
        return (getattr(self, attr) / self.raw) if self.raw else None


@dataclass
class Report:
    sources: list[SourceRow]
    totals: SourceRow
    verdicts: dict[str, int]
    currency_split: dict[str, int]
    contract_split: dict[str, int]
    mode_split: dict[str, int]
    unparsed_salaries: list[str]
    unknown_locations: list[str]
    known_available: bool
    known_note: str
    probed_at: str


@dataclass
class RuleResult:
    rule: str
    status: str  # PASS | FAIL | UNMEASURED
    value: float | None
    threshold: float
    comparator: str  # ">=" | "<="
    note: str


# ── Probe (the ONLY function that touches the network) ─────────────────────


def probe_sources(sources: Iterable[Any]) -> dict[str, tuple[list[Job], str]]:
    """name -> (jobs, error). One try/except per source, like the hunt loop."""
    out: dict[str, tuple[list[Job], str]] = {}
    for source in sources:
        name = getattr(source, "name", repr(source))
        try:
            jobs = list(source.search() or [])
            out[name] = (jobs, "")
        except Exception as exc:  # one broken scraper must not kill the probe
            out[name] = ([], f"{type(exc).__name__}: {exc}")
    return out


# ── Pure helpers ────────────────────────────────────────────────────────────


def _nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes, list, tuple, dict, set)):
        return len(value) > 0
    return True


def nonempty_raw_keys(job: Job) -> list[str]:
    """Keys of ``job.raw`` whose value is non-empty (the dump stores only these)."""
    raw = job.raw or {}
    marker = raw.get(_RAW_KEYS_MARKER)
    if isinstance(marker, list):
        return [str(k) for k in marker]
    return [str(k) for k, v in raw.items() if _nonempty(v)]


def skill_keys_present(job: Job) -> list[str]:
    """Which of SKILL_KEYS the listing payload carries with a non-empty value."""
    present = set(nonempty_raw_keys(job))
    return [k for k in SKILL_KEYS if k in present]


def _url_norm(job: Job) -> str:
    try:
        return normalize_url(job.url or "")
    except Exception:
        return ""


def summarise(
    jobs_by_source: Mapping[str, list[Job]],
    known: set[str],
    flt: Mapping[str, Any] | None,
    *,
    errors: Mapping[str, str] | None = None,
    known_available: bool = True,
    known_note: str = "",
    max_samples: int = 15,
    probed_at: str = "",
) -> Report:
    """Pure aggregation over already-fetched jobs. No network, no DB."""
    errors = errors or {}
    rows: list[SourceRow] = []
    totals = SourceRow(source="TOTAL")
    verdicts: Counter[str] = Counter()
    currency: Counter[str] = Counter()
    contract: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    unparsed_salaries: dict[str, None] = {}
    unknown_locations: dict[str, None] = {}
    total_keys: Counter[str] = Counter()
    seen_global: set[str] = set()

    names = list(jobs_by_source)
    for name in errors:
        if name not in jobs_by_source:
            names.append(name)

    for name in names:
        jobs = list(jobs_by_source.get(name, []))
        row = SourceRow(source=name, raw=len(jobs), error=errors.get(name, ""))
        seen_local: set[str] = set()
        keys_found: Counter[str] = Counter()

        for job in jobs:
            norm = _url_norm(job)
            if not norm:
                row.no_url += 1
            else:
                if norm not in seen_local:
                    seen_local.add(norm)
                    if norm in known:
                        row.known += 1
                if norm not in seen_global:
                    seen_global.add(norm)
                    totals.unique += 1
                    if norm in known:
                        totals.known += 1

            salary_raw = (job.salary or "").strip()
            if salary_raw:
                row.salary_present += 1
                sp = parse_salary(salary_raw)
                if sp.parsed:
                    row.salary_parsed += 1
                    currency[sp.currency] += 1
                    contract[sp.contract or "unspecified"] += 1
                else:
                    unparsed_salaries.setdefault(salary_raw)

            lp = classify_location(job.location, flt=flt)
            modes[lp.remote_mode] += 1
            if lp.remote_mode != UNKNOWN:
                row.location_classified += 1
            else:
                loc_raw = (job.location or "").strip()
                if loc_raw:
                    unknown_locations.setdefault(loc_raw)
            if lp.city:
                row.city_present += 1

            keys = skill_keys_present(job)
            if keys:
                row.skills_listing_present += 1
                keys_found.update(keys)

            try:
                reason = classify_job(job, flt=flt)
            except Exception as exc:  # a report must never die on one odd job
                reason = f"error:{type(exc).__name__}"
            verdicts[reason or PASSED] += 1
            if reason is None:
                row.passed += 1

        row.unique = len(seen_local)
        row.new = row.unique - row.known
        row.skill_keys_found = dict(keys_found)
        total_keys.update(keys_found)
        rows.append(row)

        totals.raw += row.raw
        totals.no_url += row.no_url
        totals.salary_present += row.salary_present
        totals.salary_parsed += row.salary_parsed
        totals.location_classified += row.location_classified
        totals.city_present += row.city_present
        totals.skills_listing_present += row.skills_listing_present
        totals.passed += row.passed

    totals.new = totals.unique - totals.known
    totals.skill_keys_found = dict(total_keys)

    return Report(
        sources=rows,
        totals=totals,
        verdicts=dict(verdicts),
        currency_split=dict(currency),
        contract_split=dict(contract),
        mode_split=dict(modes),
        unparsed_salaries=list(unparsed_salaries)[:max_samples],
        unknown_locations=list(unknown_locations)[:max_samples],
        known_available=known_available,
        known_note=known_note,
        probed_at=probed_at,
    )


# ── Decision rules (pure) ───────────────────────────────────────────────────


def evaluate_rules(report: Report) -> list[RuleResult]:
    t = report.totals
    out: list[RuleResult] = []

    if report.known_available:
        new_status = "PASS" if t.new >= MIN_NEW_PER_SWEEP else "FAIL"
        new_note = (
            "postings_seen is worth building (M1)"
            if new_status == "PASS"
            else "postings_seen adds little over applications + generation_runs — "
            "close M1, keep only M2 (skip_reason)"
        )
    else:
        new_status = "UNMEASURED"
        new_note = f"known set unavailable ({report.known_note}); new == unique here"
    out.append(
        RuleResult(
            rule="new listings per full sweep (unique, not yet known)",
            status=new_status,
            value=float(t.new),
            threshold=float(MIN_NEW_PER_SWEEP),
            comparator=">=",
            note=new_note,
        )
    )

    sal_share = t.share("salary_parsed")
    if sal_share is None:
        out.append(
            RuleResult(
                rule="salary_parsed / raw",
                status="UNMEASURED",
                value=None,
                threshold=MIN_SALARY_PARSED_SHARE,
                comparator=">=",
                note="no listings probed",
            )
        )
    else:
        ok = sal_share >= MIN_SALARY_PARSED_SHARE
        out.append(
            RuleResult(
                rule="salary_parsed / raw",
                status="PASS" if ok else "FAIL",
                value=sal_share,
                threshold=MIN_SALARY_PARSED_SHARE,
                comparator=">=",
                note=(
                    "keep the salary column set (M1.b) + the pay digest"
                    if ok
                    else "drop the salary column set (M1.b) and the pay section of the digest"
                ),
            )
        )

    loc_share = t.share("location_classified")
    if loc_share is None:
        out.append(
            RuleResult(
                rule="location_classified / raw",
                status="UNMEASURED",
                value=None,
                threshold=MIN_LOCATION_CLASSIFIED_SHARE,
                comparator=">=",
                note="no listings probed",
            )
        )
    else:
        ok = loc_share >= MIN_LOCATION_CLASSIFIED_SHARE
        out.append(
            RuleResult(
                rule="location_classified / raw",
                status="PASS" if ok else "FAIL",
                value=loc_share,
                threshold=MIN_LOCATION_CLASSIFIED_SHARE,
                comparator=">=",
                note=(
                    "keep remote_mode as a real column"
                    if ok
                    else "keep location_raw only; remote_mode best-effort with a documented "
                    "'unknown' majority; M4's remote-mode cuts are not built"
                ),
            )
        )

    new_share = (t.new / t.raw) if t.raw else None
    out.append(
        RuleResult(
            rule="expected inserts/day (M0.b x new-share)",
            status="UNMEASURED",
            value=new_share,
            threshold=float(MAX_INSERTS_PER_DAY),
            comparator="<=",
            note=(
                "needs M0.b (raw listings/day from prod source_runs). "
                f"{M0B_FORMULA}; new/raw from this probe = "
                + (f"{new_share:.3f}" if new_share is not None else "n/a")
                + ". If above the threshold: TTL 180 -> 90 days and cloudscraper sources "
                "excluded from the write."
            ),
        )
    )
    return out


# ── Dump / from-dump ────────────────────────────────────────────────────────


def build_dump(
    probed: Mapping[str, tuple[list[Job], str]], flt: Mapping[str, Any] | None
) -> dict[str, Any]:
    jobs_out: list[dict[str, Any]] = []
    for name, (jobs, _err) in probed.items():
        for job in jobs:
            try:
                verdict = classify_job(job, flt=flt) or PASSED
            except Exception as exc:
                verdict = f"error:{type(exc).__name__}"
            jobs_out.append(
                {
                    "source": name,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "salary": job.salary,
                    "url": job.url,
                    "raw_keys": nonempty_raw_keys(job),
                    "verdict": verdict,
                }
            )
    return {
        "probed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": list(probed),
        "errors": {name: err for name, (_jobs, err) in probed.items() if err},
        "jobs": jobs_out,
    }


def write_dump(path: Path, dump: dict[str, Any]) -> None:
    path.write_text(json.dumps(dump, indent=1, ensure_ascii=False), encoding="utf-8")


def load_dump(path: Path) -> tuple[dict[str, list[Job]], dict[str, str], str]:
    """Rebuild (jobs_by_source, errors, probed_at) from a ``--dump`` file.

    Jobs come back with ``raw={_RAW_KEYS_MARKER: [...]}`` — the key names only.
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    jobs_by_source: dict[str, list[Job]] = {name: [] for name in data.get("sources") or []}
    for item in data.get("jobs") or []:
        source = str(item.get("source") or "")
        job = Job(
            title=str(item.get("title") or ""),
            company=str(item.get("company") or ""),
            location=str(item.get("location") or ""),
            salary=item.get("salary") or None,
            url=str(item.get("url") or ""),
            source=source,
            raw={_RAW_KEYS_MARKER: list(item.get("raw_keys") or [])},
        )
        jobs_by_source.setdefault(source, []).append(job)
    errors = {str(k): str(v) for k, v in (data.get("errors") or {}).items()}
    return jobs_by_source, errors, str(data.get("probed_at") or "")


# ── Known-URL set (the only DB read; read-only, best-effort) ─────────────────


def load_known_urls() -> tuple[set[str], bool, str]:
    """(known url_norms, available, note). Never raises; never creates a DB."""
    try:
        from hunter import tracker

        db_path = Path(tracker.DB_PATH)
        if not db_path.exists():
            return set(), False, f"tracker.db not found at {db_path}"
        known = tracker.get_known_urls()
    except Exception as exc:
        return set(), False, f"{type(exc).__name__}: {exc}"
    if not known:
        return set(), True, "tracker.db has no rows for this user — every listing counts as new"
    return known, True, f"{len(known)} known url_norms"


# ── Report formatting ───────────────────────────────────────────────────────


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _err_short(err: str, width: int = 40) -> str:
    err = err.replace("\n", " ")
    return err if len(err) <= width else err[: width - 1] + "…"


def format_report(report: Report, rules: list[RuleResult]) -> str:
    lines: list[str] = []
    stamp = f" (probed {report.probed_at})" if report.probed_at else ""
    lines.append(f"[market_memory_m0] listing attribute coverage, one sweep{stamp}")
    lines.append(
        f"known set: {report.known_note or ('available' if report.known_available else 'n/a')}"
    )
    lines.append("")

    header = (
        f"{'source':<22} {'raw':>5} {'uniq':>5} {'new':>5} {'sal%':>5} {'salP%':>6} "
        f"{'loc%':>5} {'pass':>5} err"
    )
    lines.append(header)
    lines.append("-" * len(header))
    ordered = sorted(report.sources, key=lambda r: (-r.raw, r.source))
    for r in ordered + [report.totals]:
        if r is report.totals:
            lines.append("-" * len(header))
        lines.append(
            f"{r.source:<22} {r.raw:>5} {r.unique:>5} {r.new:>5} "
            f"{_pct(r.share('salary_present')):>5} {_pct(r.share('salary_parsed')):>6} "
            f"{_pct(r.share('location_classified')):>5} {r.passed:>5} "
            f"{_err_short(r.error)}"
        )
    t = report.totals
    lines.append(
        f"totals: raw={t.raw} unique={t.unique} known={t.known} new={t.new} "
        f"no_url={t.no_url} city_present={t.city_present} "
        f"skills_listing_present={t.skills_listing_present}"
    )
    lines.append("")

    lines.append("Filter verdicts (classify_job reason, desc):")
    for reason, n in sorted(report.verdicts.items(), key=lambda kv: (-kv[1], kv[0])):
        share = (n / t.raw) if t.raw else 0.0
        lines.append(f"  {reason:<18} {n:>5}  {share:.0%}")
    lines.append("")

    def _split(title: str, counts: Mapping[str, int]) -> None:
        lines.append(title)
        if not counts:
            lines.append("  (none)")
        for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {k or '(blank)':<14} {n:>5}")
        lines.append("")

    _split("Parsed salaries by currency:", report.currency_split)
    _split("Parsed salaries by contract:", report.contract_split)
    _split("Locations by remote_mode (all listings):", report.mode_split)

    lines.append("Listing skill keys found (source: key=count):")
    any_keys = False
    for r in ordered:
        if r.skill_keys_found:
            any_keys = True
            keys = ", ".join(f"{k}={n}" for k, n in sorted(r.skill_keys_found.items()))
            lines.append(f"  {r.source:<22} {keys}")
    if not any_keys:
        lines.append("  (none)")
    lines.append("")

    lines.append(f"Unparsed salary samples ({len(report.unparsed_salaries)} shown):")
    for s in report.unparsed_salaries:
        lines.append(f"  - {s}")
    if not report.unparsed_salaries:
        lines.append("  (none)")
    lines.append("")
    lines.append(f"Unclassified location samples ({len(report.unknown_locations)} shown):")
    for s in report.unknown_locations:
        lines.append(f"  - {s}")
    if not report.unknown_locations:
        lines.append("  (none)")
    lines.append("")

    lines.append("Decision rules (docs/MARKET_MEMORY_PLAN.md M0):")
    for rule in rules:
        if rule.value is None:
            val = "n/a"
        elif rule.threshold <= 1.0:
            val = f"{rule.value:.1%}"
        elif rule.comparator == "<=":
            val = f"new/raw={rule.value:.3f}"
        else:
            val = f"{rule.value:.0f}"
        thr = f"{rule.threshold:.0%}" if rule.threshold <= 1.0 else f"{rule.threshold:.0f}"
        lines.append(f"  [{rule.status:<10}] {rule.rule}: {val} {rule.comparator} {thr}")
        lines.append(f"               {rule.note}")
    lines.append("")
    lines.append("M0.b — run on the deploy host (not measurable here):")
    lines.append(f"  {M0B_HOWTO}")
    for sql_line in M0B_SQL.splitlines():
        lines.append(f"  {sql_line}")
    lines.append(f"  {M0B_FORMULA}")
    return "\n".join(lines)


def report_to_json(report: Report, rules: list[RuleResult]) -> dict[str, Any]:
    out = asdict(report)
    out["rules"] = [asdict(r) for r in rules]
    out["m0b"] = {"sql": M0B_SQL, "howto": M0B_HOWTO, "formula": M0B_FORMULA}
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--json", action="store_true", help="print one JSON object instead")
    parser.add_argument(
        "--sources", default="", help="comma-separated source names to probe (default: all)"
    )
    parser.add_argument("--dump", type=Path, default=None, help="write probed jobs to JSON")
    parser.add_argument(
        "--from-dump", type=Path, default=None, help="skip the network; analyse a --dump file"
    )
    parser.add_argument(
        "--unparsed-salaries",
        type=int,
        default=15,
        help="how many distinct unparsed salary strings to print (default 15)",
    )
    parser.add_argument(
        "--unknown-locations",
        type=int,
        default=15,
        help="how many distinct unclassified location strings to print (default 15)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.from_dump is not None and args.dump is not None:
        print("ERROR: --dump and --from-dump are mutually exclusive", file=sys.stderr)
        return 2
    if args.from_dump is not None and args.sources:
        print("ERROR: --sources cannot be combined with --from-dump", file=sys.stderr)
        return 2

    from hunter.filter_profile import load_profile

    flt = load_profile()
    max_samples = max(args.unparsed_salaries, args.unknown_locations, 0)

    if args.from_dump is not None:
        if not args.from_dump.exists():
            print(f"ERROR: dump not found: {args.from_dump}", file=sys.stderr)
            return 2
        try:
            jobs_by_source, errors, probed_at = load_dump(args.from_dump)
        except (ValueError, OSError) as exc:
            print(f"ERROR: cannot read dump {args.from_dump}: {exc}", file=sys.stderr)
            return 2
    else:
        from hunter.sources import ALL_SOURCES

        sources = list(ALL_SOURCES)
        if args.sources:
            wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
            by_name = {s.name: s for s in sources}
            unknown = [w for w in wanted if w not in by_name]
            if unknown:
                print(
                    f"ERROR: unknown source(s): {', '.join(unknown)}; "
                    f"available: {', '.join(sorted(by_name))}",
                    file=sys.stderr,
                )
                return 2
            sources = [by_name[w] for w in wanted]
        probed = probe_sources(sources)
        probed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if args.dump is not None:
            dump = build_dump(probed, flt)
            dump["probed_at"] = probed_at
            write_dump(args.dump, dump)
        jobs_by_source = {name: jobs for name, (jobs, _e) in probed.items()}
        errors = {name: err for name, (_j, err) in probed.items() if err}

    known, known_available, known_note = load_known_urls()
    report = summarise(
        jobs_by_source,
        known,
        flt,
        errors=errors,
        known_available=known_available,
        known_note=known_note,
        max_samples=max_samples,
        probed_at=probed_at,
    )
    report.unparsed_salaries = report.unparsed_salaries[: args.unparsed_salaries]
    report.unknown_locations = report.unknown_locations[: args.unknown_locations]
    rules = evaluate_rules(report)

    if args.json:
        print(json.dumps(report_to_json(report, rules), indent=2, ensure_ascii=False))
    else:
        print(format_report(report, rules))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
