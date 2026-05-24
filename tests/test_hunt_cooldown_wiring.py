"""B5 wiring — hunt loop must skip jobs that are within the cooldown window."""
import asyncio
import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import openpyxl
import pytest

from hunter.models import Job
from hunter.main import run_hunt


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_job(company: str, title: str, url: str) -> Job:
    return Job(title=title, company=company, location="Remote",
               salary=None, url=url, source="test")


def _make_tracker(tmp_path: Path, rows: list[dict]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Date", "Company", "Job Title", "Stack", "ATS %", "URL",
                "Folder", "Sent", "Re-application", "To Learn", "ID"])
    for r in rows:
        ws.append([r.get("date"), r.get("company", ""), r.get("title", ""),
                   "", r.get("ats", "95%"), r.get("url", ""), "", "", "", "", "abc"])
    path = tmp_path / "tracker.xlsx"
    wb.save(path)
    return path


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# core test: cooldown job never reaches send_job_cards
# ---------------------------------------------------------------------------

def test_cooldown_job_excluded_from_new_jobs(tmp_path: Path) -> None:
    today = datetime.date.today()
    tracker = _make_tracker(tmp_path, [
        # Acme was applied to 5 days ago — within 30-day cooldown
        {"date": today - datetime.timedelta(days=5),
         "company": "Acme", "title": "Angular Dev",
         "url": "https://example.com/job/1", "ats": "97%"},
    ])

    fresh_job = _make_job("Acme", "Angular Dev", "https://justjoin.it/job-offer/acme-angular-dev-new")
    unrelated_job = _make_job("OtherCo", "React Dev", "https://justjoin.it/job-offer/other-react")

    captured_cards: list[list[Job]] = []

    async def fake_send_cards(_ctx, jobs):
        captured_cards.append(jobs)

    async def fake_send_text(_ctx, *_a, **_kw):
        pass

    with (
        patch("hunter.tracker.TRACKER_PATH", tracker),
        patch("hunter.main.TRACKER_PATH", tracker),
        patch("hunter.main.AUTO_APPLY", False),
        patch("hunter.main.ALL_SOURCES", []),
        patch("hunter.main.apply_filters_with_stats", return_value=([fresh_job, unrelated_job], {})),
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


def test_expired_cooldown_job_passes_through(tmp_path: Path) -> None:
    """Job applied 20 days ago is past the 12-day default cooldown — must show again."""
    today = datetime.date.today()
    tracker = _make_tracker(tmp_path, [
        {"date": today - datetime.timedelta(days=20),
         "company": "Acme", "title": "Angular Dev",
         "url": "https://example.com/old", "ats": "97%"},
    ])

    job = _make_job("Acme", "Angular Dev", "https://justjoin.it/job-offer/acme-new-posting")
    captured_cards: list[list[Job]] = []

    async def fake_send_cards(_ctx, jobs):
        captured_cards.append(jobs)

    async def fake_send_text(_ctx, *_a, **_kw):
        pass

    with (
        patch("hunter.tracker.TRACKER_PATH", tracker),
        patch("hunter.main.TRACKER_PATH", tracker),
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
