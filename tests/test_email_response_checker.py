"""Tests for hunter/email_response_checker.py.

Test data based on real observed confirmation emails:
  - eRecruiter ATS (mail@stage.erecruiter.pl): NASK, EXATEL, Nexio, Medicover
  - SmartRecruiters (notification@smartrecruiters.com): Sigma Software
  - Pracuj.pl status (noreply@aplikacje.pracuj.pl): Hiberus, Get It Together, Devapo
  - SmartJobs Smart Tracker (noreply@thesmartjobs.com): Devapo, Hiberus
  - theprotocol.it (system@mailing.theprotocol.it): ITEAMLY
  - Recruitify.ai (system@recruitify.ai): title only, no company
  - Direct company emails (inspeerity.com, consdata.com, etc.)
"""

import base64
import uuid
import pytest
from unittest.mock import MagicMock, patch

from hunter.email_response_checker import (
    ConfirmationEmail,
    _extract_body_text,
    _is_confirmation_subject,
    _message_date,
    _parse_erecruiter,
    _parse_smartrecruiters,
    _parse_pracuj_status,
    _parse_smartjobs,
    _parse_theprotocol,
    _parse_recruitify,
    _parse_direct,
    _parse_message,
    fetch_confirmation_emails,
    match_email,
)
from hunter.db import get_db
from hunter.tracker import normalize_url, lookup_by_company_and_title


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _encode(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _make_msg(
    subject: str,
    sender: str,
    body_text: str = "",
    internal_date_ms: int = 1716307200000,
) -> dict:
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


def _insert_tracker_row(tracker_db, *, company: str = "Acme", title: str = "Dev",
                         ats: str = "85%", url: str = "", row_id: str = "",
                         confirmation: str = "") -> str:
    """Insert a row into the test DB. Returns the row ID used."""
    if not url:
        url = f"https://example.com/{uuid.uuid4().hex[:6]}"
    if not row_id:
        row_id = uuid.uuid4().hex[:8]
    norm = normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm, confirmation)
            VALUES (?, '2026-05-22', ?, ?, ?, ?, ?, ?)
            """,
            (row_id, company, title, ats, url, norm, confirmation),
        )
    return row_id


# ---------------------------------------------------------------------------
# _is_confirmation_subject — real-world subjects
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("subject", [
    # eRecruiter pattern (real)
    "NASK - Dziękujemy za złożenie aplikacji na stanowisko Senior Frontend Developer",
    "EXATEL - Dziękujemy za złożenie aplikacji na stanowisko Frontend Developer",
    # SmartRecruiters (real)
    "Thank you for applying to Sigma Software",
    # Generic English
    "Thanks for applying!",
    "Thank you for submitting your application",
    "Application submitted",
    "Application received",
    # Polish generic
    "Potwierdzenie aplikacji na stanowisko Angular Developer",
    "Dziękujemy za zainteresowanie ofertą",
])
def test_is_confirmation_subject_true(subject):
    assert _is_confirmation_subject(subject) is True


@pytest.mark.parametrize("subject", [
    "10 new Angular jobs for you",
    "New jobs matching your profile",
    "Nowe oferty pracy dla Ciebie",
    "Weekly jobs digest",
    "Your saved jobs this week",
])
def test_is_confirmation_subject_false(subject):
    assert _is_confirmation_subject(subject) is False


# ---------------------------------------------------------------------------
# _parse_erecruiter — real-world email format
# ---------------------------------------------------------------------------

def test_parse_erecruiter_nask():
    """Real NASK email via eRecruiter."""
    company, title = _parse_erecruiter(
        "NASK - Dziękujemy za złożenie aplikacji na stanowisko Senior Frontend Developer",
        "",
    )
    assert company == "NASK"
    assert title == "Senior Frontend Developer"


def test_parse_erecruiter_exatel():
    """Real EXATEL email via eRecruiter — title in body."""
    body = (
        "Dzień dobry,\n"
        "dziękujemy za złożenie aplikacji na stanowisko Frontend Developer i czas "
        "poświęcony na wypełnienie kwestionariuszy aplikacyjnych.\n"
    )
    company, title = _parse_erecruiter(
        "EXATEL - Dziękujemy za złożenie aplikacji na stanowisko Frontend Developer",
        body,
    )
    assert company == "EXATEL"
    assert title == "Frontend Developer"


def test_parse_erecruiter_body_fallback():
    """When subject doesn't match, extract title from body."""
    body = "dziękujemy za złożenie aplikacji na stanowisko Angular Developer i czas poświęcony."
    company, title = _parse_erecruiter("Dziękujemy za złożenie aplikacji", body)
    assert company == ""        # no company in subject without the dash pattern
    assert title == "Angular Developer"


