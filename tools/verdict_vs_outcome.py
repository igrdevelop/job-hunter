"""
tools/verdict_vs_outcome.py — does the independent ATS verdict predict a real
reply?

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M0.1 ("promised in path-A A3,
not written"): before ATS_VERDICT_TARGET/ATS_VERDICT_MAX_REFINES are ever
tuned, this answers whether the Step 7a verdict score correlates with
anything downstream. Read-only over tracker.db + Applications/**/content.json
— zero LLM calls, zero writes.

Sample (mirrors the plan's M0.1 exactly):
  - ats_verdict IS NOT NULL
  - sent_parse.classify(sent) == "applied"
  - parse_sent_date(sent) <= today - --min-age-days (default 21; censors
    replies that just haven't had time to arrive yet)
  - excludes cost_usd IS NULL rows dated 2026-08-07..2026-08-10 (the CLI
    outage window where the verdict wasn't actually scored by Haiku)

Two analyses, both against `answered` and separately `confirmed` (labeled
"ATS-ack proxy"):
  1. Continuous verdict: point-biserial correlation (= Pearson r against a
     0/1 outcome); an odds ratio for +10pp verdict from an unregularized
     logistic regression (`answered ~ verdict/10 + source_bucket`, sklearn
     LogisticRegression(C=inf) — no scipy/statsmodels available), with a 90%
     bootstrap CI (seeded, default 2000 resamples); a Fisher-exact test
     (implemented from scratch with math.comb — no scipy) on the top vs.
     bottom verdict tercile.
  2. A second, larger-n cut from every Applications/**/content.json's
     verdict_history: share of accepted refine rounds by kind (honest vs.
     stretch), mean accepted-round delta, and the share of runs whose
     winning round was >= 4 (i.e. needed a stretch round to hit target).

Sample-size reality (from the plan): ~700 tracker rows -> ~250-300 sent ->
at a 5-10% answer rate, 15-30 events total. That only reliably catches a
LARGE effect (5% vs 20%); absence of significance here is not evidence of
absence. The tool prints n at every step and states the plan's decision rule
verbatim, plus which branch the numbers land in.

Usage:
    docker compose exec -T job-hunter python tools/verdict_vs_outcome.py --min-age-days 21
    docker compose exec -T job-hunter python tools/verdict_vs_outcome.py --json
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

# Force UTF-8 output on Windows (console defaults to cp1252 -> emoji crash).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# CLI-outage window (docs/AGENT_LOG.md, 2026-08): ats_verdict rows here may
# have been served by the CLI subscription's default model instead of the
# pinned JUDGE_MODEL (Haiku) — only rows that ALSO have no cost_usd (API
# mode always stamps it) are excluded, since a priced row proves API mode.
_CLI_OUTAGE_START = "2026-08-07"
_CLI_OUTAGE_END = "2026-08-10"

# Source-attribution buckets for the confounder split (Easy Apply / ATS-native
# boards convert very differently — see the plan's own note). Anything not
# listed here falls into "other".
_SOURCE_BUCKETS: dict[str, set[str]] = {
    "linkedin": {"linkedin", "linkedin_scout_relay"},
    "polish_boards": {
        "justjoin",
        "nofluffjobs",
        "bulldogjob",
        "pracuj",
        "theprotocol",
        "solidjobs",
        "thesmartjobs",
    },
    "ats_direct": {"ats_aggregator"},
}


def source_bucket(source_name: str) -> str:
    for bucket, names in _SOURCE_BUCKETS.items():
        if source_name in names:
            return bucket
    return "other"


# ── Sample selection (pure) ─────────────────────────────────────────────────


def build_sample(
    raw_rows: list[dict],
    min_age_days: int = 21,
    today: date | None = None,
) -> list[dict]:
    """Filter+shape raw tracker rows into the M0.1 analysis sample.

    Each raw row is a dict with keys: date, url, ats_verdict, sent,
    confirmation, answer, cost_usd. Returns dicts with: verdict (float),
    answered (0/1), confirmed (0/1), bucket (str), url (str), date (str).
    """
    from hunter.funnel import _is_answered, _is_confirmed, source_for_url
    from hunter.sent_parse import classify, parse_sent_date

    today = today or date.today()
    cutoff = today - timedelta(days=min_age_days)

    sample: list[dict] = []
    for r in raw_rows:
        verdict = r.get("ats_verdict")
        if verdict is None:
            continue

        sent = r.get("sent") or ""
        if classify(sent) != "applied":
            continue
        sent_date = parse_sent_date(sent)
        if sent_date is None or sent_date > cutoff:
            continue

        row_date = (r.get("date") or "")[:10]
        if r.get("cost_usd") is None and _CLI_OUTAGE_START <= row_date <= _CLI_OUTAGE_END:
            continue

        sample.append(
            {
                "verdict": float(verdict),
                "answered": int(_is_answered(r.get("answer") or "")),
                "confirmed": int(_is_confirmed(r.get("confirmation") or "")),
                "bucket": source_bucket(source_for_url(r.get("url") or "")),
                "url": r.get("url") or "",
                "date": row_date,
            }
        )
    return sample


def fetch_raw_rows(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT date, url, ats_verdict, sent, confirmation, answer, cost_usd "
        "FROM applications WHERE ats_verdict IS NOT NULL"
    )
    return [dict(r) for r in cur.fetchall()]


# ── Continuous-verdict stats (pure) ─────────────────────────────────────────


def point_biserial(values: list[float], binary: list[int]) -> float | None:
    """Pearson r between a continuous var and a 0/1 var == point-biserial r."""
    import numpy as np

    n = len(values)
    if n < 2 or len(set(binary)) < 2:
        return None
    v = np.asarray(values, dtype=float)
    b = np.asarray(binary, dtype=float)
    if v.std() == 0:
        return None
    return float(np.corrcoef(v, b)[0, 1])


def _fit_or_for_plus_10pp(sample: list[dict], target: str) -> float | None:
    """Odds ratio for a +10 percentage-point verdict increase, from an
    unregularized logistic regression `target ~ verdict/10 + source_bucket`
    (baseline bucket = alphabetically first present). Returns None when the
    fit is degenerate (single-class outcome, or sklearn fails to converge)."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    y = np.array([row[target] for row in sample], dtype=float)
    if len(set(y.tolist())) < 2:
        return None

    buckets = sorted({row["bucket"] for row in sample})
    baseline = buckets[0]
    dummy_cols = [b for b in buckets if b != baseline]

    X = []
    for row in sample:
        feat = [row["verdict"] / 10.0]
        feat.extend(1.0 if row["bucket"] == b else 0.0 for b in dummy_cols)
        X.append(feat)
    X_arr = np.array(X, dtype=float)

    try:
        model = LogisticRegression(C=np.inf, max_iter=2000)
        model.fit(X_arr, y)
    except Exception:  # noqa: BLE001 — perfect separation / non-convergence
        return None

    coef_verdict = float(model.coef_[0][0])
    if not math.isfinite(coef_verdict):
        return None
    try:
        return math.exp(coef_verdict)
    except OverflowError:
        return None


