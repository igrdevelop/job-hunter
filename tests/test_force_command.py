"""Tests for /force two-step flow and cleanup logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def run(coro):
    """Helper: run a coroutine synchronously."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# tracker.delete_all_by_url
# ---------------------------------------------------------------------------

def test_delete_all_by_url_removes_all_statuses(tracker_db):
    """delete_all_by_url should delete FAIL, SKIP, MANUAL, and success rows."""
    from hunter.tracker import delete_all_by_url, normalize_url
    from hunter.db import get_db

    url1 = "https://example.com/job/1"
    url2 = "https://other.com/job/2"
    norm1 = normalize_url(url1)
    norm2 = normalize_url(url2)

    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(id, date, company, title, stack, ats_status, url, url_norm, folder, drive_url) "
            "VALUES ('aaa11111', '2026-05-10', 'AcmeCorp', 'Dev', 'Angular', '95', ?, ?, ?, ?)",
            (url1, norm1,
             "Applications/2026-05-10/AcmeCorp",
             "https://drive.google.com/drive/folders/FOLDER1"),
        )
        conn.execute(
            "INSERT INTO applications "
            "(id, date, company, title, stack, ats_status, url, url_norm, folder, drive_url) "
            "VALUES ('bbb22222', '2026-05-11', 'OtherCo', 'Dev', 'React', '87', ?, ?, ?, '')",
            (url2, norm2, "Applications/2026-05-11/OtherCo"),
        )
        conn.execute(
            "INSERT INTO applications "
            "(id, date, company, title, stack, ats_status, url, url_norm, folder) "
            "VALUES ('ccc33333', '2026-05-12', 'AcmeCorp', 'Dev', 'Angular', 'FAIL', ?, ?, '')",
            (url1, norm1),
        )

    result = delete_all_by_url(url1)

    assert result["deleted"] == 2
    assert result["folder"] == "Applications/2026-05-10/AcmeCorp"
    assert result["drive_url"] == "https://drive.google.com/drive/folders/FOLDER1"

    # Only OtherCo row should remain
    with get_db(tracker_db) as conn:
        rows = conn.execute(
            "SELECT company FROM applications ORDER BY rowid"
        ).fetchall()
    assert [r["company"] for r in rows] == ["OtherCo"]


def test_delete_all_by_url_unknown_url(tracker_db):
    """Returns zero deleted when URL not found."""
    from hunter.tracker import delete_all_by_url, normalize_url
    from hunter.db import get_db

    url1 = "https://example.com/job/1"
    norm1 = normalize_url(url1)

    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications "
            "(id, date, company, title, stack, ats_status, url, url_norm, folder) "
            "VALUES ('aaa11111', '2026-05-10', 'AcmeCorp', 'Dev', 'Angular', '95', ?, ?, 'Applications/AcmeCorp')",
            (url1, norm1),
        )

    result = delete_all_by_url("https://other.com/job/999")

    assert result["deleted"] == 0
    assert result["folder"] is None
    assert result["drive_url"] is None


# ---------------------------------------------------------------------------
# tracker_cache.invalidate_url
# ---------------------------------------------------------------------------

def test_cache_invalidate_url_removes_from_all_indexes():
    from hunter.tracker_cache import TrackerCache

    async def _run():
        c = TrackerCache()
        row = {
            "ID": "abc123", "URL": "https://example.com/job/1",
            "Company": "AcmeCorp", "Job Title": "Dev",
            "ATS %": "95", "Sent": "", "Stack": "Angular",
            "Folder": "Applications/AcmeCorp", "Re-application": "",
            "To Learn": "", "Drive URL": "",
        }
        await c.add(row)
        assert await c.is_known_url("https://example.com/job/1")
        await c.invalidate_url("https://example.com/job/1")
        assert not await c.is_known_url("https://example.com/job/1")
        assert "abc123" not in c.rows

    run(_run())


def test_cache_invalidate_url_noop_unknown():
    from hunter.tracker_cache import TrackerCache

    async def _run():
        c = TrackerCache()
        await c.invalidate_url("https://notintracker.com/job/1")  # Should not raise

    run(_run())


# ---------------------------------------------------------------------------
# gdrive_client.folder_id_from_url
# ---------------------------------------------------------------------------

def test_folder_id_from_url_valid():
    from hunter.gdrive_client import folder_id_from_url
    url = "https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
    assert folder_id_from_url(url) == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"


def test_folder_id_from_url_invalid():
    from hunter.gdrive_client import folder_id_from_url
    assert folder_id_from_url("https://google.com") is None
    assert folder_id_from_url("") is None
    assert folder_id_from_url(None) is None


# ---------------------------------------------------------------------------
# _force_waiting state in telegram_bot
# ---------------------------------------------------------------------------

