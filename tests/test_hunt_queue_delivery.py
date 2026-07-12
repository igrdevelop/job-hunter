"""Tests for docs/HUNT_QUEUE_AND_DELIVERY_PLAN.md:

M1 — scheduled hunts queue on _hunt_lock instead of being skipped
M2 — _retry_failed moved off the hunt tail to its own schedule
M3 — hunter/delivery.py instant Sheets+Drive delivery on every apply path
"""

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

from hunter import main
from hunter import delivery

import pytest


@pytest.fixture(autouse=True)
def _fresh_hunt_lock():
    """asyncio.Lock binds to the first loop that touches it; each test here runs
    its own asyncio.run() loop, so give every test a fresh lock."""
    main._hunt_lock = asyncio.Lock()
    yield
    main._hunt_lock = asyncio.Lock()


# ── M1: queue instead of skip ────────────────────────────────────────────────


def test_run_hunt_waits_for_lock_instead_of_skipping() -> None:
    """A hunt firing while the lock is held runs AFTER the holder releases it."""

    async def scenario():
        order: list[str] = []

        async def fake_impl(context, source_names=None):
            order.append(f"impl:{(source_names or ['all'])[0]}")

        with (
            patch.object(main, "_run_hunt_impl", fake_impl),
            patch.object(main, "send_text", AsyncMock()) as m_send,
        ):
            await main._hunt_lock.acquire()
            task = asyncio.create_task(main.run_hunt(None, source_names=["justjoin"]))
            await asyncio.sleep(0.05)
            assert not task.done(), "queued hunt must wait, not return"
            assert order == []
            main._hunt_lock.release()
            await asyncio.wait_for(task, timeout=2)
            assert order == ["impl:justjoin"], "queued hunt must run once the lock frees"
            # Scheduled default: silent queueing — no Telegram noise.
            m_send.assert_not_awaited()

    asyncio.run(scenario())


def test_run_hunt_notify_queued_sends_one_message() -> None:
    """Manual /hunt path: one 'queued' reply when the lock is busy, then runs."""

    async def scenario():
        with (
            patch.object(main, "_run_hunt_impl", AsyncMock()) as m_impl,
            patch.object(main, "send_text", AsyncMock()) as m_send,
        ):
            await main._hunt_lock.acquire()
            task = asyncio.create_task(main.run_hunt(None, notify_queued=True))
            await asyncio.sleep(0.05)
            main._hunt_lock.release()
            await asyncio.wait_for(task, timeout=2)
            m_impl.assert_awaited_once()
            assert m_send.await_count == 1
            assert "queued" in m_send.await_args.args[1].lower()

    asyncio.run(scenario())


def test_run_hunt_no_message_when_lock_free() -> None:
    async def scenario():
        with (
            patch.object(main, "_run_hunt_impl", AsyncMock()) as m_impl,
            patch.object(main, "send_text", AsyncMock()) as m_send,
        ):
            await main.run_hunt(None, notify_queued=True)
            m_impl.assert_awaited_once()
            m_send.assert_not_awaited()

    asyncio.run(scenario())


def test_concurrent_hunts_all_complete_serially() -> None:
    """No hunt is ever lost: N concurrent calls -> N impl runs."""

    async def scenario():
        running = 0
        max_running = 0
        done: list[str] = []

        async def fake_impl(context, source_names=None):
            nonlocal running, max_running
            running += 1
            max_running = max(max_running, running)
            await asyncio.sleep(0.01)
            running -= 1
            done.append((source_names or ["all"])[0])

        with (
            patch.object(main, "_run_hunt_impl", fake_impl),
            patch.object(main, "send_text", AsyncMock()),
        ):
            await asyncio.gather(
                main.run_hunt(None, source_names=["a"]),
                main.run_hunt(None, source_names=["b"]),
                main.run_hunt(None, source_names=["c"]),
            )
        assert sorted(done) == ["a", "b", "c"]
        assert max_running == 1, "hunts must stay serialized"

    asyncio.run(scenario())


# ── M2: retry on its own schedule ────────────────────────────────────────────


def test_hunt_impl_no_longer_calls_retry_failed() -> None:
    """The hunt tail must not retry the global FAIL list anymore (wiring guard)."""
    src = inspect.getsource(main._run_hunt_impl)
    assert "await _retry_failed" not in src


def test_run_retry_failed_runs_under_lock(monkeypatch) -> None:
    async def scenario():
        monkeypatch.setattr(main, "AUTO_APPLY", True)
        with (
            patch.object(main, "_retry_failed", AsyncMock()) as m_retry,
            patch.object(main, "_check_apply_ready", MagicMock(return_value=None)),
            patch.object(main, "send_text", AsyncMock()),
        ):
            await main.run_retry_failed(None)
            m_retry.assert_awaited_once()
            assert not main._hunt_lock.locked(), "lock must be released afterwards"

    asyncio.run(scenario())