@dataclass
class OddsRatioResult:
    point: float | None
    ci_low: float | None
    ci_high: float | None
    valid_resamples: int
    requested_resamples: int


def bootstrap_or_ci(
    sample: list[dict],
    target: str,
    n_boot: int = 2000,
    seed: int = 42,
    ci: float = 0.90,
) -> OddsRatioResult:
    import numpy as np

    point = _fit_or_for_plus_10pp(sample, target)
    n = len(sample)
    if n == 0:
        return OddsRatioResult(point, None, None, 0, n_boot)

    rng = np.random.default_rng(seed)
    idx = np.arange(n)
    ors: list[float] = []
    for _ in range(n_boot):
        resample_idx = rng.choice(idx, size=n, replace=True)
        resample = [sample[i] for i in resample_idx]
        orv = _fit_or_for_plus_10pp(resample, target)
        if orv is not None and math.isfinite(orv):
            ors.append(orv)

    # Too few valid resamples (near-constant outcome after resampling) to
    # trust a percentile CI — report the point estimate only.
    if len(ors) < 50:
        return OddsRatioResult(point, None, None, len(ors), n_boot)

    ors.sort()
    lo_q = (1 - ci) / 2
    hi_q = 1 - lo_q
    lo = ors[int(lo_q * len(ors))]
    hi = ors[min(len(ors) - 1, int(hi_q * len(ors)))]
    return OddsRatioResult(point, lo, hi, len(ors), n_boot)


