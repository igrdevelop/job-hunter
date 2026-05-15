"""Tests for hunter/gmail_enricher.py"""

from unittest.mock import MagicMock, patch

import pytest

from hunter.models import Job
from hunter.gmail_enricher import enrich_jobs, _enrich_justjoin, _enrich_via_text, _enrich_one


def _stub(url: str, source: str = "gmail") -> Job:
    return Job(
        title="Jobs for you",
        company="[justjoin]",
        location="",
        salary=None,
        url=url,
        source=source,
    )


# ── JustJoin ──────────────────────────────────────────────────────────────────

JJ_OFFER = {
    "title": "Senior Angular Developer",
    "companyName": "Acme Corp",
    "city": "Wrocław",
    "workplaceType": "hybrid",
    "employmentTypes": [{"from": 15000, "to": 20000, "currency": "PLN", "type": "b2b"}],
}


def test_enrich_justjoin_happy_path():
    job = _stub("https://justjoin.it/job-offer/acme-senior-angular-developer-wroclaw-js")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = JJ_OFFER
    mock_resp.raise_for_status = MagicMock()

    with patch("hunter.gmail_enricher.requests.get", return_value=mock_resp):
        result = _enrich_justjoin(job)

    assert result.title == "Senior Angular Developer"
    assert result.company == "Acme Corp"
    assert "Hybrid" in result.location
    assert result.salary is not None
    assert result.url == job.url
    assert result.source == job.source


def test_enrich_justjoin_404_returns_stub():
    job = _stub("https://justjoin.it/job-offer/some-expired-slug")

    mock_resp = MagicMock()
    mock_resp.status_code = 404

    with patch("hunter.gmail_enricher.requests.get", return_value=mock_resp):
        result = _enrich_justjoin(job)

    assert result is job


def test_enrich_justjoin_network_error_returns_stub():
    job = _stub("https://justjoin.it/job-offer/some-slug")

    with patch("hunter.gmail_enricher.requests.get", side_effect=OSError("timeout")):
        result = _enrich_one(job)

    assert result is job


def test_enrich_justjoin_bad_slug_returns_stub():
    job = _stub("https://justjoin.it/")
    result = _enrich_justjoin(job)
    assert result is job


# ── _enrich_via_text ──────────────────────────────────────────────────────────

NFJ_TEXT = (
    "Job Title: Frontend Developer\n"
    "Company: Beta Ltd\n"
    "Location: Remote\n"
    "Salary: 12000–16000 PLN B2B\n"
    "\nSome description here."
)


def test_enrich_via_text_happy_path():
    job = _stub("https://nofluffjobs.com/job/frontend-developer-beta-ltd-waw")

    with patch("job_fetch.fetch_job_text", return_value=NFJ_TEXT):
        result = _enrich_via_text(job)

    assert result.title == "Frontend Developer"
    assert result.company == "Beta Ltd"
    assert result.location == "Remote"
    assert result.salary == "12000–16000 PLN B2B"


def test_enrich_via_text_missing_fields_keeps_stub():
    job = _stub("https://nofluffjobs.com/job/some-job")

    with patch("job_fetch.fetch_job_text", return_value="No structured headers here"):
        result = _enrich_via_text(job)

    # title and company unchanged → original stub returned
    assert result is job


def test_enrich_via_text_exception_propagates():
    job = _stub("https://nofluffjobs.com/job/some-job")

    with patch("job_fetch.fetch_job_text", side_effect=RuntimeError("network error")):
        with pytest.raises(RuntimeError):
            _enrich_via_text(job)


# ── _enrich_one dispatcher ────────────────────────────────────────────────────

def test_enrich_one_unknown_domain_returns_stub():
    job = _stub("https://somerandomblog.com/jobs/angular-dev")
    result = _enrich_one(job)
    assert result is job


def test_enrich_one_nofluffjobs_calls_via_text():
    job = _stub("https://nofluffjobs.com/job/angular-dev-xyz")

    with patch("hunter.gmail_enricher._enrich_via_text", return_value=job) as mock_fn:
        _enrich_one(job)
        mock_fn.assert_called_once_with(job)


def test_enrich_one_exception_returns_stub():
    job = _stub("https://justjoin.it/job-offer/bad-slug")

    with patch("hunter.gmail_enricher._enrich_justjoin", side_effect=ValueError("boom")):
        result = _enrich_one(job)

    assert result is job


# ── enrich_jobs ───────────────────────────────────────────────────────────────

def test_enrich_jobs_empty_list():
    assert enrich_jobs([]) == []


def test_enrich_jobs_preserves_order():
    urls = [
        "https://justjoin.it/job-offer/job-a",
        "https://nofluffjobs.com/job/job-b",
        "https://someother.com/job/job-c",
    ]
    jobs = [_stub(u) for u in urls]

    with patch("hunter.gmail_enricher._enrich_one", side_effect=lambda j: j):
        result = enrich_jobs(jobs)

    assert [r.url for r in result] == urls


def test_enrich_jobs_disabled_via_gmail_source(monkeypatch):
    """When GMAIL_ENRICH_ENABLED=false, enrich_jobs is never called from gmail.py."""
    monkeypatch.setattr("hunter.sources.gmail.GMAIL_ENRICH_ENABLED", False)

    from hunter.sources.gmail import GmailSource

    mock_service = MagicMock()
    mock_service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": []
    }

    src = GmailSource()
    with patch("hunter.gmail_enricher.enrich_jobs") as mock_enrich:
        src._fetch_jobs(mock_service)
        mock_enrich.assert_not_called()
