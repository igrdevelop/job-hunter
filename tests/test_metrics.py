"""Tests for hunter/metrics.py — the generation_runs / pipeline_events log.

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M1.
"""

from __future__ import annotations

import sqlite3

import pytest

from hunter import metrics


@pytest.fixture()
def metrics_db(tmp_path, monkeypatch):
    """Isolate hunter.metrics on a fresh temp DB (mirrors test_source_health.py /
    test_best_effort.py's own DB_PATH monkeypatch pattern)."""
    db = tmp_path / "metrics.db"
    monkeypatch.setattr(metrics, "DB_PATH", db)
    return db


def _fetch_run(db, run_id) -> dict | None:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM generation_runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _fetch_events(db, run_id) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM pipeline_events WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── lazy table creation ─────────────────────────────────────────────────────


def test_tables_created_lazily_on_a_bare_db(metrics_db):
    """metrics.py must not depend on init_db() having run first — mirrors
    hunter.best_effort's own bare-DB test."""
    assert not metrics_db.exists()
    run_id = metrics.start_run(pipeline="api")
    assert metrics_db.exists()

    conn = sqlite3.connect(str(metrics_db))
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    assert "generation_runs" in names
    assert "pipeline_events" in names
    assert run_id  # a usable id was returned


# ── start_run / update_run / finish_run round trip ──────────────────────────


def test_start_run_always_returns_a_usable_run_id(metrics_db):
    run_id = metrics.start_run(
        user_id="u1",
        url_norm="example.com/jobs/1",
        pipeline="api",
        profile="sonnet",
        gen_model="claude-sonnet-4-6",
        judge_model="claude-haiku-4-5",
        source="justjoin",
        is_manual=True,
        is_force=False,
    )
    assert isinstance(run_id, str) and run_id

    row = _fetch_run(metrics_db, run_id)
    assert row is not None
    assert row["user_id"] == "u1"
    assert row["url_norm"] == "example.com/jobs/1"
    assert row["pipeline"] == "api"
    assert row["profile"] == "sonnet"
    assert row["gen_model"] == "claude-sonnet-4-6"
    assert row["judge_model"] == "claude-haiku-4-5"
    assert row["source"] == "justjoin"
    assert row["is_manual"] == 1
    assert row["is_force"] == 0
    assert row["started_at"]
    assert row["finished_at"] is None
    assert row["outcome"] is None


def test_update_run_merges_fields(metrics_db):
    run_id = metrics.start_run(pipeline="api")
    metrics.update_run(run_id, track="angular", posting_lang="EN")
    metrics.update_run(run_id, ats_pre_score=91.5, ats_pre_keyword=88.0)

    row = _fetch_run(metrics_db, run_id)
    assert row["track"] == "angular"
    assert row["posting_lang"] == "EN"
    assert row["ats_pre_score"] == 91.5
    assert row["ats_pre_keyword"] == 88.0


def test_update_run_rejects_unknown_field(metrics_db):
    run_id = metrics.start_run(pipeline="api")
    with pytest.raises(ValueError):
        metrics.update_run(run_id, not_a_real_column="oops")


def test_update_run_noop_on_empty_run_id_or_fields(metrics_db):
    # No exception, no table required to exist yet.
    metrics.update_run(None, track="angular")
    metrics.update_run("", track="angular")
    run_id = metrics.start_run(pipeline="api")
    metrics.update_run(run_id)  # no fields — no-op
    row = _fetch_run(metrics_db, run_id)
    assert row["track"] == ""


def test_finish_run_stamps_outcome_exit_code_and_finished_at(metrics_db):
    run_id = metrics.start_run(pipeline="api")
    metrics.finish_run(run_id, outcome="ok", exit_code=0, cost_usd=0.42, row_id="abc123")

    row = _fetch_run(metrics_db, run_id)
    assert row["outcome"] == "ok"
    assert row["exit_code"] == 0
    assert row["cost_usd"] == 0.42
    assert row["row_id"] == "abc123"
    assert row["finished_at"]


