"""docs/HUNT_DRILLDOWN_PLAN.md M1 — ``hunt_jobs``: one row per filter-passed
vacancy of a hunt, with its fate there, joinable to the hunt by ``hunt_id``.

Part 1 drives ``hunter.hunt_jobs`` on its own. Part 2 drives a REAL
``run_hunt`` through the harness of tests/test_hunt_runs.py and pins the
invariant the pipeline page relies on: the per-vacancy fates add up to the
counters of the same hunt's ``hunt_runs`` row, and both carry one hunt_id.
"""

from __future__ import annotations

from collections import Counter
from unittest.mock import patch

import pytest

from hunter import hunt_jobs, hunt_runs
from tests.test_hunt_runs import (
    _failures,
    _job,
    _mixed_jobs,
    _rows,
    _run,
    _seed_tracker,
    URL_DUP_CT,
    URL_DUP_URL,
    URL_NEW,
)

# ── Part 1: the module on its own ────────────────────────────────────────────


@pytest.fixture
def jobs_db(tmp_path, monkeypatch):
    db = tmp_path / "hunt_jobs_unit.db"
    monkeypatch.setattr(hunt_jobs, "DB_PATH", db)
    monkeypatch.setattr(hunt_runs, "DB_PATH", db)
    # The unit tests below write fixed September dates; keep the TTL out of
    # their way (test_rows_older_than_the_ttl_are_pruned sets its own).
    monkeypatch.setattr(hunt_jobs, "HUNT_JOBS_TTL_DAYS", 36500)
    return db


def _entry(url_norm: str, fate: str, **kw) -> dict:
    return {"url_norm": url_norm, "url": "https://" + url_norm, "fate": fate, **kw}


def test_record_and_read_round_trip(jobs_db) -> None:
    n = hunt_jobs.record_hunt_jobs(
        "h1",
        [
            _entry("a.test/1", "queued", title="Angular Dev", company="Acme", source="justjoin"),
            _entry("a.test/2", "dup_ct", fate_detail="tracker"),
        ],
        ts="2026-09-27T05:00:00+00:00",
    )
    assert n == 2
    rows = hunt_jobs.jobs_for_hunt("h1")
    assert [r["fate"] for r in rows] == ["queued", "dup_ct"]  # decision order kept
    assert rows[0]["title"] == "Angular Dev"
    assert rows[0]["company"] == "Acme"
    assert rows[0]["source"] == "justjoin"
    assert rows[1]["fate_detail"] == "tracker"
    assert rows[0]["ts"] == "2026-09-27T05:00:00+00:00"
    assert hunt_jobs.jobs_for_hunt("other") == []


def test_blank_hunt_id_writes_nothing(jobs_db) -> None:
    assert hunt_jobs.record_hunt_jobs("", [_entry("a.test/1", "queued")]) == 0
    assert hunt_jobs.jobs_for_hunt("") == []


def test_prune_follows_the_hunt_runs_ring(jobs_db, monkeypatch) -> None:
    monkeypatch.setattr(hunt_runs, "HUNT_RUNS_KEEP", 1)
    hunt_runs.record_hunt(
        trigger="scheduled", sources=["a"], hunt_id="old", ts="2026-09-01T00:00:00+00:00"
    )
    hunt_jobs.record_hunt_jobs(
        "old", [_entry("a.test/1", "queued")], ts="2026-09-01T00:00:00+00:00"
    )
    # The newer hunt pushes "old" out of the one-row ring...
    hunt_runs.record_hunt(
        trigger="scheduled", sources=["a"], hunt_id="new", ts="2026-09-02T00:00:00+00:00"
    )
    hunt_jobs.record_hunt_jobs(
        "new", [_entry("a.test/2", "queued")], ts="2026-09-02T00:00:00+00:00"
    )
    # ...and its vacancies go with it.
    assert hunt_jobs.jobs_for_hunt("old") == []
    assert len(hunt_jobs.jobs_for_hunt("new")) == 1


def test_prune_keeps_a_hunt_whose_hunt_runs_row_is_missing(jobs_db) -> None:
    """A failed hunt_runs write must not take the vacancy list with it."""
    hunt_runs.record_hunt(
        trigger="scheduled", sources=["a"], hunt_id="h0", ts="2026-09-01T00:00:00+00:00"
    )
    hunt_jobs.record_hunt_jobs(
        "orphan", [_entry("a.test/1", "queued")], ts="2026-09-02T00:00:00+00:00"
    )
    assert len(hunt_jobs.jobs_for_hunt("orphan")) == 1


