"""
hunter/content_qa.py — Post-generation sanity check for content.json.

Runs after sanitize_content, before generate_docs.
Catches issues that the LLM was supposed to follow but didn't:
  1. Role count in resume_en (must be 7)
  2. Polish diacritics / words in resume_en summary and bullets
  3. cover_letter_en written in wrong language (must be EN)
  4. Education stored as stringified Python dict (hallucinated)
  5. Duplicate Angular in skills frontend field
  6. Role titles deviate from profile (checked against known profile titles)
  7. Hallucinated education (wrong school / degree)

Returns a QAReport dataclass with a pass/fail per check and a human-readable summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Polish-contamination detection delegates to hunter.lang_guard (see _has_polish),
# the SAME allowlist-aware detector the apply enforce-gate uses. Sharing it keeps QA
# from warning about something the gate considers clean — notably Polish place names
# (Wrocław, Kraków) that legitimately appear in an English CV for a Poland-based
# candidate. A blunt diacritic regex here used to false-positive on the candidate's
# own city in every cover letter.
# ---------------------------------------------------------------------------
_EN_SENTENCE_RE = re.compile(
    r"\b(I am writing|I would like|I have been|As a Senior|I look forward"
    r"|I bring|I have worked|In my previous|Dear Hiring|With over)\b",
    re.IGNORECASE,
)

_EXPECTED_ROLE_COUNT = 7

# Known canonical profile titles (lowercase normalised)
_PROFILE_TITLES_NORM = {
    "frontend developer (angular, part-time contract)",  # Alten Poland
    "senior frontend developer (angular)",  # Fairmarkit, Venture Labs, SII
    "senior frontend developer",  # Altoros
    "frontend developer (angular)",  # SolbegSoft
    "frontend developer",  # Staronka
}

# Known real company names (lowercase)
_REAL_COMPANIES = {
    "alten poland",
    "fairmarkit",
    "venture labs",
    "sii",
    "altoros",
    "solbegsoft",
    "staronka",
}


def _norm_title(t: str) -> str:
    """Lowercase + strip parentheticals for loose comparison."""
    return re.sub(r"\s*\([^)]*\)", "", t or "").lower().strip()


# ---------------------------------------------------------------------------
# QAReport
# ---------------------------------------------------------------------------


@dataclass
class QACheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class QAReport:
    checks: list[QACheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[QACheck]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> str:
        lines = []
        for c in self.checks:
            icon = "✅" if c.passed else "❌"
            line = f"{icon} {c.name}"
            if not c.passed and c.detail:
                line += f": {c.detail}"
            lines.append(line)
        return "\n".join(lines)

    def telegram_summary(self, url: str) -> str:
        if self.passed:
            return f"✅ <b>QA: all checks passed</b>\n🔗 {url}"
        fails = self.failed_checks
        fail_lines = "\n".join(f"• <b>{c.name}</b>: {c.detail[:120]}" for c in fails)
        return f"⚠️ <b>QA: {len(fails)} check(s) failed</b>\n🔗 {url}\n\n{fail_lines}"


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_role_count(resume_en: dict[str, Any]) -> QACheck:
    exp = resume_en.get("experience") or []
    count = len(exp)
    ok = count >= _EXPECTED_ROLE_COUNT
    return QACheck(
        name=f"Role count ({count}/{_EXPECTED_ROLE_COUNT})",
        passed=ok,
        detail="" if ok else f"Only {count} roles — missing {_EXPECTED_ROLE_COUNT - count}",
    )


def _has_polish(text: str) -> str | None:
    """Return the first Polish-contamination fragment in `text`, or None.

    Delegates to ``hunter.lang_guard.polish_fragments`` (strong signals only) — the
    same detector the apply enforce-gate uses to decide whether to block delivery.
    It allowlists tech terms AND Polish place names, so the candidate's own city
    ("Wrocław") or a Polish office location in an otherwise-English document is not
    misflagged as contamination. QA must not disagree with the gate that ships docs.
    """
    from hunter.lang_guard import polish_fragments

    frags = polish_fragments(text or "", soft=False)
    return frags[0] if frags else None


def _check_no_polish_in_en_resume(resume_en: dict[str, Any]) -> QACheck:
    """Check summary, skills, and bullets for Polish diacritics or keywords.

    Language unity: resume_en must be entirely in English.
    Skills fields are the most common injection point for Polish job-posting
    keywords (e.g. 'Git / system kontroli wersji Git').
    """
    hits: list[str] = []

    # Summary
    summary = resume_en.get("summary") or ""
    m = _has_polish(summary)
    if m:
        hits.append(f"summary: '{m[:30]}'")

    # Skills — all skill fields concatenated
    skills = resume_en.get("skills") or {}
    for skill_key, skill_val in skills.items():
        if skill_key == "languages":
            continue  # language names like "Polish (B2)" are expected
        text = str(skill_val) if skill_val else ""
        m = _has_polish(text)
        if m:
            hits.append(f"skills.{skill_key}: '{m[:40]}'")

    # Experience bullets
    for entry in resume_en.get("experience") or []:
        company = entry.get("company", "?")
        for bullet in entry.get("bullets") or []:
            m = _has_polish(bullet)
            if m:
                hits.append(f"{company} bullet: '{m[:30]}'")
                break  # one per role

    ok = len(hits) == 0
    return QACheck(
        name="No Polish in EN resume",
        passed=ok,
        detail="; ".join(hits[:4]) if hits else "",
    )


def _check_cover_letter_en_language(content: dict[str, Any]) -> QACheck:
    """cover_letter_en must be in English."""
    cl = content.get("cover_letter_en") or ""
    frag = _has_polish(cl)
    has_en = bool(_EN_SENTENCE_RE.search(cl) or re.search(r"\bDear\b", cl, re.IGNORECASE))
    if frag and not has_en:
        return QACheck(
            name="cover_letter_en in English",
            passed=False,
            detail=f"Appears to be in Polish — found: '{frag[:40]}'",
        )
    if frag:
        return QACheck(
            name="cover_letter_en in English",
            passed=False,
            detail=f"Polish mixed into EN cover letter: '{frag[:40]}'",
        )
    return QACheck(name="cover_letter_en in English", passed=True)


def _check_education(resume_en: dict[str, Any]) -> QACheck:
    edu = (resume_en.get("education") or "").strip()
    if not edu:
        return QACheck(name="Education present", passed=False, detail="education field is empty")
    if edu.startswith("{") and ("degree" in edu or "school" in edu):
        return QACheck(
            name="Education not hallucinated dict",
            passed=False,
            detail=f"education is a stringified dict: {edu[:80]}",
        )
    # Check known correct school name
    if "belarusian state technological university" not in edu.lower():
        return QACheck(
            name="Education matches profile",
            passed=False,
            detail=f"Wrong school/degree: {edu[:100]}",
        )
    return QACheck(name="Education matches profile", passed=True)


# Canonical form for the bare Angular version skill (see generation_rules.md).
CANONICAL_ANGULAR_SKILL = "Angular (2-22)"


def is_angular_version_entry(item: str) -> bool:
    """True if a skills item is the bare Angular *version* entry (e.g. "Angular",
    "Angular (2-22)", "Angular 2+", "Angular (latest versions)") — NOT a distinct
    Angular-family skill like "Angular Material", "Angular CLI", "Angular development".

    Only version entries are deduplicated; family skills are legitimate and kept.
    """
    s = (item or "").strip()
    if not re.match(r"(?i)^angular\b", s):
        return False
    rest = s[len("angular") :]
    rest = re.sub(r"\([^)]*\)", "", rest)  # drop "(2-22)", "(latest versions)"
    rest = re.sub(r"(?i)[\d.+\-–x\s]", "", rest)  # drop version chars
    return rest == ""


def _check_no_duplicate_angular(resume_en: dict[str, Any]) -> QACheck:
    frontend = (resume_en.get("skills") or {}).get("frontend") or ""
    # Only flag duplicate *version* entries; "Angular Material" etc. are fine.
    version_entries = [e.strip() for e in frontend.split(",") if is_angular_version_entry(e)]
    ok = len(version_entries) <= 1
    return QACheck(
        name="No duplicate Angular in skills",
        passed=ok,
        detail=f"Found: {version_entries}" if not ok else "",
    )


def _check_titles(resume_en: dict[str, Any]) -> QACheck:
    bad: list[str] = []
    for entry in resume_en.get("experience") or []:
        title = (entry.get("title") or "").strip()
        norm = _norm_title(title)
        # Check against known canonical titles
        if norm not in _PROFILE_TITLES_NORM:
            bad.append(f"'{title}' at {entry.get('company', '?')}")
    ok = len(bad) == 0
    return QACheck(
        name="Experience titles match profile",
        passed=ok,
        detail="; ".join(bad[:3]) if bad else "",
    )


def _check_companies(resume_en: dict[str, Any]) -> QACheck:
    """All companies must be from the known whitelist."""
    bad: list[str] = []
    for entry in resume_en.get("experience") or []:
        company = (entry.get("company") or "").strip().lower()
        company_base = re.sub(r"\s*\(.*?\)", "", company).strip()
        matched = any(real in company_base or company_base in real for real in _REAL_COMPANIES)
        if not matched:
            bad.append(entry.get("company", "?"))
    ok = len(bad) == 0
    return QACheck(
        name="All companies from profile",
        passed=ok,
        detail=f"Unknown: {bad}" if bad else "",
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_qa(content: dict[str, Any]) -> QAReport:
    """Run all QA checks on a content dict. Returns a QAReport."""
    report = QAReport()
    resume_en = content.get("resume_en") or {}

    report.checks.append(_check_role_count(resume_en))
    report.checks.append(_check_companies(resume_en))
    report.checks.append(_check_titles(resume_en))
    report.checks.append(_check_no_polish_in_en_resume(resume_en))
    report.checks.append(_check_cover_letter_en_language(content))
    report.checks.append(_check_education(resume_en))
    report.checks.append(_check_no_duplicate_angular(resume_en))

    return report
