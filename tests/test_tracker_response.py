"""Tests for tracker Confirmation column: set_confirmation, lookup_by_company_and_title,
_title_tokens, _title_similarity."""

import uuid
import pytest

from hunter import tracker
from hunter.tracker import (
    COL_CONFIRMATION,
    _title_similarity,
    _title_tokens,
)
from hunter.db import get_db


# ---------------------------------------------------------------------------
# Helper: insert a row directly into the DB
# ---------------------------------------------------------------------------

def _insert(tracker_db, *, company: str, title: str,
            ats: str = "85%", url: str = "", sent: str = "",
            confirmation: str = "") -> str:
    """Insert a row and return its ID."""
    row_id = uuid.uuid4().hex[:8]
    if not url:
        url = f"https://example.com/jobs/{row_id}"
    norm = tracker.normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm, sent, confirmation)
            VALUES (?, '2026-05-22', ?, ?, ?, ?, ?, ?, ?)
            """,
            (row_id, company, title, ats, url, norm, sent, confirmation),
        )
    return row_id


# ---------------------------------------------------------------------------
# _title_tokens
# ---------------------------------------------------------------------------

def test_title_tokens_strips_stop_words():
    tokens = _title_tokens("Senior Angular Developer")
    assert "senior" not in tokens
    assert "angular" in tokens
    assert "developer" in tokens


def test_title_tokens_strips_diacritics():
    tokens = _title_tokens("Inżynier Frontend")
    assert "frontend" in tokens


def test_title_tokens_excludes_short_words():
    tokens = _title_tokens("UI JS Developer")
    assert "ui" not in tokens
    assert "js" not in tokens
    assert "developer" in tokens


def test_title_tokens_empty_string():
    assert _title_tokens("") == set()


# ---------------------------------------------------------------------------
# _title_similarity
# ---------------------------------------------------------------------------

def test_similarity_same_title():
    assert _title_similarity("Angular Developer", "Angular Developer") == 1.0


def test_similarity_senior_prefix_ignored():
    score = _title_similarity("Senior Angular Developer", "Angular Developer")
    assert score == 1.0


def test_similarity_partial_overlap():
    score = _title_similarity("Angular Developer", "Angular Engineer")
    assert score == pytest.approx(0.5)


def test_similarity_no_overlap():
    score = _title_similarity("Frontend Engineer", "Backend Java Developer")
    assert score == 0.0


def test_similarity_empty_titles():
    assert _title_similarity("", "Angular Developer") == 0.0
    assert _title_similarity("Angular Developer", "") == 0.0


# ---------------------------------------------------------------------------
# lookup_by_company_and_title
# ---------------------------------------------------------------------------

def test_lookup_returns_empty_when_db_empty(tracker_db):
    assert tracker.lookup_by_company_and_title("Acme", "Angular Developer") == []


def test_lookup_returns_empty_for_wrong_company(tracker_db):
    _insert(tracker_db, company="Acme", title="Angular Developer")
    result = tracker.lookup_by_company_and_title("OtherCorp", "Angular Developer")
    assert result == []


def test_lookup_returns_match(tracker_db):
    row_id = _insert(tracker_db, company="Acme", title="Angular Developer")
    result = tracker.lookup_by_company_and_title("Acme", "Angular Developer")
    assert len(result) == 1
    assert result[0]["company"] == "Acme"
    assert result[0]["title"] == "Angular Developer"
    assert result[0]["id"] == row_id


def test_lookup_returns_all_statuses(tracker_db):
    _insert(tracker_db, company="Acme", title="Angular Developer", ats="SKIP")
    _insert(tracker_db, company="Acme", title="Angular Developer", ats="85%")
    result = tracker.lookup_by_company_and_title("Acme", "Angular Developer")
    assert len(result) == 2


def test_lookup_score_threshold(tracker_db):
    _insert(tracker_db, company="Acme", title="Angular Developer")
    # high threshold — close enough
    result = tracker.lookup_by_company_and_title("Acme", "Angular Developer", title_min_score=0.5)
    assert len(result) == 1
    # too high — no match
    result = tracker.lookup_by_company_and_title("Acme", "Angular Developer", title_min_score=0.6)
    assert len(result) == 1  # exact match scores 1.0 so always passes 0.6


def test_lookup_partial_title_match(tracker_db):
    _insert(tracker_db, company="Acme", title="Angular Developer")
    result = tracker.lookup_by_company_and_title("Acme", "Angular Engineer", title_min_score=0.4)
    assert len(result) == 1
    assert result[0]["title_score"] == pytest.approx(0.5)


def test_lookup_no_match_below_threshold(tracker_db):
    _insert(tracker_db, company="Acme", title="Angular Developer")
    result = tracker.lookup_by_company_and_title("Acme", "Angular Engineer", title_min_score=0.6)
    assert result == []  # 0.5 < 0.6


def test_lookup_returns_confirmation_field(tracker_db):
    _insert(tracker_db, company="Acme", title="Angular Developer", confirmation="2026-05-15")
    result = tracker.lookup_by_company_and_title("Acme", "Angular Developer")
    assert len(result) == 1
    assert result[0]["confirmation"] == "2026-05-15"


def test_lookup_returns_empty_confirmation_when_missing(tracker_db):
    _insert(tracker_db, company="Acme", title="Angular Developer")
    result = tracker.lookup_by_company_and_title("Acme", "Angular Developer")
    assert result[0]["confirmation"] == ""


# ---------------------------------------------------------------------------
# set_confirmation — now takes row_id (str), not row_num (int)
# ---------------------------------------------------------------------------

def test_set_confirmation_noop_for_empty_id(tracker_db):
    """No-op when row_id is empty string."""
    tracker.set_confirmation("", "2026-05-20")  # must not raise


def test_set_confirmation_noop_for_unknown_id(tracker_db):
    """No-op when row_id doesn't exist in DB."""
    tracker.set_confirmation("deadbeef", "2026-05-20")  # must not raise
    # DB still empty
    with get_db(tracker_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    assert count == 0


def test_set_confirmation_writes_date(tracker_db):
    row_id = _insert(tracker_db, company="Acme", title="Angular Developer")
    tracker.set_confirmation(row_id, "2026-05-20")
    with get_db(tracker_db) as conn:
        val = conn.execute(
            "SELECT confirmation FROM applications WHERE id=?", (row_id,)
        ).fetchone()["confirmation"]
    assert val == "2026-05-20"


def test_set_confirmation_overwrites_existing_date(tracker_db):
    row_id = _insert(tracker_db, company="Acme", title="Dev", confirmation="2026-04-01")
    tracker.set_confirmation(row_id, "2026-05-20")
    with get_db(tracker_db) as conn:
        val = conn.execute(
            "SELECT confirmation FROM applications WHERE id=?", (row_id,)
        ).fetchone()["confirmation"]
    assert val == "2026-05-20"


def test_set_confirmation_roundtrip_via_lookup(tracker_db):
    row_id = _insert(tracker_db, company="Acme", title="Angular Developer")
    tracker.set_confirmation(row_id, "2026-05-20")
    result = tracker.lookup_by_company_and_title("Acme", "Angular Developer")
    assert result[0]["confirmation"] == "2026-05-20"


# ---------------------------------------------------------------------------
# Schema: TRACKER_HEADERS includes Confirmation and Answer
# ---------------------------------------------------------------------------

def test_tracker_headers_include_confirmation_and_answer():
    from hunter.tracker import TRACKER_HEADERS
    assert "Confirmation" in TRACKER_HEADERS
    assert "Answer" in TRACKER_HEADERS
    assert TRACKER_HEADERS.index("Confirmation") == COL_CONFIRMATION - 1
