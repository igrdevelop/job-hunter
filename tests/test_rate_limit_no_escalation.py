"""Step 5: a transient 429 must not escalate the permanent fail counter."""

import asyncio
from unittest.mock import AsyncMock


from hunter import main
from hunter.apply_shared import is_rate_limit_error
from hunter.models import Job


# ── is_rate_limit_error ───────────────────────────────────────────────────────

def test_is_rate_limit_error_from_response_status():
    class _Resp:
        status_code = 429

    err = Exception("boom")
    err.response = _Resp()
    assert is_rate_limit_error(err) is True


def test_is_rate_limit_error_from_message():
    assert is_rate_limit_error(Exception("429 Client Error: Too Many Requests"))
    assert is_rate_limit_error(Exception("Too Many Requests"))


def test_is_rate_limit_error_false_for_other():
    assert is_rate_limit_error(Exception("404 Not Found")) is False

    class _Resp:
        status_code = 500

    err = Exception("server error")
    err.response = _Resp()
    assert is_rate_limit_error(err) is False


# ── _retry_failed does not escalate on rate_limited ───────────────────────────

def _job(n: int) -> Job:
    return Job(
        title=f"Role {n}",
        company=f"Co{n}",
        location="Remote",
        salary=None,
        url=f"https://www.pracuj.pl/praca/x,oferta,{n}",
        source="pracuj",
    )


def test_retry_rate_limited_does_not_increment_fail_count(monkeypatch):
    jobs = [_job(0), _job(1)]
    increments: list[str] = []

    monkeypatch.setattr(main, "get_failed_jobs", lambda: list(jobs))
    monkeypatch.setattr(main, "increment_fail_count", lambda url: increments.append(url) or 1)
    monkeypatch.setattr(main, "remove_failed", lambda url: None)
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_sync_to_sheets", AsyncMock())
    monkeypatch.setattr(main, "_upload_to_drive", AsyncMock())
    monkeypatch.setattr(
        main, "_run_apply_agent", AsyncMock(side_effect=["rate_limited", "rate_limited"])
    )

    asyncio.run(main._retry_failed(context=None))

    # No escalation for transient 429s.
    assert increments == []
    msgs = " ".join(c.args[1] for c in main.send_text.await_args_list)
    assert "rate-limited" in msgs.lower()


def test_retry_real_fail_still_increments(monkeypatch):
    jobs = [_job(0)]
    increments: list[str] = []

    monkeypatch.setattr(main, "get_failed_jobs", lambda: list(jobs))
    monkeypatch.setattr(main, "increment_fail_count", lambda url: increments.append(url) or 1)
    monkeypatch.setattr(main, "remove_failed", lambda url: None)
    monkeypatch.setattr(main, "APPLY_DELAY_SEC", 0)
    monkeypatch.setattr(main, "send_text", AsyncMock())
    monkeypatch.setattr(main, "_sync_to_sheets", AsyncMock())
    monkeypatch.setattr(main, "_upload_to_drive", AsyncMock())
    monkeypatch.setattr(main, "_run_apply_agent", AsyncMock(side_effect=["fail"]))

    asyncio.run(main._retry_failed(context=None))

    assert increments == [jobs[0].url]