def test_parse_erecruiter_no_match():
    company, title = _parse_erecruiter("Nowe oferty pracy", "")
    assert company == ""
    assert title == ""


# ---------------------------------------------------------------------------
# _parse_smartrecruiters — real-world email format
# ---------------------------------------------------------------------------

def test_parse_smartrecruiters_sigma_software():
    """Real Sigma Software email via SmartRecruiters."""
    body = (
        "Dear Ihar,\n\n"
        "Thank you for submitting your application for the position of "
        "Middle Front-End Developer. We strive to be an excellent workplace...\n"
    )
    company, title = _parse_smartrecruiters(
        "Thank you for applying to Sigma Software",
        body,
        "Sigma Software <notification@smartrecruiters.com>",
    )
    assert company == "Sigma Software"
    assert title == "Middle Front-End Developer"


def test_parse_smartrecruiters_company_from_subject():
    company, title = _parse_smartrecruiters(
        "Thank you for applying to Acme Corp",
        "",
        "Acme Corp <notification@smartrecruiters.com>",
    )
    assert company == "Acme Corp"


def test_parse_smartrecruiters_company_from_display_name():
    """When subject doesn't have company, use From display name."""
    company, title = _parse_smartrecruiters(
        "Thanks for applying!",
        "",
        "Acme Corp <notification@smartrecruiters.com>",
    )
    assert company == "Acme Corp"


def test_parse_smartrecruiters_no_title():
    company, title = _parse_smartrecruiters(
        "Thank you for applying to Sigma Software",
        "We received your application.",
        "Sigma Software <notification@smartrecruiters.com>",
    )
    assert company == "Sigma Software"
    assert title == ""


# ---------------------------------------------------------------------------
# _parse_direct — direct company emails
# ---------------------------------------------------------------------------

def test_parse_direct_polish_body():
    """Inspeerity-style email: Polish stanowisko in body."""
    body = "dziękujemy za złożenie aplikacji na stanowisko Angular Developer."
    company, title = _parse_direct(
        "Dziękujemy za aplikację",
        body,
        "Inspeerity <hr@inspeerity.com>",
    )
    assert title == "Angular Developer"
    assert company == "Inspeerity"


def test_parse_direct_company_from_display_name():
    company, title = _parse_direct(
        "Thank you for applying",
        "",
        "Creatio HR Team <hr@creatio.com>",
    )
    assert company == "Creatio HR Team"


def test_parse_direct_company_from_domain_fallback():
    """Generic sender name → extract company from domain."""
    company, title = _parse_direct(
        "Thank you for applying",
        "",
        "recruitment <hr@acme-company.com>",
    )
    # "recruitment" is in the skip list → domain fallback
    assert "acme" in company.lower()


def test_parse_direct_english_body_position():
    body = "Thank you for your interest. The position of Frontend Engineer is still open."
    company, title = _parse_direct(
        "Thank you for applying",
        body,
        "HR <hr@company.com>",
    )
    assert title == "Frontend Engineer"


# ---------------------------------------------------------------------------
# _parse_pracuj_status — real Pracuj.pl status emails
# ---------------------------------------------------------------------------

def test_parse_pracuj_status_title_from_subject():
    title_in_subject = "Frontend Developer (Angular + AI)"
    company, title = _parse_pracuj_status(
        f"{title_in_subject}: pracodawca udziela bezpośrednich informacji.",
        "",
    )
    assert title == title_in_subject


def test_parse_pracuj_status_company_from_body():
    body = (
        "Sprawdź szczegóły oferty:\n"
        "logo firmy HIBERUS POLAND SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ\n"
        "Frontend Developer (Angular + AI)\n"
    )
    company, title = _parse_pracuj_status(
        "Frontend Developer (Angular + AI): pracodawca udziela bezpośrednich informacji.",
        body,
    )
    assert "HIBERUS" in company
    assert title == "Frontend Developer (Angular + AI)"


