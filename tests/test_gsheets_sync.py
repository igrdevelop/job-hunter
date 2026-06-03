"""
tests/test_gsheets_sync.py — Unit tests for hunter/gsheets_sync.py.

All tests are fully mocked — no network, no Sheets API calls.
Uses synchronous asyncio.run() wrappers (no pytest-asyncio dependency).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def run(coro):
    return asyncio.run(coro)


def _make_row(row_id: str = "abc12345") -> dict:
    return {
        "Date": "2026-05-14",
        "Company": "Acme",
        "Job Title": "Senior Frontend Developer",
        "Stack": "Angular",
        "ATS %": "85",
        "URL": "https://example.com/jobs/1",
        "Folder": "Applications/2026-05-14/Acme",
        "Sent": "",
        "Re-application": "",
        "To Learn": "",
        "ID": row_id,
    }


# ── _ready() and no-op behaviour ─────────────────────────────────────────────

def test_ready_false_when_disabled():
    with patch("hunter.gsheets_sync.GSHEETS_ENABLED", False):
        from hunter import gsheets_sync
        assert not gsheets_sync._ready()


def test_ready_false_when_no_sheet_id():
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", ""),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
    ):
        from hunter import gsheets_sync
        assert not gsheets_sync._ready()


def test_ready_false_when_no_service():
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", "sheet123"),
        patch("hunter.gsheets_sync._get_service", return_value=None),
    ):
        from hunter import gsheets_sync
        assert not gsheets_sync._ready()


def test_ready_true_when_all_set():
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", "sheet123"),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
    ):
        from hunter import gsheets_sync
        assert gsheets_sync._ready()


# ── mirror_new_row ────────────────────────────────────────────────────────────

def test_mirror_new_row_noop_when_disabled():
    with patch("hunter.gsheets_sync._ready", return_value=False):
        from hunter import gsheets_sync
        # Should return without error
        run(gsheets_sync.mirror_new_row(_make_row()))


def test_mirror_new_row_noop_when_no_id():
    with patch("hunter.gsheets_sync._ready", return_value=True):
        from hunter import gsheets_sync
        row = _make_row()
        row["ID"] = ""
        run(gsheets_sync.mirror_new_row(row))


def test_mirror_new_row_appends_row_and_caches_index():
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(return_value=[5])),
        patch("hunter.gsheets_sync.set_sheets_row") as mock_set_row,
        patch("hunter.gsheets_sync.mark_sheets_clean") as mock_clean,
        patch("hunter.gsheets_sync.mark_sheets_dirty") as mock_dirty,
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_new_row(_make_row()))

    mock_set_row.assert_called_once_with("abc12345", 5)
    mock_clean.assert_called_once_with("abc12345")
    mock_dirty.assert_not_called()


def test_mirror_new_row_marks_dirty_on_exception():
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(side_effect=Exception("network"))),
        patch("hunter.gsheets_sync.mark_sheets_dirty") as mock_dirty,
        patch("hunter.gsheets_sync.mark_sheets_clean") as mock_clean,
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_new_row(_make_row()))

    mock_dirty.assert_called_once_with("abc12345")
    mock_clean.assert_not_called()


# ── mirror_cell_update ────────────────────────────────────────────────────────

def test_mirror_cell_update_noop_when_not_ready():
    with patch("hunter.gsheets_sync._ready", return_value=False):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_cell_update("abc12345", "Sent", "EXPIRED"))


def test_mirror_cell_update_marks_dirty_when_no_sheet_row():
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync.get_sheets_row", return_value=None),
        patch("hunter.gsheets_sync.mark_sheets_dirty") as mock_dirty,
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_cell_update("abc12345", "Sent", "EXPIRED"))

    mock_dirty.assert_called_once_with("abc12345")


def test_mirror_cell_update_calls_update_cell():
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.get_sheets_row", return_value=7),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(return_value=None)),
        patch("hunter.gsheets_sync.mark_sheets_clean") as mock_clean,
        patch("hunter.gsheets_sync.mark_sheets_dirty") as mock_dirty,
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_cell_update("abc12345", "Sent", "EXPIRED"))

    mock_clean.assert_called_once_with("abc12345")
    mock_dirty.assert_not_called()


# ── mirror_expired_batch ──────────────────────────────────────────────────────

def test_mirror_expired_batch_noop_empty():
    with patch("hunter.gsheets_sync._ready", return_value=True):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_expired_batch(set()))


def test_mirror_expired_batch_calls_cell_update_for_each():
    calls = []

    async def fake_update(row_id, col, val):
        calls.append((row_id, col, val))

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync.mirror_cell_update", side_effect=fake_update),
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_expired_batch({"id1", "id2"}))

    assert len(calls) == 2
    for row_id, col, val in calls:
        assert col == "Sent"
        assert val == "EXPIRED"


# ── resync_dirty ──────────────────────────────────────────────────────────────

def test_resync_dirty_noop_when_not_ready():
    with patch("hunter.gsheets_sync._ready", return_value=False):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())
    assert result == 0


def test_resync_dirty_returns_zero_when_no_dirty_rows():
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync.get_dirty_rows_for_sheets", return_value=[]),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())

    assert result == 0


def test_resync_dirty_appends_row_without_sheet_row():
    row = _make_row("newrow1")

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.get_dirty_rows_for_sheets", return_value=[("newrow1", row, None)]),
        patch("hunter.gsheets_client.append_rows", return_value=[8]),
        patch("hunter.gsheets_sync.set_sheets_row") as mock_set_row,
        patch("hunter.gsheets_sync.mark_sheets_clean") as mock_clean,
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())

    assert result == 1
    mock_set_row.assert_called_once_with("newrow1", 8)
    mock_clean.assert_called_once_with("newrow1")


def test_resync_dirty_updates_existing_sheet_row():
    row = _make_row("existing1")

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.get_dirty_rows_for_sheets", return_value=[("existing1", row, 10)]),
        patch("hunter.gsheets_client.update_row", return_value=None),
        patch("hunter.gsheets_sync.mark_sheets_clean") as mock_clean,
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())

    assert result == 1
    mock_clean.assert_called_once_with("existing1")


def test_resync_dirty_counts_failures():
    row = _make_row("fail1")

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.get_dirty_rows_for_sheets", return_value=[("fail1", row, None)]),
        patch("hunter.gsheets_client.append_rows", side_effect=Exception("timeout")),
        patch("hunter.gsheets_sync.mark_sheets_clean") as mock_clean,
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())

    assert result == 0
    mock_clean.assert_not_called()


# ── validate_startup ──────────────────────────────────────────────────────────

def test_validate_startup_ok_when_disabled():
    with patch("hunter.gsheets_sync.GSHEETS_ENABLED", False):
        from hunter import gsheets_sync
        result = gsheets_sync.validate_startup()
    assert result["ok"] is True
    assert result["error"] is None


def test_validate_startup_error_missing_credentials(tmp_path):
    missing = tmp_path / "missing_creds.json"
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_CREDENTIALS_FILE", missing),
    ):
        from hunter import gsheets_sync
        result = gsheets_sync.validate_startup()
    assert result["ok"] is False
    assert "gsheets_credentials.json" in result["error"]


def test_validate_startup_error_missing_token(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    missing_token = tmp_path / "missing_token.json"
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_CREDENTIALS_FILE", creds),
        patch("hunter.gsheets_sync.GSHEETS_TOKEN_FILE", missing_token),
    ):
        from hunter import gsheets_sync
        result = gsheets_sync.validate_startup()
    assert result["ok"] is False
    assert "gsheets_token.json" in result["error"]


def test_validate_startup_ok_no_tracker_id(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    token = tmp_path / "token.json"
    token.write_text("{}")
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_CREDENTIALS_FILE", creds),
        patch("hunter.gsheets_sync.GSHEETS_TOKEN_FILE", token),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", ""),
    ):
        from hunter import gsheets_sync
        result = gsheets_sync.validate_startup()
    assert result["ok"] is True
    assert "warning" in result


def test_validate_startup_ok_sheet_accessible(tmp_path):
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    token = tmp_path / "token.json"
    token.write_text("{}")
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_CREDENTIALS_FILE", creds),
        patch("hunter.gsheets_sync.GSHEETS_TOKEN_FILE", token),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", "sheet123"),
    ):
        from hunter import gsheets_sync
        with patch("hunter.gsheets_client.read_all", return_value=[]):
            result = gsheets_sync.validate_startup()
    assert result["ok"] is True
    assert result["sheet_url"] is not None


# ── status_report ─────────────────────────────────────────────────────────────

def test_status_report_disabled():
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", False),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", ""),
        patch("hunter.gsheets_sync._service", None),
        patch("hunter.gsheets_sync.get_dirty_sheets_count", return_value=0),
    ):
        from hunter import gsheets_sync
        report = run(gsheets_sync.status_report())

    assert report["enabled"] is False
    assert report["dirty_count"] == 0


def test_status_report_enabled_with_sheet():
    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", "sheet123"),
        patch("hunter.gsheets_sync._service", MagicMock()),
        patch("hunter.gsheets_sync.get_dirty_sheets_count", return_value=1),
    ):
        from hunter import gsheets_sync
        report = run(gsheets_sync.status_report())

    assert report["enabled"] is True
    assert report["sheet_id"] == "sheet123"
    assert "sheet123" in report["sheet_url"]
    assert report["dirty_count"] == 1
    assert report["service_ok"] is True


# ── pull_full_snapshot ────────────────────────────────────────────────────────

def test_pull_full_snapshot_noop_when_not_ready():
    with patch("hunter.gsheets_sync._ready", return_value=False):
        from hunter import gsheets_sync
        result = run(gsheets_sync.pull_full_snapshot())
    assert result == {"pulled": 0, "inserted": 0, "updated": 0, "errors": []}


def test_pull_full_snapshot_read_all_error():
    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(side_effect=Exception("api error"))),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.pull_full_snapshot())
    assert result["pulled"] == 0
    assert result["errors"]


def test_pull_full_snapshot_no_changes():
    sheets_rows = [(2, {"ID": "abc12345", "Sent": "2026-05-01", "To Learn": "RxJS", "Re-application": ""})]

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_client.read_all", return_value=sheets_rows),
        patch("hunter.gsheets_sync.insert_pulled_rows", return_value=0),
        patch("hunter.gsheets_sync._apply_pull_delta_db", return_value=[]) as mock_delta,
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.pull_full_snapshot())

    assert result["pulled"] == 1
    assert result["inserted"] == 0
    assert result["updated"] == 0
    assert result["errors"] == []
    mock_delta.assert_called_once_with(sheets_rows)


def test_pull_full_snapshot_writes_db_on_changes():
    changed_row = _make_row("abc12345")
    changed_row["Sent"] = "2026-05-10"
    sheets_rows = [(2, {"ID": "abc12345", "Sent": "2026-05-10", "To Learn": "", "Re-application": ""})]

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_client.read_all", return_value=sheets_rows),
        patch("hunter.gsheets_sync.insert_pulled_rows", return_value=0),
        patch("hunter.gsheets_sync._apply_pull_delta_db", return_value=[changed_row]),
        patch("hunter.gsheets_sync.apply_pull_updates", return_value=1),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.pull_full_snapshot())

    assert result["pulled"] == 1
    assert result["updated"] == 1
    assert result["errors"] == []


# ── init_or_load_spreadsheet ──────────────────────────────────────────────────

def test_init_or_load_noop_when_disabled():
    with patch("hunter.gsheets_sync.GSHEETS_ENABLED", False):
        from hunter import gsheets_sync
        result = run(gsheets_sync.init_or_load_spreadsheet())
    assert result["sheet_id"] == ""
    assert not result["created"]


def test_init_or_load_from_state_file(tmp_path):
    import importlib
    state_file = tmp_path / "gsheets_state.json"
    state_file.write_text('{"sheet_id": "fromfile123"}')

    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_STATE_FILE", state_file),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", ""),
    ):
        from hunter import gsheets_sync
        gsheets_sync._state = {}  # reset state
        result = run(gsheets_sync.init_or_load_spreadsheet())

    assert result["sheet_id"] == "fromfile123"
    assert not result["created"]
    assert "fromfile123" in result["sheet_url"]


def test_init_or_load_from_env_when_no_state_file(tmp_path):
    missing_state = tmp_path / "no_state.json"

    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_STATE_FILE", missing_state),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", "envsheet999"),
    ):
        from hunter import gsheets_sync
        gsheets_sync._state = {}
        result = run(gsheets_sync.init_or_load_spreadsheet())

    assert result["sheet_id"] == "envsheet999"
    assert not result["created"]
    assert missing_state.exists()  # state was saved


def test_init_or_load_creates_new_sheet_when_no_id(tmp_path):
    missing_state = tmp_path / "no_state.json"
    notify_calls = []

    async def fake_notify(text: str):
        notify_calls.append(text)

    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_STATE_FILE", missing_state),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", ""),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(return_value="newsheet456")),
    ):
        from hunter import gsheets_sync
        gsheets_sync._state = {}
        result = run(gsheets_sync.init_or_load_spreadsheet(notify_cb=fake_notify))

    assert result["sheet_id"] == "newsheet456"
    assert result["created"] is True
    assert "newsheet456" in result["sheet_url"]
    assert notify_calls  # Telegram message was sent
    assert missing_state.exists()
    import json as _json
    saved = _json.loads(missing_state.read_text())
    assert saved["sheet_id"] == "newsheet456"


# ── _read_state / _write_state ────────────────────────────────────────────────

def test_read_state_missing_file(tmp_path):
    with patch("hunter.gsheets_sync.GSHEETS_STATE_FILE", tmp_path / "none.json"):
        from hunter import gsheets_sync
        assert gsheets_sync._read_state() == {}


def test_write_and_read_state(tmp_path):
    state_file = tmp_path / "state.json"
    with patch("hunter.gsheets_sync.GSHEETS_STATE_FILE", state_file):
        from hunter import gsheets_sync
        gsheets_sync._write_state({"sheet_id": "test123"})
        result = gsheets_sync._read_state()
    assert result == {"sheet_id": "test123"}


# ── push_missing_rows ─────────────────────────────────────────────────────────

def _tracker_row(row_id: str, company: str = "Acme") -> dict:
    return {
        "Date": "2026-05-15", "Company": company, "Job Title": "Dev",
        "Stack": "Angular", "ATS %": "90", "URL": f"https://x.com/{row_id}",
        "Folder": "", "Sent": "", "Re-application": "", "To Learn": "", "ID": row_id,
    }


def test_push_missing_rows_noop_when_not_ready():
    with patch("hunter.gsheets_sync.GSHEETS_ENABLED", False):
        from hunter import gsheets_sync
        result = run(gsheets_sync.push_missing_rows())
    assert result["pushed"] == 0
    assert "not ready" in result["errors"][0].lower()


def test_push_missing_rows_pushes_absent_rows():
    # Sheets has row "aaa", tracker has "aaa" + "bbb" → should push only "bbb"
    sheets_data = [(2, _tracker_row("aaa", "Existing"))]
    tracker_data = [_tracker_row("aaa", "Existing"), _tracker_row("bbb", "Missing")]

    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_client.read_all", return_value=sheets_data),
        patch("hunter.gsheets_client.append_rows", return_value=[3]) as mock_append,
        patch("hunter.gsheets_sync.read_all_tracker_rows", return_value=tracker_data),
        patch("hunter.gsheets_sync.set_sheets_row"),
        patch("hunter.gsheets_sync.mark_sheets_clean"),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.push_missing_rows())

    assert result["pushed"] == 1
    assert result["already_present"] == 1
    assert result["errors"] == []
    pushed_rows = mock_append.call_args.args[2]
    assert len(pushed_rows) == 1
    assert pushed_rows[0]["ID"] == "bbb"


def test_push_missing_rows_nothing_to_push_when_all_present():
    tracker_data = [_tracker_row("aaa"), _tracker_row("bbb")]
    sheets_data = [(2, _tracker_row("aaa")), (3, _tracker_row("bbb"))]

    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_client.read_all", return_value=sheets_data),
        patch("hunter.gsheets_client.append_rows") as mock_append,
        patch("hunter.gsheets_sync.read_all_tracker_rows", return_value=tracker_data),
        patch("hunter.gsheets_sync.set_sheets_row"),
        patch("hunter.gsheets_sync.mark_sheets_clean"),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.push_missing_rows())

    assert result["pushed"] == 0
    assert result["already_present"] == 2
    mock_append.assert_not_called()


# ── _apply_pull_delta_db — conflict matrix ────────────────────────────────────

def _db_row(row_id: str = "abc12345", **overrides) -> dict:
    """Build a minimal tracker row dict for conflict-matrix tests."""
    base = {
        "ID": row_id, "Company": "Acme", "Job Title": "Dev",
        "URL": f"https://x.com/{row_id}", "Sent": "", "To Learn": "",
        "Re-application": "", "Stack": "Angular", "ATS %": "80",
        "Date": "2026-05-01", "Folder": "", "Drive URL": "",
    }
    base.update(overrides)
    return base


class TestApplyPullDeltaDB:
    """Conflict matrix for _apply_pull_delta_db (moved from TrackerCache in Phase 5.5)."""

    def _run(self, db_rows, sheets_rows):
        """Call _apply_pull_delta_db with mocked DB and set_sheets_row."""
        from hunter.gsheets_sync import _apply_pull_delta_db
        with (
            patch("hunter.gsheets_sync.read_all_tracker_rows", return_value=db_rows),
            patch("hunter.gsheets_sync.set_sheets_row"),
        ):
            return _apply_pull_delta_db(sheets_rows)

    def test_no_changes_when_identical(self):
        row = _db_row(Sent="2026-05-01")
        to_write = self._run([row], [(2, dict(row))])
        assert to_write == []

    def test_user_adds_sent_date(self):
        """DB Sent="", Sheets Sent="2026-05-14" → trust Sheets."""
        row = _db_row(Sent="")
        sheet_row = {**row, "Sent": "2026-05-14"}
        to_write = self._run([row], [(2, sheet_row)])
        assert len(to_write) == 1
        assert to_write[0]["Sent"] == "2026-05-14"

    def test_bot_expired_wins_over_empty_sheets(self):
        """DB=EXPIRED, Sheets empty → keep EXPIRED, no write."""
        row = _db_row(Sent="EXPIRED")
        sheet_row = {**row, "Sent": ""}
        to_write = self._run([row], [(2, sheet_row)])
        assert to_write == []

    def test_user_sent_beats_expired(self):
        """DB=EXPIRED, Sheets has user date → trust Sheets (edge case)."""
        row = _db_row(Sent="EXPIRED")
        sheet_row = {**row, "Sent": "2026-05-10"}
        to_write = self._run([row], [(2, sheet_row)])
        assert len(to_write) == 1
        assert to_write[0]["Sent"] == "2026-05-10"

    def test_user_erases_sent(self):
        """DB has date, Sheets empty (user erased) → trust Sheets."""
        row = _db_row(Sent="2026-05-01")
        sheet_row = {**row, "Sent": ""}
        to_write = self._run([row], [(2, sheet_row)])
        assert len(to_write) == 1
        assert to_write[0]["Sent"] == ""

    def test_user_updates_to_learn(self):
        row = _db_row()
        sheet_row = {**row, "To Learn": "RxJS"}
        to_write = self._run([row], [(2, sheet_row)])
        assert len(to_write) == 1
        assert to_write[0]["To Learn"] == "RxJS"

    def test_user_updates_re_application(self):
        row = _db_row()
        sheet_row = {**row, "Re-application": "+"}
        to_write = self._run([row], [(2, sheet_row)])
        assert len(to_write) == 1
        assert to_write[0]["Re-application"] == "+"

    def test_set_sheets_row_called_for_matched_row(self):
        """sheets_row index should be persisted for every matched row."""
        row = _db_row()
        from hunter.gsheets_sync import _apply_pull_delta_db
        with (
            patch("hunter.gsheets_sync.read_all_tracker_rows", return_value=[row]),
            patch("hunter.gsheets_sync.set_sheets_row") as mock_set,
        ):
            _apply_pull_delta_db([(7, dict(row))])
        mock_set.assert_called_once_with("abc12345", 7)

    def test_missing_id_in_sheets_ignored(self):
        """Sheets rows without matching DB ID are silently skipped."""
        row = _db_row()
        sheet_row = {**row, "ID": "unknownid"}
        to_write = self._run([row], [(2, sheet_row)])
        assert to_write == []
