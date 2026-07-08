"""Tests for tools/verdict_funnel_corr.py (docs/LLM_COST_REDUCTION_PLAN.md M2)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import verdict_funnel_corr as vfc  # noqa: E402


def _row(verdict, sent="", confirmation="", answer="", date="2026-07-01"):
    return {
        "date": date,
        "ats_verdict": verdict,
        "sent": sent,
        "confirmation": confirmation,
        "answer": answer,
    }


def test_band_for_boundaries():
    assert vfc.band_for(79.9) == "<80"
    assert vfc.band_for(80.0) == "80-84"
    assert vfc.band_for(84.9) == "80-84"
    assert vfc.band_for(89.9) == "85-89"
    assert vfc.band_for(94.9) == "90-94"
    assert vfc.band_for(95.0) == "95+"
    assert vfc.band_for(100.0) == "95+"


def test_compute_bands_skips_null_verdict():
    rows = [{"date": "2026-07-01", "ats_verdict": None, "sent": "2026-07-01"}]
    bands = vfc.compute_bands(rows)
    assert all(b.count == 0 for b in bands.values())


def test_compute_bands_counts_sent_confirmed_answered():
    rows = [
        _row(72, sent="2026-07-01", confirmation="", answer=""),
        _row(78, sent="2026-07-01", confirmation="2026-07-02", answer="rejected"),
        _row(91, sent="", confirmation="", answer=""),
        _row(97, sent="2026-07-01", confirmation="", answer="interview"),
    ]
    bands = vfc.compute_bands(rows)
    assert bands["<80"].count == 2
    assert bands["<80"].sent == 2
    assert bands["<80"].confirmed == 1
    assert bands["<80"].answered == 1
    assert bands["90-94"].count == 1
    assert bands["90-94"].sent == 0
    assert bands["95+"].count == 1
    assert bands["95+"].sent == 1
    assert bands["95+"].answered == 1


def test_compute_bands_day_window_excludes_old_rows():
    rows = [
        _row(90, sent="2020-01-01", date="2020-01-01"),
    ]
    bands = vfc.compute_bands(rows, days=30)
    assert bands["90-94"].count == 0


def test_format_report_reports_spread():
    rows = [
        _row(72, sent="s", answer=""),
        _row(97, sent="s", answer="a"),
    ]
    bands = vfc.compute_bands(rows)
    report = vfc.format_report(bands)
    assert "answer-rate spread" in report


def test_format_report_handles_no_data():
    bands = vfc.compute_bands([])
    report = vfc.format_report(bands)
    assert "not enough sent rows" in report
