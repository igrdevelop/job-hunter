"""commands/dual.py — /dual command: toggle dual-apply (A/B comparison) mode.

When dual mode is on, every successful apply also generates a side-by-side
shadow set with the shadow profile (default deepseek-v3) into a {model}
subfolder of the application — comparison only, no tracker / Telegram / Sheets.
Shadow doc filenames carry the ATS score (e.g. ..._EN_ats88.pdf).

/dual                 — show current state (on/off + shadow profile)
/dual on              — enable
/dual off             — disable
/dual shadow <name>   — switch the shadow profile (e.g. deepseek-v4-pro)
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def _status_text() -> str:
    from hunter.llm_profiles import dual_enabled, get_active, shadow_profile

    on = dual_enabled()
    active = get_active()
    shadow = shadow_profile()

    lines = [f"🧪 <b>Dual-apply: {'ON' if on else 'OFF'}</b>"]
    lines.append(f"   Boevoy (real): <code>{active.name}</code> — {active.model}")
    if shadow is None:
        lines.append("   Shadow: <i>unavailable</i> (set OPENROUTER_API_KEY in .env)")
    elif shadow.name == active.name:
        lines.append(f"   Shadow: <code>{shadow.name}</code> (same as boevoy — nothing to compare)")
    else:
        lines.append(f"   Shadow: <code>{shadow.name}</code> — {shadow.model}")
    lines.append("")
    if on:
        lines.append(
            "Each apply also writes a comparison set into "
            "<code>{Company}/{shadow}/</code> with the ATS score in the filename."
        )
    lines.append(
        "Use <code>/dual on</code> / <code>/dual off</code> to toggle, "
        "<code>/dual shadow &lt;name&gt;</code> to switch the shadow profile."
    )
    return "\n".join(lines)


def _set_shadow(name: str) -> str:
    from hunter.llm_profiles import list_available, set_shadow

    if not name:
        avail = ", ".join(p.name for p in list_available())
        return f"Usage: <code>/dual shadow &lt;name&gt;</code>\nAvailable: {avail}"
    try:
        set_shadow(name)
    except ValueError as e:
        return f"❌ {e}"
    return _status_text()


def _toggle(arg: str) -> str:
    from hunter.llm_profiles import set_dual, shadow_profile

    want = arg in ("on", "1", "true", "yes", "enable")
    if want and shadow_profile() is None:
        return (
            "❌ Cannot enable — shadow profile unavailable.\n"
            "Set <code>OPENROUTER_API_KEY</code> in .env (deepseek-v3)."
        )
    set_dual(want)
    return _status_text()


async def cmd_dual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dual — show or toggle dual-apply comparison mode."""
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    args = context.args or []
    if not args:
        text = await asyncio.to_thread(_status_text)
    elif args[0].strip().lower() == "shadow":
        name = args[1].strip().lower() if len(args) > 1 else ""
        text = await asyncio.to_thread(_set_shadow, name)
    else:
        text = await asyncio.to_thread(_toggle, args[0].strip().lower())

    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )
