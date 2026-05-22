import re

# Patterns that indicate a job offer is no longer active.
# Checked against the raw fetched text — case-insensitive, dotall.
EXPIRED_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in (
        # English — generic
        r"\boffer\s+expired\b",
        r"\bhas\s+expired\b",
        r"\bthis\s+(?:job\s+)?(?:offer|posting|position)\s+(?:has\s+)?expired\b",
        r"\bjob\s+(?:no\s+longer\s+)?available\b",
        r"\bposition\s+(?:has\s+been\s+)?filled\b",
        r"\bapplication\s+(?:period\s+)?(?:has\s+)?closed\b",
        r"\bno\s+longer\s+accepting\s+applications\b",
        r"\bno\s+longer\s+accepting\b",                 # shorter LinkedIn variant
        r"\bapplications?\s+(?:are\s+)?(?:now\s+)?closed\b",
        r"\bthis\s+(?:job\s+)?(?:listing|role|position)\s+(?:is\s+)?(?:no\s+longer\s+)?(?:active|available)\b",
        # Polish — generic expiry
        r"\boferta\b.{0,40}\bwygasła\b",
        r"\bwygasła\b.{0,40}\boferta\b",
        r"\bta\s+oferta\s+(?:pracy\s+)?wygasła\b",
        r"\boferta\s+pracy\b.{0,80}\bwygasła\b",
        r"\bogloszenie\s+wygaslo\b",
        r"\boferta\s+jest\s+nieaktywna\b",
        r"\boferta\s+zostala\s+zakonczona\b",
        # Polish — Pracuj.pl archived panel (both \b-bounded and plain substring fallback)
        r"\bpracodawca\s+zakończy[łl]\s+zbieranie\s+zgłosze",
        r"\bzakończy[łl]\s+zbieranie\s+zgłosze",
        r"\bzgłoszenia\s+(?:na\s+tę\s+ofertę\s+)?(?:zostały\s+)?zamknięte\b",
    )
)

# Raw HTML markers — checked directly on page HTML (before text extraction).
# Keys are domain substrings; values are case-insensitive substrings to search for.
HTML_EXPIRED_MARKERS: dict[str, tuple[str, ...]] = {
    "pracuj.pl": (
        'data-apply-type="ArchivedApplyPanel"',
        'data-test="section-archived"',
        "Pracodawca zakończy",      # prefix — avoids diacritic encoding edge cases
        "zakończył zbieranie zgłosze",
        "oferta wygasła",
    ),
    "linkedin.com": (
        "No longer accepting applications",
        "no-longer-accepting",
    ),
    "nofluffjobs.com": (
        "This offer is no longer available",
        "oferta nie jest już dostępna",
    ),
    "justjoin.it": (
        '"isExpired":true',
        '"expired":true',
        "This job offer has expired",
    ),
}


def is_job_expired(text: str) -> bool:
    """Return True if the extracted job text contains expiry indicators."""
    if not text:
        return False
    for pattern in EXPIRED_PATTERNS:
        if pattern.search(text):
            return True
    return False


def is_expired_by_html(html: str, domain: str) -> bool:
    """Return True if raw page HTML contains domain-specific expiry markers."""
    if not html:
        return False
    html_lower = html.lower()
    for key, markers in HTML_EXPIRED_MARKERS.items():
        if key in domain:
            for marker in markers:
                if marker.lower() in html_lower:
                    return True
    return False
