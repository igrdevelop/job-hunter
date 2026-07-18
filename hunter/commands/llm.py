"""commands/llm.py — /llm command: show and switch the active LLM profile.

/llm                — show current profile + all available options with cost estimates
/llm <name>         — switch to the named profile (persisted to tracker.db)
/llm outage         — show the LLM-outage auto-apply pause state (M2)
/llm outage clear   — lift the pause early (docs/LLM_OUTAGE_RESILIENCE_PLAN.md)
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def _build_status_text() -> str:
    from hunter.llm_profiles import PROFILES, get_active, list_available

    active = get_active()
    available = list_available()
    available_names = {p.name for p in available}

    lines = [f"🤖 <b>LLM generator: {active.name}</b>"]
    lines.append(f"   Model: <code>{active.model}</code>")
    lines.append(f"   Provider: {active.provider}")
    lines.append(f"   Cost: {active.cost_estimate()}")
    lines.append("")

    lines.append("<b>Available profiles:</b>")
    for name, prof in PROFILES.items():
        if not prof.is_available():
            continue
        marker = " ← active" if name == active.name else ""
        lines.append(
            f"  • <code>/llm {name}</code> — {prof.model} ({prof.cost_estimate()}){marker}"
        )

    unavailable = [name for name, p in PROFILES.items() if name not in available_names]
    if unavailable:
        lines.append("")
        lines.append("<b>Unavailable (missing API key):</b>")
        for name in unavailable:
            p = PROFILES[name]
            lines.append(f"  • {name} — set <code>{p.env_key}</code> in .env")

    return "\n".join(lines)


def _switch_profile(name: str) -> str:
    from hunter.llm_profiles import PROFILES, set_active

    try:
        profile = set_active(name)
        return (
            f"✅ <b>Switched to {profile.name}</b>\n"
            f"Model: <code>{profile.model}</code>\n"
            f"Cost: {profile.cost_estimate()}\n\n"
            f"Takes effect on the next vacancy (no restart needed)."
        )
    except ValueError as e:
        known = ", ".join(f"<code>{n}</code>" for n in PROFILES)
        return f"❌ {e}\n\nKnown profiles: {known}"


def _outage_text(subargs: list[str]) -> str:
    """/llm outage [clear] — inspect/lift the M2 auto-apply pause."""
    from hunter import llm_outage

    if subargs and subargs[0].lower() == "clear":
        if llm_outage.clear_pause():
            return "▶️ LLM-outage pause lifted — auto-apply resumes on the next slot."
        return "ℹ️ No active LLM-outage pause."

    left = llm_outage.pause_remaining()
    if not left:
        return "✅ No LLM-outage pause active."
    mins = (left + 59) // 60
    return (
        f"⏸ <b>Auto-apply paused</b> (LLM outage) — ~{mins} min left.\n"
        "<code>/llm outage clear</code> to lift early once the account is fixed."
    )


async def cmd_llm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/llm — show/switch the LLM profile; /llm outage [clear] — the M2 pause."""
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        return

    args = context.args or []

    if not args:
        text = await asyncio.to_thread(_build_status_text)
    elif args[0].strip().lower() == "outage":
        text = await asyncio.to_thread(_outage_text, args[1:])
    else:
        name = args[0].strip().lower()
        text = await asyncio.to_thread(_switch_profile, name)

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
