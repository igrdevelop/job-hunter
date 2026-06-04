"""
Read-only statistics over the Google Sheets tracker.

The `Sent` column does double duty: it holds either an *application date* (in many
inconsistent formats) or a free-text *reason / status note* ("выгасла", "повторка",
"EXPIRED", "не тот стек", "—", ...). This tool never rewrites that data — it only
*reads* every row, classifies the Sent value, and reports:

  - how many rows are real applications (Sent parses as a date),
  - a per-day breakdown of those applications,
  - how many are expired/unavailable, other notes, or blank.

By default it just prints the report (dry run). With --write-tab it also writes a
separate "Stats" tab into the spreadsheet (created if missing, fully overwritten
each run). The data tab and every existing row are left completely untouched.

Run inside the container (reuses the bot's gsheets_token.json):

    # print stats to the console only (default):
    docker compose exec job-hunter python tools/stats_sheet.py

    # also write/update the "Stats" tab in the spreadsheet:
    docker compose exec job-hunter python tools/stats_sheet.py --write-tab
"""

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hunter import gsheets_sync
from hunter.gsheets_client import read_all
from hunter.sent_parse import classify, parse_sent_date


def build_report(rows: list[tuple[int, dict]]) -> dict:
    """Aggregate the Sent column into counts + a per-day applications histogram."""
    buckets: Counter = Counter()
    per_day: Counter = Counter()
    for _idx, row in rows:
        sent = row.get("Sent", "")
        bucket = classify(sent)
        buckets[bucket] += 1
        if bucket == "applied":
            d = parse_sent_date(sent)
            if d is not None:
                per_day[d.isoformat()] += 1
    return {
        "total": len(rows),
        "applied": buckets["applied"],
        "expired": buckets["expired"],
        "other": buckets["other"],
        "blank": buckets["blank"],
        "per_day": dict(sorted(per_day.items())),
    }


def _print_report(rep: dict) -> None:
    print("\n=== Job hunt statistics ===")
    print(f"Total rows:            {rep['total']}")
    print(f"Applied (real date):   {rep['applied']}")
    print(f"Expired / unavailable: {rep['expired']}")
    print(f"Other notes / skips:   {rep['other']}")
    print(f"Blank / dash:          {rep['blank']}")
    print("\nApplications by day:")
    if not rep["per_day"]:
        print("  (none)")
    for day, n in rep["per_day"].items():
        print(f"  {day}   {n}")


def _stats_grid(rep: dict) -> list[list[str]]:
    """Build the 2-column value grid written to the Stats tab."""
    grid: list[list[str]] = [
        ["Job hunt statistics", f"generated {date.today().isoformat()}"],
        ["", ""],
        ["Total rows", str(rep["total"])],
        ["Applied (real date)", str(rep["applied"])],
        ["Expired / unavailable", str(rep["expired"])],
        ["Other notes / skips", str(rep["other"])],
        ["Blank / dash", str(rep["blank"])],
        ["", ""],
        ["Applications by day", ""],
    ]
    for day, n in rep["per_day"].items():
        grid.append([day, str(n)])
    return grid


def _ensure_stats_tab(service, sheet_id: str, tab: str) -> None:
    """Create the Stats tab if it does not exist yet."""
    meta = service.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets.properties"
    ).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab in titles:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
    ).execute()
    print(f"Created tab {tab!r}.")


def _write_stats_tab(service, sheet_id: str, rep: dict, tab: str = "Stats") -> None:
    """Overwrite the Stats tab with the current report (clears old content first)."""
    _ensure_stats_tab(service, sheet_id, tab)
    service.spreadsheets().values().clear(
        spreadsheetId=sheet_id, range=f"'{tab}'!A:Z",
    ).execute()
    grid = _stats_grid(rep)
    service.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!A1",
        valueInputOption="RAW",
        body={"values": grid},
    ).execute()
    print(f"Wrote {len(grid)} rows to tab {tab!r}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Statistics over the Sheets tracker (read-only).")
    parser.add_argument(
        "--write-tab", action="store_true",
        help="Also write the report into a 'Stats' tab. Without it, prints only (dry run).",
    )
    parser.add_argument(
        "--tab", default="Tracker",
        help="Name of the data tab to read (default: Tracker).",
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
    print(f"Read {len(rows)} data rows from tab {args.tab!r} (sheet {sheet_id}).")

    rep = build_report(rows)
    _print_report(rep)

    if args.write_tab:
        _write_stats_tab(service, sheet_id, rep)
    else:
        print("\nDry run — nothing written. Re-run with --write-tab to publish the 'Stats' tab.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
