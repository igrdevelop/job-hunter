"""Tests for the BaseSource.fetch_text / matches_url contract (Phase 3.1).

These tests cover the new abstract-class methods only — per-source overrides
get their own coverage as they land in Phase 3.2.
"""

from unittest.mock import patch

import pytest

from hunter.models import Job
from hunter.sources.base import BaseSource


class DummySource(BaseSource):
    """Minimal concrete source — only implements search() so we can test defaults."""

    name = "dummy"

    def search(self) -> list[Job]:
        return []


def test_default_matches_url_is_false() -> None:
    src = DummySource()
    assert src.matches_url("https://example.com/job/123") is False
    assert src.matches_url("") is False


def test_default_fetch_text_delegates_to_html_fallback() -> None:
    src = DummySource()
    with patch("hunter.sources.html_fallback.fetch_html", return_value="raw text") as m:
        out = src.fetch_text("https://example.com/job/123")
    assert out == "raw text"
    m.assert_called_once_with("https://example.com/job/123")


def test_default_fetch_text_propagates_errors() -> None:
    src = DummySource()
    with patch("hunter.sources.html_fallback.fetch_html", side_effect=ValueError("too short")):
        with pytest.raises(ValueError, match="too short"):
            src.fetch_text("https://example.com/job/123")


def test_base_source_still_requires_search_override() -> None:
    with pytest.raises(TypeError):
        BaseSource()  # type: ignore[abstract]


def test_all_concrete_sources_inherit_new_methods() -> None:
    """Every registered source must expose matches_url + fetch_text."""
    from hunter.sources import ALL_SOURCES

    for src in ALL_SOURCES:
        assert hasattr(src, "matches_url"), f"{src.name} missing matches_url"
        assert hasattr(src, "fetch_text"), f"{src.name} missing fetch_text"
        # Default impl must accept a string URL without crashing on the type check
        assert callable(src.matches_url)
        assert callable(src.fetch_text)
