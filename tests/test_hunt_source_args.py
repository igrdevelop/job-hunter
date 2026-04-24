"""Tests for /hunt source name parsing."""

from hunter.telegram_bot import _parse_hunt_source_args


def test_hunt_args_empty_means_all() -> None:
    names, unk = _parse_hunt_source_args([], {"a", "b"})
    assert names is None and unk == []


def test_hunt_args_whitespace_only_means_all() -> None:
    names, unk = _parse_hunt_source_args(["", " , "], {"a", "b"})
    assert names is None and unk == []


def test_hunt_args_single_source() -> None:
    names, unk = _parse_hunt_source_args(["arbeitnow"], {"arbeitnow", "justjoin"})
    assert names == ["arbeitnow"] and unk == []


def test_hunt_args_case_insensitive_normalized() -> None:
    names, unk = _parse_hunt_source_args(["ArbeitNow"], {"arbeitnow"})
    assert names == ["arbeitnow"] and unk == []


def test_hunt_args_comma_separated() -> None:
    names, unk = _parse_hunt_source_args(
        ["arbeitnow,justjoin", "pracuj"],
        {"arbeitnow", "justjoin", "pracuj"},
    )
    assert names == ["arbeitnow", "justjoin", "pracuj"] and unk == []


def test_hunt_args_dedupes() -> None:
    names, unk = _parse_hunt_source_args(
        ["justjoin", "justjoin"],
        {"justjoin"},
    )
    assert names == ["justjoin"] and unk == []


def test_hunt_args_unknown_reported() -> None:
    names, unk = _parse_hunt_source_args(
        ["good", "nope"],
        {"good"},
    )
    assert names == [] and unk == ["nope"]
