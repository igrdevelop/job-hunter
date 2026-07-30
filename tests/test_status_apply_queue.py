"""M1 (docs/HUNT_APPLY_SPLIT_PLAN.md) — /status shows the apply-queue counts
only when APPLY_QUEUE_ENABLED is on.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _run_status() -> str:
    from hunter.commands.status import cmd_status

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    asyncio.run(cmd_status(update, context))
    return update.message.reply_text.await_args.args[0]


def test_status_omits_queue_line_when_disabled(tracker_db, monkeypatch):
    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", False)
    text = _run_status()
    assert "Apply queue" not in text


def test_status_shows_queue_counts_when_enabled(tracker_db, monkeypatch):
    from hunter import tracker

    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    from hunter.models import Job

    job = Job(
        title="Dev",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://example.com/job/1",
        source="test",
    )
    tracker.add_pending(job)

    text = _run_status()
    assert "Apply queue" in text
    assert "PENDING <b>1</b>" in text
    assert "IN_PROGRESS <b>0</b>" in text
