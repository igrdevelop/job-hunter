"""Tests for tools/verdict_vs_outcome.py (docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M0.1)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _load_module():
    """tools/ isn't a package (no __init__.py) — load it by path, same trick
    tests would use for any standalone script."""
    spec = importlib.util.spec_from_file_location(
        "verdict_vs_outcome", TOOLS_DIR / "verdict_vs_outcome.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["verdict_vs_outcome"] = module
    spec.loader.exec_module(module)
    return module


vvo = _load_module()


# ── source_bucket ────────────────────────────────────────────────────────────


def test_source_bucket_known_names():
    assert vvo.source_bucket("linkedin") == "linkedin"
    assert vvo.source_bucket("justjoin") == "polish_boards"
    assert vvo.source_bucket("ats_aggregator") == "ats_direct"
    assert vvo.source_bucket("remotive") == "other"


# ── build_sample ─────────────────────────────────────────────────────────────


def _raw_row(**kw):
    base = {
        "date": "2026-06-01",
        "url": "https://justjoin.it/offers/x",
        "ats_verdict": 90.0,
        "sent": "2026-06-01",
        "confirmation": "",
        "answer": "",
        "cost_usd": 0.5,
    }
    base.update(kw)
    return base


def test_build_sample_excludes_null_verdict():
    rows = [_raw_row(ats_verdict=None)]
    sample = vvo.build_sample(rows, min_age_days=21, today=date(2026, 7, 1))
    assert sample == []


def test_build_sample_excludes_unsent_rows():
    rows = [_raw_row(sent="")]
    sample = vvo.build_sample(rows, min_age_days=21, today=date(2026, 7, 1))
    assert sample == []


def test_build_sample_excludes_too_recent():
    # sent 5 days before "today" -> reply window hasn't closed at min_age_days=21
    rows = [_raw_row(sent="2026-06-26")]
    sample = vvo.build_sample(rows, min_age_days=21, today=date(2026, 7, 1))
    assert sample == []


def test_build_sample_includes_aged_out_row():
    # sent 30 days before "today" -> past the 21-day censor window
    rows = [_raw_row(sent="2026-06-01", answer="Interview")]
    sample = vvo.build_sample(rows, min_age_days=21, today=date(2026, 7, 1))
    assert len(sample) == 1
    assert sample[0]["verdict"] == 90.0
    assert sample[0]["answered"] == 1
    assert sample[0]["bucket"] == "polish_boards"


def test_build_sample_excludes_cli_outage_unpriced_rows():
    # inside the 2026-08-07..2026-08-10 window AND cost_usd is NULL -> excluded
    rows = [_raw_row(date="2026-08-08", sent="2026-08-08", cost_usd=None)]
    sample = vvo.build_sample(rows, min_age_days=0, today=date(2026, 9, 1))
    assert sample == []


def test_build_sample_keeps_cli_outage_window_row_when_priced():
    # same window, but cost_usd present -> proves API mode, not excluded
    rows = [_raw_row(date="2026-08-08", sent="2026-08-08", cost_usd=0.4)]
    sample = vvo.build_sample(rows, min_age_days=0, today=date(2026, 9, 1))
    assert len(sample) == 1


def test_build_sample_keeps_cli_outage_window_row_outside_dates():
    rows = [_raw_row(date="2026-08-15", sent="2026-08-15", cost_usd=None)]
    sample = vvo.build_sample(rows, min_age_days=0, today=date(2026, 9, 1))
    assert len(sample) == 1


# ── point_biserial ───────────────────────────────────────────────────────────


def test_point_biserial_perfect_positive_relationship():
    values = [10.0, 20.0, 30.0, 40.0]
    binary = [0, 0, 1, 1]
    r = vvo.point_biserial(values, binary)
    assert r is not None
    assert r > 0.85


def test_point_biserial_degenerate_returns_none():
    assert vvo.point_biserial([1.0], [1]) is None  # n < 2
    assert vvo.point_biserial([1.0, 2.0], [1, 1]) is None  # no variance in binary
    assert vvo.point_biserial([5.0, 5.0], [0, 1]) is None  # no variance in values


# ── fisher_exact_p (checked against the classic tea-tasting example) ───────


def test_fisher_exact_p_tea_tasting_reference():
    # Fisher's tea-tasting 2x2 table [[3,1],[1,3]] — textbook two-tailed
    # p-value is 0.485714... (= 34/70), independently reproducible from the
    # hypergeometric formula.
    p = vvo.fisher_exact_p(3, 1, 1, 3)
    assert p == pytest.approx(0.4857142857142857, abs=1e-9)


def test_fisher_exact_p_perfect_association_is_small():
    p = vvo.fisher_exact_p(10, 0, 0, 10)
    assert p < 0.001


def test_fisher_exact_p_degenerate_table_returns_one():
    assert vvo.fisher_exact_p(0, 0, 5, 5) == 1.0  # empty row
    assert vvo.fisher_exact_p(5, 0, 5, 0) == 1.0  # empty column


# ── tercile_fisher ───────────────────────────────────────────────────────────


def test_tercile_fisher_none_when_sample_too_small():
    sample = [{"verdict": 90.0, "answered": 1}] * 3
    assert vvo.tercile_fisher(sample, "answered") is None


def test_tercile_fisher_detects_strong_separation():
    # bottom tercile never answers, top tercile always answers
    sample = []
    for v in range(60, 70):  # 10 low-verdict rows, never answered
        sample.append({"verdict": float(v), "answered": 0})
    for v in range(90, 100):  # 10 high-verdict rows, always answered
        sample.append({"verdict": float(v), "answered": 1})
    result = vvo.tercile_fisher(sample, "answered")
    assert result is not None
    assert result["p"] < 0.01
    assert result["top_positive"] == result["tercile_n"]
    assert result["bottom_positive"] == 0


# ── bootstrap_or_ci: recovers a known synthetic odds ratio ─────────────────


def _synthetic_sample(n: int, true_log_or_per_10pp: float, seed: int) -> list[dict]:
    """Rows generated from a KNOWN logistic model:
    logit(P(answered)) = true_log_or_per_10pp * (verdict - 80) / 10.
    Bucket is assigned independently of the outcome, so the true marginal
    odds ratio for +10pp verdict is exp(true_log_or_per_10pp)."""
    rng = np.random.default_rng(seed)
    verdicts = rng.uniform(60, 100, size=n)
    logits = true_log_or_per_10pp * (verdicts - 80) / 10.0
    probs = 1 / (1 + np.exp(-logits))
    outcomes = rng.binomial(1, probs)
    buckets = rng.choice(["linkedin", "polish_boards", "other"], size=n)
    return [
        {"verdict": float(v), "answered": int(o), "confirmed": int(o), "bucket": str(b)}
        for v, o, b in zip(verdicts, outcomes, buckets, strict=True)
    ]


def test_bootstrap_or_ci_recovers_strong_synthetic_signal():
    true_log_or = 1.0  # true OR for +10pp = e^1.0 ~= 2.718
    sample = _synthetic_sample(n=400, true_log_or_per_10pp=true_log_or, seed=7)

    result = vvo.bootstrap_or_ci(sample, "answered", n_boot=300, seed=42)

    assert result.point is not None
    # Strong, deterministic (seeded) signal at n=400 — the point estimate
    # should land in the right ballpark of the true OR (~2.72), and the CI
    # should exclude 1 (a real effect, not noise).
    assert 1.5 < result.point < 6.0
    assert result.ci_low is not None
    assert result.ci_low > 1.0


def test_bootstrap_or_ci_no_signal_ci_includes_one():
    rng = np.random.default_rng(99)
    n = 200
    verdicts = rng.uniform(60, 100, size=n)
    outcomes = rng.binomial(1, 0.1, size=n)  # answered independent of verdict
    buckets = rng.choice(["linkedin", "other"], size=n)
    sample = [
        {"verdict": float(v), "answered": int(o), "bucket": str(b)}
        for v, o, b in zip(verdicts, outcomes, buckets, strict=True)
    ]

    result = vvo.bootstrap_or_ci(sample, "answered", n_boot=300, seed=42)
    assert result.point is not None
    # No real relationship -> CI should not be confidently above 1.
    if result.ci_low is not None:
        assert result.ci_low < 1.5


def test_bootstrap_or_ci_degenerate_outcome_returns_none_point():
    sample = [{"verdict": 90.0, "answered": 0, "bucket": "linkedin"}] * 10
    result = vvo.bootstrap_or_ci(sample, "answered", n_boot=50, seed=1)
    assert result.point is None


# ── decide_branch ────────────────────────────────────────────────────────────


def test_decide_branch_few_answers():
    assert "not interpretable" in vvo.decide_branch(5, 2.0, 3.0)


def test_decide_branch_significant():
    branch = vvo.decide_branch(20, 1.5, 3.0)
    assert "keep ATS_VERDICT_TARGET=95" in branch


def test_decide_branch_ci_includes_one():
    branch = vvo.decide_branch(20, 0.8, 3.0)
    assert "95->88" in branch


# ── verdict_history section ─────────────────────────────────────────────────


def test_find_content_jsons_excludes_shadow_subfolders(tmp_path):
    apps = tmp_path / "Applications"
    primary = apps / "2026-06-01" / "Acme"
    primary.mkdir(parents=True)
    (primary / "content.json").write_text("{}", encoding="utf-8")

    shadow = primary / "deepseek-v3"
    shadow.mkdir()
    (shadow / "content.json").write_text("{}", encoding="utf-8")

    found = vvo.find_content_jsons(apps)
    assert found == [primary / "content.json"]


def test_find_content_jsons_missing_dir_returns_empty(tmp_path):
    assert vvo.find_content_jsons(tmp_path / "nope") == []


def test_load_verdict_histories_skips_malformed_json(tmp_path):
    good = tmp_path / "a.json"
    good.write_text('{"verdict_history": [{"round": 1, "outcome": "accepted"}]}', encoding="utf-8")
    bad = tmp_path / "b.json"
    bad.write_text("{not json", encoding="utf-8")
    no_history = tmp_path / "c.json"
    no_history.write_text('{"foo": "bar"}', encoding="utf-8")

    histories = vvo.load_verdict_histories([good, bad, no_history])
    assert len(histories) == 1
    assert histories[0][0]["round"] == 1


def test_analyze_histories_shares_and_deltas():
    histories = [
        [
            {
                "round": 1,
                "kind": "honest",
                "score_before": 70,
                "score_after": 80,
                "outcome": "accepted",
            },
            {
                "round": 2,
                "kind": "honest",
                "score_before": 80,
                "score_after": 78,
                "outcome": "rejected",
            },
        ],
        [
            {
                "round": 1,
                "kind": "honest",
                "score_before": 60,
                "score_after": 65,
                "outcome": "accepted",
            },
            {
                "round": 2,
                "kind": "honest",
                "score_before": 65,
                "score_after": 70,
                "outcome": "accepted",
            },
            {
                "round": 3,
                "kind": "honest",
                "score_before": 70,
                "score_after": 72,
                "outcome": "accepted",
            },
            {
                "round": 4,
                "kind": "stretch",
                "score_before": 72,
                "score_after": 90,
                "outcome": "accepted",
            },
        ],
    ]
    stats = vvo.analyze_histories(histories)
    assert stats["runs_with_history"] == 2
    assert stats["accepted_rounds"] == 5  # 1 + 4
    assert stats["accepted_honest"] == 4
    assert stats["accepted_stretch"] == 1
    assert stats["accepted_stretch_share"] == pytest.approx(1 / 5)
    # deltas: 10, 5, 5, 2, 18 -> mean = 40/5 = 8
    assert stats["mean_accepted_delta"] == pytest.approx(8.0)
    # run 1's winning (max accepted) round is 1 (<4); run 2's is 4 (>=4)
    assert stats["winning_round_ge4_share"] == pytest.approx(0.5)


def test_analyze_histories_empty_input():
    stats = vvo.analyze_histories([])
    assert stats["runs_with_history"] == 0
    assert stats["accepted_rounds"] == 0
    assert stats["mean_accepted_delta"] is None
    assert stats["winning_round_ge4_share"] is None


# ── end-to-end over an isolated tracker.db fixture ──────────────────────────


def test_fetch_raw_rows_and_build_report_smoke(tracker_db):
    from hunter.db import get_db

    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, url, url_norm, "
            "ats_status, sent, answer, ats_verdict, cost_usd) VALUES "
            "('id1','2026-06-01','Co','Dev','https://justjoin.it/x','https://justjoin.it/x',"
            "'95%','2026-06-01','Interview',95.0,0.4)"
        )
        conn.execute(
            "INSERT INTO applications (id, date, company, title, url, url_norm, "
            "ats_status, sent, answer, ats_verdict, cost_usd) VALUES "
            "('id2','2026-06-02','Co2','Dev2','https://nofluffjobs.com/x','https://nofluffjobs.com/x',"
            "'70%','2026-06-02','',70.0,0.3)"
        )
        raw_rows = vvo.fetch_raw_rows(conn)
    assert len(raw_rows) == 2

    sample = vvo.build_sample(raw_rows, min_age_days=0, today=date(2026, 9, 1))
    assert len(sample) == 2

    report = vvo.build_report(raw_rows, sample, histories=[], n_boot=20, seed=1)
    assert report["sample_n"] == 2
    assert "answered" in report["targets"]
    # format_report must not raise on a tiny sample
    text = vvo.format_report(report)
    assert "decision branch" in text
