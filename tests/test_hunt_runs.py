"""docs/PIPELINE_VIZ_PLAN.md M1 — ``hunt_runs``: one row per hunt with the
funnel numbers ``hunter/main.py::_run_hunt_impl`` already computes for its
Telegram report.

Part 1 drives ``hunter.hunt_runs`` directly on an isolated DB (record + read
round-trip, prune, sum_window). Part 2 drives a REAL ``run_hunt`` — the same
harness as tests/test_postings_seen_wiring.py: one fake source, the real
filters, the real tracker dedup on the ``tracker_db`` tmp file — and asserts
the stored row equals the numbers parsed back out of the Telegram summary.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hunter import best_effort as be
from hunter import hunt_runs
from hunter.db import get_db
from hunter.main import run_hunt
from hunter.models import Job

# ── Part 1: the module on its own ────────────────────────────────────────────


@pytest.fixture
def runs_db(tmp_path, monkeypatch):
    db = tmp_path / "hunt_runs_unit.db"
    monkeypatch.setattr(hunt_runs, "DB_PATH", db)
    return db


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def test_record_and_read_round_trip(runs_db) -> None:
    row_id = hunt_runs.record_hunt(
        trigger="manual",
        sources=["justjoin", "nofluffjobs"],
        found=412,
        filtered_out=371,
        filter_reasons={"level": 200, "location": 171, "keywords": 0},
        dup_url=20,
        dup_ct=9,
        dup_cooldown=0,
        new=12,
        capped=2,
        queued=10,
        applied_inline=0,
        duration_ms=4321,
        ts="2026-09-22T05:00:00+00:00",
    )
    assert row_id >= 1

    rows = hunt_runs.recent_hunts(limit=5)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == row_id
    assert r["ts"] == "2026-09-22T05:00:00+00:00"
    assert r["trigger"] == "manual"
    assert r["sources"] == ["justjoin", "nofluffjobs"]
    assert r["found"] == 412
    assert r["filtered_out"] == 371
    # Zero-count reasons are dropped, like the Telegram report does.
    assert r["filter_reasons"] == {"level": 200, "location": 171}
    assert (r["dup_url"], r["dup_ct"], r["dup_cooldown"]) == (20, 9, 0)
    assert r["new"] == 12
    assert (r["capped"], r["queued"], r["applied_inline"]) == (2, 10, 0)
    assert r["duration_ms"] == 4321


def test_ts_defaults_to_utc_now_iso_seconds(runs_db) -> None:
    before = datetime.now(timezone.utc).replace(microsecond=0)
    hunt_runs.record_hunt(trigger="scheduled", sources=["a"])
    ts = hunt_runs.recent_hunts(1)[0]["ts"]
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None
    assert parsed >= before
    assert "." not in ts  # seconds precision, like source_runs


def test_recent_hunts_is_newest_first_and_honours_limit(runs_db) -> None:
    for i in range(4):
        hunt_runs.record_hunt(trigger="scheduled", sources=[f"s{i}"], found=i)
    rows = hunt_runs.recent_hunts(limit=2)
    assert [r["found"] for r in rows] == [3, 2]
    assert hunt_runs.recent_hunts(limit=0) == []


def test_negative_and_none_counters_clamp_to_zero(runs_db) -> None:
    hunt_runs.record_hunt(trigger="scheduled", sources=[], found=-5, new=None, queued="x")  # type: ignore[arg-type]
    r = hunt_runs.recent_hunts(1)[0]
    assert (r["found"], r["new"], r["queued"]) == (0, 0, 0)
    assert r["sources"] == []


def test_unknown_trigger_is_stored_but_logged(runs_db, caplog) -> None:
    with caplog.at_level("WARNING", logger="hunter.hunt_runs"):
        hunt_runs.record_hunt(trigger="cron", sources=["a"])
    assert hunt_runs.recent_hunts(1)[0]["trigger"] == "cron"
    assert "unknown trigger" in caplog.text


def test_prune_keeps_only_the_newest_keep_rows(runs_db) -> None:
    with patch.object(hunt_runs, "HUNT_RUNS_KEEP", 3):
        for i in range(6):
            hunt_runs.record_hunt(trigger="scheduled", sources=["a"], found=i)
    rows = hunt_runs.recent_hunts(limit=50)
    assert [r["found"] for r in rows] == [5, 4, 3]


def test_sum_window_totals_every_count_column_and_merges_reasons(runs_db) -> None:
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    hunt_runs.record_hunt(
        trigger="scheduled",
        sources=["a"],
        found=100,
        filtered_out=80,
        filter_reasons={"level": 50, "location": 30},
        dup_url=5,
        dup_ct=3,
        dup_cooldown=1,
        new=11,
        capped=1,
        queued=10,
        duration_ms=1000,
        ts=_iso(now - timedelta(hours=1)),
    )
    hunt_runs.record_hunt(
        trigger="manual",
        sources=["b"],
        found=50,
        filtered_out=40,
        filter_reasons={"level": 40},
        dup_url=2,
        new=8,
        applied_inline=8,
        duration_ms=500,
        ts=_iso(now - timedelta(minutes=10)),
    )
    # Outside the window — must not count.
    hunt_runs.record_hunt(
        trigger="scheduled",
        sources=["c"],
        found=999,
        filter_reasons={"keywords": 999},
        ts=_iso(now - timedelta(days=3)),
    )

    totals = hunt_runs.sum_window(_iso(now - timedelta(days=1)))
    assert totals["hunts"] == 2
    assert totals["found"] == 150
    assert totals["filtered_out"] == 120
    assert (totals["dup_url"], totals["dup_ct"], totals["dup_cooldown"]) == (7, 3, 1)
    assert totals["new"] == 19
    assert (totals["capped"], totals["queued"], totals["applied_inline"]) == (1, 10, 8)
    assert totals["duration_ms"] == 1500
    # Merged, most frequent first.
    assert totals["filter_reasons"] == {"level": 90, "location": 30}
    assert set(hunt_runs.COUNT_COLUMNS) <= set(totals)


def test_sum_window_on_empty_table_is_all_zeros(runs_db) -> None:
    totals = hunt_runs.sum_window("2000-01-01T00:00:00+00:00")
    assert totals["hunts"] == 0
    assert all(totals[c] == 0 for c in hunt_runs.COUNT_COLUMNS)
    assert totals["filter_reasons"] == {}


def test_record_raises_on_broken_db(tmp_path, monkeypatch) -> None:
    """The caller's best_effort needs the exception — never swallow here."""
    bad = tmp_path / "not_a_dir" / "x.db"  # parent missing -> sqlite cannot open
    monkeypatch.setattr(hunt_runs, "DB_PATH", bad)
    with pytest.raises(Exception):  # noqa: B017 — any DB error is the contract
        hunt_runs.record_hunt(trigger="scheduled", sources=["a"])


