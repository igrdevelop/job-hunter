"""Pipeline control plan, PR 1 (c) — ``hunt_live``: the step a hunt is on
right now.

Part 1 drives ``hunter.hunt_live`` directly. Part 2 drives a REAL
``run_hunt`` (the tests/test_hunt_runs.py harness: fake sources, real
filters, real tracker dedup on the ``tracker_db`` tmp file) and asserts the
step sequence, the per-source progress, the waiting row and the terminal
stamp; plus ``run_retry_failed``'s ``trigger="retry"`` row.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hunter import best_effort as be
from hunter import hunt_live
from hunter import main as hunter_main
from hunter.db import get_db
from hunter.models import Job

# ── Part 1: the module on its own ────────────────────────────────────────────


def _latest() -> dict:
    return hunt_live.latest(1)[0]


def test_start_inserts_a_waiting_row() -> None:
    hid = hunt_live.start(trigger="web", sources=["justjoin", "linkedin"], command_id="c1")
    row = _latest()
    assert row["hunt_id"] == hid
    assert row["trigger"] == "web"
    assert row["sources"] == ["justjoin", "linkedin"]
    assert row["sources_total"] == 2
    assert row["step"] == "waiting"
    assert row["command_id"] == "c1"
    assert row["finished_at"] is None
    assert row["started_at"].endswith("+00:00") and "." not in row["started_at"]
    assert row["step_started_at"] == row["started_at"]


def test_steps_progress_and_finish_is_idempotent() -> None:
    hid = hunt_live.start(trigger="scheduled", sources=["a", "b"])
    hunt_live.set_step(hid, "fetch")
    hunt_live.source_started(hid, "a")
    assert _latest()["current_source"] == "a"
    hunt_live.source_done(hid, sources_done=1, found_so_far=12)
    hunt_live.set_step(hid, "filter")
    row = _latest()
    assert (row["step"], row["sources_done"], row["found_so_far"]) == ("filter", 1, 12)
    assert row["current_source"] == ""  # cleared when leaving fetch

    assert hunt_live.finish(hid, ok=False) is True
    assert hunt_live.finish(hid, ok=True) is False  # first terminal write wins
    hunt_live.set_step(hid, "act")  # a late write never resurrects a finished row
    row = _latest()
    assert row["step"] == "error"
    assert row["finished_at"]


def test_fail_unfinished_only_touches_open_rows() -> None:
    done = hunt_live.start(trigger="scheduled", sources=["a"])
    hunt_live.finish(done)
    hunt_live.start(trigger="web", sources=["a"])
    assert hunt_live.fail_unfinished() == 1
    steps = {r["hunt_id"]: r["step"] for r in hunt_live.latest(5)}
    assert steps[done] == "done"
    assert sorted(steps.values()) == ["done", "error"]


def test_prune_keeps_the_newest_rows() -> None:
    with patch.object(hunt_live, "HUNT_LIVE_KEEP", 3):
        ids = [hunt_live.start(trigger="scheduled", sources=[str(i)]) for i in range(5)]
    assert [r["hunt_id"] for r in hunt_live.latest(10)] == ids[::-1][:3]


def test_writes_raise_on_a_broken_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(hunt_live, "DB_PATH", tmp_path / "missing" / "x.db")
    with pytest.raises(Exception):  # noqa: B017 — the caller's best_effort needs it
        hunt_live.start(trigger="scheduled", sources=[])


# ── Part 2: the hunt loop writes it ──────────────────────────────────────────


def _job(title: str, company: str, url: str, source: str = "justjoin") -> Job:
    return Job(title=title, company=company, location="Remote", salary=None, url=url, source=source)


class _FakeSource:
    manual_only = False

    def __init__(self, name: str, jobs: list[Job], seen: list[dict]) -> None:
        self.name = name
        self._jobs = jobs
        self._seen = seen

    def search(self) -> list[Job]:
        # Snapshot the live row at the moment this source is being fetched.
        self._seen.append(_latest())
        return list(self._jobs)


@pytest.fixture
def live_db(tracker_db, monkeypatch):
    import hunter.hunt_runs as hr
    import hunter.postings_seen as ps
    import hunter.source_health as sh

    for mod in (hunt_live, hr, be, ps, sh):
        monkeypatch.setattr(mod, "DB_PATH", tracker_db)
    return tracker_db


def _sources(seen: list[dict]) -> list[_FakeSource]:
    return [
        _FakeSource(
            "justjoin",
            [_job("Senior Angular Developer", "Acme", "https://justjoin.it/job-offer/acme")],
            seen,
        ),
        _FakeSource(
            "linkedin",
            [
                _job("Angular Developer", "Globex", "https://linkedin.com/jobs/view/1", "linkedin"),
                _job("Angular Developer", "Hooli", "https://linkedin.com/jobs/view/2", "linkedin"),
            ],
            seen,
        ),
    ]


def _run(sources, *, steps: list[str], **kwargs) -> None:
    real_set_step = hunt_live.set_step

    def spy(hunt_id, step):
        steps.append(step)
        real_set_step(hunt_id, step)

    with ExitStack() as stack:
        stack.enter_context(patch("hunter.main.ALL_SOURCES", sources))
        stack.enter_context(patch("hunter.main.AUTO_APPLY", False))
        stack.enter_context(patch("hunter.main.send_job_cards", AsyncMock()))
        stack.enter_context(patch("hunter.main.send_text", AsyncMock()))
        stack.enter_context(patch.object(hunt_live, "set_step", spy))
        asyncio.run(hunter_main.run_hunt(MagicMock(), **kwargs))


def test_hunt_walks_every_step_and_records_source_progress(live_db) -> None:
    seen: list[dict] = []
    steps: list[str] = []
    _run(_sources(seen), steps=steps, trigger="web", command_id="cmd-1")

    assert steps == ["fetch", "filter", "dedup", "act"]
    # While each source was fetched: step fetch, that source current, the
    # previous sources already counted.
    assert [(r["step"], r["current_source"], r["sources_done"]) for r in seen] == [
        ("fetch", "justjoin", 0),
        ("fetch", "linkedin", 1),
    ]
    assert seen[1]["found_so_far"] == 1

    row = _latest()
    assert row["step"] == "done"
    assert row["finished_at"]
    assert (row["sources_done"], row["sources_total"], row["found_so_far"]) == (2, 2, 3)
    assert row["sources"] == ["justjoin", "linkedin"]
    assert row["trigger"] == "web"
    assert row["command_id"] == "cmd-1"
    assert row["current_source"] == ""


def test_subset_hunt_lists_only_its_sources(live_db) -> None:
    seen: list[dict] = []
    _run(_sources(seen), steps=[], source_names=["linkedin"])
    row = _latest()
    assert row["sources"] == ["linkedin"]
    assert row["sources_total"] == 1
    assert row["trigger"] == "scheduled"


def test_queued_hunt_shows_waiting_until_the_lock_frees(live_db) -> None:
    async def scenario():
        with (
            patch("hunter.main.ALL_SOURCES", _sources([])),
            patch("hunter.main.AUTO_APPLY", False),
            patch("hunter.main.send_job_cards", AsyncMock()),
            patch("hunter.main.send_text", AsyncMock()),
        ):
            await hunter_main._hunt_lock.acquire()
            task = asyncio.create_task(hunter_main.run_hunt(MagicMock()))
            await asyncio.sleep(0.2)
            row = _latest()
            assert (row["step"], row["finished_at"]) == ("waiting", None)
            hunter_main._hunt_lock.release()
            await asyncio.wait_for(task, timeout=10)
            assert _latest()["step"] == "done"

    asyncio.run(scenario())


def test_dedup_read_failure_ends_the_row_in_error(live_db) -> None:
    with patch("hunter.main.get_known_urls", side_effect=RuntimeError("db on fire")):
        _run(_sources([]), steps=[])
    row = _latest()
    assert row["step"] == "error"
    assert row["finished_at"]


def test_exception_in_act_ends_the_row_in_error(live_db) -> None:
    with (
        patch("hunter.main._report_and_act", AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(RuntimeError),
    ):
        _run(_sources([]), steps=[])
    assert _latest()["step"] == "error"


def test_live_write_failures_never_cost_the_hunt(live_db) -> None:
    sent: list[str] = []

    async def fake_send(_ctx, text, **_kw):
        sent.append(text)

    with (
        patch.object(hunt_live, "set_step", side_effect=RuntimeError("table gone")),
        patch("hunter.main.ALL_SOURCES", _sources([])),
        patch("hunter.main.AUTO_APPLY", False),
        patch("hunter.main.send_job_cards", AsyncMock()),
        patch("hunter.main.send_text", fake_send),
    ):
        asyncio.run(hunter_main.run_hunt(MagicMock()))
    assert any(t.startswith("🔍 <b>Hunt ") for t in sent)  # the report still went out
    with get_db(live_db) as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM subsystem_health WHERE subsystem='hunt.live'"
        ).fetchone()
    # set_step failed four times (fetch/filter/dedup/act) and best_effort
    # counted them without raising; finish() then succeeded, stamped the row
    # and reset the counter.
    assert row is not None and row["consecutive_failures"] == 0
    assert _latest()["step"] == "done"


def test_retry_pass_writes_a_retry_row(live_db, monkeypatch) -> None:
    monkeypatch.setattr(hunter_main, "AUTO_APPLY", True)
    seen: list[dict] = []

    async def fake_retry(_ctx):
        seen.append(_latest())

    with (
        patch("hunter.main._retry_failed", fake_retry),
        patch("hunter.main._check_apply_ready", return_value=None),
        patch("hunter.llm_outage.pause_remaining", return_value=0),
    ):
        asyncio.run(hunter_main.run_retry_failed(MagicMock(), command_id="r-1"))

    assert seen and seen[0]["step"] == "act"
    row = _latest()
    assert (row["trigger"], row["sources"], row["command_id"]) == ("retry", [], "r-1")
    assert row["step"] == "done"


def test_retry_noop_without_auto_apply_writes_nothing(live_db, monkeypatch) -> None:
    monkeypatch.setattr(hunter_main, "AUTO_APPLY", False)
    asyncio.run(hunter_main.run_retry_failed(MagicMock()))
    assert hunt_live.latest(5) == []
