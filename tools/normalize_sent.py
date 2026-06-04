"""
CLI for the Sent → clean-date normalizer (see :mod:`hunter.sent_normalizer`).

Reads every row of the Sheet, extracts a real application date out of the messy
``Sent`` column, and writes it — as a real date — into column L "Applied Date".
The original ``Sent`` column and the synced A–K area are left untouched.

Run inside the container (reuses the bot's gsheets_token.json):

    # dry run — prints how many dates were found, writes nothing (default):
    docker compose exec job-hunter python tools/normalize_sent.py

    # actually write column L:
    docker compose exec job-hunter python tools/normalize_sent.py --apply
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hunter import gsheets_sync
from hunter.gsheets_client import read_all
from hunter.sent_normalizer import APPLIED_COL, APPLIED_HEADER, build_column, write_column


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize Sent → clean date column (L).")
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write column L. Without it, prints a preview (dry run).",
    )
    parser.add_argument(
        "--tab", default="Tracker",
        help="Name of the data tab to read/write (default: Tracker).",
    )
    args = parser.parse_args()

    service = gsheets_sync._get_service()
    if service is None:
        print("ERROR: Sheets service unavailable. Is GSHEETS_ENABLED=true and the token present?")
        return 1
    gsheets_sync._state = gsheets_sync._read_state()
    sheet_id = gsheets_sync._sheet_id()
    if not sheet_id:
        print("ERROR: no spreadsheet id (gsheets_state.json / GSHEETS_TRACKER_ID empty).")
        return 1

    rows = read_all(service, sheet_id, tab=args.tab)
    grid, filled = build_column(rows)
    print(f"Read {len(rows)} rows from tab {args.tab!r}. "
          f"Found {filled} application dates ({len(rows) - filled} blank).")

    # Show a small sample so the user can eyeball the parse.
    print("\nSample (Sent → Applied Date):")
    shown = 0
    for (_idx, row), cell in zip(rows, grid):
        sent = (row.get("Sent", "") or "").strip()
        if sent and shown < 15:
            print(f"  {sent[:40]:<40} → {cell[0] or '(none)'}")
            shown += 1

    if args.apply:
        write_column(service, sheet_id, grid, tab=args.tab)
        print(f"\nDone. Column {APPLIED_COL} ({APPLIED_HEADER}) now holds clean dates.")
    else:
        print(f"\nDry run — nothing written. Re-run with --apply to fill column "
              f"{APPLIED_COL} ({APPLIED_HEADER}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
