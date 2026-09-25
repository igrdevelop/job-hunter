"""Pipeline control plan, PR 1 (d) — scheduler facts into the config KV."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hunter.schedules import bot_state


class _Job:
    def __init__(self, name: str, next_t: datetime | None, data=None, *, started=True) -> None:
        self.name = name
        self._next_t = next_t
        self.data = data
        self._started = started

    @property
    def next_t(self):
        if not self._started:  # PTB raises this before the JobQueue starts
            raise AttributeError("next_run_time")
        return self._next_t


class _Queue:
    def __init__(self, jobs) -> None:
        self._jobs = jobs

    def jobs(self):
        return tuple(self._jobs)


T0 = datetime(2026, 9, 25, 11, 40, tzinfo=timezone.utc)


def _queue() -> _Queue:
    warsaw = timezone(timedelta(hours=2))
    return _Queue(
        [
            _Job("hunt_linkedin_13:00", T0 + timedelta(minutes=30), {"source_names": ["linkedin"]}),
            # Earliest hunt, and given in a non-UTC tz: must come out as UTC.
            _Job(
                "hunt_justjoin_13:00",
                (T0 + timedelta(minutes=10)).astimezone(warsaw),
                {"source_names": ["justjoin"]},
            ),
            _Job("retry_failed_0245", T0 + timedelta(hours=15)),
            _Job("retry_failed_0745", T0 + timedelta(hours=20)),
            # Not a hunt, even though it fires first.
            _Job("gsheets_resync", T0 + timedelta(minutes=1)),
            _Job("hunt_pracuj_05:00", None, {"source_names": ["pracuj"]}),  # removed job
        ]
    )


def test_collect_state_reads_the_schedulers_own_next_times() -> None:
    values = bot_state.collect_state(_queue(), ["justjoin", "linkedin", "pracuj"])
    assert list(values) == [
        "bot_state.next_hunt",
        "bot_state.next_retry",
        "bot_state.sources",
        "bot_state.updated_at",
    ]  # updated_at last: a reader seeing it fresh sees fresh next_* too
    assert json.loads(values["bot_state.next_hunt"]) == {
        "at": "2026-09-25T11:50:00+00:00",
        "source": "justjoin",
        "sources_total": 3,
    }
    assert json.loads(values["bot_state.next_retry"]) == {"at": "2026-09-26T02:40:00+00:00"}
    assert json.loads(values["bot_state.sources"]) == ["justjoin", "linkedin", "pracuj"]
    updated = json.loads(values["bot_state.updated_at"])
    assert updated.endswith("+00:00")
    assert datetime.fromisoformat(updated).tzinfo is not None


def test_collect_state_before_the_queue_starts_is_null_not_a_crash() -> None:
    q = _Queue([_Job("hunt_a_02:00", T0, {"source_names": ["a"]}, started=False)])
    values = bot_state.collect_state(q, ["a"])
    assert json.loads(values["bot_state.next_hunt"]) is None
    assert json.loads(values["bot_state.next_retry"]) is None
    assert json.loads(values["bot_state.sources"]) == ["a"]


def test_collect_state_without_a_queue() -> None:
    values = bot_state.collect_state(None, [])
    assert json.loads(values["bot_state.next_hunt"]) is None
    assert json.loads(values["bot_state.sources"]) == []


@pytest.fixture
def kv_db(tmp_path, monkeypatch):
    db = tmp_path / "kv.db"
    monkeypatch.setattr("hunter.config.TRACKER_DB_PATH", db)
    monkeypatch.setattr("hunter.best_effort.DB_PATH", tmp_path / "health.db")
    return db


def test_publish_writes_the_config_kv_rows(kv_db, monkeypatch) -> None:
    monkeypatch.setattr("hunter.sources.ALL_SOURCES", [SimpleNamespace(name="justjoin")])
    ctx = SimpleNamespace(job_queue=_queue())
    asyncio.run(bot_state.scheduled_bot_state(ctx))

    conn = sqlite3.connect(kv_db)
    rows = dict(conn.execute("SELECT key, value FROM config WHERE key LIKE 'bot_state.%'"))
    conn.close()
    assert set(rows) == {
        "bot_state.next_hunt",
        "bot_state.next_retry",
        "bot_state.sources",
        "bot_state.updated_at",
    }
    assert json.loads(rows["bot_state.next_hunt"])["source"] == "justjoin"
    assert json.loads(rows["bot_state.next_hunt"])["sources_total"] == 1
    assert json.loads(rows["bot_state.sources"]) == ["justjoin"]


def test_publish_swallows_a_collect_failure(kv_db) -> None:
    class _Broken:
        def jobs(self):
            raise RuntimeError("scheduler gone")

    asyncio.run(bot_state.publish(SimpleNamespace(job_queue=_Broken())))  # must not raise


def test_write_state_raises_on_a_broken_db(tmp_path, monkeypatch) -> None:
    # A swallowed write would count as a best_effort success and never alert.
    monkeypatch.setattr("hunter.config.TRACKER_DB_PATH", tmp_path / "missing_dir" / "t.db")
    with pytest.raises(sqlite3.Error):
        bot_state.write_state({bot_state.KEY_UPDATED_AT: json.dumps("x")})
