"""
hunter/users.py — Telegram chat ↔ web-account linking (multi-user Phase B3).

The web API generates 6-char link codes (`telegram_link_codes`, 10-minute
expiry, uppercase hex, ISO-8601 UTC `expires_at`); this module consumes them
and maintains `telegram_links` (chat_id ↔ user_id, strictly one-to-one).
Both tables are created by hunter.db's multi-user DDL mirror — the API owns
the authoritative schema (docs/MULTI_USER_UPDATE.md, shared contract).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from hunter.db import get_db

log = logging.getLogger(__name__)


def _db_path() -> Path:
    # Follow hunter.tracker's DB_PATH at call time so the test suite's
    # tracker_db fixture (which monkeypatches tracker.DB_PATH onto an
    # isolated temp DB) covers this module without a second patch point.
    from hunter import tracker

    return tracker.DB_PATH


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expiry(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def link_chat(chat_id: int, code: str) -> str | None:
    """Consume a link code and bind chat_id to its user_id.

    Returns the linked user_id, or None for an unknown/expired/blank code.
    The code is single-use: deleted on success AND on an expired hit.
    Re-linking is a move, not an error — a user linking from a new chat
    replaces their old chat row, and a chat linking to a new account
    replaces its old user row (chat_id is PK, user_id is UNIQUE).
    """
    normalized = (code or "").strip().upper()
    if not normalized:
        return None
    now = _utcnow()
    with get_db(_db_path()) as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM telegram_link_codes WHERE code = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM telegram_link_codes WHERE code = ?", (normalized,))
        expires = _parse_expiry(row["expires_at"])
        if expires is None or expires < now:
            log.info("link code %s rejected: expired at %s", normalized, row["expires_at"])
            return None
        user_id = row["user_id"]
        conn.execute("DELETE FROM telegram_links WHERE user_id = ?", (user_id,))
        conn.execute(
            "INSERT OR REPLACE INTO telegram_links (chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (chat_id, user_id, now.isoformat()),
        )
    log.info("chat %s linked to user %s", chat_id, user_id)
    return user_id


def unlink_chat(chat_id: int) -> bool:
    """Remove the link for chat_id. Returns True if a link existed."""
    with get_db(_db_path()) as conn:
        cur = conn.execute("DELETE FROM telegram_links WHERE chat_id = ?", (chat_id,))
        removed = cur.rowcount > 0
    if removed:
        log.info("chat %s unlinked", chat_id)
    return removed
