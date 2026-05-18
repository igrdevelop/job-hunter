"""Shared pytest fixtures for the hunter test suite."""
import sqlite3

import pytest

import hunter.db as db_module


@pytest.fixture(autouse=True)
def isolated_db():
    """Fresh in-memory SQLite connection per test so state never bleeds across tests."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db_module._SCHEMA)
    conn.commit()
    old = db_module._conn
    db_module._conn = conn
    yield conn
    db_module._conn = old
    conn.close()
