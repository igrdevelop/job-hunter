"""Unit tests for hunter.verdict_writer (Sheet column N, "ATS Verdict").

Mirrors the cost_writer (column M) test approach: the writer pokes column N
directly so the independent PDF-verdict score lands in the Sheet without
disturbing the A–K push, sent_normalizer's column L, or cost_writer's M.
Tests stub the Sheets API service object — no real API calls.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import hunter.verdict_writer as vw
from hunter.verdict_writer import (
    VERDICT_COL_LETTER,
    VERDICT_HEADER,
    backfill_all_verdicts_sync,
    mirror_verdict_cell_sync,
    write_verdict_header_sync,
)


def _seed_db(monkeypatch, tmp_path):
    import hunter.db as db

    db_path = tmp_path / "tracker.db"
    monkeypatch.setattr(db, "TRACKER_DB_PATH", db_path)
    monkeypatch.setattr(vw, "DB_PATH", db_path)
    db.init_db(db_path)
    vw._header_written.clear()
    return db_path


def _insert(db_path, **kwargs):
    cols = ",".join(kwargs.keys())
    placeholders = ",".join("?" * len(kwargs))
    con = sqlite3.connect(str(db_path))
    con.execute(
        f"INSERT INTO applications ({cols}) VALUES ({placeholders})",
        list(kwargs.values()),
    )
    con.commit()
    con.close()


def _make_service():
    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.update.return_value.execute.return_value = {}
    service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.return_value = {}
    return service


def _captured_update_args(service):
    calls = service.spreadsheets.return_value.values.return_value.update.call_args_list
    return [c.kwargs for c in calls]


def _captured_batch_args(service):
    calls = service.spreadsheets.return_value.values.return_value.batchUpdate.call_args_list
    return [c.kwargs for c in calls]


# ── mirror_verdict_cell_sync ──────────────────────────────────────────────────


def test_mirror_verdict_cell_writes_to_column_n(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="abc", url="x", url_norm="x", sheets_row=5, ats_verdict=91.0)
    service = _make_service()

    ok = mirror_verdict_cell_sync(service, "SHEET", "abc")
    assert ok is True

    updates = _captured_update_args(service)
    # First call sets the header N1; second writes N5 with the score.
    assert len(updates) == 2
    assert updates[0]["range"].endswith(f"!{VERDICT_COL_LETTER}1")
    assert updates[0]["body"]["values"] == [[VERDICT_HEADER]]
    assert updates[1]["range"].endswith(f"!{VERDICT_COL_LETTER}5")
    assert updates[1]["body"]["values"] == [[91]]  # whole number → int cell


def test_mirror_verdict_cell_keeps_half_points(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="abc", url="x", url_norm="x", sheets_row=2, ats_verdict=88.5)
    service = _make_service()
    mirror_verdict_cell_sync(service, "SHEET", "abc")
    updates = _captured_update_args(service)
    assert updates[-1]["body"]["values"] == [[88.5]]


def test_mirror_verdict_cell_header_written_once_per_process(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="a", url="x", url_norm="x", sheets_row=2, ats_verdict=80.0)
    _insert(db_path, id="b", url="y", url_norm="y", sheets_row=3, ats_verdict=85.0)
    service = _make_service()

    mirror_verdict_cell_sync(service, "SHEET", "a")
    mirror_verdict_cell_sync(service, "SHEET", "b")

    updates = _captured_update_args(service)
    header_calls = [u for u in updates if u["range"].endswith("!N1")]
    assert len(header_calls) == 1


def test_mirror_verdict_cell_no_sheets_row_is_noop(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    # Row never mirrored to Sheets (token down at apply time) — nothing to write.
    _insert(db_path, id="abc", url="x", url_norm="x", ats_verdict=90.0)
    service = _make_service()
    assert mirror_verdict_cell_sync(service, "SHEET", "abc") is True
    assert _captured_update_args(service) == []


def test_mirror_verdict_cell_null_verdict_is_noop(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="abc", url="x", url_norm="x", sheets_row=4)
    service = _make_service()
    assert mirror_verdict_cell_sync(service, "SHEET", "abc") is True
    assert _captured_update_args(service) == []


def test_mirror_verdict_cell_swallows_sheets_error(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="abc", url="x", url_norm="x", sheets_row=5, ats_verdict=90.0)
    service = _make_service()
    service.spreadsheets.return_value.values.return_value.update.return_value.execute.side_effect = RuntimeError(
        "quota"
    )
    # Best-effort: returns False, never raises.
    assert mirror_verdict_cell_sync(service, "SHEET", "abc") is False


# ── header + backfill ─────────────────────────────────────────────────────────


def test_write_verdict_header_idempotent(monkeypatch, tmp_path):
    _seed_db(monkeypatch, tmp_path)
    service = _make_service()
    assert write_verdict_header_sync(service, "SHEET") is True
    assert write_verdict_header_sync(service, "SHEET") is True
    updates = _captured_update_args(service)
    assert all(u["body"]["values"] == [[VERDICT_HEADER]] for u in updates)


def test_backfill_writes_all_judged_rows_in_one_batch(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="a", url="1", url_norm="1", sheets_row=2, ats_verdict=91.0)
    _insert(db_path, id="b", url="2", url_norm="2", sheets_row=3, ats_verdict=87.5)
    _insert(db_path, id="c", url="3", url_norm="3", sheets_row=4)  # no verdict
    _insert(db_path, id="d", url="4", url_norm="4", ats_verdict=90.0)  # never mirrored
    service = _make_service()

    result = backfill_all_verdicts_sync(service, "SHEET")

    assert result == {"written": 2, "skipped_no_row": 1, "skipped_no_verdict": 1}
    batches = _captured_batch_args(service)
    assert len(batches) == 1
    data = batches[0]["body"]["data"]
    ranges = [d["range"] for d in data]
    assert ranges[0].endswith("!N1")  # header pinned first
    assert any(r.endswith("!N2") for r in ranges)
    assert any(r.endswith("!N3") for r in ranges)
    assert not any(r.endswith("!N4") for r in ranges)


def test_backfill_only_header_when_no_judged_rows(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="a", url="1", url_norm="1", sheets_row=2)
    service = _make_service()
    result = backfill_all_verdicts_sync(service, "SHEET")
    assert result["written"] == 0
    # Header still labelled via the single-update path.
    updates = _captured_update_args(service)
    assert updates and updates[0]["range"].endswith("!N1")


def test_verdict_writer_never_touches_columns_a_through_m(monkeypatch, tmp_path):
    """Regression guard: every range this module writes is in column N."""
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="a", url="1", url_norm="1", sheets_row=2, ats_verdict=90.0)
    service = _make_service()

    mirror_verdict_cell_sync(service, "SHEET", "a")
    backfill_all_verdicts_sync(service, "SHEET")

    all_ranges = [u["range"] for u in _captured_update_args(service)]
    for b in _captured_batch_args(service):
        all_ranges += [d["range"] for d in b["body"]["data"]]
    assert all_ranges
    for r in all_ranges:
        assert "!N" in r, f"unexpected range outside column N: {r}"


def test_columns_a_to_k_contract_untouched():
    """gsheets_client.COLUMNS must still end at K — L/M/N are writer-owned."""
    from hunter.gsheets_client import COLUMNS

    assert len(COLUMNS) == 11  # A..K
