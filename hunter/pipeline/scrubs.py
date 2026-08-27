"""
hunter/pipeline/scrubs.py — content scrubs for the apply pipeline: strip
fabricated compliance/prestige claims and collapse skill-gloss duplicates.
Moved out of hunter/apply_shared.py
(docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) — see hunter.apply_shared
for the backward-compat re-export.
"""

from __future__ import annotations

import re

from hunter import text_repair

# Word-boundary matcher for regulatory/compliance terms that an employer lists as
# its own credentials. Used to scrub fabricated claims the LLM may still write into
# the summary / skills / about-me despite the generation_rules.md RED LINE.
_COMPLIANCE_CLAIM_RE = re.compile(
    r"\b(?:DORA|RODO|GDPR|ISO(?:\s?\d{4,5})?|SOC\s?2|HIPAA|PCI(?:[-\s]?DSS)?)\b",
    re.IGNORECASE,
)

# Removes a connector + compliance phrase embedded in a bullet/stack_line, e.g.
# " with DORA compliance", " following ISO standards", " and GDPR compliance".
_COMPLIANCE_CLAUSE_RE = re.compile(
    r"\s*(?:[,;]|\b(?:with|following|and|including|under|per|ensuring|maintaining"
    r"|adhering to|in line with|compliant with|aligned with)\b)\s+[^,.;]*?"
    r"\b(?:DORA|RODO|GDPR|ISO(?:\s?\d{4,5})?|SOC\s?2|HIPAA|PCI(?:[-\s]?DSS)?)\b"
    r"(?:\s+(?:compliance|standards?|adherence|certification|requirements?))?",
    re.IGNORECASE,
)


def _scrub_compliance_clause(text: str) -> str:
    """Remove embedded compliance clauses from a bullet/stack_line while keeping
    the rest of the sentence intact. Loops until stable to catch chained clauses
    ('following ISO standards and DORA compliance')."""
    if not isinstance(text, str):
        return text
    prev = None
    cur = text
    while prev != cur and _COMPLIANCE_CLAIM_RE.search(cur):
        prev = cur
        m = _COMPLIANCE_CLAUSE_RE.search(cur)
        if not m:
            break
        # Repair the seam where the clause actually sat (hunter.text_repair)
        # instead of patching the whole field with global regexes afterwards.
        cur = text_repair.cut_span(cur, m.start(), m.end())
    # Tidy leftovers: dangling connectors/punctuation before end.
    cur = re.sub(r"[^\S\n]+(?:and|with|following|including)\s*$", "", cur, flags=re.IGNORECASE)
    cur = re.sub(r"[^\S\n]*[,;]\s*$", "", cur)
    return cur.strip()


