"""Plain-text cover letters next to the rendered documents.

A PDF stores positioned lines, not paragraphs, so every viewer copies one
newline per RENDERED line: a cover letter pasted out of the PDF into an
application form arrives hard-wrapped and ragged (owner report 2026-09-19).
Short mode deletes the DOCX, so before this there was no paragraph-preserving
copy source in the folder at all.

The .txt must NOT leak into Telegram — both pipelines attach `*.docx`/`*.pdf`
globs only, which is what keeps About_Me_*.txt out today.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

import generate_docs
from hunter import candidate

EN_LETTER = (
    "Dear Hiring Manager,\n\n"
    "I am writing to apply for the Senior Angular Engineer position. "
    "The focus on reusable artifacts is closely aligned with the architectural "
    "ownership I have exercised throughout my career.\n\n"
    "Sincerely,\nJane Doe"
)
PL_LETTER = "Szanowni Państwo,\n\nPiszę w sprawie stanowiska Senior Angular.\n\nZ poważaniem,\nJane"


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    """Run generate_docs.main() for real, minus LibreOffice and the tracker."""
    yaml = tmp_path / "candidate.yaml"
    yaml.write_text(
        textwrap.dedent(
            """
            identity:
              full_name: "Jane Doe"
              contact: "jane@example.com | Wroclaw"
              cv_filename_prefix: "Jane_Doe_CV"
            """
        ),
        encoding="utf-8",
    )
    candidate._set_path(yaml)

    out = tmp_path / "Applications" / "2026-09-19" / "Acme"
    content = {
        "output_folder": str(out),
        "stack": "Angular",
        "company_name": "Acme",
        "job_title": "Senior Angular Engineer",
        "apply_url": "https://example.com/jobs/1",
        "cover_letter_en": EN_LETTER,
        "cover_letter_pl": PL_LETTER,
    }
    json_path = tmp_path / "content.json"
    json_path.write_text(json.dumps(content), encoding="utf-8")

    # No LibreOffice in the test environment; the DOCX -> PDF step is not
    # what this test is about.
    monkeypatch.setattr(generate_docs, "convert_all_to_pdf", lambda folder: None)
    monkeypatch.setenv("GENERATE_ABOUT_ME_PL", "false")
    monkeypatch.setattr(sys, "argv", ["generate_docs.py", str(json_path), "--no-tracker"])

    generate_docs.main()
    candidate._set_path(None)
    return out


def test_cover_letters_are_written_as_plain_text(rendered):
    en = rendered / "Cover_Letter_EN.txt"
    pl = rendered / "Cover_Letter_PL.txt"
    assert en.is_file() and pl.is_file()
    assert en.read_text(encoding="utf-8") == EN_LETTER + "\n"
    assert pl.read_text(encoding="utf-8") == PL_LETTER + "\n"


def test_paragraphs_survive_intact(rendered):
    """The whole point: no line is wrapped, so a paste keeps its paragraphs."""
    body = (rendered / "Cover_Letter_EN.txt").read_text(encoding="utf-8")
    paragraphs = [p for p in body.split("\n\n") if p.strip()]
    assert len(paragraphs) == 3
    # The long middle paragraph is one unbroken line, not PDF-style fragments.
    assert "\n" not in paragraphs[1]
    assert len(paragraphs[1]) > 150


def test_txt_is_not_a_telegram_attachment(rendered):
    """Both pipelines build `created_files` from these two globs — see
    hunter/apply_api.py and hunter/apply_cli.py."""
    attached = list(rendered.glob("*.docx")) + list(rendered.glob("*.pdf"))
    assert all(p.suffix != ".txt" for p in attached)


def test_txt_survives_short_mode_docx_cleanup(rendered, monkeypatch):
    """Short mode removes the intermediate DOCX once a PDF exists; the .txt
    is not part of that sweep, so the copy source outlives it."""
    for docx in rendered.glob("*.docx"):
        (rendered / (docx.stem + ".pdf")).write_bytes(b"%PDF-1.4 fake")
    # Re-run the cleanup the way main() does it.
    for docx in list(rendered.glob("*.docx")):
        if (rendered / (docx.stem + ".pdf")).is_file():
            docx.unlink()
    assert (rendered / "Cover_Letter_EN.txt").is_file()
    assert (rendered / "Cover_Letter_PL.txt").is_file()


def test_repost_reuse_copies_the_plain_text(tmp_path):
    """A reused package must keep its copy source too."""
    from hunter import repost_gate

    donor = tmp_path / "donor"
    donor.mkdir()
    (donor / "Cover_Letter_EN.txt").write_text(EN_LETTER, encoding="utf-8")
    matched = [
        p.name for pattern in repost_gate._COPY_PATTERNS for p in sorted(donor.glob(pattern))
    ]
    assert "Cover_Letter_EN.txt" in matched
