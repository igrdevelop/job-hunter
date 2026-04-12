"""
generate_docs.py — Job Application Document Generator
Usage: python generate_docs.py <path_to_content.json>

Expects a JSON file with the following schema:
{
  "output_folder": "D:/LearningProject/Claude/Applications/CompanyName_2026-04-06",
  "stack": "Angular",
  "lang": "EN",
  "resume_en": {
    "summary": "...",
    "skills": {
      "frontend": "Angular (2+), TypeScript, ...",
      "tools": "Jest, Git, ...",
      "methodologies": "Agile, ...",
      "languages": "English (Fluent), ..."
    },
    "experience": [
      {
        "title": "Senior Frontend Developer (Angular)",
        "company": "Fairmarkit (via contractor)",
        "period": "Jun 2025 - March 2026",
        "subtitle": "AI-powered Enterprise Procurement Platform | USA (Global)",
        "bullets": ["bullet 1", "bullet 2"],
        "stack_line": "Stack: Angular 19, TypeScript, ..."
      }
    ],
    "education": "Belarusian State Technological University - Bachelor, PE and Systems of Information Processing",
    "courses": "Angular Updates Course, Angular Advanced Course, ..."
  },
  "resume_pl": null,
  "cover_letter_en": "Full cover letter text...",
  "cover_letter_pl": "Pełny tekst listu motywacyjnego...",
  "about_me_en": "3-5 sentence elevator pitch...",
  "about_me_pl": "3-5 zdań elevator pitch..."
}
"""

import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


def set_font(run, name="Calibri", size=11, bold=False, italic=False, color=None):
    run.font.name = name
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color:
        run.font.color.rgb = RGBColor(*color)


def add_horizontal_line(paragraph):
    """Add a thin bottom border to a paragraph (acts as a divider)."""
    pPr = paragraph._p.get_or_add_pPr()
    pBdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "AAAAAA")
    pBdr.append(bottom)
    pPr.append(pBdr)


def set_paragraph_spacing(paragraph, before=0, after=4, line_spacing=1.15):
    from docx.shared import Pt
    from docx.oxml.ns import qn
    pPr = paragraph._p.get_or_add_pPr()
    pSpacing = OxmlElement("w:spacing")
    pSpacing.set(qn("w:before"), str(int(before * 20)))
    pSpacing.set(qn("w:after"), str(int(after * 20)))
    pSpacing.set(qn("w:line"), str(int(line_spacing * 240)))
    pSpacing.set(qn("w:lineRule"), "auto")
    pPr.append(pSpacing)


def add_section_heading(doc, text):
    p = doc.add_paragraph()
    run = p.add_run(text.upper())
    set_font(run, size=11, bold=True)
    add_horizontal_line(p)
    set_paragraph_spacing(p, before=8, after=3)
    return p


def build_resume(doc, data, stack):
    name = "Ihar Petrasheuski"
    subtitle = "also known as Igor Pietraszewski"
    headline = f"Senior Frontend Developer ({stack})"
    contact = "+48 571 525 110 | igrflex@gmail.com | linkedin.com/in/ijerweb | Wrocław, Poland"

    # Name
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(name)
    set_font(run, size=16, bold=True)
    set_paragraph_spacing(p, before=0, after=2)

    # Subtitle (also known as)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(subtitle)
    set_font(run, size=10, italic=True)
    set_paragraph_spacing(p, before=0, after=2)

    # Headline
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(headline)
    set_font(run, size=13, bold=True)
    set_paragraph_spacing(p, before=0, after=2)

    # Contact
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(contact)
    set_font(run, size=10)
    set_paragraph_spacing(p, before=0, after=6)

    # SUMMARY
    add_section_heading(doc, "SUMMARY")
    p = doc.add_paragraph()
    run = p.add_run(data["summary"])
    set_font(run, size=11)
    set_paragraph_spacing(p, before=3, after=6)

    # SKILLS
    add_section_heading(doc, "SKILLS")
    skills = data["skills"]
    skill_lines = [
        ("Frontend", skills.get("frontend", "")),
        ("Tools", skills.get("tools", "")),
        ("Methodologies", skills.get("methodologies", "")),
        ("Languages", skills.get("languages", "")),
    ]
    for label, value in skill_lines:
        if value:
            p = doc.add_paragraph()
            run_label = p.add_run(f"{label}: ")
            set_font(run_label, size=11, bold=True)
            run_value = p.add_run(value)
            set_font(run_value, size=11)
            set_paragraph_spacing(p, before=1, after=1)

    doc.add_paragraph()  # small gap

    # WORK EXPERIENCE
    add_section_heading(doc, "WORK EXPERIENCE")
    for job in data["experience"]:
        # Title | Company — Period
        p = doc.add_paragraph()
        run_title = p.add_run(f"{job['title']} | {job['company']}")
        set_font(run_title, size=11, bold=True)
        # Period — right aligned via tab or just appended
        run_period = p.add_run(f"   {job['period']}")
        set_font(run_period, size=10, italic=True)
        set_paragraph_spacing(p, before=6, after=1)

        # Subtitle / context line
        if job.get("subtitle"):
            p = doc.add_paragraph()
            run = p.add_run(job["subtitle"])
            set_font(run, size=10, italic=True)
            set_paragraph_spacing(p, before=0, after=2)

        # Bullets
        for bullet in job.get("bullets", []):
            p = doc.add_paragraph(style="List Bullet")
            run = p.add_run(bullet)
            set_font(run, size=11)
            set_paragraph_spacing(p, before=0, after=1)

        # Stack line
        if job.get("stack_line"):
            p = doc.add_paragraph()
            run = p.add_run(job["stack_line"])
            set_font(run, size=10, bold=True)
            set_paragraph_spacing(p, before=1, after=3)

    # EDUCATION
    add_section_heading(doc, "EDUCATION")
    p = doc.add_paragraph()
    run = p.add_run(data.get("education", ""))
    set_font(run, size=11)
    set_paragraph_spacing(p, before=3, after=3)

    # ADDITIONAL COURSES
    add_section_heading(doc, "ADDITIONAL COURSES")
    p = doc.add_paragraph()
    run = p.add_run(data.get("courses", ""))
    set_font(run, size=11)
    set_paragraph_spacing(p, before=3, after=3)


