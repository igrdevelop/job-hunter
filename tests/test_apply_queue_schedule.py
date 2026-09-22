"""M1 (docs/HUNT_APPLY_SPLIT_PLAN.md) — scheduled_reset_stale_claims."""

from __future__ import annotations

import asyncio

from hunter.schedules.apply_queue import scheduled_reset_stale_claims


def test_noop_when_queue_disabled(tracker_db, monkeypatch):
    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", False)
    called = {"n": 0}
    monkeypatch.setattr(
        "hunter.tracker.reset_stale_claims",
        lambda *_a, **_kw: called.__setitem__("n", called["n"] + 1),
    )
    asyncio.run(scheduled_reset_stale_claims(None))
    assert called["n"] == 0


def test_resets_stale_claims_when_enabled(tracker_db, monkeypatch):
    from hunter import tracker

    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    monkeypatch.setattr("hunter.config.APPLY_CLAIM_TIMEOUT_MIN", 60)

    from hunter.models import Job

    job = Job(
        title="Dev",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://example.com/job/1",
        source="test",
    )
    tracker.add_pending(job)
    row = tracker.claim_pending()
    # Backdate the claim so the sweep treats it as stale.
    with tracker.get_db(tracker.DB_PATH) as conn:
        conn.execute(
            "UPDATE applications SET claimed_at='2000-01-01T00:00:00Z' WHERE id=?",
            (row["id"],),
        )

    asyncio.run(scheduled_reset_stale_claims(None))

    rows = tracker.lookup_url(job.url)
    assert rows[0]["ats"] == "PENDING"


def test_registered_in_schedules_package():
    from hunter import schedules

    assert callable(schedules.scheduled_reset_stale_claims)


# ── orphan generation_runs sweep (docs/PIPELINE_VIZ_PLAN.md M1, 2026-09-22) ──


def _open_run_backdated(started_at: str) -> str:
    import sqlite3

    from hunter import metrics

    run_id = metrics.start_run(pipeline="api", url_norm="")
    conn = sqlite3.connect(str(metrics.DB_PATH))
    conn.execute("UPDATE generation_runs SET started_at = ? WHERE run_id = ?", (started_at, run_id))
    conn.commit()
    conn.close()
    return run_id


def _outcome(run_id: str):
    import sqlite3

    from hunter import metrics

    conn = sqlite3.connect(str(metrics.DB_PATH))
    row = conn.execute("SELECT outcome FROM generation_runs WHERE run_id = ?", (run_id,)).fetchone()
    conn.close()
    return row[0]


def test_orphan_run_sweep_runs_even_when_queue_disabled(tracker_db, monkeypatch):
    """The inline hunt path leaks open runs too — the sweep must not sit under
    the queue flag (the claim sweep still does)."""
    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", False)
    monkeypatch.setattr("hunter.config.APPLY_AGENT_CLI_TIMEOUT_SEC", 3 * 3600)
    stale = _open_run_backdated("2000-01-01T00:00:00+00:00")
    fresh = _open_run_backdated("2999-01-01T00:00:00+00:00")

    asyncio.run(scheduled_reset_stale_claims(None))

    assert _outcome(stale) == "orphan:stale"
    assert _outcome(fresh) is None


def test_orphan_run_sweep_uses_the_cli_timeout_as_cutoff(tracker_db, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", True)
    monkeypatch.setattr("hunter.config.APPLY_AGENT_CLI_TIMEOUT_SEC", 3 * 3600)
    two_h_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    four_h_ago = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat(timespec="seconds")
    in_progress = _open_run_backdated(two_h_ago)
    dead = _open_run_backdated(four_h_ago)

    asyncio.run(scheduled_reset_stale_claims(None))

    assert _outcome(dead) == "orphan:stale"
    assert _outcome(in_progress) is None  # a 2 h CLI run is still legitimately running


def test_orphan_run_sweep_failure_does_not_break_the_tick(tracker_db, monkeypatch):
    monkeypatch.setattr("hunter.config.APPLY_QUEUE_ENABLED", False)

    def _boom(*_a, **_kw):
        raise RuntimeError("metrics on fire")

    monkeypatch.setattr("hunter.metrics.reset_stale_open_runs", _boom)
    asyncio.run(scheduled_reset_stale_claims(None))  # must not raise


def test_register_wires_the_sweep_regardless_of_queue_flag(monkeypatch):
    from unittest.mock import MagicMock

    import pytz

    from hunter import schedules

    monkeypatch.setattr(schedules, "APPLY_QUEUE_ENABLED", False)
    app = MagicMock()
    schedules.register(app, pytz.timezone("Europe/Warsaw"))

    repeating = {
        c.kwargs.get("name"): c.kwargs.get("interval")
        for c in app.job_queue.run_repeating.call_args_list
    }
    assert repeating.get("reset_stale_claims") == 900
