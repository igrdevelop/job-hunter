"""
hunter/rate_limiter.py — Per-domain async rate limiting for outbound job fetches.

Shared by `expired_marker` (full unsent re-check) and `gmail_enricher` (stub
enrichment). Limits concurrent requests *per host* and enforces a per-host delay
between requests, so a burst of URLs pointing at the same Cloudflare-protected board
(e.g. pracuj.pl) does not trigger HTTP 429 Too Many Requests.

A domain may be given stricter limits than the default via `overrides`, keyed by a
hostname substring (e.g. "pracuj.pl"). This lets the caller throttle one aggressive
host hard while keeping the rest fast.
"""

import asyncio
import logging
from typing import Callable, Optional, TypeVar
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

T = TypeVar("T")


def domain_of(url: str) -> str:
    """Return the hostname of a URL, falling back to the raw string."""
    return urlparse(url).hostname or url


class DomainLimiter:
    """Global + per-domain concurrency cap with a per-domain inter-request delay.

    Usage::

        global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)
        limiter = DomainLimiter(2, 1.0, overrides={"pracuj.pl": (1, 2.5)})
        text = await limiter.fetch(url, global_sem, fetch_fn, timeout=35.0)

    The per-domain semaphore caps how many requests hit one host at once; the
    per-domain delay runs *outside* the semaphores so a slot frees immediately while
    the next request to that host still waits out the cooldown.
    """

    def __init__(
        self,
        domain_limit: int,
        domain_delay: float,
        overrides: Optional[dict[str, tuple[int, float]]] = None,
    ) -> None:
        self._limit = domain_limit
        self._delay = domain_delay
        self._overrides = overrides or {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._delays: dict[str, asyncio.Lock] = {}
        self._domain_delay: dict[str, float] = {}

    def _config_for(self, dom: str) -> tuple[int, float]:
        for needle, cfg in self._overrides.items():
            if needle in dom:
                return cfg
        return (self._limit, self._delay)

    def _get(self, dom: str) -> tuple[asyncio.Semaphore, asyncio.Lock, float]:
        if dom not in self._sems:
            limit, delay = self._config_for(dom)
            self._sems[dom] = asyncio.Semaphore(limit)
            self._delays[dom] = asyncio.Lock()
            self._domain_delay[dom] = delay
        return self._sems[dom], self._delays[dom], self._domain_delay[dom]

    async def fetch(
        self,
        url: str,
        global_sem: asyncio.Semaphore,
        fetch_fn: Callable[[str], T],
        timeout: Optional[float] = None,
    ) -> T:
        """Run `fetch_fn(url)` (in a thread) under global + per-domain caps.

        `fetch_fn` is a blocking callable taking the URL and returning any value
        (job text, an enriched Job, etc.). `timeout` (seconds) bounds a single
        call; None disables the bound. The per-domain key is derived from `url`.
        """
        dom = domain_of(url)
        dom_sem, dom_lock, delay = self._get(dom)
        async with global_sem:
            async with dom_sem:
                coro = asyncio.to_thread(fetch_fn, url)
                result = await (
                    asyncio.wait_for(coro, timeout=timeout) if timeout else coro
                )
        # Rate-limit delay runs outside semaphores so slots are freed immediately.
        if delay > 0:
            async with dom_lock:
                await asyncio.sleep(delay)
        return result
