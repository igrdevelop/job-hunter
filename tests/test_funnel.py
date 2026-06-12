"""Tests for hunter/funnel.py — application funnel analytics over tracker.db."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from hunter import funnel
from hunter.db import get_db


@pytest.fixture()
def funnel_db(tracker_db, monkeypatch):
    """tracker_db gives an isolated DB + patches tracker.DB_PATH; also point
    funnel.DB_PATH at it."""
    monkeypatch.setattr(funnel, "DB_PATH", tracker_db)
    return tracker_db


def _insert(db, *, url="https://x.com/j", ats="", sent="", answer="",
            confirmation="", d=None):
    d = d if d is not None else date.today().isoformat()
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, "
            "url_norm, sent, confirmation, answer) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:8], d, "Co", "Dev", ats, url, url, sent,
             confirmation, answer),
        )


# ── source attribution ────────────────────────────────────────────────────────

def test_source_for_url_matches_known_board():
    # justjoin.it is a registered source — matches_url should attribute it.
    assert funnel.source_for_url("https://justjoin.it/offers/some-angular-role") == "justjoin"


def test_source_for_url_domain_fallback():
    # Unknown host → registered-domain bucket.
    assert funnel.source_for_url("https://careers.randomstartup.io/jobs/1") == "randomstartup.io"


def test_source_for_url_empty():
    assert funnel.source_for_url("") == "—"


def test_registered_domain():
    assert funnel._registered_domain("https://jobs.example.com/x") == "example.com"
    assert funnel._registered_domain("https://nofluffjobs.com") == "nofluffjobs.com"
    assert funnel._registered_domain("not a url") == "?"


# ── classification helpers ────────────────────────────────────────────────────

def test_is_generated():
    assert funnel._is_generated("85%")
    assert funnel._is_generated("100%")
    assert not funnel._is_generated("SKIP")
    assert not funnel._is_generated("FAIL")
    assert not funnel._is_generated("MANUAL")
    assert not funnel._is_generated("")
    assert not funnel._is_generated("—")


def test_is_sent():
    assert funnel._is_sent("2026-06-10")
    assert funnel._is_sent("13 05 26")
    assert not funnel._is_sent("")
    assert not funnel._is_sent("—")
    assert not funnel._is_sent("EXPIRED")


def test_is_confirmed_and_answered():
    assert funnel._is_confirmed("2026-06-01")
    assert not funnel._is_confirmed("")
    assert funnel._is_answered("Rejected")
    assert not funnel._is_answered("")


# ── compute_funnel ────────────────────────────────────────────────────────────

def test_overall_counts(funnel_db):
    _insert(funnel_db, ats="90%", sent="2026-06-10", answer="Interview",
            confirmation="2026-06-11")                                     # gen+sent+conf+ans
    _insert(funnel_db, ats="80%", sent="2026-06-09", confirmation="2026-06-10")  # gen+sent+conf
    _insert(funnel_db, ats="75%")                                          # gen only
    _insert(funnel_db, ats="SKIP")                                         # tracked only
    _insert(funnel_db, ats="EXPIRED", sent="EXPIRED")                      # tracked only

    rep = funnel.compute_funnel()
    o = rep.overall
    assert o.tracked == 5
    assert o.generated == 3
    assert o.sent == 2
    assert o.confirmed == 2
    assert o.answered == 1
    assert o.sent_rate == round(100 * 2 / 3, 1)
    assert o.confirm_rate == 100.0
    assert o.answer_rate == 50.0


def test_by_source_grouping(funnel_db):
    _insert(funnel_db, url="https://justjoin.it/o/a", ats="90%", sent="2026-06-10")
    _insert(funnel_db, url="https://justjoin.it/o/b", ats="80%")
    _insert(funnel_db, url="https://nofluffjobs.com/job/x", ats="70%", sent="2026-06-11")

    rep = funnel.compute_funnel()
    assert rep.by_source["justjoin"].tracked == 2
    assert rep.by_source["justjoin"].generated == 2
    assert rep.by_source["justjoin"].sent == 1
    assert rep.by_source["nofluffjobs"].sent == 1


def test_top_sources_sorted_by_sent(funnel_db):
    _insert(funnel_db, url="https://justjoin.it/o/a", ats="90%", sent="2026-06-10")
    _insert(funnel_db, url="https://justjoin.it/o/b", ats="90%", sent="2026-06-10")
    _insert(funnel_db, url="https://nofluffjobs.com/x", ats="90%", sent="2026-06-10")

    rep = funnel.compute_funnel()
    top = rep.top_sources()
    assert top[0][0] == "justjoin"  # 2 sent ranks above 1 sent


def test_days_filter_excludes_old_and_undated(funnel_db):
    recent = date.today().isoformat()
    old = (date.today() - timedelta(days=90)).isoformat()
    _insert(funnel_db, ats="90%", sent=recent, d=recent)
    _insert(funnel_db, ats="90%", sent=old, d=old)
    _insert(funnel_db, ats="90%", d="")  # undated

    rep_all = funnel.compute_funnel()
    assert rep_all.overall.tracked == 3

    rep_30 = funnel.compute_funnel(days=30)
    # only the recent row survives the window; old + undated excluded
    assert rep_30.overall.tracked == 1


def test_empty_db(funnel_db):
    rep = funnel.compute_funnel()
    assert rep.overall.tracked == 0
    assert rep.overall.sent_rate == 0.0
    assert rep.overall.confirm_rate == 0.0
    assert rep.overall.answer_rate == 0.0


# ── command report builder ────────────────────────────────────────────────────

def test_cmd_build_report(funnel_db):
    from hunter.commands import funnel as funnel_cmd
    _insert(funnel_db, url="https://justjoin.it/o/a", ats="90%", sent="2026-06-10",
            answer="Interview")
    text = funnel_cmd._build_report(None)
    assert "Application funnel" in text
    assert "Tracked:" in text
    assert "justjoin" in text


def test_cmd_parse_days():
    from hunter.commands import funnel as funnel_cmd
    assert funnel_cmd._parse_days(["30"]) == 30
    assert funnel_cmd._parse_days([]) is None
    assert funnel_cmd._parse_days(["abc"]) is None
