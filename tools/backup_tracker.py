#!/usr/bin/env python3
"""
One-shot backup of tracker.xlsx and to_send.xlsx into backups/ (or TRACKER_BACKUP_DIR).

Run manually:
  python tools/backup_tracker.py

Or from Windows Task Scheduler (daily), working directory = project root:
  python D:\\LearningProject\\Claude\\tools\\backup_tracker.py

When hunter.py is running, the same job also runs once per day via JobQueue.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main() -> int:
    from hunter.tracker_backup import run_tracker_backup

    r = run_tracker_backup()
    for c in r["copied"]:
        print(f"  copied: {c}")
    for s in r["skipped"]:
        print(f"  skipped: {s}")
    print(f"  pruned old files: {r['pruned']}")
    if r["errors"]:
        print("ERRORS:", r["errors"])
        return 1
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