def _strip_compliance_claims(content: dict) -> tuple[dict, list[str]]:
    """Remove fabricated regulatory/compliance claims (DORA, RODO, GDPR, ISO,
    SOC2, HIPAA, PCI) from summary / skills / about-me text. These come from the
    employer's self-description and must never be claimed as the candidate's own
    expertise. Returns (content, list_of_fixes)."""
    fixes: list[str] = []

    def _scrub_sentences(text: str, label: str) -> str:
        if not isinstance(text, str) or not _COMPLIANCE_CLAIM_RE.search(text):
            return text
        new = text_repair.drop_sentences(
            text, lambda s: bool(_COMPLIANCE_CLAIM_RE.search(s))
        ).strip()
        if new != text:
            fixes.append(f"[{label}] removed compliance-claim sentence(s)")
        return new

    def _scrub_skills(skills: object, label: str) -> object:
        if isinstance(skills, dict):
            for cat, val in list(skills.items()):
                if isinstance(val, str) and _COMPLIANCE_CLAIM_RE.search(val):
                    items = [i for i in val.split(",") if not _COMPLIANCE_CLAIM_RE.search(i)]
                    new = ", ".join(s.strip() for s in items if s.strip())
                    if new != val:
                        skills[cat] = new
                        fixes.append(f"[{label}] removed compliance terms from skills.{cat}")
                elif isinstance(val, list) and any(
                    _COMPLIANCE_CLAIM_RE.search(str(i)) for i in val
                ):
                    skills[cat] = [i for i in val if not _COMPLIANCE_CLAIM_RE.search(str(i))]
                    fixes.append(f"[{label}] removed compliance terms from skills.{cat}")
        return skills

    def _scrub_experience(exp: object, label: str) -> None:
        if not isinstance(exp, list):
            return
        for role in exp:
            if not isinstance(role, dict):
                continue
            bullets = role.get("bullets")
            if isinstance(bullets, list):
                new_bullets = []
                for b in bullets:
                    nb = _scrub_compliance_clause(b) if isinstance(b, str) else b
                    if isinstance(b, str) and nb != b:
                        fixes.append(f"[{label}] scrubbed compliance clause from a bullet")
                    # Drop a bullet that was ONLY a compliance claim (now empty)
                    if isinstance(nb, str) and not nb.strip():
                        continue
                    new_bullets.append(nb)
                role["bullets"] = new_bullets
            for fld in ("stack_line", "subtitle"):
                if isinstance(role.get(fld), str) and _COMPLIANCE_CLAIM_RE.search(role[fld]):
                    new = _scrub_compliance_clause(role[fld])
                    if new != role[fld]:
                        role[fld] = new
                        fixes.append(f"[{label}] scrubbed compliance from {fld}")

    for rk, lang in (("resume_en", "EN"), ("resume_pl", "PL")):
        r = content.get(rk)
        if isinstance(r, dict):
            if "summary" in r:
                r["summary"] = _scrub_sentences(r["summary"], f"{lang} summary")
            if "skills" in r:
                r["skills"] = _scrub_skills(r["skills"], lang)
            _scrub_experience(r.get("experience"), lang)
            # Courses: comma-separated; drop any item naming a compliance framework.
            if isinstance(r.get("courses"), str) and _COMPLIANCE_CLAIM_RE.search(r["courses"]):
                items = [i for i in r["courses"].split(",") if not _COMPLIANCE_CLAIM_RE.search(i)]
                new = ", ".join(s.strip() for s in items if s.strip())
                if new != r["courses"]:
                    r["courses"] = new
                    fixes.append(f"[{lang}] removed compliance item from courses")
    for ak, lang in (("about_me_en", "EN"), ("about_me_pl", "PL")):
        if ak in content:
            content[ak] = _scrub_sentences(content[ak], f"{lang} about_me")

    return content, fixes


# ---------------------------------------------------------------------------
# Prestige-claim scrub — "Fortune 500 clients", "top-tier clients", ...
# ---------------------------------------------------------------------------
# generation_rules.md RED LINE: "NEVER invent client scale or prestige." The LLM
# still fabricates these (observed: "300+ German banks and Fortune 500 clients"
# in both EN and PL summaries for a posting that never mentions Fortune 500), so
# the rule is enforced deterministically here. A term that DOES appear in the
# job posting text is allowed (the rule's explicit exception) and is not scrubbed.
_PRESTIGE_TERMS: tuple[str, ...] = (
    r"Fortune\s?(?:50|100|500|1000)",
    r"top[-\s]tier",
    r"blue[-\s]chip",
)

# Connectors that attach a prestige clause to an otherwise-honest sentence,
# e.g. "for 300+ German banks and Fortune 500 clients" (EN) or
# "dla 300+ niemieckich banków i klientów Fortune 500" (PL).
_PRESTIGE_CONNECTORS = r"and|with|for|including|serving|plus|i|oraz|dla|w tym"

_PRESTIGE_TRAILING_NOUNS = (
    r"(?:\s+(?:clients?|companies|firms?|customers|enterprises|brands"
    r"|klientów|klienci|firm))?"
)


def _prestige_claim_re(job_text: str) -> re.Pattern | None:
    """Combined matcher for prestige terms NOT present in the job posting.
    Returns None when every term is legitimised by the posting."""
    active = [t for t in _PRESTIGE_TERMS if not re.search(t, job_text or "", re.IGNORECASE)]
    if not active:
        return None
    return re.compile(r"\b(?:" + "|".join(active) + r")\b", re.IGNORECASE)


