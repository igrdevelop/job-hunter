"""Verify that a broken source module does not crash the bot on startup."""
import sys
import types
import pytest


def test_broken_source_does_not_raise(monkeypatch):
    broken = types.ModuleType("hunter.sources._broken_test")
    broken.__spec__ = None  # make importlib skip it cleanly

    def _bad_init():
        raise RuntimeError("simulated import failure")

    # Insert a stub that raises on class instantiation
    class _BrokenSource:
        def __init__(self):
            _bad_init()

    broken._BrokenSource = _BrokenSource
    monkeypatch.setitem(sys.modules, "hunter.sources._broken_test", broken)

    from hunter.sources import _try_add, ALL_SOURCES
    before = len(ALL_SOURCES)
    # Must not raise; broken source is silently skipped
    _try_add(True, "hunter.sources._broken_test", "_BrokenSource")
    assert len(ALL_SOURCES) == before  # nothing was added


def test_disabled_source_is_not_loaded(monkeypatch):
    from hunter.sources import _try_add, ALL_SOURCES
    before = len(ALL_SOURCES)
    _try_add(False, "hunter.sources.nonexistent_module", "SomeSource")
    assert len(ALL_SOURCES) == before  # flag=False → no import attempted
