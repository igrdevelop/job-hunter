"""Tests for hunter/commands/gdrive.py — /gdrive_upload_missing command.

M1/M3 (docs/GDRIVE_SSL_RACE_PLAN.md): a busy backfill must report distinctly
from a real "0 uploaded" run, and `force` must actually reach
gdrive_sync.upload_missing_folders instead of being silently dropped.

The command fires its real work via context.application.create_task(_run())
without awaiting it — tests capture that coroutine and drive it manually to
exercise the full async body deterministically.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def _run_cmd(args: list[str]):
    """Run the synchronous prefix of cmd_gdrive_upload_missing and capture the
    background coroutine it schedules via context.application.create_task."""
    from hunter.commands.gdrive import cmd_gdrive_upload_missing

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args
    captured: dict = {}
    context.application.create_task = lambda coro: captured.setdefault("coro", coro)

    asyncio.run(cmd_gdrive_upload_missing(update, context))
    return update, captured.get("coro")


def _empty_result(**overrides) -> dict:
    base = {
        "uploaded": 0,
        "already_uploaded": 0,
        "skipped_missing": 0,
        "errors": [],
        "shadow_uploaded": 0,
        "shadow_skipped": 0,
        "shadow_errors": [],
    }
    base.update(overrides)
    return base


def test_disabled_replies_and_never_schedules_a_task():
    with patch("hunter.config.GDRIVE_ENABLED", False):
        update, coro = _run_cmd([])

    assert coro is None
    text = update.message.reply_text.await_args.args[0]
    assert "GDRIVE_ENABLED=false" in text


def test_force_flag_parsed_and_threaded_through():
    from hunter import gdrive_sync

    calls: dict = {}

    async def fake_upload_missing_folders(project_dir, progress_cb=None, *, force=False):
        calls["force"] = force
        return _empty_result()

    with (
        patch("hunter.config.GDRIVE_ENABLED", True),
        patch.object(gdrive_sync, "upload_missing_folders", fake_upload_missing_folders),
    ):
        update, coro = _run_cmd(["force"])
        assert coro is not None
        asyncio.run(coro)

    assert calls["force"] is True
    # The initial status message must say so up front, not just silently do it.
    start_text = update.message.reply_text.await_args_list[0].args[0]
    assert "force" in start_text.lower()


def test_no_force_arg_defaults_to_false():
    from hunter import gdrive_sync

    calls: dict = {}

    async def fake_upload_missing_folders(project_dir, progress_cb=None, *, force=False):
        calls["force"] = force
        return _empty_result()

    with (
        patch("hunter.config.GDRIVE_ENABLED", True),
        patch.object(gdrive_sync, "upload_missing_folders", fake_upload_missing_folders),
    ):
        update, coro = _run_cmd([])
        asyncio.run(coro)

    assert calls["force"] is False


def test_skipped_busy_reports_distinctly_from_zero_uploaded():
    from hunter import gdrive_sync

    async def fake_upload_missing_folders(project_dir, progress_cb=None, *, force=False):
        return _empty_result(skipped_busy=True)

    with (
        patch("hunter.config.GDRIVE_ENABLED", True),
        patch.object(gdrive_sync, "upload_missing_folders", fake_upload_missing_folders),
    ):
        update, coro = _run_cmd([])
        asyncio.run(coro)

    final_text = update.message.reply_text.await_args_list[-1].args[0]
    assert "already running" in final_text
    assert "Uploaded: 0" not in final_text


def test_shadow_skipped_shown_in_final_report():
    from hunter import gdrive_sync

    async def fake_upload_missing_folders(project_dir, progress_cb=None, *, force=False):
        return _empty_result(shadow_uploaded=2, shadow_skipped=84)

    with (
        patch("hunter.config.GDRIVE_ENABLED", True),
        patch.object(gdrive_sync, "upload_missing_folders", fake_upload_missing_folders),
    ):
        update, coro = _run_cmd([])
        asyncio.run(coro)

    final_text = update.message.reply_text.await_args_list[-1].args[0]
    assert "84" in final_text
