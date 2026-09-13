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


def _insert(db, *, url=None, ats="", sent="", answer="", confirmation="", d=None, source=""):
    d = d if d is not None else date.today().isoformat()
    # Each call that doesn't specify a URL gets its own unique URL so multiple
    # inserts don't collide on the (user_id, url_norm) unique constraint.
    if url is None:
        url = f"https://x.com/{uuid.uuid4().hex[:8]}"
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, "
            "url_norm, sent, confirmation, answer, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:8],
                d,
                "Co",
                "Dev",
                ats,
                url,
                url,
                sent,
                confirmation,
                answer,
                source,
            ),
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
    _insert(
        funnel_db, ats="90%", sent="2026-06-10", answer="Interview", confirmation="2026-06-11"
    )  # gen+sent+conf+ans
    _insert(funnel_db, ats="80%", sent="2026-06-09", confirmation="2026-06-10")  # gen+sent+conf
    _insert(funnel_db, ats="75%")  # gen only
    _insert(funnel_db, ats="SKIP")  # tracked only
    _insert(funnel_db, ats="EXPIRED", sent="EXPIRED")  # tracked only

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


def test_by_source_prefers_the_stored_column_over_the_url_guess(funnel_db):
    """M3: a Greenhouse link surfaced by justjoin must count under justjoin.

    The URL guess routes it to ats_aggregator — exactly the collapse the
    stored column exists to undo."""
    gh = "https://boards.greenhouse.io/acme/jobs/123"
    assert funnel.source_for_url(gh) == "ats_aggregator"
    _insert(funnel_db, url=gh, ats="90%", sent="2026-06-10", source="justjoin")

    rep = funnel.compute_funnel()
    assert rep.by_source["justjoin"].sent == 1
    assert "ats_aggregator" not in rep.by_source


def test_by_source_falls_back_to_the_url_guess_for_a_blank_source(funnel_db):
    """A pre-M3 row (source='') keeps the old attribution byte-for-byte."""
    _insert(funnel_db, url="https://nofluffjobs.com/job/x", ats="70%", source="")
    _insert(funnel_db, url="https://nofluffjobs.com/job/y", ats="70%", source="   ")

    rep = funnel.compute_funnel()
    assert rep.by_source["nofluffjobs"].tracked == 2


def test_source_for_row():
    assert funnel.source_for_row("gmail", "https://justjoin.it/o/a") == "gmail"
    assert funnel.source_for_row("", "https://justjoin.it/o/a") == "justjoin"
    assert funnel.source_for_row(None, "https://justjoin.it/o/a") == "justjoin"
    assert funnel.source_for_row("", "") == "—"


def test_compute_funnel_tolerates_a_db_without_the_source_column(tmp_path, monkeypatch):
    """get_db() does not migrate; a pre-M3 database must not crash /funnel."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE applications (id TEXT, date TEXT, ats_status TEXT, url TEXT, "
        "sent TEXT, confirmation TEXT, answer TEXT, outcome_label TEXT)"
    )
    conn.execute(
        "INSERT INTO applications VALUES ('a1b2c3d4', ?, '80%', 'https://justjoin.it/o/1', "
        "'2026-09-01', '', '', '')",
        (date.today().isoformat(),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(funnel, "DB_PATH", path)

    rep = funnel.compute_funnel()
    assert rep.overall.sent == 1
    assert rep.by_source["justjoin"].sent == 1


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

    _insert(
        funnel_db, url="https://justjoin.it/o/a", ats="90%", sent="2026-06-10", answer="Interview"
    )
    text = funnel_cmd._build_report(None)
    assert "Application funnel" in text
    assert "Tracked:" in text
    assert "justjoin" in text


def test_cmd_parse_days():
    from hunter.commands import funnel as funnel_cmd

    assert funnel_cmd._parse_days(["30"]) == 30
    assert funnel_cmd._parse_days([]) is None
    assert funnel_cmd._parse_days(["abc"]) is None