def _prestige_clause_re(claim_re: re.Pattern) -> re.Pattern:
    """Connector + clause containing a prestige term, within one comma/period
    segment. The middle part is tempered so the match cannot swallow an earlier
    honest clause ("for 300+ German banks and Fortune 500" must only remove
    " and Fortune 500 ...", not the banks)."""
    middle = rf"(?:(?!\b(?:{_PRESTIGE_CONNECTORS})\b)[^,.;])*?"
    return re.compile(
        rf"\s*(?:[,;]|\b(?:{_PRESTIGE_CONNECTORS})\b)\s+{middle}"
        rf"(?:{claim_re.pattern}){_PRESTIGE_TRAILING_NOUNS}",
        re.IGNORECASE,
    )


def _scrub_prestige_text(text: str, claim_re: re.Pattern) -> str:
    """Remove prestige clauses from a sentence-ish text; if a claim survives
    clause removal (e.g. it opens the sentence), drop the whole sentence."""
    if not isinstance(text, str) or not claim_re.search(text):
        return text
    clause_re = _prestige_clause_re(claim_re)
    prev = None
    cur = text
    while prev != cur and claim_re.search(cur):
        prev = cur
        m = clause_re.search(cur)
        if not m:
            break
        cur = text_repair.cut_span(cur, m.start(), m.end())
    if claim_re.search(cur):  # clause removal couldn't reach it → drop sentence
        cur = text_repair.drop_sentences(cur, lambda s: bool(claim_re.search(s)))
    cur = re.sub(rf"[^\S\n]+(?:{_PRESTIGE_CONNECTORS})\s*$", "", cur, flags=re.IGNORECASE)
    cur = re.sub(r"[^\S\n]+([,.;])", r"\1", cur)
    cur = re.sub(r"[^\S\n]*[,;]\s*$", "", cur)
    return cur.strip()


def _strip_prestige_claims(content: dict, job_text: str = "") -> tuple[dict, list[str]]:
    """Remove fabricated client-prestige claims (Fortune 500, top-tier,
    blue-chip) from summary / skills / experience / about-me in both resume
    languages. Terms actually present in the job posting are left alone.
    Returns (content, list_of_fixes)."""
    fixes: list[str] = []
    claim_re = _prestige_claim_re(job_text)
    if claim_re is None:
        return content, fixes

    def _scrub_field(holder: dict, key: str, label: str) -> None:
        val = holder.get(key)
        if isinstance(val, str) and claim_re.search(val):
            new = _scrub_prestige_text(val, claim_re)
            if new != val:
                holder[key] = new
                fixes.append(f"[{label}] scrubbed prestige claim from {key}")

    for rk, lang in (("resume_en", "EN"), ("resume_pl", "PL")):
        r = content.get(rk)
        if not isinstance(r, dict):
            continue
        _scrub_field(r, "summary", lang)
        skills = r.get("skills")
        if isinstance(skills, dict):
            for cat, val in list(skills.items()):
                if isinstance(val, str) and claim_re.search(val):
                    items = [i.strip() for i in _split_skill_items(val) if not claim_re.search(i)]
                    skills[cat] = ", ".join(i for i in items if i)
                    fixes.append(f"[{lang}] removed prestige claim from skills.{cat}")
                elif isinstance(val, list) and any(claim_re.search(str(i)) for i in val):
                    skills[cat] = [i for i in val if not claim_re.search(str(i))]
                    fixes.append(f"[{lang}] removed prestige claim from skills.{cat}")
        for role in r.get("experience") or []:
            if not isinstance(role, dict):
                continue
            bullets = role.get("bullets")
            if isinstance(bullets, list):
                new_bullets = []
                for b in bullets:
                    nb = _scrub_prestige_text(b, claim_re) if isinstance(b, str) else b
                    if isinstance(b, str) and nb != b:
                        fixes.append(f"[{lang}] scrubbed prestige claim from a bullet")
                    if isinstance(nb, str) and not nb.strip():
                        continue
                    new_bullets.append(nb)
                role["bullets"] = new_bullets
            for fld in ("stack_line", "subtitle"):
                _scrub_field(role, fld, lang)
    for ak, lang in (("about_me_en", "EN"), ("about_me_pl", "PL")):
        if isinstance(content.get(ak), str):
            _scrub_field(content, ak, lang)

    return content, fixes


