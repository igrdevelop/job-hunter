"""commands/export.py — /export command: generate tracker.xlsx from SQLite."""

import asyncio
import logging
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a fresh tracker.xlsx snapshot from the SQLite DB and send it.

    Usage:  /export
    """
    status = await update.message.reply_text(
        "⏳ Generating <b>tracker.xlsx</b> from SQLite…",
        parse_mode="HTML",
    )

    try:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "tracker_export.xlsx"
            n_rows = await asyncio.to_thread(_do_export, out_path)
            await status.edit_text(
                f"✅ Export complete — <b>{n_rows}</b> rows. Sending file…",
                parse_mode="HTML",
            )
            with open(out_path, "rb") as fh:
                await update.message.reply_document(
                    document=fh,
                    filename="tracker.xlsx",
                    caption=f"tracker.xlsx — {n_rows} rows",
                )
    except Exception as exc:
        logger.exception("[export] failed")
        await status.edit_text(
            f"❌ Export failed: <code>{exc}</code>",
            parse_mode="HTML",
        )


def _do_export(out_path: Path) -> int:
    """Synchronous worker — called via asyncio.to_thread."""
    from hunter.export_xlsx import export_tracker_xlsx

    return export_tracker_xlsx(out_path)
