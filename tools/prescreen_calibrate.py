#!/usr/bin/env python3
"""Offline calibration for the stack pre-screen (docs/STACK_PRESCREEN_PLAN.md M3).

Replays `hunter.prescreen.assess_stack` over every job_posting.txt in the local
Applications/ corpus and scores it against what the full pipeline concluded
afterwards (the tracker's Stack column) and against what the owner actually did
(the Sent column, decoded by hunter.sent_parse).

The decision rule was fixed BEFORE the first run, and this tool prints its own
verdict against it:

    recall  >= 5 of the 7 known React failures        AND
    false skips == 0 among rows the owner actually sent

Fail either half and M4/M5 do not ship — the pre-screen stays unbuilt and M1/M2
carry the branch on their own.

Read-only: no tracker writes, no Sheets, no Drive, no files touched. The only
side effect is one cheap-model call per posting (~$0.0016 each on the API; on a
drained account they are served through the Claude CLI subscription instead).

Usage, on the deploy host where the corpus lives:

    docker compose exec -T job-hunter python tools/prescreen_calibrate.py
    docker compose exec -T job-hunter python tools/prescreen_calibrate.py --limit 20
    docker compose exec -T job-hunter python tools/prescreen_calibrate.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hunter.config import APPLICATIONS_DIR  # noqa: E402
from hunter.prescreen import assess_stack  # noqa: E402
from hunter.sent_parse import classify  # noqa: E402

# The seven August postings that reached generation on a React stack. Named
# explicitly so the recall half of the decision rule is checked against the
# cases that motivated the work, not against a self-selected sample.
KNOWN_REACT_FAILURES = {
    "LanceSoftEurope",
    "ITFS",
    "GeckoDynamics",
    "Aigorithmics",
    "HelloFresh",
    "LeadtechGroup",
    "Interia",
}


def _tracker_rows() -> list[dict]:
    from hunter import tracker
    from hunter.db import get_db

    with get_db(tracker.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT company, title, stack, ats_status, sent, folder, url FROM applications"
        ).fetchall()
    return [dict(r) for r in rows]


def _corpus(rows: list[dict], limit: int | None) -> list[dict]:
    """Rows whose posting text is still on disk, newest first."""
    out = []
    for r in rows:
        folder = (r.get("folder") or "").strip()
        if not folder:
            continue
        posting = Path(folder) / "job_posting.txt"
        if not posting.exists():
            posting = APPLICATIONS_DIR / folder / "job_posting.txt"
        if not posting.exists():
            continue
        try:
            text = posting.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(text.strip()) < 200:
            continue
        out.append({**r, "job_text": text})
    out.sort(key=lambda r: r.get("folder") or "", reverse=True)
    return out[:limit] if limit else out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="only the N newest postings")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="list the corpus and exit — no model calls, no cost",
    )
    ap.add_argument("--out", default="", help="write the per-posting results as JSON")
    args = ap.parse_args()

    corpus = _corpus(_tracker_rows(), args.limit or None)
    if not corpus:
        print("No postings found on disk — run this on the deploy host.")
        return 1

    known_present = {r["company"] for r in corpus} & KNOWN_REACT_FAILURES
    print(f"Corpus: {len(corpus)} postings")
    print(f"Known React failures present: {len(known_present)}/7 {sorted(known_present)}")
    if args.dry_run:
        print("\n--dry-run: no model calls made.")
        return 0

    results = []
    for i, row in enumerate(corpus, 1):
        verdict = assess_stack(row["job_text"], title=row.get("title") or "")
        results.append(
            {
                "company": row.get("company"),
                "title": row.get("title"),
                "tracker_stack": row.get("stack"),
                "sent": classify(row.get("sent") or ""),
                "was_react_failure": row.get("company") in KNOWN_REACT_FAILURES,
                "ok": verdict.ok,
                "primary_stack": verdict.primary_stack,
                "angular_required": verdict.angular_required,
                "verdict": verdict.verdict,
                "confidence": verdict.confidence,
                "evidence": verdict.evidence[:160],
            }
        )
        flag = "MISMATCH" if verdict.is_mismatch else ("fit" if verdict.ok else "unusable")
        print(
            f"  [{i}/{len(corpus)}] {(row.get('company') or '?')[:24]:24} "
            f"tracker={str(row.get('stack'))[:12]:12} -> {verdict.primary_stack:10} "
            f"{flag:9} conf={verdict.confidence:.2f}"
        )

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False), "utf-8")
        print(f"\nWrote {args.out}")

    # ── the decision rule ────────────────────────────────────────────────
    caught = [
        r for r in results if r["was_react_failure"] and r["verdict"] == "mismatch" and r["ok"]
    ]
    false_skips = [
        r for r in results if r["verdict"] == "mismatch" and r["ok"] and r["sent"] == "applied"
    ]
    unusable = [r for r in results if not r["ok"]]

    print("\n" + "=" * 68)
    print("DECISION RULE (fixed before this ran)")
    print("=" * 68)
    print(f"  recall on known React failures : {len(caught)}/{len(known_present)}  (need >= 5)")
    print(f"  false skips among SENT rows    : {len(false_skips)}       (need 0)")
    print(f"  unusable verdicts (no quote)   : {len(unusable)}/{len(results)}")
    for r in false_skips:
        print(f"    FALSE SKIP: {r['company']} — {r['title']} (evidence: {r['evidence'][:80]})")

    passed = len(caught) >= 5 and not false_skips
    print("\n  VERDICT:", "PASS — M4/M5 may ship" if passed else "FAIL — close M4/M5, keep M1/M2")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