def test_run_retry_failed_noop_without_auto_apply(monkeypatch) -> None:
    async def scenario():
        monkeypatch.setattr(main, "AUTO_APPLY", False)
        with patch.object(main, "_retry_failed", AsyncMock()) as m_retry:
            await main.run_retry_failed(None)
            m_retry.assert_not_awaited()

    asyncio.run(scenario())


def test_run_retry_failed_reports_auth_error(monkeypatch) -> None:
    async def scenario():
        monkeypatch.setattr(main, "AUTO_APPLY", True)
        with (
            patch.object(main, "_retry_failed", AsyncMock()) as m_retry,
            patch.object(main, "_check_apply_ready", MagicMock(return_value="no key")),
            patch.object(main, "send_text", AsyncMock()) as m_send,
        ):
            await main.run_retry_failed(None)
            m_retry.assert_not_awaited()
            assert m_send.await_count == 1
            assert "not ready" in m_send.await_args.args[1]

    asyncio.run(scenario())


def test_register_creates_retry_jobs_and_drive_interval(monkeypatch) -> None:
    """schedules.register() wires retry_failed daily jobs + the new Drive interval."""
    import pytz
    from hunter import schedules

    monkeypatch.setattr(schedules, "RETRY_FAILED_TIMES", ["07:45", "18:45", "garbage"])
    monkeypatch.setattr(schedules, "GDRIVE_UPLOAD_MISSING_INTERVAL_MIN", 30)

    app = MagicMock()
    schedules.register(app, pytz.timezone("Europe/Warsaw"))

    daily_names = [c.kwargs.get("name") for c in app.job_queue.run_daily.call_args_list]
    assert "retry_failed_0745" in daily_names
    assert "retry_failed_1845" in daily_names
    # malformed entry skipped, not registered and not crashing
    assert sum(1 for n in daily_names if n and n.startswith("retry_failed_")) == 2

    repeating = {
        c.kwargs.get("name"): c.kwargs.get("interval")
        for c in app.job_queue.run_repeating.call_args_list
    }
    assert repeating.get("gdrive_upload_missing") == 30 * 60


# ── M3: instant delivery ─────────────────────────────────────────────────────


def _cache_mock(row):
    cache = MagicMock()
    cache.load_from_db = AsyncMock()
    cache.get_row_by_url = AsyncMock(return_value=row)
    return cache


def test_delivery_targeted_paths_when_url_known(monkeypatch, tmp_path) -> None:
    async def scenario():
        row = {"ID": "abc123", "URL": "https://x.example/job"}
        cache = _cache_mock(row)
        monkeypatch.setattr("hunter.tracker_cache.cache", cache)
        monkeypatch.setattr("hunter.config.GDRIVE_ENABLED", True)
        monkeypatch.setattr("hunter.config.PROJECT_DIR", tmp_path)
        with (
            patch("hunter.gsheets_sync.mirror_new_row", AsyncMock()) as m_mirror,
            patch("hunter.gsheets_sync.push_missing_rows", AsyncMock()) as m_push,
            patch("hunter.tracker.get_folder_by_url", return_value="Applications/x/Co"),
            patch(
                "hunter.gdrive_sync.upload_application_folder",
                AsyncMock(return_value="https://drive/f"),
            ) as m_up,
            patch("hunter.gdrive_sync.upload_missing_folders", AsyncMock()) as m_up_missing,
        ):
            drive_url = await delivery.deliver_apply_now("https://x.example/job")
        m_mirror.assert_awaited_once_with(row)
        m_push.assert_not_awaited()
        m_up.assert_awaited_once()
        m_up_missing.assert_not_awaited()
        assert drive_url == "https://drive/f"

    asyncio.run(scenario())


def test_delivery_falls_back_when_no_url(monkeypatch, tmp_path) -> None:
    """Paste without a URL: both backfills run immediately, no targeted lookups."""

    async def scenario():
        monkeypatch.setattr("hunter.config.GDRIVE_ENABLED", True)
        monkeypatch.setattr("hunter.config.PROJECT_DIR", tmp_path)
        with (
            patch(
                "hunter.gsheets_sync.push_missing_rows",
                AsyncMock(return_value={"pushed": 1}),
            ) as m_push,
            patch(
                "hunter.gdrive_sync.upload_missing_folders",
                AsyncMock(return_value={"uploaded": 1}),
            ) as m_up_missing,
            patch("hunter.gsheets_sync.mirror_new_row", AsyncMock()) as m_mirror,
        ):
            drive_url = await delivery.deliver_apply_now(None)
        m_push.assert_awaited_once()
        m_up_missing.assert_awaited_once()
        m_mirror.assert_not_awaited()
        assert drive_url is None

    asyncio.run(scenario())