def build_cover_letter(doc, text):
    for line in text.split("\n"):
        p = doc.add_paragraph()
        run = p.add_run(line)
        set_font(run, size=11)
        set_paragraph_spacing(p, before=0, after=6)


def set_margins(doc, margin_cm=2.0):
    for section in doc.sections:
        section.top_margin = Cm(margin_cm)
        section.bottom_margin = Cm(margin_cm)
        section.left_margin = Cm(margin_cm)
        section.right_margin = Cm(margin_cm)


def set_author(doc, name="Ihar Petrasheuski"):
    props = doc.core_properties
    props.author = name
    props.last_modified_by = name
    props.title = ""
    props.subject = ""
    props.keywords = ""


def save_docx(doc, path_docx):
    """Save DOCX only. PDFs are converted in bulk at the end."""
    set_author(doc)
    doc.save(path_docx)
    print(f"  [OK] DOCX: {path_docx}")


def convert_all_to_pdf(output_folder):
    """Convert all DOCX files in the folder to PDF in a single LibreOffice call."""
    import subprocess
    soffice = r"C:\Program Files\LibreOffice\program\soffice.exe"
    docx_files = list(Path(output_folder).glob("*.docx"))
    if not docx_files:
        return
    result = subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(output_folder)]
        + [str(f) for f in docx_files],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode == 0:
        for f in docx_files:
            print(f"  [OK] PDF:  {str(f).replace('.docx', '.pdf')}")
    else:
        print(f"  [WARN] PDF conversion failed: {result.stderr.strip()}")


TRACKER_PATH = Path("D:/LearningProject/Claude/tracker.xlsx")
TRACKER_HEADERS = ["Date", "Company", "Job Title", "Stack", "ATS %", "URL", "Folder", "Sent", "Re-application", "To Learn"]


