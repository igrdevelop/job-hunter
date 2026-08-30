"""Tests for hunter/profile_parse.py.

Step 3a: extract_resume_text (docx/pdf/txt -> plain text, raises
ProfileParseError on anything unreadable).
Step 3b: parse_resume_text's no-LLM fallback branch — the whole text lands
in leftovers, plus a deterministic contact pre-fill; the parse itself never
raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter.profile_parse import ProfileParseError, extract_resume_text, parse_resume_text
from hunter.profile_schema import from_dict, to_dict

FIXTURES = Path(__file__).parent / "fixtures" / "resumes"
JANE_TEXT = (FIXTURES / "jane.txt").read_text(encoding="utf-8")


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


# ── parse_resume_text — no-LLM fallback (step 3b) ────────────────────────────


def test_parse_without_llm_puts_whole_text_in_leftovers() -> None:
    profile = parse_resume_text(JANE_TEXT)
    assert len(profile.leftovers) == 1
    assert profile.leftovers[0].text == JANE_TEXT.strip()
    assert profile.core.roles == []
    assert profile.core.skills == []


def test_parse_without_llm_fills_contact_from_text() -> None:
    profile = parse_resume_text(JANE_TEXT)
    assert "jane.doe@example.com" in profile.core.identity.contact
    assert "+1 555 000 1234" in profile.core.identity.contact
    # The candidate's own name is never guessed from free text.
    assert profile.core.identity.full_name == ""


def test_parse_without_llm_stamps_source_upload_id() -> None:
    profile = parse_resume_text(JANE_TEXT, source_upload_id="upload-1")
    assert profile.leftovers[0].source_upload_id == "upload-1"


def test_parse_without_llm_empty_text_is_empty_profile() -> None:
    profile = parse_resume_text("   ")
    assert profile.leftovers == []
    assert profile.core.identity.contact == ""


def test_parse_without_llm_round_trips() -> None:
    profile = parse_resume_text(JANE_TEXT)
    rebuilt = from_dict(to_dict(profile))
    assert rebuilt.core.identity.contact == profile.core.identity.contact
    assert rebuilt.leftovers[0].text == profile.leftovers[0].text
