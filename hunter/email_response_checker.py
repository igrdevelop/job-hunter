"""
email_response_checker.py — detect application-confirmation emails in Gmail
and match them against tracker rows.

Supported platforms (sender domain → parser):
  linkedin.com    — "You applied to {title} at {company}"
  pracuj.pl       — "Potwierdzenie aplikacji na stanowisko {title} w {company}"
  nofluffjobs.com — "Application sent to {company} - {title}"
  justjoin.it     — "Dziękujemy za aplikację na stanowisko {title} w {company}"

Only called when gmail_token.json exists (GMAIL_ENABLED is not required —
the checker is useful independently of the job-alert scraping).
"""

import base64
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from hunter.gmail_client import get_gmail_service

logger = logging.getLogger(__name__)

# Sender domains we query for confirmation emails
_CONFIRMATION_SENDERS = [
    "linkedin.com",
    "pracuj.pl",
    "nofluffjobs.com",
    "justjoin.it",
]

# Subject substrings that indicate a confirmation (case-insensitive)
_CONFIRMATION_SUBJECTS = [
    "you applied",
    "application was sent",
    "your application was sent",
    "application submitted",
    "application sent",
    "aplikacja została wysłana",
    "twoja aplikacja",
    "potwierdzenie aplikacji",
    "dziękujemy za aplikację",
]


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ConfirmationEmail:
    company: str        # extracted company name; may be "" if parsing failed
    title: str          # extracted job title; may be ""
    date: str           # "YYYY-MM-DD" (UTC)
    subject: str        # raw email subject
    platform: str       # "linkedin" | "pracuj" | "nofluffjobs" | "justjoin" | "unknown"


@dataclass
class MatchResult:
    email: ConfirmationEmail
    match_type: str                         # "exact" | "fuzzy" | "ambiguous" | "no_match"
    candidates: list[dict] = field(default_factory=list)   # rows from lookup_by_company_and_title
    row_num: int | None = None              # set for exact/fuzzy matches


# ── Per-platform subject/body parsers ─────────────────────────────────────────

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
    # Fallback: scan body for "Stanowisko: X" and "Firma: Y"
    if body_text:
        title_m = re.search(r"stanowisko[:\s]+(.+)", body_text, re.IGNORECASE)
        company_m = re.search(r"(?:firma|pracodawca)[:\s]+(.+)", body_text, re.IGNORECASE)
        title = title_m.group(1).strip() if title_m else ""
        company = company_m.group(1).strip() if company_m else ""
        return company, title
    return "", ""


def _parse_nofluffjobs(subject: str, body_text: str) -> tuple[str, str]:
    # "Application sent to Acme Corp - Senior Angular Developer"
    m = re.search(r"(?:application sent|aplikacja wysłana) to (.+?) [-–] (.+)", subject, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # "Application sent to Acme Corp"
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


_PLATFORM_PARSERS: dict[str, tuple[str, callable]] = {
    "linkedin.com":    ("linkedin",    _parse_linkedin),
    "pracuj.pl":       ("pracuj",      _parse_pracuj),
    "nofluffjobs.com": ("nofluffjobs", _parse_nofluffjobs),
    "justjoin.it":     ("justjoin",    _parse_justjoin),
}


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
    sender = headers.get("From", "").lower()

    if not _is_confirmation_subject(subject):
        return None

    body_text = _extract_body_text(msg["payload"])
    date_str = _message_date(msg)

    for domain, (platform, parser_fn) in _PLATFORM_PARSERS.items():
        if domain in sender:
            try:
                company, title = parser_fn(subject, body_text)
            except Exception as exc:
                logger.debug(f"[email_response] parser error ({domain}): {exc}")
                company, title = "", ""
            return ConfirmationEmail(
                company=company.strip(),
                title=title.strip(),
                date=date_str,
                subject=subject,
                platform=platform,
            )

    # Sender matched a confirmation subject but no known platform
    return ConfirmationEmail(
        company="", title="", date=date_str, subject=subject, platform="unknown"
    )


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_confirmation_emails(service, lookback_days: int = 7) -> list[ConfirmationEmail]:
    """Query Gmail for confirmation emails and return parsed results.

    May return items where company/title are empty (parsing failed).
    """
    after_ts = int(
        (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    )
    sender_filter = " OR ".join(f"from:{d}" for d in _CONFIRMATION_SENDERS)
    query = f"({sender_filter}) after:{after_ts}"

    try:
        results = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=200)
            .execute()
        )
    except Exception as exc:
        logger.error(f"[email_response] Gmail list error: {exc}")
        return []

    stubs = results.get("messages", [])
    logger.info(f"[email_response] {len(stubs)} candidate emails in last {lookback_days}d")

    emails: list[ConfirmationEmail] = []
    for stub in stubs:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="full")
                .execute()
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
      "no_match"  — company not found or no title candidates above threshold
    """
    from hunter.tracker import lookup_by_company_and_title

    if not email.company:
        return MatchResult(email=email, match_type="no_match")

    # When title is empty use threshold 0.0 to get all rows for this company
    threshold = 0.4 if email.title else 0.0
    candidates = lookup_by_company_and_title(email.company, email.title, title_min_score=threshold)

    if not candidates:
        return MatchResult(email=email, match_type="no_match")

    # No title available → single company row is a fuzzy match
    if not email.title:
        if len(candidates) == 1:
            return MatchResult(
                email=email, match_type="fuzzy",
                candidates=candidates, row_num=candidates[0]["row"],
            )
        return MatchResult(email=email, match_type="ambiguous", candidates=candidates)

    top = candidates[0]
    # Ambiguous: runner-up is close to top
    if len(candidates) > 1 and candidates[1]["title_score"] >= top["title_score"] * 0.85:
        return MatchResult(email=email, match_type="ambiguous", candidates=candidates)

    match_type = "exact" if top["title_score"] == 1.0 else "fuzzy"
    return MatchResult(
        email=email, match_type=match_type,
        candidates=candidates, row_num=top["row"],
    )


# ── Public entry point ────────────────────────────────────────────────────────

def run_confirmation_check(lookback_days: int | None = None) -> list[MatchResult]:
    """Fetch confirmation emails, match against tracker, write CONFIRMED for clear matches.

    Returns all MatchResult items so caller can report ambiguous/unmatched ones.
    Raises FileNotFoundError if gmail_token.json is missing.
    """
    from hunter.config import EMAIL_RESPONSE_LOOKBACK_DAYS as _DEFAULT_DAYS
    from hunter.tracker import set_response

    days = lookback_days if lookback_days is not None else _DEFAULT_DAYS

    service = get_gmail_service()
    emails = fetch_confirmation_emails(service, days)

    results: list[MatchResult] = []
    for email in emails:
        result = match_email(email)
        if result.match_type in ("exact", "fuzzy") and result.row_num is not None:
            existing = result.candidates[0].get("response", "")
            if not existing:
                set_response(result.row_num, "CONFIRMED")
                logger.info(
                    f"[email_response] CONFIRMED row {result.row_num}: "
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
