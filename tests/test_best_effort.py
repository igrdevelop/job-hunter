"""Tests for hunter/best_effort.py — silent-degradation alerts for best-effort subsystems."""

from __future__ import annotations

import pytest

from hunter import best_effort as be


@pytest.fixture()
def be_db(tmp_path, monkeypatch):
    """Isolate best_effort on a fresh temp DB (mirrors test_source_health.py)."""
    db = tmp_path / "health.db"
    monkeypatch.setattr(be, "DB_PATH", db)
    monkeypatch.setattr(be, "ALERT_COOLDOWN_SEC", 6 * 3600)
    return db


@pytest.fixture()
def sent():
    """Collects notify() calls instead of hitting Telegram."""
    messages: list[str] = []
    return messages


def _notify(messages):
    return lambda text: messages.append(text)


# ── swallow contract ────────────────────────────────────────────────────────


def test_exception_is_swallowed(be_db, sent):
    with be.best_effort("test.subsystem", notify=_notify(sent)):
        raise ValueError("boom")
    # No exception escaped — the with-block above completed.


def test_success_no_alert(be_db, sent):
    with be.best_effort("test.subsystem", notify=_notify(sent)):
        pass
    assert sent == []


# ── consecutive-failure threshold ───────────────────────────────────────────


def test_three_consecutive_failures_alert_exactly_once(be_db, sent):
    for _ in range(3):
        with be.best_effort("test.subsystem", notify=_notify(sent)):
            raise RuntimeError("fail")
    assert len(sent) == 1
    assert "test.subsystem" in sent[0]
    assert "3" in sent[0]


def test_below_threshold_no_alert(be_db, sent):
    for _ in range(2):
        with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
            raise RuntimeError("fail")
    assert sent == []


def test_custom_threshold_fires_on_first_failure(be_db, sent):
    with be.best_effort("test.subsystem", threshold=1, notify=_notify(sent)):
        raise RuntimeError("fail")
    assert len(sent) == 1


# ── cooldown ─────────────────────────────────────────────────────────────────


def test_repeated_failures_within_cooldown_no_second_alert(be_db, sent):
    for _ in range(5):
        with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
            raise RuntimeError("fail")
    # 5 consecutive failures, threshold 3 — only the 3rd should have alerted.
    assert len(sent) == 1


def test_cooldown_elapsed_allows_second_alert(be_db, sent, monkeypatch):
    for _ in range(3):
        with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
            raise RuntimeError("fail")
    assert len(sent) == 1

    # Simulate cooldown elapsed by shrinking it to 0 for subsequent failures.
    monkeypatch.setattr(be, "ALERT_COOLDOWN_SEC", 0)
    with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
        raise RuntimeError("fail")
    assert len(sent) == 2


# ── recovery ─────────────────────────────────────────────────────────────────


def test_success_after_alert_sends_recovery(be_db, sent):
    for _ in range(3):
        with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
            raise RuntimeError("fail")
    assert len(sent) == 1  # the failure alert

    with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
        pass  # success

    assert len(sent) == 2
    assert "восстановился" in sent[1]


def test_success_without_prior_alert_no_recovery_message(be_db, sent):
    with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
        raise RuntimeError("fail")  # 1 failure, below threshold, no alert

    with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
        pass  # success

    assert sent == []


def test_success_resets_consecutive_counter(be_db, sent):
    with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
        raise RuntimeError("fail")
    with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
        raise RuntimeError("fail")
    with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
        pass  # success resets

    # Two more failures should NOT reach threshold=3 again (counter was reset).
    for _ in range(2):
        with be.best_effort("test.subsystem", threshold=3, notify=_notify(sent)):
            raise RuntimeError("fail")
    assert sent == []


# ── independent subsystems ──────────────────────────────────────────────────


def test_subsystems_are_independent(be_db, sent):
    for _ in range(3):
        with be.best_effort("gdrive.upload", threshold=3, notify=_notify(sent)):
            raise RuntimeError("fail")
    assert len(sent) == 1

    # A different subsystem's failures don't inherit gdrive's streak.
    with be.best_effort("gsheets.mirror", threshold=3, notify=_notify(sent)):
        raise RuntimeError("fail")
    assert len(sent) == 1  # still just the gdrive alert


# ── cross-"process" summation (counters live in SQLite, not memory) ────────


def test_counters_persist_across_separate_calls_via_db(be_db, sent):
    """Each `with best_effort(...)` call reads/writes only SQLite state — no
    module-level in-memory counter — so consecutive failures recorded by
    what would be separate apply subprocesses in production still sum
    correctly here."""
    with be.best_effort("gdrive.upload", threshold=3, notify=_notify(sent)):
        raise RuntimeError("fail 1")

    with be.get_db(be.DB_PATH) as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM subsystem_health WHERE subsystem = ?",
            ("gdrive.upload",),
        ).fetchone()
    assert row["consecutive_failures"] == 1

    with be.best_effort("gdrive.upload", threshold=3, notify=_notify(sent)):
        raise RuntimeError("fail 2")

    with be.get_db(be.DB_PATH) as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM subsystem_health WHERE subsystem = ?",
            ("gdrive.upload",),
        ).fetchone()
    assert row["consecutive_failures"] == 2
    assert sent == []  # threshold not reached yet


# ── notify itself failing must never break the caller ───────────────────────


def test_notify_failure_does_not_propagate(be_db):
    def _broken_notify(_text):
        raise ConnectionError("telegram down")

    for _ in range(3):
        with be.best_effort("test.subsystem", threshold=3, notify=_broken_notify):
            raise RuntimeError("fail")
    # No exception escaped despite notify() raising on the 3rd (alerting) call.


# ── db bootstrap ─────────────────────────────────────────────────────────────


def test_works_against_a_bare_db_without_init_db(tmp_path, monkeypatch, sent):
    """best_effort must not depend on init_db() having run first (e.g. a
    standalone apply subprocess pointed at a brand-new DB file)."""
    db = tmp_path / "bare.db"
    assert not db.exists()
    monkeypatch.setattr(be, "DB_PATH", db)

    with be.best_effort("test.subsystem", notify=_notify(sent)):
        pass  # should create the table lazily and succeed
    assert db.exists()
