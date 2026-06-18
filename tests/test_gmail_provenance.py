"""Phase A: per-email provenance (email_meta) flows from parse → enrich.

GmailSource records one log entry per email (incl. 0-URL and skipped ones) and
stamps every extracted Job with email_meta so the hunt report can group by email.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from hunter.models import Job
from hunter.sources.gmail import GmailSource, _aggregator_name


def _msg(msg_id: str, sender: str, subject: str, html: str, date: str = "Mon, 09 Jun 2026 14:20:00 +0000") -> dict:
    """Minimal Gmail messages().get() 'full' payload with an HTML body."""
    import base64

    b64 = base64.urlsafe_b64encode(html.encode()).decode()
    return {
        "id": msg_id,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date},
            ],
            "mimeType": "text/html",
            "body": {"data": b64},
        },
    }


def test_aggregator_name():
    assert _aggregator_name("linkedin.com") == "linkedin"
    assert _aggregator_name("nofluffjobs.com") == "nofluffjobs"
    assert _aggregator_name("bulldogjob.pl") == "bulldogjob"
    assert _aggregator_name("pracuj.pl") == "pracuj"
    assert _aggregator_name("justjoin.it") == "justjoin"


def test_parse_message_stamps_email_meta():
    src = GmailSource()
    src.last_email_log = []
    html = 'href="https://www.linkedin.com/jobs/view/1234567890"'
    msg = _msg("m1", "alerts@linkedin.com", "10 new jobs for you", html)

    jobs = src._parse_message(msg)

    assert len(jobs) == 1
    meta = jobs[0].email_meta
    assert meta["msg_id"] == "m1"
    assert meta["subject"] == "10 new jobs for you"
    assert meta["aggregator"] == "linkedin"
    assert meta["sender"] == "alerts@linkedin.com"
    assert isinstance(meta["date"], datetime)


def test_parse_message_logs_email_record():
    src = GmailSource()
    src.last_email_log = []
    html = 'href="https://www.linkedin.com/jobs/view/1234567890"'
    msg = _msg("m1", "alerts@linkedin.com", "10 new jobs for you", html)

    src._parse_message(msg)

    assert len(src.last_email_log) == 1
    rec = src.last_email_log[0]
    assert rec["aggregator"] == "linkedin"
    assert rec["extracted"] == 1
    assert rec["skipped"] is False


def test_parse_message_zero_urls_still_logged():
    """A regex miss (no extractable URL) is recorded with extracted=0 so the
    report can surface coverage gaps instead of hiding the email."""
    src = GmailSource()
    src.last_email_log = []
    msg = _msg("m2", "alerts@linkedin.com", "10 new jobs for you", "no links here")

    jobs = src._parse_message(msg)

    assert jobs == []
    rec = src.last_email_log[0]
    assert rec["aggregator"] == "linkedin"
    assert rec["extracted"] == 0
    assert rec["skipped"] is False


def test_parse_message_skip_subject_flagged():
    src = GmailSource()
    src.last_email_log = []
    msg = _msg("m3", "alerts@linkedin.com", "Your application was sent", "irrelevant")

    jobs = src._parse_message(msg)

    assert jobs == []
    rec = src.last_email_log[0]
    assert rec["skipped"] is True
    assert rec["extracted"] == 0


def test_ack_subject_with_similar_offers_is_parsed_not_skipped():
    # NoFluffJobs sends "Your application for X @ Y" with a "similar job
    # offers especially for you" block. The ACK-subject SKIP must not throw
    # those recommendations away.
    src = GmailSource()
    src.last_email_log = []
    html = (
        'Your application has been sent successfully.'
        '<a href="https://nofluffjobs.com/pl/job/senior-angular-developer-remote-link-group">a</a>'
        '<a href="https://nofluffjobs.com/pl/job/mid-angular-developer-link-group">b</a>'
        '<a href="https://nofluffjobs.com/pl/job/senior-angular-developer-xtb">c</a>'
    )
    msg = _msg(
        "m_ack",
        "notifications@nofluffjobs.com",
        "Your application for Senior Angular Developer @ j-labs software specialist",
        html,
    )

    jobs = src._parse_message(msg)

    assert len(jobs) == 3
    rec = src.last_email_log[0]
    assert rec["skipped"] is False
    assert rec["extracted"] == 3
    assert rec["aggregator"] == "nofluffjobs"


def test_ack_subject_without_similar_offers_still_skipped():
    # Pure ACK email (no recommendations) still skipped, so the report keeps
    # grouping these under "подтверждений пропущено".
    src = GmailSource()
    src.last_email_log = []
    msg = _msg(
        "m_ack_only",
        "notifications@nofluffjobs.com",
        "Your application for Senior Angular Developer @ j-labs",
        "Your application has been sent successfully. Thank you.",
    )

    jobs = src._parse_message(msg)

    assert jobs == []
    rec = src.last_email_log[0]
    assert rec["skipped"] is True
    assert rec["extracted"] == 0
    # Aggregator is now known even though we skipped (parser ran first).
    assert rec["aggregator"] == "nofluffjobs"


def test_parse_date_unparseable_returns_none():
    assert GmailSource._parse_date("(no date)") is None
    assert isinstance(
        GmailSource._parse_date("Mon, 09 Jun 2026 14:20:00 +0000"), datetime
    )


def test_email_meta_survives_enrichment():
    """The enricher recreates Job objects — email_meta must be carried over."""
    from hunter.gmail_enricher import _enrich_via_text

    meta = {"msg_id": "m1", "subject": "digest", "aggregator": "nofluffjobs",
            "sender": "x@nofluffjobs.com", "date": datetime.now(timezone.utc)}
    job = Job(
        title="Jobs for you", company="[nofluffjobs]", location="", salary=None,
        url="https://nofluffjobs.com/job/frontend-developer-beta-ltd-waw",
        source="gmail_nofluffjobs", email_meta=meta,
    )
    enriched_text = (
        "Job Title: Frontend Developer\nCompany: Beta Ltd\n"
        "Location: Remote\nSalary: 12000 PLN\n"
    )
    with patch("hunter.sources.fetch_job_text", return_value=enriched_text):
        result = _enrich_via_text(job)

    assert result is not job  # was enriched
    assert result.email_meta == meta


def test_fetch_jobs_sets_capped_flag(monkeypatch):
    monkeypatch.setattr("hunter.sources.gmail.GMAIL_MAX_RESULTS", 2)
    monkeypatch.setattr("hunter.sources.gmail.GMAIL_ENRICH_ENABLED", False)
    src = GmailSource()

    service = MagicMock()
    service.users.return_value.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "a"}, {"id": "b"}]
    }
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = _msg(
        "a", "alerts@linkedin.com", "10 new jobs", "no links"
    )

    src._fetch_jobs(service)
    assert src.last_capped is True
    assert len(src.last_email_log) == 2
