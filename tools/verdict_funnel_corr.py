"""
tools/verdict_funnel_corr.py — does a higher independent ATS verdict actually
correlate with a better funnel outcome?

docs/LLM_COST_REDUCTION_PLAN.md M2: before anyone considers lowering
ATS_VERDICT_TARGET (currently 95, real verdicts run 72-94 so the refine loop
fires on almost every generation), we need data on whether the verdict score
is predictive of anything downstream. Read-only over tracker.db — no LLM
calls, no writes, no side effects.

Buckets rows with a non-NULL ats_verdict into score bands and reports, per
band: count, sent-rate, confirmed-rate, answered-rate. Reuses
hunter.funnel's own row classification (_is_sent/_is_confirmed/_is_answered)
so the definitions can't drift from the real /funnel command.

Usage:
    python tools/verdict_funnel_corr.py [--days N]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# (low, high] bucket labels — high=None means "and above".
BANDS: list[tuple[str, float, float | None]] = [
    ("<80", 0.0, 80.0),
    ("80-84", 80.0, 85.0),
    ("85-89", 85.0, 90.0),
    ("90-94", 90.0, 95.0),
    ("95+", 95.0, None),
]


@dataclass
class BandCounts:
    count: int = 0
    sent: int = 0
    confirmed: int = 0
    answered: int = 0

    def add(self, *, sent: bool, confirmed: bool, answered: bool) -> None:
        self.count += 1
        self.sent += int(sent)
        self.confirmed += int(confirmed)
        self.answered += int(answered)

    @property
    def sent_rate(self) -> float:
        return round(100 * self.sent / self.count, 1) if self.count else 0.0

    @property
    def confirm_rate(self) -> float:
        return round(100 * self.confirmed / self.sent, 1) if self.sent else 0.0

    @property
    def answer_rate(self) -> float:
        return round(100 * self.answered / self.sent, 1) if self.sent else 0.0


def band_for(score: float) -> str:
    for label, lo, hi in BANDS:
        if score >= lo and (hi is None or score < hi):
            return label
    return "?"


def compute_bands(rows: list[dict], days: int | None = None) -> dict[str, BandCounts]:
    """rows: list of dicts with keys date/ats_verdict/sent/confirmation/answer.

    Pure function over already-fetched rows so it's testable without a real
    tracker.db.
    """
    from hunter.funnel import _cutoff, _is_answered, _is_confirmed, _is_sent

    cutoff = _cutoff(days)
    bands: dict[str, BandCounts] = {label: BandCounts() for label, _, _ in BANDS}

    for r in rows:
        verdict = r.get("ats_verdict")
        if verdict is None:
            continue
        if cutoff is not None:
            d = (r.get("date") or "").strip()
            import re
            if not re.match(r"^\d{4}-\d{2}-\d{2}", d) or d < cutoff:
                continue
        label = band_for(float(verdict))
        bands[label].add(
            sent=_is_sent(r.get("sent") or ""),
            confirmed=_is_confirmed(r.get("confirmation") or ""),
            answered=_is_answered(r.get("answer") or ""),
        )
    return bands


def fetch_rows() -> list[dict]:
    from hunter.config import TRACKER_DB_PATH
    from hunter.db import get_db

    with get_db(TRACKER_DB_PATH) as conn:
        cur = conn.execute(
            "SELECT date, ats_verdict, sent, confirmation, answer "
            "FROM applications WHERE ats_verdict IS NOT NULL"
        )
        return [dict(r) for r in cur.fetchall()]


def format_report(bands: dict[str, BandCounts]) -> str:
    lines = [
        f"{'Band':<8} {'n':>5} {'sent%':>7} {'confirm%':>9} {'answer%':>8}",
        "-" * 42,
    ]
    for label, _, _ in BANDS:
        b = bands[label]
        lines.append(
            f"{label:<8} {b.count:>5} {b.sent_rate:>6.1f}% "
            f"{b.confirm_rate:>8.1f}% {b.answer_rate:>7.1f}%"
        )
    answer_rates = [bands[label].answer_rate for label, _, _ in BANDS if bands[label].sent > 0]
    if len(answer_rates) >= 2:
        spread = max(answer_rates) - min(answer_rates)
        verdict_line = (
            f"\nanswer-rate spread across bands: {spread:.1f}pp "
            f"({'looks predictive' if spread >= 10 else 'looks like noise, not signal'})"
        )
    else:
        verdict_line = "\nnot enough sent rows across bands to compare answer-rate."
    return "\n".join(lines) + verdict_line


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=None, help="limit to the last N days")
    args = parser.parse_args()

    rows = fetch_rows()
    print(f"[verdict_funnel_corr] {len(rows)} row(s) with a recorded ats_verdict")
    bands = compute_bands(rows, days=args.days)
    print()
    print(format_report(bands))


if __name__ == "__main__":
    main()
