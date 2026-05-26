"""
bot/state.py — Shared in-memory bot state.

All mutable dicts/sets live here as module-level objects.
Python caches modules, so every importer sees the same instance.
"""

from datetime import datetime
from hunter.models import Job

# job_id (10-char hash) → Job; cleared on bot restart (acceptable trade-off)
_pending_jobs: dict[str, Job] = {}

# URL → start datetime; used by /status to show active generation progress
_active_apply_urls: dict[str, datetime] = {}

# chat_ids waiting for a URL/text after bare /force (no inline args)
_force_waiting: set[int] = set()

# Hard cap per apply_agent subprocess (seconds)
_APPLY_AGENT_TIMEOUT: int = 900
