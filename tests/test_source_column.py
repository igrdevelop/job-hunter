"""docs/MARKET_MEMORY_PLAN.md M3 — every tracker INSERT stamps `source`.

One assertion per writer (add_pending / add_skipped / add_failed /
add_react_skipped / add_expired / add_manual_jobleads_pending / add_applied),
the `_source_for_write` resolution rules (a real source name wins; a synthetic
apply-pipeline marker such as `doomed_gate` falls through to the postings_seen
row; no postings_seen table → '' and the write still succeeds), the migration
onto a pre-M3 database, and the funnel preferring the stored column over its
URL guess. The column is reports-only: nothing gates on it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hunter import postings_seen, tracker
from hunter.db import get_db, init_db
from hunter.models import Job
from hunter.tracker import (
    _source_for_write,
    add_applied,
    add_expired,
    add_failed,
    add_manual_jobleads_pending,
    add_pending,
    add_react_skipped,
    add_skipped,
)

T0 = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc)
GH = "https://boards.greenhouse.io/acme/jobs/4242"


def _job(url: str, source: str, company: str = "Acme", title: str = "Senior Angular Dev") -> Job:
    return Job(title=title, company=company, location="Remote", salary=None, url=url, source=source)


def _stored_source(url: str) -> str | None:
    with get_db(tracker.DB_PATH) as conn:
        row = conn.execute(
            "SELECT source FROM applications WHERE url_norm=?", (tracker.normalize_url(url),)
        ).fetchone()
    return row["source"] if row else None


@pytest.fixture()
def seen_db(tracker_db: Path, monkeypatch) -> Path:
    """postings_seen shares the tracker's tmp DB — the prod layout (one
    tracker.db holding both tables) is what _source_for_write's join relies on."""
    monkeypatch.setattr(postings_seen, "DB_PATH", tracker_db)
    return tracker_db


# ── _source_for_write ────────────────────────────────────────────────────────


def test_real_source_name_wins_without_any_lookup(tracker_db):
    assert _source_for_write("https://justjoin.it/o/x", "justjoin") == "justjoin"
    assert _source_for_write("https://x/y", "gmail") == "gmail"  # toggle-independent roster


def test_gmail_prefixed_source_is_real_without_any_lookup(tracker_db):
    # hunter/gmail_parsers.py stamps ``gmail_<aggregator>``; hunter/main.py keys
    # its Gmail bookkeeping on the same prefix. No postings_seen row needed.
    assert tracker._source_for_write("https://example.com/x", "gmail_linkedin") == "gmail_linkedin"


def test_synthetic_marker_falls_through_to_postings_seen(seen_db):
    """hunter/pipeline/gates.py builds Job(source='doomed_gate') — that names
    the WRITER, not the board. The postings_seen row knows the real one."""
    postings_seen.record_listings([(_job(GH, "justjoin"), "passed")], now=T0)
    assert _source_for_write(GH, "doomed_gate") == "justjoin"
    assert _source_for_write(GH, "post_generation_abort") == "justjoin"
    assert _source_for_write(GH) == "justjoin"


