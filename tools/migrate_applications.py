"""
tools/migrate_applications.py — One-time migration of Applications/ to date-based subfolders.

Old structure:  Applications/{CompanyName}_{YYYY-MM-DD}[_N]/
New structure:  Applications/{YYYY-MM-DD}/{CompanyName}[_N]/

Also updates the Folder column in tracker.xlsx so existing rows still resolve correctly.

Run:
    python tools/migrate_applications.py          # dry-run (shows what would move)
    python tools/migrate_applications.py --apply  # actually moves folders + updates tracker
"""

import re
import shutil
import sys
from pathlib import Path

import openpyxl

PROJECT_DIR = Path(__file__).parent.parent
APPLICATIONS_DIR = PROJECT_DIR / "Applications"
TRACKER_PATH = PROJECT_DIR / "tracker.xlsx"

# Column index for Folder (1-based) — matches hunter/tracker.py
FOLDER_COL_INDEX = 7

DATE_PATTERN = re.compile(r"^(.+)_(\d{4}-\d{2}-\d{2})(_\d+)?$")


def plan_moves() -> list[tuple[Path, Path]]:
    """Return list of (src, dst) pairs for all migratable folders."""
    moves: list[tuple[Path, Path]] = []

    for folder in sorted(APPLICATIONS_DIR.iterdir()):
        if not folder.is_dir():
            continue

        # Skip folders that are already date directories (YYYY-MM-DD)
        if re.match(r"^\d{4}-\d{2}-\d{2}$", folder.name):
            continue

        m = DATE_PATTERN.match(folder.name)
        if not m:
            print(f"  SKIP (no date pattern):  {folder.name}")
            continue

        company = m.group(1)
        date_str = m.group(2)
        suffix = m.group(3) or ""  # e.g. "_2", "_3", or ""

        dst = APPLICATIONS_DIR / date_str / f"{company}{suffix}"
        moves.append((folder, dst))

    return moves


def update_tracker(path_map: dict[str, str], dry_run: bool) -> None:
    """Replace old Folder paths with new paths in tracker.xlsx."""
    if not TRACKER_PATH.exists():
        print("  tracker.xlsx not found — skipping tracker update.")
        return

    wb = openpyxl.load_workbook(TRACKER_PATH)
    ws = wb.active
    updated = 0

    for row in ws.iter_rows(min_row=2):
        cell = row[FOLDER_COL_INDEX - 1]
        val = str(cell.value or "").strip()
        if not val:
            continue

        # Normalise to forward slashes for comparison
        norm = val.replace("\\", "/")
        if norm in path_map:
            new_val = path_map[norm]
            if not dry_run:
                cell.value = new_val
            print(f"  tracker row {cell.row}: {norm}  ->  {new_val}")
            updated += 1

    if updated == 0:
        print("  No tracker rows needed updating.")
        return

    if not dry_run:
        wb.save(TRACKER_PATH)
        print(f"  tracker.xlsx saved ({updated} rows updated).")
    else:
        print(f"  (dry-run) Would update {updated} tracker rows.")

    wb.close()


def main() -> None:
    dry_run = "--apply" not in sys.argv

    if dry_run:
        print("DRY-RUN mode — pass --apply to actually migrate.\n")
    else:
        print("APPLY mode — folders will be moved and tracker updated.\n")

    moves = plan_moves()

    if not moves:
        print("Nothing to migrate.")
        return

    print(f"{'MOVE PLAN' if dry_run else 'MOVING'} ({len(moves)} folders):\n")

    path_map: dict[str, str] = {}

    for src, dst in moves:
        label = f"  {src.name}  ->  {dst.parent.name}/{dst.name}"
        if dst.exists() and not dry_run:
            print(f"  WARNING: destination already exists, skipping: {dst}")
            continue

        print(label)

        # Build path map for tracker update (forward-slash normalised)
        old_path = str(src).replace("\\", "/")
        new_path = str(dst).replace("\\", "/")
        path_map[old_path] = new_path

        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))

    print()
    print("Updating tracker.xlsx Folder column:")
    update_tracker(path_map, dry_run)

    print()
    if dry_run:
        print("Dry-run complete. Run with --apply to execute.")
    else:
        print("Migration complete.")


if __name__ == "__main__":
    main()
