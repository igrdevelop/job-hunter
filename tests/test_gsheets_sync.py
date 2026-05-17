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
    fake_cache = MagicMock()
    fake_cache.set_sheet_row_index = AsyncMock()
    fake_cache.mark_clean = AsyncMock()
    fake_cache.mark_dirty = AsyncMock()

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(return_value=[5])),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_new_row(_make_row()))

    fake_cache.set_sheet_row_index.assert_called_once_with("abc12345", 5)
    fake_cache.mark_clean.assert_called_once_with("abc12345")
    fake_cache.mark_dirty.assert_not_called()


def test_mirror_new_row_marks_dirty_on_exception():
    fake_cache = MagicMock()
    fake_cache.set_sheet_row_index = AsyncMock()
    fake_cache.mark_clean = AsyncMock()
    fake_cache.mark_dirty = AsyncMock()

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(side_effect=Exception("network"))),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_new_row(_make_row()))

    fake_cache.mark_dirty.assert_called_once_with("abc12345")
    fake_cache.mark_clean.assert_not_called()


# ── mirror_cell_update ────────────────────────────────────────────────────────

def test_mirror_cell_update_noop_when_not_ready():
    with patch("hunter.gsheets_sync._ready", return_value=False):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_cell_update("abc12345", "Sent", "EXPIRED"))


def test_mirror_cell_update_marks_dirty_when_no_sheet_row():
    fake_cache = MagicMock()
    fake_cache.sheet_row_index = {}
    fake_cache.mark_dirty = AsyncMock()

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_cell_update("abc12345", "Sent", "EXPIRED"))

    fake_cache.mark_dirty.assert_called_once_with("abc12345")


def test_mirror_cell_update_calls_update_cell():
    fake_cache = MagicMock()
    fake_cache.sheet_row_index = {"abc12345": 7}
    fake_cache.mark_clean = AsyncMock()
    fake_cache.mark_dirty = AsyncMock()

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(return_value=None)),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        run(gsheets_sync.mirror_cell_update("abc12345", "Sent", "EXPIRED"))

    fake_cache.mark_clean.assert_called_once_with("abc12345")
    fake_cache.mark_dirty.assert_not_called()


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
    fake_cache = MagicMock()
    fake_cache.dirty_rows = AsyncMock(return_value=[])

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())

    assert result == 0


def test_resync_dirty_appends_row_without_sheet_row():
    fake_cache = MagicMock()
    row = _make_row("newrow1")
    fake_cache.dirty_rows = AsyncMock(return_value=[("newrow1", row, None)])
    fake_cache.set_sheet_row_index = AsyncMock()
    fake_cache.mark_clean = AsyncMock()

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(return_value=[8])),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())

    assert result == 1
    fake_cache.set_sheet_row_index.assert_called_once_with("newrow1", 8)
    fake_cache.mark_clean.assert_called_once_with("newrow1")


def test_resync_dirty_updates_existing_sheet_row():
    fake_cache = MagicMock()
    row = _make_row("existing1")
    fake_cache.dirty_rows = AsyncMock(return_value=[("existing1", row, 10)])
    fake_cache.mark_clean = AsyncMock()

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(return_value=None)),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())

    assert result == 1
    fake_cache.mark_clean.assert_called_once_with("existing1")


def test_resync_dirty_counts_failures():
    fake_cache = MagicMock()
    row = _make_row("fail1")
    fake_cache.dirty_rows = AsyncMock(return_value=[("fail1", row, None)])
    fake_cache.mark_clean = AsyncMock()

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(side_effect=Exception("timeout"))),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.resync_dirty())

    assert result == 0
    fake_cache.mark_clean.assert_not_called()


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
    fake_cache = MagicMock()
    fake_cache.dirty_rows = AsyncMock(return_value=[])

    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", False),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", ""),
        patch("hunter.gsheets_sync._service", None),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        report = run(gsheets_sync.status_report())

    assert report["enabled"] is False
    assert report["dirty_count"] == 0


def test_status_report_enabled_with_sheet():
    fake_cache = MagicMock()
    fake_cache.dirty_rows = AsyncMock(return_value=[("id1", {}, None)])

    with (
        patch("hunter.gsheets_sync.GSHEETS_ENABLED", True),
        patch("hunter.gsheets_sync.GSHEETS_TRACKER_ID", "sheet123"),
        patch("hunter.gsheets_sync._service", MagicMock()),
        patch("hunter.gsheets_sync.cache", fake_cache),
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
    assert result == {"pulled": 0, "updated": 0, "errors": []}


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
    fake_cache = MagicMock()
    fake_cache.set_sheet_row_index = AsyncMock()
    fake_cache.apply_pull_delta = AsyncMock(return_value=[])

    sheets_rows = [(2, {"ID": "abc12345", "Sent": "2026-05-01", "To Learn": "RxJS", "Re-application": ""})]

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", new=AsyncMock(return_value=sheets_rows)),
        patch("hunter.gsheets_sync.cache", fake_cache),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.pull_full_snapshot())

    assert result["pulled"] == 1
    assert result["updated"] == 0
    assert result["errors"] == []
    fake_cache.set_sheet_row_index.assert_called_once_with("abc12345", 2)


def test_pull_full_snapshot_writes_excel_on_changes():
    fake_cache = MagicMock()
    fake_cache.set_sheet_row_index = AsyncMock()
    changed_row = _make_row("abc12345")
    changed_row["Sent"] = "2026-05-10"
    fake_cache.apply_pull_delta = AsyncMock(return_value=[changed_row])

    sheets_rows = [(2, {"ID": "abc12345", "Sent": "2026-05-10", "To Learn": "", "Re-application": ""})]
    excel_call_results = [sheets_rows, 1]  # first call = read_all, second = apply_pull_updates

    call_count = 0

    async def fake_to_thread(fn, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return sheets_rows
        return 1  # apply_pull_updates return value

    with (
        patch("hunter.gsheets_sync._ready", return_value=True),
        patch("hunter.gsheets_sync._get_service", return_value=MagicMock()),
        patch("hunter.gsheets_sync._sheet_id", return_value="sheet123"),
        patch("hunter.gsheets_sync.asyncio.to_thread", side_effect=fake_to_thread),
        patch("hunter.gsheets_sync.cache", fake_cache),
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
        patch("hunter.tracker.read_all_tracker_rows", return_value=tracker_data),
        patch("hunter.tracker_cache.cache.set_sheet_row_index", new_callable=AsyncMock),
        patch("hunter.tracker_cache.cache.mark_clean", new_callable=AsyncMock),
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
        patch("hunter.tracker.read_all_tracker_rows", return_value=tracker_data),
        patch("hunter.tracker_cache.cache.set_sheet_row_index", new_callable=AsyncMock),
        patch("hunter.tracker_cache.cache.mark_clean", new_callable=AsyncMock),
    ):
        from hunter import gsheets_sync
        result = run(gsheets_sync.push_missing_rows())

    assert result["pushed"] == 0
    assert result["already_present"] == 2
    mock_append.assert_not_called()
