"""Tests for hunter/source_health.py — per-source yield tracking + breakage flag."""

from __future__ import annotations

import pytest

from hunter import source_health


@pytest.fixture()
def health_db(tmp_path, monkeypatch):
    """Isolate source_health on a fresh temp DB."""
    db = tmp_path / "health.db"
    monkeypatch.setattr(source_health, "DB_PATH", db)
    # Deterministic thresholds regardless of .env.
    monkeypatch.setattr(source_health, "SOURCE_HEALTH_ALERT_STREAK", 3)
    monkeypatch.setattr(source_health, "SOURCE_HEALTH_KEEP", 50)
    return db


def _feed(source, *yields):
    """Record a sequence of yields (oldest first). Ints = ok run; 'ERR' = failure."""
    for y in yields:
        if y == "ERR":
            source_health.record_run(source, 0, ok=False, error="boom")
        else:
            source_health.record_run(source, y, ok=True)


# ── record / recent ───────────────────────────────────────────────────────────


def test_record_and_recent_newest_first(health_db):
    _feed("justjoin", 5, 3, 8)
    runs = source_health.recent_runs("justjoin")
    assert [r.yield_count for r in runs] == [8, 3, 5]
    assert all(r.ok for r in runs)


def test_recent_limit(health_db):
    _feed("x", *range(10))
    assert len(source_health.recent_runs("x", limit=4)) == 4


def test_prune_keeps_only_keep_rows(health_db, monkeypatch):
    monkeypatch.setattr(source_health, "SOURCE_HEALTH_KEEP", 5)
    _feed("x", *range(12))
    runs = source_health.recent_runs("x", limit=100)
    assert len(runs) == 5
    # Newest five yields kept: 11,10,9,8,7
    assert [r.yield_count for r in runs] == [11, 10, 9, 8, 7]


# ── status classification ─────────────────────────────────────────────────────


def test_status_ok(health_db):
    _feed("a", 4, 5, 6)
    h = source_health.source_health("a")
    assert h.status == "OK"
    assert h.last_yield == 6
    assert h.avg_yield == 5.0


def test_status_error_on_last_failure(health_db):
    _feed("a", 5, "ERR")
    assert source_health.source_health("a").status == "ERROR"


def test_status_idle_zero_but_not_enough_streak(health_db):
    # one zero after positives → IDLE, not BROKEN?
    _feed("a", 5, 5, 0)
    h = source_health.source_health("a")
    assert h.status == "IDLE"
    assert h.zero_streak == 1


def test_status_broken_when_working_source_goes_dry(health_db):
    _feed("a", 7, 6, 0, 0, 0)
    h = source_health.source_health("a")
    assert h.status == "BROKEN?"
    assert h.zero_streak == 3
    assert h.ever_positive


def test_status_idle_when_never_positive(health_db):
    # always zero → never alarming (board just has no matches for us)
    _feed("a", 0, 0, 0, 0)
    h = source_health.source_health("a")
    assert h.status == "IDLE"
    assert not h.ever_positive


def test_nodata_for_unknown_source(health_db):
    h = source_health.source_health("ghost")
    assert h.status == "NODATA"
    assert h.last_yield is None


def test_error_run_counts_toward_streak(health_db):
    _feed("a", 9, "ERR", "ERR", "ERR")
    h = source_health.source_health("a")
    # last run is an error → ERROR status takes precedence over BROKEN?
    assert h.status == "ERROR"
    assert h.zero_streak == 3


# ── newly_broken (alert-once semantics) ───────────────────────────────────────


def test_newly_broken_fires_once_at_threshold(health_db):
    _feed("a", 8, 8)
    source_health.record_run("a", 0)  # streak 1
    assert not source_health.newly_broken("a")
    source_health.record_run("a", 0)  # streak 2
    assert not source_health.newly_broken("a")
    source_health.record_run("a", 0)  # streak 3 → fire
    assert source_health.newly_broken("a")
    source_health.record_run("a", 0)  # streak 4 → no longer "newly"
    assert not source_health.newly_broken("a")


def test_newly_broken_false_when_never_positive(health_db):
    _feed("a", 0, 0, 0)
    assert not source_health.newly_broken("a")


# ── health_report ─────────────────────────────────────────────────────────────


def test_health_report_all_sources_sorted_by_severity(health_db):
    _feed("good", 5, 5, 5)
    _feed("broken", 5, 0, 0, 0)
    _feed("err", 5, "ERR")
    report = source_health.health_report()
    statuses = [(h.source, h.status) for h in report]
    # ERROR first, then BROKEN?, then OK
    assert statuses[0] == ("err", "ERROR")
    assert statuses[1] == ("broken", "BROKEN?")
    assert statuses[-1] == ("good", "OK")


def test_health_report_named_sources_include_nodata(health_db):
    _feed("known", 5)
    report = source_health.health_report(["known", "never"])
    by_name = {h.source: h.status for h in report}
    assert by_name["known"] == "OK"
    assert by_name["never"] == "NODATA"


# ── /health command report builder ────────────────────────────────────────────


def test_build_report_sections(health_db, monkeypatch):
    """The /health text groups sources into attention / healthy / idle / nodata."""
    # A real source name + a synthetic broken/idle set; _build_report uses the
    # live ALL_SOURCES roster, so seed a couple of real source names.
    from hunter.commands import health as health_cmd
    from hunter.sources import ALL_SOURCES

    names = [s.name for s in ALL_SOURCES]
    good, broken = names[0], names[1]
    _feed(good, 5, 6, 7)
    _feed(broken, 9, 0, 0, 0)  # was working, now dry → BROKEN?

    text = health_cmd._build_report()
    assert "Scraper health" in text
    assert "Needs attention" in text
    assert broken in text
    assert good in text
    # Sources never recorded show up under "No data yet".
    assert "No data yet" in text