def _create_tracker():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Applications"

    header_fill = PatternFill("solid", fgColor="2B579A")
    header_font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)

    for col, header in enumerate(TRACKER_HEADERS, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # Column widths
    widths = [12, 20, 30, 12, 8, 50, 40, 8, 16, 35]
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width

    ws.row_dimensions[1].height = 20
    ws.freeze_panes = "A2"
    wb.save(TRACKER_PATH)
    return wb


def _tracker_company_name(content: dict) -> str:
    """Company column: use LLM company_name, not folder basename (avoids Upvanta_2026-04-11 from collisions)."""
    cn = (content.get("company_name") or "").strip()
    if cn:
        return cn
    folder_name = Path(content["output_folder"]).name
    # Legacy: strip trailing _2, _3 collision suffix, then strip trailing _YYYY-MM-DD
    s = re.sub(r"_(\d+)$", "", folder_name)
    m = re.search(r"^(.+)_[0-9]{4}-[0-9]{2}-[0-9]{2}$", s)
    return m.group(1) if m else s


def update_tracker(content):
    company = _tracker_company_name(content)
    job_title = content.get("job_title", "")
    stack = content.get("stack", "")
    apply_url = content.get("apply_url", "")
    folder = content["output_folder"]
    to_learn = content.get("to_learn", "")
    ats_score = content.get("ats_score", "")
    ats_display = f"{ats_score}%" if ats_score else ""
    today = date.today().strftime("%Y-%m-%d")

    if TRACKER_PATH.exists():
        wb = openpyxl.load_workbook(TRACKER_PATH)
        ws = wb.active
    else:
        wb = _create_tracker()
        ws = wb.active

    from hunter.tracker import normalize_url, has_successful_entry
    norm_url = normalize_url(apply_url) if apply_url else ""

    # Don't add a duplicate row if docs were already generated for this URL
    if norm_url and has_successful_entry(apply_url):
        print(f"  [tracker] Skipping — successful entry already exists for {apply_url[:60]}")
        wb.close()
        return

    # Check if this URL was already applied to → mark as re-application
    is_reapply = any(
        normalize_url(str(row[5] or "")) == norm_url
        for row in ws.iter_rows(min_row=2, values_only=True)
        if norm_url and row and len(row) > 5 and row[5]
    )

    next_row = ws.max_row + 1
    row_font = Font(name="Calibri", size=11)
    values = [today, company, job_title, stack, ats_display, apply_url, folder, "", "+" if is_reapply else "", to_learn]
    for col, val in enumerate(values, 1):
        cell = ws.cell(row=next_row, column=col, value=val)
        cell.font = row_font
        if col == 6 and val:  # URL — hyperlink
            cell.hyperlink = val
            cell.font = Font(name="Calibri", size=11, color="0563C1", underline="single")
        if col == 5 and ats_score:  # ATS % — color coded
            cell.alignment = Alignment(horizontal="center")
            score = int(ats_score)
            if score >= 80:
                cell.fill = PatternFill("solid", fgColor="C6EFCE")  # green
                cell.font = Font(name="Calibri", size=11, color="276221", bold=True)
            elif score >= 60:
                cell.fill = PatternFill("solid", fgColor="FFEB9C")  # yellow
                cell.font = Font(name="Calibri", size=11, color="9C6500", bold=True)
            else:
                cell.fill = PatternFill("solid", fgColor="FFC7CE")  # red
                cell.font = Font(name="Calibri", size=11, color="9C0006", bold=True)
        if col in (8, 9):  # Sent / Re-application — centered
            cell.alignment = Alignment(horizontal="center")

    # Alternate row color
    if next_row % 2 == 0:
        fill = PatternFill("solid", fgColor="EEF2FA")
        for col in range(1, len(TRACKER_HEADERS) + 1):
            ws.cell(row=next_row, column=col).fill = fill

    # Retry save — tracker.xlsx may be open in Excel
    for attempt in range(1, 6):
        try:
            wb.save(TRACKER_PATH)
            print(f"  [OK] Tracker updated: {TRACKER_PATH}")
            break
        except PermissionError:
            if attempt == 5:
                print(f"  [WARN] Could not save tracker (file locked after 5 attempts). Close Excel and re-run.")
                return
            import time as _time
            print(f"  [tracker] Locked by Excel — retry {attempt}/5 in 3s...")
            _time.sleep(3)


def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_docs.py <content.json> [--full]")
        sys.exit(1)

    json_path = sys.argv[1]
    full_mode = "--full" in sys.argv

    with open(json_path, "r", encoding="utf-8") as f:
        content = json.load(f)

    output_folder = content["output_folder"]
    stack = content["stack"]

    os.makedirs(output_folder, exist_ok=True)
    mode_label = "FULL" if full_mode else "SHORT (PDF-only, EN CV)"
    print(f"\nOutput folder: {output_folder}")
    print(f"Mode: {mode_label}\n")

    # --- Resume EN ---
    if content.get("resume_en"):
        doc = Document()
        set_margins(doc)
        build_resume(doc, content["resume_en"], stack)
        fname = f"Ihar Petrasheuski CV Senior Frontend Developer ({stack}) 2026.docx"
        save_docx(doc, Path(output_folder) / fname)

    # --- Resume PL (full mode only) ---
    if full_mode and content.get("resume_pl"):
        doc = Document()
        set_margins(doc)
        build_resume(doc, content["resume_pl"], stack)
        fname = f"Ihar Petrasheuski CV Senior Frontend Developer ({stack}) 2026 PL.docx"
        save_docx(doc, Path(output_folder) / fname)

    # --- Cover Letter EN ---
    if content.get("cover_letter_en"):
        doc = Document()
        set_margins(doc)
        build_cover_letter(doc, content["cover_letter_en"])
        save_docx(doc, Path(output_folder) / "Cover_Letter_EN.docx")

    # --- Cover Letter PL ---
    if content.get("cover_letter_pl"):
        doc = Document()
        set_margins(doc)
        build_cover_letter(doc, content["cover_letter_pl"])
        save_docx(doc, Path(output_folder) / "Cover_Letter_PL.docx")

    # --- About Me (full mode only) ---
    if full_mode:
        if content.get("about_me_en"):
            p = Path(output_folder) / "About_Me_EN.txt"
            p.write_text(content["about_me_en"], encoding="utf-8")
            print(f"  [OK] TXT:  {p}")

        if content.get("about_me_pl"):
            p = Path(output_folder) / "About_Me_PL.txt"
            p.write_text(content["about_me_pl"], encoding="utf-8")
            print(f"  [OK] TXT:  {p}")

    # --- Convert all DOCX to PDF in one shot ---
    convert_all_to_pdf(output_folder)

    # --- Short mode: remove DOCX intermediates, keep only PDFs ---
    if not full_mode:
        for docx_file in Path(output_folder).glob("*.docx"):
            docx_file.unlink()
            print(f"  [cleanup] Removed intermediate: {docx_file.name}")

    # --- Update tracker.xlsx ---
    update_tracker(content)

    print("\nDone!\n")


if __name__ == "__main__":
    main()
