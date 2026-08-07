"""commands/link.py — /link and /unlink handlers (multi-user Phase B3).

/link CODE binds the sender's chat to the web account that generated CODE
on the site; /unlink removes the binding. Deliberately open to ANY chat —
an unlinked stranger holding no valid code gets nothing but a rejection,
and linking is exactly what an unbound chat is supposed to be able to do.
"""

from telegram import Update
from telegram.ext import ContextTypes

from hunter import users


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
    user_id = users.link_chat(chat.id, args[0])
    if user_id is None:
        await update.message.reply_text(
            "❌ Invalid or expired code. Generate a fresh one on the website and try again."
        )
        return
    await update.message.reply_text(
        "✅ Linked. This chat now receives your Job Hunter notifications.\n"
        "Send /unlink to disconnect."
    )


async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if update.message is None or chat is None:
        return
    if users.unlink_chat(chat.id):
        await update.message.reply_text("Unlinked. This chat will no longer receive notifications.")
    else:
        await update.message.reply_text("This chat is not linked to any account.")
