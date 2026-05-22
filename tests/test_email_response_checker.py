"""Tests for hunter/email_response_checker.py.

All tests that touch the tracker use tmp_path + monkeypatch to avoid
touching the real tracker.xlsx. Gmail API calls are fully mocked.
"""

import base64
import openpyxl
import pytest
from unittest.mock import MagicMock, patch

from hunter.email_response_checker import (
    ConfirmationEmail,
    MatchResult,
    _extract_body_text,
    _is_confirmation_subject,
    _message_date,
    _parse_justjoin,
    _parse_linkedin,
    _parse_nofluffjobs,
    _parse_pracuj,
    _parse_message,
    fetch_confirmation_emails,
    match_email,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _make_msg(subject: str, sender: str, body_text: str = "", internal_date_ms: int = 1716307200000) -> dict:
    """Build a minimal Gmail message dict."""
    parts = []
    if body_text:
        parts.append({
            "mimeType": "text/plain",
            "body": {"data": _encode(body_text)},
        })
    return {
        "internalDate": str(internal_date_ms),
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "parts": parts,
            "mimeType": "multipart/mixed" if parts else "text/plain",
            "body": {"data": _encode(body_text)} if not parts and body_text else {},
        },
    }


def _make_tracker(tmp_path, rows: list[dict]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([
        "Date", "Company", "Job Title", "Stack", "ATS %", "URL",
        "Folder", "Sent", "Re-application", "To Learn", "ID", "Drive URL", "Response",
    ])
    for i, r in enumerate(rows):
        ws.append([
            "2026-05-22", r.get("company", "Acme"), r.get("title", "Dev"),
            "Angular", r.get("ats", "85%"), r.get("url", f"https://example.com/{i}"),
            "", "", "", "", r.get("id", f"id{i}"), "", r.get("response", ""),
        ])
    wb.save(tmp_path / "tracker.xlsx")


# ---------------------------------------------------------------------------
# _is_confirmation_subject
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject", [
    "You applied to Senior Angular Developer at Acme Corp",
    "Your application was sent to Widgets Inc",
    "Application submitted",
    "Application sent",
    "Twoja aplikacja została wysłana",
    "Potwierdzenie aplikacji na stanowisko",
    "Dziękujemy za aplikację na stanowisko",
])
def test_is_confirmation_subject_true(subject):
    assert _is_confirmation_subject(subject) is True


@pytest.mark.parametrize("subject", [
    "10 new Angular jobs for you",
    "New jobs matching your alert",
    "Nowe oferty pracy",
    "Weekly digest",
])
def test_is_confirmation_subject_false(subject):
    assert _is_confirmation_subject(subject) is False


# ---------------------------------------------------------------------------
# _parse_linkedin
# ---------------------------------------------------------------------------

def test_parse_linkedin_applied_to():
    company, title = _parse_linkedin(
        "You applied to Senior Angular Developer at Acme Corp", ""
    )
    assert company == "Acme Corp"
    assert title == "Senior Angular Developer"


def test_parse_linkedin_application_sent():
    company, title = _parse_linkedin(
        "Your application was sent to Widgets Inc", ""
    )
    assert company == "Widgets Inc"
    assert title == ""


def test_parse_linkedin_no_match():
    company, title = _parse_linkedin("Some unrelated subject", "")
    assert company == ""
    assert title == ""


# ---------------------------------------------------------------------------
# _parse_pracuj
# ---------------------------------------------------------------------------

def test_parse_pracuj_subject():
    company, title = _parse_pracuj(
        "Potwierdzenie aplikacji na stanowisko: Senior Angular Developer w Acme Corp", ""
    )
    assert company == "Acme Corp"
    assert title == "Senior Angular Developer"


def test_parse_pracuj_body_fallback():
    body = "Stanowisko: Frontend Engineer\nFirma: Tech Solutions Sp. z o.o."
    company, title = _parse_pracuj("Twoja aplikacja została wysłana", body)
    assert "Tech Solutions" in company
    assert title == "Frontend Engineer"


def test_parse_pracuj_no_match():
    company, title = _parse_pracuj("Twoja aplikacja została wysłana", "")
    assert company == ""
    assert title == ""


# ---------------------------------------------------------------------------
# _parse_nofluffjobs
# ---------------------------------------------------------------------------

def test_parse_nofluffjobs_with_title():
    company, title = _parse_nofluffjobs(
        "Application sent to Acme Corp - Senior Angular Developer", ""
    )
    assert company == "Acme Corp"
    assert title == "Senior Angular Developer"


def test_parse_nofluffjobs_company_only():
    company, title = _parse_nofluffjobs("Application sent to Acme Corp", "")
    assert company == "Acme Corp"
    assert title == ""


def test_parse_nofluffjobs_en_dash():
    company, title = _parse_nofluffjobs(
        "Application sent to Acme Corp – Frontend Dev", ""
    )
    assert company == "Acme Corp"
    assert title == "Frontend Dev"


def test_parse_nofluffjobs_no_match():
    company, title = _parse_nofluffjobs("Weekly digest", "")
    assert company == ""


