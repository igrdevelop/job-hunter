"""Tests for hunter/commands/scoutfound.py — the /scoutfound command handler
that receives a candidate relayed from the standalone linkedin_scout script
via the owner's own Telegram user session.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from unittest.mock import patch

from hunter.commands.scoutfound import cmd_scoutfound

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "scout_payload_v1.json"


def _run(coro):
    return asyncio.run(coro)


class _FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class _FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


class _FakeUpdate:
    def __init__(self, chat_id: int) -> None:
        self.effective_chat = _FakeChat(chat_id)
        self.message = _FakeMessage()


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


# --- Payload contract v1 (docs/SCOUT_REPO_SPLIT_PLAN.md §5) ---------------
#
# tests/fixtures/scout_payload_v1.json is the golden fixture shared
# (byte-identical) between this repo's decoder test and the scout-side
# builder test in tests/test_linkedin_scout_telegram_relay.py — after the
# scout's planned move to its own private repo, this is the only thing that
# still proves the two sides agree on the schema.


def test_scoutfound_decodes_golden_fixture_v1():
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=111)
        context = _FakeContext(args=[_payload(fixture)])
        _run(cmd_scoutfound(update, context))

    assert len(calls) == 1
    assert calls[0] == fixture


def test_scoutfound_rejects_unsupported_version():
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=111)
        record = {"v": 3, "author": "A", "body": "We're hiring."}
        context = _FakeContext(args=[_payload(record)])
        _run(cmd_scoutfound(update, context))

    assert calls == []
    assert len(update.message.replies) == 1
    assert "v3" in update.message.replies[0]
    assert "not supported" in update.message.replies[0]


def test_scoutfound_rejects_v2_before_field_validation():
    """A future kind carries fields this version cannot know about.

    The version gate must run FIRST so the owner gets the actionable "update the
    bot" reply. Until v2 the body check ran first, which would have dropped such
    a payload mutely — the reply could never have fired.
    """
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=111)
        # v3 with neither `body` nor `url` — only the version gate can catch it.
        record = {"v": 3, "kind": "somethingnew", "payload": {"x": 1}}
        context = _FakeContext(args=[_payload(record)])
        _run(cmd_scoutfound(update, context))

    assert calls == []
    assert len(update.message.replies) == 1
    assert "v3" in update.message.replies[0]


# --- Payload contract v2: the scout's jobs track ---------------------------
#
# A "job" kind carries a REAL LinkedIn Jobs url and NO body (the bot fetches the
# description itself); a "post" carries body and no fetchable url. Golden
# fixture: tests/fixtures/scout_payload_v2_job.json, shared byte-identically
# with the scout repo's own builder test.

FIXTURE_V2_JOB = Path(__file__).parent / "fixtures" / "scout_payload_v2_job.json"


def _queue_calls(record: dict) -> list[dict]:
    calls: list[dict] = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        _run(cmd_scoutfound(_FakeUpdate(chat_id=111), _FakeContext(args=[_payload(record)])))
    return calls


def test_scoutfound_accepts_v2_job_without_body():
    record = {
        "v": 2,
        "kind": "job",
        "url": "https://www.linkedin.com/jobs/view/4451158730/",
        "title": "Senior Angular Developer",
        "company": "ACME",
    }
    calls = _queue_calls(record)

    assert len(calls) == 1
    assert calls[0]["url"].endswith("/4451158730/")


def test_scoutfound_rejects_v2_job_without_url():
    """`url` is to a job what `body` is to a post — without it there is nothing."""
    record = {"v": 2, "kind": "job", "title": "Senior Angular Developer"}

    assert _queue_calls(record) == []


def test_scoutfound_rejects_unknown_kind():
    record = {"v": 2, "kind": "carrier-pigeon", "body": "x", "url": "https://example.com"}

    assert _queue_calls(record) == []


def test_scoutfound_v2_post_still_requires_body():
    assert _queue_calls({"v": 2, "kind": "post", "author": "A"}) == []
    assert len(_queue_calls({"v": 2, "kind": "post", "author": "A", "body": "hiring"})) == 1


def test_scoutfound_decodes_golden_fixture_v2_job():
    fixture = json.loads(FIXTURE_V2_JOB.read_text(encoding="utf-8"))
    calls = _queue_calls(fixture)

    assert len(calls) == 1
    assert calls[0] == fixture


def test_scoutfound_treats_missing_version_as_v1():
    calls = []
    with (
        patch("hunter.commands.scoutfound.TELEGRAM_CHAT_ID", 111),
        patch(
            "hunter.sources.linkedin_scout_relay.append_to_queue",
            lambda rec: calls.append(rec),
        ),
    ):
        update = _FakeUpdate(chat_id=111)
        record = {"author": "A", "body": "We're hiring."}  # no "v" key at all
        context = _FakeContext(args=[_payload(record)])
        _run(cmd_scoutfound(update, context))

    assert len(calls) == 1
    assert update.message.replies == []
