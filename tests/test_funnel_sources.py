"""Tests for tools/funnel_sources.py (docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M0.2)."""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

from hunter.db import get_db

TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _load_module():
    spec = importlib.util.spec_from_file_location("funnel_sources", TOOLS_DIR / "funnel_sources.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["funnel_sources"] = module
    spec.loader.exec_module(module)
    return module


fsrc = _load_module()


def _insert(db, *, url=None, ats="", sent="", answer="", cost=None, d=None):
    d = d if d is not None else date.today().isoformat()
    if url is None:
        url = f"https://x.com/{uuid.uuid4().hex[:8]}"
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, "
            "url_norm, sent, answer, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex[:8], d, "Co", "Dev", ats, url, url, sent, answer, cost),
        )


# ── wilson_ci: checked against hand-computed reference values ──────────────


def test_wilson_ci_matches_hand_computed_95pct():
    # n=100, x=50, z=1.96 (95%) -> hand-computed via the standard Wilson
    # score formula (Wikipedia): center=(phat+z^2/2n)/(1+z^2/n),
    # margin=z*sqrt(phat(1-phat)/n + z^2/4n^2)/(1+z^2/n).
    lo, hi = fsrc.wilson_ci(50, 100, z=1.96)
    assert lo == pytest.approx(0.40382982859014716, abs=1e-9)
    assert hi == pytest.approx(0.5961701714098528, abs=1e-9)


def test_wilson_ci_matches_hand_computed_90pct():
    # n=20, x=10, z=1.645 (90%, the tool's own default)
    lo, hi = fsrc.wilson_ci(10, 20, z=1.645)
    assert lo == pytest.approx(0.3273902391760559, abs=1e-9)
    assert hi == pytest.approx(0.6726097608239442, abs=1e-9)


def test_wilson_ci_zero_total():
    assert fsrc.wilson_ci(0, 0) == (0.0, 0.0)


def test_wilson_ci_bounds_stay_within_unit_interval():
    lo, hi = fsrc.wilson_ci(0, 10)
    assert 0.0 <= lo <= hi <= 1.0
    lo, hi = fsrc.wilson_ci(10, 10)
    assert 0.0 <= lo <= hi <= 1.0


# ── aggregate_extra ──────────────────────────────────────────────────────────


def test_aggregate_extra_counts_fail_skip_and_cost():
    rows = [
        {
            "date": "2026-06-01",
            "url": "https://justjoin.it/a",
            "ats_status": "FAIL",
            "sent": "",
            "cost_usd": None,
        },
        {
            "date": "2026-06-01",
            "url": "https://justjoin.it/b",
            "ats_status": "SKIP",
            "sent": "",
            "cost_usd": None,
        },
        {
            "date": "2026-06-01",
            "url": "https://justjoin.it/c",
            "ats_status": "90%",
            "sent": "2026-06-02",
            "cost_usd": 0.5,
        },
        {
            "date": "2026-06-01",
            "url": "https://justjoin.it/d",
            "ats_status": "80%",
            "sent": "2026-06-02",
            "cost_usd": None,
        },
    ]
    extra = fsrc.aggregate_extra(rows, days=None)
    j = extra["justjoin"]
    assert j.fail == 1
    assert j.skip == 1
    assert j.cost_priced_sent == 1
    assert j.cost_unpriced_sent == 1
    assert j.cost_per_sent == pytest.approx(0.5)
    assert j.filtered_urls == ["https://justjoin.it/b"]


def test_aggregate_extra_respects_days_window():
    old = (date.today() - __import__("datetime").timedelta(days=200)).isoformat()
    rows = [
        {
            "date": old,
            "url": "https://justjoin.it/old",
            "ats_status": "SKIP",
            "sent": "",
            "cost_usd": None,
        },
        {
            "date": date.today().isoformat(),
            "url": "https://justjoin.it/new",
            "ats_status": "SKIP",
            "sent": "",
            "cost_usd": None,
        },
    ]
    extra = fsrc.aggregate_extra(rows, days=30)
    assert extra["justjoin"].skip == 1


