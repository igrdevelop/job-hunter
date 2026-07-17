"""commands/tracks.py — /tracks command: show or switch active candidate tracks.

Which stacks the candidate is applying for (docs/quality/09-multi-track-react.md).
Default is angular-only — today's behavior, unchanged. Adding react lets
React-only vacancies pass the listing filters and the two apply-pipeline
React-only checks instead of being skipped.

/tracks               — show current active tracks
/tracks angular       — angular only
/tracks react         — react only
/tracks both          — angular + react
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_PRESETS = {
    "angular": frozenset({"angular"}),
    "react": frozenset({"react"}),
    "both": frozenset({"angular", "react"}),
}


def _status_text() -> str:
    from hunter.config import active_tracks

    tracks = sorted(active_tracks())
    return (
        f"🧭 <b>Tracks: {', '.join(tracks)}</b>\n\n"
        "Use <code>/tracks angular</code>, <code>/tracks react</code>, or "
        "<code>/tracks both</code> to switch — takes effect on the next hunt/apply, "
        "no restart needed."
    )


def _switch(preset: str) -> str:
    from hunter.config import set_active_tracks

    tracks = _PRESETS.get(preset)
    if tracks is None:
        return f"Usage: <code>/tracks angular|react|both</code> (got {preset!r})"
    set_active_tracks(tracks)
    return _status_text()


async def cmd_tracks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tracks — show or switch which candidate tracks are active."""
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    args = context.args or []
    if not args:
        text = await asyncio.to_thread(_status_text)
    else:
        text = await asyncio.to_thread(_switch, args[0].strip().lower())

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
