"""hunter/bot/auth.py — per-chat authorization for handlers (multi-user B3).

Authorization source of truth is `telegram_links` (hunter.users). The legacy
admin chat (`TELEGRAM_CHAT_ID` env) keeps working as the owner even without
a link row, so a deploy that precedes the one-time owner seed
(docs/MULTI_USER_UPDATE.md item 9) never bricks the bot; it also keeps
single-user dev setups (no DEFAULT_USER_ID) fully functional.

Two decorators, applied at registration time in telegram_bot.build_application:
- require_user  — any linked chat (or the admin chat) may call the handler;
- require_owner — only the owner (DEFAULT_USER_ID link or the admin chat).

This closes the pre-B3 hole where /hunt, /force, URL paste and the
Apply/Skip buttons checked nothing at all — any stranger who found the bot
could trigger real LLM spend.
"""

from __future__ import annotations

import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from hunter import users

log = logging.getLogger(__name__)

NOT_LINKED_TEXT = (
    "🔗 This chat is not linked to a Job Hunter account.\n"
    "Generate a code on the website (Settings → Link Telegram) "
    "and send /link CODE here."
)
OWNER_ONLY_TEXT = "⛔ This command is available to the bot admin only."


def _admin_chat_id() -> int:
    from hunter.config import TELEGRAM_CHAT_ID

    return TELEGRAM_CHAT_ID


def _owner_user_id() -> str:
    from hunter.config import DEFAULT_USER_ID

    return DEFAULT_USER_ID


def authorized_user(update: Update) -> str | None:
    """user_id of the calling chat, or None for an unauthorized chat.

    The admin chat resolves to DEFAULT_USER_ID; the empty string is a valid
    return there (legacy single-user setup with no DEFAULT_USER_ID) and
    still counts as authorized — only None means "reject".
    """
    chat = update.effective_chat
    if chat is None:
        return None
    admin = _admin_chat_id()
    if admin and chat.id == admin:
        return _owner_user_id()
    return users.resolve_user(chat.id)


def is_owner(update: Update) -> bool:
    """True when the calling chat is the admin chat or is linked to the owner."""
    chat = update.effective_chat
    if chat is None:
        return False
    admin = _admin_chat_id()
    if admin and chat.id == admin:
        return True
    owner = _owner_user_id()
    return bool(owner) and users.resolve_user(chat.id) == owner


async def _reject(update: Update, text: str) -> None:
    """Send the rejection where the caller can see it (message or callback)."""
    try:
        if update.callback_query is not None:
            await update.callback_query.answer(text, show_alert=True)
        elif update.message is not None:
            await update.message.reply_text(text)
    except Exception as e:  # noqa: BLE001 — a failed rejection must never raise
        log.warning("[auth] rejection reply failed: %s", e)


def require_user(handler):
    """Allow any linked chat (or the admin chat); reject the rest."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if authorized_user(update) is None:
            chat = update.effective_chat
            log.warning(
                "[auth] %s rejected: chat %s not linked",
                getattr(handler, "__name__", "handler"),
                chat.id if chat else "?",
            )
            await _reject(update, NOT_LINKED_TEXT)
            return None
        return await handler(update, context)

    return wrapper


def require_owner(handler):
    """Allow only the owner/admin; linked non-owners get a distinct message."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            chat = update.effective_chat
            linked = authorized_user(update)
            log.warning(
                "[auth] %s rejected: chat %s (linked user: %s)",
                getattr(handler, "__name__", "handler"),
                chat.id if chat else "?",
                linked or "none",
            )
            await _reject(update, OWNER_ONLY_TEXT if linked is not None else NOT_LINKED_TEXT)
            return None
        return await handler(update, context)

    return wrapper
