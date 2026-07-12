"""Tests for the consecutive-failure circuit breaker in hunter.main._retry_failed."""

import asyncio
from unittest.mock import AsyncMock

from hunter import main
from hunter.models import Job


def _job(n: int) -> Job:
    return Job(
        title=f"Role {n}",
        company=f"Co{n}",
        location="Remote",
        salary=None,
        url=f"https://www.pracuj.pl/praca/x,oferta,{n}",
        source="pracuj",
    )


def _wire(monkeypatch, jobs, outcomes):
    """Patch main's collaborators; return the AsyncMock standing in for apply."""
    monkeypatch.setattr(main, "get_failed_jobs", lambda: list(jobs))
    monkeypatch.setattr(main, "increment_fail_count", lambda url: 1)
    monkeypatch.setattr(main, "remove_failed", lambda url: None)
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_sync_to_sheets", AsyncMock())
    monkeypatch.setattr(main, "_upload_to_drive", AsyncMock())
    apply_mock = AsyncMock(side_effect=outcomes)
    monkeypatch.setattr(main, "_run_apply_agent", apply_mock)
    return apply_mock


def test_retry_stops_after_three_consecutive_failures(monkeypatch):
    jobs = [_job(i) for i in range(6)]
    apply_mock = _wire(monkeypatch, jobs, outcomes=["fail"] * 6)

    asyncio.run(main._retry_failed(context=None))

    # Breaker trips after the 3rd straight failure; jobs 4-6 are never touched.
    assert apply_mock.await_count == main._CONSECUTIVE_FAIL_LIMIT

    msgs = " ".join(c.args[1] for c in main.send_text.await_args_list)
    assert "consecutive failures" in msgs


def test_retry_success_resets_consecutive_counter(monkeypatch):
    jobs = [_job(i) for i in range(5)]
    # fail, fail, ok, fail, fail → never 3 in a row → all processed, no break.
    apply_mock = _wire(monkeypatch, jobs, outcomes=["fail", "fail", "ok", "fail", "fail"])

    asyncio.run(main._retry_failed(context=None))

    assert apply_mock.await_count == 5
    msgs = " ".join(c.args[1] for c in main.send_text.await_args_list)
    assert "consecutive failures" not in msgs
