"""One-shot backfill of Sheet column N (`ATS Verdict`) from tracker.db.

Heals rows whose live verdict mirror failed (e.g. Sheets token down at apply
time) and labels the column on sheets created before the feature shipped.

Usage:
    python tools/sync_verdicts.py
    python tools/sync_verdicts.py --dry-run     # show what would be written
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
from hunter.gsheets_client import build_service  # noqa: E402
from hunter.gsheets_sync import _sheet_id  # noqa: E402
from hunter.verdict_writer import backfill_all_verdicts_sync  # noqa: E402


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
        con = sqlite3.connect(str(TRACKER_DB_PATH))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, company, title, sheets_row, ats_verdict "
            "FROM applications "
            "WHERE sheets_row IS NOT NULL AND ats_verdict IS NOT NULL "
            "ORDER BY sheets_row"
        ).fetchall()
        no_row = con.execute(
            "SELECT count(*) FROM applications WHERE sheets_row IS NULL"
        ).fetchone()[0]
        no_verdict = con.execute(
            "SELECT count(*) FROM applications "
            "WHERE sheets_row IS NOT NULL AND ats_verdict IS NULL"
        ).fetchone()[0]
        print(f"would write {len(rows)} rows to N column:")
        for r in rows:
            print(f"  N{r['sheets_row']:>4}  {r['ats_verdict']:>6}  "
                  f"{r['company'][:30]:30s}  {r['title'][:40]}")
        print(f"\nskipped: no_sheets_row={no_row}  no_verdict={no_verdict}")
        return 0

    service = build_service(GSHEETS_CREDENTIALS_FILE, GSHEETS_TOKEN_FILE)
    result = backfill_all_verdicts_sync(service, sheet_id)
    print(f"wrote {result['written']} verdict cells to column N")
    print(f"  skipped (no sheets_row): {result['skipped_no_row']}")
    print(f"  skipped (no verdict): {result['skipped_no_verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
