"""Tests for tools/verdict_noise.py (docs/LLM_COST_REDUCTION_PLAN.md M2)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import verdict_noise  # noqa: E402


def test_summarize_reports_min_max_spread_and_sigma():
    per_folder = {
        "Acme/2026-07-01": [80.0, 84.0, 82.0],
        "Beta/2026-07-02": [90.0, 90.0, 92.0],
    }
    out = verdict_noise.summarize(per_folder)
    assert "min=80.0 max=84.0" in out
    assert "spread=4.0pp" in out
    assert "Judge noise" in out


def test_summarize_skips_folders_with_fewer_than_two_scores():
    per_folder = {"Acme/x": [80.0], "Beta/y": [90.0, 92.0]}
    out = verdict_noise.summarize(per_folder)
    assert "skipped" in out
    assert "Beta/y" in out


def test_summarize_handles_no_usable_data():
    out = verdict_noise.summarize({"Acme/x": [80.0]})
    assert "Not enough usable scores" in out


def test_find_candidate_folders_requires_pdf_and_posting(tmp_path, monkeypatch):
    # Folder with posting but no matching PDF is skipped.
    no_pdf = tmp_path / "Acme"
    no_pdf.mkdir()
    (no_pdf / "job_posting.txt").write_text("job", encoding="utf-8")

    # Folder with both is kept.
    with_pdf = tmp_path / "Beta"
    with_pdf.mkdir()
    (with_pdf / "job_posting.txt").write_text("job", encoding="utf-8")
    (with_pdf / "Resume_EN.pdf").write_bytes(b"%PDF-1.4")

    folders = verdict_noise.find_candidate_folders(tmp_path, limit=10)
    assert with_pdf in folders
    assert no_pdf not in folders


def test_find_candidate_folders_missing_root(tmp_path):
    assert verdict_noise.find_candidate_folders(tmp_path / "nope", limit=5) == []


def test_find_candidate_folders_respects_limit(tmp_path):
    for i in range(3):
        f = tmp_path / f"Co{i}"
        f.mkdir()
        (f / "job_posting.txt").write_text("job", encoding="utf-8")
        (f / "Resume_EN.pdf").write_bytes(b"%PDF-1.4")
    folders = verdict_noise.find_candidate_folders(tmp_path, limit=2)
    assert len(folders) == 2


def test_measure_folder_collects_scores_and_skips_none(tmp_path, monkeypatch):
    folder = tmp_path / "Acme"
    folder.mkdir()
    (folder / "job_posting.txt").write_text("job text", encoding="utf-8")

    calls = []
    responses = iter([{"score": 80.0}, None, {"score": 84.0}])

    def _fake_verdict(folder, job_text):
        calls.append((folder, job_text))
        return next(responses)

    monkeypatch.setattr(
        "hunter.ats_pdf_roundtrip.run_llm_verdict", _fake_verdict
    )
    scores = verdict_noise.measure_folder(folder, k=3)
    assert scores == [80.0, 84.0]
    assert len(calls) == 3
