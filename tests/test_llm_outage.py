"""M1 (docs/LLM_OUTAGE_RESILIENCE_PLAN.md): LLM billing/auth outages are a
distinct, non-escalating outcome.

A drained balance / bad key is a GLOBAL account state, not a property of the
vacancy — it must never write FAIL rows or burn fail_count. The two guards
that must fail if M1 is reverted:
  - test_auto_apply_outage_writes_no_fail_row_and_stops
  - test_retry_outage_leaves_fail_count_untouched
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from hunter import main
from hunter.models import Job
from llm_client import (
    LLMError,
    LLMOutageError,
    LLMRateLimitError,
    is_outage_signature,
)


@pytest.fixture(autouse=True)
def _isolated_config_db(tmp_path, monkeypatch):
    """Route the config KV table to a tmp DB for EVERY test in this file.

    The batch-loop outage branches arm the M2 pause (hunter.llm_outage) as a
    side effect — without this, running these tests would write
    `llm_outage_until` into the REAL repo tracker.db and make unrelated
    run_hunt tests skip their apply step (observed 2026-07-18: 3 ordering-
    dependent failures in test_hunt_queue_delivery/test_main_manual_only_
    partition after this file ran first).
    """
    monkeypatch.setattr("hunter.llm_profiles._get_db_path", lambda: tmp_path / "tracker.db")


# ── is_outage_signature classification table ──────────────────────────────────


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        # Billing-shaped 400s (Anthropic drained balance) → outage
        (400, "Your credit balance is too low to access the Anthropic API", True),
        (400, "Please go to Plans & Billing to upgrade or purchase credits", True),
        # Plain 400 (a genuine request bug) must stay a normal error —
        # misclassifying a code bug as an outage would retry it forever.
        (400, "max_tokens: field required", False),
        (400, "Unexpected value for output_config.effort", False),
        # Auth/permission/payment statuses → always outage, message irrelevant
        (401, "invalid x-api-key", True),
        (402, "Payment Required", True),
        (403, "access denied", True),
        # OpenAI drained quota rides a 429 — message check catches it
        (None, "Error code: 429 - insufficient_quota: check your plan and billing details", True),
        (None, "You exceeded your current quota, please check your plan and billing", True),
        # Genuine throttling stays NOT an outage (must remain retryable)
        (None, "429 Too Many Requests, retry after 3s", False),
        (429, "rate_limit_error: too many tokens per minute", False),
        # Spend-limit phrasing
        (None, "you have reached your monthly spend limit", True),
    ],
)
def test_outage_signature_table(status, message, expected):
    assert is_outage_signature(status, message) is expected


def test_outage_is_llm_error_subclass():
    """Existing `except LLMError` callers must keep catching outages."""
    assert issubclass(LLMOutageError, LLMError)
    assert not issubclass(LLMOutageError, LLMRateLimitError)


def test_call_llm_does_not_retry_outage(monkeypatch):
    """An outage propagates immediately — no backoff ladder, no fallback model."""
    import llm_client

    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise LLMOutageError("credit balance is too low")

    monkeypatch.setattr(llm_client, "_call_anthropic", _boom)
    monkeypatch.setattr(llm_client.time, "sleep", lambda s: pytest.fail("must not sleep"))

    with pytest.raises(LLMOutageError):
        llm_client.call_llm("sys", "user", provider="anthropic", api_key="k")
    assert calls["n"] == 1


# ── exit-code plumbing: 46 → "llm_outage" ────────────────────────────────────


class _FakeProc:
    def __init__(self, returncode: int):
        self.returncode = returncode

    async def communicate(self):
        return b"", b""


def _job(n: int = 0) -> Job:
    return Job(
        title=f"Role {n}",
        company=f"Co{n}",
        location="Remote",
        salary=None,
        url=f"https://example.com/job/{n}",
        source="test",
    )


def test_subprocess_exit_46_maps_to_llm_outage(monkeypatch, tmp_path):
    from hunter.services import apply_service

    async def _fake_exec(*a, **k):
        return _FakeProc(46)

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", _fake_exec)
    outcome = asyncio.run(
        apply_service.run_apply_agent_subprocess(
            job=_job(),
            timeout_sec=5,
            apply_agent_path=tmp_path / "apply_agent.py",
            python_executable="python",
        )
    )
    assert outcome == "llm_outage"


def test_url_variant_exit_46_maps_to_llm_outage(monkeypatch, tmp_path):
    from hunter.services import apply_service

    async def _fake_exec(*a, **k):
        return _FakeProc(46)

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", _fake_exec)
    outcome, detail = asyncio.run(
        apply_service.run_apply_agent_for_url(
            url="https://example.com/job/1",
            timeout_sec=5,
            apply_agent_path=tmp_path / "apply_agent.py",
            python_executable="python",
        )
    )
    assert outcome == "llm_outage"
    assert "outage" in detail.lower()


def test_exit_code_constants_match():
    from hunter.apply_shared import APPLY_LLM_OUTAGE_EXIT_CODE
    from hunter.bot import apply_runner
    from hunter.services import apply_service

    assert APPLY_LLM_OUTAGE_EXIT_CODE == 46
    assert apply_service._APPLY_LLM_OUTAGE_EXIT_CODE == 46
    assert apply_runner._APPLY_LLM_OUTAGE_EXIT_CODE == 46


# ── _auto_apply_all: no FAIL row, immediate stop ──────────────────────────────


def test_auto_apply_outage_writes_no_fail_row_and_stops(monkeypatch):
    jobs = [_job(0), _job(1), _job(2)]
    failed_writes: list[str] = []

    monkeypatch.setattr(main, "add_failed", lambda job: failed_writes.append(job.url))
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_deliver_now", AsyncMock())
    runner = AsyncMock(side_effect=["ok", "llm_outage", "fail"])
    monkeypatch.setattr(main, "_run_apply_agent", runner)

    asyncio.run(main._auto_apply_all(context=None, jobs=jobs))

    # Job 1 hit the outage: no FAIL row for it, and job 2 was never attempted.
    assert failed_writes == []
    assert runner.await_count == 2
    msgs = " ".join(c.args[1] for c in main.send_text.await_args_list)
    assert "LLM outage" in msgs


# ── _retry_failed: fail_count untouched, immediate stop ───────────────────────


def test_retry_outage_leaves_fail_count_untouched(monkeypatch):
    jobs = [_job(0), _job(1)]
    increments: list[str] = []

    monkeypatch.setattr(main, "get_failed_jobs", lambda: list(jobs))
    monkeypatch.setattr(main, "increment_fail_count", lambda url: increments.append(url) or 1)
    monkeypatch.setattr(main, "remove_failed", lambda url: None)
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_deliver_now", AsyncMock())
    runner = AsyncMock(side_effect=["llm_outage", "fail"])
    monkeypatch.setattr(main, "_run_apply_agent", runner)

    asyncio.run(main._retry_failed(context=None))

    # No escalation, and the second row was never attempted.
    assert increments == []
    assert runner.await_count == 1
    msgs = " ".join(c.args[1] for c in main.send_text.await_args_list)
    assert "LLM outage" in msgs
