import re

# Patterns that indicate a job offer is no longer active.
# Checked against the raw fetched text — case-insensitive, dotall.
EXPIRED_PATTERNS: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in (
        # English
        r"\boffer\s+expired\b",
        r"\bhas\s+expired\b",                          # "Job offer X has expired"
        r"\bthis\s+(?:job\s+)?(?:offer|posting|position)\s+(?:has\s+)?expired\b",
        r"\bjob\s+(?:no\s+longer\s+)?available\b",
        r"\bposition\s+(?:has\s+been\s+)?filled\b",
        r"\bapplication\s+(?:period\s+)?(?:has\s+)?closed\b",
        r"\bno\s+longer\s+accepting\s+applications\b",
        r"\bapplications?\s+(?:are\s+)?(?:now\s+)?closed\b",
        r"\bthis\s+(?:job\s+)?(?:listing|role|position)\s+(?:is\s+)?(?:no\s+longer\s+)?(?:active|available)\b",
        # Polish
        r"\boferta\b.{0,40}\bwygasła\b",
        r"\bwygasła\b.{0,40}\boferta\b",
        r"\bta\s+oferta\s+(?:pracy\s+)?wygasła\b",
        r"\boferta\s+pracy\b.{0,80}\bwygasła\b",
        r"\bogloszenie\s+wygaslo\b",
        r"\boferta\s+jest\s+nieaktywna\b",
        r"\boferta\s+zostala\s+zakonczona\b",
        r"\bpracodawca\s+zakończył\s+zbieranie\s+zgłoszeń\b",
        r"\bzakończył\s+zbieranie\s+zgłoszeń\b",
        r"\bzgłoszenia\s+(?:na\s+tę\s+ofertę\s+)?(?:zostały\s+)?zamknięte\b",
    )
)


def is_job_expired(text: str) -> bool:
    """Return True if the job text contains expiry indicators."""
    if not text:
        return False
    for pattern in EXPIRED_PATTERNS:
        if pattern.search(text):
            return True
    return False
