"""M4 (docs/HUNT_APPLY_SPLIT_PLAN.md): fail audit log + /fails command.

Every non-ok, non-manual, non-llm_outage apply outcome (fail, cli_timeout,
rate_limited) appends one JSON line to logs/apply_failures.jsonl. Tests use
`set_log_path_for_tests` to redirect at a tmp file so nothing touches the
real log.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def failures_log(tmp_path):
    from hunter import apply_failures_log

    path = tmp_path / "apply_failures.jsonl"
    apply_failures_log.set_log_path_for_tests(path)
    yield path
    apply_failures_log.set_log_path_for_tests(None)


# ── log_apply_failure ─────────────────────────────────────────────────────────


def test_logs_fail_outcome(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(
        url="https://example.com/1",
        outcome="fail",
        company="Acme",
        title="Dev",
        exit_code=1,
        error="boom",
        duration_sec=1.234,
        cli_mode=False,
    )
    lines = failures_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["url"] == "https://example.com/1"
    assert record["outcome"] == "fail"
    assert record["company"] == "Acme"
    assert record["title"] == "Dev"
    assert record["exit_code"] == 1
    assert record["error"] == "boom"
    assert record["duration_sec"] == 1.2
    assert record["cli_mode"] is False
    assert "ts" in record


def test_logs_cli_timeout_outcome(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(url="https://example.com/2", outcome="cli_timeout", cli_mode=True)
    lines = failures_log.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[0])
    assert record["outcome"] == "cli_timeout"
    assert record["cli_mode"] is True


def test_logs_rate_limited_outcome(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(url="https://example.com/3", outcome="rate_limited")
    lines = failures_log.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["outcome"] == "rate_limited"


def test_does_not_log_ok_outcome(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(url="https://example.com/4", outcome="ok")
    assert not failures_log.exists() or failures_log.read_text(encoding="utf-8").strip() == ""


def test_does_not_log_manual_outcome(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(url="https://example.com/5", outcome="manual")
    assert not failures_log.exists() or failures_log.read_text(encoding="utf-8").strip() == ""


def test_does_not_log_llm_outage_outcome(failures_log):
    """llm_outage is global account state, tracked separately (M2) — not per-vacancy."""
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(url="https://example.com/6", outcome="llm_outage")
    assert not failures_log.exists() or failures_log.read_text(encoding="utf-8").strip() == ""


def test_error_truncated(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(url="https://example.com/7", outcome="fail", error="x" * 1000)
    record = json.loads(failures_log.read_text(encoding="utf-8").strip())
    assert len(record["error"]) == 500


def test_logging_never_raises(failures_log, monkeypatch):
    """Best-effort: a broken write must not bubble up."""
    from hunter import apply_failures_log

    monkeypatch.setattr(
        apply_failures_log,
        "_get_failure_logger",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    apply_failures_log.log_apply_failure(url="https://example.com/8", outcome="fail")  # no raise


def test_stale_stream_handler_does_not_swallow_writes(failures_log):
    """Regression: a non-file handler left on the logger must not block the JSONL write.

    CI failed with FileNotFoundError on the tmp path while still showing the
    JSON in pytest's captured log — classic early-return on a stale
    StreamHandler (propagate to root) that never opened the override file.
    """
    import logging

    from hunter import apply_failures_log
    from hunter.apply_failures_log import log_apply_failure

    log_obj = logging.getLogger(apply_failures_log._FAILURE_LOGGER_NAME)
    log_obj.addHandler(logging.StreamHandler())
    log_apply_failure(url="https://example.com/stale", outcome="fail", company="Stale")
    assert failures_log.exists()
    assert "Stale" in failures_log.read_text(encoding="utf-8")


# ── read_last_failures ────────────────────────────────────────────────────────


def test_read_last_failures_order_and_limit(failures_log):
    from hunter.apply_failures_log import log_apply_failure, read_last_failures

    for i in range(5):
        log_apply_failure(url=f"https://example.com/{i}", outcome="fail", company=f"Co{i}")
    records = read_last_failures(3)
    assert len(records) == 3
    assert [r["company"] for r in records] == ["Co2", "Co3", "Co4"]


def test_read_last_failures_missing_file(tmp_path):
    from hunter import apply_failures_log

    apply_failures_log.set_log_path_for_tests(tmp_path / "does_not_exist.jsonl")
    try:
        assert apply_failures_log.read_last_failures(5) == []
    finally:
        apply_failures_log.set_log_path_for_tests(None)


def test_read_last_failures_skips_corrupt_lines(failures_log):
    from hunter.apply_failures_log import read_last_failures

    with open(failures_log, "w", encoding="utf-8") as fh:
        fh.write('{"outcome": "fail", "company": "Good"}\n')
        fh.write("not json\n")
    records = read_last_failures(5)
    assert len(records) == 1
    assert records[0]["company"] == "Good"


# ── /fails command ─────────────────────────────────────────────────────────────


def _run_cmd(args: list[str]):
    from hunter.commands.fails import cmd_fails

    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.args = args
    asyncio.run(cmd_fails(update, context))
    return update.message.reply_text.await_args.args[0]


def test_cmd_fails_empty_log(failures_log):
    text = _run_cmd([])
    assert "No entries" in text


def test_cmd_fails_lists_entries(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(url="https://example.com/1", outcome="fail", company="Acme", title="Dev")
    log_apply_failure(url="https://example.com/2", outcome="cli_timeout", company="Beta")
    text = _run_cmd([])
    assert "Acme" in text
    assert "Beta" in text
    assert "cli_timeout" in text


def test_cmd_fails_respects_n_arg(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    for i in range(5):
        log_apply_failure(url=f"https://example.com/{i}", outcome="fail", company=f"Co{i}")
    text = _run_cmd(["2"])
    assert "Co3" in text and "Co4" in text
    assert "Co0" not in text


def test_cmd_fails_invalid_n_falls_back_to_default(failures_log):
    from hunter.apply_failures_log import log_apply_failure

    log_apply_failure(url="https://example.com/1", outcome="fail", company="Acme")
    text = _run_cmd(["not-a-number"])
    assert "Acme" in text


def test_cmd_fails_registered_in_dispatcher():
    from hunter import telegram_bot

    assert callable(telegram_bot.cmd_fails)
