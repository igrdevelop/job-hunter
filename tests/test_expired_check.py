"""
Tests for P-2.x: expired_check.py + iter_unsent_rows dash-marker fix.
"""
import uuid
import pytest

from hunter.expired_check import is_job_expired, is_expired_by_html
from hunter.tracker import iter_unsent_rows, _is_unsent, normalize_url
from hunter.db import get_db


# ---------------------------------------------------------------------------
# _is_unsent — P-2.3 helper
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sent, expected", [
    ("", True),           # empty → unsent
    ("—", True),          # em-dash → unsent
    ("–", True),          # en-dash → unsent
    ("-", True),          # hyphen → unsent
    ("2026-05-01", False),  # real date → sent
    ("EXPIRED", False),     # EXPIRED stamp → processed
    ("yes", False),         # any non-empty non-dash → sent
])
def test_is_unsent(sent: str, expected: bool) -> None:
    assert _is_unsent(sent) == expected


# ---------------------------------------------------------------------------
# iter_unsent_rows — dash-marked rows are included (P-2.3)
# ---------------------------------------------------------------------------

def _insert_row(tracker_db, *, company, title, ats, url, sent, rid=None):
    row_id = rid or uuid.uuid4().hex[:8]
    norm = normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm, sent)
            VALUES (?, '2026-05-10', ?, ?, ?, ?, ?, ?)
            """,
            (row_id, company, title, ats, url, norm, sent),
        )


def test_iter_unsent_includes_em_dash_sent(tracker_db) -> None:
    """Rows with Sent=— must appear in iter_unsent_rows (P-2.3 fix)."""
    _insert_row(tracker_db, company="Acme", title="Angular Dev", ats="87%",
                url="https://example.com/1", sent="—", rid="aaa00001")
    rows = iter_unsent_rows()
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"


def test_iter_unsent_excludes_real_date_sent(tracker_db) -> None:
    """Rows with a real Sent date must NOT appear."""
    _insert_row(tracker_db, company="Acme", title="Angular Dev", ats="87%",
                url="https://example.com/1", sent="2026-05-10", rid="aaa00002")
    rows = iter_unsent_rows()
    assert rows == []


def test_iter_unsent_excludes_expired_sent(tracker_db) -> None:
    _insert_row(tracker_db, company="Acme", title="Angular Dev", ats="87%",
                url="https://example.com/1", sent="EXPIRED", rid="aaa00003")
    rows = iter_unsent_rows()
    assert rows == []


# ---------------------------------------------------------------------------
# is_job_expired — core text patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "This offer has expired.",
    "This job is no longer available.",
    "Position has been filled.",
    "Application period has closed.",
    "No longer accepting applications.",
    "Oferta wygasła.",
    "Ta oferta pracy wygasła.",
    "Pracodawca zakończył zbieranie zgłoszeń",
])
def test_is_job_expired_positive(text: str) -> None:
    assert is_job_expired(text), f"Expected expired for: {text!r}"


def test_is_job_expired_false_for_live_posting() -> None:
    text = "We are looking for a Senior Angular Developer to join our team."
    assert not is_job_expired(text)


def test_is_job_expired_empty_text() -> None:
    assert not is_job_expired("")


# ---------------------------------------------------------------------------
# is_expired_by_html — new domains (P-2.1)
# ---------------------------------------------------------------------------

def test_html_expired_smartrecruiters() -> None:
    html = "<p>Hey, requested application form is inactive</p>"
    assert is_expired_by_html(html, "smartrecruiters.com")


def test_html_expired_theprotocol_isactive_false() -> None:
    html = '{"isActive":false,"title":"Angular Dev"}'
    assert is_expired_by_html(html, "theprotocol.it")


def test_html_expired_greenhouse() -> None:
    html = "<h1>This job has been closed</h1>"
    assert is_expired_by_html(html, "boards.greenhouse.io")


def test_html_expired_lever() -> None:
    html = "<p>This job posting is no longer available</p>"
    assert is_expired_by_html(html, "jobs.lever.co")


def test_html_expired_no_match_different_domain() -> None:
    html = "<p>Hey, requested application form is inactive</p>"
    # smartrecruiters marker should NOT match on a different domain
    assert not is_expired_by_html(html, "justjoin.it")
