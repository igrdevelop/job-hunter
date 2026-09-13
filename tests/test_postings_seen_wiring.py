"""docs/MARKET_MEMORY_PLAN.md M1 — hunter/main.py records every listing a
sweep SAW into ``postings_seen`` (after the filter, before dedup) and
``hunter/schedules/postings_prune.py`` trims it nightly by TTL.

Harness mirrors tests/test_main_apply_queue_wiring.py: drive ``run_hunt`` with
one fake source, the real filters (so the stored verdict is a REAL
``FILTER_REASONS`` value), Telegram mocked. Every SQLite-backed module that
would otherwise touch the repo's own tracker.db (postings_seen, best_effort,
source_health) is pointed at the ``tracker_db`` tmp file.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hunter import best_effort as be
from hunter import postings_seen
from hunter.main import run_hunt
from hunter.models import Job
from hunter.schedules.postings_prune import scheduled_postings_prune
from hunter.tracker import normalize_url

URL_OK_1 = "https://justjoin.it/job-offer/acme-senior-angular"
URL_OK_2 = "https://justjoin.it/job-offer/globex-angular"
URL_REJECT = "https://justjoin.it/job-offer/initech-junior-angular"


def _job(title: str, company: str, url: str) -> Job:
    return Job(
        title=title, company=company, location="Remote", salary=None, url=url, source="justjoin"
    )


def _three_jobs() -> list[Job]:
    return [
        _job("Senior Angular Developer", "Acme", URL_OK_1),
        _job("Angular Developer", "Globex", URL_OK_2),
        # "junior" is in the default profile's exclude_levels -> verdict "level".
        _job("Junior Angular Developer", "Initech", URL_REJECT),
    ]


class _FakeSource:
    name = "justjoin"
    manual_only = False

    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs

    def search(self) -> list[Job]:
        return list(self._jobs)


@pytest.fixture
def seen_db(tracker_db, monkeypatch):
    """Point every lazily-created side table at the isolated tracker db."""
    monkeypatch.setattr(postings_seen, "DB_PATH", tracker_db)
    monkeypatch.setattr(be, "DB_PATH", tracker_db)
    import hunter.source_health as sh

    monkeypatch.setattr(sh, "DB_PATH", tracker_db)
    return tracker_db


def _run_hunt(jobs: list[Job], *, enabled: bool = True) -> AsyncMock:
    """Run one real hunt over ``jobs`` (manual mode) and return the cards mock."""
    cards = AsyncMock()
    with (
        patch("hunter.main.AUTO_APPLY", False),
        patch("hunter.main.POSTINGS_SEEN_ENABLED", enabled),
        patch("hunter.main.ALL_SOURCES", [_FakeSource(jobs)]),
        patch("hunter.main.send_job_cards", cards),
        patch("hunter.main.send_text", AsyncMock()),
    ):
        asyncio.run(run_hunt(MagicMock()))
    return cards


def _rows(db) -> dict[str, dict]:
    with be.get_db(db) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='postings_seen'"
        ).fetchone()
        if not exists:
            return {}
        rows = conn.execute("SELECT * FROM postings_seen").fetchall()
    return {r["url_norm"]: dict(r) for r in rows}


def _failures(db, subsystem: str) -> int:
    with be.get_db(db) as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM subsystem_health WHERE subsystem = ?",
            (subsystem,),
        ).fetchone()
    return int(row["consecutive_failures"]) if row else 0


def _cards_urls(cards: AsyncMock) -> set[str]:
    return {j.url for call in cards.await_args_list for j in call.args[1]}


# ── 1. every listing is recorded with its real verdict ──────────────────────


def test_hunt_records_every_listing_with_filter_verdict(seen_db) -> None:
    cards = _run_hunt(_three_jobs())

    rows = _rows(seen_db)
    assert len(rows) == 3
    assert rows[normalize_url(URL_OK_1)]["filter_verdict"] == "passed"
    assert rows[normalize_url(URL_OK_2)]["filter_verdict"] == "passed"
    assert rows[normalize_url(URL_REJECT)]["filter_verdict"] == "level"
    assert all(r["seen_count"] == 1 for r in rows.values())
    assert all(r["filter_verdict_last"] == r["filter_verdict"] for r in rows.values())
    assert rows[normalize_url(URL_REJECT)]["company"] == "Initech"
    # The hunt itself is untouched: only the two filter-passing jobs reach ACT.
    assert _cards_urls(cards) == {URL_OK_1, URL_OK_2}


# ── 2. a second sweep bumps seen_count, never duplicates ─────────────────────


def test_second_hunt_bumps_seen_count_and_keeps_row_count(seen_db) -> None:
    _run_hunt(_three_jobs())
    _run_hunt(_three_jobs())

    rows = _rows(seen_db)
    assert len(rows) == 3
    assert {r["seen_count"] for r in rows.values()} == {2}


# ── 2b. placement: before dedup, so a tracker-known URL still counts as seen ─


def test_tracker_known_url_still_recorded(seen_db) -> None:
    from hunter import tracker

    jobs = _three_jobs()
    tracker.add_skipped(jobs[0])  # URL-known before the hunt
    cards = _run_hunt(jobs)

    rows = _rows(seen_db)
    assert normalize_url(URL_OK_1) in rows
    assert rows[normalize_url(URL_OK_1)]["filter_verdict"] == "passed"
    # ...while dedup still keeps it out of the ACT step.
    assert _cards_urls(cards) == {URL_OK_2}


# ── 3. flag off: no table, hunt otherwise identical ─────────────────────────


def test_flag_off_writes_nothing_and_hunt_is_unchanged(seen_db) -> None:
    with patch("hunter.main.record_listings", side_effect=AssertionError("must not be called")):
        cards = _run_hunt(_three_jobs(), enabled=False)

    assert _rows(seen_db) == {}
    assert _cards_urls(cards) == {URL_OK_1, URL_OK_2}


# ── 4. a broken writer never breaks the hunt, but best_effort counts it ─────


def test_record_failure_is_swallowed_and_counted(seen_db) -> None:
    with patch("hunter.main.record_listings", side_effect=RuntimeError("db on fire")):
        cards = _run_hunt(_three_jobs())

    assert _cards_urls(cards) == {URL_OK_1, URL_OK_2}
    assert _failures(seen_db, "postings.record") == 1

    # A healthy sweep afterwards resets the counter (recovery semantics).
    _run_hunt(_three_jobs())
    assert _failures(seen_db, "postings.record") == 0
    assert len(_rows(seen_db)) == 3


# ── 5. nightly prune ─────────────────────────────────────────────────────────


def _seed_aged_rows(db) -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=200)
    postings_seen.record_listings([(_job("Angular Developer", "Old", URL_OK_1), "passed")], now=old)
    postings_seen.record_listings([(_job("Angular Developer", "New", URL_OK_2), "passed")], now=now)
    assert len(_rows(db)) == 2


def test_scheduled_prune_deletes_rows_older_than_ttl(seen_db) -> None:
    _seed_aged_rows(seen_db)

    with (
        patch("hunter.config.POSTINGS_SEEN_ENABLED", True),
        patch("hunter.config.POSTINGS_TTL_DAYS", 180),
    ):
        asyncio.run(scheduled_postings_prune(MagicMock()))

    rows = _rows(seen_db)
    assert set(rows) == {normalize_url(URL_OK_2)}


def test_scheduled_prune_noop_when_flag_off(seen_db) -> None:
    _seed_aged_rows(seen_db)

    with (
        patch("hunter.config.POSTINGS_SEEN_ENABLED", False),
        patch("hunter.config.POSTINGS_TTL_DAYS", 180),
    ):
        asyncio.run(scheduled_postings_prune(MagicMock()))

    assert len(_rows(seen_db)) == 2


def test_scheduled_prune_failure_is_swallowed_and_counted(seen_db) -> None:
    with (
        patch("hunter.config.POSTINGS_SEEN_ENABLED", True),
        patch("hunter.postings_seen.prune", side_effect=RuntimeError("db on fire")),
    ):
        asyncio.run(scheduled_postings_prune(MagicMock()))  # must not raise

    assert _failures(seen_db, "postings.prune") == 1


# ── config: a non-integer TTL falls back with a warning, never crashes ───────


def test_env_int_falls_back_on_garbage(monkeypatch, caplog) -> None:
    from hunter.config import _env_int

    monkeypatch.setenv("POSTINGS_TTL_DAYS_TEST", "ninety")
    with caplog.at_level("WARNING", logger="hunter.config"):
        assert _env_int("POSTINGS_TTL_DAYS_TEST", 180) == 180
    assert "not an integer" in caplog.text
    monkeypatch.setenv("POSTINGS_TTL_DAYS_TEST", " 90 ")
    assert _env_int("POSTINGS_TTL_DAYS_TEST", 180) == 90
