"""Tests for tools/backfill_runs.py — corpus backfill into generation_runs.

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M1.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import tools.backfill_runs as backfill_mod
from hunter import metrics


@pytest.fixture()
def corpus_db(tmp_path, monkeypatch):
    """Isolated tracker.db + Applications/ root for the backfill tool.

    Mirrors tests/test_source_health.py's own DB_PATH monkeypatch pattern —
    hunter.metrics reads its module-level DB_PATH on every call.
    """
    db = tmp_path / "tracker.db"
    monkeypatch.setattr(metrics, "DB_PATH", db)
    root = tmp_path / "Applications"
    root.mkdir()
    return root, db


def _write_folder(root: Path, rel: str, content: dict, *, with_pdf: bool = True) -> Path:
    folder = root / rel
    folder.mkdir(parents=True, exist_ok=True)
    content = dict(content)
    content.setdefault("output_folder", str(folder).replace("\\", "/"))
    (folder / "content.json").write_text(json.dumps(content), encoding="utf-8")
    if with_pdf:
        (folder / "CV_Angular_EN.pdf").write_bytes(b"%PDF-1.4 fake")
    return folder


def _rows(db: Path) -> list[dict]:
    """generation_runs rows, or [] when the table does not exist yet.

    A dry run writes nothing at all — not even the lazily-created table — so
    "no table" and "no rows" are the same answer to the only question the
    tests ask here.
    """
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM generation_runs")]
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()
    return rows


WITH_HISTORY = {
    "apply_url": "https://example.com/jobs/nordic-angular",
    "company_name": "Nordic Frontend Labs",
    "job_title": "Senior Angular Developer",
    "primary_lang": "EN",
    "ats_check": {"score": 91.2, "keyword_score": 88.0},
    "ats_check_pdf": {"score": 93.5},
    "ats_verdict": {"score": 96},
    "verdict_history": [
        {
            "round": 1,
            "kind": "honest",
            "score_before": 88.0,
            "score_after": 93.0,
            "outcome": "accepted",
        },
        {
            "round": 2,
            "kind": "honest",
            "score_before": 93.0,
            "score_after": 91.0,
            "outcome": "rejected",
        },
        {
            "round": 3,
            "kind": "stretch",
            "score_before": 93.0,
            "score_after": 96.0,
            "outcome": "accepted",
        },
    ],
    "cost": {"total_usd": 0.53},
}

WITHOUT_HISTORY = {
    "apply_url": "https://example.com/jobs/plain-react",
    "company_name": "Plain React Co",
    "job_title": "Frontend Developer",
    "primary_lang": "EN",
    "ats_verdict": {"score": 90},
    "cost": {"mode": "cli", "total_usd": None},
}


# ── derive_run_fields ────────────────────────────────────────────────────────


def test_derive_run_fields_with_verdict_history(corpus_db):
    root, _db = corpus_db
    folder = _write_folder(root, "2026-09-01/NordicFrontendLabs", WITH_HISTORY)

    fields = backfill_mod.derive_run_fields(WITH_HISTORY, folder)

    assert fields["pipeline"] == "backfill"
    assert fields["posting_lang"] == "EN"
    assert fields["ats_pre_score"] == 91.2
    assert fields["ats_pre_keyword"] == 88.0
    assert fields["ats_pdf_score"] == 93.5
    assert fields["verdict_first"] == 88.0  # verdict_history[0].score_before
    assert fields["verdict_final"] == 96  # ats_verdict.score
    assert fields["refine_rounds"] == 3
    assert fields["refine_accepted"] == 2
    assert fields["best_round_kind"] == "stretch"  # last accepted round
    assert fields["cost_usd"] == 0.53
    assert fields["outcome"] == "ok"  # a PDF is on disk


def test_derive_run_fields_without_verdict_history_falls_back_to_ats_verdict(corpus_db):
    root, _db = corpus_db
    folder = _write_folder(root, "2026-09-02/PlainReactCo", WITHOUT_HISTORY, with_pdf=False)

    fields = backfill_mod.derive_run_fields(WITHOUT_HISTORY, folder)

    assert fields["verdict_first"] == 90  # no history -> falls back to ats_verdict.score
    assert fields["verdict_final"] == 90
    assert "refine_rounds" not in fields  # None-filtered: no verdict_history at all
    assert fields["outcome"] == "no_docs"  # no PDF/DOCX on disk


def test_derive_run_fields_reused_donor(corpus_db):
    root, _db = corpus_db
    content = dict(WITHOUT_HISTORY, reused_from="/Applications/2026-08-01/Donor")
    folder = _write_folder(root, "2026-09-03/ReusedCo", content)

    fields = backfill_mod.derive_run_fields(content, folder)

    assert fields["reused_donor"] == "/Applications/2026-08-01/Donor"
    assert fields["outcome"] == "reused_repost"


# ── folder discovery: shadow subfolders excluded ────────────────────────────


def test_iter_content_json_folders_excludes_shadow_subfolders(corpus_db, monkeypatch):
    root, _db = corpus_db
    _write_folder(root, "2026-09-01/NordicFrontendLabs", WITH_HISTORY)
    # A dual-apply shadow subfolder is named after a known llm_profiles entry.
    monkeypatch.setattr(backfill_mod, "_shadow_profile_names", lambda: {"deepseek-v3"})
    _write_folder(root, "2026-09-01/NordicFrontendLabs/deepseek-v3", WITHOUT_HISTORY)

    folders = backfill_mod.iter_content_json_folders(root)

    assert len(folders) == 1
    assert folders[0].name == "NordicFrontendLabs"


# ── end-to-end backfill() + idempotency ─────────────────────────────────────


def test_backfill_writes_one_row_per_folder(corpus_db):
    root, db = corpus_db
    _write_folder(root, "2026-09-01/NordicFrontendLabs", WITH_HISTORY)
    _write_folder(root, "2026-09-02/PlainReactCo", WITHOUT_HISTORY, with_pdf=False)

    result = backfill_mod.backfill(root, db)

    assert result["scanned"] == 2
    assert result["written"] == 2
    assert result["skipped_existing"] == 0
    assert result["errors"] == []

    rows = _rows(db)
    assert len(rows) == 2
    by_url = {r["url_norm"]: r for r in rows}
    assert "https://example.com/jobs/nordic-angular" in by_url
    assert "https://example.com/jobs/plain-react" in by_url
    # started_at is deliberately left NULL — a backfilled row never knows its
    # real start time, and must not be mistaken for one the live pipeline timed.
    for row in rows:
        assert row["started_at"] is None
        assert row["pipeline"] == "backfill"


def test_backfill_is_idempotent(corpus_db):
    root, db = corpus_db
    _write_folder(root, "2026-09-01/NordicFrontendLabs", WITH_HISTORY)

    first = backfill_mod.backfill(root, db)
    assert first["written"] == 1

    second = backfill_mod.backfill(root, db)
    assert second["scanned"] == 1
    assert second["written"] == 0
    assert second["skipped_existing"] == 1

    rows = _rows(db)
    assert len(rows) == 1  # no duplicate row from the second pass


def test_backfill_dry_run_writes_nothing(corpus_db):
    root, db = corpus_db
    _write_folder(root, "2026-09-01/NordicFrontendLabs", WITH_HISTORY)

    result = backfill_mod.backfill(root, db, dry_run=True)

    assert result["written"] == 1
    # A dry run must not write a row. It may not even create the table:
    # the tables are made lazily by the first real start_run().
    assert _rows(db) == []


def test_backfill_matches_applications_row_by_url_norm(corpus_db):
    root, db = corpus_db
    from hunter.db import init_db

    init_db(db, xlsx_path=root / "no_tracker.xlsx")
    with backfill_mod.get_db(db) as conn:
        conn.execute(
            "INSERT INTO applications (id, url, url_norm, company, title, folder, user_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "abc12345",
                WITH_HISTORY["apply_url"],
                "https://example.com/jobs/nordic-angular",
                "Nordic Frontend Labs",
                "Senior Angular Developer",
                str(root / "2026-09-01/NordicFrontendLabs"),
                "user-1",
            ),
        )
    _write_folder(root, "2026-09-01/NordicFrontendLabs", WITH_HISTORY)

    backfill_mod.backfill(root, db)

    rows = _rows(db)
    matched = [r for r in rows if r["url_norm"] == "https://example.com/jobs/nordic-angular"]
    assert matched
    assert matched[0]["row_id"] == "abc12345"
    assert matched[0]["user_id"] == "user-1"


def test_backfill_reports_a_malformed_content_json_as_an_error(corpus_db):
    root, db = corpus_db
    folder = root / "2026-09-01" / "BrokenCo"
    folder.mkdir(parents=True)
    (folder / "content.json").write_text("{not valid json", encoding="utf-8")

    result = backfill_mod.backfill(root, db)

    assert result["scanned"] == 1
    assert result["written"] == 0
    assert len(result["errors"]) == 1


# ── run_id determinism ───────────────────────────────────────────────────────


def test_run_id_is_stable_for_the_same_folder(tmp_path):
    folder = tmp_path / "Applications" / "2026-09-01" / "SomeCo"
    folder.mkdir(parents=True)
    assert backfill_mod._run_id_for_folder(folder) == backfill_mod._run_id_for_folder(folder)


def test_run_id_differs_for_different_folders(tmp_path):
    a = tmp_path / "A"
    b = tmp_path / "B"
    a.mkdir()
    b.mkdir()
    assert backfill_mod._run_id_for_folder(a) != backfill_mod._run_id_for_folder(b)
