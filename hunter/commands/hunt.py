"""commands/hunt.py — /hunt command handler."""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def parse_hunt_source_args(
    args: list[str], valid_names: set[str]
) -> tuple[list[str] | None, list[str]]:
    """Split /hunt arguments into source slugs.

    Returns (names_or_None_for_all, unknown_slugs).
    """
    requested: list[str] = []
    for a in args:
        for part in a.split(","):
            part = part.strip().lower()
            if part:
                requested.append(part)
    if not requested:
        return None, []
    seen: set[str] = set()
    unique: list[str] = []
    for r in requested:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    unknown = [r for r in unique if r not in valid_names]
    if unknown:
        return [], unknown
    return unique, []


# Backward-compat alias used by tests
_parse_hunt_source_args = parse_hunt_source_args


async def cmd_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger — full hunt or a subset of sources (same names as in /schedule)."""
    from hunter.main import run_hunt
    from hunter.sources import ALL_SOURCES

    valid_names = {s.name for s in ALL_SOURCES}
    source_names, unknown = parse_hunt_source_args(context.args or [], valid_names)

    if unknown:
        avail = ", ".join(sorted(valid_names))
        await update.message.reply_text(
            f"❌ Unknown source(s): <b>{', '.join(unknown)}</b>\n\nAvailable: <code>{avail}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if source_names:
        label = ", ".join(source_names)
        await update.message.reply_text(
            f"🔍 Running hunt: <b>{label}</b>",
            parse_mode=ParseMode.HTML,
        )
        await run_hunt(context, source_names=source_names)
    else:
        await update.message.reply_text("🔍 Running hunt (all sources)...")
        await run_hunt(context)