# ── Part 2: the hunt loop writes the row the Telegram summary shows ──────────

URL_NEW = "https://justjoin.it/job-offer/acme-senior-angular"
URL_DUP_URL = "https://justjoin.it/job-offer/globex-angular"
URL_DUP_CT = "https://justjoin.it/job-offer/initech-angular-repost"
URL_FILTERED = "https://justjoin.it/job-offer/hooli-junior-angular"


def _job(title: str, company: str, url: str) -> Job:
    return Job(
        title=title, company=company, location="Remote", salary=None, url=url, source="justjoin"
    )


def _mixed_jobs() -> list[Job]:
    return [
        _job("Senior Angular Developer", "Acme", URL_NEW),  # new
        _job("Angular Developer", "Globex", URL_DUP_URL),  # URL known to tracker
        _job("Angular Developer", "Initech", URL_DUP_CT),  # company+title known, new URL
        # "junior" is in the default profile's exclude_levels -> filter reason "level".
        _job("Junior Angular Developer", "Hooli", URL_FILTERED),  # filtered
    ]


class _FakeSource:
    name = "justjoin"
    manual_only = False

    def __init__(self, jobs: list[Job]) -> None:
        self._jobs = jobs

    def search(self) -> list[Job]:
        return list(self._jobs)


@pytest.fixture
def hunt_db(tracker_db, monkeypatch):
    """Every lazily-created side table lands in the isolated tracker db."""
    import hunter.postings_seen as ps
    import hunter.source_health as sh

    monkeypatch.setattr(hunt_runs, "DB_PATH", tracker_db)
    monkeypatch.setattr(be, "DB_PATH", tracker_db)
    monkeypatch.setattr(ps, "DB_PATH", tracker_db)
    monkeypatch.setattr(sh, "DB_PATH", tracker_db)
    return tracker_db


