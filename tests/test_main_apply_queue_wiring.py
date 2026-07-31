"""M1 (docs/HUNT_APPLY_SPLIT_PLAN.md) — hunter/main.py's AUTO_APPLY branch
writes PENDING rows instead of calling _auto_apply_all when
APPLY_QUEUE_ENABLED is on. With the flag off, behavior is unchanged (already
covered by the pre-existing test_main_manual_only_partition.py etc.).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hunter.main import run_hunt
from hunter.models import Job


def _make_job(company: str, title: str, url: str, source: str = "justjoin") -> Job:
    return Job(title=title, company=company, location="Remote", salary=None, url=url, source=source)


class _FakeNormalSource:
    name = "justjoin"
    manual_only = False


def _boom(*_a, **_kw):
    raise AssertionError("must not be called when APPLY_QUEUE_ENABLED is on")


def test_auto_apply_writes_pending_rows_when_queue_enabled(tracker_db) -> None:
    job = _make_job("Acme", "Angular Dev", "https://justjoin.it/job-offer/acme")
    sent_texts: list[str] = []

    async def fake_send_text(_ctx, text, **_kw):
        sent_texts.append(text)

    with (
        patch("hunter.main.AUTO_APPLY", True),
        patch("hunter.main.APPLY_QUEUE_ENABLED", True),
        patch("hunter.main.ALL_SOURCES", [_FakeNormalSource()]),
        patch("hunter.main.apply_filters_with_stats", return_value=([job], {})),
        patch("hunter.main.get_known_urls", return_value=set()),
        patch("hunter.main.get_known_company_titles", return_value=set()),
        patch("hunter.main.send_job_cards", AsyncMock()),
        patch("hunter.main.send_text", fake_send_text),
        # The queue path must not touch any of the inline-apply machinery.
        patch("hunter.main._auto_apply_all", AsyncMock(side_effect=_boom)),
        # Readiness gate still runs before enqueue (must pass).
        patch("hunter.main._check_apply_ready", return_value=None),
        # Outage pause lives in the worker — hunt must not consult it here.
        patch("hunter.main.llm_outage.pause_remaining", side_effect=_boom),
    ):
        asyncio.run(run_hunt(MagicMock()))

    from hunter import tracker

    rows = tracker.lookup_url(job.url)
    assert len(rows) == 1
    assert rows[0]["ats"] == "PENDING"
    assert any("Queued" in t for t in sent_texts)


def test_auto_apply_does_not_queue_when_apply_not_ready(tracker_db) -> None:
    job = _make_job("Acme", "Angular Dev", "https://justjoin.it/job-offer/not-ready")
    sent_texts: list[str] = []

    async def fake_send_text(_ctx, text, **_kw):
        sent_texts.append(text)

    with (
        patch("hunter.main.AUTO_APPLY", True),
        patch("hunter.main.APPLY_QUEUE_ENABLED", True),
        patch("hunter.main.ALL_SOURCES", [_FakeNormalSource()]),
        patch("hunter.main.apply_filters_with_stats", return_value=([job], {})),
        patch("hunter.main.get_known_urls", return_value=set()),
        patch("hunter.main.get_known_company_titles", return_value=set()),
        patch("hunter.main.send_job_cards", AsyncMock()),
        patch("hunter.main.send_text", fake_send_text),
        patch("hunter.main._auto_apply_all", AsyncMock(side_effect=_boom)),
        patch("hunter.main._check_apply_ready", return_value="LLM_API_KEY not set"),
    ):
        asyncio.run(run_hunt(MagicMock()))

    from hunter import tracker

    assert tracker.lookup_url(job.url) == []
    assert any("not queuing" in t.lower() or "not ready" in t.lower() for t in sent_texts)


def test_auto_apply_respects_max_jobs_per_run_cap_when_queued(tracker_db) -> None:
    jobs = [
        _make_job(f"Co{i}", "Angular Dev", f"https://justjoin.it/job-offer/{i}") for i in range(3)
    ]
    sent_texts: list[str] = []

    async def fake_send_text(_ctx, text, **_kw):
        sent_texts.append(text)

    with (
        patch("hunter.main.AUTO_APPLY", True),
        patch("hunter.main.APPLY_QUEUE_ENABLED", True),
        patch("hunter.main.MAX_JOBS_PER_RUN", 2),
        patch("hunter.main.ALL_SOURCES", [_FakeNormalSource()]),
        patch("hunter.main.apply_filters_with_stats", return_value=(jobs, {})),
        patch("hunter.main.get_known_urls", return_value=set()),
        patch("hunter.main.get_known_company_titles", return_value=set()),
        patch("hunter.main.send_job_cards", AsyncMock()),
        patch("hunter.main.send_text", fake_send_text),
        patch("hunter.main._auto_apply_all", AsyncMock(side_effect=_boom)),
        patch("hunter.main._check_apply_ready", return_value=None),
    ):
        asyncio.run(run_hunt(MagicMock()))

    from hunter import tracker

    queued = sum(1 for j in jobs if tracker.lookup_url(j.url))
    assert queued == 2
    assert any("Capped to 2" in t for t in sent_texts)


@pytest.mark.parametrize("queue_enabled", [False])
def test_auto_apply_unchanged_when_queue_disabled(tracker_db, queue_enabled) -> None:
    job = _make_job("Acme", "Angular Dev", "https://justjoin.it/job-offer/acme2")
    auto_apply_called = []

    async def fake_auto_apply_all(_ctx, jobs):
        auto_apply_called.append(jobs)

    with (
        patch("hunter.main.AUTO_APPLY", True),
        patch("hunter.main.APPLY_QUEUE_ENABLED", queue_enabled),
        patch("hunter.main.ALL_SOURCES", [_FakeNormalSource()]),
        patch("hunter.main.apply_filters_with_stats", return_value=([job], {})),
        patch("hunter.main.get_known_urls", return_value=set()),
        patch("hunter.main.get_known_company_titles", return_value=set()),
        patch("hunter.main.send_job_cards", AsyncMock()),
        patch("hunter.main.send_text", AsyncMock()),
        patch("hunter.main._check_apply_ready", return_value=None),
        patch("hunter.main._auto_apply_all", fake_auto_apply_all),
    ):
        asyncio.run(run_hunt(MagicMock()))

    assert auto_apply_called, "_auto_apply_all must still run with the flag off"
    from hunter import tracker

    # The old path never writes a tracker row itself — _auto_apply_all is
    # mocked, so no row exists at all (neither PENDING nor otherwise).
    assert tracker.lookup_url(job.url) == []