def test_delivery_falls_back_when_row_lookup_misses(monkeypatch, tmp_path) -> None:
    async def scenario():
        cache = _cache_mock(None)  # URL not found in tracker cache
        monkeypatch.setattr("hunter.tracker_cache.cache", cache)
        monkeypatch.setattr("hunter.config.GDRIVE_ENABLED", True)
        monkeypatch.setattr("hunter.config.PROJECT_DIR", tmp_path)
        with (
            patch("hunter.gsheets_sync.mirror_new_row", AsyncMock()) as m_mirror,
            patch(
                "hunter.gsheets_sync.push_missing_rows",
                AsyncMock(return_value={"pushed": 0}),
            ) as m_push,
            patch("hunter.tracker.get_folder_by_url", return_value=None),
            patch(
                "hunter.gdrive_sync.upload_missing_folders",
                AsyncMock(return_value={"uploaded": 0}),
            ) as m_up_missing,
        ):
            await delivery.deliver_apply_now("https://x.example/unknown")
        m_mirror.assert_not_awaited()
        m_push.assert_awaited_once()
        m_up_missing.assert_awaited_once()

    asyncio.run(scenario())


def test_delivery_sheets_failure_does_not_block_drive(monkeypatch, tmp_path) -> None:
    async def scenario():
        cache = MagicMock()
        cache.load_from_db = AsyncMock(side_effect=OSError("sheets down"))
        monkeypatch.setattr("hunter.tracker_cache.cache", cache)
        monkeypatch.setattr("hunter.config.GDRIVE_ENABLED", True)
        monkeypatch.setattr("hunter.config.PROJECT_DIR", tmp_path)
        with (
            patch("hunter.gsheets_sync.push_missing_rows", AsyncMock()),
            patch("hunter.tracker.get_folder_by_url", return_value="Applications/x/Co"),
            patch(
                "hunter.gdrive_sync.upload_application_folder",
                AsyncMock(return_value="https://drive/f"),
            ) as m_up,
        ):
            drive_url = await delivery.deliver_apply_now("https://x.example/job")
        m_up.assert_awaited_once()
        assert drive_url == "https://drive/f"

    asyncio.run(scenario())


def test_delivery_never_raises(monkeypatch) -> None:
    """Everything failing at once must still return quietly (best-effort contract)."""

    async def scenario():
        cache = MagicMock()
        cache.load_from_db = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr("hunter.tracker_cache.cache", cache)
        with (
            patch(
                "hunter.gsheets_sync.push_missing_rows",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch("hunter.tracker.get_folder_by_url", side_effect=RuntimeError("boom")),
            patch(
                "hunter.gdrive_sync.upload_missing_folders",
                AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            assert await delivery.deliver_apply_now("https://x.example/job") is None

    asyncio.run(scenario())


def test_delivery_gdrive_disabled_skips_drive_only(monkeypatch, tmp_path) -> None:
    async def scenario():
        row = {"ID": "abc123"}
        cache = _cache_mock(row)
        monkeypatch.setattr("hunter.tracker_cache.cache", cache)
        monkeypatch.setattr("hunter.config.GDRIVE_ENABLED", False)
        with (
            patch("hunter.gsheets_sync.mirror_new_row", AsyncMock()) as m_mirror,
            patch("hunter.gdrive_sync.upload_application_folder", AsyncMock()) as m_up,
            patch("hunter.gdrive_sync.upload_missing_folders", AsyncMock()) as m_up_missing,
        ):
            await delivery.deliver_apply_now("https://x.example/job")
        m_mirror.assert_awaited_once()
        m_up.assert_not_awaited()
        m_up_missing.assert_not_awaited()

    asyncio.run(scenario())


# ── M3 wiring: every apply path calls deliver_apply_now ──────────────────────


def test_main_deliver_now_delegates_to_delivery() -> None:
    async def scenario():
        with patch("hunter.delivery.deliver_apply_now", AsyncMock()) as m:
            await main._deliver_now("https://x.example/job")
        m.assert_awaited_once_with("https://x.example/job")

    asyncio.run(scenario())


def test_apply_runner_paste_without_url_calls_delivery() -> None:
    """url='' (paste with no URL) must reach deliver_apply_now(None)."""
    from hunter.bot import apply_runner

    async def scenario():
        with (
            patch(
                "hunter.services.apply_service.run_apply_agent_for_url",
                AsyncMock(return_value=("ok", "")),
            ),
            patch("hunter.delivery.deliver_apply_now", AsyncMock(return_value=None)) as m_deliver,
            patch.object(apply_runner, "_tg_notify", AsyncMock()),
        ):
            await apply_runner._run_apply_agent("", paste_file=None)
        m_deliver.assert_awaited_once_with(None)

    asyncio.run(scenario())


def test_linkedin_batch_calls_delivery_on_success() -> None:
    from hunter.bot import apply_runner

    async def scenario():
        proc = MagicMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        with (
            patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
            patch("hunter.delivery.deliver_apply_now", AsyncMock(return_value=None)) as m_deliver,
        ):
            await apply_runner._run_linkedin_batch(["12345"], update)
        m_deliver.assert_awaited_once()

    asyncio.run(scenario())
