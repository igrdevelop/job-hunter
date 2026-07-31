"""Force must not race a live apply-queue / in-flight generation (PR #177 Bugbot)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter.bot import state as bot_state
from hunter.commands import force
from hunter.models import Job
from hunter import tracker


def _job(url: str = "https://example.com/force-race") -> Job:
    return Job(
        title="Angular Dev",
        company="Acme",
        location="Remote",
        salary=None,
        url=url,
        source="test",
    )


class _Msg:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **_kw):
        self.replies.append(text)


class _Update:
    def __init__(self):
        self.message = _Msg()


@pytest.fixture(autouse=True)
def _clear_inflight():
    bot_state._active_apply_urls.clear()
    yield
    bot_state._active_apply_urls.clear()


def test_force_blocked_when_tracker_in_progress(tracker_db, monkeypatch):
    job = _job()
    tracker.add_pending(job)
    tracker.claim_pending()
    assert tracker.count_in_progress() == 1

    cleanup = AsyncMock()
    monkeypatch.setattr(force, "_force_cleanup", cleanup)
    run_agent = MagicMock()
    monkeypatch.setattr(force, "_run_apply_agent", run_agent)

    update = _Update()
    asyncio.run(force._force_run(update, url=job.url, body=""))

    assert any("IN_PROGRESS" in t or "Already generating" in t for t in update.message.replies)
    cleanup.assert_not_awaited()
    run_agent.assert_not_called()


def test_force_blocked_when_active_apply_urls(tracker_db, monkeypatch):
    job = _job("https://example.com/force-active")
    assert bot_state.try_mark_apply_active(job.url) is True

    cleanup = AsyncMock()
    monkeypatch.setattr(force, "_force_cleanup", cleanup)
    monkeypatch.setattr(force, "_run_apply_agent", MagicMock())

    update = _Update()
    asyncio.run(force._force_run(update, url=job.url, body=""))

    assert any("Already generating" in t for t in update.message.replies)
    cleanup.assert_not_awaited()


def test_force_allowed_for_pending_not_claimed(tracker_db, monkeypatch):
    job = _job("https://example.com/force-pending")
    tracker.add_pending(job)
    assert tracker.count_pending() == 1

    cleanup = AsyncMock(return_value="cleaned")
    monkeypatch.setattr(force, "_force_cleanup", cleanup)
    monkeypatch.setattr(force.asyncio, "create_task", MagicMock())

    update = _Update()
    asyncio.run(force._force_run(update, url=job.url, body=""))

    cleanup.assert_awaited_once()
    assert any("Starting generation" in t for t in update.message.replies)
