"""Multi-user Phase B3 item 1 — /link + /unlink (hunter/users.py + commands/link.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from hunter import users
from hunter.db import get_db

CHAT_ID = 111222333
USER_ID = "user-abc-123"


def _seed_code(db, code: str, user_id: str = USER_ID, expires_in_min: int = 10) -> None:
    expires = datetime.now(timezone.utc) + timedelta(minutes=expires_in_min)
    # Mirror the API's format: uppercase hex code, ISO-8601 UTC expiry.
    with get_db(db) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_link_codes (code, user_id, expires_at)"
            " VALUES (?, ?, ?)",
            (code, user_id, expires.isoformat().replace("+00:00", "Z")),
        )


def _links(db) -> list[tuple[int, str]]:
    with get_db(db) as conn:
        rows = conn.execute("SELECT chat_id, user_id FROM telegram_links").fetchall()
    return [(r["chat_id"], r["user_id"]) for r in rows]


def _codes(db) -> list[str]:
    with get_db(db) as conn:
        return [r["code"] for r in conn.execute("SELECT code FROM telegram_link_codes")]


def _run_cmd(handler_name: str, args: list[str], chat_id: int = CHAT_ID) -> str:
    from hunter.commands import link as link_module

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = chat_id
    context = MagicMock()
    context.args = args
    asyncio.run(getattr(link_module, handler_name)(update, context))
    return update.message.reply_text.await_args.args[0]


# ── users.link_chat / unlink_chat ─────────────────────────────────────────────


def test_link_chat_valid_code(tracker_db):
    _seed_code(tracker_db, "A1B2C3")
    assert users.link_chat(CHAT_ID, "A1B2C3") == USER_ID
    assert _links(tracker_db) == [(CHAT_ID, USER_ID)]
    assert _codes(tracker_db) == []  # single-use


def test_link_chat_lowercase_and_whitespace_accepted(tracker_db):
    _seed_code(tracker_db, "A1B2C3")
    assert users.link_chat(CHAT_ID, "  a1b2c3 ") == USER_ID


def test_link_chat_unknown_code(tracker_db):
    assert users.link_chat(CHAT_ID, "NOPE99") is None
    assert _links(tracker_db) == []


def test_link_chat_expired_code_rejected_and_purged(tracker_db):
    _seed_code(tracker_db, "OLD001", expires_in_min=-1)
    assert users.link_chat(CHAT_ID, "OLD001") is None
    assert _links(tracker_db) == []
    assert _codes(tracker_db) == []  # expired hit still consumes the code


def test_link_chat_blank_code(tracker_db):
    assert users.link_chat(CHAT_ID, "   ") is None


def test_relink_same_user_from_new_chat_moves_link(tracker_db):
    _seed_code(tracker_db, "AAAAAA")
    users.link_chat(CHAT_ID, "AAAAAA")
    _seed_code(tracker_db, "BBBBBB")
    assert users.link_chat(999, "BBBBBB") == USER_ID
    assert _links(tracker_db) == [(999, USER_ID)]  # old chat row gone


def test_relink_same_chat_to_new_user_replaces(tracker_db):
    _seed_code(tracker_db, "AAAAAA", user_id="user-one")
    users.link_chat(CHAT_ID, "AAAAAA")
    _seed_code(tracker_db, "BBBBBB", user_id="user-two")
    assert users.link_chat(CHAT_ID, "BBBBBB") == "user-two"
    assert _links(tracker_db) == [(CHAT_ID, "user-two")]


def test_unlink_chat(tracker_db):
    _seed_code(tracker_db, "A1B2C3")
    users.link_chat(CHAT_ID, "A1B2C3")
    assert users.unlink_chat(CHAT_ID) is True
    assert _links(tracker_db) == []
    assert users.unlink_chat(CHAT_ID) is False


# ── /link and /unlink handlers ────────────────────────────────────────────────


def test_cmd_link_success(tracker_db):
    _seed_code(tracker_db, "A1B2C3")
    text = _run_cmd("cmd_link", ["A1B2C3"])
    assert "Linked" in text
    assert _links(tracker_db) == [(CHAT_ID, USER_ID)]


def test_cmd_link_invalid_code(tracker_db):
    text = _run_cmd("cmd_link", ["ZZZZZZ"])
    assert "Invalid or expired" in text


def test_cmd_link_no_args_shows_usage(tracker_db):
    text = _run_cmd("cmd_link", [])
    assert "Usage" in text
    assert _links(tracker_db) == []


def test_cmd_unlink_linked(tracker_db):
    _seed_code(tracker_db, "A1B2C3")
    users.link_chat(CHAT_ID, "A1B2C3")
    text = _run_cmd("cmd_unlink", [])
    assert "Unlinked" in text
    assert _links(tracker_db) == []


def test_cmd_unlink_not_linked(tracker_db):
    text = _run_cmd("cmd_unlink", [])
    assert "not linked" in text


def test_handlers_registered_in_dispatcher():
    from hunter import telegram_bot

    assert callable(telegram_bot.cmd_link)
    assert callable(telegram_bot.cmd_unlink)