def test_aggregate_extra_caps_filtered_url_sample_at_10():
    rows = [
        {
            "date": "2026-06-01",
            "url": f"https://justjoin.it/{i}",
            "ats_status": "SKIP",
            "sent": "",
            "cost_usd": None,
        }
        for i in range(15)
    ]
    extra = fsrc.aggregate_extra(rows, days=None)
    assert extra["justjoin"].skip == 15
    assert len(extra["justjoin"].filtered_urls) == 10


# ── decide ────────────────────────────────────────────────────────────────────


def test_decide_ballast():
    d = fsrc.decide(
        tracked=40,
        sent=0,
        answered=0,
        health_status="OK",
        health_source="x",
        zero_streak=0,
        days=90,
    )
    assert d == "ballast"


def test_decide_watch():
    d = fsrc.decide(
        tracked=10,
        sent=5,
        answered=0,
        health_status="OK",
        health_source="x",
        zero_streak=0,
        days=30,
    )
    assert d == "watch"


def test_decide_watch_requires_min_window():
    # sent>=5, answered==0, but window < 21 days -> not enough to call "watch"
    d = fsrc.decide(
        tracked=10,
        sent=5,
        answered=0,
        health_status="OK",
        health_source="x",
        zero_streak=0,
        days=10,
    )
    assert d == "ok"


def test_decide_ok_when_nothing_flagged():
    d = fsrc.decide(
        tracked=5, sent=3, answered=1, health_status="OK", health_source="x", zero_streak=0, days=90
    )
    assert d == "ok"


def test_decide_watch_broken_when_streak_recent(monkeypatch):
    # BROKEN? status but the streak hasn't lasted 14 days yet
    monkeypatch.setattr(fsrc, "broken_since_days", lambda source, streak: 2)
    d = fsrc.decide(
        tracked=5,
        sent=0,
        answered=0,
        health_status="BROKEN?",
        health_source="x",
        zero_streak=3,
        days=90,
    )
    assert d == "watch-broken"


def test_decide_broken_when_streak_over_14_days(monkeypatch):
    monkeypatch.setattr(fsrc, "broken_since_days", lambda source, streak: 20)
    d = fsrc.decide(
        tracked=5,
        sent=0,
        answered=0,
        health_status="BROKEN?",
        health_source="x",
        zero_streak=3,
        days=90,
    )
    assert d == "broken"


# ── build_report end-to-end over an isolated tracker.db ────────────────────


def test_build_report_ballast_source(tracker_db):
    for _ in range(31):
        _insert(tracker_db, url=f"https://justjoin.it/{uuid.uuid4().hex[:8]}", ats="SKIP")

    report = fsrc.build_report(days=90, db_path=tracker_db)
    assert "justjoin" in report["sources"]
    s = report["sources"]["justjoin"]
    assert s["tracked"] == 31
    assert s["sent"] == 0
    assert s["decision"] == "ballast"
    assert len(s["filtered_urls_sample"]) <= 10


def test_build_report_sent_and_cost(tracker_db):
    _insert(tracker_db, url="https://justjoin.it/a", ats="90%", sent="2026-06-01", cost=0.4)
    _insert(tracker_db, url="https://justjoin.it/b", ats="80%", sent="2026-06-02", cost=0.6)

    report = fsrc.build_report(days=None, db_path=tracker_db)
    s = report["sources"]["justjoin"]
    assert s["sent"] == 2
    assert s["cost_per_sent"] == pytest.approx(0.5)


def test_build_report_format_smoke(tracker_db):
    _insert(tracker_db, url="https://justjoin.it/a", ats="90%", sent="2026-06-01", cost=0.4)
    report = fsrc.build_report(days=90, db_path=tracker_db)
    text = fsrc.format_report(report)
    assert "justjoin" in text
    assert "Decision rule" in text