def test_unknown_url_and_no_table_resolve_to_blank(tracker_db):
    """A fresh DB has no postings_seen table (M1 creates it lazily) — the
    helper must return '' rather than let 'no such table' reach a writer."""
    with get_db(tracker_db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "postings_seen" not in tables
    assert _source_for_write(GH, "doomed_gate") == ""
    assert _source_for_write(GH) == ""
    assert _source_for_write("", "doomed_gate") == ""


def test_table_present_but_url_unseen_resolves_to_blank(seen_db):
    postings_seen.record_listings([(_job("https://justjoin.it/o/other", "justjoin"), "passed")])
    assert _source_for_write(GH, "dedup_ct_gate") == ""


# ── writers ──────────────────────────────────────────────────────────────────


def test_add_pending_stamps_job_source(tracker_db):
    add_pending(_job("https://nofluffjobs.com/job/a", "nofluffjobs"))
    assert _stored_source("https://nofluffjobs.com/job/a") == "nofluffjobs"


def test_add_skipped_stamps_job_source(tracker_db):
    add_skipped(_job("https://justjoin.it/o/b", "justjoin"), reason="button")
    assert _stored_source("https://justjoin.it/o/b") == "justjoin"


def test_add_skipped_with_synthetic_source_uses_postings_seen(seen_db):
    postings_seen.record_listings([(_job(GH, "telegram_channels"), "passed")], now=T0)
    add_skipped(_job(GH, "doomed_gate"), reason="doomed:foreign_onsite_hybrid")
    assert _stored_source(GH) == "telegram_channels"


def test_add_skipped_with_synthetic_source_and_no_table_still_writes(tracker_db):
    add_skipped(_job(GH, "doomed_gate"), reason="doomed:rule")
    assert _stored_source(GH) == ""
    assert tracker.is_known(GH)


def test_add_failed_stamps_job_source(tracker_db):
    add_failed(_job("https://justjoin.it/o/c", "justjoin"))
    assert _stored_source("https://justjoin.it/o/c") == "justjoin"


def test_add_react_skipped_resolves_from_postings_seen(seen_db):
    postings_seen.record_listings([(_job(GH, "gmail"), "passed")], now=T0)
    add_react_skipped({"company_name": "Acme", "job_title": "React Dev", "stack": "React"}, GH)
    assert _stored_source(GH) == "gmail"


def test_add_expired_resolves_from_postings_seen(seen_db):
    postings_seen.record_listings([(_job(GH, "linkedin"), "passed")], now=T0)
    add_expired(GH, company="Acme", title="Dev")
    assert _stored_source(GH) == "linkedin"


def test_add_expired_without_a_seen_row_stays_blank(tracker_db):
    add_expired(GH, company="Acme", title="Dev")
    assert _stored_source(GH) == ""


def test_add_manual_jobleads_pending_resolves_from_postings_seen(seen_db, tmp_path):
    url = "https://www.jobleads.com/job/123"
    postings_seen.record_listings([(_job(url, "jobleads"), "passed")], now=T0)
    assert add_manual_jobleads_pending(
        url=url, company="Acme", title="Dev", folder_abs=tmp_path / "Acme"
    )
    assert _stored_source(url) == "jobleads"


def test_add_applied_picks_the_source_from_postings_seen(seen_db, tmp_path):
    postings_seen.record_listings([(_job(GH, "justjoin"), "passed")], now=T0)
    assert add_applied(
        {
            "company_name": "Acme",
            "job_title": "Senior Angular Dev",
            "apply_url": GH,
            "output_folder": str(tmp_path / "Acme"),
            "ats_score": "88%",
        }
    )
    assert _stored_source(GH) == "justjoin"


def test_add_applied_without_a_seen_row_stays_blank(tracker_db, tmp_path):
    assert add_applied(
        {"company_name": "Acme", "job_title": "Dev", "apply_url": GH, "ats_score": "88%"}
    )
    assert _stored_source(GH) == ""


def test_pending_placeholder_replaced_by_terminal_row_keeps_source(tracker_db):
    """Queue mode: add_pending writes the placeholder, the worker's terminal
    write replaces it — the terminal row recomputes the same value."""
    job = _job("https://justjoin.it/o/q", "justjoin")
    add_pending(job)
    add_failed(job)
    with get_db(tracker_db) as conn:
        rows = conn.execute(
            "SELECT ats_status, source FROM applications WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        ).fetchall()
    assert [(r["ats_status"], r["source"]) for r in rows] == [("FAIL", "justjoin")]


def test_fail_to_skip_conversion_keeps_the_original_source(seen_db):
    """_convert_own_fail_row is an UPDATE — the FAIL row's source survives
    even when postings_seen would now say something else."""
    job = _job(GH, "justjoin")
    add_failed(job)
    postings_seen.record_listings([(_job(GH, "gmail"), "passed")], now=T0)
    assert add_skipped(_job(GH, "doomed_gate"), reason="doomed:rule") is None  # converted
    with get_db(seen_db) as conn:
        row = conn.execute(
            "SELECT ats_status, source FROM applications WHERE url_norm=?",
            (tracker.normalize_url(GH),),
        ).fetchone()
    assert (row["ats_status"], row["source"]) == ("SKIP", "justjoin")


# ── migration ────────────────────────────────────────────────────────────────


def test_migration_adds_source_to_a_pre_m3_db(tmp_path: Path) -> None:
    """A tracker.db created before M3 gains the column on the next init_db;
    old rows read as '' (no backfill — owner decision 2026-09-12)."""
    import sqlite3

    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE applications (id TEXT PRIMARY KEY, date TEXT, company TEXT, title TEXT, "
        "stack TEXT, ats_status TEXT, url TEXT, url_norm TEXT, folder TEXT, sent TEXT, "
        "reapplication TEXT, to_learn TEXT, drive_url TEXT, confirmation TEXT, answer TEXT)"
    )
    conn.execute(
        "INSERT INTO applications (id, company, title, ats_status, url, url_norm, sent) "
        "VALUES ('abcd1234', 'Old', 'Row', '85%', 'https://x/1', 'https://x/1', '2026-08-01')"
    )
    conn.commit()
    conn.close()

    init_db(db, xlsx_path=tmp_path / "no_tracker.xlsx")

    with get_db(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
        assert "source" in cols
        old = conn.execute("SELECT source FROM applications WHERE id='abcd1234'").fetchone()
    assert old["source"] == "", "a pre-M3 row reads as blank, never NULL"


# ── funnel reads the column ──────────────────────────────────────────────────


def test_funnel_buckets_a_written_row_by_its_stored_source(seen_db, monkeypatch):
    """End to end: a Greenhouse link surfaced by justjoin, skipped by the
    doomed gate, lands under justjoin in /funnel — not ats_aggregator."""
    from hunter import funnel

    monkeypatch.setattr(funnel, "DB_PATH", seen_db)
    postings_seen.record_listings([(_job(GH, "justjoin"), "passed")], now=T0)
    add_skipped(_job(GH, "doomed_gate"), reason="doomed:rule")

    rep = funnel.compute_funnel()
    assert rep.by_source["justjoin"].tracked == 1
    assert "ats_aggregator" not in rep.by_source
