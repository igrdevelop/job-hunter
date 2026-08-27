"""
hunter/pipeline/errors.py — apply-pipeline exceptions, exit codes, and
transient-failure classification. Moved out of hunter/apply_shared.py
(docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) — see hunter.apply_shared
for the backward-compat re-export.
"""

from __future__ import annotations

# Exit code: JobLeads fetch blocked — MANUAL tracker row + stub job_posting.txt written
APPLY_MANUAL_EXIT_CODE = 44

# Exit code: fetch hit a transient rate limit (HTTP 429). The caller should retry
# later WITHOUT escalating the permanent fail counter — the offer is likely fine.
APPLY_RATE_LIMITED_EXIT_CODE = 45

# Exit code: LLM account-level outage (drained balance / bad key — llm_client.
# LLMOutageError). Global state, not the vacancy's fault: the caller must stop
# the batch immediately, write NO tracker row and never escalate fail_count
# (docs/LLM_OUTAGE_RESILIENCE_PLAN.md M1).
APPLY_LLM_OUTAGE_EXIT_CODE = 46

# Placeholder URL used when user pastes job text into Telegram without any link.
PASTE_NO_URL_PLACEHOLDER = "paste://no-url"


def is_rate_limit_error(exc: Exception) -> bool:
    """True if an exception represents an HTTP 429 / rate-limit response.

    Checks a requests/cloudscraper-style ``exc.response.status_code`` first, then
    falls back to scanning the message for a 429 / "too many requests" signal.
    """
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg


# Hosts behind anti-bot / CDN protection where a 403 / blocked fetch means
# "blocked right now, retry later" rather than a permanent failure. Treating their
# blocks as transient keeps the job in the retry queue instead of escalating to a
# permanent "gave up" FAIL row that pollutes the tracker.
_ANTIBOT_HOSTS = ("pracuj.pl", "linkedin.com", "theprotocol.it")


def is_transient_fetch_error(exc: Exception, url: str = "") -> bool:
    """True for fetch failures that are transient anti-bot blocks (retry later),
    not permanent failures.

    Covers HTTP 429 everywhere (``is_rate_limit_error``), plus 403 / Cloudflare /
    cloudscraper blocks on known anti-bot hosts (``_ANTIBOT_HOSTS``). A generic 403
    on an arbitrary host is NOT treated as transient (it may be a genuinely gone
    page) — only blocks on hosts we know front their listings with anti-bot CDNs.
    """
    if is_rate_limit_error(exc):
        return True
    from urllib.parse import urlparse

    msg = str(exc).lower()
    host = (urlparse(url).hostname or "").lower() if url else ""
    on_antibot = any(h in host for h in _ANTIBOT_HOSTS) or any(h in msg for h in _ANTIBOT_HOSTS)
    return bool(
        on_antibot
        and ("403" in msg or "forbidden" in msg or "cloudscraper" in msg or "cloudflare" in msg)
    )


class ApplyError(RuntimeError):
    """Raised when an apply attempt fails and fallback should be tried."""