def fisher_exact_p(a: int, b: int, c: int, d: int) -> float:
    """Two-tailed Fisher exact test p-value for a 2x2 table
    [[a, b], [c, d]], computed from scratch with math.comb (no scipy)."""
    n = a + b + c + d
    row1 = a + b
    row2 = c + d
    col1 = a + c
    if n == 0 or row1 == 0 or row2 == 0 or col1 == 0 or col1 == n:
        return 1.0

    lo = max(0, col1 - row2)
    hi = min(row1, col1)

    def p_of(x: int) -> Fraction:
        return Fraction(math.comb(row1, x) * math.comb(row2, col1 - x), math.comb(n, col1))

    p_obs = p_of(a)
    total = Fraction(0)
    for x in range(lo, hi + 1):
        px = p_of(x)
        if px <= p_obs:
            total += px
    return float(total)


def tercile_fisher(sample: list[dict], target: str) -> dict[str, Any] | None:
    """Fisher exact test comparing the outcome rate in the top vs. bottom
    verdict tercile. None when the sample is too small to form two non-empty
    terciles (n < 6)."""
    if len(sample) < 6:
        return None
    ordered = sorted(sample, key=lambda r: r["verdict"])
    n = len(ordered)
    k = n // 3
    if k == 0:
        return None
    bottom = ordered[:k]
    top = ordered[-k:]
    a = sum(r[target] for r in top)  # positive in top tercile
    b = k - a
    c = sum(r[target] for r in bottom)  # positive in bottom tercile
    d = k - c
    return {
        "p": fisher_exact_p(a, b, c, d),
        "tercile_n": k,
        "top_positive": a,
        "bottom_positive": c,
    }


# ── verdict_history section (pure over pre-loaded histories) ───────────────


def find_content_jsons(applications_dir: Path) -> list[Path]:
    """content.json under Applications/{date}/{Company}/ only — two levels
    deep. A dual-apply shadow lives one level deeper
    (Applications/{date}/{Company}/{shadow}/content.json) and is excluded on
    purpose: it never carries a real verdict_history (no tracker row, no
    refine-loop stamping)."""
    if not applications_dir.exists():
        return []
    return sorted(applications_dir.glob("*/*/content.json"))


def load_verdict_histories(paths: list[Path]) -> list[list[dict]]:
    histories: list[list[dict]] = []
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        vh = data.get("verdict_history")
        if isinstance(vh, list) and vh:
            histories.append(vh)
    return histories


def analyze_histories(histories: list[list[dict]]) -> dict[str, Any]:
    accepted = [r for h in histories for r in h if r.get("outcome") == "accepted"]
    by_kind = {"honest": 0, "stretch": 0, "other": 0}
    deltas: list[float] = []
    for r in accepted:
        kind = r.get("kind") if r.get("kind") in ("honest", "stretch") else "other"
        by_kind[kind] += 1
        before = r.get("score_before")
        after = r.get("score_after")
        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
            deltas.append(float(after) - float(before))

    winning_ge4 = 0
    for h in histories:
        accepted_rounds = [
            r["round"] for r in h if r.get("outcome") == "accepted" and r.get("round") is not None
        ]
        if accepted_rounds and max(accepted_rounds) >= 4:
            winning_ge4 += 1

    n_runs = len(histories)
    n_accepted = len(accepted)
    return {
        "runs_with_history": n_runs,
        "accepted_rounds": n_accepted,
        "accepted_honest": by_kind["honest"],
        "accepted_stretch": by_kind["stretch"],
        "accepted_honest_share": (by_kind["honest"] / n_accepted) if n_accepted else None,
        "accepted_stretch_share": (by_kind["stretch"] / n_accepted) if n_accepted else None,
        "mean_accepted_delta": (sum(deltas) / len(deltas)) if deltas else None,
        "winning_round_ge4_share": (winning_ge4 / n_runs) if n_runs else None,
    }


