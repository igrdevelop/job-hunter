"""ATS PDF roundtrip — diagnostic CLI.

Walks one apply folder (or a directory of them) and re-scores each rendered
EN CV PDF against the saved job_posting.txt. Prints a per-folder table and
aggregate stats so we can spot generate_docs regressions early.

Usage:
    # Score one folder
    python tools/ats_pdf_roundtrip.py Applications/2026-06-19/Acronis

    # Walk every dated folder under Applications/
    python tools/ats_pdf_roundtrip.py Applications/

    # Same but limit to N most recent apply folders
    python tools/ats_pdf_roundtrip.py Applications/ --limit 30

Heuristic-only (keyword match + TF-IDF) — no LLM calls, runs offline.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

# Project root on path so `hunter.*` resolves when run as `python tools/...`.
sys.path.insert(0, str(Path(__file__).parent.parent))

from hunter.ats_pdf_roundtrip import run_pdf_roundtrip  # noqa: E402


def _iter_apply_folders(root: Path) -> list[Path]:
    """Yield apply folders under `root`.

    If root itself looks like an apply folder (has content.json), yield it.
    Otherwise walk one level (date dir) or two levels (Applications/date/app).
    """
    if (root / "content.json").exists():
        return [root]
    out: list[Path] = []
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        if (child / "content.json").exists():
            out.append(child)
            continue
        # date dir
        for sub in sorted(child.iterdir(), reverse=True):
            if sub.is_dir() and (sub / "content.json").exists():
                out.append(sub)
    return out


def _job_text_for(folder: Path) -> str:
    """Read job_posting.txt; strip the leading 'URL: ...' header line."""
    p = folder / "job_posting.txt"
    if not p.exists():
        return ""
    raw = p.read_text(encoding="utf-8", errors="replace")
    if raw.startswith("URL:"):
        raw = raw.split("\n", 2)[-1]
    return raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="apply folder or directory of them")
    ap.add_argument("--limit", type=int, default=0, help="cap folders processed (0 = no cap)")
    ap.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON instead of a table"
    )
    args = ap.parse_args()

    if not args.path.exists():
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2

    folders = _iter_apply_folders(args.path)
    if args.limit:
        folders = folders[: args.limit]
    if not folders:
        print(f"no apply folders found under {args.path}", file=sys.stderr)
        return 1

    rows: list[dict] = []
    for folder in folders:
        job_text = _job_text_for(folder)
        if not job_text.strip():
            rows.append({"folder": str(folder), "error": "no job_posting.txt"})
            continue
        try:
            content = json.loads((folder / "content.json").read_text(encoding="utf-8"))
        except Exception as e:
            rows.append({"folder": str(folder), "error": f"content.json: {e}"})
            continue
        pdf_check = run_pdf_roundtrip(
            folder=folder,
            job_text=job_text,
            json_ats_score=content.get("ats_score"),
        )
        if pdf_check is None:
            rows.append({"folder": str(folder), "error": "no PDF or extraction failed"})
            continue
        rows.append(
            {
                "folder": str(
                    folder.relative_to(args.path) if args.path in folder.parents else folder
                ),
                "pdf_score": pdf_check["score"],
                "json_score": content.get("ats_score"),
                "delta": pdf_check.get("delta_from_json"),
                "missing": pdf_check.get("missing_keywords", [])[:10],
            }
        )

    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        return 0

    print(f"{'folder':50s}  json   pdf   Δ    missing")
    print("-" * 110)
    for r in rows:
        if "error" in r:
            print(f"{r['folder'][:50]:50s}  {r['error']}")
            continue
        delta = r["delta"]
        delta_s = f"{delta:+.1f}" if delta is not None else "  —  "
        json_s = f"{r['json_score']:.1f}" if isinstance(r["json_score"], (int, float)) else " ?  "
        miss = ", ".join(r["missing"][:5])
        print(f"{r['folder'][:50]:50s}  {json_s:>5}  {r['pdf_score']:5.1f}  {delta_s:>5}  {miss}")

    deltas = [r["delta"] for r in rows if isinstance(r.get("delta"), (int, float))]
    if deltas:
        print(
            f"\nΔ stats over {len(deltas)} folders: "
            f"min={min(deltas):+.1f}  median={statistics.median(deltas):+.1f}  max={max(deltas):+.1f}"
        )
        n_bad = sum(1 for d in deltas if d <= -5)
        print(f"folders with PDF score ≥5pp BELOW JSON: {n_bad}/{len(deltas)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
