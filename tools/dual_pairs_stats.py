"""
tools/dual_pairs_stats.py — dual-apply (A/B) pair statistics, read-only, $0 by
default.

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M2: hunter/dual_apply.py already
produces paired primary/shadow generations (``{Company}/{shadow_profile}/``),
but nothing summarizes them — "≈86 shadow folders on the live corpus, no
statistics, 0/12 August shadows without a verdict" per the plan's "Что уже
измеряется" table. This tool walks the local ``Applications/**`` corpus,
pairs every primary ``content.json`` with its shadow subfolder(s) (a
directory named after a known ``hunter.llm_profiles`` profile), and reports
whether the shadow model is actually competitive.

Score source, per pair side: the independent PDF verdict (``content["ats_verdict"]
["score"]`` — the same Haiku judge on both sides, so primary and shadow are
scored by one yardstick). With ``--allow-deterministic``, a side missing a
verdict falls back to ``ats_check_pdf`` then ``ats_check`` (both deterministic,
NOT judge-scored) — the report labels which source each pair actually used so
a verdict-vs-deterministic mismatch is never silently averaged together.

Statistics (all pure functions, no LLM calls):
  - share of pairs where shadow >= primary, with a Wilson 90% CI
  - mean paired difference (shadow - primary), with a seeded 2000-resample
    bootstrap 95% CI
  - sign test p-value (exact binomial via math.comb, ties excluded)
  - share of pairs whose |delta| falls under noise_sigma*2 — "statistically
    indistinguishable from Haiku-judge noise" (see tools/verdict_noise.py,
    which is where the real sigma comes from; pass it via --noise-sigma)
  - cost per side, from content["cost"]["total_usd"] when present (the shadow
    pipeline does not always stamp a cost — see hunter/dual_apply.py)

Usage (run where the Applications/ corpus lives — the deploy host):
    python tools/dual_pairs_stats.py
    docker compose exec -T job-hunter python tools/dual_pairs_stats.py --json out.json
    docker compose exec -T job-hunter python tools/dual_pairs_stats.py \\
        --allow-deterministic --noise-sigma 4.2
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

# Deterministic fallback keys, preference order (verdict is always tried first).
_DETERMINISTIC_KEYS = ("ats_check_pdf", "ats_check")


@dataclass
class PairRecord:
    primary_folder: Path
    shadow_folder: Path
    shadow_name: str
    primary_score: float
    primary_source: str
    shadow_score: float
    shadow_source: str
    primary_cost: float | None
    shadow_cost: float | None


# ── I/O ──────────────────────────────────────────────────────────────────────


def find_pairs(root: Path) -> list[tuple[Path, Path, str]]:
    """Every (primary_folder, shadow_folder, shadow_name) triple under `root`.

    A shadow folder is a direct subdirectory of a primary application folder
    whose name matches a known hunter.llm_profiles.PROFILES key and which
    itself holds a content.json (hunter.dual_apply's own layout).
    """
    from hunter.llm_profiles import PROFILES

    profile_names = set(PROFILES)
    pairs: list[tuple[Path, Path, str]] = []
    if not root.exists():
        return pairs

    for content_path in sorted(root.rglob("content.json")):
        folder = content_path.parent
        if folder.name in profile_names:
            continue  # this IS a shadow folder — reached from its parent below
        try:
            children = sorted(p for p in folder.iterdir() if p.is_dir())
        except OSError:
            continue
        for sub in children:
            if sub.name in profile_names and (sub / "content.json").exists():
                pairs.append((folder, sub, sub.name))
    return pairs


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _score_from_content(content: dict, allow_deterministic: bool) -> tuple[float | None, str]:
    """Prefer the independent verdict; optionally fall back to a deterministic
    score. Returns (score, source_label) — source_label is '' when no score
    was found at all."""

    def _get(key: str) -> float | None:
        v = content.get(key)
        if not isinstance(v, dict):
            return None
        s = v.get("score")
        try:
            return float(s) if s is not None else None
        except (TypeError, ValueError):
            return None

    verdict = _get("ats_verdict")
    if verdict is not None:
        return verdict, "verdict"
    if allow_deterministic:
        for key in _DETERMINISTIC_KEYS:
            s = _get(key)
            if s is not None:
                return s, f"deterministic:{key}"
    return None, ""


def _cost_from_content(content: dict) -> float | None:
    cost = content.get("cost")
    if not isinstance(cost, dict):
        return None
    total = cost.get("total_usd")
    try:
        return float(total) if total is not None else None
    except (TypeError, ValueError):
        return None


def load_pair(
    primary_folder: Path, shadow_folder: Path, shadow_name: str, *, allow_deterministic: bool
) -> PairRecord | None:
    """Load and score one pair. Returns None if either side has no usable score."""
    primary_content = _load_json(primary_folder / "content.json")
    shadow_content = _load_json(shadow_folder / "content.json")
    if primary_content is None or shadow_content is None:
        return None

    p_score, p_source = _score_from_content(primary_content, allow_deterministic)
    s_score, s_source = _score_from_content(shadow_content, allow_deterministic)
    if p_score is None or s_score is None:
        return None

    return PairRecord(
        primary_folder=primary_folder,
        shadow_folder=shadow_folder,
        shadow_name=shadow_name,
        primary_score=p_score,
        primary_source=p_source,
        shadow_score=s_score,
        shadow_source=s_source,
        primary_cost=_cost_from_content(primary_content),
        shadow_cost=_cost_from_content(shadow_content),
    )


# ── Pure statistics (no I/O, no LLM) ────────────────────────────────────────


def wilson_ci(k: int, n: int, z: float = 1.645) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. z=1.645 -> 90% CI."""
    if n <= 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    lo = (center - margin) / denom
    hi = (center + margin) / denom
    return (max(0.0, lo), min(1.0, hi))


def _percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = p * (len(sorted_vals) - 1)
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return sorted_vals[int(idx)]
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def bootstrap_mean_ci(
    diffs: list[float], *, resamples: int = 2000, seed: int = 42, ci: float = 0.95
) -> tuple[float, float]:
    """Seeded percentile-bootstrap CI of the mean of `diffs`."""
    if not diffs:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(diffs)
    means = []
    for _ in range(resamples):
        s = 0.0
        for _j in range(n):
            s += diffs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    alpha = (1 - ci) / 2
    return (_percentile(means, alpha), _percentile(means, 1 - alpha))


def sign_test_p(pos: int, neg: int) -> float:
    """Two-sided exact binomial sign-test p-value (ties excluded)."""
    n = pos + neg
    if n == 0:
        return 1.0
    k = min(pos, neg)
    cum = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, 2 * cum / (2**n))


