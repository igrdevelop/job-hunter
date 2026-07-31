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
