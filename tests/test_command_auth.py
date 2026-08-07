"""Multi-user Phase B3 item 3 — per-chat authorization (hunter/bot/auth.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from hunter.bot import auth
from hunter.db import get_db

ADMIN_CHAT = 777000
OWNER = "owner-uid"
OTHER = "other-uid"


def _link(db, chat_id: int, user_id: str) -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO telegram_links (chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (chat_id, user_id, datetime.now(timezone.utc).isoformat()),
        )


def _env(monkeypatch, admin: int = ADMIN_CHAT, owner: str = OWNER) -> None:
    monkeypatch.setattr("hunter.config.TELEGRAM_CHAT_ID", admin)
    monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", owner)


def _update(chat_id: int | None, *, callback: bool = False) -> MagicMock:
    update = MagicMock()
    if chat_id is None:
        update.effective_chat = None
    else:
        update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    if callback:
        update.callback_query.answer = AsyncMock()
    else:
        update.callback_query = None
    return update


def _call(decorator, update) -> tuple[AsyncMock, MagicMock]:
    inner = AsyncMock(return_value="ran")
    inner.__name__ = "inner"
    wrapped = decorator(inner)
    result = asyncio.run(wrapped(update, MagicMock()))
    return inner, result


# ── require_owner ─────────────────────────────────────────────────────────────


def test_admin_chat_passes_without_link_row(tracker_db, monkeypatch):
    _env(monkeypatch)
    inner, result = _call(auth.require_owner, _update(ADMIN_CHAT))
    inner.assert_awaited_once()
    assert result == "ran"


def test_linked_owner_passes(tracker_db, monkeypatch):
    _env(monkeypatch)
    _link(tracker_db, 123, OWNER)
    inner, _ = _call(auth.require_owner, _update(123))
    inner.assert_awaited_once()


def test_linked_non_owner_rejected_with_owner_only(tracker_db, monkeypatch):
    _env(monkeypatch)
    _link(tracker_db, 456, OTHER)
    update = _update(456)
    inner, _ = _call(auth.require_owner, update)
    inner.assert_not_awaited()
    text = update.message.reply_text.await_args.args[0]
    assert "admin only" in text


def test_stranger_rejected_with_not_linked(tracker_db, monkeypatch):
    _env(monkeypatch)
    update = _update(999)
    inner, _ = _call(auth.require_owner, update)
    inner.assert_not_awaited()
    text = update.message.reply_text.await_args.args[0]
    assert "/link" in text


def test_callback_rejection_uses_answer_alert(tracker_db, monkeypatch):
    _env(monkeypatch)
    update = _update(999, callback=True)
    inner, _ = _call(auth.require_owner, update)
    inner.assert_not_awaited()
    update.callback_query.answer.assert_awaited_once()
    assert update.callback_query.answer.await_args.kwargs.get("show_alert") is True


def test_unset_admin_chat_zero_never_authorizes(tracker_db, monkeypatch):
    _env(monkeypatch, admin=0)
    update = _update(0)
    inner, _ = _call(auth.require_owner, update)
    inner.assert_not_awaited()


def test_missing_chat_rejected_silently(tracker_db, monkeypatch):
    _env(monkeypatch)
    update = _update(None)
    update.message = None
    update.callback_query = None
    inner, result = _call(auth.require_owner, update)
    inner.assert_not_awaited()
    assert result is None


# ── require_user ──────────────────────────────────────────────────────────────


def test_require_user_linked_non_owner_passes(tracker_db, monkeypatch):
    _env(monkeypatch)
    _link(tracker_db, 456, OTHER)
    inner, _ = _call(auth.require_user, _update(456))
    inner.assert_awaited_once()


def test_require_user_stranger_rejected(tracker_db, monkeypatch):
    _env(monkeypatch)
    update = _update(999)
    inner, _ = _call(auth.require_user, update)
    inner.assert_not_awaited()
    assert "/link" in update.message.reply_text.await_args.args[0]


def test_require_user_admin_chat_passes_without_default_user_id(tracker_db, monkeypatch):
    """Single-user dev setup: no DEFAULT_USER_ID, admin chat still works."""
    _env(monkeypatch, owner="")
    inner, _ = _call(auth.require_user, _update(ADMIN_CHAT))
    inner.assert_awaited_once()


# ── is_owner / authorized_user helpers ────────────────────────────────────────


def test_is_owner_admin_chat(tracker_db, monkeypatch):
    _env(monkeypatch)
    assert auth.is_owner(_update(ADMIN_CHAT)) is True


def test_is_owner_linked_owner_and_non_owner(tracker_db, monkeypatch):
    _env(monkeypatch)
    _link(tracker_db, 123, OWNER)
    _link(tracker_db, 456, OTHER)
    assert auth.is_owner(_update(123)) is True
    assert auth.is_owner(_update(456)) is False


def test_authorized_user_resolves_link(tracker_db, monkeypatch):
    _env(monkeypatch)
    _link(tracker_db, 456, OTHER)
    assert auth.authorized_user(_update(456)) == OTHER
    assert auth.authorized_user(_update(ADMIN_CHAT)) == OWNER
    assert auth.authorized_user(_update(999)) is None
