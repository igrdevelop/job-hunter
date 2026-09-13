"""
hunter/funnel.py — application funnel analytics over tracker.db.

Turns the flat tracker into a funnel so effort can be steered toward what
actually converts:

    tracked → docs generated → sent → responded

both overall and per source. Since docs/MARKET_MEMORY_PLAN.md M3 the source
is STORED on the row (`applications.source`, stamped by every tracker INSERT
from `Job.source` / the `postings_seen` row); the old inference from the URL
via each source's own `matches_url` (registered-domain fallback) is kept only
as the fallback for blank values — pre-M3 rows, which stay blank by owner
decision (no backfill), and writers that had no source in hand.

Public API
----------
    compute_funnel(days=None) -> FunnelReport
    source_for_row(stored, url) -> str
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


def source_for_row(stored: str | None, url: str) -> str:
    """Attribute a tracker ROW to a source: the stored `source` column when
    the writer stamped one (M3), else the URL guess.

    The guess is wrong for exactly the class ROADMAP 4.3 needs to judge — a
    Greenhouse/Lever/Workable link surfaced by justjoin/gmail/a Telegram
    channel is guessed as `ats_aggregator` — so a stored value always wins.
    """
    name = (stored or "").strip()
    return name if name else source_for_url(url)


# ── Row classification ────────────────────────────────────────────────────────


def _is_generated(ats_status: str) -> bool:
    """A CV was generated when the ATS column holds a numeric score (e.g. '85%')."""
    return bool(re.search(r"\d", ats_status or "")) and "%" in (ats_status or "")


def _is_sent(sent: str) -> bool:
    return (sent or "").strip().lower() not in _NON_SENT


def _is_confirmed(confirmation: str) -> bool:
    """An ATS / board acknowledged the application (automated receipt)."""
    return bool((confirmation or "").strip())


def _is_answered(answer: str, outcome_label: str = "") -> bool:
    """A human reply landed (rejection / interview / offer) — the real signal.

    Two sources, either is enough: the legacy free-text `answer` column (only
    ever filled by hand in the Sheet — kept so historical rows still count) and
    the structured `outcome_label` (tracker.OUTCOME_LABELS). `silence` is an
    OBSERVED outcome but deliberately not a reply — counting it here would turn
    "nobody wrote back" into a reply rate.
    """
    from hunter.tracker import OUTCOME_REPLY_LABELS

    if (outcome_label or "").strip() in OUTCOME_REPLY_LABELS:
        return True
    return bool((answer or "").strip())


def _has_outcome(outcome_label: str) -> bool:
    """Any outcome was recorded, `silence` included.

    This is what separates "no replies" from "nobody recorded anything" — the
    distinction a 90-day prod run could not make (399 sent, 0 outcomes).
    """
    from hunter.tracker import OUTCOME_LABELS

    return (outcome_label or "").strip() in OUTCOME_LABELS


# ── Report dataclasses ────────────────────────────────────────────────────────


@dataclass
class FunnelCounts:
    tracked: int = 0
    generated: int = 0
    sent: int = 0
    confirmed: int = 0  # ATS / board automated acknowledgement
    answered: int = 0  # human reply (rejection / interview / offer)
    # Rows with ANY recorded outcome, `silence` included. answered == 0 with
    # outcome_recorded == 0 means "unmeasured", not "no replies".
    outcome_recorded: int = 0

    def add(
        self,
        *,
        generated: bool,
        sent: bool,
        confirmed: bool,
        answered: bool,
        outcome_recorded: bool = False,
    ) -> None:
        self.tracked += 1
        self.generated += int(generated)
        self.sent += int(sent)
        self.confirmed += int(confirmed)
        self.answered += int(answered)
        self.outcome_recorded += int(outcome_recorded)

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
        # get_db() does not migrate — hunter.db.init_db() does, at bot startup.
        # Probe instead of assuming, so /funnel and tools/funnel_sources.py
        # keep working against a database that predates outcome_label (a
        # stale dev fixture, or a tool run before the new image first starts).
        cols = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
        outcome_col = "outcome_label" if "outcome_label" in cols else "'' AS outcome_label"
        # Same probe for `source` (M3): a blank reads as "guess from the URL".
        source_col = "source" if "source" in cols else "'' AS source"
        rows = conn.execute(
            "SELECT date, ats_status, url, sent, confirmation, answer, "  # noqa: S608
            f"{outcome_col}, {source_col} FROM applications"
        ).fetchall()

    for r in rows:
        d = (r["date"] or "").strip()
        # Keep only rows on/after cutoff with a comparable ISO date.
        if cutoff is not None and (not re.match(r"^\d{4}-\d{2}-\d{2}", d) or d < cutoff):
            continue

        generated = _is_generated(r["ats_status"])
        sent = _is_sent(r["sent"])
        confirmed = _is_confirmed(r["confirmation"])
        label = r["outcome_label"]
        answered = _is_answered(r["answer"], label)
        recorded = _has_outcome(label)

        report.overall.add(
            generated=generated,
            sent=sent,
            confirmed=confirmed,
            answered=answered,
            outcome_recorded=recorded,
        )
        src = source_for_row(r["source"], r["url"])
        report.by_source.setdefault(src, FunnelCounts()).add(
            generated=generated,
            sent=sent,
            confirmed=confirmed,
            answered=answered,
            outcome_recorded=recorded,
        )

    return report