def test_parse_pracuj_status_no_match():
    company, title = _parse_pracuj_status("Nowe oferty dla Ciebie", "")
    assert company == ""
    assert title == ""


# ---------------------------------------------------------------------------
# _parse_smartjobs — real SmartJobs Smart Tracker emails
# ---------------------------------------------------------------------------

def test_parse_smartjobs_from_body():
    """Real SmartJobs email body contains stanowisko + w firmie."""
    body = (
        "Oto Smart Tracker dla Twojej aplikacji na stanowisko Senior Angular Developer "
        "w firmie Devapo. Smart Tracker to link śledzący..."
    )
    company, title = _parse_smartjobs(
        "Twój Smart Tracker dla Senior Angular Developer",
        body,
    )
    assert company == "Devapo"
    assert title == "Senior Angular Developer"


def test_parse_smartjobs_hiberus():
    body = (
        "Oto Smart Tracker dla Twojej aplikacji na stanowisko Frontend Developer (Angular + AI) "
        "w firmie Hiberus Poland."
    )
    company, title = _parse_smartjobs("Twój Smart Tracker dla Frontend Developer (Angular + AI)", body)
    assert company == "Hiberus Poland"
    assert title == "Frontend Developer (Angular + AI)"


def test_parse_smartjobs_title_fallback_from_subject():
    """When body parsing fails, fall back to subject."""
    company, title = _parse_smartjobs(
        "Twój Smart Tracker dla Senior Angular Developer",
        "",
    )
    assert title == "Senior Angular Developer"
    assert company == ""


# ---------------------------------------------------------------------------
# _parse_theprotocol — real theprotocol.it confirmation emails
# ---------------------------------------------------------------------------

def test_parse_theprotocol_iteamly():
    """Real theprotocol.it subject."""
    company, title = _parse_theprotocol(
        "Potwierdzenie zgłoszenia - ITEAMLY SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ:"
        " Senior Frontend Developer (Angular)",
        "",
    )
    assert "ITEAMLY" in company
    assert title == "Senior Frontend Developer (Angular)"


def test_parse_theprotocol_no_match():
    company, title = _parse_theprotocol("Nowe oferty pracy", "")
    assert company == ""
    assert title == ""


# ---------------------------------------------------------------------------
# _parse_recruitify — real Recruitify.ai emails
# ---------------------------------------------------------------------------

def test_parse_recruitify_title_from_body():
    body = "Hello Igor\n\nThanks for applying. Your application is being processed.\n\nPosition: Frontend Developer (Angular)\n"
    company, title = _parse_recruitify(
        "Thanks for filling up the questionnaire.",
        body,
    )
    assert title == "Frontend Developer (Angular)"
    assert company == ""  # Recruitify doesn't include company


def test_parse_recruitify_no_body():
    company, title = _parse_recruitify("Thanks for filling up the questionnaire.", "")
    assert company == ""
    assert title == ""


# ---------------------------------------------------------------------------
# _parse_message routing — new platforms
# ---------------------------------------------------------------------------

