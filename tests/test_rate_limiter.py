"""Tests for hunter/rate_limiter.py — per-domain concurrency + delay."""

import asyncio
import time

import pytest

from hunter.rate_limiter import DomainLimiter, domain_of


def test_domain_of_extracts_host():
    assert domain_of("https://www.pracuj.pl/praca/x,oferta,1") == "www.pracuj.pl"
    assert domain_of("https://justjoin.it/job-offer/abc") == "justjoin.it"


def test_domain_of_falls_back_to_raw_string():
    assert domain_of("not-a-url") == "not-a-url"


def _make_fetch(active: dict, peak: dict):
    """Build a blocking fetch_fn that records concurrent in-flight count per host."""

    def fetch_fn(url: str) -> str:
        host = domain_of(url)
        active[host] = active.get(host, 0) + 1
        peak[host] = max(peak.get(host, 0), active[host])
        time.sleep(0.02)  # hold the slot so overlap is observable
        active[host] -= 1
        return f"text:{url}"

    return fetch_fn


def test_per_domain_concurrency_cap():
    """No more than `domain_limit` requests to one host run at once."""
    active: dict = {}
    peak: dict = {}
    fetch_fn = _make_fetch(active, peak)

    async def run():
        limiter = DomainLimiter(domain_limit=2, domain_delay=0.0)
        global_sem = asyncio.Semaphore(50)
        urls = [f"https://pracuj.pl/job/{i}" for i in range(10)]
        await asyncio.gather(*[limiter.fetch(u, global_sem, fetch_fn) for u in urls])

    asyncio.run(run())
    assert peak["pracuj.pl"] <= 2


def test_override_throttles_one_host_harder():
    """A host with an override gets its stricter limit; others use the default."""
    active: dict = {}
    peak: dict = {}
    fetch_fn = _make_fetch(active, peak)

    async def run():
        limiter = DomainLimiter(domain_limit=5, domain_delay=0.0, overrides={"pracuj.pl": (1, 0.0)})
        global_sem = asyncio.Semaphore(50)
        urls = [f"https://pracuj.pl/job/{i}" for i in range(6)] + [
            f"https://justjoin.it/job/{i}" for i in range(6)
        ]
        await asyncio.gather(*[limiter.fetch(u, global_sem, fetch_fn) for u in urls])

    asyncio.run(run())
    assert peak["pracuj.pl"] == 1
    assert peak["justjoin.it"] <= 5
    assert peak["justjoin.it"] >= 2  # default allows real parallelism


def test_per_domain_delay_serializes_requests():
    """A nonzero per-domain delay spaces out requests to the same host."""

    async def run():
        delay = 0.05
        limiter = DomainLimiter(domain_limit=1, domain_delay=delay)
        global_sem = asyncio.Semaphore(50)
        urls = [f"https://pracuj.pl/job/{i}" for i in range(3)]
        await asyncio.gather(*[limiter.fetch(u, global_sem, lambda u: "ok") for u in urls])

    start = time.monotonic()
    asyncio.run(run())
    elapsed = time.monotonic() - start
    # 3 requests, limit 1, delay each → at least ~2 delays of serialized wait.
    assert elapsed >= 0.05 * 2


def test_timeout_raises():
    """A fetch exceeding the timeout raises asyncio.TimeoutError."""

    def slow(url: str) -> str:
        time.sleep(0.2)
        return "late"

    async def run():
        limiter = DomainLimiter(domain_limit=1, domain_delay=0.0)
        global_sem = asyncio.Semaphore(1)
        await limiter.fetch("https://pracuj.pl/x", global_sem, slow, timeout=0.01)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(run())


def test_fetch_returns_fetch_fn_result():
    async def run():
        limiter = DomainLimiter(domain_limit=2, domain_delay=0.0)
        global_sem = asyncio.Semaphore(2)
        return await limiter.fetch("https://justjoin.it/job/1", global_sem, lambda u: f"R:{u}")

    assert asyncio.run(run()) == "R:https://justjoin.it/job/1"
