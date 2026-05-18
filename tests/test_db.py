"""Tests for hunter/db.py — SQLite persistence layer."""

import hunter.db as db_module
from hunter.db import (
    delete_where,
    get_all_rows,
    get_by_ats,
    get_by_norm_url,
    get_known_ct_keys,
    get_known_norm_urls,
    get_unsent_rows,
    insert_job,
    is_empty,
    is_known,
    row_count,
    update_sent,
    update_user_fields,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row(
    *,
    id="abc12345",
    date="2026-05-19",
    company="Acme Corp",
    title="Senior Angular Dev",
    stack="Angular",
    ats="%",
    url="https://example.com/jobs/1",
    folder="",
    sent="",
    reapply="",
    to_learn="",
) -> dict:
    return {
        "ID": id, "Date": date, "Company": company, "Job Title": title,
        "Stack": stack, "ATS %": ats, "URL": url, "Folder": folder,
        "Sent": sent, "Re-application": reapply, "To Learn": to_learn,
    }


# ── Insert / basic CRUD ───────────────────────────────────────────────────────

def test_insert_and_count():
    assert is_empty()
    insert_job(_row())
    assert row_count() == 1
    assert not is_empty()


def test_insert_ignores_duplicate_by_default():
    insert_job(_row())
    insert_job(_row())
    assert row_count() == 1


def test_insert_replace_overwrites():
    insert_job(_row(ats="SKIP"))
    insert_job(_row(ats="87%"), replace=True)
    rows = get_all_rows()
    assert rows[0]["ATS %"] == "87%"


def test_insert_skips_missing_id():
    insert_job(_row(id=""))
    assert is_empty()


# ── Read helpers ──────────────────────────────────────────────────────────────

def test_get_known_norm_urls_normalized():
    insert_job(_row(url="https://example.com/jobs/1/"))
    norms = get_known_norm_urls()
    assert "https://example.com/jobs/1" in norms


def test_get_known_ct_keys():
    insert_job(_row())
    keys = get_known_ct_keys()
    assert any("acmecorp" in k for k in keys)


def test_get_by_norm_url():
    insert_job(_row())
    results = get_by_norm_url("https://example.com/jobs/1")
    assert len(results) == 1
    assert results[0]["company"] == "Acme Corp"


def test_get_by_norm_url_empty_for_unknown():
    results = get_by_norm_url("https://nope.example.com/x")
    assert results == []


def test_get_by_ats():
    insert_job(_row(id="aaaa1111", ats="FAIL"))
    insert_job(_row(id="bbbb2222", ats="SKIP", url="https://example.com/jobs/2"))
    fail_rows = get_by_ats("FAIL")
    assert len(fail_rows) == 1
    assert fail_rows[0]["ats"] == "FAIL"


def test_get_unsent_rows_excludes_skip():
    insert_job(_row(id="aaa1", ats="SKIP"))
    insert_job(_row(id="bbb2", ats="87%", url="https://example.com/2"))
    unsent = get_unsent_rows()
    assert len(unsent) == 1
    assert unsent[0]["ats"] == "87%"


def test_get_unsent_rows_excludes_sent():
    insert_job(_row(sent="2026-05-01"))
    assert get_unsent_rows() == []


# ── is_known ─────────────────────────────────────────────────────────────────

def test_is_known_by_url():
    insert_job(_row())
    assert is_known("https://example.com/jobs/1")


def test_is_known_by_ct_key():
    insert_job(_row())
    assert is_known("https://other.com", ct_key="acmecorp|seniorangulardev")


def test_is_known_false():
    assert not is_known("https://nothing.com")


# ── Updates ───────────────────────────────────────────────────────────────────

def test_update_sent():
    insert_job(_row())
    n = update_sent("abc12345", "2026-05-19")
    assert n == 1
    rows = get_by_norm_url("https://example.com/jobs/1")
    assert rows[0]["sent"] == "2026-05-19"


def test_update_sent_unknown_id_returns_zero():
    assert update_sent("nope0000", "2026-05-19") == 0


def test_update_user_fields():
    insert_job(_row())
    update_user_fields("abc12345", sent="2026-05-20", reapply="+", to_learn="RxJS")
    rows = get_all_rows()
    assert rows[0]["Sent"] == "2026-05-20"
    assert rows[0]["Re-application"] == "+"
    assert rows[0]["To Learn"] == "RxJS"


# ── Delete ────────────────────────────────────────────────────────────────────

def test_delete_where():
    insert_job(_row(ats="FAIL"))
    assert delete_where("https://example.com/jobs/1", "FAIL") == 1
    assert is_empty()


def test_delete_where_no_match():
    insert_job(_row(ats="FAIL"))
    assert delete_where("https://example.com/jobs/1", "SKIP") == 0
    assert row_count() == 1


# ── get_all_rows ──────────────────────────────────────────────────────────────

def test_get_all_rows_headers():
    insert_job(_row())
    rows = get_all_rows()
    assert len(rows) == 1
    assert set(rows[0].keys()) == {
        "ID", "Date", "Company", "Job Title", "Stack", "ATS %",
        "URL", "Folder", "Sent", "Re-application", "To Learn",
    }
