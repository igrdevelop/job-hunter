"""M1 (docs/HUNT_APPLY_SPLIT_PLAN.md) — PENDING queue tracker functions.

Covers: add_pending, job_from_pending_row, claim_pending, release_claim,
reset_stale_claims, count_pending/count_in_progress/list_pending, and the
cross-cutting guard: a PENDING/IN_PROGRESS placeholder must not block (and
must be replaced in place by) the pipeline's own terminal write for that
same URL (add_applied/add_failed/add_skipped/add_expired/
add_manual_jobleads_pending).
"""

from pathlib import Path

from hunter.models import Job


def _job(n: int, **kwargs) -> Job:
    defaults = {
        "title": f"Role {n}",
        "company": f"Co{n}",
        "location": "Remote",
        "salary": "15000 PLN",
        "url": f"https://example.com/job/{n}",
        "source": "test",
    }
    defaults.update(kwargs)
    return Job(**defaults)


# ── add_pending / job_from_pending_row ────────────────────────────────────────


def test_add_pending_writes_pending_row(tracker_db):
    from hunter import tracker

    job = _job(1)
    row_id = tracker.add_pending(job)
    assert row_id

    with tracker.get_db(tracker.DB_PATH) as conn:
        row = conn.execute("SELECT * FROM applications WHERE id=?", (row_id,)).fetchone()
    assert row["ats_status"] == "PENDING"
    assert row["company"] == "Co1"
    assert row["title"] == "Role 1"
    assert row["claimed_at"] is None
    assert row["pending_meta"]


def test_add_pending_makes_job_known(tracker_db):
    from hunter import tracker

    job = _job(2)
    tracker.add_pending(job)
    assert tracker.is_known(job.url, job.company, job.title) is True


def test_job_from_pending_row_roundtrips_raw_and_email_meta(tracker_db):
    from hunter import tracker

    job = _job(3, raw={"permalink": "https://real.example.com/p/3", "post_text": "hello"})
    row_id = tracker.add_pending(job)
    with tracker.get_db(tracker.DB_PATH) as conn:
        row = dict(conn.execute("SELECT * FROM applications WHERE id=?", (row_id,)).fetchone())

    rebuilt = tracker.job_from_pending_row(row)
    assert rebuilt.title == "Role 3"
    assert rebuilt.company == "Co3"
    assert rebuilt.url == job.url
    assert rebuilt.raw["permalink"] == "https://real.example.com/p/3"
    assert rebuilt.raw["post_text"] == "hello"


def test_job_from_pending_row_defensive_on_bad_json(tracker_db):
    from hunter import tracker

    row = {
        "pending_meta": "not json",
        "title": "Fallback Title",
        "company": "Fallback Co",
        "url": "https://example.com/x",
    }
    rebuilt = tracker.job_from_pending_row(row)
    assert rebuilt.title == "Fallback Title"
    assert rebuilt.company == "Fallback Co"
    assert rebuilt.url == "https://example.com/x"


# ── claim_pending ──────────────────────────────────────────────────────────────


def test_claim_pending_empty_queue_returns_none(tracker_db):
    from hunter import tracker

    assert tracker.claim_pending() is None


def test_claim_pending_returns_oldest_and_flips_status(tracker_db):
    from hunter import tracker

    j1 = _job(1)
    j2 = _job(2)
    tracker.add_pending(j1)
    tracker.add_pending(j2)

    claimed = tracker.claim_pending()
    assert claimed is not None
    assert claimed["url"] == j1.url
    assert claimed["ats_status"] == "IN_PROGRESS"
    assert claimed["claimed_at"]

    # Still known as in-progress; second claim gets the other job.
    assert tracker.count_pending() == 1
    assert tracker.count_in_progress() == 1
    claimed2 = tracker.claim_pending()
    assert claimed2["url"] == j2.url


def test_claim_pending_drains_fully(tracker_db):
    from hunter import tracker

    for i in range(3):
        tracker.add_pending(_job(i))
    seen = []
    while (row := tracker.claim_pending()) is not None:
        seen.append(row["url"])
    assert len(seen) == 3
    assert tracker.claim_pending() is None


# ── release_claim ──────────────────────────────────────────────────────────────


