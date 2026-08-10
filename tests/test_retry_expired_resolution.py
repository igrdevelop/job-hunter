"""tests/test_retry_expired_resolution.py — retry loop vs dead postings.

Regression coverage for the 2026-08-10 retry-log incident: six dead postings
were reported as "✅ Retry OK" and their FAIL rows deleted without any
terminal marker left behind. Three bugs, three fix layers:

1. apply_api ordering — the synthetic 29-char expired marker ("This job
   posting has expired.") from findmyremote/Lever/thesmartjobs was swallowed
   by the too-short abort before the expired check ran (covered in
   test_golden_apply_e2e.py::test_golden_short_expired_marker_writes_expired).
2. tracker.add_expired / add_skipped — an existing FAIL row for the same URL
   made _is_known_terminal() silently drop the terminal write; now the FAIL
   row is converted in place (_convert_own_fail_row).
3. hunter.main._retry_failed — exit 0 was blindly treated as "applied";
   now classify_retry_outcome() inspects what the pipeline actually wrote.
"""

from __future__ import annotations

import asyncio
from datetime import date

from hunter.models import Job


def _job(url: str = "https://jobs.lever.co/jobgether/abc") -> Job:
    return Job(
        title="Senior Angular Developer",
        company="Jobgether",
        location="",
        salary=None,
        url=url,
        source="retry",
    )


def _rows_for(url: str):
    from hunter import tracker
    from hunter.db import get_db

    norm = tracker.normalize_url(url)
    with get_db(tracker.DB_PATH) as conn:
        return conn.execute(
            "SELECT id, ats_status, sent, sheets_dirty FROM applications WHERE url_norm=?",
            (norm,),
        ).fetchall()


def _insert_applied_row(url: str) -> None:
    from hunter import tracker
    from hunter.db import get_db

    norm = tracker.normalize_url(url)
    with get_db(tracker.DB_PATH) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, user_id, company, title, ats_status, url, url_norm)"
            " VALUES ('aaaa1111', ?, '', 'Jobgether', 'Senior Angular Developer', '85%', ?, ?)",
            (date.today().strftime("%Y-%m-%d"), norm, norm),
        )


# ── Marker drift guard ───────────────────────────────────────────────────────


def test_synthetic_expired_markers_are_recognized_as_expired():
    """Every fetcher's synthetic deleted-posting marker must trip
    is_job_expired — the whole clean-$0-EXPIRED design rests on it."""
    from hunter.expired_check import is_job_expired
    from hunter.sources.ats_aggregator import _LEVER_EXPIRED_TEXT
    from hunter.sources.findmyremote import _EXPIRED_TEXT as _FMR_EXPIRED
    from hunter.sources.thesmartjobs import _EXPIRED_TEXT as _TSJ_EXPIRED

    for marker in (_LEVER_EXPIRED_TEXT, _FMR_EXPIRED, _TSJ_EXPIRED):
        assert is_job_expired(marker), f"marker not recognized: {marker!r}"


# ── tracker: FAIL-row conversion ─────────────────────────────────────────────


def test_add_expired_converts_own_fail_row(tracker_db):
    from hunter import tracker

    job = _job()
    tracker.add_failed(job)
    (before,) = _rows_for(job.url)
    assert before["ats_status"] == "FAIL"

    tracker.add_expired(job.url)

    (after,) = _rows_for(job.url)  # still exactly one row — converted, not appended
    assert after["id"] == before["id"], "conversion must keep the row id (Sheets sync key)"
    assert after["ats_status"] == "SKIP"
    assert after["sent"] == "EXPIRED"
    assert after["sheets_dirty"] == 1, "resync must push the new status to the mirrored row"


def test_add_expired_still_inserts_fresh_row_without_fail(tracker_db):
    from hunter import tracker

    url = "https://example.com/jobs/gone"
    tracker.add_expired(url)

    (row,) = _rows_for(url)
    assert row["ats_status"] == "SKIP"
    assert row["sent"] == "EXPIRED"


def test_add_expired_noop_when_applied_row_exists(tracker_db):
    from hunter import tracker

    url = "https://jobs.lever.co/jobgether/abc"
    _insert_applied_row(url)

    tracker.add_expired(url)

    (row,) = _rows_for(url)
    assert row["ats_status"] == "85%", "an applied row must never be overwritten"


def test_add_skipped_converts_own_fail_row(tracker_db):
    from hunter import tracker

    job = _job()
    tracker.add_failed(job)

    assert tracker.add_skipped(job) is None  # converted in place, not appended

    (row,) = _rows_for(job.url)
    assert row["ats_status"] == "SKIP"
    assert row["sent"] == "—"
    assert row["sheets_dirty"] == 1


# ── tracker: classify_retry_outcome ──────────────────────────────────────────


