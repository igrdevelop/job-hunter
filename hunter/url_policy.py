"""hunter/url_policy.py — SSRF guard for user-supplied URLs.

docs/improvement-2026-09/05-SECURITY_PLAN.md finding #6 / M5: user-supplied
URLs (a Telegram paste/URL message, a `linkedin_scout_relay`/gmail/telegram-
channel outbound link, a re-post gate donor) eventually reach
`requests.get()` in `hunter/sources/html_fallback.py` with no scheme/host
check — a URL like `http://169.254.169.254/latest/meta-data` or
`http://10.0.0.1/` would be fetched from inside the bot container and its
response handed to the LLM (and, on the CLI path, to an agent with WebFetch).

``validate_public_url()`` is the single guard applied at the ENTRY points for
untrusted input — `hunter.sources.fetch_job_text(url, use_session=True)` (the
apply pipeline's own fetch, the point that actually spends LLM budget on
whatever text comes back) and the Telegram URL-message handler
(`hunter/commands/url_message.py::cmd_url`, so a private-address URL is
refused with a short reply instead of spawning an apply subprocess at all).
It is deliberately NOT wired into every individual scraper's `fetch_text()` —
those hit hardcoded, known API hosts (`nofluffjobs.com`, `api.lever.co`, …)
constructed by the source itself, not the raw user URL, so validating there
would only add per-call DNS-resolution cost for zero security benefit.

The other bypass this closes is a REDIRECT off an already-validated public
host onto a private one — `hunter.sources.html_fallback.fetch_html()` (the
generic HTML fallback, the one fetcher that ever calls an arbitrary,
non-hardcoded host) disables `requests`' automatic redirect-following and
manually follows up to `MAX_REDIRECTS` hops, revalidating every `Location`
with this same function before it is fetched.

DNS resolution is done via `socket.getaddrinfo` (mockable in tests via
`monkeypatch.setattr(url_policy.socket, "getaddrinfo", ...)`) and checked
with the stdlib `ipaddress` module against loopback / RFC1918 private /
link-local (169.254.0.0/16, fe80::/10) / IPv6 unique-local (fc00::/7) /
reserved / multicast / unspecified ranges. An IP-literal host that resolves
to a disallowed address always fails closed. A HOSTNAME whose DNS lookup
itself fails (NXDOMAIN, resolver timeout, offline test sandbox) does NOT
raise here — that failure is indistinguishable from an ordinary flaky/typo'd
job-board URL, and the normal fetch call downstream will raise its own
(clearer) network error when it can't connect. Failing open on THAT specific
case only (never on a resolved private address, and never on a malformed IP
literal) is what keeps every normal job URL behaving byte-for-byte as before.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = {"http", "https"}

# Hostnames that are never a legitimate public job-board address regardless
# of what they resolve to (or whether they resolve at all).
_BLOCKED_HOST_EXACT = {"localhost", "localhost.localdomain"}
_BLOCKED_HOST_SUFFIXES = (".internal", ".local", ".localhost")

# requests' own default is effectively unbounded via `max_redirects=30`; we
# fetch known job-posting pages, which never legitimately chain more than a
# couple of hops.
MAX_REDIRECTS = 3


class UrlPolicyError(ValueError):
    """Raised by `validate_public_url` when a URL fails the SSRF policy.

    Subclasses ValueError so existing `except Exception` / `except ValueError`
    handling around fetch calls (e.g. `hunter.apply_api`'s Step 1 fetch
    try/except) needs no changes to also catch this.
    """


def _is_disallowed_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """True for loopback / private / link-local / reserved / multicast /
    unspecified — everything that isn't a routable public address.

    Covers RFC1918 (10/8, 172.16/12, 192.168/16), 169.254.0.0/16 link-local
    (the cloud metadata endpoint), IPv6 fe80::/10 link-local, IPv6 fc00::/7
    unique-local, and loopback for both families via the stdlib's own
    `is_private`/`is_loopback`/`is_link_local`/`is_reserved` classification.
    """
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_host_ips(host: str) -> list:
    """Resolve `host` to every address `getaddrinfo` returns.

    An IP-literal host is checked directly (no DNS round-trip) and a
    disallowed literal raises immediately. A genuine DNS failure for a
    hostname returns `[]` (see module docstring) instead of raising — the
    caller then has nothing to flag as unsafe, and the real fetch downstream
    reports the connection failure on its own.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass  # not an IP literal — resolve as a hostname below

    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, socket.timeout, OSError) as e:
        logger.debug("[url_policy] DNS resolution failed for %r: %s", host, e)
        return []

    resolved = []
    for info in infos:
        raw_ip = info[4][0]
        try:
            # Strip an IPv6 zone id (fe80::1%eth0) before parsing.
            resolved.append(ipaddress.ip_address(raw_ip.split("%", 1)[0]))
        except ValueError:
            continue
    return resolved


def validate_public_url(url: str) -> str:
    """Raise `UrlPolicyError` unless `url` is a plain http(s) URL whose host
    resolves to a public, routable address. Returns `url` unchanged on
    success so a call site can do ``url = validate_public_url(url)`` or just
    call it for its side effect (raise-on-reject).
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError(f"URL scheme not allowed: {scheme or '(none)'!r} in {url!r}")

    host = (parsed.hostname or "").lower()
    if not host:
        raise UrlPolicyError(f"URL has no host: {url!r}")

    if host in _BLOCKED_HOST_EXACT or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise UrlPolicyError(f"URL host is not a public address: {host!r}")

    ips = _resolve_host_ips(host)
    for ip in ips:
        if _is_disallowed_ip(ip):
            raise UrlPolicyError(f"URL host resolves to a disallowed address: {host!r} -> {ip}")

    return url
