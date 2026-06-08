"""
tools/repair_false_expired.py — un-EXPIRE rows that are actually still live.

Background
---------
A pull deletion-reconcile (fixed in `mark_orphans_expired`, PR #84) falsely
stamped ``Sent='EXPIRED'`` on rows that were never mirrored during a Sheets-token
outage. ``/gsheets_push_missing`` later re-mirrored them, so they now carry a
``sheets_row`` and can no longer be told apart from genuinely-expired rows by
metadata alone.

This tool re-verifies each recent EXPIRED row against the live posting and clears
the *false* ones on BOTH sides (Sheet cell + DB ``Sent``), so the pull conflict
matrix (which keeps DB-EXPIRED over a blank Sheet cell) can't flip them back.

Decision per EXPIRED row in scope:
  - explicit --ids given        → trust the operator, treat as live
  - row has no URL              → can't fetch; only reconcile can EXPIRE a
                                  URL-less row, so treat as live (false positive)
  - row has a URL               → fetch + is_job_expired(): live ⇒ clear, else keep
  - fetch error                 → leave as-is, report (don't guess)

Dry-run by default. Use --apply to write.

  python tools/repair_false_expired.py --since 2026-06-02
  python tools/repair_false_expired.py --since 2026-06-02 --apply
  python tools/repair_false_expired.py --ids 16002d8b,40a12003 --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hunter import gsheets_sync, tracker
from hunter.db import get_db
from hunter.expired_check import is_job_expired
from hunter.gsheets_client import read_all, update_cell
from hunter.sources import fetch_job_text


def _scope_rows(since: str | None, ids: set[str] | None) -> list[dict]:
    sql = "SELECT id, date, company, title, url, sheets_row FROM applications WHERE sent='EXPIRED'"
    params: list = []
    if since:
        sql += " AND date >= ?"
        params.append(since)
    sql += " ORDER BY date DESC"
    with get_db(tracker.DB_PATH) as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if ids:
        rows = [r for r in rows if r["id"] in ids]
    return rows


def _is_live(row: dict, trust: bool) -> tuple[bool | None, str]:
    """Return (live, reason). live=None ⇒ undetermined (fetch error)."""
    if trust:
        return True, "explicit --ids"
    url = (row.get("url") or "").strip()
    if not url:
        return True, "no URL (reconcile-only victim)"
    try:
        text = fetch_job_text(url)
    except Exception as e:  # noqa: BLE001
        return None, f"fetch error: {type(e).__name__}"
    if not text or len(text) < 200:
        return None, f"thin fetch ({len(text or '')} chars)"
    return (not is_job_expired(text)), "verified via is_job_expired"


def _clear_row(service, sheet_id: str, row_id: str, sheet_idx: int | None) -> None:
    """Clear Sent on both sides so the pull conflict matrix stays stable."""
    if sheet_idx is not None:
        update_cell(service, sheet_id, sheet_idx, "Sent", "")
        with get_db(tracker.DB_PATH) as conn:
            conn.execute(
                "UPDATE applications SET sent='', sheets_row=?, sheets_dirty=0 WHERE id=?",
                (sheet_idx, row_id),
            )
    else:
        # Not in the Sheet — clear DB and flag for the next push.
        with get_db(tracker.DB_PATH) as conn:
            conn.execute(
                "UPDATE applications SET sent='', sheets_dirty=1 WHERE id=?",
                (row_id,),
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help="Only rows with date >= this (YYYY-MM-DD)")
    ap.add_argument("--ids", help="Comma-separated row IDs to target (skips re-verify)")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = ap.parse_args()

    id_set = {i.strip() for i in args.ids.split(",") if i.strip()} if args.ids else None
    if not args.since and not id_set:
        print("Refusing to scan ALL history -- pass --since YYYY-MM-DD or --ids.", file=sys.stderr)
        return 2

    if not gsheets_sync._ready():
        print("Google Sheets not ready (check token / GSHEETS_ENABLED).", file=sys.stderr)
        return 1
    service = gsheets_sync._get_service()
    sheet_id = gsheets_sync._sheet_id()

    sheet_by_id: dict[str, int] = {
        (r.get("ID") or "").strip(): idx
        for idx, r in read_all(service, sheet_id)
        if (r.get("ID") or "").strip()
    }

    rows = _scope_rows(args.since, id_set)
    print(f"Scope: {len(rows)} EXPIRED row(s)"
          + (f" since {args.since}" if args.since else "")
          + (f", ids={len(id_set)}" if id_set else ""))

    cleared = kept = undetermined = 0
    for r in rows:
        live, reason = _is_live(r, trust=bool(id_set))
        rid, comp, title = r["id"], r["company"] or "?", r["title"] or "?"
        if live is None:
            undetermined += 1
            print(f"  ?    KEEP   {rid}  {comp} - {title[:50]}  ({reason})")
            continue
        if not live:
            kept += 1
            print(f"  [-]  KEEP   {rid}  {comp} - {title[:50]}  (still expired)")
            continue
        sheet_idx = sheet_by_id.get(rid)
        loc = f"sheet row {sheet_idx}" if sheet_idx else "not in sheet -> mark dirty"
        if args.apply:
            _clear_row(service, sheet_id, rid, sheet_idx)
        cleared += 1
        verb = "CLEARED   " if args.apply else "would clear"
        print(f"  [+]  {verb} {rid}  {comp} - {title[:50]}  ({reason}; {loc})")

    mode = "APPLIED" if args.apply else "DRY-RUN (no changes)"
    print(f"\n{mode}: live->cleared={cleared}, still-expired->kept={kept}, "
          f"undetermined->kept={undetermined}")
    if args.apply and cleared:
        print("Note: restart the container or wait for the next pull so the "
              "in-memory cache (/unsent, /status) reflects the change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