# ---------------------------------------------------------------------------
# _parse_justjoin
# ---------------------------------------------------------------------------

def test_parse_justjoin_full():
    company, title = _parse_justjoin(
        "Dziękujemy za aplikację na stanowisko Angular Developer w Acme Corp", ""
    )
    assert company == "Acme Corp"
    assert title == "Angular Developer"


def test_parse_justjoin_no_match():
    company, title = _parse_justjoin("Nowe oferty pracy", "")
    assert company == ""
    assert title == ""


# ---------------------------------------------------------------------------
# _extract_body_text
# ---------------------------------------------------------------------------

def test_extract_body_text_plain():
    payload = {
        "mimeType": "text/plain",
        "body": {"data": _encode("Hello world")},
    }
    assert _extract_body_text(payload) == "Hello world"


def test_extract_body_text_multipart():
    payload = {
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _encode("Plain part")}},
            {"mimeType": "text/html", "body": {"data": _encode("<p>HTML</p>")}},
        ]
    }
    assert _extract_body_text(payload) == "Plain part"


def test_extract_body_text_empty():
    assert _extract_body_text({"mimeType": "text/html", "body": {}}) == ""


# ---------------------------------------------------------------------------
# _message_date
# ---------------------------------------------------------------------------

def test_message_date_returns_iso():
    # 1716307200000 ms = 2024-05-21 12:00:00 UTC
    msg = {"internalDate": "1716307200000"}
    result = _message_date(msg)
    assert result == "2024-05-21"


# ---------------------------------------------------------------------------
# _parse_message
# ---------------------------------------------------------------------------

def test_parse_message_linkedin_confirmation():
    msg = _make_msg(
        subject="You applied to Senior Angular Developer at Acme Corp",
        sender="jobs-noreply@linkedin.com",
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "linkedin"
    assert result.company == "Acme Corp"
    assert result.title == "Senior Angular Developer"


def test_parse_message_skips_non_confirmation():
    msg = _make_msg(
        subject="10 new Angular jobs for you",
        sender="jobs-noreply@linkedin.com",
    )
    assert _parse_message(msg) is None


def test_parse_message_nofluffjobs():
    msg = _make_msg(
        subject="Application sent to Widgets Inc - Frontend Engineer",
        sender="noreply@nofluffjobs.com",
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "nofluffjobs"
    assert result.company == "Widgets Inc"
    assert result.title == "Frontend Engineer"


def test_parse_message_justjoin():
    msg = _make_msg(
        subject="Dziękujemy za aplikację na stanowisko Angular Dev w TechCorp",
        sender="noreply@justjoin.it",
    )
    result = _parse_message(msg)
    assert result.platform == "justjoin"
    assert result.company == "TechCorp"


def test_parse_message_unknown_platform_confirmation():
    msg = _make_msg(
        subject="Application submitted",
        sender="hr@unknownboard.com",
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "unknown"
    assert result.company == ""


# ---------------------------------------------------------------------------
# match_email
# ---------------------------------------------------------------------------

def test_match_email_no_company_is_no_match(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    email = ConfirmationEmail(
        company="", title="Angular Dev", date="2026-05-20",
        subject="...", platform="linkedin",
    )
    result = match_email(email)
    assert result.match_type == "no_match"


def test_match_email_exact(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [{"company": "Acme Corp", "title": "Angular Developer"}])
    email = ConfirmationEmail(
        company="Acme Corp", title="Angular Developer", date="2026-05-20",
        subject="You applied to Angular Developer at Acme Corp", platform="linkedin",
    )
    result = match_email(email)
    assert result.match_type == "exact"
    assert result.row_num == 2


def test_match_email_fuzzy_senior_stripped(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [{"company": "Acme Corp", "title": "Senior Angular Developer"}])
    email = ConfirmationEmail(
        company="Acme Corp", title="Angular Developer", date="2026-05-20",
        subject="...", platform="linkedin",
    )
    result = match_email(email)
    # "senior" is a stop word → tokens identical → score 1.0 → exact
    assert result.match_type == "exact"
    assert result.row_num == 2


def test_match_email_fuzzy_partial(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [{"company": "Acme Corp", "title": "Angular Engineer"}])
    email = ConfirmationEmail(
        company="Acme Corp", title="Angular Developer", date="2026-05-20",
        subject="...", platform="linkedin",
    )
    result = match_email(email)
    # "angular" overlaps, "developer"/"engineer" don't → score 0.5 → fuzzy
    assert result.match_type == "fuzzy"
    assert result.row_num == 2


def test_match_email_ambiguous(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [
        {"company": "Acme", "title": "Angular Developer", "id": "id0"},
        {"company": "Acme", "title": "Angular Engineer", "id": "id1"},
    ])
    email = ConfirmationEmail(
        company="Acme", title="Angular Dev", date="2026-05-20",
        subject="...", platform="linkedin",
    )
    result = match_email(email)
    assert result.match_type == "ambiguous"
    assert result.row_num is None


def test_match_email_no_match_company_absent(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [{"company": "Other Corp", "title": "Angular Developer"}])
    email = ConfirmationEmail(
        company="Acme Corp", title="Angular Developer", date="2026-05-20",
        subject="...", platform="linkedin",
    )
    result = match_email(email)
    assert result.match_type == "no_match"


def test_match_email_no_title_single_candidate(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [{"company": "Acme Corp", "title": "Angular Developer"}])
    email = ConfirmationEmail(
        company="Acme Corp", title="", date="2026-05-20",
        subject="Your application was sent to Acme Corp", platform="linkedin",
    )
    result = match_email(email)
    assert result.match_type == "fuzzy"
    assert result.row_num == 2


def test_match_email_no_title_multiple_candidates_is_ambiguous(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [
        {"company": "Acme Corp", "title": "Angular Developer", "id": "id0"},
        {"company": "Acme Corp", "title": "React Developer", "id": "id1"},
    ])
    email = ConfirmationEmail(
        company="Acme Corp", title="", date="2026-05-20",
        subject="Your application was sent to Acme Corp", platform="linkedin",
    )
    result = match_email(email)
    assert result.match_type == "ambiguous"


# ---------------------------------------------------------------------------
# fetch_confirmation_emails (mocked Gmail service)
# ---------------------------------------------------------------------------

def _mock_service(messages: list[dict]) -> MagicMock:
    """Build a mock Gmail service that returns the given messages."""
    service = MagicMock()
    service.users().messages().list().execute.return_value = {
        "messages": [{"id": str(i)} for i in range(len(messages))]
    }
    for i, msg in enumerate(messages):
        service.users().messages().get(
            userId="me", id=str(i), format="full"
        ).execute.return_value = msg
    # Make chained calls work
    service.users.return_value = service.users()
    return service


def test_fetch_returns_empty_on_gmail_error():
    service = MagicMock()
    service.users().messages().list().execute.side_effect = Exception("network error")
    result = fetch_confirmation_emails(service, lookback_days=7)
    assert result == []


def test_fetch_filters_non_confirmation_subjects():
    msg = _make_msg(
        subject="10 new jobs for you",
        sender="jobs-noreply@linkedin.com",
    )
    service = MagicMock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "1"}]}
    service.users().messages().get().execute.return_value = msg

    result = fetch_confirmation_emails(service, lookback_days=7)
    assert result == []


def test_fetch_parses_confirmation_email():
    msg = _make_msg(
        subject="You applied to Angular Developer at Acme Corp",
        sender="jobs-noreply@linkedin.com",
    )
    service = MagicMock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "1"}]}
    service.users().messages().get().execute.return_value = msg

    result = fetch_confirmation_emails(service, lookback_days=7)
    assert len(result) == 1
    assert result[0].company == "Acme Corp"
    assert result[0].title == "Angular Developer"
    assert result[0].platform == "linkedin"


