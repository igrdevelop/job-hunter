"""commands/gdrive.py — /gdrive_upload_missing command handler."""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_gdrive_upload_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Upload all tracker.xlsx application folders to Google Drive (runs in background)."""
    from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
    if not GDRIVE_ENABLED:
        await update.message.reply_text(
            "⚠️ GDRIVE_ENABLED=false — Google Drive is not enabled.",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = await update.message.reply_text(
        "⏳ Upload to Google Drive started in background…",
        parse_mode=ParseMode.HTML,
    )

    async def _run() -> None:
        async def _progress(text: str) -> None:
            try:
                await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
            except Exception:
                pass

        try:
            from hunter import gdrive_sync
            result = await gdrive_sync.upload_missing_folders(PROJECT_DIR, progress_cb=_progress)
        except Exception as e:
            await update.message.reply_text(
                f"❌ gdrive_upload_missing error: <code>{e}</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        uploaded = result["uploaded"]
        already = result.get("already_uploaded", 0)
        skipped = result["skipped_missing"]
        errors = result.get("errors", [])
        shadow_uploaded = result.get("shadow_uploaded", 0)
        shadow_errors = result.get("shadow_errors", [])
        err_note = ""
        if errors:
            err_lines = "\n".join(f"  • {e[:120]}" for e in errors[:5])
            err_note = f"\n⚠️ Errors ({len(errors)}):\n<code>{err_lines}</code>"
        if shadow_errors:
            sh_lines = "\n".join(f"  • {e[:120]}" for e in shadow_errors[:5])
            err_note += f"\n⚠️ Shadow errors ({len(shadow_errors)}):\n<code>{sh_lines}</code>"
        await update.message.reply_text(
            f"✅ <b>gdrive_upload_missing</b>\n"
            f"  📤 Uploaded: {uploaded}\n"
            f"  ✔ Already on Drive: {already}\n"
            f"  ⏭ Missing locally: {skipped}\n"
            f"  🔀 Shadow (dual-apply) uploaded: {shadow_uploaded}"
            f"{err_note}",
            parse_mode=ParseMode.HTML,
        )

    context.application.create_task(_run())