# ---------------------------------------------------------------------------
# Skills slash-gloss dedup — "Performance Optimization / Performance optimisation"
# ---------------------------------------------------------------------------
# The ATS rewrite loop mirrors the posting's phrasing of a skill the base CV
# already lists, and the LLM keeps BOTH joined by " / " instead of picking one
# (observed: "technical documentation / High-quality technical documentation",
# "Performance Optimization / Performance optimisation" — US vs UK spelling).
# Genuinely different skills sharing a slash ("OpenShift / container platforms")
# are kept; only near-duplicate sides are collapsed (keep the first side — the
# base-CV phrasing).

_GLOSS_STOPWORDS = frozenset(
    {
        "and",
        "or",
        "of",
        "the",
        "a",
        "an",
        "in",
        "with",
        "by",
        "to",
        "high-quality",
        "i",
        "oraz",
        "z",
        "w",
        "do",
        "na",
    }
)


def _split_skill_items(value: str) -> list[str]:
    """Split a comma-separated skills string into items, ignoring commas inside
    parentheses ("Agile (Scrum, SAFe)" stays one item)."""
    items: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            items.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    items.append("".join(cur))
    return [i for i in (it.strip() for it in items) if i]


_PL_DIACRITIC_FOLD = str.maketrans("ąćęłńóśźż", "acelnoszz")


def _gloss_stem(token: str) -> str:
    """Crude stem so "validation"/"validating", UK/US spellings and
    with/without-diacritics Polish variants compare equal."""
    t = token.lower().translate(_PL_DIACRITIC_FOLD).replace("isation", "ization")
    for suf in ("ations", "ation", "ing", "ies", "ied", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            t = t[: -len(suf)]
            break
    if t.endswith("at") and len(t) - 2 >= 4:
        t = t[:-2]
    return t


def _gloss_tokens(side: str) -> frozenset[str]:
    words = re.findall(r"[\w+#.-]+", side.lower())
    return frozenset(_gloss_stem(w) for w in words if w not in _GLOSS_STOPWORDS)


def _sides_are_gloss(a: str, b: str) -> bool:
    """True when the two slash sides describe the same skill."""
    ta, tb = _gloss_tokens(a), _gloss_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb or ta <= tb or tb <= ta:
        return True
    jaccard = len(ta & tb) / len(ta | tb)
    return jaccard >= 0.6


def _collapse_gloss_item(item: str) -> str:
    """Collapse "A / B" (spaced slash) when the sides are near-duplicates.
    Compact slashes (UI/UX, CI/CD) are untouched."""
    sides = [s.strip() for s in item.split(" / ") if s.strip()]
    if len(sides) < 2:
        return item
    kept: list[str] = [sides[0]]
    for side in sides[1:]:
        if not any(_sides_are_gloss(side, k) for k in kept):
            kept.append(side)
    return " / ".join(kept)


def _dedup_skill_glosses(content: dict) -> tuple[dict, list[str]]:
    """Collapse "term / synonym" gloss pairs inside every skills category of
    resume_en and resume_pl. Returns (content, list_of_fixes)."""
    fixes: list[str] = []
    for rk, lang in (("resume_en", "EN"), ("resume_pl", "PL")):
        r = content.get(rk)
        skills = r.get("skills") if isinstance(r, dict) else None
        if not isinstance(skills, dict):
            continue
        for cat, val in list(skills.items()):
            if cat == "languages":
                continue
            if isinstance(val, str) and " / " in val:
                items = _split_skill_items(val)
                new_items = [_collapse_gloss_item(i) for i in items]
                new = ", ".join(new_items)
                if new != val:
                    skills[cat] = new
                    fixes.append(f"[{lang}] collapsed gloss pair(s) in skills.{cat}")
            elif isinstance(val, list):
                new_list = [_collapse_gloss_item(i) if isinstance(i, str) else i for i in val]
                if new_list != val:
                    skills[cat] = new_list
                    fixes.append(f"[{lang}] collapsed gloss pair(s) in skills.{cat}")
    return content, fixes
