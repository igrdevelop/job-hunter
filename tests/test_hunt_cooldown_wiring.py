"""B5 wiring — hunt loop must skip jobs that are within the cooldown window."""

import asyncio
import datetime
import uuid
from unittest.mock import patch, MagicMock


from hunter.models import Job
from hunter.main import run_hunt
from hunter.tracker import normalize_url
from hunter.db import get_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_job(company: str, title: str, url: str) -> Job:
    return Job(title=title, company=company, location="Remote", salary=None, url=url, source="test")


def _insert_cooldown_row(
    tracker_db, *, date_str, company: str, title: str, ats: str, url: str = ""
) -> None:
    """Insert a row with the given date into the test DB."""
    if not url:
        url = f"https://example.com/{uuid.uuid4().hex[:8]}"
    norm = normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (uuid.uuid4().hex[:8], str(date_str), company, title, ats, url, norm),
        )


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# core test: cooldown job never reaches send_job_cards
# ---------------------------------------------------------------------------


def test_cooldown_job_excluded_from_new_jobs(tracker_db) -> None:
    today = datetime.date.today()
    _insert_cooldown_row(
        tracker_db,
        date_str=today - datetime.timedelta(days=5),
        company="Acme",
        title="Angular Dev",
        url="https://example.com/job/1",
        ats="97%",
    )

    fresh_job = _make_job(
        "Acme", "Angular Dev", "https://justjoin.it/job-offer/acme-angular-dev-new"
    )
    unrelated_job = _make_job("OtherCo", "React Dev", "https://justjoin.it/job-offer/other-react")

    captured_cards: list[list[Job]] = []

    async def fake_send_cards(_ctx, jobs):
        captured_cards.append(jobs)

    async def fake_send_text(_ctx, *_a, **_kw):
        pass

    with (
        patch("hunter.main.AUTO_APPLY", False),
        patch("hunter.main.ALL_SOURCES", []),
        patch(
            "hunter.main.apply_filters_with_stats", return_value=([fresh_job, unrelated_job], {})
        ),
        patch("hunter.main.get_known_urls", return_value=set()),
        patch("hunter.main.get_known_company_titles", return_value=set()),
        patch("hunter.main.send_job_cards", fake_send_cards),
        patch("hunter.main.send_text", fake_send_text),
    ):
        _run(run_hunt(MagicMock()))

    assert captured_cards, "send_job_cards was never called"
    sent_companies = {j.company for j in captured_cards[0]}
    assert "Acme" not in sent_companies, "Acme should have been blocked by cooldown"
    assert "OtherCo" in sent_companies, "OtherCo should pass through"


def test_expired_cooldown_job_passes_through(tracker_db) -> None:
    """Job applied 20 days ago is past the 12-day default cooldown — must show again."""
    today = datetime.date.today()
    _insert_cooldown_row(
        tracker_db,
        date_str=today - datetime.timedelta(days=20),
        company="Acme",
        title="Angular Dev",
        url="https://example.com/old",
        ats="97%",
    )

    job = _make_job("Acme", "Angular Dev", "https://justjoin.it/job-offer/acme-new-posting")
    captured_cards: list[list[Job]] = []

    async def fake_send_cards(_ctx, jobs):
        captured_cards.append(jobs)

    async def fake_send_text(_ctx, *_a, **_kw):
        pass

    with (
        patch("hunter.main.AUTO_APPLY", False),
        patch("hunter.main.ALL_SOURCES", []),
        patch("hunter.main.apply_filters_with_stats", return_value=([job], {})),
        patch("hunter.main.get_known_urls", return_value=set()),
        patch("hunter.main.get_known_company_titles", return_value=set()),
        patch("hunter.main.send_job_cards", fake_send_cards),
        patch("hunter.main.send_text", fake_send_text),
    ):
        _run(run_hunt(MagicMock()))

    assert captured_cards, "send_job_cards was never called"
    sent_companies = {j.company for j in captured_cards[0]}
    assert "Acme" in sent_companies, "Acme should pass through after cooldown expires"
