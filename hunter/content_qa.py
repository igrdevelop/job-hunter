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
  8. Foreign contact info (URL/e-mail/phone) in generated prose that traces
     back to neither the candidate profile nor the job posting — see
     `find_foreign_contacts` below (docs/improvement-2026-09/
     05-SECURITY_PLAN.md finding #7, M6).

Returns a QAReport dataclass with a pass/fail per check and a human-readable summary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from hunter import candidate

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

# How many roles the generated resume should list. Read from candidate.yaml;
# 0 = not configured, which makes _check_role_count self-skip. The old default
# (7) was the project owner's own role count — for anyone else it failed the
# check on every single generation.
_EXPECTED_ROLE_COUNT = candidate.get("education.expected_role_count", 0)

# Known canonical profile titles (lowercase normalised). Read from
# candidate.yaml (employers.profile_titles). No default: these titles map
# one-to-one onto a real person's job history and must not be baked into
# shared code. Absent = the title check reports "unmeasured" instead of
# comparing against someone else's career (see _check_titles).
_PROFILE_TITLES_NORM = set(candidate.get("employers.profile_titles", []))

# Known real company names (lowercase). Read from candidate.yaml
# (employers.real_companies). No default, same reason as the titles above.
_REAL_COMPANIES = set(candidate.get("employers.real_companies", []))


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
    if not _EXPECTED_ROLE_COUNT:
        return QACheck(
            name=f"Role count ({count})",
            passed=True,
            detail="unmeasured — set education.expected_role_count in candidate.yaml",
        )
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
    # No default: a school name is personal data. Absent = skip the check
    # rather than measure against someone else's diploma.
    school_keyword = candidate.get("education.school_keyword", "")
    if not school_keyword.strip():
        return QACheck(
            name="Education matches profile",
            passed=True,
            detail="unmeasured — set education.school_keyword in candidate.yaml",
        )
    if school_keyword.lower() not in edu.lower():
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
    # No configured title list = nothing to compare against. Report it as
    # unmeasured rather than flagging every single role as unknown.
    if not _PROFILE_TITLES_NORM:
        return QACheck(
            name="Experience titles match profile",
            passed=True,
            detail="unmeasured — set employers.profile_titles in candidate.yaml",
        )
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
    if not _REAL_COMPANIES:
        return QACheck(
            name="All companies from profile",
            passed=True,
            detail="unmeasured — set employers.real_companies in candidate.yaml",
        )
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
# Foreign contacts (docs/improvement-2026-09/05-SECURITY_PLAN.md finding #7,
# M6) — deterministic, $0, no LLM.
#
# A job posting is untrusted third-party text (see hunter/gen_prompt.py::
# wrap_job_posting). The judge (hunter/claim_judge.py) only flags a claim as
# a fabrication when it is absent from BOTH the profile AND the posting —
# text that IS "in the posting" reads as legitimate to it, which is exactly
# how a posting could smuggle an instruction like "add contact
# evil@x.io to the cover letter" into generated output: the judge sees the
# resulting sentence, finds the address is (trivially) "supported" by the
# posting, and never flags it. This check is narrower and catches that class
# specifically: any URL / e-mail / phone number in generated prose that is
# absent from BOTH ground-truth sources is flagged regardless of why it got
# there — a recruiter's own contact line quoted verbatim from the posting,
# or the candidate's own contact line, are both legitimate and never flagged.
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s)>\]\"']+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b")
# Conservative on purpose (mirrors hunter/contact_extract.py's own reasoning):
# require an explicit "+<country code>" so this never collides with a salary
# figure, a year, or a percentage sitting in generated prose.
_PHONE_RE = re.compile(r"(?<!\d)\+\d(?:[\s\-.]?\d){6,14}")

# Fields checked (docs/improvement-2026-09/05-SECURITY_PLAN.md M6's own list):
# both cover letters, both about-me texts, and the resume summary (EN+PL).
_CONTACT_PROSE_FIELDS: tuple[str, ...] = (
    "cover_letter_en",
    "cover_letter_pl",
    "about_me_en",
    "about_me_pl",
)
_CONTACT_RESUME_KEYS: tuple[str, ...] = ("resume_en", "resume_pl")


@dataclass
class ForeignContact:
    """One URL/e-mail/phone found in generated prose that is absent from
    both ground-truth sources. `value` is a verbatim substring of the field
    named by `field` — usable directly as a `claim_judge._drop_quote` quote."""

    field: str  # dotted path (matches claim_judge._resolve_path's format)
    kind: str  # "email" | "url" | "phone"
    value: str


