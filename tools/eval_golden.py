"""
tools/eval_golden.py — offline eval harness for prompt/model changes, $0 by
default.

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M2: before changing a prompt or
swapping a generator model, this measures whether the new content.json is
actually better than the old one, on a fixed golden set, without spending on
an LLM judge unless explicitly asked.

Two subcommands:

  build   Sample a stratified (source x posting-language x track) golden set
          of application folders from the local Applications/ corpus and
          write it to tests/fixtures/golden_set.json — ONLY relative paths
          and sha256(job_posting.txt), never posting text (the corpus stays
          on the deploy host; the fixture is safe to commit).

  score   Compare two generator runs over the SAME golden set: --baseline DIR
          and --candidate DIR each hold "<relative_path>/content.json"
          (produced however you like — see "Generating candidate runs"
          below). Computes $0 deterministic metrics per folder
          (hunter.ats_checker score, hunter.lang_guard contamination hits,
          hunter.pipeline.validate errors, hunter.content_qa warnings, role/
          bullet counts, resume text length), then a PAIRED comparison:
          mean delta with a bootstrap 95% CI, a sign test, and the plan's
          accept/reject gate. Optional --judge/--verdict add paid LLM
          metrics (hunter.claim_judge / hunter.ats_checker.llm_verdict) —
          OFF by default; the estimated cost is printed and --yes is
          required before any call is made.

Generating candidate runs (not part of this tool): run the pipeline against
each golden-set posting and collect the resulting content.json under
"eval_runs/<tag>/<relative_path>/content.json" — e.g. per-folder via
    python tools/preview_apply.py <job_posting.txt> --cli
or by pointing a modified prompts/generation_rules.md / model profile at the
real apply pipeline for each golden URL and copying the output folder's
content.json into eval_runs/<tag>/<relative_path>/.

Usage (run where the Applications/ corpus lives — the deploy host):
    python tools/eval_golden.py build --n 50
    python tools/eval_golden.py score --baseline eval_runs/before --candidate eval_runs/after
    docker compose exec -T job-hunter python tools/eval_golden.py score \\
        --baseline eval_runs/before --candidate eval_runs/after --judge --verdict --yes
"""

from __future__ import annotations

import argparse
import hashlib
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

DEFAULT_GOLDEN_PATH = PROJECT_DIR / "tests" / "fixtures" / "golden_set.json"

# Rough per-call cost, same order of magnitude tools/verdict_noise.py quotes
# for JUDGE_MODEL (Haiku) calls — used only to print an estimate before an
# optional paid run, never to bill anything.
_EST_JUDGE_COST_USD = 0.02
_EST_VERDICT_COST_USD = 0.015


# ── Pure helpers: text / role-family-free track guess ──────────────────────


def _guess_track(job_text: str) -> str:
    """Cheap, dependency-free track guess for stratification only (NOT the
    production _BASE_CV_FILES stack hint — this only needs to be a stable
    bucket key, not perfectly accurate)."""
    text = (job_text or "").lower()
    has_next = "next.js" in text or "nextjs" in text
    has_nest = "nestjs" in text or "nest.js" in text
    has_angular = "angular" in text
    has_react = "react" in text
    if has_next or has_nest:
        return "fullstack"
    if has_angular:
        return "angular"
    if has_react:
        return "react"
    return "other"


def _resume_text(content: dict) -> str:
    """Flatten resume_en the same way tools/reuse_calibrate.py does."""
    resume_en = content.get("resume_en", "")
    if not resume_en:
        return ""
    if isinstance(resume_en, dict):
        return json.dumps(resume_en, ensure_ascii=False)
    return str(resume_en)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── build: pure sampling ─────────────────────────────────────────────────────


