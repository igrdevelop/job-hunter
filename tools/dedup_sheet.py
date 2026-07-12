"""
One-time cleanup of duplicate rows in the Google Sheets tracker.

Background (see docs/archive/BOOTSTRAP_DEDUP_PLAN.md / DUPLICATE_INVESTIGATION.md): before the
self-heal fix, a blind/empty tracker.db let the bot re-process live vacancies and
append duplicate rows to the shared Sheet. The fix prevents *new* duplicates; this
tool removes the *historical* ones already sitting in the Sheet.

What it does:
  - reads the whole Tracker tab,
  - groups rows by normalize_url(URL) (rows without a URL are left untouched),
  - in each group keeps the "best" row and deletes the rest:
        best = a row with a non-empty Sent value, else the earliest by Date
               (ties → earliest sheet row).

Only the Google Sheet is modified. The local tracker.db is left alone — its dedup
is already correct by url_norm, and the next periodic pull re-persists sheets_row
for the surviving rows.

Run inside the container (it reuses the bot's gsheets_token.json):

    # dry run — prints the deletion plan, changes nothing (default):
    docker compose exec job-hunter python tools/dedup_sheet.py

    # actually delete the duplicate rows:
    docker compose exec job-hunter python tools/dedup_sheet.py --apply
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hunter import gsheets_sync
from hunter.gsheets_client import read_all, delete_sheet_row
from hunter.tracker import normalize_url


def _parse_date(value: str) -> date:
    """Best-effort date parse for ordering; unknown formats sort last."""
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value[: len(fmt) + 4], fmt).date()
        except ValueError:
            continue
    return date.max


def _pick_keeper(group: list[tuple[int, dict]]) -> tuple[int, dict]:
    """Choose the row to keep: prefer a filled Sent, then earliest Date, then row idx."""
    with_sent = [g for g in group if (g[1].get("Sent") or "").strip()]
    pool = with_sent or group
    return min(pool, key=lambda g: (_parse_date(g[1].get("Date", "")), g[0]))


def _resolve_sheet_id() -> str:
    """Resolve the active spreadsheet id from state file / env (no creation)."""
    gsheets_sync._state = gsheets_sync._read_state()
    return gsheets_sync._sheet_id()


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove duplicate rows from the Sheets tracker.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually delete duplicates. Without it, only prints the plan (dry run).",
    )
    args = parser.parse_args()

    service = gsheets_sync._get_service()
    if service is None:
        print("ERROR: Sheets service unavailable. Is GSHEETS_ENABLED=true and the token present?")
        return 1
    sheet_id = _resolve_sheet_id()
    if not sheet_id:
        print("ERROR: no spreadsheet id (gsheets_state.json / GSHEETS_TRACKER_ID empty).")
        return 1

    rows = read_all(service, sheet_id)
    print(f"Read {len(rows)} data rows from Sheet {sheet_id}.")

    # Group by normalized URL; skip rows without a URL (SKIP/EXPIRED placeholders).
    groups: dict[str, list[tuple[int, dict]]] = {}
    skipped_no_url = 0
    for idx, row in rows:
        norm = normalize_url(row.get("URL", "") or "")
        if not norm:
            skipped_no_url += 1
            continue
        groups.setdefault(norm, []).append((idx, row))

    dup_groups = {n: g for n, g in groups.items() if len(g) > 1}
    to_delete: list[tuple[int, dict]] = []

    for norm, group in sorted(dup_groups.items()):
        keeper = _pick_keeper(group)
        losers = [g for g in group if g[0] != keeper[0]]
        to_delete.extend(losers)
        keep_idx, keep_row = keeper
        print(f"\n{norm}  ({len(group)} rows)")
        print(
            f"  KEEP   row {keep_idx}: Date={keep_row.get('Date', '')!r} "
            f"Sent={keep_row.get('Sent', '')!r} Company={keep_row.get('Company', '')!r}"
        )
        for li, lr in losers:
            print(
                f"  DELETE row {li}: Date={lr.get('Date', '')!r} "
                f"Sent={lr.get('Sent', '')!r} Company={lr.get('Company', '')!r}"
            )

    print(
        f"\nSummary: {len(groups)} unique URLs, {len(dup_groups)} duplicated, "
        f"{len(to_delete)} rows to delete, {skipped_no_url} rows without URL untouched."
    )

    if not to_delete:
        print("Nothing to do.")
        return 0

    if not args.apply:
        print("\nDry run — no changes made. Re-run with --apply to delete the rows above.")
        return 0

    # Delete from highest row index to lowest so earlier indices don't shift.
    deleted = 0
    for idx, _row in sorted(to_delete, key=lambda g: g[0], reverse=True):
        try:
            delete_sheet_row(service, sheet_id, idx)
            deleted += 1
        except Exception as e:
            print(f"  ! failed to delete row {idx}: {e}")

    print(f"\nDeleted {deleted}/{len(to_delete)} duplicate rows.")
    print("Local tracker.db untouched; sheets_row will re-sync on the next periodic pull.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
