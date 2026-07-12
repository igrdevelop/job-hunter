"""Relays scout candidates to the bot via the owner's own Telegram user
session (Telethon/MTProto), replacing the earlier local-queue-file design
(owner discovery 2026-07-08: the bot auto-deploys to its own server and does
not share a filesystem with this script's Windows desktop — a local queue
file the bot could never see was a dead end).

Why a USER session, not the bot's own token: Telegram never delivers a bot's
own outgoing sendMessage calls back to that same bot as an incoming update —
there is no way to make the bot's polling Application react to something it
sent to itself. Sending as a distinct account (the owner's own, via Telethon)
produces a genuine incoming message the bot's `/scoutfound` command handler
receives exactly like any other command.

Prerequisite: `python tools/telegram_user_login.py` once, to create the
session file at TELEGRAM_USER_SESSION (interactive: phone number + login
code, and 2FA password if enabled).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

from linkedin_scout.browser import ScoutCandidate
from linkedin_scout.seen_store import SeenStore, dedup_key

logger = logging.getLogger("linkedin_scout.telegram_relay")

# Telegram command messages have a 4096-char limit; base64 inflates by ~4/3.
# Cap the raw post body well under that so keyword/author/json overhead never
# pushes the encoded payload over the limit.
_MAX_BODY_CHARS = 3000

# Payload schema version (docs/SCOUT_REPO_SPLIT_PLAN.md §5). Bump on any field
# change and update BOTH this repo's payload builder and the bot-side decoder
# (hunter/commands/scoutfound.py) + tests/fixtures/scout_payload_v1.json in
# lockstep — after the repo split this contract spans two git histories, so
# nothing else catches drift.
PAYLOAD_VERSION = 1


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_payload(candidate: ScoutCandidate) -> str:
    """Base64(JSON) payload for the `/scoutfound <payload>` command."""
    body = candidate.body
    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS]
        logger.warning(
            "[linkedin_scout] post body truncated to %d chars for the Telegram payload",
            _MAX_BODY_CHARS,
        )
    record = {
        "v": PAYLOAD_VERSION,
        "keyword": candidate.keyword,
        "author": candidate.author,
        "body": body,
        "scouted_at": candidate.scouted_at,
        "author_profile_url": candidate.author_profile_url,
        "permalink": candidate.permalink,
    }
    raw = json.dumps(record, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def send_candidates(candidates: list[ScoutCandidate], seen_store: SeenStore) -> int:
    """Send one `/scoutfound` command per not-yet-seen candidate that has a
    captured permalink.

    Owner decision (2026-07-08): only candidates with a real, clickable
    LinkedIn post link (DOM marker or menu-click capture — see browser.py)
    are relayed to the bot; a candidate that passed the M1 heuristic gate but
    got no permalink is held back. It is deliberately NOT marked seen, so a
    later run (better DOM luck, or a selector fix) gets another shot at the
    same post instead of losing it silently.

    Dedup check happens BEFORE sending (same contract as the old queue-file
    design) so the same post is never relayed twice across scout runs.
    `seen_store` is marked + saved only after a successful send, so a failed
    send is retried on the next run instead of silently lost. Returns the
    count of messages actually sent.
    """
    api_id = _env("TELEGRAM_API_ID")
    api_hash = _env("TELEGRAM_API_HASH")
    bot_username = _env("TELEGRAM_BOT_USERNAME")
    session_path = _env("TELEGRAM_USER_SESSION")

    if not (api_id and api_hash and bot_username and session_path):
        logger.warning(
            "[linkedin_scout] TELEGRAM_API_ID/TELEGRAM_API_HASH/TELEGRAM_BOT_USERNAME/"
            "TELEGRAM_USER_SESSION not fully configured — run "
            "tools/telegram_user_login.py and set .env. Candidates NOT sent."
        )
        return 0
    if not Path(session_path).exists() and not Path(session_path + ".session").exists():
        logger.warning(
            "[linkedin_scout] no Telegram user session at %s — run "
            "tools/telegram_user_login.py first. Candidates NOT sent.",
            session_path,
        )
        return 0

    to_send = []
    for candidate in candidates:
        if not candidate.permalink:
            logger.info("[linkedin_scout] skip (no permalink captured): %s", candidate.author)
            continue
        key = dedup_key(candidate.author, candidate.body)
        if seen_store.is_seen(key):
            logger.info("[linkedin_scout] skip (already seen): %s", candidate.author)
            continue
        to_send.append((key, candidate))

    if not to_send:
        return 0

    from telethon.sync import TelegramClient

    sent = 0
    with TelegramClient(session_path, int(api_id), api_hash) as client:
        for key, candidate in to_send:
            payload = build_payload(candidate)
            try:
                client.send_message(bot_username, f"/scoutfound {payload}")
            except Exception as e:  # noqa: BLE001 — best-effort, one candidate must not block the rest
                logger.warning("[linkedin_scout] failed to relay %s: %s", candidate.author, e)
                continue
            seen_store.mark_seen(key)
            seen_store.save()
            sent += 1

    logger.info("[linkedin_scout] relayed %d/%d candidate(s) via Telegram", sent, len(to_send))
    return sent
