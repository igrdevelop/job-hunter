"""Instant post-apply delivery: Sheets mirror + Drive upload, for EVERY apply path.

One shared entry point (docs/HUNT_QUEUE_AND_DELIVERY_PLAN.md M3) replacing three
divergent copies of the same hooks (main._auto_apply_all / main._retry_failed /
bot.apply_runner) — and covering the paths that had NO immediate delivery at all
(paste without a URL, the LinkedIn batch), which used to wait for the periodic
backfills (Sheets resync 5 min, Drive upload-missing 3 h — the "не сразу на
диске" the owner reported 2026-07-12).

Best-effort throughout: every stage has its own try/except, a Sheets failure
never blocks the Drive upload and vice versa, and the periodic backfills remain
as the safety net behind this.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


async def deliver_apply_now(url: str | None) -> str | None:
    """Mirror the just-applied tracker row to Sheets and upload its folder to Drive NOW.

    Targeted fast path when a URL is known; falls back to the idempotent
    backfills (push_missing_rows / upload_missing_folders) when it isn't, or
    when the targeted lookup misses — those deliver exactly the rows/folders
    that are missing, immediately, instead of on the next periodic slot.

    Returns the Drive folder URL when the targeted upload produced one
    (callers may show an "Open folder on Drive" link). Never raises.
    """
    delivered_sheets = False
    if url:
        delivered_sheets = await _mirror_row_targeted(url)
    if not delivered_sheets:
        await _push_missing_rows()

    drive_url = None
    delivered_drive = False
    if url:
        drive_url = await _upload_folder_targeted(url)
        delivered_drive = drive_url is not None
    if not delivered_drive:
        await _upload_missing_folders()
    return drive_url


async def _mirror_row_targeted(url: str) -> bool:
    """Append this URL's tracker row to Sheets. True if the row was found."""
    try:
        from hunter.tracker_cache import cache
        from hunter import gsheets_sync

        await cache.load_from_db()
        row = await cache.get_row_by_url(url)
        if not row:
            logger.warning("[delivery] no tracker row found for %s — falling back", url)
            return False
        await gsheets_sync.mirror_new_row(row)
        return True
    except Exception as e:
        logger.warning("[delivery] gsheets mirror failed for %s: %s", url, e)
        # mirror_new_row failing leaves the row sheets_dirty — the resync job
        # picks it up; don't double-append via push_missing in the same breath.
        return True


async def _push_missing_rows() -> None:
    """Fallback: append every DB row absent from Sheets (no-URL paste rows etc.)."""
    try:
        from hunter import gsheets_sync

        result = await gsheets_sync.push_missing_rows()
        pushed = result.get("pushed", 0)
        if pushed:
            logger.info("[delivery] push_missing_rows: %d row(s) delivered", pushed)
    except Exception as e:
        logger.warning("[delivery] push_missing_rows failed: %s", e)


async def _upload_folder_targeted(url: str) -> str | None:
    """Upload this URL's application folder to Drive. Returns the Drive URL."""
    try:
        from hunter.config import GDRIVE_ENABLED, PROJECT_DIR

        if not GDRIVE_ENABLED:
            return None
        from hunter.tracker import get_folder_by_url
        from hunter import gdrive_sync

        folder_str = await asyncio.to_thread(get_folder_by_url, url)
        if not folder_str:
            logger.warning("[delivery] no folder recorded for %s — falling back", url)
            return None
        return await gdrive_sync.upload_application_folder(PROJECT_DIR / folder_str, job_url=url)
    except Exception as e:
        logger.warning("[delivery] gdrive upload failed for %s: %s", url, e)
        return None


async def _upload_missing_folders() -> None:
    """Fallback: upload every application folder that has no Drive URL yet."""
    try:
        from hunter.config import GDRIVE_ENABLED, PROJECT_DIR

        if not GDRIVE_ENABLED:
            return
        from hunter import gdrive_sync

        result = await gdrive_sync.upload_missing_folders(PROJECT_DIR)
        uploaded = result.get("uploaded", 0)
        if uploaded:
            logger.info("[delivery] upload_missing_folders: %d folder(s) delivered", uploaded)
    except Exception as e:
        logger.warning("[delivery] upload_missing_folders failed: %s", e)
