"""Tests for hunter/profile_parse.py.

Step 3a: extract_resume_text (docx/pdf/txt -> plain text, raises
ProfileParseError on anything unreadable).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter.profile_parse import ProfileParseError, extract_resume_text

FIXTURES = Path(__file__).parent / "fixtures" / "resumes"


def test_extract_text_from_txt() -> None:
    text = extract_resume_text(FIXTURES / "jane.txt")
    assert "Jane Doe" in text
    assert "jane.doe@example.com" in text


def test_extract_text_from_md(tmp_path: Path) -> None:
    path = tmp_path / "resume.md"
    path.write_text("# Jane Doe\n\nSenior Frontend Developer\n", encoding="utf-8")
    text = extract_resume_text(path)
    assert "Jane Doe" in text


def test_extract_text_from_docx(tmp_path: Path) -> None:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Jane Doe")
    doc.add_paragraph("Senior Frontend Developer")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Acme Corp"
    table.rows[0].cells[1].text = "Jan 2024 - Present"
    path = tmp_path / "resume.docx"
    doc.save(str(path))

    text = extract_resume_text(path)
    assert "Jane Doe" in text
    assert "Acme Corp" in text
    assert "Jan 2024 - Present" in text


def test_extract_text_unsupported_extension(tmp_path: Path) -> None:
    path = tmp_path / "resume.xyz"
    path.write_text("whatever", encoding="utf-8")
    with pytest.raises(ProfileParseError):
        extract_resume_text(path)


def test_extract_text_unreadable_pdf(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a real pdf")
    with pytest.raises(ProfileParseError):
        extract_resume_text(path)


def test_extract_text_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProfileParseError):
        extract_resume_text(tmp_path / "missing.docx")


def test_extract_text_empty_txt_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ProfileParseError):
        extract_resume_text(path)