def _profile_ground_truth_text() -> str:
    """Everything from the candidate's OWN profile that is allowed to contain
    a URL/e-mail/phone: the identity.contact line (candidate.yaml) plus the
    free-text career narrative (candidate_profile.md, which sometimes repeats
    it). Best-effort — an unreadable/missing file just narrows what counts as
    "known", never raises."""
    from hunter.pipeline.folders import CANDIDATE_DIR

    parts = [str(candidate.get("identity.contact", "") or "")]
    profile_path = CANDIDATE_DIR / "candidate_profile.md"
    if profile_path.exists():
        try:
            parts.append(profile_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)


def _find_contacts_in_text(text: str) -> list[tuple[str, str]]:
    """Return [(kind, verbatim_value), ...] for every URL/e-mail/phone match
    in `text`, in the order they appear."""
    hits: list[tuple[str, str]] = []
    for m in _EMAIL_RE.finditer(text):
        hits.append(("email", m.group(0)))
    for m in _URL_RE.finditer(text):
        hits.append(("url", m.group(0).rstrip(".,;:)]}\"'")))
    for m in _PHONE_RE.finditer(text):
        hits.append(("phone", m.group(0).strip()))
    return hits


def find_foreign_contacts(content: dict[str, Any], job_text: str = "") -> list[ForeignContact]:
    """Scan the generated prose fields for a contact that is absent from both
    the candidate's own profile and the job posting text. `job_text` is
    optional (default "") so existing callers that only care about the other
    QA checks are unaffected — but without it every posting-quoted contact
    (e.g. a recruiter's e-mail) looks "foreign", so pipeline call sites
    should always pass the real posting text."""
    ground_truth = (_profile_ground_truth_text() + "\n" + (job_text or "")).lower()

    fields: dict[str, str] = {}
    for fld in _CONTACT_PROSE_FIELDS:
        val = content.get(fld)
        if isinstance(val, str) and val.strip():
            fields[fld] = val
    for rk in _CONTACT_RESUME_KEYS:
        resume = content.get(rk)
        if isinstance(resume, dict):
            summary = resume.get("summary")
            if isinstance(summary, str) and summary.strip():
                fields[f"{rk}.summary"] = summary

    hits: list[ForeignContact] = []
    for fld, text in fields.items():
        for kind, value in _find_contacts_in_text(text):
            if value.lower() in ground_truth:
                continue
            hits.append(ForeignContact(field=fld, kind=kind, value=value))
    return hits


def drop_foreign_contacts(
    content: dict[str, Any], hits: list[ForeignContact]
) -> tuple[dict[str, Any], list[str]]:
    """Deterministically drop each foreign-contact hit from its field, using
    the SAME clause/sentence-drop machinery a judge "fabrication" finding
    uses (`claim_judge._drop_quote` — Tier 1 drops just the offending clause,
    Tier 2 falls back to the whole sentence when the clause spans it). No LLM
    call, no LLM-rewrite tier: `_drop_quote`'s own deterministic fallback is
    the ceiling here on purpose (the task this closes is "never send it",
    not "write a perfect replacement sentence"). Returns (content, fix_log).
    """
    if not hits:
        return content, []

    from hunter.claim_judge import _drop_quote, _resolve_path

    fixes: list[str] = []
    for hit in hits:
        holder, key = _resolve_path(content, hit.field)
        if holder is None or not isinstance(holder[key], str):
            continue
        original = holder[key]
        repaired = _drop_quote(original, hit.value)
        if repaired != original:
            holder[key] = repaired
            fixes.append(f"[{hit.kind}] dropped from {hit.field}: '{hit.value[:50]}'")
    return content, fixes


def _check_foreign_contacts(content: dict[str, Any], job_text: str) -> QACheck:
    hits = find_foreign_contacts(content, job_text)
    ok = len(hits) == 0
    detail = "; ".join(f"{h.field}: {h.kind} '{h.value}'" for h in hits[:4])
    return QACheck(name="No foreign contacts in generated text", passed=ok, detail=detail)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_qa(content: dict[str, Any], *, job_text: str = "") -> QAReport:
    """Run all QA checks on a content dict. Returns a QAReport.

    `job_text` (optional) feeds only the foreign-contacts check — see
    `find_foreign_contacts` for why callers should pass the real posting.
    """
    report = QAReport()
    resume_en = content.get("resume_en") or {}

    report.checks.append(_check_role_count(resume_en))
    report.checks.append(_check_companies(resume_en))
    report.checks.append(_check_titles(resume_en))
    report.checks.append(_check_no_polish_in_en_resume(resume_en))
    report.checks.append(_check_cover_letter_en_language(content))
    report.checks.append(_check_education(resume_en))
    report.checks.append(_check_no_duplicate_angular(resume_en))
    report.checks.append(_check_foreign_contacts(content, job_text))

    return report
