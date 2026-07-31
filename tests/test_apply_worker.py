"""M1 (docs/HUNT_APPLY_SPLIT_PLAN.md) — apply_worker.py.

Two layers:
  - `_resolve_outcome` unit tests: one test per ApplyOutcome, checking the
    Telegram message + tracker side effect (release_claim / add_failed /
    neither) against the REAL (isolated) tracker DB.
  - `apply_worker_loop` integration tests: the claim -> resolve -> sleep
    cycle, the llm_outage pause skip, the consecutive-fail breaker, and
    resilience to an unexpected exception mid-loop. The infinite loop is
    stopped deterministically by making a mocked `tracker.claim_pending`
    raise `_StopLoop` (a bare `BaseException`, so it is NOT swallowed by the
    loop's `except Exception` resilience clause) after N real iterations.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter import apply_worker, tracker
from hunter.bot import state as bot_state
from hunter.models import Job


def _job(n: int = 1, **kwargs) -> Job:
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


class _StopLoop(BaseException):
    """Deliberately not an Exception subclass — must escape the loop's
    generic `except Exception` resilience clause and end the test."""


@pytest.fixture(autouse=True)
def _no_real_delay(monkeypatch):
    """Every test in this file runs against the fast/instant path."""
    monkeypatch.setattr(apply_worker, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(apply_worker, "POLL_INTERVAL_SEC", 0)
    monkeypatch.setattr(apply_worker, "_BACKOFF_SEC", 0)
    # Never touch the real repo tracker.db's config KV table.
    monkeypatch.setattr(apply_worker.llm_outage, "pause_remaining", MagicMock(return_value=0))
    # Readiness gate must pass by default — individual tests override.
    monkeypatch.setattr("hunter.main._check_apply_ready", MagicMock(return_value=None))
    # Reset the alert cooldown so readiness-alert tests don't leak state.
    apply_worker._last_ready_alert_at = 0.0
    bot_state._active_apply_urls.clear()
    yield
    bot_state._active_apply_urls.clear()


# ── _resolve_outcome: one test per ApplyOutcome ───────────────────────────────


def test_resolve_ok_delivers_and_notifies(tracker_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )
    deliver = AsyncMock()
    monkeypatch.setattr("hunter.delivery.deliver_apply_now", deliver)

    job = _job(1)
    # Real success leaves a terminal applied row (add_applied clears any
    # IN_PROGRESS placeholder). Without it, "ok" is treated as a soft abort.
    tracker.add_applied(
        {
            "company_name": job.company,
            "job_title": job.title,
            "apply_url": job.url,
            "stack": "Angular",
            "ats_score": "90",
            "output_folder": "/tmp/x",
        }
    )
    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "ok"))

    assert is_fail is False
    deliver.assert_awaited_once_with(job.url)
    assert any("Done" in t and job.company in t for t in sent)


def test_resolve_ok_shows_permalink_when_present(tracker_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )
    monkeypatch.setattr("hunter.delivery.deliver_apply_now", AsyncMock())

    job = _job(2, raw={"permalink": "https://real.example.com/p/2"})
    tracker.add_applied(
        {
            "company_name": job.company,
            "job_title": job.title,
            "apply_url": job.url,
            "stack": "Angular",
            "ats_score": "90",
            "output_folder": "/tmp/x",
        }
    )
    asyncio.run(apply_worker._resolve_outcome(None, 0, job, "ok"))

    assert any("https://real.example.com/p/2" in t for t in sent)


def test_resolve_ok_soft_abort_clears_in_progress_placeholder(tracker_db, monkeypatch):
    """Exit 0 with no terminal tracker write must not leave IN_PROGRESS."""
    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )
    deliver = AsyncMock()
    monkeypatch.setattr("hunter.delivery.deliver_apply_now", deliver)

    job = _job(20)
    tracker.add_pending(job)
    tracker.claim_pending()
    assert tracker.count_in_progress() == 1

    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "ok"))

    assert is_fail is False
    deliver.assert_not_awaited()
    assert tracker.lookup_url(job.url) == []
    assert tracker.count_in_progress() == 0
    assert any("without docs" in t for t in sent)
    assert not any("Done" in t for t in sent)


def test_resolve_ok_soft_terminal_skips_delivery(tracker_db, monkeypatch):
    """Exit 0 after EXPIRED/SKIP (terminal write) — no Done, no deliver."""
    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )
    deliver = AsyncMock()
    monkeypatch.setattr("hunter.delivery.deliver_apply_now", deliver)

    job = _job(21)
    tracker.add_pending(job)
    tracker.claim_pending()
    tracker.add_expired(job.url)  # clears IN_PROGRESS, writes EXPIRED

    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "ok"))

    assert is_fail is False
    deliver.assert_not_awaited()
    assert not any("Done" in t for t in sent)
    rows = tracker.lookup_url(job.url)
    assert len(rows) == 1
    # Convention: ats_status=SKIP, the EXPIRED marker lives in Sent.
    assert rows[0]["ats"] == "SKIP"
    assert (rows[0].get("sent") or "").upper() == "EXPIRED"


def test_resolve_manual_notifies_no_tracker_write(tracker_db, monkeypatch):
    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )

    job = _job(3)
    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "manual"))

    assert is_fail is False
    assert any("MANUAL" in t for t in sent)
    assert tracker.lookup_url(job.url) == []


def test_resolve_llm_outage_releases_claim_and_arms_pause(tracker_db, monkeypatch):
    job = _job(4)
    tracker.add_pending(job)
    claimed = tracker.claim_pending()
    assert claimed["ats_status"] == "IN_PROGRESS"

    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )
    arm_pause = MagicMock(return_value=1234567890)
    monkeypatch.setattr(apply_worker.llm_outage, "arm_pause", arm_pause)

    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "llm_outage"))

    assert is_fail is False
    arm_pause.assert_called_once()
    rows = tracker.lookup_url(job.url)
    assert len(rows) == 1
    assert rows[0]["ats"] == "PENDING"
    assert any("outage" in t.lower() for t in sent)


def test_resolve_cli_timeout_releases_claim_no_fail_row(tracker_db, monkeypatch):
    job = _job(5)
    tracker.add_pending(job)
    tracker.claim_pending()

    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )

    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "cli_timeout"))

    assert is_fail is False
    rows = tracker.lookup_url(job.url)
    assert len(rows) == 1
    assert rows[0]["ats"] == "PENDING"
    assert any("timed out" in t.lower() for t in sent)


def test_resolve_fail_writes_failed_row_replacing_placeholder(tracker_db, monkeypatch):
    job = _job(6)
    tracker.add_pending(job)
    tracker.claim_pending()

    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )

    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "fail"))

    assert is_fail is True
    rows = tracker.lookup_url(job.url)
    assert len(rows) == 1
    assert rows[0]["ats"] == "FAIL"
    assert any("Failed" in t for t in sent)


def test_resolve_rate_limited_writes_failed_row_and_counts_as_fail(tracker_db, monkeypatch):
    job = _job(7)
    tracker.add_pending(job)
    tracker.claim_pending()

    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )

    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "rate_limited"))

    assert is_fail is True
    rows = tracker.lookup_url(job.url)
    assert len(rows) == 1
    assert rows[0]["ats"] == "FAIL"
    assert any("rate-limited" in t.lower() for t in sent)


def test_resolve_unknown_outcome_defaults_to_fail(tracker_db, monkeypatch):
    """Defensive fallback — an outcome string this module doesn't recognize
    must still resolve safely instead of silently dropping the job."""
    job = _job(8)
    tracker.add_pending(job)
    tracker.claim_pending()
    monkeypatch.setattr(apply_worker, "send_text", AsyncMock())

    is_fail = asyncio.run(apply_worker._resolve_outcome(None, 0, job, "something_new"))

    assert is_fail is True
    rows = tracker.lookup_url(job.url)
    assert rows[0]["ats"] == "FAIL"


# ── apply_worker_loop: claim/resolve/sleep cycle + resilience ────────────────


def test_loop_claims_and_processes_one_pending_job(tracker_db, monkeypatch):
    job = _job(10)
    tracker.add_pending(job)

    subprocess_mock = AsyncMock(return_value="ok")
    monkeypatch.setattr(apply_worker, "run_apply_agent_subprocess", subprocess_mock)
    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )
    monkeypatch.setattr("hunter.delivery.deliver_apply_now", AsyncMock())

    real_claim = tracker.claim_pending
    calls = {"n": 0}

    def _claim_then_stop():
        calls["n"] += 1
        if calls["n"] > 1:
            raise _StopLoop()
        row = real_claim()
        # Simulate a successful apply writing the terminal row before the
        # worker resolves the "ok" outcome (what apply_agent does on exit 0
        # after generate_docs).
        tracker.add_applied(
            {
                "company_name": job.company,
                "job_title": job.title,
                "apply_url": job.url,
                "stack": "Angular",
                "ats_score": "90",
                "output_folder": "/tmp/x",
            }
        )
        return row

    monkeypatch.setattr(tracker, "claim_pending", MagicMock(side_effect=_claim_then_stop))

    with pytest.raises(_StopLoop):
        asyncio.run(apply_worker.apply_worker_loop(None, worker_id=0))

    subprocess_mock.assert_awaited_once()
    assert any("Processing" in t and job.company in t for t in sent)
    assert any("Done" in t for t in sent)


def test_loop_skips_claim_while_outage_pause_active(tracker_db, monkeypatch):
    pause_mock = MagicMock(side_effect=[5, 0])
    monkeypatch.setattr(apply_worker.llm_outage, "pause_remaining", pause_mock)
    monkeypatch.setattr(apply_worker, "send_text", AsyncMock())

    claim_mock = MagicMock(side_effect=[_StopLoop()])
    monkeypatch.setattr(tracker, "claim_pending", claim_mock)

    with pytest.raises(_StopLoop):
        asyncio.run(apply_worker.apply_worker_loop(None, worker_id=0))

    assert pause_mock.call_count == 2
    claim_mock.assert_called_once()  # not called during the paused iteration


def test_loop_skips_claim_when_apply_not_ready(tracker_db, monkeypatch):
    """Missing API key / CLI must not claim jobs (would burn FAIL rows)."""
    ready = MagicMock(side_effect=["LLM_API_KEY not set", None])
    monkeypatch.setattr("hunter.main._check_apply_ready", ready)
    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )

    claim_mock = MagicMock(side_effect=[_StopLoop()])
    monkeypatch.setattr(tracker, "claim_pending", claim_mock)
    subprocess_mock = AsyncMock()
    monkeypatch.setattr(apply_worker, "run_apply_agent_subprocess", subprocess_mock)

    with pytest.raises(_StopLoop):
        asyncio.run(apply_worker.apply_worker_loop(None, worker_id=0))

    assert any("not ready" in t.lower() for t in sent)
    # First poll: not ready → no claim. Second poll: ready → claim raises stop.
    assert claim_mock.call_count == 1
    subprocess_mock.assert_not_awaited()
    assert ready.call_count == 2


def test_loop_consecutive_fail_breaker_backs_off_and_resets(tracker_db, monkeypatch):
    jobs = [_job(20 + i) for i in range(3)]
    for j in jobs:
        tracker.add_pending(j)

    monkeypatch.setattr(apply_worker, "run_apply_agent_subprocess", AsyncMock(return_value="fail"))
    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )

    real_claim = tracker.claim_pending
    calls = {"n": 0}

    def _claim_then_stop():
        calls["n"] += 1
        if calls["n"] > 3:
            raise _StopLoop()
        return real_claim()

    monkeypatch.setattr(tracker, "claim_pending", MagicMock(side_effect=_claim_then_stop))

    with pytest.raises(_StopLoop):
        asyncio.run(apply_worker.apply_worker_loop(None, worker_id=0))

    assert any("consecutive failures" in t for t in sent)
    for j in jobs:
        rows = tracker.lookup_url(j.url)
        assert rows[0]["ats"] == "FAIL"


def test_loop_swallows_unexpected_exception_and_continues(tracker_db, monkeypatch):
    monkeypatch.setattr(apply_worker, "send_text", AsyncMock())
    claim_mock = MagicMock(side_effect=[RuntimeError("boom"), _StopLoop()])
    monkeypatch.setattr(tracker, "claim_pending", claim_mock)

    with pytest.raises(_StopLoop):
        asyncio.run(apply_worker.apply_worker_loop(None, worker_id=0))

    assert claim_mock.call_count == 2


def test_loop_releases_claim_when_duplicate_inflight(tracker_db, monkeypatch):
    """Worker must not start apply_agent if try_mark_apply_active fails."""
    job = _job(50)
    tracker.add_pending(job)
    assert bot_state.try_mark_apply_active(job.url) is True  # pretend manual run holds it

    sent = []
    monkeypatch.setattr(
        apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
    )
    subprocess_mock = AsyncMock()
    monkeypatch.setattr(apply_worker, "run_apply_agent_subprocess", subprocess_mock)

    real_claim = tracker.claim_pending
    calls = {"n": 0}

    def _claim_then_stop():
        calls["n"] += 1
        if calls["n"] > 1:
            raise _StopLoop()
        return real_claim()

    monkeypatch.setattr(tracker, "claim_pending", MagicMock(side_effect=_claim_then_stop))

    with pytest.raises(_StopLoop):
        asyncio.run(apply_worker.apply_worker_loop(None, worker_id=0))

    subprocess_mock.assert_not_awaited()
    assert any("already generating" in t.lower() for t in sent)
    rows = tracker.lookup_url(job.url)
    assert len(rows) == 1
    assert rows[0]["ats"] == "PENDING"
    """Exception after claim must not leave IN_PROGRESS until the stale sweep."""
    job = _job(40)
    tracker.add_pending(job)

    # First send_text (Processing) blows up; claim already succeeded.
    monkeypatch.setattr(apply_worker, "send_text", AsyncMock(side_effect=RuntimeError("tg down")))

    real_claim = tracker.claim_pending
    calls = {"n": 0}

    def _claim_then_stop():
        calls["n"] += 1
        if calls["n"] > 1:
            raise _StopLoop()
        return real_claim()

    monkeypatch.setattr(tracker, "claim_pending", MagicMock(side_effect=_claim_then_stop))

    with pytest.raises(_StopLoop):
        asyncio.run(apply_worker.apply_worker_loop(None, worker_id=0))

    rows = tracker.lookup_url(job.url)
    assert len(rows) == 1
    assert rows[0]["ats"] == "PENDING"
    assert tracker.count_in_progress() == 0
