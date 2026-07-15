"""
tests/conftest.py — shared fixtures for the test suite.

tracker_db  — isolated SQLite tracker DB (monkeypatches tracker.DB_PATH).
             Use in any test that needs a clean tracker (replaces the old
             monkeypatch of tracker.TRACKER_PATH + openpyxl setup).
fake_llm    — routes llm_client.call_llm by prompt shape to configurable
             fixture responses (generation / judge / verdict), so a test can
             drive a real pipeline without hitting a real LLM. See
             tests/test_golden_apply_e2e.py for the primary consumer.
"""

from pathlib import Path

import pytest

from hunter import tracker as tracker_module
from hunter.db import init_db


@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch) -> None:
    """Guarantee no test ever sends a real Telegram message.

    The apply pipeline's ``notify()`` / ``send_telegram_documents()`` short-circuit
    when the bot token / chat id are empty. Several tests drive the real pipeline
    (main_api / main_cli) without mocking ``notify``; with a populated ``.env`` those
    calls would hit api.telegram.org and spam the live chat. Blank the module-level
    constants for the duration of every test. monkeypatch restores them afterwards,
    so the real bot is unaffected.
    """
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_BOT_TOKEN", "", raising=False)
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_CHAT_ID", "", raising=False)


@pytest.fixture()
def tracker_db(tmp_path: Path, monkeypatch) -> Path:
    """Return a path to a fresh, isolated SQLite tracker DB.

    Also monkeypatches ``hunter.tracker.DB_PATH`` so all tracker.py functions
    use this DB for the duration of the test.

    Usage::

        def test_something(tracker_db):
            from hunter import tracker
            tracker.add_skipped(job)
            assert tracker.is_known(job.url)
    """
    db = tmp_path / "tracker.db"
    # Prevent auto-migration from a real tracker.xlsx
    init_db(db, xlsx_path=tmp_path / "no_tracker.xlsx")
    monkeypatch.setattr(tracker_module, "DB_PATH", db)
    return db


class FakeLLM:
    """Callable stand-in for ``llm_client.call_llm``, routed by prompt shape.

    Every generation-pipeline LLM call (main generation, validation repair,
    ATS boost, claim judge, independent PDF verdict) goes through
    ``from llm_client import call_llm`` INSIDE the calling function body — a
    lazy import re-resolved at call time, not bound at module-import time —
    so patching ``llm_client.call_llm`` once here transparently intercepts
    every call site, however deep in the pipeline.

    Routing (most specific first):
      1. system_prompt contains the ATS-verdict marker text -> verdict_response
      2. system_prompt contains the outreach-draft marker    -> outreach_response
      3. model == JUDGE_MODEL                                -> judge_response
      4. anything else (generation / repair / ATS-boost)      -> generation_response
    """

    # Verbatim substring of hunter.ats_checker._LLM_SYSTEM, used by both the
    # in-loop reviewer and ats_pdf_roundtrip.run_llm_verdict.
    _VERDICT_MARKER = "Applicant Tracking System (ATS) evaluating"
    # Verbatim substring of hunter.outreach._SYSTEM_PROMPT.
    _OUTREACH_MARKER = "ghost-write a short LinkedIn outreach note"

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.generation_response: dict | None = None
        self.judge_response: dict = {"violations": []}
        self.verdict_response: dict | None = None
        self.outreach_response: dict = {
            "message": "Hi — saw your posting, let's connect.",
            "message_en": None,
        }

    def __call__(
        self,
        *,
        system_prompt: str = "",
        user_message: str = "",
        provider: str = "",
        model: str = "",
        api_key: str = "",
        **kwargs,
    ) -> dict:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_message": user_message,
                "provider": provider,
                "model": model,
                **kwargs,
            }
        )
        if self._VERDICT_MARKER in system_prompt:
            if self.verdict_response is None:
                raise AssertionError("fake_llm: verdict call arrived but no verdict_response set")
            return dict(self.verdict_response)

        if self._OUTREACH_MARKER in system_prompt:
            return dict(self.outreach_response)

        from hunter.config import JUDGE_MODEL

        if model == JUDGE_MODEL:
            return dict(self.judge_response)

        if self.generation_response is None:
            raise AssertionError("fake_llm: generation call arrived but no generation_response set")
        return dict(self.generation_response)


@pytest.fixture()
def fake_llm(monkeypatch) -> FakeLLM:
    """Patches llm_client.call_llm with a FakeLLM router. See FakeLLM docstring."""
    import llm_client

    fake = FakeLLM()
    monkeypatch.setattr(llm_client, "call_llm", fake)
    return fake
