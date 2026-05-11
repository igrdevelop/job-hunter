"""Timestamped copies of tracker.xlsx and to_send.xlsx for disaster recovery."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path

from hunter import config as cfg

logger = logging.getLogger(__name__)


def run_tracker_backup() -> dict:
    """Copy tracker + to_send into :data:`cfg.TRACKER_BACKUP_DIR`; prune old files.

    Returns a JSON-serializable summary: ``ok``, ``copied``, ``skipped``, ``errors``, ``pruned``.
    """
    result: dict = {
        "ok": True,
        "copied": [],
        "skipped": [],
        "errors": [],
        "pruned": 0,
    }
    backup_dir = Path(cfg.TRACKER_BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = f"{datetime.now():%Y%m%d_%H%M%S}_{time.perf_counter_ns()}"

    def _copy(label: str, src: Path) -> None:
        if not src.is_file():
            result["skipped"].append(f"{label}: missing ({src.name})")
            return
        dest = backup_dir / f"{label}_{ts}.xlsx"
        try:
            shutil.copy2(src, dest)
            try:
                rel = dest.relative_to(cfg.PROJECT_DIR)
                result["copied"].append(str(rel).replace("\\", "/"))
            except ValueError:
                result["copied"].append(str(dest))
        except OSError as e:
            result["ok"] = False
            result["errors"].append(f"{label}: {e}")

    _copy("tracker", Path(cfg.TRACKER_PATH))
    _copy("to_send", Path(cfg.TO_SEND_PATH))

    keep = cfg.TRACKER_BACKUP_KEEP_FILES
    result["pruned"] += _prune_old(backup_dir, "tracker", keep)
    result["pruned"] += _prune_old(backup_dir, "to_send", keep)

    if result["errors"]:
        result["ok"] = False
    logger.info(
        "[tracker_backup] copied=%s skipped=%s pruned=%s errors=%s",
        result["copied"],
        result["skipped"],
        result["pruned"],
        result["errors"],
    )
    return result


def _prune_old(backup_dir: Path, prefix: str, keep: int) -> int:
    if keep <= 0:
        return 0
    files = sorted(
        (p for p in backup_dir.glob(f"{prefix}_*.xlsx") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for p in files[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("[tracker_backup] prune failed %s: %s", p, exc)
    return removed
