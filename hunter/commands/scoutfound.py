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

Payload contract v1 (docs/SCOUT_REPO_SPLIT_PLAN.md §5): the scout's
`telegram_relay.build_payload()` and this decoder are tested in one suite
today but will live in two separate git repos after the scout's planned move
to a private repo — the schema can then drift silently. `MAX_SUPPORTED_
PAYLOAD_VERSION` is the mitigation: a payload with no "v" key is treated as
v1 (backward compatible with anything already in flight); a payload with
"v" > MAX_SUPPORTED_PAYLOAD_VERSION is rejected with a clear reply instead of
silently mis-parsed. tests/fixtures/scout_payload_v1.json is the golden
fixture shared (byte-identical) by both repos' test suites.
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

MAX_SUPPORTED_PAYLOAD_VERSION = 1


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

    version = payload.get("v", 1)
    if version > MAX_SUPPORTED_PAYLOAD_VERSION:
        logger.warning("[scoutfound] rejected: unsupported payload version %s", version)
        if update.message:
            await update.message.reply_text(
                f"scout payload v{version} not supported — update the bot"
            )
        return

    from hunter.sources.linkedin_scout_relay import append_to_queue

    await asyncio.to_thread(append_to_queue, payload)
    logger.info("[scoutfound] queued candidate from %s", payload.get("author", "?"))