def _make_update(text: str, chat_id: int = 12345):
    update = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = chat_id
    return update


def test_cmd_force_bare_adds_to_waiting():
    """Bare /force with no args → adds chat to _force_waiting, sends prompt."""
    import hunter.telegram_bot as bot

    async def _run():
        bot._force_waiting.clear()
        update = _make_update("/force")
        context = MagicMock()
        await bot.cmd_force(update, context)
        assert 12345 in bot._force_waiting
        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args[0][0]
        assert "Force mode" in msg or "force" in msg.lower()

    run(_run())


def test_cmd_url_force_waiting_triggers_force_run():
    """Message from a chat in _force_waiting → calls _force_run, clears waiting."""
    import hunter.telegram_bot as bot

    async def _run():
        bot._force_waiting.clear()
        bot._force_waiting.add(12345)
        update = _make_update("https://justjoin.it/job-offer/some-company-dev")

        # After the refactor, cmd_url lives in url_message.py and uses its own
        # local import of _force_run from hunter.commands.force.  Patching
        # bot._force_run only affects the telegram_bot re-export; it does NOT
        # intercept the call inside url_message.  Patch the attribute where it
        # is actually looked up at call time.
        with patch("hunter.commands.url_message._force_run", new_callable=AsyncMock) as mock_run:
            context = MagicMock()
            await bot.cmd_url(update, context)

        assert 12345 not in bot._force_waiting
        mock_run.assert_called_once()

    run(_run())


def test_cmd_url_no_force_waiting_normal_flow():
    """Message from chat NOT in _force_waiting → normal flow (not _force_run)."""
    import hunter.telegram_bot as bot

    async def _run():
        bot._force_waiting.clear()
        update = _make_update("hello world")

        with patch.object(bot, "_force_run", new_callable=AsyncMock) as mock_run:
            with patch.object(bot, "_looks_like_paste", return_value=False):
                context = MagicMock()
                await bot.cmd_url(update, context)

        mock_run.assert_not_called()

    run(_run())


# ---------------------------------------------------------------------------
# _force_cleanup
# ---------------------------------------------------------------------------

def test_force_cleanup_deletes_server_folder(tmp_path):
    """_force_cleanup removes server folder when it exists."""
    import hunter.telegram_bot as bot

    async def _run():
        app_folder = tmp_path / "Applications" / "2026-05-22" / "AcmeCorp"
        app_folder.mkdir(parents=True)
        (app_folder / "CV.pdf").write_text("dummy")

        tracker_result = {"deleted": 1, "folder": str(app_folder), "drive_url": ""}
        update = _make_update("dummy")

        with patch("hunter.tracker.delete_all_by_url", return_value=tracker_result):
            with patch("hunter.tracker_cache.cache.invalidate_url", new_callable=AsyncMock):
                summary = await bot._force_cleanup("https://example.com/job/1", update)

        assert not app_folder.exists()
        assert "deleted" in summary.lower() or "AcmeCorp" in summary

    run(_run())


def test_force_cleanup_no_existing_entry():
    """_force_cleanup handles gracefully when URL is not in tracker."""
    import hunter.telegram_bot as bot

    async def _run():
        tracker_result = {"deleted": 0, "folder": None, "drive_url": None}
        update = _make_update("dummy")

        with patch("hunter.tracker.delete_all_by_url", return_value=tracker_result):
            with patch("hunter.tracker_cache.cache.invalidate_url", new_callable=AsyncMock):
                summary = await bot._force_cleanup("https://new-vacancy.com/job/99", update)

        assert "no existing" in summary.lower() or "0 row" in summary.lower() or "not found" in summary.lower()

    run(_run())


# ---------------------------------------------------------------------------
# gsheets_client — delete_sheet_row / get_tab_sheet_id
# ---------------------------------------------------------------------------

def _make_sheets_service(tab_sheet_id=999):
    """Build a minimal mock Sheets service for delete tests."""
    svc = MagicMock()
    # spreadsheets().get().execute() returns sheet metadata
    meta = {
        "sheets": [
            {"properties": {"title": "Tracker", "sheetId": tab_sheet_id}},
        ]
    }
    svc.spreadsheets().get().execute.return_value = meta
    # spreadsheets().batchUpdate().execute() returns {}
    svc.spreadsheets().batchUpdate().execute.return_value = {}
    return svc


def test_get_tab_sheet_id_found():
    from hunter.gsheets_client import get_tab_sheet_id
    svc = _make_sheets_service(tab_sheet_id=42)
    result = get_tab_sheet_id(svc, "spreadsheet-id-123")
    assert result == 42


def test_get_tab_sheet_id_not_found():
    from hunter.gsheets_client import get_tab_sheet_id
    svc = MagicMock()
    svc.spreadsheets().get().execute.return_value = {
        "sheets": [{"properties": {"title": "OtherTab", "sheetId": 0}}]
    }
    result = get_tab_sheet_id(svc, "spreadsheet-id-123", tab="Tracker")
    assert result is None