def test_record_raises_on_broken_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hunt_jobs, "DB_PATH", tmp_path / "missing_dir" / "x.db")
    with pytest.raises(Exception):  # noqa: B017 — any DB error is the contract
        hunt_jobs.record_hunt_jobs("h1", [_entry("a.test/1", "queued")])


def test_hunt_runs_stores_hunt_id_and_per_source(jobs_db) -> None:
    hunt_runs.record_hunt(
        trigger="web",
        sources=["justjoin", "pracuj"],
        hunt_id="abc",
        per_source={"justjoin": 12, "pracuj": "ERR: 403 Forbidden"},
    )
    row = hunt_runs.recent_hunts(1)[0]
    assert row["hunt_id"] == "abc"
    assert row["per_source"] == {"justjoin": 12, "pracuj": "ERR"}


def test_hunt_runs_migrates_a_table_created_before_the_columns(tmp_path, monkeypatch) -> None:
    """Prod already has hunt_runs without hunt_id/per_source: ALTER, never fail."""
    from hunter.db import get_db

    db = tmp_path / "legacy.db"
    monkeypatch.setattr(hunt_runs, "DB_PATH", db)
    with get_db(db) as conn:
        conn.executescript(
            "CREATE TABLE hunt_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
            '"trigger" TEXT NOT NULL, sources TEXT NOT NULL, found INTEGER NOT NULL DEFAULT 0, '
            "filtered_out INTEGER NOT NULL DEFAULT 0, filter_reasons TEXT NOT NULL DEFAULT '{}', "
            "dup_url INTEGER NOT NULL DEFAULT 0, dup_ct INTEGER NOT NULL DEFAULT 0, "
            'dup_cooldown INTEGER NOT NULL DEFAULT 0, "new" INTEGER NOT NULL DEFAULT 0, '
            "capped INTEGER NOT NULL DEFAULT 0, queued INTEGER NOT NULL DEFAULT 0, "
            "applied_inline INTEGER NOT NULL DEFAULT 0, duration_ms INTEGER NOT NULL DEFAULT 0);"
            "INSERT INTO hunt_runs (ts, \"trigger\", sources) VALUES ('2026-09-01T00:00:00+00:00', 'scheduled', '[]');"
        )
    hunt_runs.record_hunt(trigger="scheduled", sources=["a"], hunt_id="h2")
    rows = hunt_runs.recent_hunts(5)
    assert [r["hunt_id"] for r in rows] == ["h2", ""]
    assert rows[1]["per_source"] == {}


# ── Part 2: the hunt loop writes the list behind the counts ──────────────────


@pytest.fixture
def loop_db(tracker_db, monkeypatch):
    import hunter.postings_seen as ps
    import hunter.source_health as sh
    from hunter import best_effort as be

    for mod in (hunt_runs, hunt_jobs, be, ps, sh):
        monkeypatch.setattr(mod, "DB_PATH", tracker_db)
    return tracker_db


def _only_hunt() -> tuple[dict, list[dict]]:
    runs = hunt_runs.recent_hunts(5)
    assert len(runs) == 1
    run = runs[0]
    assert run["hunt_id"], "the hunt_runs row carries the hunt_live id"
    return run, hunt_jobs.jobs_for_hunt(run["hunt_id"])


def _assert_fates_match_counters(run: dict, jobs: list[dict]) -> None:
    fates = Counter(j["fate"] for j in jobs)
    assert fates["dup_url"] == run["dup_url"]
    assert fates["dup_ct"] == run["dup_ct"]
    assert fates["dup_cooldown"] == run["dup_cooldown"]
    assert sum(n for f, n in fates.items() if f not in hunt_jobs.DUP_FATES) == run["new"]
    assert fates["queued"] == run["queued"]
    assert fates["capped"] == run["capped"]
    assert fates["applied_inline"] == run["applied_inline"]