# ---------------------------------------------------------------------------
# run_confirmation_check writes CONFIRMED to tracker
# ---------------------------------------------------------------------------

def test_run_confirmation_check_writes_confirmed(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    import hunter.email_response_checker as checker_mod

    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [{"company": "Acme Corp", "title": "Angular Developer"}])

    # Mock gmail_client and fetch inside the module
    confirmed_email = ConfirmationEmail(
        company="Acme Corp", title="Angular Developer",
        date="2026-05-20", subject="You applied...", platform="linkedin",
    )
    monkeypatch.setattr(checker_mod, "fetch_confirmation_emails", lambda svc, days: [confirmed_email])

    mock_service = MagicMock()
    with patch("hunter.email_response_checker.get_gmail_service", return_value=mock_service):
        results = checker_mod.run_confirmation_check(lookback_days=7)

    assert len(results) == 1
    assert results[0].match_type in ("exact", "fuzzy")

    wb = openpyxl.load_workbook(tmp_path / "tracker.xlsx", read_only=True, data_only=True)
    ws = wb.active
    from hunter.tracker import COL_RESPONSE
    val = ws.cell(row=2, column=COL_RESPONSE).value
    wb.close()
    assert val == "CONFIRMED"


def test_run_confirmation_check_does_not_overwrite_existing(tmp_path, monkeypatch):
    import hunter.tracker as tracker_mod
    import hunter.email_response_checker as checker_mod

    monkeypatch.setattr(tracker_mod, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    _make_tracker(tmp_path, [
        {"company": "Acme Corp", "title": "Angular Developer", "response": "INTERVIEW"}
    ])

    confirmed_email = ConfirmationEmail(
        company="Acme Corp", title="Angular Developer",
        date="2026-05-20", subject="You applied...", platform="linkedin",
    )
    monkeypatch.setattr(checker_mod, "fetch_confirmation_emails", lambda svc, days: [confirmed_email])

    mock_service = MagicMock()
    with patch("hunter.email_response_checker.get_gmail_service", return_value=mock_service):
        checker_mod.run_confirmation_check(lookback_days=7)

    wb = openpyxl.load_workbook(tmp_path / "tracker.xlsx", read_only=True, data_only=True)
    ws = wb.active
    from hunter.tracker import COL_RESPONSE
    val = ws.cell(row=2, column=COL_RESPONSE).value
    wb.close()
    assert val == "INTERVIEW"  # untouched