def test_delete_sheet_row_calls_batch_update():
    """delete_sheet_row issues a deleteDimension batchUpdate with correct 0-based index."""
    from hunter.gsheets_client import delete_sheet_row
    svc = _make_sheets_service(tab_sheet_id=7)

    delete_sheet_row(svc, "sheet-id", row_idx=5)

    # Capture the body passed to batchUpdate
    call_kwargs = svc.spreadsheets().batchUpdate.call_args
    body = call_kwargs[1]["body"] if call_kwargs[1] else call_kwargs[0][1]
    req = body["requests"][0]["deleteDimension"]["range"]
    assert req["sheetId"] == 7
    assert req["startIndex"] == 4   # row_idx=5 → 0-based 4
    assert req["endIndex"] == 5
    assert req["dimension"] == "ROWS"


# ---------------------------------------------------------------------------
# gsheets_sync — delete_row_by_url
# ---------------------------------------------------------------------------

def test_delete_row_by_url_success():
    """delete_row_by_url returns True and calls delete_sheet_row when URL is in DB."""
    import hunter.gsheets_sync as gsync

    async def _run():
        with patch.object(gsync, "_ready", return_value=True), \
             patch.object(gsync, "_get_service", return_value=MagicMock()), \
             patch.object(gsync, "_sheet_id", return_value="sheet-id-abc"), \
             patch.object(gsync, "lookup_url", return_value=[{"id": "abc123"}]), \
             patch.object(gsync, "get_sheets_row", return_value=5), \
             patch("hunter.gsheets_client.delete_sheet_row") as mock_del:
            result = await gsync.delete_row_by_url("https://example.com/job/1")

        assert result is True
        # Confirm delete_sheet_row was called with correct row index
        mock_del.assert_called_once()
        _, _, row_idx = mock_del.call_args[0]
        assert row_idx == 5

    run(_run())


def test_delete_row_by_url_not_in_db():
    """delete_row_by_url returns False when URL is not in tracker DB."""
    import hunter.gsheets_sync as gsync

    async def _run():
        with patch.object(gsync, "_ready", return_value=True), \
             patch.object(gsync, "lookup_url", return_value=[]):
            result = await gsync.delete_row_by_url("https://notfound.com/job/1")
        assert result is False

    run(_run())


def test_delete_row_by_url_no_sheet_index():
    """delete_row_by_url returns False when row has no sheets_row recorded in DB."""
    import hunter.gsheets_sync as gsync

    async def _run():
        with patch.object(gsync, "_ready", return_value=True), \
             patch.object(gsync, "lookup_url", return_value=[{"id": "xyz999"}]), \
             patch.object(gsync, "get_sheets_row", return_value=None):
            result = await gsync.delete_row_by_url("https://example.com/job/no-index")
        assert result is False

    run(_run())


def test_force_cleanup_calls_sheets_delete():
    """_force_cleanup calls gsheets_sync.delete_row_by_url and includes result in summary."""
    import hunter.telegram_bot as bot

    async def _run():
        tracker_result = {"deleted": 1, "folder": None, "drive_url": None}
        update = _make_update("dummy")

        with patch("hunter.tracker.delete_all_by_url", return_value=tracker_result), \
             patch("hunter.tracker_cache.cache.invalidate_url", new_callable=AsyncMock), \
             patch("hunter.gsheets_sync.delete_row_by_url", new_callable=AsyncMock, return_value=True) as mock_sheets:

            summary = await bot._force_cleanup("https://example.com/job/1", update)

        mock_sheets.assert_called_once_with("https://example.com/job/1")
        assert "sheet" in summary.lower()

    run(_run())


def test_force_cleanup_sheets_delete_always_called():
    """_force_cleanup calls delete_row_by_url unconditionally (even when tracker had 0 rows).

    Sheets cleanup must run BEFORE the tracker delete so that delete_row_by_url
    can still find sheets_row in the DB.  It is therefore always attempted
    regardless of how many local rows were deleted.
    """
    import hunter.telegram_bot as bot

    async def _run():
        tracker_result = {"deleted": 0, "folder": None, "drive_url": None}
        update = _make_update("dummy")

        with patch("hunter.tracker.delete_all_by_url", return_value=tracker_result), \
             patch("hunter.tracker_cache.cache.invalidate_url", new_callable=AsyncMock), \
             patch("hunter.gsheets_sync.delete_row_by_url", new_callable=AsyncMock, return_value=False) as mock_sheets:

            await bot._force_cleanup("https://new-vacancy.com/job/99", update)

        mock_sheets.assert_called_once_with("https://new-vacancy.com/job/99")

    run(_run())
