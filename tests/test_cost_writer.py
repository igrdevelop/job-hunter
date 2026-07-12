"""Unit tests for hunter.cost_writer.

The writer pokes column M directly so cost values can land in the Sheet
without disturbing the bot's A–K push (gsheets_client.COLUMNS) or
sent_normalizer's column L ("Applied Date"). Tests stub the Sheets API
service object — no real API calls.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import hunter.cost_writer as cw
from hunter.cost_writer import (
    COST_COL_LETTER,
    COST_HEADER,
    backfill_all_costs_sync,
    mirror_cost_cell_sync,
    write_cost_header_sync,
)


def _seed_db(monkeypatch, tmp_path):
    """Point hunter.cost_writer at a fresh tracker DB schema for the test."""
    import hunter.db as db

    db_path = tmp_path / "tracker.db"
    monkeypatch.setattr(db, "TRACKER_DB_PATH", db_path)
    monkeypatch.setattr(cw, "DB_PATH", db_path)
    db.init_db(db_path)
    cw._header_written.clear()
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
    """Stub Sheets API service object with a recording values() chain."""
    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.update.return_value.execute.return_value = {}
    service.spreadsheets.return_value.values.return_value.batchUpdate.return_value.execute.return_value = {}
    return service


def _captured_update_args(service):
    """Return the kwargs passed to spreadsheets().values().update() in order."""
    calls = service.spreadsheets.return_value.values.return_value.update.call_args_list
    return [c.kwargs for c in calls]


def _captured_batch_args(service):
    calls = service.spreadsheets.return_value.values.return_value.batchUpdate.call_args_list
    return [c.kwargs for c in calls]


def test_mirror_cost_cell_writes_to_column_m(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="abc", url="x", url_norm="x", sheets_row=5, cost_usd=0.4712)
    service = _make_service()

    ok = mirror_cost_cell_sync(service, "SHEET", "abc")
    assert ok is True

    updates = _captured_update_args(service)
    # First call sets the header M1; second call writes M5 with the cost.
    assert len(updates) == 2
    assert updates[0]["range"].endswith(f"!{COST_COL_LETTER}1")
    assert updates[0]["body"]["values"] == [[COST_HEADER]]
    assert updates[1]["range"].endswith(f"!{COST_COL_LETTER}5")
    assert updates[1]["body"]["values"] == [["$0.4712"]]


def test_mirror_cost_cell_header_written_once_per_process(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="a", url="x", url_norm="x", sheets_row=2, cost_usd=0.10)
    _insert(db_path, id="b", url="y", url_norm="y", sheets_row=3, cost_usd=0.20)
    service = _make_service()

    mirror_cost_cell_sync(service, "SHEET", "a")
    mirror_cost_cell_sync(service, "SHEET", "b")

    updates = _captured_update_args(service)
    # First call: header + M2. Second call: only M3 (header cached).
    header_calls = [u for u in updates if u["range"].endswith("!M1")]
    assert len(header_calls) == 1


def test_mirror_cost_cell_no_sheets_row_is_noop(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    # Row exists in DB but never made it to Sheets (sheets_row is NULL).
    _insert(db_path, id="orphan", url="x", url_norm="x", cost_usd=0.10)
    service = _make_service()

    ok = mirror_cost_cell_sync(service, "SHEET", "orphan")
    assert ok is True
    # No write at all — not even the header.
    assert _captured_update_args(service) == []


def test_mirror_cost_cell_null_cost_is_noop(monkeypatch, tmp_path):
    # CLI-mode runs and pre-tracking rows have NULL cost_usd. We must not
    # blank the M column with an empty string — leave whatever is there.
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="cli", url="x", url_norm="x", sheets_row=10, cost_usd=None)
    service = _make_service()

    ok = mirror_cost_cell_sync(service, "SHEET", "cli")
    assert ok is True
    assert _captured_update_args(service) == []


def test_mirror_cost_cell_swallows_sheets_error(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="abc", url="x", url_norm="x", sheets_row=5, cost_usd=0.50)
    service = _make_service()
    # Make the value write (second call) raise.
    service.spreadsheets.return_value.values.return_value.update.return_value.execute.side_effect = [
        {},  # header write succeeds
        RuntimeError("API down"),  # value write fails
    ]

    ok = mirror_cost_cell_sync(service, "SHEET", "abc")
    assert ok is False  # logged, swallowed, signalled to caller


def test_write_cost_header_idempotent(monkeypatch, tmp_path):
    _seed_db(monkeypatch, tmp_path)
    service = _make_service()
    assert write_cost_header_sync(service, "SHEET") is True
    assert write_cost_header_sync(service, "SHEET") is True
    updates = _captured_update_args(service)
    assert all(u["range"].endswith("!M1") for u in updates)
    assert all(u["body"]["values"] == [[COST_HEADER]] for u in updates)


def test_backfill_writes_all_priced_rows_in_one_batch(monkeypatch, tmp_path):
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="a", url="x", url_norm="x", sheets_row=2, cost_usd=0.10)
    _insert(db_path, id="b", url="y", url_norm="y", sheets_row=3, cost_usd=0.25)
    _insert(db_path, id="c", url="z", url_norm="z", sheets_row=4, cost_usd=0.50)
    # CLI run — no cost → skipped.
    _insert(db_path, id="cli", url="cli", url_norm="cli", sheets_row=5, cost_usd=None)
    # Never pushed → skipped.
    _insert(db_path, id="orphan", url="o", url_norm="o", cost_usd=0.99)
    service = _make_service()

    result = backfill_all_costs_sync(service, "SHEET")
    assert result == {"written": 3, "skipped_no_row": 1, "skipped_no_cost": 1}

    batches = _captured_batch_args(service)
    assert len(batches) == 1
    payload = batches[0]["body"]["data"]
    # Header + 3 data rows.
    assert payload[0]["range"].endswith("!M1")
    assert payload[0]["values"] == [[COST_HEADER]]
    written_ranges = sorted(p["range"] for p in payload[1:])
    assert written_ranges[0].endswith("!M2")
    assert written_ranges[1].endswith("!M3")
    assert written_ranges[2].endswith("!M4")


def test_backfill_only_header_when_no_priced_rows(monkeypatch, tmp_path):
    _seed_db(monkeypatch, tmp_path)
    service = _make_service()
    result = backfill_all_costs_sync(service, "SHEET")
    assert result == {"written": 0, "skipped_no_row": 0, "skipped_no_cost": 0}
    # No batchUpdate; header written via the single-cell update path.
    assert _captured_batch_args(service) == []
    assert any(u["range"].endswith("!M1") for u in _captured_update_args(service))


def test_cost_writer_never_touches_columns_a_through_l(monkeypatch, tmp_path):
    # Defensive: any cell address this module writes MUST start with M.
    # If a future change accidentally targets L (Applied Date) or A–K (the
    # bot's main range), it would silently destroy data. This test catches
    # that by assertion on every captured range.
    db_path = _seed_db(monkeypatch, tmp_path)
    _insert(db_path, id="a", url="x", url_norm="x", sheets_row=2, cost_usd=0.10)
    _insert(db_path, id="b", url="y", url_norm="y", sheets_row=3, cost_usd=0.25)
    service = _make_service()

    mirror_cost_cell_sync(service, "SHEET", "a")
    write_cost_header_sync(service, "SHEET")
    backfill_all_costs_sync(service, "SHEET")

    for u in _captured_update_args(service):
        cell = u["range"].split("!")[-1]
        assert cell.startswith("M"), f"unexpected cell write: {u['range']}"
    for b in _captured_batch_args(service):
        for entry in b["body"]["data"]:
            cell = entry["range"].split("!")[-1]
            assert cell.startswith("M"), f"unexpected cell write: {entry['range']}"
