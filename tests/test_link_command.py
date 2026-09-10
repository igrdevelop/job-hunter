"""Multi-user Phase B3 item 1 — /link + /unlink (hunter/users.py + commands/link.py).

Also covers the M4 brute-force limiter and old-chat displacement notice
(docs/improvement-2026-09/05-SECURITY_PLAN.md finding #4).
"""

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


# ── M4: /link brute-force limiter ─────────────────────────────────────────────


def test_sixth_wrong_code_refused_without_querying_codes_table(tracker_db, monkeypatch):
    """>= LINK_ATTEMPT_LIMIT failures block further attempts WITHOUT even
    reading telegram_link_codes — verified with a SQL spy, not just the
    outcome, per docs/improvement-2026-09/05-SECURITY_PLAN.md M4.

    sqlite3.Connection is a C-level immutable type, so its `execute` method
    can't be monkeypatched directly — instead hunter.users.get_db itself is
    swapped for a thin recording proxy around the real connection.
    """
    import contextlib

    from hunter.db import get_db as real_get_db

    _seed_code(tracker_db, "REALCD")  # a genuinely valid code, never touched

    for _ in range(users.LINK_ATTEMPT_LIMIT):
        assert users.link_chat(CHAT_ID, "WRONG1") is None

    calls: list[str] = []

    class _SpyConn:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            calls.append(sql)
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextlib.contextmanager
    def spy_get_db(path):
        with real_get_db(path) as conn:
            yield _SpyConn(conn)

    monkeypatch.setattr(users, "get_db", spy_get_db)

    result = users.link_chat_with_details(CHAT_ID, "REALCD")
    assert result.rate_limited is True
    assert result.user_id is None
    assert not any("telegram_link_codes" in sql for sql in calls)
    assert _codes(tracker_db) == ["REALCD"]  # untouched
    assert _links(tracker_db) == []


def test_fewer_than_limit_wrong_codes_not_blocked(tracker_db):
    for _ in range(users.LINK_ATTEMPT_LIMIT - 1):
        assert users.link_chat(CHAT_ID, "WRONG1") is None
    result = users.link_chat_with_details(CHAT_ID, "WRONG1")
    assert result.rate_limited is False  # still an ordinary invalid-code failure


def test_window_expiry_resets_attempt_count(tracker_db, monkeypatch):
    for _ in range(users.LINK_ATTEMPT_LIMIT):
        assert users.link_chat(CHAT_ID, "WRONG1") is None
    assert users.link_chat_with_details(CHAT_ID, "WRONG1").rate_limited is True

    future = datetime.now(timezone.utc) + timedelta(minutes=users.LINK_ATTEMPT_WINDOW_MIN + 1)
    monkeypatch.setattr(users, "_utcnow", lambda: future)

    result = users.link_chat_with_details(CHAT_ID, "WRONG1")
    assert result.rate_limited is False  # window elapsed — fresh count


def test_successful_link_clears_attempt_counter(tracker_db):
    for _ in range(users.LINK_ATTEMPT_LIMIT - 1):
        assert users.link_chat(CHAT_ID, "WRONG1") is None

    _seed_code(tracker_db, "GOOD01")
    assert users.link_chat(CHAT_ID, "GOOD01") == USER_ID

    with get_db(tracker_db) as conn:
        row = conn.execute(
            "SELECT attempts FROM link_attempts WHERE chat_id = ?", (CHAT_ID,)
        ).fetchone()
    assert row is None  # counter row deleted on success

    # A fresh run of wrong codes after a success is NOT pre-blocked.
    for _ in range(users.LINK_ATTEMPT_LIMIT - 1):
        assert users.link_chat(CHAT_ID, "WRONG2") is None
    assert users.link_chat_with_details(CHAT_ID, "WRONG2").rate_limited is False


def test_cmd_link_rate_limited_reply(tracker_db):
    for _ in range(users.LINK_ATTEMPT_LIMIT):
        _run_cmd("cmd_link", ["WRONG1"])
    text = _run_cmd("cmd_link", ["WRONG1"])
    assert "Too many" in text


def test_cmd_link_rate_limit_is_per_chat(tracker_db):
    """A different chat's own attempts are unaffected by CHAT_ID's block."""
    for _ in range(users.LINK_ATTEMPT_LIMIT):
        _run_cmd("cmd_link", ["WRONG1"], chat_id=CHAT_ID)
    assert users.link_chat_with_details(CHAT_ID, "WRONG1").rate_limited is True
    assert users.link_chat_with_details(555444, "WRONG1").rate_limited is False


# ── M4: old-chat displacement notice ──────────────────────────────────────────


def test_cmd_link_notifies_old_chat_on_relink(tracker_db, monkeypatch):
    """The SAME user re-linking from a NEW chat sends a best-effort notice
    to the chat that just lost its link."""
    from hunter.commands import link as link_module

    _seed_code(tracker_db, "AAAAAA")
    users.link_chat(CHAT_ID, "AAAAAA")  # user first linked from CHAT_ID

    _seed_code(tracker_db, "BBBBBB")
    notified: list[tuple[int, str]] = []

    async def fake_notify(text, chat_id=None):
        notified.append((chat_id, text))

    monkeypatch.setattr(link_module, "_tg_notify", fake_notify)

    new_chat = 999888
    text = _run_cmd("cmd_link", ["BBBBBB"], chat_id=new_chat)
    assert "Linked" in text
    assert _links(tracker_db) == [(new_chat, USER_ID)]
    assert len(notified) == 1
    old_chat, notice_text = notified[0]
    assert old_chat == CHAT_ID
    assert "linked to another" in notice_text.lower()


def test_cmd_link_no_notice_when_same_chat_relinks(tracker_db, monkeypatch):
    """Re-linking the SAME chat (no chat change) never fires the notice."""
    from hunter.commands import link as link_module

    _seed_code(tracker_db, "AAAAAA")
    users.link_chat(CHAT_ID, "AAAAAA")

    _seed_code(tracker_db, "BBBBBB")
    notify = AsyncMock()
    monkeypatch.setattr(link_module, "_tg_notify", notify)

    _run_cmd("cmd_link", ["BBBBBB"], chat_id=CHAT_ID)
    notify.assert_not_called()


def test_cmd_link_notify_failure_does_not_break_success_reply(tracker_db, monkeypatch):
    """The old-chat notice is best-effort — a Telegram send failure must not
    turn a successful re-link into a user-visible error."""
    from hunter.commands import link as link_module

    _seed_code(tracker_db, "AAAAAA")
    users.link_chat(CHAT_ID, "AAAAAA")
    _seed_code(tracker_db, "BBBBBB")

    async def boom(text, chat_id=None):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(link_module, "_tg_notify", boom)

    text = _run_cmd("cmd_link", ["BBBBBB"], chat_id=999888)
    assert "Linked" in text


def test_link_accepts_longer_than_six_char_code(tracker_db):
    """No hardcoded length check — a future longer API-side code (e.g.
    randomBytes(8)) works without a bot change."""
    _seed_code(tracker_db, "A1B2C3D4E5F6")
    assert users.link_chat(CHAT_ID, "A1B2C3D4E5F6") == USER_ID