def test_release_claim_puts_row_back_to_pending(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    claimed = tracker.claim_pending()
    assert claimed["ats_status"] == "IN_PROGRESS"

    tracker.release_claim(job.url)

    with tracker.get_db(tracker.DB_PATH) as conn:
        row = conn.execute(
            "SELECT ats_status, claimed_at FROM applications WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        ).fetchone()
    assert row["ats_status"] == "PENDING"
    assert row["claimed_at"] is None
    assert tracker.count_pending() == 1
    assert tracker.count_in_progress() == 0


def test_release_claim_noop_for_unknown_url(tracker_db):
    from hunter import tracker

    tracker.release_claim("https://example.com/does-not-exist")  # no raise


# ── reset_stale_claims ─────────────────────────────────────────────────────────


def test_reset_stale_claims_resets_old_claim(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    tracker.claim_pending()

    # Backdate claimed_at well past the timeout.
    with tracker.get_db(tracker.DB_PATH) as conn:
        conn.execute(
            "UPDATE applications SET claimed_at='2000-01-01T00:00:00Z' WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        )

    reset_count = tracker.reset_stale_claims(timeout_min=60)
    assert reset_count == 1
    assert tracker.count_pending() == 1
    assert tracker.count_in_progress() == 0


def test_reset_stale_claims_leaves_fresh_claim(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    tracker.claim_pending()  # claimed_at = now

    reset_count = tracker.reset_stale_claims(timeout_min=60)
    assert reset_count == 0
    assert tracker.count_in_progress() == 1


def test_reset_stale_claims_ignores_pending_rows(tracker_db):
    from hunter import tracker

    tracker.add_pending(_job(1))  # never claimed
    assert tracker.reset_stale_claims(timeout_min=0) == 0
    assert tracker.count_pending() == 1


# ── count_pending / count_in_progress / list_pending ───────────────────────────


def test_counts_and_list_pending(tracker_db):
    from hunter import tracker

    for i in range(3):
        tracker.add_pending(_job(i))
    assert tracker.count_pending() == 3
    assert tracker.count_in_progress() == 0

    tracker.claim_pending()
    assert tracker.count_pending() == 2
    assert tracker.count_in_progress() == 1

    listed = tracker.list_pending()
    assert len(listed) == 2
    assert all("company" in r and "url" in r for r in listed)


def test_list_pending_respects_limit(tracker_db):
    from hunter import tracker

    for i in range(5):
        tracker.add_pending(_job(i))
    assert len(tracker.list_pending(limit=2)) == 2


# ── delete_pending_row ─────────────────────────────────────────────────────────


def test_delete_pending_row(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    assert tracker.delete_pending_row(job.url) is True
    assert tracker.count_pending() == 0
    assert tracker.is_known(job.url) is False


def test_delete_pending_row_noop_when_absent(tracker_db):
    from hunter import tracker

    assert tracker.delete_pending_row("https://example.com/nope") is False


# ── Cross-cutting: terminal writes must supersede a PENDING/IN_PROGRESS row ───


def test_add_failed_replaces_in_progress_placeholder(tracker_db):
    """The apply worker's own IN_PROGRESS row must not block add_failed."""
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    tracker.claim_pending()  # -> IN_PROGRESS

    tracker.add_failed(job)

    with tracker.get_db(tracker.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ats_status FROM applications WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        ).fetchall()
    assert [r["ats_status"] for r in rows] == ["FAIL"]  # exactly one row, not two
    assert [j.url for j in tracker.get_failed_jobs()] == [job.url]


def test_add_skipped_replaces_pending_placeholder(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)

    result = tracker.add_skipped(job)
    assert result is not None

    with tracker.get_db(tracker.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ats_status FROM applications WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        ).fetchall()
    assert [r["ats_status"] for r in rows] == ["SKIP"]


def test_add_expired_replaces_in_progress_placeholder(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    tracker.claim_pending()

    tracker.add_expired(job.url, job.company, job.title)

    with tracker.get_db(tracker.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ats_status, sent FROM applications WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ats_status"] == "SKIP"
    assert rows[0]["sent"] == "EXPIRED"


def test_add_applied_replaces_in_progress_placeholder(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    tracker.claim_pending()

    content = {
        "company_name": job.company,
        "job_title": job.title,
        "stack": "Angular",
        "ats_score": "88",
        "apply_url": job.url,
        "output_folder": str(Path("/tmp/Applications/2026-07-30/Co1")),
        "to_learn": "",
    }
    written = tracker.add_applied(content)
    assert written is True

    with tracker.get_db(tracker.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ats_status, reapplication FROM applications WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        ).fetchall()
    assert len(rows) == 1  # placeholder replaced, not duplicated
    assert rows[0]["ats_status"] == "88%"
    # A fresh queued apply must NOT be flagged as a re-application just
    # because its own PENDING placeholder already existed.
    assert rows[0]["reapplication"] == ""


def test_add_manual_jobleads_pending_replaces_placeholder(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    tracker.claim_pending()

    written = tracker.add_manual_jobleads_pending(
        url=job.url,
        company=job.company,
        title=job.title,
        folder_abs=Path("/tmp/Applications/2026-07-30/Co1"),
    )
    assert written is True

    with tracker.get_db(tracker.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ats_status FROM applications WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        ).fetchall()
    assert [r["ats_status"] for r in rows] == ["MANUAL"]


def test_add_failed_still_blocks_on_genuine_existing_success(tracker_db):
    """A real terminal row (not a placeholder) must still block, same as before."""
    from hunter import tracker

    job = _job(1)
    content = {
        "company_name": job.company,
        "job_title": job.title,
        "apply_url": job.url,
        "ats_score": "90",
        "output_folder": "",
    }
    tracker.add_applied(content)

    tracker.add_failed(job)  # must be a no-op — already has a real success row

    with tracker.get_db(tracker.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT ats_status FROM applications WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        ).fetchall()
    assert [r["ats_status"] for r in rows] == ["90%"]


# ── _COOLDOWN_SKIP_STATUSES / is_in_cooldown ───────────────────────────────────


def test_pending_row_does_not_count_toward_cooldown(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    assert tracker.is_in_cooldown(job.company, job.title) is False


def test_in_progress_row_does_not_count_toward_cooldown(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    tracker.claim_pending()
    assert tracker.is_in_cooldown(job.company, job.title) is False
    assert tracker.company_cooldown_active(job.company) is False


# ── read_all_tracker_rows / iter_unsent_rows exclude PENDING/IN_PROGRESS ──────


def test_iter_unsent_rows_excludes_pending(tracker_db):
    from hunter import tracker

    tracker.add_pending(_job(1))
    assert tracker.iter_unsent_rows() == []


def test_iter_unsent_rows_excludes_in_progress(tracker_db):
    from hunter import tracker

    job = _job(1)
    tracker.add_pending(job)
    tracker.claim_pending()
    assert tracker.iter_unsent_rows() == []


def test_read_all_tracker_rows_excludes_pending_and_in_progress(tracker_db):
    from hunter import tracker

    tracker.add_pending(_job(1))
    j2 = _job(2)
    tracker.add_pending(j2)
    tracker.claim_pending()
    assert tracker.read_all_tracker_rows() == []
