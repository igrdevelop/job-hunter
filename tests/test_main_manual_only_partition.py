"""hunter/main.py's ACT-step partition: manual_only sources (e.g.
linkedin_scout_relay) must ALWAYS get a Telegram Apply/Skip card, even under
AUTO_APPLY=true — see hunter/sources/base.py::BaseSource.manual_only and the
"LinkedIn Posts Scout" section of CLAUDE.md.
"""

import asyncio
from unittest.mock import MagicMock, patch

from hunter.main import run_hunt
from hunter.models import Job


def _make_job(company: str, title: str, url: str, source: str) -> Job:
    return Job(
        title=title, company=company, location="Remote",
        salary=None, url=url, source=source,
    )


class _FakeManualOnlySource:
    name = "linkedin_scout_relay"
    manual_only = True


class _FakeNormalSource:
    name = "justjoin"
    manual_only = False


def _run(coro):
    return asyncio.run(coro)


def test_manual_only_job_gets_card_even_under_auto_apply(tracker_db) -> None:
    manual_job = _make_job("Deloitte", "[LI post] hiring", "https://linkedin.com/scout-posts/#pabc", "linkedin_scout_relay")
    auto_job = _make_job("Acme", "Angular Dev", "https://justjoin.it/job-offer/acme", "justjoin")

    captured_cards: list[list[Job]] = []
    auto_applied: list[list[Job]] = []

    async def fake_send_cards(_ctx, jobs):
        captured_cards.append(jobs)

    async def fake_send_text(_ctx, *_a, **_kw):
        pass

    async def fake_auto_apply_all(_ctx, jobs):
        auto_applied.append(jobs)

    async def fake_retry_failed(_ctx):
        pass

    with (
        patch("hunter.main.AUTO_APPLY", True),
        patch("hunter.main.ALL_SOURCES", [_FakeManualOnlySource(), _FakeNormalSource()]),
        patch(
            "hunter.main.apply_filters_with_stats",
            return_value=([manual_job, auto_job], {}),
        ),
        patch("hunter.main.get_known_urls", return_value=set()),
        patch("hunter.main.get_known_company_titles", return_value=set()),
        patch("hunter.main.send_job_cards", fake_send_cards),
        patch("hunter.main.send_text", fake_send_text),
        patch("hunter.main._check_apply_ready", return_value=None),
        patch("hunter.main._auto_apply_all", fake_auto_apply_all),
        patch("hunter.main._retry_failed", fake_retry_failed),
    ):
        _run(run_hunt(MagicMock()))

    assert captured_cards, "send_job_cards was never called for the manual_only job"
    card_companies = {j.company for j in captured_cards[0]}
    assert "Deloitte" in card_companies

    assert auto_applied, "_auto_apply_all was never called for the auto-eligible job"
    auto_companies = {j.company for j in auto_applied[0]}
    assert "Acme" in auto_companies
    assert "Deloitte" not in auto_companies


def test_all_manual_only_jobs_skip_auto_apply_entirely(tracker_db) -> None:
    manual_job = _make_job("Deloitte", "[LI post] hiring", "https://linkedin.com/scout-posts/#pxyz", "linkedin_scout_relay")

    captured_cards: list[list[Job]] = []
    auto_apply_called = []

    async def fake_send_cards(_ctx, jobs):
        captured_cards.append(jobs)

    async def fake_send_text(_ctx, *_a, **_kw):
        pass

    async def fake_auto_apply_all(_ctx, jobs):
        auto_apply_called.append(jobs)

    with (
        patch("hunter.main.AUTO_APPLY", True),
        patch("hunter.main.ALL_SOURCES", [_FakeManualOnlySource()]),
        patch("hunter.main.apply_filters_with_stats", return_value=([manual_job], {})),
        patch("hunter.main.get_known_urls", return_value=set()),
        patch("hunter.main.get_known_company_titles", return_value=set()),
        patch("hunter.main.send_job_cards", fake_send_cards),
        patch("hunter.main.send_text", fake_send_text),
        patch("hunter.main._auto_apply_all", fake_auto_apply_all),
    ):
        _run(run_hunt(MagicMock()))

    assert captured_cards
    assert not auto_apply_called, "_auto_apply_all must not run when only manual_only jobs are new"
