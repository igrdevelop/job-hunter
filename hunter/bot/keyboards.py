"""
bot/keyboards.py — Inline keyboard factories for Telegram messages.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _make_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Apply / Skip button pair for a pending job card."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Apply", callback_data=f"apply:{job_id}"),
                InlineKeyboardButton("❌ Skip", callback_data=f"skip:{job_id}"),
            ]
        ]
    )