def _seed_tracker(jobs: list[Job]) -> None:
    """Make URL_DUP_URL known by URL and URL_DUP_CT known by company+title."""
    from hunter import tracker

    tracker.add_skipped(jobs[1])  # same URL as the hunt's second job
    tracker.add_skipped(
        _job("Angular Developer", "Initech", "https://justjoin.it/job-offer/initech-original")
    )


def _run(jobs: list[Job], *, sent: list[str], batch=None, **overrides) -> None:
    async def fake_send_text(_ctx, text, **_kw):
        sent.append(text)

    flags = {
        "hunter.main.AUTO_APPLY": False,
        "hunter.main.APPLY_QUEUE_ENABLED": False,
        "hunter.main.HUNT_RUNS_ENABLED": True,
    }
    flags.update(overrides)
    with ExitStack() as stack:
        stack.enter_context(patch("hunter.main.ALL_SOURCES", [_FakeSource(jobs)]))
        stack.enter_context(patch("hunter.main.send_job_cards", AsyncMock()))
        stack.enter_context(patch("hunter.main.send_text", fake_send_text))
        stack.enter_context(patch("hunter.main._check_apply_ready", return_value=None))
        stack.enter_context(patch("hunter.main._auto_apply_all", batch or AsyncMock()))
        for target, value in flags.items():
            stack.enter_context(patch(target, value))
        asyncio.run(run_hunt(MagicMock()))


def _summary_numbers(sent: list[str]) -> dict:
    """Parse the funnel back out of the ONE hunt report message."""
    report = next(t for t in sent if t.startswith("🔍 <b>Hunt "))
    m_filter = re.search(r"(\d+) raw -> <b>(\d+)</b> passed \((\d+) filtered out\)", report)
    m_dedup = re.search(r"(\d+) passed -> <b>(\d+)</b> new", report)
    m_skip = re.search(r"Skipped: (\d+) by URL, (\d+) by company\+title", report)
    assert m_filter and m_dedup and m_skip, report
    reasons = {reason: int(cnt) for cnt, reason in re.findall(r"✂️ (\d+) by (\S+)", report)}
    return {
        "found": int(m_filter.group(1)),
        "passed": int(m_filter.group(2)),
        "filtered_out": int(m_filter.group(3)),
        "new": int(m_dedup.group(2)),
        "dup_url": int(m_skip.group(1)),
        "dup_ct": int(m_skip.group(2)),
        "filter_reasons": reasons,
    }


def _rows(db) -> list[dict]:
    with get_db(db) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hunt_runs'"
        ).fetchone()
        if not exists:
            return []
    return hunt_runs.recent_hunts(limit=50)


def _failures(db, subsystem: str) -> int:
    with get_db(db) as conn:
        row = conn.execute(
            "SELECT consecutive_failures FROM subsystem_health WHERE subsystem = ?",
            (subsystem,),
        ).fetchone()
    return int(row["consecutive_failures"]) if row else 0


def test_hunt_row_equals_telegram_summary(hunt_db) -> None:
    jobs = _mixed_jobs()
    _seed_tracker(jobs)
    sent: list[str] = []
    _run(jobs, sent=sent)

    rows = _rows(hunt_db)
    assert len(rows) == 1, "exactly one row per hunt"
    row = rows[0]
    tg = _summary_numbers(sent)

    # The fixture really exercised every branch of the funnel.
    assert tg == {
        "found": 4,
        "passed": 3,
        "filtered_out": 1,
        "new": 1,
        "dup_url": 1,
        "dup_ct": 1,
        "filter_reasons": {"level": 1},
    }
    assert row["found"] == tg["found"]
    assert row["filtered_out"] == tg["filtered_out"]
    assert row["filter_reasons"] == tg["filter_reasons"]
    assert row["dup_url"] == tg["dup_url"]
    assert row["dup_ct"] == tg["dup_ct"]
    assert row["new"] == tg["new"]
    assert row["found"] - row["filtered_out"] == tg["passed"]
    assert row["dup_cooldown"] == 0
    # Manual mode: cards, nothing queued or applied.
    assert (row["capped"], row["queued"], row["applied_inline"]) == (0, 0, 0)
    assert row["trigger"] == "scheduled"
    assert row["sources"] == ["justjoin"]
    assert row["duration_ms"] >= 0
    assert "+00:00" in row["ts"]


