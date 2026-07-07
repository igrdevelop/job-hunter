"""commands/scoutfound.py — /scoutfound <payload> receives one LinkedIn scout
candidate relayed from the standalone linkedin_scout/ script.

Delivery path (owner decision 2026-07-08, after discovering the bot
auto-deploys to its own server and does not share a filesystem with the
scout's Windows desktop): linkedin_scout/telegram_relay.py sends this command
through the OWNER'S OWN Telegram user session (Telethon, not the Bot API —
Telegram never delivers a bot's own outgoing messages back to itself as an
incoming update, so the scout can't just "message the bot" as the bot).

Only accepted from the configured owner chat (TELEGRAM_CHAT_ID) — this
command ultimately feeds hunter/sources/linkedin_scout_relay.py's queue,
which flows straight into AUTO_APPLY (real LLM spend), so it must not be
triggerable by anyone who isn't the owner.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def cmd_scoutfound(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id != TELEGRAM_CHAT_ID:
        logger.warning("[scoutfound] rejected: chat_id %s is not the owner chat", chat_id)
        return

    if not context.args:
        return

    try:
        payload = json.loads(base64.b64decode(context.args[0]).decode("utf-8"))
    except Exception as e:  # noqa: BLE001 — malformed payload, never crash the bot
        logger.warning("[scoutfound] failed to decode payload: %s", e)
        return

    if not isinstance(payload, dict) or not payload.get("body"):
        logger.warning("[scoutfound] payload missing body, ignoring")
        return

    from hunter.sources.linkedin_scout_relay import append_to_queue

    await asyncio.to_thread(append_to_queue, payload)
    logger.info("[scoutfound] queued candidate from %s", payload.get("author", "?"))
