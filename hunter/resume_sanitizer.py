"""
hunter/resume_sanitizer.py — Resume integrity guard.

Runs after the LLM returns content.json, before rendering.

Responsibilities:
  1. Company whitelist enforcement — if the LLM invented / renamed a company,
     replace it with the nearest real role from candidate_profile.md (matched by
     period overlap; positional fallback when dates can't be parsed).
     Bullets and stack_line are kept intact (tailoring is allowed).
  2. Title enforcement — if the LLM renamed an experience title for a real company,
     restore it verbatim from the profile.
  3. Education & courses fallback — if the LLM omitted or left empty the
     `education` / `courses` fields, fill them verbatim from the profile.
     Also detects education stored as stringified Python dict.
  4. Language mixing guard (EN resumes only) — flags Polish diacritics / function
     words in resume_en summary and experience bullets.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

# Polish diacritics / function words that should never appear in an EN resume.
# Note: "jest" excluded — it matches the Jest testing framework (false positive).
_PL_IN_EN_RESUME_RE = re.compile(
    r"[ąęóśźżćńł]"
    r"|\b(się|przez|oraz|który|która|które|tego|czy|już"
    r"|jestem|moje|mojej|moich|swoim|swoją|swoje|gdzie|będę|będzie|chciałbym"
    r"|chciałabym|doświadczenie|specjalizuję|zajmuję|pracowałem|pracowałam"
    r"|zbudowałem|przeprowadziłem|posiadam|poszukuję|szukam|pisanie|pokrywanie"
    r"|projektowanie|programowaniu|programowanie|rozwiązań|rozwiązania"
    r"|frontendowych|jednostkowych|podobnymi|kontroli|wersji|systemu|wiedzy"
    r"|technicznej|wymiany|doświadczenia)\b",
    re.IGNORECASE,
)
# IT terms to strip before Polish checks (avoid false positives like "Jest")
_IT_TERMS_STRIP_RE = re.compile(
    r"\b(Jest|Angular|React|TypeScript|JavaScript|NgRx|RxJS|Nx|Node\.?js"
    r"|Jasmine|Karma|Jenkins|Webpack|Docker|GitHub|GitLab|SCSS|Bootstrap"
    r"|AG\s*Grid|Signals|Agile|Scrum|SAFe|REST|API|JSON|HTML|CSS|WCAG"
    r"|Cypress|Playwright|Next\.?js|NestJS|Redux|SonarQube)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_PROFILE_PATH = _PROMPTS_DIR / "candidate_profile.md"


def _coerce_str(val: Any) -> str:
    """Coerce LLM output to plain string (handles dict/list from malformed LLM responses)."""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return " ".join(str(v) for v in val.values() if v)
    if isinstance(val, list):
        return ", ".join(str(v) for v in val if v)
    return str(val) if val is not None else ""

# ---------------------------------------------------------------------------
# Month name → number map (handles EN and PL month names)
# ---------------------------------------------------------------------------
_MONTH_MAP: dict[str, int] = {
    # English
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    # Polish
    "sty": 1, "lut": 2, "mar": 3, "kwi": 4, "maj": 5, "cze": 6,
    "lip": 7, "sie": 8, "wrz": 9, "paź": 10, "paz": 10, "lis": 11, "gru": 12,
}


def _parse_period_date(token: str) -> int | None:
    """Parse a single date token like 'Apr 2026', 'June 2023', '2018' → YYYYMM int.
    Returns None if unparseable."""
    token = token.strip()
    # Try "Month YYYY" or "YYYY Month"
    m = re.search(r"([A-Za-zśćżźąęóńłŚĆŻŹĄĘÓŃŁ]+)\s+(\d{4})", token)
    if m:
        month_str = m.group(1).lower()[:3]
        month = _MONTH_MAP.get(month_str)
        if month:
            return int(m.group(2)) * 100 + month
    # Try bare year
    m = re.search(r"\b(\d{4})\b", token)
    if m:
        return int(m.group(1)) * 100 + 1  # treat as January
    return None


def _parse_period(period_str: str) -> tuple[int, int] | None:
    """Parse 'Apr 2026 - May 2026' → (start_yyyymm, end_yyyymm).
    'present' / 'current' treated as 209912. Returns None if unparseable."""
    period_str = period_str.strip()
    # Split on dash variants: " - ", " – ", " — "
    parts = re.split(r"\s*[-–—]\s*", period_str, maxsplit=1)
    if len(parts) == 2:
        start = _parse_period_date(parts[0])
        end_token = parts[1].lower().strip()
        if "present" in end_token or "current" in end_token or "now" in end_token:
            end = 209912
        else:
            end = _parse_period_date(parts[1])
            # Bare year as end → treat as December (job lasted until end of that year)
            if end and re.match(r"^\d{4}$", parts[1].strip()):
                end = (end // 100) * 100 + 12
        if start and end:
            return start, end
    # Single year range "2014 - 2017"
    years = re.findall(r"\d{4}", period_str)
    if len(years) == 2:
        return int(years[0]) * 100 + 1, int(years[1]) * 100 + 12
    if len(years) == 1:
        y = int(years[0]) * 100
        return y + 1, y + 12
    return None


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    """Return overlap in months between two (start, end) YYYYMM ranges."""
    latest_start = max(a[0], b[0])
    earliest_end = min(a[1], b[1])
    return max(0, earliest_end - latest_start)


# ---------------------------------------------------------------------------
# Profile parsing (cached — reads file once per process)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _load_profile_roles() -> list[dict[str, Any]]:
    """Parse ## Work Experience from candidate_profile.md → list of role dicts."""
    if not _PROFILE_PATH.exists():
        return []

    text = _PROFILE_PATH.read_text(encoding="utf-8")

    # Extract the Work Experience block
    we_match = re.search(r"### Work Experience\s*(.*?)(?=^---|\Z)", text, re.DOTALL | re.MULTILINE)
    if not we_match:
        return []
    we_block = we_match.group(1)

    roles: list[dict[str, Any]] = []

    # Each role starts with: **Title | Company** - Period
    role_pattern = re.compile(
        r"\*\*(?P<title>[^|*]+?)\s*\|\s*(?P<company>[^*]+?)\*\*\s*-\s*(?P<period>[^\n]+)\n"
        r"(?P<subtitle>[^\n]*)\n",
        re.MULTILINE,
    )

    for m in role_pattern.finditer(we_block):
        company = m.group("company").strip()
        period = m.group("period").strip()
        subtitle = m.group("subtitle").strip()
        parsed = _parse_period(period)
        roles.append({
            "company": company,
            "period": period,
            "subtitle": subtitle,
            "title": m.group("title").strip(),
            "parsed": parsed,  # (start_yyyymm, end_yyyymm) or None
        })

    return roles


