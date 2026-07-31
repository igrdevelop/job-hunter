"""
bot/state.py — Shared in-memory bot state.

All mutable dicts/sets live here as module-level objects.
Python caches modules, so every importer sees the same instance.
"""

from datetime import datetime, timezone
from hunter.models import Job

# job_id (10-char hash) → Job; cleared on bot restart (acceptable trade-off)
_pending_jobs: dict[str, Job] = {}

# normalized URL → start datetime. Used by /status to show active generation
# progress AND, via try_mark_apply_active/mark_apply_done below, as the
# concurrency guard that stops the SAME vacancy from being generated twice
# at once. Two independent entry points can otherwise race on one URL: the
# auto-hunt loop (hunter.main._run_apply_agent) and a manual paste/Apply-
# button click (hunter.bot.apply_runner._run_apply_agent) both start their
# subprocess before either has written a tracker row — the tracker-based
# dedup in add_applied() only fires at the END of generation, so nothing
# stopped both from running to completion (owner report 2026-07-30: the
# Billennium "Mid Frontend Developer" posting was generated twice, ~$0.64
# combined, one copy silently orphaned since add_applied() rejected its
# late tracker write).
_active_apply_urls: dict[str, datetime] = {}

# chat_ids waiting for a URL/text after bare /force (no inline args)
_force_waiting: set[int] = set()

# Hard cap per apply_agent subprocess (seconds)
_APPLY_AGENT_TIMEOUT: int = 900


def try_mark_apply_active(url: str) -> bool:
    """Atomically claim ``url`` as in-flight. Returns False when a
    generation for the same (normalized) URL is already running — the
    caller must not start a second one.

    No lock needed: asyncio is single-threaded and neither the membership
    check nor the dict write below awaits anything, so no other coroutine
    can interleave between them.
    """
    if not url:
        return True
    from hunter.tracker import normalize_url

    key = normalize_url(url)
    if not key:
        return True
    if key in _active_apply_urls:
        return False
    _active_apply_urls[key] = datetime.now(timezone.utc)
    return True


def mark_apply_done(url: str) -> None:
    """Release the in-flight claim taken by try_mark_apply_active."""
    if not url:
        return
    from hunter.tracker import normalize_url

    key = normalize_url(url)
    if key:
        _active_apply_urls.pop(key, None)