def stratified_sample(entries: list[dict], n: int, *, seed: int = 42) -> list[dict]:
    """Round-robin sample across (source, lang, track) strata, up to `n`.

    Pure function: `entries` must already carry 'source'/'lang'/'track' keys.
    Deterministic given the same seed (each stratum is shuffled, then strata
    are visited in sorted-key order, one item per round).
    """
    if n >= len(entries):
        return list(entries)
    rng = random.Random(seed)
    groups: dict[tuple, list[dict]] = {}
    for e in entries:
        key = (e.get("source", "?"), e.get("lang", "?"), e.get("track", "?"))
        groups.setdefault(key, []).append(e)
    for g in groups.values():
        rng.shuffle(g)

    keys = sorted(groups.keys())
    selected: list[dict] = []
    idx = 0
    while len(selected) < n:
        progressed = False
        for k in keys:
            if idx < len(groups[k]):
                selected.append(groups[k][idx])
                progressed = True
                if len(selected) >= n:
                    break
        if not progressed:
            break
        idx += 1
    return selected


# ── build: I/O ───────────────────────────────────────────────────────────────


def collect_entries(root: Path) -> list[dict]:
    """Walk `root` for primary application folders (shadow subfolders of a
    known llm_profiles name excluded) with both content.json and
    job_posting.txt. Returns strata-annotated dicts (no golden-set fields
    yet — those are added by build_golden_set)."""
    from hunter.funnel import source_for_url
    from hunter.llm_profiles import PROFILES

    profile_names = set(PROFILES)
    entries: list[dict] = []
    if not root.exists():
        return entries

    for content_path in sorted(root.rglob("content.json")):
        folder = content_path.parent
        if folder.name in profile_names:
            continue
        posting_path = folder / "job_posting.txt"
        if not posting_path.exists():
            continue
        try:
            content = json.loads(content_path.read_text(encoding="utf-8"))
            job_text = posting_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, json.JSONDecodeError):
            continue

        lang = content.get("primary_lang")
        if lang not in ("EN", "PL"):
            from hunter.lang_guard import detect_posting_language

            lang = detect_posting_language(job_text)

        entries.append(
            {
                "relative_path": folder.relative_to(root).as_posix(),
                "sha256": sha256_text(job_text),
                "source": source_for_url(content.get("apply_url", "")),
                "lang": lang,
                "track": _guess_track(job_text),
            }
        )
    return entries


def build_golden_set(root: Path, n: int, *, seed: int = 42) -> list[dict]:
    entries = collect_entries(root)
    sample = stratified_sample(entries, n, seed=seed)
    return sorted(sample, key=lambda e: e["relative_path"])


def write_golden_set(entries: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


# ── score: pure statistics (shared shape with tools/dual_pairs_stats.py) ────


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
    n = pos + neg
    if n == 0:
        return 1.0
    k = min(pos, neg)
    cum = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, 2 * cum / (2**n))


# ── score: $0 per-folder metrics ────────────────────────────────────────────


@dataclass
class SideMetrics:
    det_score: float
    keyword_score: float
    lang_hits: int
    validation_errors: int
    qa_warnings: int
    role_count: int
    bullet_count: int
    text_length: int


def evaluate_side(content: dict, job_text: str) -> SideMetrics:
    from hunter import ats_checker
    from hunter.content_qa import run_qa
    from hunter.lang_guard import scan_content
    from hunter.pipeline.validate import validate_content

    resume_text = _resume_text(content)
    det = ats_checker.check(job_text=job_text, resume_text=resume_text, run_llm_review=False)

    scan = scan_content(content)
    lang_hits = sum(len(bucket) for bucket in scan.values())

    # pl_optional=True: an EN posting legitimately omits the _pl fields
    # (GEN_SKIP_PL_FOR_EN) — comparing baseline vs candidate must not
    # penalize BOTH sides for a deliberate, unrelated behavior.
    validation_errors = len(validate_content(content, pl_optional=True))
    qa_warnings = len(run_qa(content).failed_checks)

    resume_en = content.get("resume_en") or {}
    experience = resume_en.get("experience") or []
    role_count = len(experience) if isinstance(experience, list) else 0
    bullet_count = 0
    if isinstance(experience, list):
        for role in experience:
            if isinstance(role, dict):
                bullet_count += len(role.get("bullets") or [])

    return SideMetrics(
        det_score=det.score,
        keyword_score=det.keyword_score,
        lang_hits=lang_hits,
        validation_errors=validation_errors,
        qa_warnings=qa_warnings,
        role_count=role_count,
        bullet_count=bullet_count,
        text_length=len(resume_text),
    )


