"""Outcome round trip through Sheet column O (hunter.outcome_writer + gsheets_sync).

PR #276 put `outcome_label` in tracker.db and /outcome in Telegram, but the
owner's daily workflow happens in the Sheet, which could neither show nor edit
it: the A–K push (gsheets_client.COLUMNS) never writes O and the pull parser
never reads it. These tests pin the fifth writer and the pull merge:

- a dirty row's outcome reaches O, and a failed O write keeps the row dirty;
- the A–K push can never touch O, so an owner-typed label is never blanked;
- a valid label in O fills an empty DB value; a blank or garbage cell never
  clears or overwrites one; a dirty DB row keeps its unpushed value.

Sheets API calls go to a MagicMock service, same style as test_gsheets_sync.py
and test_verdict_writer.py.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock, patch

import pytest

from hunter import gsheets_client, outcome_writer
from hunter.db import get_db


@pytest.fixture(autouse=True)
def _fresh_column_state():
    outcome_writer._column_ready.clear()
    yield
    outcome_writer._column_ready.clear()


def _service(grid: list[list[str]] | None = None) -> MagicMock:
    svc = MagicMock()
    values = svc.spreadsheets.return_value.values.return_value
    values.get.return_value.execute.return_value = {"values": grid or []}
    values.update.return_value.execute.return_value = {}
    values.append.return_value.execute.return_value = {
        "updates": {"updatedRange": "'Tracker'!A9:K9"}
    }
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Tracker", "sheetId": 777}}]
    }
    return svc


def _update_ranges(svc: MagicMock) -> list[str]:
    calls = svc.spreadsheets.return_value.values.return_value.update.call_args_list
    return [c.kwargs["range"] for c in calls]


def _update_body_for(svc: MagicMock, cell: str) -> list[list[str]]:
    for c in svc.spreadsheets.return_value.values.return_value.update.call_args_list:
        if c.kwargs["range"] == cell:
            return c.kwargs["body"]["values"]
    raise AssertionError(f"no update to {cell}; saw {_update_ranges(svc)}")


def _dirty_row(row_id: str = "abc12345", outcome: str = "") -> dict:
    return {
        "Date": "2026-09-01",
        "Company": "Acme",
        "Job Title": "Senior Frontend Developer",
        "Stack": "Angular",
        "ATS %": "91%",
        "URL": "https://example.com/jobs/1",
        "Folder": "/app/Applications/2026-09-01/Acme",
        "Sent": "2026-09-02",
        "Re-application": "",
        "To Learn": "",
        "ID": row_id,
        "Outcome": outcome,
    }


def _resync(svc: MagicMock, dirty: list) -> tuple[int, MagicMock]:
    from hunter import gsheets_sync

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=svc),
        patch("hunter.gsheets_sync._sheet_id", return_value="SHEET"),
        patch("hunter.gsheets_sync.get_dirty_rows_for_sheets", return_value=dirty),
        patch("hunter.gsheets_sync.set_sheets_row"),
        patch("hunter.gsheets_sync.mark_sheets_clean") as mock_clean,
    ):
        synced = asyncio.run(gsheets_sync.resync_dirty())
    return synced, mock_clean


# ── push: dirty rows ─────────────────────────────────────────────────────────


def test_resync_writes_outcome_to_column_o_for_dirty_row():
    svc = _service()
    synced, mock_clean = _resync(svc, [("abc12345", _dirty_row(outcome="interview"), 10)])

    assert synced == 1
    assert _update_body_for(svc, "'Tracker'!O10") == [["interview"]]
    assert _update_body_for(svc, "'Tracker'!O1") == [["Outcome"]]
    mock_clean.assert_called_once_with("abc12345")


def test_resync_writes_outcome_at_the_row_a_fresh_append_landed_on():
    svc = _service()
    _resync(svc, [("abc12345", _dirty_row(outcome="offer"), None)])

    assert _update_body_for(svc, "'Tracker'!O9") == [["offer"]]


def test_resync_keeps_row_dirty_when_outcome_write_fails():
    svc = _service()
    values = svc.spreadsheets.return_value.values.return_value

    def _update(**kwargs):
        call = MagicMock()
        if kwargs["range"] == "'Tracker'!O10":
            call.execute.side_effect = RuntimeError("sheets 503")
        return call

    values.update.side_effect = _update
    synced, mock_clean = _resync(svc, [("abc12345", _dirty_row(outcome="rejected"), 10)])

    assert synced == 0
    mock_clean.assert_not_called()


def test_resync_blank_outcome_leaves_column_o_alone():
    """An empty DB value must not wipe a label the owner just typed into O."""
    svc = _service()
    synced, _ = _resync(svc, [("abc12345", _dirty_row(outcome=""), 10)])

    assert synced == 1
    assert not [r for r in _update_ranges(svc) if "!O" in r]


# ── push: the A–K writers never reach column O ───────────────────────────────


def test_a_to_k_push_never_touches_column_o():
    svc = _service()
    row = _dirty_row(outcome="interview")

    gsheets_client.update_row(svc, "SHEET", 10, row)
    gsheets_client.append_rows(svc, "SHEET", [row])
    gsheets_client.batch_write_all(svc, "SHEET", [row])

    values = svc.spreadsheets.return_value.values.return_value
    written = [c.kwargs for c in values.update.call_args_list] + [
        c.kwargs for c in values.append.call_args_list
    ]
    assert len(written) == 3
    for kwargs in written:
        assert not kwargs["range"].rstrip("0123456789").endswith("O")
        for line in kwargs["body"]["values"]:
            assert len(line) == gsheets_client.COL_COUNT
            assert "interview" not in line
    assert "Outcome" not in gsheets_client.COLUMNS


# ── pull: read_all sees column O ─────────────────────────────────────────────


def test_read_all_reads_through_column_o():
    header = gsheets_client.COLUMNS + ["Applied Date", "Cost $", "ATS Verdict", "Outcome"]
    full = ["2026-09-01", "Acme", "Dev", "Angular", "91%", "https://x", "f", "", "", "", "abc12345"]
    full += ["2026-09-02", "0.42", "91", "Interview"]
    short = ["2026-09-01", "Beta", "Dev", "", "", "https://y", "", "", "", "", "def67890"]
    svc = _service([header, full, short])

    rows = gsheets_client.read_all(svc, "SHEET")

    get_kwargs = svc.spreadsheets.return_value.values.return_value.get.call_args.kwargs
    assert get_kwargs["range"] == "'Tracker'!A:O"
    assert rows[0] == (2, {**rows[0][1], "ID": "abc12345", "Outcome": "Interview"})
    assert rows[1][1]["Outcome"] == ""


# ── pull: merge rule (pure) ──────────────────────────────────────────────────


def _merge(cell: str, db_label: str = "", dirty: bool = False) -> dict[str, str]:
    from hunter.gsheets_sync import _merge_outcomes

    return _merge_outcomes(
        [(5, {"ID": "abc12345", "Outcome": cell})], {"abc12345": (db_label, dirty)}
    )


def test_merge_takes_valid_sheet_label_into_empty_db_value():
    assert _merge("rejected") == {"abc12345": "rejected"}


def test_merge_normalises_case_and_whitespace():
    assert _merge("  Interview ") == {"abc12345": "interview"}


def test_merge_sheet_wins_over_an_older_db_label():
    assert _merge("offer", db_label="interview") == {"abc12345": "offer"}


def test_merge_ignores_garbage_cell_with_a_log_line(caplog):
    with caplog.at_level(logging.WARNING, logger="hunter.gsheets_sync"):
        assert _merge("called back maybe", db_label="interview") == {}
    assert "called back maybe" in caplog.text


def test_merge_blank_cell_never_clears_db():
    assert _merge("", db_label="silence") == {}


def test_merge_dirty_db_row_keeps_its_unpushed_value():
    assert _merge("silence", db_label="interview", dirty=True) == {}


def test_merge_unchanged_label_is_not_rewritten():
    assert _merge("interview", db_label="interview") == {}


def test_merge_skips_rows_not_in_db():
    from hunter.gsheets_sync import _merge_outcomes

    assert _merge_outcomes([(5, {"ID": "zzz99999", "Outcome": "offer"})], {}) == {}


# ── pull: end to end against a real tracker.db ───────────────────────────────


@pytest.fixture()
def db(tracker_db, monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_USER_ID", "u1")
    return tracker_db


def _insert(db, row_id: str, *, label: str = "", dirty: int = 0, sheets_row: int = 2) -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, url_norm, "
            "sent, outcome_label, sheets_row, sheets_dirty, user_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                "2026-09-01",
                "Acme",
                "Dev",
                "91%",
                f"https://example.com/{row_id}",
                f"example.com/{row_id}",
                "2026-09-02",
                label,
                sheets_row,
                dirty,
                "u1",
            ),
        )


def _db_row(db, row_id: str) -> dict:
    with get_db(db) as conn:
        return dict(conn.execute("SELECT * FROM applications WHERE id=?", (row_id,)).fetchone())


def _sheet_grid(*rows: tuple[str, str]) -> list[list[str]]:
    header = gsheets_client.COLUMNS + ["Applied Date", "Cost $", "ATS Verdict", "Outcome"]
    grid = [header]
    for row_id, outcome in rows:
        line = ["2026-09-01", "Acme", "Dev", "", "91%", "", "", "2026-09-02", "", "", row_id]
        grid.append(line + ["", "", "", outcome])
    return grid


def test_pull_writes_sheet_outcome_into_empty_db_value(db):
    from hunter.gsheets_sync import _apply_outcome_pull_db

    _insert(db, "aaaa1111")
    _insert(db, "bbbb2222", label="offer", sheets_row=3)
    _insert(db, "cccc3333", label="silence", sheets_row=4)
    svc = _service(_sheet_grid(("aaaa1111", "Rejected"), ("bbbb2222", "maybe??"), ("cccc3333", "")))

    updated = _apply_outcome_pull_db(gsheets_client.read_all(svc, "SHEET"))

    assert updated == 1
    first = _db_row(db, "aaaa1111")
    assert first["outcome_label"] == "rejected"
    assert first["outcome_at"], "a pulled outcome is stamped like /outcome stamps it"
    assert first["sheets_dirty"] == 0, "the Sheet already shows it — nothing to push back"
    assert _db_row(db, "bbbb2222")["outcome_label"] == "offer", "garbage never overwrites"
    assert _db_row(db, "cccc3333")["outcome_label"] == "silence", "blank never clears"


def test_outcome_round_trip_telegram_then_sheet(db):
    """/outcome → O written immediately; the owner's later Sheet edit comes back."""
    from hunter import gsheets_sync, tracker

    _insert(db, "dddd4444", sheets_row=6)
    assert tracker.set_outcome("dddd4444", "interview")
    svc = _service()
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=svc),
        patch("hunter.gsheets_sync._sheet_id", return_value="SHEET"),
    ):
        assert asyncio.run(gsheets_sync.mirror_outcome("dddd4444")) == 1
    assert _update_body_for(svc, "'Tracker'!O6") == [["interview"]]

    # Still dirty (the immediate write never marks clean), so a stale cell loses.
    stale = _service(_sheet_grid(("dddd4444", "silence")))
    assert gsheets_sync._apply_outcome_pull_db(gsheets_client.read_all(stale, "SHEET")) == 0

    tracker.mark_sheets_clean("dddd4444")
    edited = _service(_sheet_grid(("dddd4444", "offer")))
    assert gsheets_sync._apply_outcome_pull_db(gsheets_client.read_all(edited, "SHEET")) == 1
    assert _db_row(db, "dddd4444")["outcome_label"] == "offer"