def test_manual_hunt_command_is_recorded_as_manual(hunt_db) -> None:
    sent: list[str] = []
    with (
        patch("hunter.main.AUTO_APPLY", False),
        patch("hunter.main.ALL_SOURCES", [_FakeSource(_mixed_jobs())]),
        patch("hunter.main.send_job_cards", AsyncMock()),
        patch("hunter.main.send_text", AsyncMock(side_effect=lambda _c, t, **_k: sent.append(t))),
    ):
        asyncio.run(run_hunt(MagicMock(), notify_queued=True))
    assert _rows(hunt_db)[0]["trigger"] == "manual"


def test_queue_path_records_queued_and_capped(hunt_db) -> None:
    jobs = [
        _job("Senior Angular Developer", f"Co{i}", f"https://justjoin.it/job-offer/co{i}")
        for i in range(3)
    ]
    sent: list[str] = []
    with patch("hunter.main.MAX_JOBS_PER_RUN", 2):
        _run(
            jobs,
            sent=sent,
            **{"hunter.main.AUTO_APPLY": True, "hunter.main.APPLY_QUEUE_ENABLED": True},
        )

    row = _rows(hunt_db)[0]
    assert row["new"] == 3
    assert row["queued"] == 2
    assert row["capped"] == 1
    assert row["applied_inline"] == 0
    from hunter import tracker

    assert sum(1 for j in jobs if tracker.lookup_url(j.url)) == 2
    assert any("Queued 2" in t for t in sent)


def test_inline_path_records_applied_inline_before_the_batch(hunt_db) -> None:
    jobs = [
        _job("Senior Angular Developer", f"Co{i}", f"https://justjoin.it/job-offer/in{i}")
        for i in range(2)
    ]
    seen_at_batch: list[int] = []

    async def fake_batch(_ctx, batch):
        # The row must already exist when the (potentially hours-long)
        # inline batch starts.
        seen_at_batch.append(len(_rows(hunt_db)))

    sent: list[str] = []
    _run(
        jobs,
        sent=sent,
        batch=fake_batch,
        **{"hunter.main.AUTO_APPLY": True, "hunter.main.APPLY_QUEUE_ENABLED": False},
    )

    assert seen_at_batch == [1]
    rows = _rows(hunt_db)
    assert len(rows) == 1  # the finally-flush is idempotent — no second row
    assert rows[0]["applied_inline"] == 2
    assert rows[0]["queued"] == 0


def test_flag_off_writes_nothing_and_hunt_is_unchanged(hunt_db) -> None:
    jobs = _mixed_jobs()
    _seed_tracker(jobs)
    sent: list[str] = []
    with patch("hunter.main.record_hunt", side_effect=AssertionError("must not be called")):
        _run(jobs, sent=sent, **{"hunter.main.HUNT_RUNS_ENABLED": False})

    assert _rows(hunt_db) == []
    assert _summary_numbers(sent)["new"] == 1


def test_no_row_when_dedup_db_read_fails(hunt_db) -> None:
    sent: list[str] = []
    with patch("hunter.main.get_known_urls", side_effect=RuntimeError("db on fire")):
        _run(_mixed_jobs(), sent=sent)
    assert _rows(hunt_db) == []
    assert any("Failed to read tracker DB" in t for t in sent)


def test_record_failure_is_swallowed_and_counted(hunt_db) -> None:
    sent: list[str] = []
    with patch("hunter.main.record_hunt", side_effect=RuntimeError("db on fire")):
        _run(_mixed_jobs(), sent=sent)  # must not raise

    assert _summary_numbers(sent)["found"] == 4  # the hunt itself completed
    assert _failures(hunt_db, "hunt.record") == 1

    # A healthy hunt afterwards resets the counter (recovery semantics).
    _run(_mixed_jobs(), sent=[])
    assert _failures(hunt_db, "hunt.record") == 0
    assert len(_rows(hunt_db)) == 1