@dataclass
class FolderResult:
    relative_path: str
    baseline: SideMetrics
    candidate: SideMetrics


def load_golden_set(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def score_folders(
    golden: list[dict], root: Path, baseline_dir: Path, candidate_dir: Path
) -> tuple[list[FolderResult], list[str]]:
    """Returns (results, skip_reasons). Pure boundary: takes already-resolved
    directories, does its own (read-only) file I/O."""
    results: list[FolderResult] = []
    skipped: list[str] = []

    for entry in golden:
        rel = entry["relative_path"]
        posting_path = root / rel / "job_posting.txt"
        if not posting_path.exists():
            skipped.append(f"{rel}: job_posting.txt missing under --root")
            continue
        job_text = posting_path.read_text(encoding="utf-8", errors="replace")
        actual_hash = sha256_text(job_text)
        if actual_hash != entry.get("sha256"):
            skipped.append(f"{rel}: sha256 mismatch — corpus changed since golden set was built")
            continue

        baseline_cj = baseline_dir / rel / "content.json"
        candidate_cj = candidate_dir / rel / "content.json"
        if not baseline_cj.exists():
            skipped.append(f"{rel}: missing in --baseline")
            continue
        if not candidate_cj.exists():
            skipped.append(f"{rel}: missing in --candidate")
            continue
        try:
            baseline_content = json.loads(baseline_cj.read_text(encoding="utf-8"))
            candidate_content = json.loads(candidate_cj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            skipped.append(f"{rel}: unreadable content.json ({e})")
            continue

        results.append(
            FolderResult(
                relative_path=rel,
                baseline=evaluate_side(baseline_content, job_text),
                candidate=evaluate_side(candidate_content, job_text),
            )
        )
    return results, skipped


def compute_gate(results: list[FolderResult], *, resamples: int = 2000, seed: int = 42) -> dict:
    """Pure aggregation + the plan's accept/reject rule:
    accept if CI-low of mean delta(det_score) >= -1pp AND
             candidate lang-gate hits did not increase AND
             candidate validation errors == 0.
    """
    n = len(results)
    if n == 0:
        return {"n": 0, "accepted": False, "reason": "no scored folders"}

    det_diffs = [r.candidate.det_score - r.baseline.det_score for r in results]
    kw_diffs = [r.candidate.keyword_score - r.baseline.keyword_score for r in results]
    wins = sum(1 for d in det_diffs if d > 0)
    losses = sum(1 for d in det_diffs if d < 0)

    det_ci = bootstrap_mean_ci(det_diffs, resamples=resamples, seed=seed)
    total_lang_baseline = sum(r.baseline.lang_hits for r in results)
    total_lang_candidate = sum(r.candidate.lang_hits for r in results)
    total_val_candidate = sum(r.candidate.validation_errors for r in results)
    total_val_baseline = sum(r.baseline.validation_errors for r in results)

    det_ci_ok = det_ci[0] >= -1.0
    lang_ok = total_lang_candidate <= total_lang_baseline
    validation_ok = total_val_candidate == 0

    return {
        "n": n,
        "mean_det_delta": sum(det_diffs) / n,
        "det_delta_ci95": det_ci,
        "mean_keyword_delta": sum(kw_diffs) / n,
        "keyword_delta_ci95": bootstrap_mean_ci(kw_diffs, resamples=resamples, seed=seed),
        "sign_test_p": sign_test_p(wins, losses),
        "wins": wins,
        "losses": losses,
        "lang_hits_baseline": total_lang_baseline,
        "lang_hits_candidate": total_lang_candidate,
        "validation_errors_baseline": total_val_baseline,
        "validation_errors_candidate": total_val_candidate,
        "qa_warnings_baseline": sum(r.baseline.qa_warnings for r in results),
        "qa_warnings_candidate": sum(r.candidate.qa_warnings for r in results),
        "gate": {
            "det_score_ci_ok": det_ci_ok,
            "lang_hits_not_increased": lang_ok,
            "validation_errors_zero": validation_ok,
        },
        "accepted": det_ci_ok and lang_ok and validation_ok,
    }


# ── score: optional paid metrics ────────────────────────────────────────────


def estimate_paid_cost(n_folders: int, *, judge: bool, verdict: bool) -> float:
    cost = 0.0
    if judge:
        cost += n_folders * 2 * _EST_JUDGE_COST_USD
    if verdict:
        cost += n_folders * 2 * _EST_VERDICT_COST_USD
    return cost


def run_paid_metrics(
    results: list[FolderResult],
    golden: list[dict],
    root: Path,
    baseline_dir: Path,
    candidate_dir: Path,
    *,
    judge: bool,
    verdict: bool,
) -> dict:
    """Best-effort: any single call failure is skipped, never raises."""
    from hunter import ats_checker
    from hunter.config import JUDGE_API_KEY, JUDGE_MODEL, JUDGE_PROVIDER

    out: dict[str, dict] = {"judge": {}, "verdict": {}}
    for r in results:
        posting_path = root / r.relative_path / "job_posting.txt"
        job_text = posting_path.read_text(encoding="utf-8", errors="replace")
        for label, side_dir in (("baseline", baseline_dir), ("candidate", candidate_dir)):
            content_path = side_dir / r.relative_path / "content.json"
            try:
                content = json.loads(content_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            if judge:
                try:
                    from hunter.claim_judge import judge_content

                    report = judge_content(content, job_text)
                    out["judge"].setdefault(label, []).append(len(report.actionable))
                except Exception as e:  # noqa: BLE001 — best-effort optional metric
                    print(f"[eval-golden] judge call failed for {r.relative_path}/{label}: {e}")

            if verdict:
                try:
                    v = ats_checker.llm_verdict(
                        job_text,
                        _resume_text(content),
                        provider=JUDGE_PROVIDER,
                        model=JUDGE_MODEL,
                        api_key=JUDGE_API_KEY,
                    )
                    if v is not None:
                        out["verdict"].setdefault(label, []).append(float(v["score"]))
                except Exception as e:  # noqa: BLE001
                    print(f"[eval-golden] verdict call failed for {r.relative_path}/{label}: {e}")
    return out


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cmd_build(args: argparse.Namespace) -> int:
    if args.root:
        root = Path(args.root)
    else:
        from hunter.config import APPLICATIONS_DIR

        root = Path(APPLICATIONS_DIR)
    if not root.is_dir():
        print(f"[eval-golden] corpus dir not found: {root}")
        return 1

    print(f"[eval-golden] scanning {root} ...")
    entries = collect_entries(root)
    print(f"[eval-golden] {len(entries)} candidate folder(s) found")
    if not entries:
        print("[eval-golden] nothing to sample")
        return 1

    sample = stratified_sample(entries, args.n, seed=args.seed)
    sample = sorted(sample, key=lambda e: e["relative_path"])

    strata = {}
    for e in sample:
        key = (e["source"], e["lang"], e["track"])
        strata[key] = strata.get(key, 0) + 1
    print(f"[eval-golden] sampled {len(sample)} folder(s) across {len(strata)} strata:")
    for key, count in sorted(strata.items()):
        print(f"  {key}: {count}")

    out_path = Path(args.out) if args.out else DEFAULT_GOLDEN_PATH
    write_golden_set(sample, out_path)
    print(f"[eval-golden] golden set written to {out_path} (paths + sha256 only, no text)")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    golden_path = Path(args.golden) if args.golden else DEFAULT_GOLDEN_PATH
    if not golden_path.exists():
        print(
            f"[eval-golden] golden set not found: {golden_path} (run the 'build' subcommand first)"
        )
        return 1
    golden = load_golden_set(golden_path)

    if args.root:
        root = Path(args.root)
    else:
        from hunter.config import APPLICATIONS_DIR

        root = Path(APPLICATIONS_DIR)

    baseline_dir = Path(args.baseline)
    candidate_dir = Path(args.candidate)

    results, skipped = score_folders(golden, root, baseline_dir, candidate_dir)
    print(f"[eval-golden] scored {len(results)}/{len(golden)} folder(s)")
    for s in skipped:
        print(f"  skip: {s}")

    gate = compute_gate(results, resamples=args.resamples, seed=args.seed)
    print("\n=== Deterministic ($0) comparison ===")
    if gate["n"] == 0:
        print("No scored folders — nothing to compare.")
        return 1

    lo, hi = gate["det_delta_ci95"]
    print(
        f"mean det score delta (candidate-baseline): {gate['mean_det_delta']:+.2f}pp"
        f"  (95% CI {lo:+.2f}pp to {hi:+.2f}pp)"
    )
    lo, hi = gate["keyword_delta_ci95"]
    print(
        f"mean keyword score delta                 : {gate['mean_keyword_delta']:+.2f}pp"
        f"  (95% CI {lo:+.2f}pp to {hi:+.2f}pp)"
    )
    print(
        f"sign test p-value: {gate['sign_test_p']:.4f}  (wins={gate['wins']} losses={gate['losses']})"
    )
    print(
        f"lang-gate hits   : baseline={gate['lang_hits_baseline']} candidate={gate['lang_hits_candidate']}"
    )
    print(
        f"validation errors: baseline={gate['validation_errors_baseline']} "
        f"candidate={gate['validation_errors_candidate']}"
    )
    print(
        f"QA warnings      : baseline={gate['qa_warnings_baseline']} "
        f"candidate={gate['qa_warnings_candidate']}"
    )

    print("\n=== Gate ===")
    for k, v in gate["gate"].items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    print(f"  => {'ACCEPT' if gate['accepted'] else 'REJECT'} candidate")

    paid = None
    if args.judge or args.verdict:
        est = estimate_paid_cost(len(results), judge=args.judge, verdict=args.verdict)
        print(f"\n[eval-golden] estimated paid-metric cost: ~${est:.2f} ({len(results)} folder(s))")
        if not args.yes:
            print("[eval-golden] pass --yes to actually run judge/verdict calls")
        else:
            paid = run_paid_metrics(
                results,
                golden,
                root,
                baseline_dir,
                candidate_dir,
                judge=args.judge,
                verdict=args.verdict,
            )
            for metric, sides in paid.items():
                if not sides:
                    continue
                print(f"\n--- {metric} ---")
                for label, values in sides.items():
                    if values:
                        print(f"  {label}: n={len(values)} mean={sum(values) / len(values):.2f}")

    if args.json:
        payload = {
            "gate": gate,
            "skipped": skipped,
            "folders": [
                {
                    "relative_path": r.relative_path,
                    "baseline": vars(r.baseline),
                    "candidate": vars(r.candidate),
                }
                for r in results
            ],
        }
        if paid is not None:
            payload["paid"] = paid
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[eval-golden] results written to {args.json}")

    return 0 if gate["accepted"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="sample a stratified golden set")
    p_build.add_argument(
        "--root", default=None, help="Applications corpus root (default APPLICATIONS_DIR)"
    )
    p_build.add_argument("--n", type=int, default=50, help="target sample size (default 50)")
    p_build.add_argument("--seed", type=int, default=42, help="sampling RNG seed")
    p_build.add_argument("--out", default=None, help=f"output path (default {DEFAULT_GOLDEN_PATH})")
    p_build.set_defaults(func=_cmd_build)

    p_score = sub.add_parser("score", help="compare two generator runs over the golden set")
    p_score.add_argument(
        "--golden", default=None, help=f"golden set path (default {DEFAULT_GOLDEN_PATH})"
    )
    p_score.add_argument(
        "--root", default=None, help="Applications corpus root (for job_posting.txt)"
    )
    p_score.add_argument(
        "--baseline", required=True, help="dir holding <relative_path>/content.json"
    )
    p_score.add_argument(
        "--candidate", required=True, help="dir holding <relative_path>/content.json"
    )
    p_score.add_argument("--resamples", type=int, default=2000, help="bootstrap resample count")
    p_score.add_argument("--seed", type=int, default=42, help="bootstrap RNG seed")
    p_score.add_argument(
        "--judge", action="store_true", help="also run claim_judge (paid, needs --yes)"
    )
    p_score.add_argument(
        "--verdict",
        action="store_true",
        help="also run ats_checker.llm_verdict (paid, needs --yes)",
    )
    p_score.add_argument("--yes", action="store_true", help="confirm running the paid calls above")
    p_score.add_argument("--json", default=None, help="write full results to this path")
    p_score.set_defaults(func=_cmd_score)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
