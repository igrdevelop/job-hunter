"""
tests/conftest.py — shared fixtures for the test suite.

tracker_db  — isolated SQLite tracker DB (monkeypatches tracker.DB_PATH).
             Use in any test that needs a clean tracker (replaces the old
             monkeypatch of tracker.TRACKER_PATH + openpyxl setup).
"""

from pathlib import Path

import pytest

from hunter import tracker as tracker_module
from hunter.db import init_db


@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch) -> None:
    """Guarantee no test ever sends a real Telegram message.

    The apply pipeline's ``notify()`` / ``send_telegram_documents()`` short-circuit
    when the bot token / chat id are empty. Several tests drive the real pipeline
    (main_api / main_cli) without mocking ``notify``; with a populated ``.env`` those
    calls would hit api.telegram.org and spam the live chat. Blank the module-level
    constants for the duration of every test. monkeypatch restores them afterwards,
    so the real bot is unaffected.
    """
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_BOT_TOKEN", "", raising=False)
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_CHAT_ID", "", raising=False)


@pytest.fixture()
def tracker_db(tmp_path: Path, monkeypatch) -> Path:
    """Return a path to a fresh, isolated SQLite tracker DB.

    Also monkeypatches ``hunter.tracker.DB_PATH`` so all tracker.py functions
    use this DB for the duration of the test.

    Usage::

        def test_something(tracker_db):
            from hunter import tracker
            tracker.add_skipped(job)
            assert tracker.is_known(job.url)
    """
    db = tmp_path / "tracker.db"
    # Prevent auto-migration from a real tracker.xlsx
    init_db(db, xlsx_path=tmp_path / "no_tracker.xlsx")
    monkeypatch.setattr(tracker_module, "DB_PATH", db)
    return db
