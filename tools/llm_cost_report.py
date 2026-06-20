"""LLM cost report — read every content.json under Applications/ and aggregate.

Usage:
    python tools/llm_cost_report.py Applications/
    python tools/llm_cost_report.py Applications/ --since 2026-06-01
    python tools/llm_cost_report.py Applications/ --by-day
    python tools/llm_cost_report.py Applications/ --json     # raw rows

Reads content["cost"] written by hunter.apply_api (and the mode=cli stub from
hunter.apply_cli). Rows with no cost block are reported as "(not measured)".
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _iter_apply_folders(root: Path):
    """Yield every apply folder under root (one or two levels deep)."""
    if (root / "content.json").exists():
        yield root
        return
    for child in sorted(root.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        if (child / "content.json").exists():
            yield child
            continue
        for sub in sorted(child.iterdir(), reverse=True):
            if sub.is_dir() and (sub / "content.json").exists():
                yield sub


def _load_cost(folder: Path) -> dict | None:
    """Return the cost dict (with extra `_folder`, `_date` keys) or None."""
    try:
        content = json.loads((folder / "content.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    cost = content.get("cost") if isinstance(content.get("cost"), dict) else None
    if cost is None:
        return None
    return {
        **cost,
        "_folder": str(folder.name),
        "_date": folder.parent.name if len(folder.parent.name) == 10 else "",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", type=Path, help="Applications/ or a single apply folder")
    ap.add_argument("--since", type=str, default="",
                    help="only consider date folders >= YYYY-MM-DD")
    ap.add_argument("--by-day", action="store_true", help="aggregate by day")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args()

    if not args.path.exists():
        print(f"error: {args.path} does not exist", file=sys.stderr)
        return 2

    rows: list[dict] = []
    no_cost = 0
    cli_runs = 0
    for folder in _iter_apply_folders(args.path):
        if args.since and folder.parent.name < args.since:
            continue
        cost = _load_cost(folder)
        if cost is None:
            no_cost += 1
            continue
        if cost.get("mode") == "cli":
            cli_runs += 1
            continue
        if cost.get("total_usd") is None:
            cli_runs += 1
            continue
        rows.append(cost)

    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        return 0

    if not rows:
        print("no priced rows found", file=sys.stderr)
        if no_cost or cli_runs:
            print(f"({no_cost} folders without cost record, {cli_runs} CLI-mode runs)",
                  file=sys.stderr)
        return 1

    if args.by_day:
        by_day: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            by_day[r.get("_date") or "?"].append(float(r["total_usd"]))
        print(f"{'date':12s}  count   total      avg       max")
        print("-" * 56)
        grand = 0.0
        for day in sorted(by_day):
            day_total = sum(by_day[day])
            grand += day_total
            print(f"{day:12s}  {len(by_day[day]):5d}  ${day_total:7.2f}  "
                  f"${day_total/len(by_day[day]):7.4f}  ${max(by_day[day]):7.4f}")
        print("-" * 56)
        print(f"{'GRAND':12s}  {len(rows):5d}  ${grand:7.2f}")
    else:
        print(f"{'folder':45s}  cost      calls   tokens (in/out/cache_r/cache_w)")
        print("-" * 110)
        for r in sorted(rows, key=lambda r: -float(r["total_usd"]))[:30]:
            print(
                f"{(r.get('_date') + '/' + r.get('_folder',''))[:45]:45s}  "
                f"${float(r['total_usd']):7.4f}  "
                f"{r.get('calls', 0):4d}    "
                f"{r.get('input_tokens', 0):>6} / {r.get('output_tokens', 0):>6} / "
                f"{r.get('cache_read_tokens', 0):>6} / {r.get('cache_write_tokens', 0):>6}"
            )

    totals = [float(r["total_usd"]) for r in rows]
    print("\n--- summary ---")
    print(f"folders with cost: {len(totals)}  (CLI runs skipped: {cli_runs}, "
          f"no cost record: {no_cost})")
    print(f"total spend: ${sum(totals):.2f}")
    print(f"per vacancy: min=${min(totals):.4f}  median=${statistics.median(totals):.4f}  "
          f"max=${max(totals):.4f}  avg=${sum(totals)/len(totals):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
