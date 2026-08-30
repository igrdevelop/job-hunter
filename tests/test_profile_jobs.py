"""tests/test_profile_jobs.py — hunter/profile_jobs.py's claim/finish/fail
primitives over the shared `profile_jobs` queue table.

docs/RESUME_PROFILE_STORE_PLAN.md step 4b. The table is written by
job-hunter-api (PUT /api/profile / POST /api/profile/uploads); this repo is
its only consumer, so these tests insert rows directly via SQL — exactly the
shape the API would insert (job-hunter-api/docs/RESUME_PROFILE_STORE.md).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from hunter import profile_jobs
from hunter.db import get_db


@pytest.fixture()
def jobs_db(tracker_db, monkeypatch):
    """tracker_db already ran init_db() (creates profile_jobs); point this
    module's own DB_PATH symbol at the same isolated file."""
    monkeypatch.setattr(profile_jobs, "DB_PATH", tracker_db)
    return tracker_db


def _insert_job(
    db,
    *,
    kind="render",
    user_id="u1",
    payload="{}",
    status="pending",
    created_at=None,
    updated_at=None,
) -> str:
    job_id = str(uuid.uuid4())
    created_at = created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO profile_jobs (id, user_id, kind, payload, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job_id, user_id, kind, payload, status, created_at, updated_at),
        )
    return job_id


def _row(db, job_id: str) -> dict:
    with get_db(db) as conn:
        row = conn.execute("SELECT * FROM profile_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row)


class TestClaimNextProfileJob:
    def test_returns_none_when_queue_empty(self, jobs_db):
        assert profile_jobs.claim_next_profile_job() is None

    def test_claims_oldest_pending_row_first(self, jobs_db):
        older = _insert_job(jobs_db, created_at="2026-01-01T00:00:00Z")
        _insert_job(jobs_db, created_at="2026-01-02T00:00:00Z")
        claimed = profile_jobs.claim_next_profile_job()
        assert claimed["id"] == older

    def test_claim_flips_status_to_running_and_stamps_updated_at(self, jobs_db):
        _insert_job(jobs_db)
        claimed = profile_jobs.claim_next_profile_job()
        assert claimed["status"] == "running"
        assert claimed["updated_at"]

    def test_claimed_row_is_not_returned_again(self, jobs_db):
        _insert_job(jobs_db)
        assert profile_jobs.claim_next_profile_job() is not None
        assert profile_jobs.claim_next_profile_job() is None

    def test_only_pending_rows_are_eligible(self, jobs_db):
        _insert_job(jobs_db, status="done")
        _insert_job(jobs_db, status="error")
        _insert_job(jobs_db, status="running")
        assert profile_jobs.claim_next_profile_job() is None

    def test_returns_full_row_including_payload(self, jobs_db):
        _insert_job(jobs_db, kind="parse", user_id="u42", payload="uploads/a.docx")
        claimed = profile_jobs.claim_next_profile_job()
        assert claimed["kind"] == "parse"
        assert claimed["user_id"] == "u42"
        assert claimed["payload"] == "uploads/a.docx"


class TestFinishAndFailProfileJob:
    def test_finish_marks_done_and_stores_result(self, jobs_db):
        job_id = _insert_job(jobs_db)
        profile_jobs.claim_next_profile_job()
        profile_jobs.finish_profile_job(job_id, '{"written": ["a.yaml"]}')
        row = _row(jobs_db, job_id)
        assert row["status"] == "done"
        assert row["result"] == '{"written": ["a.yaml"]}'

    def test_fail_marks_error_and_stores_message(self, jobs_db):
        job_id = _insert_job(jobs_db)
        profile_jobs.claim_next_profile_job()
        profile_jobs.fail_profile_job(job_id, "boom")
        row = _row(jobs_db, job_id)
        assert row["status"] == "error"
        assert row["error"] == "boom"

    def test_fail_truncates_very_long_error_messages(self, jobs_db):
        job_id = _insert_job(jobs_db)
        profile_jobs.fail_profile_job(job_id, "x" * 5000)
        row = _row(jobs_db, job_id)
        assert len(row["error"]) <= 2000


class TestResetStaleProfileJobs:
    def _stale_iso(self, minutes: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    def test_resets_running_rows_older_than_timeout(self, jobs_db):
        job_id = _insert_job(jobs_db, status="running", updated_at=self._stale_iso(20))
        assert profile_jobs.reset_stale_profile_jobs(10) == 1
        row = _row(jobs_db, job_id)
        assert row["status"] == "pending"
        assert row["updated_at"] is None

    def test_leaves_recently_claimed_running_rows_alone(self, jobs_db):
        job_id = _insert_job(jobs_db, status="running", updated_at=self._stale_iso(1))
        assert profile_jobs.reset_stale_profile_jobs(10) == 0
        assert _row(jobs_db, job_id)["status"] == "running"

    def test_leaves_pending_and_terminal_rows_alone(self, jobs_db):
        _insert_job(jobs_db, status="pending", updated_at=self._stale_iso(20))
        _insert_job(jobs_db, status="done", updated_at=self._stale_iso(20))
        _insert_job(jobs_db, status="error", updated_at=self._stale_iso(20))
        assert profile_jobs.reset_stale_profile_jobs(10) == 0

    def test_row_with_null_updated_at_is_left_alone(self, jobs_db):
        job_id = _insert_job(jobs_db, status="running", updated_at=None)
        assert profile_jobs.reset_stale_profile_jobs(10) == 0
        assert _row(jobs_db, job_id)["status"] == "running"