# ── stage / pipeline_events ─────────────────────────────────────────────────


def test_stage_appends_an_event(metrics_db):
    run_id = metrics.start_run(pipeline="api")
    metrics.stage(run_id, "fetch", "ok", duration_ms=120, payload={"chars": 4000})
    metrics.stage(run_id, "judge", "blocked")

    events = _fetch_events(metrics_db, run_id)
    assert len(events) == 2
    assert events[0]["stage"] == "fetch"
    assert events[0]["event"] == "ok"
    assert events[0]["duration_ms"] == 120
    assert "4000" in events[0]["payload"]
    assert events[1]["stage"] == "judge"
    assert events[1]["event"] == "blocked"


def test_stage_noop_on_empty_run_id(metrics_db):
    metrics.stage(None, "fetch", "ok")  # must not raise, must not create the table
    metrics.stage("", "fetch", "ok")


def test_stage_survives_an_unserializable_payload(metrics_db):
    run_id = metrics.start_run(pipeline="api")

    class Weird:
        def __str__(self):
            return "weird-object"

    metrics.stage(run_id, "judge", "ok", payload={"thing": Weird()})
    events = _fetch_events(metrics_db, run_id)
    assert len(events) == 1
    assert "weird-object" in events[0]["payload"]


def test_timed_stage_records_ok_and_reraises_on_error(metrics_db):
    run_id = metrics.start_run(pipeline="api")

    with metrics.timed_stage(run_id, "render"):
        pass

    with pytest.raises(RuntimeError):
        with metrics.timed_stage(run_id, "render"):
            raise RuntimeError("boom")

    events = _fetch_events(metrics_db, run_id)
    assert [e["event"] for e in events] == ["ok", "error"]
    assert all(e["duration_ms"] is not None for e in events)


# ── best-effort contract: DB failures never propagate ───────────────────────


def test_start_run_swallows_db_failure_and_still_returns_an_id(metrics_db, monkeypatch):
    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(metrics, "get_db", _boom)
    run_id = metrics.start_run(pipeline="api")
    assert isinstance(run_id, str) and run_id
    assert not metrics_db.exists()  # the failed INSERT never created the file


def test_stage_swallows_db_failure(metrics_db, monkeypatch):
    run_id = metrics.start_run(pipeline="api")

    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(metrics, "get_db", _boom)
    metrics.stage(run_id, "fetch", "ok")  # must not raise


def test_update_run_swallows_db_failure(metrics_db, monkeypatch):
    run_id = metrics.start_run(pipeline="api")

    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(metrics, "get_db", _boom)
    metrics.update_run(run_id, track="angular")  # must not raise


def test_finish_run_swallows_db_failure(metrics_db, monkeypatch):
    run_id = metrics.start_run(pipeline="api")

    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(metrics, "get_db", _boom)
    metrics.finish_run(run_id, outcome="ok", exit_code=0)  # must not raise


# ── read: count_runs_since ───────────────────────────────────────────────────


def test_count_runs_since(metrics_db):
    metrics.start_run(pipeline="api")
    metrics.start_run(pipeline="cli")
    assert metrics.count_runs_since(days=7) == 2

    # A stale run started well outside the window must not be counted.
    stale_id = metrics.start_run(pipeline="api")
    metrics.update_run(stale_id, url_norm="stale")
    conn = sqlite3.connect(str(metrics_db))
    conn.execute(
        "UPDATE generation_runs SET started_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
        (stale_id,),
    )
    conn.commit()
    conn.close()
    assert metrics.count_runs_since(days=7) == 2


def test_count_runs_since_returns_zero_on_failure(tmp_path, monkeypatch):
    # A directory instead of a file makes sqlite3.connect raise.
    bad_path = tmp_path / "not_a_db_dir"
    bad_path.mkdir()
    monkeypatch.setattr(metrics, "DB_PATH", bad_path)
    assert metrics.count_runs_since() == 0