# ── Decision rule ────────────────────────────────────────────────────────────

DECISION_RULE = """
Decision rule (docs/improvement-2026-09/08-DATA_EVAL_PLAN.md, M0.1):
  - answered >= 15 AND the 90% bootstrap CI for OR(+10pp) is entirely > 1
    -> ATS_VERDICT_TARGET stays at 95.
  - answered >= 15 AND the CI includes 1
    -> run ATS_VERDICT_TARGET 95->88 and ATS_VERDICT_MAX_REFINES 5->2 as a
       6-week env experiment.
  - answered < 15
    -> do not interpret the correlation. Decide from the verdict_history cut
       instead: if stretch rounds are accepted < 20% of the time, or the
       mean accepted delta is < 2*sigma from `tools/verdict_noise.py`
       -> ATS_VERDICT_MAX_REFINES 5->3 (drop the stretch rounds); leave
       ATS_VERDICT_TARGET untouched either way.
""".strip()


def decide_branch(answered_n: int, ci_low: float | None, ci_high: float | None) -> str:
    if answered_n >= 15:
        if ci_low is not None and ci_low > 1.0:
            return "answered>=15, CI(+10pp) entirely > 1 -> keep ATS_VERDICT_TARGET=95"
        return (
            "answered>=15, CI includes 1 (or could not be computed) -> "
            "consider the 95->88 / MAX_REFINES 5->2 env experiment"
        )
    return (
        "answered<15 -> correlation is not interpretable here; decide from the "
        "verdict_history section instead (see decision rule)"
    )


# ── Report ────────────────────────────────────────────────────────────────────


def build_report(
    raw_rows: list[dict],
    sample: list[dict],
    histories: list[list[dict]],
    n_boot: int,
    seed: int,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "raw_verdict_rows": len(raw_rows),
        "sample_n": len(sample),
        "targets": {},
    }

    bucket_counts: dict[str, int] = {}
    for row in sample:
        bucket_counts[row["bucket"]] = bucket_counts.get(row["bucket"], 0) + 1
    report["source_bucket_n"] = bucket_counts

    for target, label in (("answered", "answered"), ("confirmed", "confirmed (ATS-ack proxy)")):
        n_positive = sum(row[target] for row in sample)
        r_pb = point_biserial([row["verdict"] for row in sample], [row[target] for row in sample])
        or_result = bootstrap_or_ci(sample, target, n_boot=n_boot, seed=seed)
        fisher = tercile_fisher(sample, target)
        report["targets"][target] = {
            "label": label,
            "n_positive": n_positive,
            "point_biserial_r": r_pb,
            "odds_ratio_plus_10pp": {
                "point": or_result.point,
                "ci90_low": or_result.ci_low,
                "ci90_high": or_result.ci_high,
                "valid_resamples": or_result.valid_resamples,
                "requested_resamples": or_result.requested_resamples,
            },
            "tercile_fisher_exact": fisher,
            "decision_branch": decide_branch(n_positive, or_result.ci_low, or_result.ci_high),
        }

    report["verdict_history"] = analyze_histories(histories)
    return report