def test_parse_message_pracuj_status():
    msg = _make_msg(
        subject="Senior Angular Developer: pracodawca udziela bezpośrednich informacji.",
        sender="Pracuj.pl - Status aplikacji <noreply@aplikacje.pracuj.pl>",
        body_text="logo firmy DEVAPO SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ\nSenior Angular Developer\n",
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "pracuj_status"
    assert result.title == "Senior Angular Developer"
    assert "DEVAPO" in result.company


def test_parse_message_smartjobs():
    body = "aplikacja na stanowisko Senior Angular Developer w firmie Devapo."
    msg = _make_msg(
        subject="Twój Smart Tracker dla Senior Angular Developer",
        sender="Smart Tracker <noreply@thesmartjobs.com>",
        body_text=body,
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "smartjobs"
    assert result.company == "Devapo"
    assert result.title == "Senior Angular Developer"


def test_parse_message_theprotocol():
    msg = _make_msg(
        subject="Potwierdzenie zgłoszenia - ITEAMLY SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ: Senior Frontend Developer (Angular)",
        sender="the:protocol <system@mailing.theprotocol.it>",
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "theprotocol"
    assert "ITEAMLY" in result.company
    assert result.title == "Senior Frontend Developer (Angular)"


def test_parse_message_recruitify():
    body = "Position: Frontend Developer (Angular)\n"
    msg = _make_msg(
        subject="Thanks for filling up the questionnaire.",
        sender="Recruitify.ai <system@recruitify.ai>",
        body_text=body,
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "recruitify"
    assert result.title == "Frontend Developer (Angular)"
    assert result.company == ""


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
    msg = {"internalDate": "1716307200000"}
    assert _message_date(msg) == "2024-05-21"


# ---------------------------------------------------------------------------
# _parse_message — integration across parsers
# ---------------------------------------------------------------------------

def test_parse_message_erecruiter_nask():
    msg = _make_msg(
        subject="NASK - Dziękujemy za złożenie aplikacji na stanowisko Senior Frontend Developer",
        sender="eRecruiter <mail@stage.erecruiter.pl>",
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "erecruiter"
    assert result.company == "NASK"
    assert result.title == "Senior Frontend Developer"


def test_parse_message_smartrecruiters():
    body = "Thank you for submitting your application for the position of Middle Front-End Developer."
    msg = _make_msg(
        subject="Thank you for applying to Sigma Software",
        sender="Sigma Software <notification@smartrecruiters.com>",
        body_text=body,
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "smartrecruiters"
    assert result.company == "Sigma Software"
    assert result.title == "Middle Front-End Developer"


def test_parse_message_direct_company():
    body = "dziękujemy za złożenie aplikacji na stanowisko Angular Developer."
    msg = _make_msg(
        subject="Dziękujemy za złożenie aplikacji",
        sender="Inspeerity <hr@inspeerity.com>",
        body_text=body,
    )
    result = _parse_message(msg)
    assert result is not None
    assert result.platform == "direct"
    assert result.title == "Angular Developer"


def test_parse_message_skips_non_confirmation():
    msg = _make_msg(
        subject="10 new Angular jobs for you",
        sender="jobs-noreply@linkedin.com",
    )
    assert _parse_message(msg) is None


# ---------------------------------------------------------------------------
# match_email
# ---------------------------------------------------------------------------

def test_match_email_no_company(tracker_db):
    email = ConfirmationEmail(company="", title="Angular Dev", date="2026-05-20",
                              subject="...", platform="erecruiter")
    assert match_email(email).match_type == "no_match"


def test_match_email_exact(tracker_db):
    _insert_tracker_row(tracker_db, company="NASK", title="Senior Frontend Developer")
    email = ConfirmationEmail(company="NASK", title="Senior Frontend Developer",
                              date="2026-05-20", subject="...", platform="erecruiter")
    result = match_email(email)
    assert result.match_type == "exact"
    assert result.row_id is not None


def test_match_email_fuzzy_partial_title(tracker_db):
    _insert_tracker_row(tracker_db, company="NASK", title="Senior Frontend Developer")
    # "Frontend Developer" vs "Senior Frontend Developer" — "senior" is stop word → exact
    email = ConfirmationEmail(company="NASK", title="Frontend Developer",
                              date="2026-05-20", subject="...", platform="erecruiter")
    result = match_email(email)
    assert result.match_type in ("exact", "fuzzy")
    assert result.row_id is not None


def test_match_email_no_match_wrong_company(tracker_db):
    _insert_tracker_row(tracker_db, company="Other Corp", title="Angular Developer")
    email = ConfirmationEmail(company="NASK", title="Angular Developer",
                              date="2026-05-20", subject="...", platform="erecruiter")
    assert match_email(email).match_type == "no_match"


def test_match_email_ambiguous(tracker_db):
    _insert_tracker_row(tracker_db, company="Acme", title="Angular Developer",
                        row_id="id0id0id")
    _insert_tracker_row(tracker_db, company="Acme", title="Angular Engineer",
                        row_id="id1id1id")
    email = ConfirmationEmail(company="Acme", title="Angular Dev",
                              date="2026-05-20", subject="...", platform="erecruiter")
    result = match_email(email)
    assert result.match_type == "ambiguous"
    assert result.row_id is None


def test_match_email_no_title_single_row(tracker_db):
    _insert_tracker_row(tracker_db, company="Sigma Software",
                        title="Middle Front-End Developer")
    # SmartRecruiters email where title couldn't be extracted
    email = ConfirmationEmail(company="Sigma Software", title="",
                              date="2026-05-20", subject="...", platform="smartrecruiters")
    result = match_email(email)
    assert result.match_type == "fuzzy"
    assert result.row_id is not None


def test_match_email_no_title_multiple_rows_ambiguous(tracker_db):
    _insert_tracker_row(tracker_db, company="Acme", title="Angular Developer",
                        row_id="id0id0id")
    _insert_tracker_row(tracker_db, company="Acme", title="React Developer",
                        row_id="id1id1id")
    email = ConfirmationEmail(company="Acme", title="",
                              date="2026-05-20", subject="...", platform="direct")
    assert match_email(email).match_type == "ambiguous"


# ---------------------------------------------------------------------------
# fetch_confirmation_emails (mocked Gmail service)
# ---------------------------------------------------------------------------

def test_fetch_returns_empty_on_gmail_error():
    service = MagicMock()
    service.users().messages().list().execute.side_effect = Exception("network error")
    result = fetch_confirmation_emails(service, lookback_days=7)
    assert result == []


def test_fetch_filters_non_confirmation_subjects():
    msg = _make_msg(subject="10 new jobs for you", sender="jobs@erecruiter.pl")
    service = MagicMock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "1"}]}
    service.users().messages().get().execute.return_value = msg
    result = fetch_confirmation_emails(service, lookback_days=7)
    assert result == []


def test_fetch_parses_erecruiter_confirmation():
    msg = _make_msg(
        subject="NASK - Dziękujemy za złożenie aplikacji na stanowisko Senior Frontend Developer",
        sender="eRecruiter <mail@stage.erecruiter.pl>",
    )
    service = MagicMock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "1"}]}
    service.users().messages().get().execute.return_value = msg
    result = fetch_confirmation_emails(service, lookback_days=7)
    assert len(result) == 1
    assert result[0].company == "NASK"
    assert result[0].title == "Senior Frontend Developer"
    assert result[0].platform == "erecruiter"


