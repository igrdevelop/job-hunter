"""Tests for hunter/commands/scoutfound.py — the /scoutfound command handler
that receives a candidate relayed from the standalone linkedin_scout script
via the owner's own Telegram user session.
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import patch

from hunter.commands.scoutfound import cmd_scoutfound


def _run(coro):
    return asyncio.run(coro)


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeUpdate:
    def __init__(self, chat_id: int) -> None:
        self.effective_chat = _FakeChat(chat_id)


class _FakeContext:
    def __init__(self, args: list[str]) -> None:
        self.args = args


def _payload(record: dict) -> str:
    return base64.b64encode(json.dumps(record).encode("utf-8")).decode("ascii")


def test_scoutfound_rejects_wrong_chat_id():
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=999)
        context = _FakeContext(args=[_payload({"author": "A", "body": "We're hiring."})])
        _run(cmd_scoutfound(update, context))

    assert calls == []


def test_scoutfound_accepts_owner_chat_and_queues(tmp_path):
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=111)
        record = {"author": "Deloitte Poland", "body": "We're hiring an Angular Developer."}
        context = _FakeContext(args=[_payload(record)])
        _run(cmd_scoutfound(update, context))

    assert len(calls) == 1
    assert calls[0]["author"] == "Deloitte Poland"


def test_scoutfound_ignores_missing_args():
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=111)
        context = _FakeContext(args=[])
        _run(cmd_scoutfound(update, context))

    assert calls == []


def test_scoutfound_ignores_malformed_payload():
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=111)
        context = _FakeContext(args=["not-valid-base64!!!"])
        _run(cmd_scoutfound(update, context))

    assert calls == []


def test_scoutfound_ignores_payload_without_body():
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=111)
        context = _FakeContext(args=[_payload({"author": "A"})])
        _run(cmd_scoutfound(update, context))

    assert calls == []
