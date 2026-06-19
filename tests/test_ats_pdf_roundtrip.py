"""Unit tests for hunter.ats_pdf_roundtrip.

The pipeline is best-effort: missing PDF, unreadable PDF, empty job text or
empty content should all return None rather than blowing up the apply flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from hunter.ats_pdf_roundtrip import (
    find_en_cv_pdf,
    format_summary,
    run_pdf_roundtrip,
)


def test_find_en_cv_pdf_prefers_cv_en_pattern(tmp_path: Path) -> None:
    (tmp_path / "Cover_Letter_EN.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "Ihar_Petrasheuski_CV_Angular_2026_EN.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "Ihar_Petrasheuski_CV_Angular_2026_PL.pdf").write_bytes(b"%PDF-1.4\n")
    found = find_en_cv_pdf(tmp_path)
    assert found is not None
    assert "CV" in found.name and "EN" in found.name and "cover" not in found.name.lower()


def test_find_en_cv_pdf_returns_none_when_only_cover_letters(tmp_path: Path) -> None:
    (tmp_path / "Cover_Letter_EN.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "Cover_Letter_PL.pdf").write_bytes(b"%PDF-1.4\n")
    assert find_en_cv_pdf(tmp_path) is None


def test_run_pdf_roundtrip_empty_job_text_returns_none(tmp_path: Path) -> None:
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    assert run_pdf_roundtrip(tmp_path, job_text="   ", json_ats_score=90.0) is None


def test_run_pdf_roundtrip_no_pdf_returns_none(tmp_path: Path) -> None:
    assert run_pdf_roundtrip(tmp_path, job_text="we need Angular", json_ats_score=90.0) is None


def test_run_pdf_roundtrip_pdf_extract_empty_returns_none(tmp_path: Path) -> None:
    # Real pypdf will fail to extract text from a stub byte stream.
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf")
    assert run_pdf_roundtrip(tmp_path, job_text="we need Angular", json_ats_score=90.0) is None


def test_run_pdf_roundtrip_returns_score_and_delta(tmp_path: Path) -> None:
    # Bypass the real pypdf extraction so the test stays pure unit-level.
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    pdf_text = (
        "Senior Frontend Engineer Angular TypeScript RxJS NgRx React Vue.js "
        "Jest Cypress Playwright Webpack Vite Docker Kubernetes AWS GraphQL REST"
    )
    job_text = "Looking for Angular + TypeScript engineer with RxJS, NgRx, Jest experience."
    with patch("hunter.ats_pdf_roundtrip.extract_pdf_text", return_value=pdf_text):
        result = run_pdf_roundtrip(tmp_path, job_text=job_text, json_ats_score=88.0)
    assert result is not None
    assert "score" in result
    assert "delta_from_json" in result
    assert result["delta_from_json"] == round(result["score"] - 88.0, 1)
    assert result["pdf_text_chars"] == len(pdf_text)
    assert result["pdf_file"].endswith("_EN.pdf")


def test_run_pdf_roundtrip_no_json_score_skips_delta(tmp_path: Path) -> None:
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    with patch("hunter.ats_pdf_roundtrip.extract_pdf_text", return_value="Angular TypeScript"):
        result = run_pdf_roundtrip(tmp_path, job_text="Angular role", json_ats_score=None)
    assert result is not None
    assert "delta_from_json" not in result


def test_format_summary_includes_delta_and_warn_flag() -> None:
    s = format_summary({"score": 92.0, "delta_from_json": -7.5})
    assert "92.0%" in s
    assert "-7.5" in s
    assert "⚠️" in s


def test_format_summary_no_warn_when_delta_small() -> None:
    s = format_summary({"score": 96.0, "delta_from_json": -2.0})
    assert "⚠️" not in s
    assert "-2.0" in s


def test_format_summary_positive_delta_has_plus_sign() -> None:
    s = format_summary({"score": 95.0, "delta_from_json": 3.0})
    assert "+3.0" in s


def test_format_summary_without_delta() -> None:
    s = format_summary({"score": 90.0})
    assert "90.0%" in s
    assert "vs JSON" not in s


def test_run_pdf_roundtrip_stores_content_payload(tmp_path: Path) -> None:
    """The dict result is JSON-serializable so callers can persist it on content.json."""
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    with patch("hunter.ats_pdf_roundtrip.extract_pdf_text", return_value="Angular TypeScript Jest"):
        result = run_pdf_roundtrip(tmp_path, job_text="need Angular Jest", json_ats_score=80.0)
    assert result is not None
    # Round-trips through JSON unchanged.
    assert json.loads(json.dumps(result)) == result