def compute_stats(
    pairs: list[tuple[float, float]],
    *,
    noise_sigma: float,
    resamples: int = 2000,
    seed: int = 42,
) -> dict:
    """pairs: list of (primary_score, shadow_score). Pure — no I/O."""
    n = len(pairs)
    if n == 0:
        return {"n": 0}

    diffs = [s - p for p, s in pairs]
    wins = sum(1 for p, s in pairs if s > p)
    losses = sum(1 for p, s in pairs if s < p)
    ties = n - wins - losses
    ge = wins + ties  # "shadow >= primary"

    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "share_shadow_ge_primary": ge / n,
        "share_shadow_ge_primary_ci90": wilson_ci(ge, n),
        "mean_delta": sum(diffs) / n,
        "mean_delta_ci95": bootstrap_mean_ci(diffs, resamples=resamples, seed=seed),
        "sign_test_p": sign_test_p(wins, losses),
        "noise_sigma": noise_sigma,
        "share_within_noise": sum(1 for d in diffs if abs(d) < noise_sigma * 2) / n,
    }


# ── Report ───────────────────────────────────────────────────────────────────


def print_report(records: list[PairRecord], stats: dict, *, allow_deterministic: bool) -> None:
    print("\n=== Dual-apply (A/B) pair statistics ===")
    print(f"pairs evaluated: {stats.get('n', 0)}")
    if not records:
        print("No scored pairs found — nothing to report.")
        if not allow_deterministic:
            print("(pass --allow-deterministic to include pairs with no independent verdict)")
        return

    by_shadow: dict[str, int] = {}
    for r in records:
        by_shadow[r.shadow_name] = by_shadow.get(r.shadow_name, 0) + 1
    print("by shadow profile: " + ", ".join(f"{k}={v}" for k, v in sorted(by_shadow.items())))

    n_verdict = sum(
        1 for r in records if r.primary_source == "verdict" and r.shadow_source == "verdict"
    )
    n_det = stats["n"] - n_verdict
    print(f"scored by independent verdict (both sides): {n_verdict}")
    if n_det:
        print(f"scored via deterministic fallback (at least one side): {n_det}")

    print("\n--- outcome ---")
    print(f"  wins (shadow>primary)  : {stats['wins']}")
    print(f"  losses (shadow<primary): {stats['losses']}")
    print(f"  ties                   : {stats['ties']}")
    lo, hi = stats["share_shadow_ge_primary_ci90"]
    print(
        f"  share shadow>=primary  : {100 * stats['share_shadow_ge_primary']:.1f}%"
        f"  (90% CI {100 * lo:.1f}%-{100 * hi:.1f}%)"
    )

    print("\n--- magnitude ---")
    lo, hi = stats["mean_delta_ci95"]
    print(
        f"  mean delta (shadow-primary): {stats['mean_delta']:+.2f}pp"
        f"  (95% CI {lo:+.2f}pp to {hi:+.2f}pp, seeded 2000-resample bootstrap)"
    )
    print(f"  sign-test p-value          : {stats['sign_test_p']:.4f}")
    print(
        f"  within noise (|delta|<{2 * stats['noise_sigma']:.1f}pp, "
        f"sigma={stats['noise_sigma']:.1f}pp): {100 * stats['share_within_noise']:.1f}%"
        "  (sigma from tools/verdict_noise.py — pass --noise-sigma)"
    )

    primary_costs = [r.primary_cost for r in records if r.primary_cost is not None]
    shadow_costs = [r.shadow_cost for r in records if r.shadow_cost is not None]
    print("\n--- cost (from content['cost']['total_usd'], where present) ---")
    if primary_costs:
        print(
            f"  primary: n={len(primary_costs)} mean=${sum(primary_costs) / len(primary_costs):.3f}"
        )
    else:
        print("  primary: unpriced")
    if shadow_costs:
        print(f"  shadow : n={len(shadow_costs)} mean=${sum(shadow_costs) / len(shadow_costs):.3f}")
    else:
        print("  shadow : unpriced")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--root",
        default=None,
        help="Applications corpus root (default: hunter.config APPLICATIONS_DIR)",
    )
    parser.add_argument(
        "--allow-deterministic",
        action="store_true",
        help="fall back to ats_check_pdf/ats_check when a side has no independent verdict "
        "(labelled separately in the report — mixes judge-scored and deterministic numbers)",
    )
    parser.add_argument(
        "--noise-sigma",
        type=float,
        default=5.0,
        help="Haiku-judge noise sigma in pp (placeholder default — run "
        "'python tools/verdict_noise.py --n 15 --k 3' first and pass its reported "
        "sigma here for a real 'indistinguishable from noise' bucket)",
    )
    parser.add_argument("--resamples", type=int, default=2000, help="bootstrap resample count")
    parser.add_argument("--seed", type=int, default=42, help="bootstrap RNG seed (reproducibility)")
    parser.add_argument("--json", default=None, help="also dump per-pair rows + stats to this path")
    args = parser.parse_args()

    if args.root:
        root = Path(args.root)
    else:
        from hunter.config import APPLICATIONS_DIR

        root = Path(APPLICATIONS_DIR)

    print(f"[dual-pairs] scanning {root} ...")
    triples = find_pairs(root)
    print(f"[dual-pairs] {len(triples)} candidate pair(s) found")

    records: list[PairRecord] = []
    for primary_folder, shadow_folder, shadow_name in triples:
        rec = load_pair(
            primary_folder, shadow_folder, shadow_name, allow_deterministic=args.allow_deterministic
        )
        if rec is not None:
            records.append(rec)

    stats = compute_stats(
        [(r.primary_score, r.shadow_score) for r in records],
        noise_sigma=args.noise_sigma,
        resamples=args.resamples,
        seed=args.seed,
    )
    print_report(records, stats, allow_deterministic=args.allow_deterministic)

    if args.json:
        payload = {
            "stats": stats,
            "pairs": [
                {
                    "primary_folder": str(r.primary_folder),
                    "shadow_folder": str(r.shadow_folder),
                    "shadow_name": r.shadow_name,
                    "primary_score": r.primary_score,
                    "primary_source": r.primary_source,
                    "shadow_score": r.shadow_score,
                    "shadow_source": r.shadow_source,
                    "primary_cost": r.primary_cost,
                    "shadow_cost": r.shadow_cost,
                }
                for r in records
            ],
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[dual-pairs] rows + stats written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
