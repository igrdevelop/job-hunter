"""
hunter/drive_ledger.py — content-signature ledger for dual-apply shadow uploads.

Shadow sets (``{company}/{shadow_profile_name}/``) have no tracker row, so
they're invisible to the per-row "already has a Drive URL" check
``upload_missing_folders`` uses for primary application folders — by design
they were re-uploaded on EVERY backfill pass forever (docs/GDRIVE_SSL_RACE_PLAN.md
M3: 86 distinct shadow folders, ~1192 uploads/day on the live corpus, on the
order of 700 pointless Drive API calls every 30 minutes).

This module tracks a lightweight content signature per local folder path in
SQLite (``drive_uploads``, same tracker.db) so a shadow folder that hasn't
changed since its last successful upload is skipped. The signature is
content-derived (file count, latest mtime, total size) rather than a marker
file inside the folder: a marker would itself be uploaded to Drive, and the
folder lives in ``Applications/`` (gitignored, container-mounted, pruned) —
the DB is the durable, already-mounted store the rest of the bot uses. A
renamed/regenerated file (e.g. a verdict-suffix filename change) or an
added/removed file changes the signature and forces a re-upload; an
untouched folder is skipped.

Public API
----------
    signature(folder)              content signature string for a folder
    is_current(path, sig)          True if the ledger already has this exact
                                    signature recorded for this path
    record(path, sig, url)         persist a successful upload
    forget(path)                   drop a path from the ledger (escape hatch
                                    for a folder deleted on Drive by hand —
                                    the ledger can't see that; use `force`
                                    in the caller instead of this directly)

Storage: a `drive_uploads` table in the same tracker.db (created lazily,
mirrors hunter.source_health's own lazy-ensure pattern for `source_runs` —
self-contained, not part of hunter.db's init_db()).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from hunter.config import TRACKER_DB_PATH
from hunter.db import get_db

log = logging.getLogger(__name__)

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.source_health.DB_PATH).
DB_PATH = TRACKER_DB_PATH

_DDL = """
CREATE TABLE IF NOT EXISTS drive_uploads (
    path        TEXT    PRIMARY KEY,
    signature   TEXT    NOT NULL,
    drive_url   TEXT    NOT NULL DEFAULT '',
    uploaded_at TEXT    NOT NULL
);
"""


def _ensure_table(conn) -> None:
    conn.executescript(_DDL)


def signature(folder: Path) -> str:
    """Content-derived signature: "file_count:max_mtime_ns:total_size".

    Only direct files are considered (Applications/ folders are flat, same
    assumption gdrive_client.upload_folder makes). A missing/empty folder
    signs as ``"0:0:0"``.
    """
    files = [f for f in folder.iterdir() if f.is_file()] if folder.is_dir() else []
    count = len(files)
    max_mtime_ns = max((f.stat().st_mtime_ns for f in files), default=0)
    total_size = sum(f.stat().st_size for f in files)
    return f"{count}:{max_mtime_ns}:{total_size}"


def is_current(path: str, sig: str) -> bool:
    """True if the ledger already recorded this exact signature for `path`."""
    try:
        with get_db(DB_PATH) as conn:
            _ensure_table(conn)
            row = conn.execute(
                "SELECT signature FROM drive_uploads WHERE path = ?", (path,)
            ).fetchone()
    except Exception as e:  # noqa: BLE001 — a ledger read failure must never block an upload
        log.warning("drive_ledger.is_current failed for %s: %s", path, e)
        return False
    return bool(row) and row["signature"] == sig


def record(path: str, sig: str, url: str) -> None:
    """Persist a successful upload. Best-effort: never raises into the caller.

    Deliberately called only AFTER a successful upload — a failed upload
    must be retried on the next pass, so it must never be recorded here.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with get_db(DB_PATH) as conn:
            _ensure_table(conn)
            conn.execute(
                """
                INSERT INTO drive_uploads (path, signature, drive_url, uploaded_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    signature = excluded.signature,
                    drive_url = excluded.drive_url,
                    uploaded_at = excluded.uploaded_at
                """,
                (path, sig, url or "", now),
            )
    except Exception as e:  # noqa: BLE001 — telemetry must never break the upload it followed
        log.warning("drive_ledger.record failed for %s: %s", path, e)


def forget(path: str) -> None:
    """Drop a path from the ledger, forcing its next check to be a miss."""
    try:
        with get_db(DB_PATH) as conn:
            _ensure_table(conn)
            conn.execute("DELETE FROM drive_uploads WHERE path = ?", (path,))
    except Exception as e:  # noqa: BLE001
        log.warning("drive_ledger.forget failed for %s: %s", path, e)
