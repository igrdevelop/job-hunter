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
# Regex helpers (same patterns as Gate 8 in apply_shared.py)
# ---------------------------------------------------------------------------
_PL_DIACRITICS_RE = re.compile(r"[ąęóśźżćńł]", re.IGNORECASE)
_PL_WORDS_RE = re.compile(
    r"\b(się|przez|oraz|który|która|które|tego|czy|już"
    r"|pisanie|pokrywanie|projektowanie|programowaniu|programowanie|rozwiązań"
    r"|frontendowych|jednostkowych|podobnymi|kontroli|wersji|systemu|wiedzy"
    r"|technicznej|doświadczenia|doświadczenie|wymagania|umiejętność)\b",
    re.IGNORECASE,
)
# IT terms that look like Polish words — strip before checking
_IT_TERMS_RE = re.compile(
    r"\b(Jest|Angular|React|TypeScript|JavaScript|NgRx|RxJS|Nx|Node\.?js"
    r"|Jasmine|Karma|Jenkins|Webpack|Docker|GitHub|GitLab|CI/CD|SCSS|Bootstrap"
    r"|AG\s*Grid|Signals|Agile|Scrum|SAFe|REST|API|JSON|HTML|CSS|WCAG"
    r"|Cypress|Playwright|Next\.?js|NestJS|Redux|SonarQube|Jasmine)\b",
    re.IGNORECASE,
)
_EN_SENTENCE_RE = re.compile(
    r"\b(I am writing|I would like|I have been|As a Senior|I look forward"
    r"|I bring|I have worked|In my previous|Dear Hiring|With over)\b",
    re.IGNORECASE,
)

_EXPECTED_ROLE_COUNT = 7

# Known canonical profile titles (lowercase normalised)
_PROFILE_TITLES_NORM = {
    "frontend developer (angular, part-time contract)",  # Alten Poland
    "senior frontend developer (angular)",               # Fairmarkit, Venture Labs, SII
    "senior frontend developer",                         # Altoros
    "frontend developer (angular)",                      # SolbegSoft
    "frontend developer",                                # Staronka
}

# Known real company names (lowercase)
_REAL_COMPANIES = {
    "alten poland", "fairmarkit", "venture labs", "sii", "altoros",
    "solbegsoft", "staronka",
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
        fail_lines = "\n".join(
            f"• <b>{c.name}</b>: {c.detail[:120]}" for c in fails
        )
        return (
            f"⚠️ <b>QA: {len(fails)} check(s) failed</b>\n"
            f"🔗 {url}\n\n"
            f"{fail_lines}"
        )


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


def _strip_it_terms(text: str) -> str:
    """Remove known IT terms before language-mixing checks to avoid false positives."""
    return _IT_TERMS_RE.sub("", text)


def _has_polish(text: str) -> re.Match | None:
    """Return first Polish match after stripping IT terms, or None."""
    cleaned = _strip_it_terms(text)
    return _PL_DIACRITICS_RE.search(cleaned) or _PL_WORDS_RE.search(cleaned)


def _check_no_polish_in_en_resume(resume_en: dict[str, Any]) -> QACheck:
    """Check summary and bullets for Polish diacritics or keywords."""
    hits: list[str] = []

    summary = resume_en.get("summary") or ""
    m = _has_polish(summary)
    if m:
        hits.append(f"summary: '{m.group()[:30]}'")

    for entry in (resume_en.get("experience") or []):
        company = entry.get("company", "?")
        for bullet in (entry.get("bullets") or []):
            m = _has_polish(bullet)
            if m:
                hits.append(f"{company} bullet: '{m.group()[:30]}'")
                break  # one per role

    ok = len(hits) == 0
    return QACheck(
        name="No Polish in EN resume",
        passed=ok,
        detail="; ".join(hits[:3]) if hits else "",
    )


def _check_cover_letter_en_language(content: dict[str, Any]) -> QACheck:
    """cover_letter_en must be in English."""
    cl = content.get("cover_letter_en") or ""
    cleaned = _strip_it_terms(cl)
    has_pl = bool(_PL_DIACRITICS_RE.search(cleaned) or _PL_WORDS_RE.search(cleaned))
    has_en = bool(_EN_SENTENCE_RE.search(cl) or re.search(r"\bDear\b", cl, re.IGNORECASE))
    if has_pl and not has_en:
        m = _PL_DIACRITICS_RE.search(cleaned) or _PL_WORDS_RE.search(cleaned)
        return QACheck(
            name="cover_letter_en in English",
            passed=False,
            detail=f"Appears to be in Polish — found: '{m.group()[:40]}'",
        )
    if has_pl:
        m = _PL_DIACRITICS_RE.search(cleaned) or _PL_WORDS_RE.search(cleaned)
        return QACheck(
            name="cover_letter_en in English",
            passed=False,
            detail=f"Polish mixed into EN cover letter: '{m.group()[:40]}'",
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


def _check_no_duplicate_angular(resume_en: dict[str, Any]) -> QACheck:
    frontend = (resume_en.get("skills") or {}).get("frontend") or ""
    # Count Angular-like entries: "Angular (2-XX)", "Angular 2+", plain "Angular"
    angular_entries = re.findall(r"\bAngular\b[^,]*", frontend, re.IGNORECASE)
    # Deduplicate by normalising
    unique = {re.sub(r"\s*\(.*?\)", "", e).strip().lower() for e in angular_entries}
    ok = len(unique) <= 1
    return QACheck(
        name="No duplicate Angular in skills",
        passed=ok,
        detail=f"Found: {angular_entries}" if not ok else "",
    )


def _check_titles(resume_en: dict[str, Any]) -> QACheck:
    bad: list[str] = []
    for entry in (resume_en.get("experience") or []):
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
    for entry in (resume_en.get("experience") or []):
        company = (entry.get("company") or "").strip().lower()
        company_base = re.sub(r"\s*\(.*?\)", "", company).strip()
        matched = any(
            real in company_base or company_base in real
            for real in _REAL_COMPANIES
        )
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
