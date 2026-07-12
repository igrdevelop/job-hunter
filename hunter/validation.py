"""Input validation helpers for the apply pipeline."""
import re

MIN_JOB_TEXT_LEN = 300  # P-2.2: raised from 200 — real postings are rarely <300 chars

# LinkedIn Scout relay jobs carry a synthetic dedup-key URL (see
# hunter/sources/linkedin_scout_relay.URL_PREFIX — a drift-guard test asserts
# the two stay consistent). Defined here rather than imported: validation must
# stay a leaf module, and tracker.py also needs this marker without pulling in
# the whole hunter.sources package. No trailing slash: normalize_url() strips
# it, and pre-#144 rows in prod are stored as the bare collapsed form.
# Deliberately NOT "linkedin.com/..." — that collided with LinkedInSource.
# matches_url() (hostname-only "linkedin.com" in host check), routing any
# non-paste-text fetch of a scout URL to the real LinkedIn fetcher instead of
# the relay's own "no fetchable URL" error.
SCOUT_POSTS_URL_MARKER = "linkedin-scout.internal/posts"

# Old (pre-fix) prefix — kept only so already-recorded prod rows still match
# the lower text-length floor / retry-exclusion / expired-check-skip logic
# below after this deploy; new rows always use SCOUT_POSTS_URL_MARKER.
_LEGACY_SCOUT_POSTS_URL_MARKER = "linkedin.com/scout-posts"

# Scout feed posts are legitimately short ("We're hiring an Angular dev — DM
# me") and already passed is_hiring_post() heuristics on the owner's desktop;
# the 300-char floor calibrated for scraped board postings would silently
# reject most of them.
MIN_SCOUT_TEXT_LEN = 80

# Telegram channel posts fetched via our own t.me permalink (see
# hunter/sources/telegram_channels.py) are legitimately short board-style
# listings ("Senior Frontend @ Company | Remote | <link>") — same issue as
# scout posts (#143). External-link jobs from this source use the linked
# board's own URL (not t.me), so they keep the normal 300-char floor
# automatically. No trailing slash, same reasoning as SCOUT_POSTS_URL_MARKER.
TELEGRAM_POST_URL_MARKER = "//t.me/"


def min_job_text_len_for(url: str) -> int:
    """Return the too-short floor for this apply: scout/telegram posts get a lower one."""
    u = url or ""
    if (
        SCOUT_POSTS_URL_MARKER in u
        or _LEGACY_SCOUT_POSTS_URL_MARKER in u
        or TELEGRAM_POST_URL_MARKER in u
    ):
        return MIN_SCOUT_TEXT_LEN
    return MIN_JOB_TEXT_LEN

_BOGUS_NAMES: frozenset[str] = frozenset({
    "unknown",
    "unknowncompany",
    "pracujportal",
    "generaljobboard",
    "generaljobposting",
    "generaljobsearch",
    "jobportal",
    "portal",
})


def is_bogus_company(name: str) -> bool:
    """Return True if the LLM-extracted company name is a placeholder or portal name."""
    normalized = re.sub(r"[^a-z0-9]", "", (name or "").lower().strip())
    return not normalized or normalized in _BOGUS_NAMES


def is_job_text_too_short(text: str, min_len: int = MIN_JOB_TEXT_LEN) -> bool:
    """Return True if the fetched job text is too short to be a real posting."""
    return len((text or "").strip()) < min_len