def format_report(report: dict[str, Any]) -> str:
    lines = [
        f"[verdict_vs_outcome] {report['raw_verdict_rows']} row(s) with a recorded ats_verdict",
        f"[verdict_vs_outcome] {report['sample_n']} row(s) in the M0.1 sample "
        "(applied, aged out, not CLI-outage-window)",
        f"[verdict_vs_outcome] source_bucket split: {report['source_bucket_n']}",
        "",
    ]
    for _target, data in report["targets"].items():
        lines.append(f"── {data['label']} " + "─" * max(1, 60 - len(data["label"])))
        lines.append(f"  n positive: {data['n_positive']}")
        r_pb = data["point_biserial_r"]
        lines.append(
            f"  point-biserial r: {r_pb:.3f}" if r_pb is not None else "  point-biserial r: n/a"
        )
        orr = data["odds_ratio_plus_10pp"]
        if orr["point"] is not None:
            ci_text = (
                f"[{orr['ci90_low']:.2f}, {orr['ci90_high']:.2f}]"
                if orr["ci90_low"] is not None
                else "(CI not computable — too few valid resamples)"
            )
            lines.append(
                f"  odds ratio (+10pp verdict): {orr['point']:.2f}, 90% CI {ci_text} "
                f"({orr['valid_resamples']}/{orr['requested_resamples']} valid resamples)"
            )
        else:
            lines.append("  odds ratio (+10pp verdict): n/a (degenerate outcome)")
        fisher = data["tercile_fisher_exact"]
        if fisher:
            lines.append(
                f"  Fisher exact, top vs bottom tercile (n={fisher['tercile_n']} each): "
                f"top={fisher['top_positive']}/{fisher['tercile_n']}, "
                f"bottom={fisher['bottom_positive']}/{fisher['tercile_n']}, p={fisher['p']:.3f}"
            )
        else:
            lines.append("  Fisher exact, top vs bottom tercile: n/a (sample too small)")
        lines.append(f"  decision branch: {data['decision_branch']}")
        lines.append("")

    vh = report["verdict_history"]
    lines.append("── verdict_history (Applications/**/content.json) " + "─" * 10)
    lines.append(f"  runs with a verdict_history: {vh['runs_with_history']}")
    lines.append(f"  accepted rounds: {vh['accepted_rounds']}")
    lines.append(
        f"  accepted honest/stretch: {vh['accepted_honest']}/{vh['accepted_stretch']} "
        f"(share stretch: {vh['accepted_stretch_share']:.2f})"
        if vh["accepted_stretch_share"] is not None
        else f"  accepted honest/stretch: {vh['accepted_honest']}/{vh['accepted_stretch']}"
    )
    if vh["mean_accepted_delta"] is not None:
        lines.append(f"  mean accepted delta: {vh['mean_accepted_delta']:.2f}pp")
    if vh["winning_round_ge4_share"] is not None:
        lines.append(f"  runs where the winning round was >=4: {vh['winning_round_ge4_share']:.2%}")
    lines.append("")
    lines.append(DECISION_RULE)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", type=Path, default=None, help="tracker.db path override")
    parser.add_argument(
        "--applications-dir", type=Path, default=None, help="Applications/ root override"
    )
    parser.add_argument(
        "--min-age-days",
        type=int,
        default=21,
        help="only count a sent row whose reply window has had this long to close (default 21)",
    )
    parser.add_argument("--bootstrap-n", type=int, default=2000, help="bootstrap resamples")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for the bootstrap")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args()

    from hunter.config import APPLICATIONS_DIR, TRACKER_DB_PATH
    from hunter.db import get_db

    db_path = args.db or TRACKER_DB_PATH
    apps_dir = args.applications_dir or APPLICATIONS_DIR

    with get_db(db_path) as conn:
        raw_rows = fetch_raw_rows(conn)
    sample = build_sample(raw_rows, min_age_days=args.min_age_days)
    histories = load_verdict_histories(find_content_jsons(apps_dir))

    report = build_report(raw_rows, sample, histories, n_boot=args.bootstrap_n, seed=args.seed)
    report["db"] = str(db_path)
    report["applications_dir"] = str(apps_dir)
    report["min_age_days"] = args.min_age_days

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(report))


if __name__ == "__main__":
    main()
