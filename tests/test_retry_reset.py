"""M3 (docs/LLM_OUTAGE_RESILIENCE_PLAN.md): /retry_reset revives gave-up FAIL rows.

A FAIL row at fail_count >= MAX_FAIL_RETRIES is permanently invisible to
get_failed_jobs(); reset_fail_counts() is the only path back into the retry
loop. The key regression guard here: after a reset, get_failed_jobs() must
actually return the row again — that is the whole point of the milestone.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from hunter.models import Job


def _job(n: int) -> Job:
    return Job(
        title=f"Role {n}",
        company=f"Co{n}",
        location="Remote",
        salary=None,
        url=f"https://example.com/job/{n}",
        source="test",
    )


def _add_fail(n: int, fail_count: int) -> Job:
    from hunter import tracker

    job = _job(n)
    tracker.add_failed(job)
    if fail_count:
        with tracker.get_db(tracker.DB_PATH) as conn:
            conn.execute(
                "UPDATE applications SET fail_count=? WHERE url_norm=?",
                (fail_count, tracker.normalize_url(job.url)),
            )
    return job


# ── tracker.get_gave_up_failed ────────────────────────────────────────────────


def test_gave_up_rows_listed(tracker_db):
    from hunter import tracker

    _add_fail(1, tracker.MAX_FAIL_RETRIES)  # gave up
    _add_fail(2, 1)  # still retryable
    rows = tracker.get_gave_up_failed()
    assert len(rows) == 1
    assert rows[0]["company"] == "Co1"
    assert rows[0]["fail_count"] == tracker.MAX_FAIL_RETRIES


def test_no_gave_up_rows(tracker_db):
    from hunter import tracker

    _add_fail(1, 1)
    assert tracker.get_gave_up_failed() == []


# ── tracker.reset_fail_counts ─────────────────────────────────────────────────


def test_reset_all_revives_gave_up_row(tracker_db):
    """The M3 core guarantee: reset → the row is retryable again."""
    from hunter import tracker

    job = _add_fail(1, tracker.MAX_FAIL_RETRIES)

    # Dead: invisible to the retry loop.
    assert [j.url for j in tracker.get_failed_jobs()] == []

    changed = tracker.reset_fail_counts()
    assert changed == 1

    # Alive again: the next RETRY_FAILED_TIMES slot picks it up.
    assert [j.url for j in tracker.get_failed_jobs()] == [job.url]
    assert tracker.get_gave_up_failed() == []


def test_reset_single_url_leaves_others(tracker_db):
    from hunter import tracker

    j1 = _add_fail(1, tracker.MAX_FAIL_RETRIES)
    _add_fail(2, tracker.MAX_FAIL_RETRIES)

    changed = tracker.reset_fail_counts([j1.url])
    assert changed == 1
    assert [j.url for j in tracker.get_failed_jobs()] == [j1.url]
    assert len(tracker.get_gave_up_failed()) == 1  # j2 untouched


def test_reset_zero_counts_is_noop(tracker_db):
    from hunter import tracker

    _add_fail(1, 0)
    assert tracker.reset_fail_counts() == 0


def test_reset_unknown_or_empty_urls(tracker_db):
    from hunter import tracker

    _add_fail(1, tracker.MAX_FAIL_RETRIES)
    assert tracker.reset_fail_counts(["https://other.example.com/x"]) == 0
    assert tracker.reset_fail_counts([""]) == 0
    # Non-FAIL rows are never touched even on reset-all.
    assert tracker.reset_fail_counts() == 1


def test_reset_does_not_touch_non_fail_rows(tracker_db):
    from hunter import tracker

    job = _job(9)
    tracker.add_skipped(job)
    with tracker.get_db(tracker.DB_PATH) as conn:
        conn.execute(
            "UPDATE applications SET fail_count=5 WHERE url_norm=?",
            (tracker.normalize_url(job.url),),
        )
    assert tracker.reset_fail_counts() == 0


# ── /retry_reset command ──────────────────────────────────────────────────────


def _run_cmd(args: list[str]):
    from hunter.commands.retry_reset import cmd_retry_reset

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args
    asyncio.run(cmd_retry_reset(update, context))
    return update.message.reply_text.await_args.args[0]


def test_cmd_no_args_reports_without_mutating(tracker_db):
    from hunter import tracker

    _add_fail(1, tracker.MAX_FAIL_RETRIES)
    text = _run_cmd([])
    assert "Co1" in text
    assert "1 FAIL row" in text
    # Report must not mutate: the row is still gave-up.
    assert len(tracker.get_gave_up_failed()) == 1


def test_cmd_all_resets(tracker_db):
    from hunter import tracker

    _add_fail(1, tracker.MAX_FAIL_RETRIES)
    _add_fail(2, 2)
    text = _run_cmd(["all"])
    assert "2" in text and "Reset" in text
    assert tracker.get_gave_up_failed() == []


def test_cmd_single_url(tracker_db):
    from hunter import tracker

    j1 = _add_fail(1, tracker.MAX_FAIL_RETRIES)
    _add_fail(2, tracker.MAX_FAIL_RETRIES)
    _run_cmd([j1.url])
    assert [j.url for j in tracker.get_failed_jobs()] == [j1.url]


def test_cmd_empty_db_message(tracker_db):
    text = _run_cmd([])
    assert "No gave-up" in text


def test_cmd_registered_in_dispatcher():
    """The lazy re-export must resolve (guards the _LAZY_ATTRS wiring)."""
    from hunter import telegram_bot

    assert callable(telegram_bot.cmd_retry_reset)
