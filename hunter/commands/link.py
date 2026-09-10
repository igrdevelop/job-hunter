"""commands/link.py — /link and /unlink handlers (multi-user Phase B3).

/link CODE binds the sender's chat to the web account that generated CODE
on the site; /unlink removes the binding. Deliberately open to ANY chat —
an unlinked stranger holding no valid code gets nothing but a rejection,
and linking is exactly what an unbound chat is supposed to be able to do.

Brute-force limiter (docs/improvement-2026-09/05-SECURITY_PLAN.md finding
#4/M4): the API's link code is short-lived but not very high entropy, and
this handler used to accept unlimited guesses. `users.link_chat_with_details`
refuses a chat past LINK_ATTEMPT_LIMIT failures within LINK_ATTEMPT_WINDOW_MIN
minutes without even touching telegram_link_codes; a successful link accepts
codes of any length >= 6 on purpose, so a longer API-side code (a planned
`randomBytes(8)` upgrade) works here without a bot change.
"""

from telegram import Update
from telegram.ext import ContextTypes

from hunter import users
from hunter.best_effort import best_effort
from hunter.bot.notifications import _tg_notify

RATE_LIMITED_TEXT = "⛔ Too many incorrect codes from this chat. Wait a few minutes and try again."


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if update.message is None or chat is None:
        return
    args = context.args or []
    if len(args) != 1:
        await update.message.reply_text(
            "Usage: /link CODE\n"
            "Generate the code on the website (Settings → Link Telegram); "
            "it expires in 10 minutes."
        )
        return
    result = users.link_chat_with_details(chat.id, args[0])
    if result.rate_limited:
        await update.message.reply_text(RATE_LIMITED_TEXT)
        return
    if result.user_id is None:
        await update.message.reply_text(
            "❌ Invalid or expired code. Generate a fresh one on the website and try again."
        )
        return
    await update.message.reply_text(
        "✅ Linked. This chat now receives your Job Hunter notifications.\n"
        "Send /unlink to disconnect."
    )
    if result.displaced_chat_id is not None:
        # Best-effort: the old chat this account was linked from just lost
        # its link (a device switch by the same person, or a leaked code —
        # either way the old chat should know). Never blocks the reply above.
        with best_effort("link.notify_old_chat"):
            await _tg_notify(
                "⚠️ This account was linked to another Telegram chat. "
                "If this wasn't you, re-link here with a fresh code.",
                chat_id=result.displaced_chat_id,
            )


async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if update.message is None or chat is None:
        return
    if users.unlink_chat(chat.id):
        await update.message.reply_text("Unlinked. This chat will no longer receive notifications.")
    else:
        await update.message.reply_text("This chat is not linked to any account.")