@lru_cache(maxsize=1)
def _load_profile_education_courses() -> tuple[str, str]:
    """Parse Education and Additional Courses from candidate_profile.md.
    Returns (education_en, courses_en)."""
    if not _PROFILE_PATH.exists():
        return ("", "")

    text = _PROFILE_PATH.read_text(encoding="utf-8")

    edu_match = re.search(r"\*\*Education\*\*:\s*(.+)", text)
    edu = edu_match.group(1).strip() if edu_match else ""

    courses_match = re.search(r"\*\*Additional Courses\*\*:\s*(.+)", text)
    courses = courses_match.group(1).strip() if courses_match else ""

    return edu, courses


def _whitelist() -> set[str]:
    """Return the set of real company names (lowercased for comparison)."""
    return {r["company"].lower() for r in _load_profile_roles()}


def _base_name(company: str) -> str:
    """Strip parenthetical suffixes: 'Fairmarkit (via contractor)' → 'fairmarkit'."""
    return re.sub(r"\s*\(.*?\)", "", company).lower().strip()


def _is_real_company(company: str) -> bool:
    """Check if company name (fuzzy) matches any whitelisted real company.

    Handles:
    - Exact match: "SII" == "SII"
    - Contains: "Fairmarkit (via contractor)" in whitelist; input "Fairmarkit (via contractor)"
    - Base-name match: "Fairmarkit (przez kontraktora)" → base "fairmarkit" matches
      whitelist entry base "fairmarkit"
    """
    c = company.lower().strip()
    c_base = _base_name(company)
    for real in _whitelist():
        real_base = _base_name(real)
        if c == real or c in real or real in c:
            return True
        if c_base and real_base and (c_base == real_base or c_base in real_base or real_base in c_base):
            return True
    return False


