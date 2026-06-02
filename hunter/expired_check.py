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
        r"\bjob\s+(?:is\s+)?(?:no\s+longer\s+)?available\b",
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
        # Polish — NoFluffJobs / JustJoin "offer not found / not available"
        r"\bta\s+oferta\s+nie\s+jest\s+już\s+dostępna\b",
        r"\boferta\s+pracy\s+nie\s+została\s+odnaleziona\b",
        r"\boferta\s+nie\s+jest\s+już\s+dostępna\b",
        # English — generic 404 / "not found" page content
        # Require a page/job/position object after "find" to avoid matching job-description prose
        r"\bwe\s+(?:didn.t|could\s+not|couldn.t)\s+find\s+(?:the\s+|this\s+)?(?:page|job|position|offer|listing|posting|what\s+you\s+were\s+looking\s+for)\b",
        r"\bpage\s+(?:not\s+found|does\s+not\s+exist|no\s+longer\s+exists)\b",
        r"\b404\b.{0,30}\bnot\s+found\b",
        # SmartRecruiters deactivated form
        r"\brequested\s+application\s+form\s+is\s+inactive\b",
        # Workable / Greenhouse / generic ATS — job closed
        r"\bthis\s+job\s+(?:is\s+)?(?:has\s+been\s+)?closed\b",
        r"\bthis\s+position\s+is\s+(?:now\s+)?closed\b",
        r"\bsorry[,.]?\s+this\s+(?:job|position|role)\s+(?:is\s+)?no\s+longer\s+available\b",
    )
)

# Raw HTML markers — checked directly on page HTML (before text extraction).
# Keys are domain substrings; values are case-insensitive substrings to search for.
HTML_EXPIRED_MARKERS: dict[str, tuple[str, ...]] = {
    "pracuj.pl": (
        'data-apply-type="ArchivedApplyPanel"',
        'data-test="section-archived"',
        "Pracodawca zakończy",          # prefix — avoids diacritic encoding edge cases
        "zakończył zbieranie zgłosze",
        "oferta wygasła",
        '"isActive":false',             # __NEXT_DATA__ JSON field — most reliable
        '"isActive": false',            # space variant
    ),
    "linkedin.com": (
        "No longer accepting applications",
        "no-longer-accepting",
    ),
    "nofluffjobs.com": (
        "This offer is no longer available",
        "oferta nie jest już dostępna",
        "ta oferta nie jest już dostępna",
        "oferta pracy nie została odnaleziona",
    ),
    "justjoin.it": (
        '"isExpired":true',
        '"expired":true',
        "This job offer has expired",
    ),
    "smartrecruiters.com": (
        # Shown when the job posting has been deactivated by the recruiter
        "Hey, requested application form is inactive",
        "this job is no longer accepting applications",
        "job is no longer active",
    ),
    "theprotocol.it": (
        # Dehydrated state signals offer ended
        '"isActive":false',
        '"isActive": false',
        "oferta jest nieaktywna",
        "ta oferta wygasła",
    ),
    "greenhouse.io": (
        "This job has been closed",
        "this position has been filled",
        "Job Closed",
    ),
    "lever.co": (
        "This job posting is no longer available",
        "No longer accepting applications",
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
