"""
hunter/funnel.py — application funnel analytics over tracker.db.

Turns the flat tracker into a funnel so effort can be steered toward what
actually converts:

    tracked → docs generated → sent → responded

both overall and per source. The source isn't stored on the row (the tracker
predates this), so it's inferred from the URL via each source's own
`matches_url`, with a registered-domain fallback.

Public API
----------
    compute_funnel(days=None) -> FunnelReport
    source_for_url(url) -> str
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import urlparse

from hunter.config import TRACKER_DB_PATH
from hunter.db import get_db

# Module-level so tests can point it at an isolated DB (mirrors tracker.DB_PATH).
DB_PATH = TRACKER_DB_PATH

# Sent-column values that do NOT mean "submitted to employer".
_NON_SENT = {"", "—", "–", "-", "expired"}


# ── Source attribution ────────────────────────────────────────────────────────

_SOURCE_CACHE: list[tuple[str, object]] | None = None


def _sources() -> list[tuple[str, object]]:
    """(name, source_instance) pairs, cached. Best-effort import."""
    global _SOURCE_CACHE
    if _SOURCE_CACHE is None:
        try:
            from hunter.sources import ALL_SOURCES

            _SOURCE_CACHE = [(s.name, s) for s in ALL_SOURCES]
        except Exception:
            _SOURCE_CACHE = []
    return _SOURCE_CACHE


def _registered_domain(url: str) -> str:
    """Best-effort registered domain from a URL (e.g. 'jobs.example.co' → 'example.co')."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "?"
    host = host.split("@")[-1].split(":")[0]
    if not host:
        return "?"
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def source_for_url(url: str) -> str:
    """Attribute a tracker URL to a source name.

    Tries each registered source's `matches_url`; falls back to the registered
    domain so even ad-hoc / pasted URLs get a stable bucket.
    """
    if not url:
        return "—"
    for name, src in _sources():
        try:
            if src.matches_url(url):
                return name
        except Exception:
            continue
    return _registered_domain(url)


# ── Row classification ────────────────────────────────────────────────────────


def _is_generated(ats_status: str) -> bool:
    """A CV was generated when the ATS column holds a numeric score (e.g. '85%')."""
    return bool(re.search(r"\d", ats_status or "")) and "%" in (ats_status or "")


def _is_sent(sent: str) -> bool:
    return (sent or "").strip().lower() not in _NON_SENT


def _is_confirmed(confirmation: str) -> bool:
    """An ATS / board acknowledged the application (automated receipt)."""
    return bool((confirmation or "").strip())


def _is_answered(answer: str) -> bool:
    """A human reply landed (rejection / interview / offer) — the real signal."""
    return bool((answer or "").strip())


# ── Report dataclasses ────────────────────────────────────────────────────────


@dataclass
class FunnelCounts:
    tracked: int = 0
    generated: int = 0
    sent: int = 0
    confirmed: int = 0  # ATS / board automated acknowledgement
    answered: int = 0  # human reply (rejection / interview / offer)

    def add(self, *, generated: bool, sent: bool, confirmed: bool, answered: bool) -> None:
        self.tracked += 1
        self.generated += int(generated)
        self.sent += int(sent)
        self.confirmed += int(confirmed)
        self.answered += int(answered)

    @property
    def sent_rate(self) -> float:
        return round(100 * self.sent / self.generated, 1) if self.generated else 0.0

    @property
    def confirm_rate(self) -> float:
        return round(100 * self.confirmed / self.sent, 1) if self.sent else 0.0

    @property
    def answer_rate(self) -> float:
        return round(100 * self.answered / self.sent, 1) if self.sent else 0.0


@dataclass
class FunnelReport:
    days: int | None
    overall: FunnelCounts = field(default_factory=FunnelCounts)
    by_source: dict[str, FunnelCounts] = field(default_factory=dict)

    def top_sources(self, limit: int = 25) -> list[tuple[str, FunnelCounts]]:
        """Sources sorted by sent desc, then generated desc, then name."""
        items = list(self.by_source.items())
        items.sort(key=lambda kv: (-kv[1].sent, -kv[1].generated, kv[0]))
        return items[:limit]


# ── Aggregation ───────────────────────────────────────────────────────────────


def _cutoff(days: int | None) -> str | None:
    if not days:
        return None
    return (date.today() - timedelta(days=days)).isoformat()


def compute_funnel(days: int | None = None) -> FunnelReport:
    """Aggregate tracker.db into a funnel, optionally limited to the last `days`.

    Rows with an empty/unparseable date are included only when no period filter
    is set (so a date window never silently drops undated rows into nowhere).
    """
    report = FunnelReport(days=days)
    cutoff = _cutoff(days)

    with get_db(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, ats_status, url, sent, confirmation, answer FROM applications"
        ).fetchall()

    for r in rows:
        d = (r["date"] or "").strip()
        if cutoff is not None:
            # Keep only rows on/after cutoff with a comparable ISO date.
            if not re.match(r"^\d{4}-\d{2}-\d{2}", d) or d < cutoff:
                continue

        generated = _is_generated(r["ats_status"])
        sent = _is_sent(r["sent"])
        confirmed = _is_confirmed(r["confirmation"])
        answered = _is_answered(r["answer"])

        report.overall.add(generated=generated, sent=sent, confirmed=confirmed, answered=answered)
        src = source_for_url(r["url"])
        report.by_source.setdefault(src, FunnelCounts()).add(
            generated=generated, sent=sent, confirmed=confirmed, answered=answered
        )

    return report