def test_manual_mode_hunt_lists_dups_and_cards(loop_db) -> None:
    jobs = _mixed_jobs()
    _seed_tracker(jobs)
    _run(jobs, sent=[])

    run, rows = _only_hunt()
    _assert_fates_match_counters(run, rows)
    by_url = {r["url"]: r for r in rows}
    # The filtered-out listing is a count only, never a row.
    assert set(by_url) == {URL_NEW, URL_DUP_URL, URL_DUP_CT}
    assert by_url[URL_NEW]["fate"] == "card"  # AUTO_APPLY off -> Telegram card
    assert by_url[URL_DUP_URL]["fate"] == "dup_url"
    assert by_url[URL_DUP_URL]["fate_detail"] == "tracker"
    assert by_url[URL_DUP_CT]["fate"] == "dup_ct"
    assert by_url[URL_NEW]["company"] == "Acme"
    assert by_url[URL_NEW]["source"] == "justjoin"
    assert by_url[URL_NEW]["url_norm"]
    assert run["per_source"] == {"justjoin": 4}


def test_queue_path_lists_queued_and_capped(loop_db) -> None:
    jobs = [
        _job("Senior Angular Developer", f"Co{i}", f"https://justjoin.it/job-offer/q{i}")
        for i in range(3)
    ]
    with patch("hunter.main.MAX_JOBS_PER_RUN", 2):
        _run(
            jobs,
            sent=[],
            **{"hunter.main.AUTO_APPLY": True, "hunter.main.APPLY_QUEUE_ENABLED": True},
        )

    run, rows = _only_hunt()
    _assert_fates_match_counters(run, rows)
    assert [r["fate"] for r in rows] == ["queued", "queued", "capped"]


def test_inline_path_writes_the_list_before_the_batch(loop_db) -> None:
    jobs = [
        _job("Senior Angular Developer", f"Co{i}", f"https://justjoin.it/job-offer/i{i}")
        for i in range(2)
    ]
    seen: list[list[str]] = []

    async def fake_batch(_ctx, _batch):
        run = hunt_runs.recent_hunts(1)[0]
        seen.append([r["fate"] for r in hunt_jobs.jobs_for_hunt(run["hunt_id"])])

    _run(jobs, sent=[], batch=fake_batch, **{"hunter.main.AUTO_APPLY": True})

    assert seen == [["applied_inline", "applied_inline"]]
    run, rows = _only_hunt()
    assert len(rows) == 2  # the finally-flush did not write a second copy
    _assert_fates_match_counters(run, rows)


def test_same_url_twice_in_one_hunt_is_a_same_hunt_dup(loop_db) -> None:
    jobs = [
        _job("Senior Angular Developer", "Acme", URL_NEW),
        _job("Senior Angular Developer", "Acme", URL_NEW),
    ]
    _run(jobs, sent=[])
    run, rows = _only_hunt()
    _assert_fates_match_counters(run, rows)
    assert [(r["fate"], r["fate_detail"]) for r in rows] == [("card", ""), ("dup_url", "same hunt")]


def test_flag_off_writes_no_list(loop_db) -> None:
    jobs = _mixed_jobs()
    _seed_tracker(jobs)
    with patch("hunter.main.record_hunt_jobs", side_effect=AssertionError("must not be called")):
        _run(jobs, sent=[], **{"hunter.main.HUNT_JOBS_ENABLED": False})
    assert len(_rows(loop_db)) == 1  # hunt_runs still written


def test_list_failure_is_swallowed_and_counted(loop_db) -> None:
    sent: list[str] = []
    with patch("hunter.main.record_hunt_jobs", side_effect=RuntimeError("db on fire")):
        _run(_mixed_jobs(), sent=sent)  # must not raise
    assert any(t.startswith("🔍 <b>Hunt ") for t in sent)
    assert len(_rows(loop_db)) == 1  # the counts row is independent
    assert _failures(loop_db, "hunt.jobs") == 1


def test_rows_older_than_the_ttl_are_pruned(jobs_db, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(hunt_jobs, "HUNT_JOBS_TTL_DAYS", 30)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=31)).isoformat(timespec="seconds")
    recent = (now - timedelta(days=29)).isoformat(timespec="seconds")
    # No hunt_runs table: only the TTL rule can delete here.
    hunt_jobs.record_hunt_jobs("h_old", [_entry("a.test/1", "queued")], ts=old)
    hunt_jobs.record_hunt_jobs("h_recent", [_entry("a.test/2", "queued")], ts=recent)
    assert hunt_jobs.jobs_for_hunt("h_old") == []
    assert len(hunt_jobs.jobs_for_hunt("h_recent")) == 1
