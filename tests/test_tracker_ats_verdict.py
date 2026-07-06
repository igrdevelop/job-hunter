"""Tests for the ats_verdict DB column + tracker.set_ats_verdict.

Phase 2 of the ATS-verdict work (docs/ATS_VERDICT_PHASE2_PLAN.md, M1): the
independent PDF-verdict score is stamped on the tracker row post-hoc (the row
already exists when the verdict is computed), matched by normalized URL.

docs/VERDICT_REFINE_PLAN.md (M4) extended the same stamp to also overwrite
`ats_status` (the "ATS %" column) so every interface shows one number: the
independent verdict, not the generator's own self-score.
"""

from hunter import tracker
from hunter.db import get_db


def _insert_row(tracker_db, *, url: str, row_id: str = "abc12345") -> None:
    norm = tracker.normalize_url(url)
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, ats_status, url, url_norm)
            VALUES (?, '2026-07-02', 'Acme', 'Dev', '97%', ?, ?)
            """,
            (row_id, url, norm),
        )


def _verdict_of(tracker_db, row_id: str):
    with get_db(tracker_db) as conn:
        row = conn.execute(
            "SELECT ats_verdict FROM applications WHERE id=?", (row_id,)
        ).fetchone()
    return row["ats_verdict"] if row else None


def _status_of(tracker_db, row_id: str):
    with get_db(tracker_db) as conn:
        row = conn.execute(
            "SELECT ats_status FROM applications WHERE id=?", (row_id,)
        ).fetchone()
    return row["ats_status"] if row else None


# ── Schema migration ──────────────────────────────────────────────────────────

def test_ats_verdict_column_exists(tracker_db):
    """The lazy migration in db._ensure_columns adds ats_verdict to fresh DBs."""
    with get_db(tracker_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(applications)")}
    assert "ats_verdict" in cols


# ── set_ats_verdict ───────────────────────────────────────────────────────────

def test_set_ats_verdict_writes_value(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_ats_verdict("https://example.com/jobs/1", 91.0) is True
    assert _verdict_of(tracker_db, "abc12345") == 91.0


def test_set_ats_verdict_normalizes_url(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_ats_verdict(
        "https://example.com/jobs/1/?utm_source=x", 88.5
    ) is True
    assert _verdict_of(tracker_db, "abc12345") == 88.5


def test_set_ats_verdict_false_when_url_not_found(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_ats_verdict("https://example.com/jobs/99", 90.0) is False
    assert _verdict_of(tracker_db, "abc12345") is None


def test_set_ats_verdict_false_on_empty_url(tracker_db):
    assert tracker.set_ats_verdict("", 90.0) is False


def test_set_ats_verdict_overwrites_previous(tracker_db):
    """A re-run (e.g. /force) refreshes the verdict."""
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    tracker.set_ats_verdict("https://example.com/jobs/1", 80.0)
    tracker.set_ats_verdict("https://example.com/jobs/1", 92.0)
    assert _verdict_of(tracker_db, "abc12345") == 92.0


# ── ats_status overwrite (VERDICT_REFINE_PLAN M4) ─────────────────────────────
# The owner asked for a single ATS number across every interface: the
# tracker/Sheet "ATS %" column should show the independent verdict, not the
# generator's own self-assessment, once the verdict has been stamped.

def test_set_ats_verdict_overwrites_ats_status(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert _status_of(tracker_db, "abc12345") == "97%"  # generator self-score
    tracker.set_ats_verdict("https://example.com/jobs/1", 91.0)
    assert _status_of(tracker_db, "abc12345") == "91%"  # replaced by the verdict


def test_set_ats_verdict_ats_status_rounds_to_int_percent(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    tracker.set_ats_verdict("https://example.com/jobs/1", 88.6)
    assert _status_of(tracker_db, "abc12345") == "89%"


def test_set_ats_verdict_never_raises(tracker_db, monkeypatch):
    """Best-effort contract: DB failure logs and returns False."""
    def _boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(tracker, "get_db", _boom)
    assert tracker.set_ats_verdict("https://example.com/jobs/1", 90.0) is False


# ── set_to_learn (VERDICT_REFINE_PLAN review Fix 1) ───────────────────────────
# The tracker row is created (Step 7, generate_docs -> add_applied) BEFORE the
# verdict refine loop's round-2 stretch additions land in content["to_learn"]
# — so this is the same post-hoc-UPDATE-by-URL contract as set_ats_verdict.

def _to_learn_of(tracker_db, row_id: str):
    with get_db(tracker_db) as conn:
        row = conn.execute(
            "SELECT to_learn FROM applications WHERE id=?", (row_id,)
        ).fetchone()
    return row["to_learn"] if row else None


def test_set_to_learn_writes_value(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_to_learn("https://example.com/jobs/1", "Vitest, GraphQL") is True
    assert _to_learn_of(tracker_db, "abc12345") == "Vitest, GraphQL"


def test_set_to_learn_normalizes_url(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_to_learn(
        "https://example.com/jobs/1/?utm_source=x", "Vitest"
    ) is True
    assert _to_learn_of(tracker_db, "abc12345") == "Vitest"


def test_set_to_learn_overwrites_previous(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    tracker.set_to_learn("https://example.com/jobs/1", "Vitest")
    tracker.set_to_learn("https://example.com/jobs/1", "Vitest, GraphQL")
    assert _to_learn_of(tracker_db, "abc12345") == "Vitest, GraphQL"


def test_set_to_learn_false_when_url_not_found(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_to_learn("https://example.com/jobs/99", "Vitest") is False


def test_set_to_learn_false_on_empty_url(tracker_db):
    assert tracker.set_to_learn("", "Vitest") is False


def test_set_to_learn_never_raises(tracker_db, monkeypatch):
    """Best-effort contract: DB failure logs and returns False."""
    def _boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(tracker, "get_db", _boom)
    assert tracker.set_to_learn("https://example.com/jobs/1", "Vitest") is False


# ── set_cost ──────────────────────────────────────────────────────────────────
# The row is created (Step 7) with the Step 6.5 pre-verdict/pre-refine cost;
# the verdict + refine loop spend more AFTER that, so the pipeline re-prices
# and re-stamps — same post-hoc-UPDATE-by-URL contract as set_ats_verdict.

def _cost_of(tracker_db, row_id: str):
    with get_db(tracker_db) as conn:
        row = conn.execute(
            "SELECT cost_usd FROM applications WHERE id=?", (row_id,)
        ).fetchone()
    return row["cost_usd"] if row else None


def test_set_cost_writes_value(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_cost("https://example.com/jobs/1", 0.4213) is True
    assert _cost_of(tracker_db, "abc12345") == 0.4213


def test_set_cost_normalizes_url(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_cost(
        "https://example.com/jobs/1/?utm_source=x", 0.31
    ) is True
    assert _cost_of(tracker_db, "abc12345") == 0.31


def test_set_cost_overwrites_previous(tracker_db):
    """The refine loop replaces the Step 6.5 figure with the real total."""
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    tracker.set_cost("https://example.com/jobs/1", 0.18)
    tracker.set_cost("https://example.com/jobs/1", 0.55)
    assert _cost_of(tracker_db, "abc12345") == 0.55


def test_set_cost_false_when_url_not_found(tracker_db):
    _insert_row(tracker_db, url="https://example.com/jobs/1")
    assert tracker.set_cost("https://example.com/jobs/99", 0.5) is False


def test_set_cost_false_on_empty_url(tracker_db):
    assert tracker.set_cost("", 0.5) is False


def test_set_cost_never_raises(tracker_db, monkeypatch):
    """Best-effort contract: DB failure logs and returns False."""
    def _boom(*a, **k):
        raise RuntimeError("db locked")
    monkeypatch.setattr(tracker, "get_db", _boom)
    assert tracker.set_cost("https://example.com/jobs/1", 0.5) is False