def test_classify_retry_outcome_noop_on_untouched_fail_row(tracker_db):
    from hunter import tracker

    job = _job()
    tracker.add_failed(job)
    assert tracker.classify_retry_outcome(job.url) == "noop"


def test_classify_retry_outcome_expired(tracker_db):
    from hunter import tracker

    job = _job()
    tracker.add_failed(job)
    tracker.add_expired(job.url)
    assert tracker.classify_retry_outcome(job.url) == "expired"


def test_classify_retry_outcome_skipped(tracker_db):
    from hunter import tracker

    job = _job()
    tracker.add_failed(job)
    tracker.add_skipped(job)
    assert tracker.classify_retry_outcome(job.url) == "skipped"


def test_classify_retry_outcome_applied(tracker_db):
    from hunter import tracker

    job = _job()
    _insert_applied_row(job.url)
    assert tracker.classify_retry_outcome(job.url) == "applied"


def test_add_applied_replaces_own_fail_row(tracker_db):
    """A successful retry generation must be able to write its applied row
    while the FAIL row still exists: the unique (user_id, url_norm) index (B1)
    allows only one row per URL, and the retry loop removes the FAIL row only
    AFTER the subprocess exits — the bare INSERT used to raise IntegrityError
    and crash generate_docs at the tracker write."""
    from hunter import tracker

    job = _job()
    tracker.add_failed(job)

    content = {
        "company_name": "Jobgether",
        "job_title": "Senior Angular Developer",
        "stack": "Angular",
        "apply_url": job.url,
        "output_folder": "Applications/2026-08-10/Jobgether",
        "ats_score": "85%",
    }
    assert tracker.add_applied(content) is True

    (row,) = _rows_for(job.url)
    assert row["ats_status"].strip() == "85%"
    assert tracker.classify_retry_outcome(job.url) == "applied"


# ── main._retry_failed: exit 0 is no longer blindly "Retry OK" ───────────────


def _run_retry_with(monkeypatch, *, resolution: str):
    """Run _retry_failed with one FAIL job whose subprocess exits 0, and the
    given classify_retry_outcome resolution. Returns (messages, calls)."""
    import hunter.main as main_mod

    job = _job()
    messages: list[str] = []
    calls: dict[str, list] = {"remove_failed": [], "deliver": [], "increment": []}

    async def fake_send_text(_context, text, **_kw):
        messages.append(text)

    async def fake_run_apply_agent(_job):
        return "ok"

    async def fake_deliver(url):
        calls["deliver"].append(url)

    def fake_increment(url):
        calls["increment"].append(url)
        return 1

    monkeypatch.setattr(main_mod, "get_failed_jobs", lambda: [job])
    monkeypatch.setattr(main_mod, "send_text", fake_send_text)
    monkeypatch.setattr(main_mod, "_run_apply_agent", fake_run_apply_agent)
    monkeypatch.setattr(main_mod, "_deliver_now", fake_deliver)
    monkeypatch.setattr(main_mod, "classify_retry_outcome", lambda url: resolution)
    monkeypatch.setattr(main_mod, "remove_failed", lambda url: calls["remove_failed"].append(url))
    monkeypatch.setattr(main_mod, "increment_fail_count", fake_increment)
    monkeypatch.setattr(main_mod, "APPLY_DELAY_SEC", 0)

    asyncio.run(main_mod._retry_failed(context=None))
    return messages, calls


def test_retry_applied_keeps_old_behavior(monkeypatch):
    messages, calls = _run_retry_with(monkeypatch, resolution="applied")
    assert calls["remove_failed"], "a real apply must still remove the FAIL row"
    assert calls["deliver"], "a real apply must still trigger instant delivery"
    assert any("Retry OK" in m for m in messages)


def test_retry_expired_does_not_claim_ok(monkeypatch):
    messages, calls = _run_retry_with(monkeypatch, resolution="expired")
    assert not calls["remove_failed"], "converted row must not be deleted"
    assert not calls["deliver"]
    assert not any("Retry OK" in m for m in messages)
    assert any("EXPIRED" in m for m in messages)
    # Summary must not count it as fixed or still-failing
    assert any("✅ 0 fixed" in m and "⏭ 1 expired/skipped" in m for m in messages)


def test_retry_noop_escalates_fail_count(monkeypatch):
    messages, calls = _run_retry_with(monkeypatch, resolution="noop")
    assert calls["increment"], "a no-write exit 0 must escalate fail_count"
    assert not calls["remove_failed"]
    assert not any("Retry OK" in m for m in messages)
    assert any("skipped without result" in m for m in messages)


def test_retry_summary_counts_skipped(monkeypatch):
    messages, _calls = _run_retry_with(monkeypatch, resolution="skipped")
    assert any("⏭ 1 expired/skipped" in m and "❌ 0 still failing" in m for m in messages)
