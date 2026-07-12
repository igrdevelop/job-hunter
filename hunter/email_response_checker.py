"""
email_response_checker.py — detect application-confirmation emails in Gmail
and match them against tracker rows.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIRMED REAL-WORLD SOURCES (parsers verified against real emails):

  erecruiter.pl        — Polish ATS (NASK, EXATEL, Nexio, Medicover...)
                         Subject: "{Company} - Dziękujemy za złożenie aplikacji na stanowisko {Title}"
  smartrecruiters.com  — International ATS (Sigma Software...)
                         Subject: "Thank you for applying to {Company}"
                         Body:    "application for the position of {Title}"
  aplikacje.pracuj.pl  — Pracuj.pl status notifications (Hiberus, Devapo, Get It Together...)
                         Subject: "{Title}: pracodawca udziela bezpośrednich informacji."
                         Body:    "logo firmy {COMPANY}"
  thesmartjobs.com     — SmartJobs Smart Tracker (Devapo, Hiberus...)
                         Body:    "stanowisko {Title} w firmie {Company}"
  mailing.theprotocol.it — theprotocol.it (ITEAMLY...)
                         Subject: "Potwierdzenie zgłoszenia - {Company}: {Title}"
  recruitify.ai        — Recruitify ATS
                         Body:    "Position: {Title}"  (no company in email)
  Direct company mail  — Highly variable; caught via subject keywords + _parse_direct.

SPECULATIVE SOURCES (parsers written, real format not yet observed):

  linkedin.com         — "You applied to {Title} at {Company}"
  pracuj.pl            — "Potwierdzenie aplikacji na stanowisko {Title} w {Company}"
  nofluffjobs.com      — "Application sent to {Company} - {Title}"
  justjoin.it          — "Dziękujemy za aplikację na stanowisko {Title} w {Company}"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO ADD A NEW PLATFORM when you receive a confirmation email from it:

  1. Note the sender domain (From header) and exact Subject line.
  2. Add sender domain to _CONFIRMATION_SENDERS.
  3. Add subject keyword(s) to _CONFIRMATION_SUBJECTS and _SUBJECT_QUERY_KEYWORDS.
  4. Write _parse_<platform>(subject, body_text) -> tuple[str, str] returning (company, title).
  5. Wire into _parse_message() elif chain (before the generic "else" branch).
  6. Add tests in tests/test_email_response_checker.py with real subject/body fixtures.

Platforms likely to add next (common Polish/EU ATS not yet observed):
  - traffit.com        — Polish ATS used by many startups
  - teamtailor.com     — Scandinavian ATS, present in Poland
  - bullhorn.com       — International ATS
  - hrlink.pl          — Polish HR platform
  - successfactors.com — SAP ATS (enterprise)
  - taleo.net          — Oracle ATS (enterprise)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from hunter.gmail_client import get_gmail_service

logger = logging.getLogger(__name__)

# Known sender domains — ATS platforms + job boards that may send confirmations
_CONFIRMATION_SENDERS = [
    # Confirmed real-world ATS
    "erecruiter.pl",
    "smartrecruiters.com",
    "workable.com",
    "greenhouse.io",
    "lever.co",
    # Confirmed real-world job board / tracking platforms
    "aplikacje.pracuj.pl",  # Pracuj.pl status notifications
    "thesmartjobs.com",  # SmartJobs Smart Tracker
    "mailing.theprotocol.it",  # theprotocol.it confirmation
    "recruitify.ai",  # Recruitify ATS
    # Speculative (may send per-apply confirmations)
    "linkedin.com",
    "pracuj.pl",
    "nofluffjobs.com",
    "justjoin.it",
]

# Subject phrases that trigger Gmail query (short, for API query string)
_SUBJECT_QUERY_KEYWORDS = [
    "dziękujemy za złożenie aplikacji",
    "thank you for applying",
    "thanks for applying",
    "thank you for submitting",
    # Confirmed real-world patterns
    "pracodawca udziela bezpośrednich informacji",
    "potwierdzenie zgłoszenia",
    "smart tracker",
    "thanks for filling",
]

# Subject substrings for local filtering (broader, case-insensitive)
_CONFIRMATION_SUBJECTS = [
    "dziękujemy za złożenie aplikacji",
    "dziękujemy za zainteresowanie",
    "thank you for applying",
    "thanks for applying",
    "thank you for submitting",
    "thanks for filling",
    "application submitted",
    "application received",
    # Confirmed real-world patterns
    "pracodawca udziela bezpośrednich informacji",  # Pracuj.pl status
    "potwierdzenie zgłoszenia",  # theprotocol.it
    "smart tracker",  # SmartJobs
    # Speculative fallbacks
    "application was sent",
    "application sent",
    "potwierdzenie aplikacji",
]


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class ConfirmationEmail:
    company: str  # extracted company name; may be "" if parsing failed
    title: str  # extracted job title; may be ""
    date: str  # "YYYY-MM-DD" (UTC)
    subject: str  # raw email subject
    platform: str  # "erecruiter" | "smartrecruiters" | "direct" | "unknown"


@dataclass
class MatchResult:
    email: ConfirmationEmail
    match_type: str  # "exact" | "fuzzy" | "ambiguous" | "no_match"
    candidates: list[dict] = field(default_factory=list)
    row_id: str | None = None  # set for exact/fuzzy matches (8-char hex ID)


# ── Per-platform parsers ──────────────────────────────────────────────────────


def _parse_erecruiter(subject: str, body_text: str) -> tuple[str, str]:
    """eRecruiter ATS (mail@stage.erecruiter.pl).

    Subject: "NASK - Dziękujemy za złożenie aplikacji na stanowisko Senior Frontend Developer"
    """
    # Company before the dash, title after "stanowisko"
    m = re.search(
        r"^(.+?)\s*[-–]\s*Dzi[eę]kujemy za z[lł]o[zż]enie aplikacji na stanowisko\s+(.+)",
        subject,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Body fallback: "złożenie aplikacji na stanowisko {Title}"
    title = ""
    if body_text:
        bm = re.search(
            r"z[lł]o[zż]enie aplikacji na stanowisko\s+(.+?)(?:\s+i\s+czas|\.|$)",
            body_text,
            re.IGNORECASE,
        )
        if bm:
            title = bm.group(1).strip()
    return "", title


def _parse_smartrecruiters(subject: str, body_text: str, from_header: str) -> tuple[str, str]:
    """SmartRecruiters ATS (notification@smartrecruiters.com).

    From:    "Sigma Software <notification@smartrecruiters.com>"
    Subject: "Thank you for applying to Sigma Software"
    Body:    "application for the position of Middle Front-End Developer"
    """
    # Company from subject: "Thank you for applying to {Company}"
    company = ""
    m = re.search(r"(?:thank you for applying|thanks for applying) to (.+)", subject, re.IGNORECASE)
    if m:
        company = m.group(1).strip()

    # Company from From display name as fallback: "Acme Corp <notification@...>"
    if not company:
        dm = re.match(r'"?([^"<@]+?)"?\s*<', from_header)
        if dm:
            company = dm.group(1).strip()

    # Title from body: "application for the position of {Title}"
    title = ""
    if body_text:
        bm = re.search(
            r"(?:application for the position of|position of)\s+(.+?)(?:[.\n]|$)",
            body_text,
            re.IGNORECASE,
        )
        if bm:
            title = bm.group(1).strip()

    return company, title


def _parse_direct(subject: str, body_text: str, from_header: str) -> tuple[str, str]:
    """Generic parser for direct company emails.

    Tries to extract title from body using common Polish/English patterns.
    Company extracted from From display name or sender domain.
    """
    # Title from body (Polish)
    title = ""
    if body_text:
        for pattern in (
            r"z[lł]o[zż]enie aplikacji na stanowisko\s+(.+?)(?:\s+i\s+czas|\.|$)",
            r"aplikacj[ię] na stanowisko[:\s]+(.+?)(?:[.\n]|$)",
            r"na stanowisko[:\s]+(.+?)(?:[.\n]|$)",
        ):
            bm = re.search(pattern, body_text, re.IGNORECASE)
            if bm:
                title = bm.group(1).strip()
                break

        # Title from body (English) — stop before auxiliary verb or punctuation
        if not title:
            bm = re.search(
                r"(?:position of|position:)\s+(.+?)(?:\s+(?:is|are|was|will|has|have)\b|[.,\n]|$)",
                body_text,
                re.IGNORECASE,
            )
            if bm:
                title = bm.group(1).strip()

    # Company from From display name: "Zespół HR <recruitment@company.com>"
    company = ""
    dm = re.match(r'"?([^"<@\d][^"<@]*?)"?\s*<', from_header)
    if dm:
        raw = dm.group(1).strip()
        # Skip generic sender names
        if raw.lower() not in (
            "notification",
            "noreply",
            "no-reply",
            "recruiter",
            "hr",
            "recruitment",
            "careers",
            "talent",
        ):
            company = raw

    # Company from domain as last resort: "recruitment@sigma-software.com" → "Sigma Software"
    if not company:
        domain_m = re.search(r"@([\w-]+)\.", from_header)
        if domain_m:
            domain_part = domain_m.group(1).replace("-", " ").replace("_", " ")
            company = domain_part.title()

    return company, title


# ── Confirmed new-platform parsers ───────────────────────────────────────────


def _parse_pracuj_status(subject: str, body_text: str) -> tuple[str, str]:
    """Pracuj.pl status notifications (noreply@aplikacje.pracuj.pl).

    Subject: "Frontend Developer (Angular): pracodawca udziela bezpośrednich informacji."
    Body:    "logo firmy {COMPANY}\n{Title}\n{COMPANY} {City, region}"
    """
    title = ""
    m = re.search(r"^(.+?):\s*pracodawca udziela", subject, re.IGNORECASE)
    if m:
        title = m.group(1).strip()

    company = ""
    if body_text:
        # Company appears right after "logo firmy" label
        cm = re.search(r"logo firmy\s*\n?\s*(.+?)(?:\n|$)", body_text, re.IGNORECASE)
        if cm:
            company = cm.group(1).strip()

    return company, title


def _parse_smartjobs(subject: str, body_text: str) -> tuple[str, str]:
    """SmartJobs Smart Tracker (noreply@thesmartjobs.com).

    Subject: "Twój Smart Tracker dla Senior Angular Developer"
    Body:    "aplikacja na stanowisko {Title} w firmie {Company}"
    """
    # Title + company from body (most reliable)
    if body_text:
        m = re.search(
            r"(?:aplikacj[ię] na stanowisko|stanowisko)\s+(.+?)\s+w firmie\s+(.+?)(?:\.|$|\n)",
            body_text,
            re.IGNORECASE,
        )
        if m:
            return m.group(2).strip(), m.group(1).strip()

    # Title from subject as fallback
    title = ""
    m = re.search(r"Smart Tracker dla\s+(.+)", subject, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
    return "", title


def _parse_theprotocol(subject: str, body_text: str) -> tuple[str, str]:
    """theprotocol.it confirmation (system@mailing.theprotocol.it).

    Subject: "Potwierdzenie zgłoszenia - ITEAMLY SPÓŁKA Z O.O.: Senior Frontend Developer (Angular)"
    """
    m = re.search(
        r"Potwierdzenie zgłoszenia\s*[-–]\s*(.+?):\s+(.+)",
        subject,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", ""


def _parse_recruitify(subject: str, body_text: str) -> tuple[str, str]:
    """Recruitify.ai ATS (system@recruitify.ai).

    Subject: "Thanks for filling up the questionnaire."
    Body:    "Position: Frontend Developer (Angular)"
    Company not included in email.
    """
    title = ""
    if body_text:
        m = re.search(r"Position:\s+(.+?)(?:\n|$)", body_text, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
    return "", title


# ── Speculative job-board parsers (kept for future coverage) ──────────────────


def _parse_linkedin(subject: str, body_text: str) -> tuple[str, str]:
    # "You applied to Senior Angular Developer at Acme Corp"
    m = re.search(r"you applied to (.+?) at (.+)", subject, re.IGNORECASE)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    # "Your application was sent to Acme Corp"
    m = re.search(r"(?:application was sent|application submitted) to (.+)", subject, re.IGNORECASE)
    if m:
        return m.group(1).strip(), ""
    return "", ""


def _parse_pracuj(subject: str, body_text: str) -> tuple[str, str]:
    # "Potwierdzenie aplikacji na stanowisko: Senior Angular Developer w Acme Corp"
    m = re.search(r"stanowisko[:\s]+(.+?)\s+w\s+(.+)", subject, re.IGNORECASE)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    if body_text:
        title_m = re.search(r"stanowisko[:\s]+(.+)", body_text, re.IGNORECASE)
        company_m = re.search(r"(?:firma|pracodawca)[:\s]+(.+)", body_text, re.IGNORECASE)
        return (company_m.group(1).strip() if company_m else ""), (
            title_m.group(1).strip() if title_m else ""
        )
    return "", ""


def _parse_nofluffjobs(subject: str, body_text: str) -> tuple[str, str]:
    # "Application sent to Acme Corp - Senior Angular Developer"
    m = re.search(
        r"(?:application sent|aplikacja wysłana) to (.+?) [-–] (.+)", subject, re.IGNORECASE
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(r"(?:application sent|aplikacja wysłana) to (.+)", subject, re.IGNORECASE)
    if m:
        return m.group(1).strip(), ""
    return "", ""


def _parse_justjoin(subject: str, body_text: str) -> tuple[str, str]:
    # "Dziękujemy za aplikację na stanowisko Senior Angular Developer w Acme Corp"
    m = re.search(r"stanowisko\s+(.+?)\s+w\s+(.+)", subject, re.IGNORECASE)
    if m:
        return m.group(2).strip(), m.group(1).strip()
    return "", ""


# ── Gmail helpers ─────────────────────────────────────────────────────────────


def _extract_body_text(payload: dict) -> str:
    """Recursively extract the first text/plain part from a MIME tree."""
    if "parts" in payload:
        for part in payload["parts"]:
            text = _extract_body_text(part)
            if text:
                return text
    elif payload.get("mimeType") == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return ""


def _is_confirmation_subject(subject: str) -> bool:
    sl = subject.lower()
    return any(kw in sl for kw in _CONFIRMATION_SUBJECTS)


def _message_date(msg: dict) -> str:
    """Return ISO date string (UTC) from Gmail internalDate (milliseconds)."""
    ts_ms = int(msg.get("internalDate", 0))
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _parse_message(msg: dict) -> ConfirmationEmail | None:
    """Parse one Gmail message. Returns ConfirmationEmail or None if not a confirmation."""
    headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
    subject = headers.get("Subject", "")
    from_header = headers.get("From", "")
    sender = from_header.lower()

    if not _is_confirmation_subject(subject):
        return None

    body_text = _extract_body_text(msg["payload"])
    date_str = _message_date(msg)

    if "erecruiter.pl" in sender:
        company, title = _parse_erecruiter(subject, body_text)
        platform = "erecruiter"
    elif "smartrecruiters.com" in sender:
        company, title = _parse_smartrecruiters(subject, body_text, from_header)
        platform = "smartrecruiters"
    elif "aplikacje.pracuj.pl" in sender:
        company, title = _parse_pracuj_status(subject, body_text)
        platform = "pracuj_status"
    elif "thesmartjobs.com" in sender:
        company, title = _parse_smartjobs(subject, body_text)
        platform = "smartjobs"
    elif "mailing.theprotocol.it" in sender:
        company, title = _parse_theprotocol(subject, body_text)
        platform = "theprotocol"
    elif "recruitify.ai" in sender:
        company, title = _parse_recruitify(subject, body_text)
        platform = "recruitify"
    elif "linkedin.com" in sender:
        company, title = _parse_linkedin(subject, body_text)
        platform = "linkedin"
    elif "pracuj.pl" in sender:
        company, title = _parse_pracuj(subject, body_text)
        platform = "pracuj"
    elif "nofluffjobs.com" in sender:
        company, title = _parse_nofluffjobs(subject, body_text)
        platform = "nofluffjobs"
    elif "justjoin.it" in sender:
        company, title = _parse_justjoin(subject, body_text)
        platform = "justjoin"
    else:
        # Direct company email or unknown ATS — generic fallback
        company, title = _parse_direct(subject, body_text, from_header)
        platform = "direct"

    return ConfirmationEmail(
        company=company.strip(),
        title=title.strip(),
        date=date_str,
        subject=subject,
        platform=platform,
    )


# ── Fetch ─────────────────────────────────────────────────────────────────────


def fetch_confirmation_emails(service, lookback_days: int = 7) -> list[ConfirmationEmail]:
    """Query Gmail for confirmation emails and return parsed results.

    Queries by ATS sender domains OR subject keywords to catch both
    ATS-platform emails and direct company confirmation emails.
    May return items where company/title are empty (parsing failed).
    """
    after_ts = int((datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp())
    sender_filter = " OR ".join(f"from:{d}" for d in _CONFIRMATION_SENDERS)
    subject_filter = " OR ".join(f'subject:"{kw}"' for kw in _SUBJECT_QUERY_KEYWORDS)
    query = f"({sender_filter} OR {subject_filter}) after:{after_ts}"

    try:
        results = service.users().messages().list(userId="me", q=query, maxResults=200).execute()
    except Exception as exc:
        logger.error(f"[email_response] Gmail list error: {exc}")
        return []

    stubs = results.get("messages", [])
    logger.info(f"[email_response] {len(stubs)} candidate emails in last {lookback_days}d")

    emails: list[ConfirmationEmail] = []
    for stub in stubs:
        try:
            msg = (
                service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
            )
        except Exception as exc:
            logger.debug(f"[email_response] fetch error for {stub['id']}: {exc}")
            continue
        parsed = _parse_message(msg)
        if parsed is not None:
            emails.append(parsed)

    logger.info(f"[email_response] {len(emails)} confirmation emails parsed")
    return emails


# ── Matching ──────────────────────────────────────────────────────────────────


def match_email(email: ConfirmationEmail) -> MatchResult:
    """Match one ConfirmationEmail against tracker rows.

    match_type semantics:
      "exact"     — single candidate, title_score == 1.0
      "fuzzy"     — single candidate, title_score in [0.4, 1.0)
      "ambiguous" — multiple candidates at similar score; human review needed
      "no_match"  — company not found or no candidates above threshold
    """
    from hunter.tracker import lookup_by_company_and_title

    if not email.company:
        return MatchResult(email=email, match_type="no_match")

    threshold = 0.4 if email.title else 0.0
    candidates = lookup_by_company_and_title(email.company, email.title, title_min_score=threshold)

    if not candidates:
        return MatchResult(email=email, match_type="no_match")

    if not email.title:
        if len(candidates) == 1:
            return MatchResult(
                email=email,
                match_type="fuzzy",
                candidates=candidates,
                row_id=candidates[0]["id"],
            )
        return MatchResult(email=email, match_type="ambiguous", candidates=candidates)

    top = candidates[0]
    if len(candidates) > 1 and candidates[1]["title_score"] >= top["title_score"] * 0.85:
        return MatchResult(email=email, match_type="ambiguous", candidates=candidates)

    match_type = "exact" if top["title_score"] == 1.0 else "fuzzy"
    return MatchResult(
        email=email,
        match_type=match_type,
        candidates=candidates,
        row_id=top["id"],
    )


# ── Public entry point ────────────────────────────────────────────────────────


def run_confirmation_check(lookback_days: int | None = None) -> list[MatchResult]:
    """Fetch confirmation emails, match against tracker, write CONFIRMED for clear matches.

    Returns all MatchResult items so caller can report ambiguous/unmatched ones.
    Raises FileNotFoundError if gmail_token.json is missing.
    """
    from hunter.config import EMAIL_RESPONSE_LOOKBACK_DAYS as _DEFAULT_DAYS
    from hunter.tracker import set_confirmation

    days = lookback_days if lookback_days is not None else _DEFAULT_DAYS

    service = get_gmail_service()
    emails = fetch_confirmation_emails(service, days)

    results: list[MatchResult] = []
    for email in emails:
        result = match_email(email)
        if result.match_type in ("exact", "fuzzy") and result.row_id is not None:
            existing = result.candidates[0].get("confirmation", "")
            if not existing:
                set_confirmation(result.row_id, email.date)
                logger.info(
                    f"[email_response] confirmed row {result.row_id}: "
                    f"{result.candidates[0]['company']} — {result.candidates[0]['title']}"
                )
        results.append(result)

    confirmed = sum(1 for r in results if r.match_type in ("exact", "fuzzy"))
    ambiguous = sum(1 for r in results if r.match_type == "ambiguous")
    logger.info(
        f"[email_response] check complete: {confirmed} confirmed, "
        f"{ambiguous} ambiguous, {len(results) - confirmed - ambiguous} no_match"
    )
    return results
