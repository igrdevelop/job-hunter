"""
tools/verdict_noise.py — how much does the independent ATS verdict wobble on
an UNCHANGED input?

docs/LLM_COST_REDUCTION_PLAN.md M2: ATS_VERDICT_TARGET=95 against real
verdicts of 72-94 fires the (expensive) refine loop on almost every
generation. Before anyone touches the target, we need to know how much of
that gap is real quality headroom vs. Haiku judge noise — a target within
noise range of the typical score is paying for randomness, not quality.

Re-runs `hunter.ats_pdf_roundtrip.run_llm_verdict` --k times on each of the
last --n Applications/ folders that have both a rendered EN CV PDF and a
saved job_posting.txt (same inputs every time — the PDF/posting are already
on disk, nothing is regenerated). Reports per-folder min/max/spread and an
overall population std-dev of the within-folder deviations.

Cost: ~n*k Haiku calls (JUDGE_MODEL), roughly $0.01-0.02 each — a run with
the defaults (n=10, k=3) is ~30 calls, well under $1. Read-only: no writes
to tracker/Sheets/content.json.

Usage:
    python tools/verdict_noise.py [--n 10] [--k 3] [--dir Applications]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def find_candidate_folders(root: Path, limit: int) -> list[Path]:
    """Application folders with both an EN CV PDF and job_posting.txt,
    newest first (by folder mtime)."""
    from hunter.ats_pdf_roundtrip import find_en_cv_pdf

    candidates: list[Path] = []
    if not root.exists():
        return candidates
    for posting in root.rglob("job_posting.txt"):
        folder = posting.parent
        if find_en_cv_pdf(folder) is not None:
            candidates.append(folder)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[:limit]


def measure_folder(folder: Path, k: int) -> list[float]:
    """Run the verdict k times on the same folder/job text. Returns the list
    of scores obtained (skips any call that returns None/no signal)."""
    from hunter.ats_pdf_roundtrip import run_llm_verdict

    job_text = (folder / "job_posting.txt").read_text(encoding="utf-8", errors="replace")
    scores: list[float] = []
    for _ in range(k):
        verdict = run_llm_verdict(folder=folder, job_text=job_text)
        if isinstance(verdict, dict) and verdict.get("score") is not None:
            scores.append(float(verdict["score"]))
    return scores


def summarize(per_folder: dict[str, list[float]]) -> str:
    """Pure formatting over already-measured scores, so it's testable
    without any LLM calls."""
    lines = []
    all_deviations: list[float] = []
    for name, scores in per_folder.items():
        if len(scores) < 2:
            lines.append(f"{name}: only {len(scores)} usable score(s) — skipped")
            continue
        lo, hi = min(scores), max(scores)
        mean = statistics.mean(scores)
        spread = hi - lo
        lines.append(
            f"{name}: n={len(scores)} min={lo:.1f} max={hi:.1f} "
            f"mean={mean:.1f} spread={spread:.1f}pp"
        )
        all_deviations.extend(s - mean for s in scores)

    if len(all_deviations) >= 2:
        sigma = statistics.pstdev(all_deviations)
        lines.append(
            f"\nJudge noise σ={sigma:.1f}pp. A target within σ*2 ({sigma * 2:.1f}pp) "
            "of typical scores buys noise, not quality."
        )
    else:
        lines.append("\nNot enough usable scores to estimate noise (need >=2 folders with n>=2).")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=10, help="how many recent folders to sample")
    parser.add_argument("--k", type=int, default=3, help="how many times to re-score each folder")
    parser.add_argument("--dir", default="Applications", help="applications root directory")
    args = parser.parse_args()

    root = Path(args.dir)
    folders = find_candidate_folders(root, args.n)
    if not folders:
        print(f"[verdict_noise] no usable folders (EN CV PDF + job_posting.txt) found under {root}")
        return

    print(f"[verdict_noise] sampling {len(folders)} folder(s) x {args.k} verdict call(s) "
          f"= up to {len(folders) * args.k} Haiku calls")

    per_folder: dict[str, list[float]] = {}
    for folder in folders:
        label = f"{folder.parent.name}/{folder.name}"
        scores = measure_folder(folder, args.k)
        per_folder[label] = scores
        print(f"  {label}: {scores}")

    print()
    print(summarize(per_folder))


if __name__ == "__main__":
    main()
