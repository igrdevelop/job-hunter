"""Deterministic recruiter-contact extraction from job posting text (issue #138).

Zero LLM cost: pure regex over the already-saved job_posting.txt. Polish
postings (NoFluff, pracuj, theprotocol) often name the recruiter, and agency
postings (Antal, Devire, ASTEK — the majority of current yield) almost always
do. What this finds feeds `hunter/outreach.py`'s outreach.md; when nothing is
found here, the (separate, LLM+web-search) lookup fallback takes over.

Precision over recall throughout: a wrong name/phone in outreach.md wastes the
owner's LinkedIn message, so every pattern requires an explicit recruiting
label or role line — bare capitalized word pairs are never treated as names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Building blocks ───────────────────────────────────────────────────────────

# One capitalized name token, Polish diacritics included ("Łukasz", "Zofia").
_NAME_TOKEN = r"[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż'’\-]+"
# "Anna Kowalska" / "Anna Maria Kowalska-Nowak". Horizontal whitespace ONLY —
# \s+ would swallow the newline and capture the next line's first word.
_FULL_NAME = rf"{_NAME_TOKEN}(?:[ \t]+{_NAME_TOKEN}){{1,2}}"

# Labels that explicitly introduce a contact person (PL + EN).
_LABELS = (
    r"kontakt",
    r"osoba\s+kontaktowa",
    r"contact(?:\s+person)?",
    r"recruiter",
    r"rekruter(?:ka)?",
    r"hiring\s+manager",
    r"aplikuj\s+do",
    r"cv\s+(?:wyślij|prześlij|send)\s+(?:do|to)",
    r"pytania\s+(?:kieruj\s+)?do",
    r"questions\s+to",
    r"reach\s+out\s+to",
)
# The label match is case-insensitive, but the name itself must keep real
# capitalization — `(?-i:...)` scopes that (otherwise "do"/"and" match as name
# tokens). The label→name gap is horizontal-only: crossing a newline turns
# "…IT Recruiter\nAntal Sp. z o.o." into a false "Antal Sp" contact.
_LABELED_NAME_RE = re.compile(
    r"(?:" + "|".join(_LABELS) + r")[ \t]*[:\-–—]?[ \t]*((?-i:" + _FULL_NAME + r"))",
    re.IGNORECASE,
)

# Signature block: a full name on its own line, the NEXT line a recruiting
# role ("Anna Kowalska\nSenior IT Recruiter"). Common in agency postings.
_ROLE_WORDS = (
    r"recruit",
    r"rekrut",
    r"talent",
    r"sourcing",
    r"people\s",
    r"hr\b",
    r"human\s+resources",
    r"hiring",
    r"employer\s+branding",
)
_SIGNATURE_RE = re.compile(
    r"^[ \t>*•\-]*((?-i:" + _FULL_NAME + r"))[ \t]*$\n"
    r"^[ \t>*•\-]*[^\n]*(?:" + "|".join(_ROLE_WORDS) + r")[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)
# The name group itself must keep its capitalization even under IGNORECASE —
# checked post-match (IGNORECASE is needed for the label/role words only).
_NAME_SHAPE_RE = re.compile(rf"^{_FULL_NAME}$")

_EMAIL_RE = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b")
# Never worth contacting: legal/technical mailboxes.
_EMAIL_SKIP = re.compile(
    r"^(?:no-?reply|noreply|privacy|rodo|gdpr|dpo|unsubscribe|abuse|postmaster)",
    re.IGNORECASE,
)

# Conservative phone matching: must start with "+<country>" OR be preceded by
# an explicit tel/phone label — bare digit groups collide with salary ranges.
_PHONE_RE = re.compile(
    r"(?:(?:tel|phone|kom|mob)\.?\s*[:\-]?\s*)?(\+\d{1,3}[\s\-.]?\d{2,3}[\s\-.]?\d{3}[\s\-.]?\d{2,4})"
    r"|(?:tel|phone|kom|mob)\.?\s*[:\-]?\s*(\d{3}[\s\-.]?\d{3}[\s\-.]?\d{3})",
    re.IGNORECASE,
)

_MAX_CONTACTS = 3
_EVIDENCE_LEN = 120


@dataclass
class Contact:
    """One extracted contact. All fields optional except evidence."""

    name: str = ""
    email: str = ""
    phone: str = ""
    evidence: str = field(default="")  # the matched line, for a sanity check


def _evidence_line(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    if end == -1:
        end = len(text)
    return text[start:end].strip()[:_EVIDENCE_LEN]


def _email_matches_name(email: str, name: str) -> bool:
    """anna.kowalska@agency.pl ↔ 'Anna Kowalska' (diacritics folded)."""
    local = email.split("@", 1)[0].lower()
    fold = str.maketrans("ąćęłńóśźż", "acelnoszz")
    tokens = [t.translate(fold) for t in name.lower().split()]
    return any(len(t) >= 3 and t in local for t in tokens)


def extract_contacts(text: str) -> list[Contact]:
    """Return up to 3 contacts found in the posting text. Deterministic, $0.

    Named contacts (labeled or signature-style) come first, then bare
    recruiting emails. Phones only ever attach to an existing contact —
    a bare phone number alone is too noisy to act on.
    """
    if not text:
        return []

    contacts: list[Contact] = []
    seen_names: set[str] = set()

    for regex in (_LABELED_NAME_RE, _SIGNATURE_RE):
        for m in regex.finditer(text):
            name = " ".join(m.group(1).split())
            # IGNORECASE serves the label words; the name itself must still
            # look like a name ("aplikuj do końca miesiąca" must not match).
            if not _NAME_SHAPE_RE.match(name):
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            contacts.append(Contact(name=name, evidence=_evidence_line(text, m.start(1))))

    emails: list[tuple[str, int]] = []
    seen_emails: set[str] = set()
    for m in _EMAIL_RE.finditer(text):
        email = m.group(0)
        if _EMAIL_SKIP.match(email) or email.lower() in seen_emails:
            continue
        seen_emails.add(email.lower())
        emails.append((email, m.start()))

    # Attach an email to the contact whose name it echoes; leftovers become
    # email-only contacts.
    for email, pos in emails:
        owner = next(
            (c for c in contacts if c.name and not c.email and _email_matches_name(email, c.name)),
            None,
        )
        if owner is not None:
            owner.email = email
        else:
            contacts.append(Contact(email=email, evidence=_evidence_line(text, pos)))

    # First confidently-matched phone attaches to the first contact.
    m = _PHONE_RE.search(text)
    if m and contacts:
        contacts[0].phone = (m.group(1) or m.group(2) or "").strip()

    return contacts[:_MAX_CONTACTS]
