"""Tests for tools/judge_stats.py (docs/LLM_COST_REDUCTION_PLAN.md M6)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import judge_stats  # noqa: E402


def _report(violations):
    return {"violations": violations}


def _v(
    field="resume_en.summary",
    quote="Fortune 500 clients",
    reason="Fabricated client scale",
    severity="fabrication",
):
    return {"field": field, "quote": quote, "reason": reason, "severity": severity}


def test_normalize_field_collapses_array_indices():
    assert (
        judge_stats.normalize_field("resume_en.experience[2].bullets[1]")
        == "resume_en.experience[].bullets[]"
    )
    assert (
        judge_stats.normalize_field("resume_en.experience[5].bullets[0]")
        == "resume_en.experience[].bullets[]"
    )
    assert judge_stats.normalize_field("cover_letter_en") == "cover_letter_en"


def test_normalize_reason_lowercases_and_collapses_whitespace():
    assert (
        judge_stats.normalize_reason("  Fabricated   Client Scale  ") == "fabricated client scale"
    )


def test_find_judge_reports_missing_root(tmp_path):
    assert judge_stats.find_judge_reports(tmp_path / "nope") == []


def test_find_judge_reports_recursive(tmp_path):
    a = tmp_path / "Acme" / "judge_report.json"
    a.parent.mkdir(parents=True)
    a.write_text(json.dumps(_report([])), encoding="utf-8")
    b = tmp_path / "Beta" / "shadow" / "judge_report.json"
    b.parent.mkdir(parents=True)
    b.write_text(json.dumps(_report([])), encoding="utf-8")
    found = judge_stats.find_judge_reports(tmp_path)
    assert a in found
    assert b in found


def test_load_violations_skips_malformed_file(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_report([_v()])), encoding="utf-8")
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    out = judge_stats.load_violations([good, bad])
    assert len(out) == 1
    assert "skipping unreadable" in capsys.readouterr().out


def test_aggregate_groups_by_severity_field_reason():
    violations = [
        _v(field="resume_en.experience[0].bullets[0]", reason="Fabricated metric"),
        _v(field="resume_en.experience[3].bullets[1]", reason="Fabricated metric"),
        _v(field="cover_letter_en", reason="Exaggerated ownership", severity="exaggeration"),
    ]
    buckets = judge_stats.aggregate(violations)
    assert len(buckets) == 2
    fab_key = ("fabrication", "resume_en.experience[].bullets[]", "fabricated metric")
    assert buckets[fab_key].count == 2
    assert len(buckets[fab_key].examples) <= 3


def test_aggregate_caps_examples_at_three_and_dedupes():
    violations = [_v(quote=f"quote {i % 2}") for i in range(5)]
    buckets = judge_stats.aggregate(violations)
    (stats,) = buckets.values()
    assert stats.count == 5
    assert stats.examples == ["quote 0", "quote 1"]


def test_severity_breakdown_counts():
    violations = [_v(severity="fabrication"), _v(severity="fabrication"), _v(severity="style")]
    breakdown = judge_stats.severity_breakdown(violations)
    assert breakdown["fabrication"] == 2
    assert breakdown["style"] == 1


def test_format_top_empty():
    assert judge_stats.format_top({}, 10) == "(no violations found)"


def test_format_top_includes_examples():
    buckets = judge_stats.aggregate([_v()])
    out = judge_stats.format_top(buckets, 10)
    assert "[fabrication]" in out
    assert "Fortune 500 clients" in out


def test_suggest_rule_candidates_requires_min_count():
    violations = [_v(reason="Rare thing")]
    buckets = judge_stats.aggregate(violations)
    out = judge_stats.suggest_rule_candidates(buckets, min_count=2)
    assert "nothing to suggest" in out


def test_suggest_rule_candidates_frequent_class():
    violations = [_v(reason="Fabricated client scale") for _ in range(3)]
    buckets = judge_stats.aggregate(violations)
    out = judge_stats.suggest_rule_candidates(buckets, min_count=2)
    assert "RED LINE candidate" in out
    assert "fabricated client scale" in out
