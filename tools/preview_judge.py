"""
tools/preview_judge.py — Run the claim-judge (+ deterministic scrubs) against an
already-generated content.json, without regenerating the CV.

This is the M3 follow-up verifier from docs/CV_JUDGE_PLAN.md: it lets you check
the judge on a real CLI-generated CV with a single cheap Haiku call (no expensive
generation tokens — the CV is already on disk). It applies the same scrubs the
pipeline runs, calls judge_content, then repair_content, and prints a diff of
what the judge flagged and what the repair changed.

Usage:
    python tools/preview_judge.py <path/to/content.json> [path/to/job_posting.txt]

If the job-posting file is omitted, the tool looks for job_posting.txt next to
content.json, else runs with empty posting text (profile-only grounding).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Force UTF-8 output on Windows (console defaults to cp1252 → emoji crash).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))


def _flatten(content: dict) -> dict[str, str]:
    from hunter.claim_judge import iter_judged_fields

    return iter_judged_fields(content)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python tools/preview_judge.py <content.json> [job_posting.txt]")
        sys.exit(1)

    content_path = Path(sys.argv[1])
    if not content_path.exists():
        print(f"[preview-judge] ERROR: {content_path} not found")
        sys.exit(1)
    content = json.loads(content_path.read_text(encoding="utf-8"))

    job_path = Path(sys.argv[2]) if len(sys.argv) >= 3 else content_path.parent / "job_posting.txt"
    job_text = job_path.read_text(encoding="utf-8") if job_path.exists() else ""
    print(f"[preview-judge] content: {content_path}")
    print(
        f"[preview-judge] job text: {job_path if job_path.exists() else '(none)'} "
        f"({len(job_text)} chars)\n"
    )

    # Parity scrubs (same order as the pipeline) — so the judge sees post-scrub text.
    from hunter.apply_shared import _dedup_skill_glosses, _strip_prestige_claims

    content, pf = _strip_prestige_claims(content, job_text)
    content, gf = _dedup_skill_glosses(content)
    for line in pf + gf:
        print(f"[scrub] {line}")

    # Mirror the real pipeline: mode-aware stage (default warn = repair
    # fabrications only, surface exaggerations). Override with JUDGE_MODE env.
    import os

    from hunter.claim_judge import run_judge_stage

    mode = os.getenv("JUDGE_MODE", "warn").strip().lower()
    before = _flatten(content)
    outcome = run_judge_stage(content, job_text, enabled=True, mode=mode)
    report = outcome.report

    print(
        f"\n[preview-judge] mode={mode} | {len(report.violations)} finding(s) "
        f"({len(report.actionable)} actionable):"
    )
    for v in report.violations:
        print(f"  • [{v.severity}] {v.field}")
        print(f"      quote : {v.quote!r}")
        print(f"      reason: {v.reason}")

    if not report.actionable:
        print("\n[preview-judge] No actionable findings — CV is clean. ✅")
        return

    print(
        f"\n[preview-judge] repair applied {len(outcome.fixes)} fix(es) "
        f"(fabrication-only in warn/block):"
    )
    for f in outcome.fixes:
        print(f"  - {f}")

    after = _flatten(outcome.content)
    print("\n[preview-judge] changed fields:")
    changed = False
    for k in sorted(set(before) | set(after)):
        if before.get(k) != after.get(k):
            changed = True
            print(f"  ~ {k}")
            print(f"      before: {before.get(k)!r}")
            print(f"      after : {after.get(k)!r}")
    if not changed:
        print("  (none — report mode or no fabrication repaired)")

    if outcome.blocked:
        print(
            f"\n[preview-judge] ⛔ BLOCKED — {len(outcome.survivors)} fabrication(s) "
            f"survived repair:"
        )
        for v in outcome.survivors:
            print(f"  • {v.field}: {v.quote!r}")
    elif outcome.survivors:
        print(
            f"\n[preview-judge] ⚠️ {len(outcome.survivors)} fabrication(s) survived "
            f"(would block in JUDGE_MODE=block)."
        )
    else:
        print("\n[preview-judge] All fabrications repaired. ✅")


if __name__ == "__main__":
    main()
