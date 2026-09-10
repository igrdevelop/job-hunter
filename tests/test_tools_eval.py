"""Tests for the offline eval harness tools (docs/improvement-2026-09/
08-DATA_EVAL_PLAN.md M2, M3.0):
  - tools/dual_pairs_stats.py
  - tools/eval_golden.py
  - tools/market_m0.py

All synthetic tmp_path corpora — no network, no LLM calls. Deterministic
metrics only (ats_checker.check(run_llm_review=False), lang_guard.scan_content,
pipeline.validate.validate_content, content_qa.run_qa) are exercised for real;
nothing here calls out to an LLM.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import dual_pairs_stats  # noqa: E402
import eval_golden  # noqa: E402
import market_m0  # noqa: E402

# ============================================================================
# tools/dual_pairs_stats.py
# ============================================================================


def test_wilson_ci_hand_checked():
    lo, hi = dual_pairs_stats.wilson_ci(9, 10)
    assert lo == pytest.approx(0.6522548907904218)
    assert hi == pytest.approx(0.9773676701599938)

    lo, hi = dual_pairs_stats.wilson_ci(5, 10)
    assert lo == pytest.approx(0.2692556616014269)
    assert hi == pytest.approx(0.7307443383985731)

    # Edge cases: all-zero and all-n.
    lo, hi = dual_pairs_stats.wilson_ci(0, 10)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(0.21297179881198092)

    lo, hi = dual_pairs_stats.wilson_ci(10, 10)
    assert lo == pytest.approx(0.7870282011880192)
    assert hi == pytest.approx(1.0)

    # n=0 degenerates to (0, 0), never a ZeroDivisionError.
    assert dual_pairs_stats.wilson_ci(0, 0) == (0.0, 0.0)


def test_sign_test_p_hand_checked():
    assert dual_pairs_stats.sign_test_p(5, 5) == pytest.approx(1.0)
    assert dual_pairs_stats.sign_test_p(9, 1) == pytest.approx(0.021484375)
    assert dual_pairs_stats.sign_test_p(10, 0) == pytest.approx(0.001953125)
    assert dual_pairs_stats.sign_test_p(0, 0) == 1.0


def test_bootstrap_mean_ci_reproducible_with_seed():
    diffs = [1.0, 2.0, -1.0, 3.0, 0.5, -0.5, 2.0]
    ci_a = dual_pairs_stats.bootstrap_mean_ci(diffs, resamples=500, seed=7)
    ci_b = dual_pairs_stats.bootstrap_mean_ci(diffs, resamples=500, seed=7)
    assert ci_a == ci_b  # same seed -> identical resampling


def test_bootstrap_mean_ci_constant_diffs_collapses_to_the_constant():
    diffs = [4.0] * 20
    lo, hi = dual_pairs_stats.bootstrap_mean_ci(diffs, resamples=500, seed=1)
    assert lo == pytest.approx(4.0)
    assert hi == pytest.approx(4.0)


def test_bootstrap_mean_ci_empty_diffs():
    assert dual_pairs_stats.bootstrap_mean_ci([]) == (0.0, 0.0)


def test_compute_stats_recovers_a_known_constant_delta():
    # Shadow always scores exactly 5pp above primary -> mean_delta == 5.0,
    # every pair is a "win", share_shadow_ge_primary == 1.0.
    pairs = [(70.0 + i, 75.0 + i) for i in range(20)]
    stats = dual_pairs_stats.compute_stats(pairs, noise_sigma=1.0, resamples=500, seed=3)
    assert stats["n"] == 20
    assert stats["wins"] == 20
    assert stats["losses"] == 0
    assert stats["ties"] == 0
    assert stats["mean_delta"] == pytest.approx(5.0)
    assert stats["share_shadow_ge_primary"] == pytest.approx(1.0)
    lo, hi = stats["mean_delta_ci95"]
    assert lo == pytest.approx(5.0)
    assert hi == pytest.approx(5.0)
    # Delta of 5pp is well outside 2*sigma=2pp -> not "within noise".
    assert stats["share_within_noise"] == pytest.approx(0.0)


def test_compute_stats_ties_and_losses():
    pairs = [(80.0, 80.0), (80.0, 70.0), (80.0, 90.0)]
    stats = dual_pairs_stats.compute_stats(pairs, noise_sigma=5.0, resamples=200, seed=1)
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["ties"] == 1
    assert stats["share_shadow_ge_primary"] == pytest.approx(2 / 3)


def test_compute_stats_empty():
    assert dual_pairs_stats.compute_stats([], noise_sigma=5.0) == {"n": 0}


def test_score_from_content_prefers_verdict_over_deterministic():
    content = {"ats_verdict": {"score": 91}, "ats_check_pdf": {"score": 50}}
    score, source = dual_pairs_stats._score_from_content(content, allow_deterministic=True)
    assert score == 91.0
    assert source == "verdict"


def test_score_from_content_deterministic_fallback_requires_flag():
    content = {"ats_check_pdf": {"score": 77}}
    assert dual_pairs_stats._score_from_content(content, allow_deterministic=False) == (None, "")
    score, source = dual_pairs_stats._score_from_content(content, allow_deterministic=True)
    assert score == 77.0
    assert source == "deterministic:ats_check_pdf"


def test_score_from_content_no_score_anywhere():
    assert dual_pairs_stats._score_from_content({}, allow_deterministic=True) == (None, "")


def test_cost_from_content():
    assert dual_pairs_stats._cost_from_content({"cost": {"total_usd": 0.42}}) == 0.42
    assert dual_pairs_stats._cost_from_content({"cost": {"total_usd": None}}) is None
    assert dual_pairs_stats._cost_from_content({}) is None


def test_find_pairs_and_load_pair_end_to_end(tmp_path):
    primary = tmp_path / "2026-01-01" / "Acme"
    shadow = primary / "deepseek-v3"
    shadow.mkdir(parents=True)
    (primary / "content.json").write_text(
        json.dumps({"ats_verdict": {"score": 80}, "cost": {"total_usd": 0.5}}), encoding="utf-8"
    )
    (shadow / "content.json").write_text(
        json.dumps({"ats_verdict": {"score": 88}, "cost": {"total_usd": 0.1}}), encoding="utf-8"
    )

    triples = dual_pairs_stats.find_pairs(tmp_path)
    assert len(triples) == 1
    p_folder, s_folder, s_name = triples[0]
    assert p_folder == primary
    assert s_folder == shadow
    assert s_name == "deepseek-v3"

    rec = dual_pairs_stats.load_pair(p_folder, s_folder, s_name, allow_deterministic=False)
    assert rec is not None
    assert rec.primary_score == 80.0
    assert rec.shadow_score == 88.0
    assert rec.primary_cost == 0.5
    assert rec.shadow_cost == 0.1


def test_find_pairs_ignores_folders_without_a_shadow(tmp_path):
    solo = tmp_path / "2026-01-01" / "Beta"
    solo.mkdir(parents=True)
    (solo / "content.json").write_text(json.dumps({"ats_verdict": {"score": 80}}), encoding="utf-8")
    assert dual_pairs_stats.find_pairs(tmp_path) == []


def test_load_pair_returns_none_without_allow_deterministic(tmp_path):
    primary = tmp_path / "Acme"
    shadow = primary / "deepseek-v3"
    shadow.mkdir(parents=True)
    (primary / "content.json").write_text(
        json.dumps({"ats_check": {"score": 80}}), encoding="utf-8"
    )
    (shadow / "content.json").write_text(json.dumps({"ats_check": {"score": 85}}), encoding="utf-8")
    assert (
        dual_pairs_stats.load_pair(primary, shadow, "deepseek-v3", allow_deterministic=False)
        is None
    )
    rec = dual_pairs_stats.load_pair(primary, shadow, "deepseek-v3", allow_deterministic=True)
    assert rec is not None
    assert rec.primary_source == "deterministic:ats_check"


# ============================================================================
# tools/eval_golden.py
# ============================================================================


def test_stratified_sample_returns_everything_when_n_covers_all():
    entries = [{"source": "s", "lang": "EN", "track": "angular"} for _ in range(3)]
    assert eval_golden.stratified_sample(entries, 10) == entries


def test_stratified_sample_deterministic_with_seed():
    entries = [
        {"source": src, "lang": lang, "track": "angular", "relative_path": f"{src}-{lang}-{i}"}
        for src in ("a", "b", "c")
        for lang in ("EN", "PL")
        for i in range(5)
    ]
    sample_a = eval_golden.stratified_sample(entries, 6, seed=1)
    sample_b = eval_golden.stratified_sample(entries, 6, seed=1)
    assert sample_a == sample_b
    assert len(sample_a) == 6


def test_stratified_sample_spans_multiple_strata():
    entries = [
        {"source": src, "lang": "EN", "track": "angular", "relative_path": f"{src}-{i}"}
        for src in ("a", "b", "c")
        for i in range(10)
    ]
    sample = eval_golden.stratified_sample(entries, 6, seed=1)
    strata_hit = {e["source"] for e in sample}
    assert strata_hit == {"a", "b", "c"}  # round-robin visits every stratum


def test_sha256_text_matches_hashlib():
    import hashlib

    assert eval_golden.sha256_text("hello") == hashlib.sha256(b"hello").hexdigest()


def test_build_writes_hashes_not_texts(tmp_path):
    root = tmp_path / "Applications"
    folder1 = root / "2026-01-01" / "CompanyA"
    folder2 = root / "2026-01-02" / "CompanyB"
    folder1.mkdir(parents=True)
    folder2.mkdir(parents=True)

    unique_posting_text_1 = (
        "UNIQUE POSTING TEXT ONE — must never leak into the golden set. Angular role."
    )
    unique_posting_text_2 = (
        "UNIQUE POSTING TEXT TWO — also must never leak. React role in Warsaw remote."
    )
    (folder1 / "job_posting.txt").write_text(unique_posting_text_1, encoding="utf-8")
    (folder2 / "job_posting.txt").write_text(unique_posting_text_2, encoding="utf-8")
    (folder1 / "content.json").write_text(
        json.dumps({"apply_url": "https://boards.greenhouse.io/acme/jobs/1", "primary_lang": "EN"}),
        encoding="utf-8",
    )
    (folder2 / "content.json").write_text(
        json.dumps({"apply_url": "https://example.com/jobs/2", "primary_lang": "EN"}),
        encoding="utf-8",
    )

    sample = eval_golden.build_golden_set(root, n=10, seed=1)
    assert len(sample) == 2

    out_path = tmp_path / "golden_set.json"
    eval_golden.write_golden_set(sample, out_path)
    raw = out_path.read_text(encoding="utf-8")

    assert "UNIQUE POSTING TEXT" not in raw
    for entry in json.loads(raw):
        assert set(entry.keys()) == {"relative_path", "sha256", "source", "lang", "track"}
        assert len(entry["sha256"]) == 64  # sha256 hex digest length
    # sha256 in the golden set matches the actual posting text.
    by_path = {e["relative_path"]: e for e in json.loads(raw)}
    assert by_path["2026-01-01/CompanyA"]["sha256"] == eval_golden.sha256_text(
        unique_posting_text_1
    )


def _make_content(*, drop_key: str | None = None) -> dict:
    resume_en = {
        "summary": "Senior Frontend Developer with Angular experience.",
        "skills": {"frontend": "Angular, TypeScript, RxJS"},
        "experience": [
            {
                "company": f"Company{i}",
                "title": "Senior Developer",
                "period": "2020-2024",
                "bullets": ["Built dashboards using Angular and TypeScript."],
            }
            for i in range(7)
        ],
        "education": "BSc Computer Science, Test University",
    }
    content = {
        "company_name": "Acme",
        "stack": "Angular",
        "lang": "EN",
        "job_title": "Senior Frontend Developer",
        "resume_en": resume_en,
        "cover_letter_en": "Dear Hiring Manager, I am excited to apply for this role.",
        "cover_letter_pl": "Szanowni Panstwo, chcialbym aplikowac na to stanowisko.",
        "about_me_en": "I am a senior frontend developer with Angular expertise.",
        "about_me_pl": "Jestem starszym programista frontend ze znajomoscia Angulara.",
        "apply_url": "https://example.com/jobs/1",
        "primary_lang": "EN",
    }
    if drop_key:
        content.pop(drop_key, None)
    return content


_JOB_TEXT = (
    "We are looking for a Senior Frontend Developer with strong Angular, "
    "TypeScript and RxJS experience. Requirements: Angular, TypeScript, "
    "Docker, Git, Agile."
)


def _write_golden_fixture(tmp_path: Path, *, candidate_drop_key: str | None = None):
    root = tmp_path / "root"
    baseline_dir = tmp_path / "baseline"
    candidate_dir = tmp_path / "candidate"
    rel = "2026-01-01/Acme"
    (root / rel).mkdir(parents=True)
    (root / rel / "job_posting.txt").write_text(_JOB_TEXT, encoding="utf-8")
    (baseline_dir / rel).mkdir(parents=True)
    (candidate_dir / rel).mkdir(parents=True)
    (baseline_dir / rel / "content.json").write_text(json.dumps(_make_content()), encoding="utf-8")
    (candidate_dir / rel / "content.json").write_text(
        json.dumps(_make_content(drop_key=candidate_drop_key)), encoding="utf-8"
    )
    golden = [
        {
            "relative_path": rel,
            "sha256": eval_golden.sha256_text(_JOB_TEXT),
            "source": "greenhouse",
            "lang": "EN",
            "track": "angular",
        }
    ]
    return golden, root, baseline_dir, candidate_dir


def test_score_gate_accepts_identical_candidate(tmp_path):
    golden, root, baseline_dir, candidate_dir = _write_golden_fixture(tmp_path)
    results, skipped = eval_golden.score_folders(golden, root, baseline_dir, candidate_dir)
    assert skipped == []
    assert len(results) == 1

    gate = eval_golden.compute_gate(results, resamples=200, seed=1)
    assert gate["mean_det_delta"] == pytest.approx(0.0)
    assert gate["validation_errors_baseline"] == 0
    assert gate["validation_errors_candidate"] == 0
    assert gate["gate"]["det_score_ci_ok"] is True
    assert gate["gate"]["lang_hits_not_increased"] is True
    assert gate["gate"]["validation_errors_zero"] is True
    assert gate["accepted"] is True


def test_score_gate_rejects_candidate_with_validation_error(tmp_path):
    golden, root, baseline_dir, candidate_dir = _write_golden_fixture(
        tmp_path, candidate_drop_key="cover_letter_en"
    )
    results, skipped = eval_golden.score_folders(golden, root, baseline_dir, candidate_dir)
    assert skipped == []
    assert len(results) == 1

    gate = eval_golden.compute_gate(results, resamples=200, seed=1)
    assert gate["validation_errors_candidate"] >= 1
    assert gate["gate"]["validation_errors_zero"] is False
    assert gate["accepted"] is False


def test_score_folders_flags_sha256_mismatch(tmp_path):
    golden, root, baseline_dir, candidate_dir = _write_golden_fixture(tmp_path)
    golden[0]["sha256"] = "0" * 64  # deliberately wrong
    results, skipped = eval_golden.score_folders(golden, root, baseline_dir, candidate_dir)
    assert results == []
    assert any("sha256 mismatch" in s for s in skipped)


def test_score_folders_flags_missing_content_json(tmp_path):
    golden, root, baseline_dir, candidate_dir = _write_golden_fixture(tmp_path)
    (candidate_dir / golden[0]["relative_path"] / "content.json").unlink()
    results, skipped = eval_golden.score_folders(golden, root, baseline_dir, candidate_dir)
    assert results == []
    assert any("missing in --candidate" in s for s in skipped)


def test_compute_gate_empty_results():
    gate = eval_golden.compute_gate([])
    assert gate == {"n": 0, "accepted": False, "reason": "no scored folders"}


def test_estimate_paid_cost():
    assert eval_golden.estimate_paid_cost(10, judge=False, verdict=False) == 0.0
    assert eval_golden.estimate_paid_cost(10, judge=True, verdict=False) == pytest.approx(
        10 * 2 * eval_golden._EST_JUDGE_COST_USD
    )


# ============================================================================
# tools/market_m0.py
# ============================================================================


def test_role_family_from_title():
    assert market_m0.role_family_from_title("Senior Angular Developer") == "angular"
    assert market_m0.role_family_from_title("React Frontend Engineer") == "react"
    assert market_m0.role_family_from_title("Full Stack Engineer") == "fullstack"
    assert market_m0.role_family_from_title("Frontend Developer") == "frontend-generic"
    assert market_m0.role_family_from_title("Backend Java Developer") == "other"
    assert market_m0.role_family_from_title("") == "other"


def test_classify_region():
    city_sets = {
        "home": frozenset({"wroclaw", "wrocław"}),
        "poland_other": frozenset({"warsaw", "krakow"}),
        "abroad": frozenset({"berlin"}),
        "remote": frozenset({"remote", "anywhere"}),
    }
    assert market_m0.classify_region("Office based in Wroclaw", city_sets) == "home"
    assert market_m0.classify_region("Hybrid role, Warsaw office", city_sets) == "poland-other"
    assert market_m0.classify_region("Hybrid role, Berlin office", city_sets) == "abroad"
    assert market_m0.classify_region("Fully remote position", city_sets) == "remote"
    assert market_m0.classify_region("On-site only, Tokyo", city_sets) == "unknown"


def test_jaccard_index():
    assert market_m0.jaccard_index({1, 2, 3}, {2, 3, 4}) == pytest.approx(0.5)
    assert market_m0.jaccard_index(set(), set()) == 1.0
    assert market_m0.jaccard_index({1}, {1}) == 1.0
    assert market_m0.jaccard_index({1}, {2}) == 0.0


def test_spearman_corr_identical_and_reversed():
    x = [1.0, 2.0, 3.0, 4.0]
    assert market_m0.spearman_corr(x, x) == pytest.approx(1.0)
    assert market_m0.spearman_corr(x, list(reversed(x))) == pytest.approx(-1.0)


def test_spearman_corr_constant_series():
    # Zero variance on one side with unequal series -> defined as 0.0, not NaN/crash.
    assert market_m0.spearman_corr([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0
    assert market_m0.spearman_corr([1.0, 1.0], [5.0, 5.0]) == 1.0
    assert market_m0.spearman_corr([1.0], [1.0]) == 1.0


def test_top_n_terms_sorted_with_alpha_tiebreak():
    shares = {"c": 0.9, "b": 0.5, "a": 0.5, "d": 0.1}
    assert market_m0.top_n_terms(shares, 3) == ["c", "a", "b"]


def test_held_out_coverage():
    covs = market_m0.held_out_coverage(["Angular Docker role"], ["angular", "docker"])
    assert covs == [1.0]
    covs = market_m0.held_out_coverage(["Angular Docker role"], ["react"])
    assert covs == [0.0]
    # A posting with no keyword-extractor terms contributes nothing.
    assert market_m0.held_out_coverage(["nothing recognizable here"], ["angular"]) == []


def test_compute_term_shares_exact_from_planted_frequencies():
    texts = []
    for i in range(40):
        parts = ["Angular framework role requiring strong TypeScript skills."]
        if i < 20:
            parts.append("Docker container tooling experience is required.")
        if i < 30:
            parts.append("React familiarity is considered a plus for this role.")
        parts.append(f"Filler padding sentence number {i} to add corpus diversity.")
        texts.append(" ".join(parts))

    shares = market_m0.compute_term_shares(texts)
    assert shares["angular"] == pytest.approx(1.0)  # 40/40
    assert shares["docker"] == pytest.approx(0.5)  # 20/40
    assert shares["react"] == pytest.approx(0.75)  # 30/40


def test_dedup_postings_collapses_exact_duplicate(tmp_path):
    e1 = market_m0.PostingEntry(path=tmp_path / "a", day="2026-01-01", title="X", text="same text")
    e2 = market_m0.PostingEntry(path=tmp_path / "b", day="2026-01-02", title="X", text="same text")
    e3 = market_m0.PostingEntry(
        path=tmp_path / "c", day="2026-01-03", title="X", text="different text"
    )
    out = market_m0.dedup_postings([e1, e2, e3])
    assert len(out) == 2
    assert out[0] is e1  # earliest chronological kept


def test_dedup_postings_collapses_near_duplicate(tmp_path):
    shared = "Senior Angular Developer role with TypeScript and RxJS. " * 40
    e1 = market_m0.PostingEntry(
        path=tmp_path / "a", day="2026-01-01", title="X", text=shared + "alpha"
    )
    e2 = market_m0.PostingEntry(
        path=tmp_path / "b", day="2026-01-02", title="X", text=shared + "beta"
    )
    e3 = market_m0.PostingEntry(
        path=tmp_path / "c",
        day="2026-01-03",
        title="X",
        text="Completely unrelated backend Java Kafka microservices posting " * 10,
    )
    out = market_m0.dedup_postings([e1, e2, e3], cosine_threshold=0.5)
    assert len(out) == 2  # e1/e2 collapsed, e3 kept
    assert out[0] is e1
    assert out[1] is e3


def test_analyze_cell_identical_halves_are_fully_stable():
    text = (
        "Senior Angular Developer role. Requirements: Angular, TypeScript, RxJS, Docker, Git. "
        "Fully remote position."
    )
    entries = [
        market_m0.PostingEntry(
            path=Path(f"/tmp/e{i}"), day=f"2026-01-{i + 1:02d}", title="Angular Dev", text=text
        )
        for i in range(30)
    ]
    result = market_m0.analyze_cell(entries)
    assert result["n"] == 30
    assert result["jaccard"] == pytest.approx(1.0)
    assert result["spearman"] == pytest.approx(1.0)
    # Held-out coverage is a softer metric (it's computed against the OTHER
    # half's top-30, which — for a short repeated sentence — can include
    # enough tied-alphabetically TF-IDF n-grams to nudge one canonical
    # keyword term just outside the top 30); identical halves still means
    # it should be high, not necessarily exactly 1.0.
    assert result["held_out_coverage"] >= 0.5
    assert result["stable"] is True


def test_load_postings_excludes_shadow_subfolders(tmp_path):
    from hunter.llm_profiles import PROFILES

    shadow_name = next(iter(PROFILES))
    primary = tmp_path / "2026-01-01" / "Acme"
    shadow = primary / shadow_name
    shadow.mkdir(parents=True)
    long_text = "Angular developer wanted. " * 20
    (primary / "job_posting.txt").write_text(long_text, encoding="utf-8")
    (shadow / "job_posting.txt").write_text(long_text, encoding="utf-8")

    entries = market_m0.load_postings(tmp_path)
    assert len(entries) == 1
    assert entries[0].path == primary
