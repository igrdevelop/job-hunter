"""One-shot backfill of Sheet column M (`Cost $`) from tracker.db.

Used right after this PR ships to surface historical cost_usd values that
were captured locally on apply but never made it to the Sheet, and as a
manual re-sync if the cost mirror ever drifts.

Usage:
    python tools/sync_costs.py
    python tools/sync_costs.py --dry-run     # show what would be written
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hunter.config import (  # noqa: E402
    GSHEETS_CREDENTIALS_FILE,
    GSHEETS_TOKEN_FILE,
)
from hunter.cost_writer import backfill_all_costs_sync  # noqa: E402
from hunter.gsheets_client import build_service  # noqa: E402
from hunter.gsheets_sync import _sheet_id  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be written without touching Sheets")
    args = ap.parse_args()

    sheet_id = _sheet_id()
    if not sheet_id:
        print("error: GSHEETS_TRACKER_ID is not set / no sheet bootstrapped",
              file=sys.stderr)
        return 2

    if args.dry_run:
        import sqlite3
        from hunter.config import TRACKER_DB_PATH
        from hunter.tracker import _format_cost
        con = sqlite3.connect(str(TRACKER_DB_PATH))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, company, title, sheets_row, cost_usd "
            "FROM applications "
            "WHERE sheets_row IS NOT NULL AND cost_usd IS NOT NULL "
            "ORDER BY sheets_row"
        ).fetchall()
        no_row = con.execute(
            "SELECT count(*) FROM applications WHERE sheets_row IS NULL"
        ).fetchone()[0]
        no_cost = con.execute(
            "SELECT count(*) FROM applications "
            "WHERE sheets_row IS NOT NULL AND cost_usd IS NULL"
        ).fetchone()[0]
        print(f"would write {len(rows)} rows to M column:")
        for r in rows:
            print(f"  M{r['sheets_row']:>4}  {_format_cost(r['cost_usd']):>10}  "
                  f"{r['company'][:30]:30s}  {r['title'][:40]}")
        print(f"\nskipped: no_sheets_row={no_row}  no_cost_measured={no_cost}")
        return 0

    service = build_service(GSHEETS_CREDENTIALS_FILE, GSHEETS_TOKEN_FILE)
    result = backfill_all_costs_sync(service, sheet_id)
    print(f"wrote {result['written']} cost cells to column M")
    print(f"  skipped (no sheets_row): {result['skipped_no_row']}")
    print(f"  skipped (CLI run / no cost): {result['skipped_no_cost']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
