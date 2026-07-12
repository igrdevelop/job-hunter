"""Unit tests for the usage-accounting plumbing in llm_client.

Exercises account_usage / push_usage_log / pop_usage_log without actually
calling out to Anthropic — _record_usage is the single integration point and
we feed it synthetic Usage objects.
"""

from unittest.mock import MagicMock

from llm_client import (
    _USAGE_STACK,
    _record_usage,
    account_usage,
    pop_usage_log,
    push_usage_log,
)


def test_account_usage_collects_records_from_nested_calls() -> None:
    with account_usage() as log:
        _record_usage(
            "claude-sonnet-4-6",
            MagicMock(
                input_tokens=1000,
                output_tokens=500,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )
        _record_usage(
            "claude-haiku-4-5-20251001",
            MagicMock(
                input_tokens=200,
                output_tokens=100,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )
    assert len(log) == 2
    assert log[0]["model"] == "claude-sonnet-4-6"
    assert log[0]["input_tokens"] == 1000
    assert log[1]["model"] == "claude-haiku-4-5-20251001"


def test_record_usage_silently_ignored_when_no_active_log() -> None:
    # Defensive: a stray _record_usage outside any account_usage block must
    # not crash. Real apply pipeline always pushes, but ad-hoc test scripts
    # and lone /apply CLI invocations don't.
    assert len(_USAGE_STACK) == 0
    _record_usage("sonnet-4-6", MagicMock(input_tokens=1))
    assert len(_USAGE_STACK) == 0  # still empty, no record was kept anywhere


def test_account_usage_pops_on_exception() -> None:
    # Even if the body raises, the stack frame must pop so the next pipeline
    # run starts with a clean accounting frame.
    assert len(_USAGE_STACK) == 0
    try:
        with account_usage():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert len(_USAGE_STACK) == 0


def test_push_pop_manual_pair_for_pipelines_with_early_returns() -> None:
    # apply_api uses the manual push/pop pair because main_api has many
    # sys.exit / return paths. Verify the manual API works identically to
    # the context manager.
    log = push_usage_log()
    _record_usage(
        "sonnet-4-6",
        MagicMock(
            input_tokens=500,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )
    popped = pop_usage_log()
    assert popped is log
    assert len(popped) == 1


def test_pop_with_empty_stack_returns_none() -> None:
    # Defensive: pop without a matching push shouldn't raise.
    assert len(_USAGE_STACK) == 0
    assert pop_usage_log() is None


def test_record_usage_supports_dict_input() -> None:
    # OpenAI provider remaps to dict; should be accepted identically.
    with account_usage() as log:
        _record_usage("gpt-x", {"input_tokens": 100, "output_tokens": 50})
    assert log[0]["input_tokens"] == 100
    assert log[0]["output_tokens"] == 50
    assert log[0]["cache_read_input_tokens"] == 0


def test_record_usage_tolerates_attribute_missing() -> None:
    # SDK Usage objects don't always carry every field (e.g. cache_* are
    # absent in older response versions). Missing → treated as 0.
    class StubUsage:
        input_tokens = 100
        output_tokens = 50

    with account_usage() as log:
        _record_usage("sonnet-4-6", StubUsage())
    assert log[0]["input_tokens"] == 100
    assert log[0]["cache_creation_input_tokens"] == 0
    assert log[0]["cache_read_input_tokens"] == 0


def test_nested_account_usage_isolated() -> None:
    # Frames stack: an inner with-block's calls go to the inner log only.
    # Outer log records its own call. Not a usage pattern we depend on in
    # the apply pipeline, but the reentrant-safe behaviour is documented.
    with account_usage() as outer:
        _record_usage(
            "sonnet-4-6",
            MagicMock(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )
        with account_usage() as inner:
            _record_usage(
                "haiku-4-5",
                MagicMock(
                    input_tokens=2,
                    output_tokens=1,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                ),
            )
        assert len(inner) == 1
        assert inner[0]["model"] == "haiku-4-5"
    assert len(outer) == 1
    assert outer[0]["model"] == "sonnet-4-6"