def test_mirror_outcome_writes_blank_on_explicit_clear(db):
    from hunter import gsheets_sync, tracker

    _insert(db, "eeee5555", label="silence", sheets_row=8)
    assert tracker.set_outcome("eeee5555", "")
    svc = _service()
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=svc),
        patch("hunter.gsheets_sync._sheet_id", return_value="SHEET"),
    ):
        asyncio.run(gsheets_sync.mirror_outcome("eeee5555"))

    assert _update_body_for(svc, "'Tracker'!O8") == [[""]]


def test_mirror_outcome_is_scoped_to_the_callers_rows(db, monkeypatch):
    from hunter import gsheets_sync

    _insert(db, "ffff6666", label="offer", sheets_row=9)
    monkeypatch.setenv("JOB_HUNTER_USER_ID", "someone-else")
    svc = _service()
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=svc),
        patch("hunter.gsheets_sync._sheet_id", return_value="SHEET"),
    ):
        assert asyncio.run(gsheets_sync.mirror_outcome("ffff6666")) == 0
    assert _update_ranges(svc) == []


# ── column setup ─────────────────────────────────────────────────────────────


def test_header_and_dropdown_are_set_once_per_sheet():
    svc = _service()
    outcome_writer.write_outcome_cell_sync(svc, "SHEET", 4, "offer")
    outcome_writer.write_outcome_cell_sync(svc, "SHEET", 5, "silence")

    assert _update_ranges(svc).count("'Tracker'!O1") == 1
    batch = svc.spreadsheets.return_value.batchUpdate.call_args_list
    assert len(batch) == 1
    rule = batch[0].kwargs["body"]["requests"][0]["setDataValidation"]
    assert rule["range"] == {
        "sheetId": 777,
        "startRowIndex": 1,
        "startColumnIndex": 14,
        "endColumnIndex": 15,
    }
    listed = [v["userEnteredValue"] for v in rule["rule"]["condition"]["values"]]
    from hunter.tracker import OUTCOME_LABELS

    assert listed == list(OUTCOME_LABELS)


def test_dropdown_failure_does_not_block_the_cell_write():
    svc = _service()
    svc.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = RuntimeError("x")

    assert outcome_writer.write_outcome_cell_sync(svc, "SHEET", 4, "offer") is True
    assert _update_body_for(svc, "'Tracker'!O4") == [["offer"]]


def test_parse_outcome_cell():
    assert outcome_writer.parse_outcome_cell(" SILENCE ") == "silence"
    assert outcome_writer.parse_outcome_cell("") == ""
    assert outcome_writer.parse_outcome_cell(None) == ""
    assert outcome_writer.parse_outcome_cell("ghosted") is None
