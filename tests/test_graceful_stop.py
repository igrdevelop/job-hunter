"""Graceful shutdown of the apply worker (docs/improvement-2026-09/06-OPS_PLAN.md M3).

Before this, `apply_worker_loop` was an unbounded `while True` started via
`app.create_task`, so PTB's `Application.stop()` — which awaits created tasks —
never completed and Docker SIGKILLed the container ten seconds later. The row
the worker had claimed stayed `IN_PROGRESS` until the stale sweep noticed it,
`APPLY_CLAIM_TIMEOUT_MIN` (60 min) later, with the first tick 15 minutes after
start: a vacancy sat undone for over an hour because a Telegram fix shipped.

The tests below pin the three halves of the fix:

* `WorkerControl` — the stop signal itself, and that a sleeping worker wakes on
  it instead of sitting out `APPLY_DELAY_SEC`;
* `shutdown_workers()` — a task that will not stop is cancelled, and any URL
  still recorded as claimed is released as a safety net;
* `tracker.release_claims_by_host()` — a restart releases what THIS host
  claimed (whatever pid) and leaves another host's live claim alone.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hunter.models import Job


def _job(url: str) -> Job:
    return Job(
        title="Senior Angular Developer",
        company="Acme",
        location="Remote",
        salary=None,
        url=url,
        source="justjoin",
    )


# ---------------------------------------------------------------- WorkerControl


def test_request_stop_sets_the_flag() -> None:
    from hunter.apply_worker import WorkerControl

    wc = WorkerControl()
    assert not wc.is_stopping()
    wc.request_stop()
    assert wc.is_stopping()


def test_sleep_or_stop_returns_early_once_stop_is_requested() -> None:
    """The whole point: a worker asleep for APPLY_DELAY_SEC must not hold up
    shutdown for the rest of that sleep."""
    from hunter.apply_worker import WorkerControl

    async def scenario() -> float:
        wc = WorkerControl()
        started = time.perf_counter()
        waiter = asyncio.ensure_future(wc.sleep_or_stop(30.0))
        await asyncio.sleep(0.01)
        wc.request_stop()
        await asyncio.wait_for(waiter, timeout=5.0)
        return time.perf_counter() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 5.0, f"sleep_or_stop waited {elapsed:.1f}s instead of waking on stop"


def test_sleep_or_stop_sleeps_when_no_stop_is_requested() -> None:
    from hunter.apply_worker import WorkerControl

    async def scenario() -> float:
        wc = WorkerControl()
        started = time.perf_counter()
        await wc.sleep_or_stop(0.05)
        return time.perf_counter() - started

    assert asyncio.run(scenario()) >= 0.04


def test_claim_map_round_trip() -> None:
    from hunter.apply_worker import WorkerControl

    wc = WorkerControl()
    assert wc.claimed_urls() == {}
    wc.set_claim(0, "https://example.com/a")
    wc.set_claim(1, "https://example.com/b")
    assert wc.claimed_urls() == {0: "https://example.com/a", 1: "https://example.com/b"}
    wc.clear_claim(0)
    assert wc.claimed_urls() == {1: "https://example.com/b"}
    # Clearing an unknown worker must not raise — shutdown calls it blindly.
    wc.clear_claim(99)


# ------------------------------------------------------------- shutdown_workers


def test_shutdown_stops_a_cooperative_worker_and_releases_nothing(monkeypatch) -> None:
    from hunter import apply_worker

    released: list[str] = []
    monkeypatch.setattr(apply_worker.tracker, "release_claim", released.append)

    async def scenario() -> None:
        wc = apply_worker.WorkerControl()
        monkeypatch.setattr(apply_worker, "_worker_tasks", {})

        async def cooperative() -> None:
            while not wc.is_stopping():
                await wc.sleep_or_stop(30.0)

        task = asyncio.ensure_future(cooperative())
        apply_worker.register_worker_task(0, task)
        await asyncio.sleep(0.01)
        await apply_worker.shutdown_workers(wait_timeout=5.0, kill_timeout=1.0, ctrl=wc)
        assert task.done() and not task.cancelled()

    asyncio.run(scenario())
    # The loop cleared its own claim (it never had one), so the safety net
    # must not have released anything.
    assert released == []


def test_shutdown_cancels_a_worker_that_will_not_stop(monkeypatch) -> None:
    """A worker mid `apply_agent.py` subprocess can legitimately run for hours;
    shutdown escalates to cancellation rather than waiting it out."""
    from hunter import apply_worker

    monkeypatch.setattr(apply_worker.tracker, "release_claim", lambda url: None)

    async def scenario() -> "asyncio.Task[None]":
        wc = apply_worker.WorkerControl()
        monkeypatch.setattr(apply_worker, "_worker_tasks", {})

        async def stubborn() -> None:
            await asyncio.sleep(3600)

        task = asyncio.ensure_future(stubborn())
        apply_worker.register_worker_task(0, task)
        await asyncio.sleep(0.01)
        await apply_worker.shutdown_workers(wait_timeout=0.05, kill_timeout=1.0, ctrl=wc)
        return task

    task = asyncio.run(scenario())
    assert task.cancelled(), "a worker that ignores the stop event must be cancelled"


def test_shutdown_safety_net_releases_a_still_claimed_url(monkeypatch) -> None:
    """If a task dies without clearing its claim, shutdown releases it anyway —
    otherwise the row waits out APPLY_CLAIM_TIMEOUT_MIN, which is the bug."""
    from hunter import apply_worker

    released: list[str] = []
    monkeypatch.setattr(apply_worker.tracker, "release_claim", released.append)

    async def scenario() -> "apply_worker.WorkerControl":
        wc = apply_worker.WorkerControl()
        monkeypatch.setattr(apply_worker, "_worker_tasks", {})
        wc.set_claim(0, "https://example.com/stranded")
        await apply_worker.shutdown_workers(wait_timeout=0.05, kill_timeout=0.05, ctrl=wc)
        return wc

    wc = asyncio.run(scenario())
    assert released == ["https://example.com/stranded"]
    assert wc.claimed_urls() == {}, "a released claim must be dropped from the map"


def test_shutdown_survives_a_failing_release(monkeypatch) -> None:
    """The safety net is best-effort: a tracker error must not stop shutdown."""
    from hunter import apply_worker

    def boom(url: str) -> None:
        raise RuntimeError("db gone")

    monkeypatch.setattr(apply_worker.tracker, "release_claim", boom)

    async def scenario() -> "apply_worker.WorkerControl":
        wc = apply_worker.WorkerControl()
        monkeypatch.setattr(apply_worker, "_worker_tasks", {})
        wc.set_claim(0, "https://example.com/x")
        await apply_worker.shutdown_workers(wait_timeout=0.05, kill_timeout=0.05, ctrl=wc)
        return wc

    wc = asyncio.run(scenario())
    assert wc.claimed_urls() == {}


# ------------------------------------------------- release_claims_by_host (DB)


def test_claim_pending_stamps_claimed_by(tracker_db) -> None:
    from hunter import tracker

    tracker.add_pending(_job("https://example.com/j1"))
    row = tracker.claim_pending(claimed_by="box-a:123")
    assert row is not None
    assert row["claimed_by"] == "box-a:123"


def test_release_claims_by_host_releases_this_host_any_pid(tracker_db) -> None:
    """A restarted container gets a new pid, so the match is by hostname."""
    from hunter import tracker

    tracker.add_pending(_job("https://example.com/j1"))
    tracker.claim_pending(claimed_by="box-a:111")

    assert tracker.release_claims_by_host("box-a") == 1
    assert tracker.count_pending() == 1
    assert tracker.count_in_progress() == 0


def test_release_claims_by_host_leaves_another_host_alone(tracker_db) -> None:
    from hunter import tracker

    tracker.add_pending(_job("https://example.com/mine"))
    tracker.claim_pending(claimed_by="box-a:1")
    tracker.add_pending(_job("https://example.com/theirs"))
    tracker.claim_pending(claimed_by="box-b:1")

    assert tracker.release_claims_by_host("box-a") == 1
    assert tracker.count_in_progress() == 1, "box-b's live claim must survive"
    assert tracker.count_pending() == 1


def test_release_claims_by_host_is_a_noop_for_an_empty_hostname(tracker_db) -> None:
    """A blank hostname must never mass-release every row via a blank LIKE."""
    from hunter import tracker

    tracker.add_pending(_job("https://example.com/j1"))
    tracker.claim_pending(claimed_by="box-a:1")

    assert tracker.release_claims_by_host("") == 0
    assert tracker.count_in_progress() == 1


def test_release_claims_by_host_does_not_prefix_match_another_host(tracker_db) -> None:
    """`box-a` must not release `box-a2`'s claim — the tag is `host:pid`."""
    from hunter import tracker

    tracker.add_pending(_job("https://example.com/j1"))
    tracker.claim_pending(claimed_by="box-a2:1")

    assert tracker.release_claims_by_host("box-a") == 0
    assert tracker.count_in_progress() == 1


def test_release_claims_by_host_escapes_like_wildcards(tracker_db) -> None:
    """A hostname containing an underscore must not act as a single-character
    wildcard and release a different host's claim."""
    from hunter import tracker

    tracker.add_pending(_job("https://example.com/j1"))
    tracker.claim_pending(claimed_by="boxXa:1")

    assert tracker.release_claims_by_host("box_a") == 0
    assert tracker.count_in_progress() == 1


def test_claimed_by_tag_shape() -> None:
    from hunter.apply_worker import claimed_by_tag

    host, _, pid = claimed_by_tag().partition(":")
    assert host
    assert pid.isdigit()


@pytest.mark.parametrize("hostname", ["box-a", "box_a", "box%a"])
def test_release_claims_by_host_matches_its_own_tag(tracker_db, hostname: str) -> None:
    """Whatever the hostname contains, the host always releases its own rows."""
    from hunter import tracker

    tracker.add_pending(_job("https://example.com/j1"))
    tracker.claim_pending(claimed_by=f"{hostname}:42")

    assert tracker.release_claims_by_host(hostname) == 1
    assert tracker.count_in_progress() == 0
