"""commands/outcome.py — /outcome: record what happened to a sent application.

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M1, owner decision 2026-09-12.
A 90-day funnel run on prod found 399 sent applications and zero recorded
outcomes — nothing in the codebase wrote the `answer` column — so every metric
past "sent" was unmeasurable. This is the capture side.

    /outcome                      list the newest sent applications with no
                                  outcome yet, one card each, four buttons
    /outcome <id|url> <label>     record it directly
    /outcome <id|url> clear       remove a mistaken entry

Labels come from `hunter.tracker.OUTCOME_LABELS` (never re-spelled here).
Registered under `require_user`, not `require_owner`: a linked user records
outcomes for their OWN rows, and `tracker.set_outcome` scopes every write by
user_id, so a button press can never touch someone else's application.
"""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "outcome"
_LIST_LIMIT = 5

_EMOJI = {
    "interview": "🗣",
    "rejected": "✖️",
    "offer": "🎉",
    "silence": "🔇",
}


def _labels() -> tuple[str, ...]:
    from hunter.tracker import OUTCOME_LABELS

    return OUTCOME_LABELS


def callback_data(row_id: str, label: str) -> str:
    """`outcome:<id>:<label>` — 26 bytes at most, well under Telegram's 64."""
    return f"{CALLBACK_PREFIX}:{row_id}:{label}"


def parse_callback_data(data: str) -> tuple[str, str] | None:
    """Inverse of callback_data(); None for anything that isn't ours."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    _, row_id, label = parts
    if label not in _labels():
        return None
    return row_id, label


def _keyboard(row_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"{_EMOJI.get(lbl, '')} {lbl}", callback_data=callback_data(row_id, lbl)
                )
                for lbl in _labels()
            ]
        ]
    )


def _card_text(row: dict) -> str:
    company = html.escape(str(row.get("company") or "—"))
    title = html.escape(str(row.get("title") or "—"))
    sent = html.escape(str(row.get("sent") or ""))
    return f"<b>{company}</b> — {title}\nSent: {sent} · <code>{html.escape(str(row['id']))}</code>"


async def _mirror_to_sheet(key: str) -> None:
    """Push the new label into Sheet column O now instead of at the next resync.

    Best-effort and silent: the outcome is already saved, the row is dirty, and
    the 5-minute resync writes the cell if this attempt fails.
    """
    from hunter.best_effort import best_effort

    # mirror_outcome counts its own Sheets failures; this outer wrapper covers
    # anything that escapes it (an import or setup error) so it still alerts.
    with best_effort("outcome.sheet_mirror"):
        from hunter import gsheets_sync

        await gsheets_sync.mirror_outcome(key)


def _usage() -> str:
    labels = " | ".join(_labels())
    return (
        "Usage:\n"
        "<code>/outcome</code> — list sent applications with no outcome yet\n"
        f"<code>/outcome &lt;id|url&gt; &lt;{labels}&gt;</code> — record one\n"
        "<code>/outcome &lt;id|url&gt; clear</code> — remove a mistaken entry"
    )


async def cmd_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    args = list(context.args or [])

    if not args:
        from hunter.tracker import get_rows_awaiting_outcome

        rows = await asyncio.to_thread(get_rows_awaiting_outcome, _LIST_LIMIT)
        if not rows:
            await message.reply_text("✅ Every sent application already has an outcome recorded.")
            return
        await message.reply_text(
            f"📬 <b>{len(rows)} sent application(s) with no outcome</b> — newest first.",
            parse_mode=ParseMode.HTML,
        )
        for row in rows:
            await message.reply_text(
                _card_text(row), parse_mode=ParseMode.HTML, reply_markup=_keyboard(row["id"])
            )
        return

    if len(args) != 2:
        await message.reply_text(_usage(), parse_mode=ParseMode.HTML)
        return

    key, label = args[0], args[1].strip().lower()
    if label == "clear":
        label = ""
    from hunter.tracker import set_outcome

    try:
        updated = await asyncio.to_thread(set_outcome, key, label)
    except ValueError:
        await message.reply_text(_usage(), parse_mode=ParseMode.HTML)
        return
    if not updated:
        await message.reply_text("⚠️ No application of yours matches that id or URL.")
        return
    shown = f"{_EMOJI.get(label, '')} {label}" if label else "cleared"
    await message.reply_text(f"✅ Outcome recorded: {shown}")
    # After the reply: the label is saved, so a slow Sheets API must not hold up
    # the confirmation.
    await _mirror_to_sheet(key)


async def outcome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    parsed = parse_callback_data(query.data or "")
    if parsed is None:
        await query.answer()
        return
    row_id, label = parsed
    from hunter.tracker import set_outcome

    try:
        updated = await asyncio.to_thread(set_outcome, row_id, label)
    except Exception:
        logger.exception("[outcome] set_outcome failed for %s", row_id)
        await query.answer("Could not record the outcome, try again.", show_alert=True)
        return
    # The row id is durable (unlike an Apply/Skip card's in-memory job id), so a
    # press on an old card is still valid — the DB is re-checked on every press.
    if not updated:
        await query.answer("That application is no longer yours or was removed.", show_alert=True)
        return
    await query.answer(f"Recorded: {label}")
    # Telegram hands back an InaccessibleMessage for a card older than ~48 h:
    # it has no text to extend. The outcome is already saved, so skip the edit.
    if isinstance(query.message, Message):
        try:
            original = query.message.text_html or ""
            await query.edit_message_text(
                f"{original}\n\n{_EMOJI.get(label, '')} <b>{html.escape(label)}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:  # noqa: BLE001 — the outcome is saved; a stale card is cosmetic
            logger.debug("[outcome] could not edit the card for %s", row_id)
    # Last: a slow Sheets API must not hold up the toast or the card edit.
    await _mirror_to_sheet(row_id)
