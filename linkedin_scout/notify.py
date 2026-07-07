"""Telegram delivery for the LinkedIn posts scout (M3, task spec §3.2/§3.4).

Direct `requests.post` to the Telegram Bot API — no python-telegram-bot
`Application`, no polling, so this script can send even if the bot process
isn't running. Reuses `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` from
`hunter.config` (same `.env`), same minimal pattern as
`browser._send_circuit_breaker_alert` and `hunter/oauth_alert.py`.
"""

from __future__ import annotations

import logging

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from linkedin_scout.browser import ScoutCandidate
from linkedin_scout.seen_store import SeenStore, dedup_key

logger = logging.getLogger("linkedin_scout.notify")

# "First ~300 characters of the post text" — task spec §3.2.
_SNIPPET_CHARS = 300


def format_message(candidate: ScoutCandidate) -> str:
    """Author / profile-link-if-available / snippet / keyword / timestamp."""
    lines = [f"🔎 LinkedIn scout match — keyword: {candidate.keyword}", f"👤 {candidate.author}"]
    if candidate.author_profile_url:
        lines.append(candidate.author_profile_url)

    body = candidate.body.strip()
    snippet = body[:_SNIPPET_CHARS]
    if len(body) > _SNIPPET_CHARS:
        snippet += "…"

    lines.append("")
    lines.append(snippet)
    lines.append("")
    lines.append(f"🕒 {candidate.scouted_at}")
    return "\n".join(lines)


def _send_telegram(text: str) -> bool:
    """Direct, dependency-light Telegram send (sync). Best-effort."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("[linkedin_scout] no Telegram configured — message not sent")
        return False
    try:
        import requests

        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return resp.ok
    except Exception as e:  # noqa: BLE001 — best-effort, never fatal
        logger.warning("[linkedin_scout] telegram send failed: %s", e)
        return False


def notify_candidates(candidates: list[ScoutCandidate], seen_store: SeenStore) -> int:
    """Send one Telegram message per not-yet-seen candidate.

    Dedup check happens BEFORE send (task spec §3.3/§3.4); `seen_store` is
    marked + saved only after a successful send, so a failed send is retried
    on the next run rather than silently lost. Returns the number of messages
    actually sent.
    """
    sent = 0
    for candidate in candidates:
        key = dedup_key(candidate.author, candidate.body)
        if seen_store.is_seen(key):
            logger.info("[linkedin_scout] skip (already seen): %s", candidate.author)
            continue

        text = format_message(candidate)
        if _send_telegram(text):
            seen_store.mark_seen(key)
            seen_store.save()
            sent += 1
        else:
            logger.warning(
                "[linkedin_scout] send failed, not marking seen (will retry): %s",
                candidate.author,
            )
    return sent