def _best_match_role(fake_period: str, used_indices: set[int]) -> dict[str, Any] | None:
    """Find the best-matching real role for a hallucinated entry.

    Strategy:
      1. Parse fake period → try period-overlap match.
      2. If no parseable dates → positional fallback (first unused role by index).
    """
    roles = _load_profile_roles()
    if not roles:
        return None

    fake_parsed = _parse_period(fake_period) if fake_period else None

    if fake_parsed:
        # Score = overlap; pick highest overlap not yet used
        best_score = -1
        best_idx = -1
        for i, role in enumerate(roles):
            if i in used_indices:
                continue
            if role["parsed"]:
                score = _overlap(fake_parsed, role["parsed"])
                if score > best_score:
                    best_score = score
                    best_idx = i

        if best_idx >= 0 and best_score > 0:
            return {"idx": best_idx, **roles[best_idx]}

    # Positional fallback: first unused role in profile order
    for i, role in enumerate(roles):
        if i not in used_indices:
            return {"idx": i, **roles[i]}

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sanitize_resume(resume: dict[str, Any], lang: str = "EN") -> tuple[dict[str, Any], list[str]]:
    """Sanitize one resume dict (resume_en or resume_pl).

    Modifies in-place and returns (resume, list_of_fix_messages).

    Fixes applied:
      - Hallucinated company → replaced with real company from profile (by period overlap).
        Bullets and stack_line are kept (tailoring is allowed).
      - Missing / empty education → filled from profile.
      - Missing / empty courses → filled from profile.
    """
    fixes: list[str] = []
    if not resume:
        return resume, fixes

    edu_en, courses_en = _load_profile_education_courses()

    # -- 1. Education & courses fallback --
    # Coerce to string first (LLM sometimes returns dict/list)
    if not isinstance(resume.get("education"), str):
        resume["education"] = _coerce_str(resume.get("education"))
        fixes.append(f"[{lang}] education coerced to string")
    if not isinstance(resume.get("courses"), str):
        resume["courses"] = _coerce_str(resume.get("courses"))
        fixes.append(f"[{lang}] courses coerced to string")

    # Detect education that is a stringified Python dict — LLM hallucinated a structured object
    edu_val = (resume.get("education") or "").strip()
    if edu_val.startswith("{") and ("degree" in edu_val or "school" in edu_val):
        resume["education"] = edu_en
        fixes.append(f"[{lang}] education was dict-as-string (hallucinated) → replaced with profile education")
    elif not edu_val:
        if edu_en:
            resume["education"] = edu_en
            fixes.append(f"[{lang}] education filled from profile")

    if not (resume.get("courses") or "").strip():
        if courses_en:
            resume["courses"] = courses_en
            fixes.append(f"[{lang}] courses filled from profile")

    # -- 1b. Collapse duplicate Angular version entries in skills.frontend --
    # The LLM/ATS rewrite sometimes lists two version forms ("Angular (2-22)" +
    # "Angular (latest versions)"). Keep ONE canonical entry; distinct family
    # skills ("Angular Material", "Angular CLI") are left untouched.
    skills = resume.get("skills")
    if isinstance(skills, dict) and isinstance(skills.get("frontend"), str):
        from hunter.content_qa import CANONICAL_ANGULAR_SKILL, is_angular_version_entry
        items = [i.strip() for i in skills["frontend"].split(",") if i.strip()]
        version_idx = [n for n, it in enumerate(items) if is_angular_version_entry(it)]
        if len(version_idx) > 1:
            keep = version_idx[0]
            items[keep] = CANONICAL_ANGULAR_SKILL
            drop = set(version_idx[1:])
            new_items = [it for n, it in enumerate(items) if n not in drop]
            skills["frontend"] = ", ".join(new_items)
            fixes.append(
                f"[{lang}] collapsed {len(version_idx)} Angular version entries → "
                f"'{CANONICAL_ANGULAR_SKILL}'"
            )

    # -- 2. Company whitelist enforcement --
    experience = resume.get("experience")
    if not experience:
        return resume, fixes

    used_indices: set[int] = set()

    # First pass: mark already-correct entries so they don't get re-used
    for entry in experience:
        company = (entry.get("company") or "").strip()
        if _is_real_company(company):
            roles = _load_profile_roles()
            for i, role in enumerate(roles):
                if role["company"].lower() in company.lower() or company.lower() in role["company"].lower():
                    used_indices.add(i)
                    break

    # Second pass: fix hallucinated entries; enforce title for real ones
    roles = _load_profile_roles()
    for entry in experience:
        company = (entry.get("company") or "").strip()
        if _is_real_company(company):
            # Title enforcement: restore profile title if LLM renamed it
            if lang == "EN":
                for role in roles:
                    if (role["company"].lower() in company.lower()
                            or company.lower() in role["company"].lower()):
                        profile_title = role["title"]
                        entry_title = (entry.get("title") or "").strip()
                        # Normalise for comparison: lowercase, strip (Angular)/(React) suffix
                        def _norm(t: str) -> str:
                            return re.sub(r"\s*\([^)]+\)", "", t).lower().strip()
                        if _norm(entry_title) != _norm(profile_title):
                            fixes.append(
                                f"[EN] title '{entry_title}' for '{company}' → '{profile_title}' (verbatim from profile)"
                            )
                            entry["title"] = profile_title
                        break
            continue  # company is real, skip hallucination fix

        fake_period = (entry.get("period") or "").strip()
        match = _best_match_role(fake_period, used_indices)

        if match is None:
            fixes.append(f"[{lang}] WARNING: could not find match for fake company '{company}' — left as-is")
            continue

        used_indices.add(match["idx"])
        old_company = company
        entry["company"] = match["company"]
        entry["period"] = match["period"]
        entry["subtitle"] = match["subtitle"]
        fixes.append(
            f"[{lang}] '{old_company}' ({fake_period}) → '{match['company']}' ({match['period']})"
        )

    # -- 3. Language unity guard (EN only) --
    # resume_en must be entirely in English — check summary, skills, and bullets.
    # Skills are the most common injection point for raw Polish job-posting keywords.
    if lang == "EN":
        summary = resume.get("summary") or ""
        cleaned_summary = _IT_TERMS_STRIP_RE.sub("", summary)
        if _PL_IN_EN_RESUME_RE.search(cleaned_summary):
            m = _PL_IN_EN_RESUME_RE.search(cleaned_summary)
            fixes.append(
                f"[EN] WARNING: Polish in summary: '{m.group()[:40]}' — "
                "translate job-posting keywords to English before inserting."
            )

        skills = resume.get("skills") or {}
        for skill_key, skill_val in skills.items():
            if skill_key == "languages":
                continue  # "Polish (B2)" etc. are expected
            cleaned_skill = _IT_TERMS_STRIP_RE.sub("", str(skill_val or ""))
            if _PL_IN_EN_RESUME_RE.search(cleaned_skill):
                m = _PL_IN_EN_RESUME_RE.search(cleaned_skill)
                fixes.append(
                    f"[EN] WARNING: Polish in skills.{skill_key}: '{m.group()[:40]}' — "
                    "LLM pasted PL job-posting keywords into skills verbatim."
                )

        for entry in experience:
            for bullet in (entry.get("bullets") or []):
                cleaned_bullet = _IT_TERMS_STRIP_RE.sub("", bullet)
                if _PL_IN_EN_RESUME_RE.search(cleaned_bullet):
                    m = _PL_IN_EN_RESUME_RE.search(cleaned_bullet)
                    fixes.append(
                        f"[EN] WARNING: Polish in bullet "
                        f"({entry.get('company','')}): '{m.group()[:40]}'"
                    )
                    break  # one warning per role is enough

    return resume, fixes


def sanitize_content(content: dict[str, Any]) -> dict[str, Any]:
    """Sanitize both resume_en and resume_pl inside a full content dict.

    Logs all fixes. Returns the modified content dict.
    """
    all_fixes: list[str] = []

    if content.get("resume_en"):
        content["resume_en"], fixes = sanitize_resume(content["resume_en"], lang="EN")
        all_fixes.extend(fixes)

    if content.get("resume_pl"):
        content["resume_pl"], fixes = sanitize_resume(content["resume_pl"], lang="PL")
        all_fixes.extend(fixes)

    if all_fixes:
        print("[resume_sanitizer] Applied fixes:")
        for fix in all_fixes:
            print(f"  {fix}")
    else:
        print("[resume_sanitizer] No sanitization needed.")

    return content