def test_fetch_parses_smartrecruiters_confirmation():
    body = "Thank you for submitting your application for the position of Middle Front-End Developer."
    msg = _make_msg(
        subject="Thank you for applying to Sigma Software",
        sender="Sigma Software <notification@smartrecruiters.com>",
        body_text=body,
    )
    service = MagicMock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": "1"}]}
    service.users().messages().get().execute.return_value = msg
    result = fetch_confirmation_emails(service, lookback_days=7)
    assert len(result) == 1
    assert result[0].company == "Sigma Software"
    assert result[0].title == "Middle Front-End Developer"


# ---------------------------------------------------------------------------
# run_confirmation_check — writes CONFIRMED to tracker
# ---------------------------------------------------------------------------

def test_run_writes_confirmed(tracker_db, monkeypatch):
    import hunter.email_response_checker as checker

    _insert_tracker_row(tracker_db, company="NASK", title="Senior Frontend Developer")

    confirmed_email = ConfirmationEmail(
        company="NASK", title="Senior Frontend Developer",
        date="2026-05-20", subject="NASK - Dziękujemy...", platform="erecruiter",
    )
    monkeypatch.setattr(checker, "fetch_confirmation_emails",
                        lambda svc, days: [confirmed_email])

    with patch("hunter.email_response_checker.get_gmail_service",
               return_value=MagicMock()):
        results = checker.run_confirmation_check(lookback_days=7)

    assert results[0].match_type in ("exact", "fuzzy")
    rows = lookup_by_company_and_title("NASK", "Senior Frontend Developer")
    assert len(rows) == 1
    assert rows[0]["confirmation"] == "2026-05-20"


def test_run_does_not_overwrite_existing_confirmation(tracker_db, monkeypatch):
    import hunter.email_response_checker as checker

    _insert_tracker_row(tracker_db, company="NASK", title="Senior Frontend Developer",
                        confirmation="2026-04-01")

    confirmed_email = ConfirmationEmail(
        company="NASK", title="Senior Frontend Developer",
        date="2026-05-20", subject="...", platform="erecruiter",
    )
    monkeypatch.setattr(checker, "fetch_confirmation_emails",
                        lambda svc, days: [confirmed_email])

    with patch("hunter.email_response_checker.get_gmail_service",
               return_value=MagicMock()):
        checker.run_confirmation_check(lookback_days=7)

    rows = lookup_by_company_and_title("NASK", "Senior Frontend Developer")
    assert rows[0]["confirmation"] == "2026-04-01"  # untouched
