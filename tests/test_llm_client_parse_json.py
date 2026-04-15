import pytest

from llm_client import LLMError, _parse_json


def test_parse_json_accepts_direct_json() -> None:
    assert _parse_json('{"score": 9}') == {"score": 9}


def test_parse_json_extracts_first_valid_object_when_multiple_present() -> None:
    raw = 'prefix {"score": 7} middle {"score": 9}'
    assert _parse_json(raw) == {"score": 7}


def test_parse_json_raises_for_invalid_payload() -> None:
    with pytest.raises(LLMError):
        _parse_json("no-json-here")
