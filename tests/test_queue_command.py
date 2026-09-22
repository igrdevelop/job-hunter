"""M1 (docs/HUNT_APPLY_SPLIT_PLAN.md) — /queue command."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from hunter.models import Job


def _job(n: int, **kwargs) -> Job:
    defaults = {
        "title": f"Role {n}",
        "company": f"Co{n}",
        "location": "Remote",
        "salary": None,
        "url": f"https://example.com/job/{n}",
        "source": "test",
    }
    defaults.update(kwargs)
    return Job(**defaults)


def _run_cmd(args: list[str]) -> str:
    from hunter.commands.queue import cmd_queue

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args
    asyncio.run(cmd_queue(update, context))
    return update.message.reply_text.await_args.args[0]


def test_cmd_queue_off_by_default(tracker_db, monkeypatch):
    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", False)
    text = _run_cmd([])
    assert "off" in text.lower()


def test_cmd_queue_empty_when_enabled(tracker_db, monkeypatch):
    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    text = _run_cmd([])
    assert "Nothing waiting" in text
    assert "PENDING: <b>0</b>" in text


def test_cmd_queue_lists_pending_jobs_oldest_first(tracker_db, monkeypatch):
    from hunter import tracker

    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    for i in range(3):
        tracker.add_pending(_job(i))

    text = _run_cmd([])
    assert "PENDING: <b>3</b>" in text
    assert "Co0" in text and "Co1" in text and "Co2" in text
    assert text.index("Co0") < text.index("Co1") < text.index("Co2")


def test_cmd_queue_respects_limit_arg(tracker_db, monkeypatch):
    from hunter import tracker

    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    for i in range(5):
        tracker.add_pending(_job(i))

    text = _run_cmd(["2"])
    assert "Co0" in text and "Co1" in text
    assert "Co4" not in text
    assert "+3 more" in text


def test_cmd_queue_shows_in_progress_count(tracker_db, monkeypatch):
    from hunter import tracker

    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    tracker.add_pending(_job(1))
    tracker.claim_pending()

    text = _run_cmd([])
    assert "IN_PROGRESS: <b>1</b>" in text


def test_cmd_queue_header_shows_oldest_wait(tracker_db, monkeypatch):
    from hunter import tracker

    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    tracker.add_pending(_job(1))
    monkeypatch.setattr(tracker, "oldest_pending_wait_min", lambda now=None: 38)

    text = _run_cmd([])
    header = text.split("\n", 1)[0]
    assert "oldest waits <b>38 min</b>" in header


def test_cmd_queue_header_omits_wait_when_unknown(tracker_db, monkeypatch):
    """A legacy PENDING row without queued_at (or an empty queue) must not
    render a bogus wait."""
    from hunter import tracker

    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    job = _job(1)
    tracker.add_pending(job)
    with tracker.get_db(tracker.DB_PATH) as conn:
        conn.execute("UPDATE applications SET queued_at=NULL")

    text = _run_cmd([])
    assert "oldest waits" not in text
    assert "PENDING: <b>1</b>" in text


def test_cmd_queue_registered_in_dispatcher():
    from hunter import telegram_bot

    assert callable(telegram_bot.cmd_queue)
