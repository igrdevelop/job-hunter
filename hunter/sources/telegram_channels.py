"""Telegram job channels — public `t.me/s/{channel}` preview, no auth.

Mechanism inspired by https://github.com/strelov1/freehire, but we copy only
the transport (plain HTTP GET on the public web preview, no MTProto/Bot API/
login) and the parse idea (each post is a `.tgme_widget_message` block). We do
NOT copy freehire's channel list (a live probe flipped it — see
docs/TELEGRAM_CHANNELS_SOURCE_PLAN.md §1.2) or its LLM extraction step.

Channels are owner-curated in `telegram_channels.json` (repo root), loaded via
`hunter.config.TELEGRAM_CHANNELS_FILE`. Each entry: `{"channel": "...",
"kind": "board"|"authored"}` — "board" channels post one vacancy per message
(every post is a candidate); "authored" channels are editorial digests where
the hiring-signal prefilter (§2.5 of the plan) still applies.

`job.url` (see plan §2.1): the post's first outbound external link when
present (cleaned via `html_fallback.clean_url` — lets a vacancy that later
also appears via another board dedup correctly, and routes through the normal
`fetch_job_text` dispatcher). Falls back to the post's own stable permalink
`https://t.me/{channel}/{msg_id}` for self-contained text posts — our own
`fetch_text()` serves those via the single-post embed page. The permalink is
always kept in `job.raw["permalink"]` (also `job.raw["tg_permalink"]`) purely
for convenience — `hunter/main.py::_auto_apply_all` already surfaces
`raw["permalink"]` generically in the pre-apply Telegram notification.

Do NOT set `job.raw["post_text"]` — that key triggers the scout-relay paste
flow (`hunter/services/apply_service.py`), which this source does not need:
every job here has a real fetchable URL (external link or our own embed
fetch), so retries/expiry-checks work through the normal machinery.

Title synthesis (plan §2.4) matters because the central filter
(`hunter.filters.classify_job`) checks `job.title` only, and these posts have
no title field: `title` = first non-empty text line (90-char cap), with the
matched prefilter keyword appended if it isn't already in that line — keeps
the central whitelist honest without bypassing it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.sources.html_fallback import clean_url

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pl;q=0.8,ru;q=0.7",
}
TIMEOUT = 15
MAX_BODY_BYTES = 8 * 1024 * 1024  # freehire's own cap; a runaway page is not a channel

# ── Prefilter patterns (vendored, EN/PL/RU — deliberately NOT imported from
# linkedin_scout, which is leaving the repo per docs/SCOUT_REPO_SPLIT_PLAN.md) ──

HIRING_SIGNAL_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhiring\b",
        r"we[’']?re\s+looking\s+for",
        r"\blooking\s+for\s+a\b",
        r"\bopen\s+role\b",
        r"\bopen\s+position\b",
        r"\bvacanc\w*\b",
        r"\bjoin\s+(?:our|the)\s+team\b",
        r"#hiring\b",
        r"\bszukamy\b",
        r"\bposzukujemy\b",
        r"\bzatrudnimy\b",
        r"\bищем\b",
        r"\bтребуется\b",
        r"\bвакансия\b",
        r"\bнабираем\b",
        r"#вакансия\b",
    )
)

# Candidate-side negatives — people announcing THEY seek work, not a hiring post.
# \bszukam\b (singular) deliberately does NOT match "szukamy" (plural, hiring) —
# see linkedin_scout/heuristics.py, same load-bearing distinction.
CANDIDATE_SIDE_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bopen\s+to\s+work\b",
        r"\bszukam\b\s+pracy",
        r"#opentowork\b",
        r"\bищу\s+работу\b",
        r"\bв\s+поиске\s+работы\b",
    )
)

# Course/webinar/mentorship lead-gen spam — common noise on RU recruiting channels.
SPAM_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcourse\b",
        r"\bwebinar\b",
        r"\bbootcamp\b",
        r"\bszkolenie\b",
        r"\bkurs\w*\b",
        r"\bкурс\w*\b",
        r"\bвебинар\b",
        r"\bбуткемп\b",
        r"\bментор\w*\b",
        r"\bсобеседовани\w*\b",  # "interview practice" event posts, not a vacancy
    )
)

_REMOTE_TOKEN_RE = re.compile(
    r"\bremote\b|\bzdaln\w*|удал[её]нн?\w*|дистанцион\w*",
    re.IGNORECASE,
)

# Telegram wraps a photo attachment's caption in an anchor with empty/zero-width
# text pointing at a telegra.ph page — never a job link (seen in real fixtures).
_PHOTO_WRAPPER_HOSTS = {"telegra.ph"}


@dataclass
class TgPost:
    msg_id: int
    channel: str
    permalink: str
    text: str
    links: list[str] = field(default_factory=list)
    has_text: bool = False


def _is_job_link_candidate(href: str) -> bool:
    href = (href or "").strip()
    if not href.lower().startswith(("http://", "https://")):
        return False
    host = (urlparse(href).netloc or "").lower()
    if host in ("t.me", "telegram.me", "www.t.me", "www.telegram.me"):
        return False
    return not (host in _PHOTO_WRAPPER_HOSTS and "/file/" in urlparse(href).path)


def _extract_text_and_links(text_div) -> tuple[str, list[str]]:
    import html as html_module

    for br in text_div.find_all("br"):
        br.replace_with("\n")
    links = [
        # M4 live finding: some channels' raw HTML double-encodes query-string
        # ampersands ("&amp;amp;") — BeautifulSoup only unescapes once, leaving
        # a literal "&amp;" in the href. A second unescape is a no-op on a
        # normally-encoded href and fixes the double-encoded ones.
        html_module.unescape(href)
        for a in text_div.find_all("a")
        if _is_job_link_candidate(href := (a.get("href") or ""))
    ]
    text = text_div.get_text()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n[ \t]*(\n[ \t]*)+", "\n\n", text)
    return text.strip(), links


def _parse_posts(html: str, channel: str) -> list[TgPost]:
    """Parse every `.tgme_widget_message` block on a channel or embed page."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    posts: list[TgPost] = []
    for msg in soup.find_all("div", class_="tgme_widget_message"):
        data_post = msg.get("data-post") or ""
        ch, _, msg_id_str = data_post.rpartition("/")
        if not ch or not msg_id_str.isdigit():
            continue
        permalink = f"https://t.me/{ch}/{msg_id_str}"
        # M4 live finding: a "pinned"/deleted-message service post carries the
        # `service_message` class AND a tgme_widget_message_text div (e.g.
        # "<Author> pinned Deleted message") — real but useless content that
        # would otherwise synthesize a garbage job title.
        classes = msg.get("class") or []
        text_div = (
            None
            if "service_message" in classes
            else msg.find("div", class_="tgme_widget_message_text")
        )
        if text_div is None:
            # Media-only / service message (e.g. "pinned", deleted) — no body text.
            posts.append(TgPost(int(msg_id_str), ch, permalink, "", [], has_text=False))
            continue
        text, links = _extract_text_and_links(text_div)
        posts.append(TgPost(int(msg_id_str), ch, permalink, text, links, has_text=bool(text)))
    return posts


def _matched_title_keyword(text: str) -> str | None:
    low = text.lower()
    for kw in FILTER.get("title_keywords", []):
        if kw.lower() in low:
            return kw
    return None


def _first_nonempty_line(text: str) -> str:
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def synthesize_title(text: str, matched_kw: str | None = None) -> str:
    """First non-empty text line, 90-char cap; appends the matched keyword
    if the prefilter matched on a keyword absent from that line — keeps the
    title-only central filter honest without bypassing it (plan §2.4)."""
    if matched_kw is None:
        matched_kw = _matched_title_keyword(text)
    first_line = _first_nonempty_line(text)[:90].strip()
    if matched_kw and matched_kw.lower() not in first_line.lower():
        first_line = f"{first_line} · {matched_kw}" if first_line else matched_kw
    return first_line or "Telegram post"


# The `@` must sit at line/text start or after whitespace — NOT mid-token, so a
# URL path like `teletype.in/@courierus/7ZGWxSxMZZ7` (the `@` follows `/`) is not
# mistaken for a " @ Company" mention (real bug 2026-07-12: the URL path became
# the tracker Company). The captured name also excludes `/` so it can never
# swallow a URL path segment.
_COMPANY_AT_RE = re.compile(r"(?:^|(?<=\s))@\s*([A-Za-z0-9][^\n@/]{1,60}?)(?=\s{2,}|\n|$)")


def guess_company(text: str, channel: str) -> str:
    """Best-effort ` @ Company` extraction; else the channel name.

    The LLM extracts the real company at generation time (same contract as
    gmail stubs) — deliberately not over-engineered here.
    """
    m = _COMPANY_AT_RE.search(text)
    if m:
        candidate = m.group(1).strip(" \t.,;:|")
        if candidate and re.search(r"[A-Za-zЀ-ӿ]", candidate):
            return candidate[:60]
    return f"@{channel}"


def guess_location(text: str) -> str:
    return "Remote" if _REMOTE_TOKEN_RE.search(text) else ""


def passes_prefilter(text: str, kind: str) -> bool:
    """Cheap noise reduction before central filters/doomed gate (plan §2.5)."""
    if not text or not text.strip():
        return False
    t = text.lower()
    for pat in FILTER.get("exclude_patterns", []):
        if re.search(pat, t, re.IGNORECASE):
            return False
    keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
    if not any(kw in t for kw in keywords):
        return False
    if any(p.search(text) for p in CANDIDATE_SIDE_RES):
        return False
    if any(p.search(text) for p in SPAM_RES):
        return False
    return not (kind == "authored" and not any(p.search(text) for p in HIRING_SIGNAL_RES))


def _load_channels() -> list[dict]:
    from hunter.config import TELEGRAM_CHANNELS_FILE

    path = Path(TELEGRAM_CHANNELS_FILE)
    if not path.exists():
        logger.warning("[telegram_channels] channel list not found: %s", path)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("[telegram_channels] failed to read channel list: %s", e)
        return []
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict) and c.get("channel")]


def _fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
    resp.raise_for_status()
    content = resp.raw.read(MAX_BODY_BYTES + 1, decode_content=True)
    return content[:MAX_BODY_BYTES].decode("utf-8", errors="replace")


def _parse_permalink(url: str) -> tuple[str, str]:
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] == "s":
        parts = parts[1:]
    if len(parts) < 2 or not parts[1].isdigit():
        raise ValueError(f"Not a Telegram post permalink: {url}")
    return parts[0], parts[1]


def build_job(post: TgPost, kind: str, source_name: str) -> Job:
    matched_kw = _matched_title_keyword(post.text)
    title = synthesize_title(post.text, matched_kw)
    company = guess_company(post.text, post.channel)
    location = guess_location(post.text)
    external = post.links[0] if post.links else None
    url = clean_url(external) if external else post.permalink
    return Job(
        title=title,
        company=company,
        location=location,
        salary=None,
        url=url,
        source=source_name,
        raw={
            "permalink": post.permalink,
            "tg_permalink": post.permalink,
            "channel": post.channel,
            "kind": kind,
        },
    )


class TelegramChannelsSource(BaseSource):
    name = "telegram_channels"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host in ("t.me", "telegram.me")

    def fetch_text(self, url: str) -> str:
        channel, msg_id = _parse_permalink(url)
        embed_url = f"https://t.me/{channel}/{msg_id}?embed=1&mode=tme"
        html = _fetch(embed_url)
        posts = _parse_posts(html, channel)
        if not posts or not posts[0].has_text:
            raise ValueError(f"Telegram post {url} is empty or deleted")
        return posts[0].text

    def search(self) -> list[Job]:
        jobs: list[Job] = []
        from hunter.config import TELEGRAM_CHANNELS_DELAY_SEC

        channels = _load_channels()
        for i, cfg in enumerate(channels):
            channel = cfg["channel"]
            kind = cfg.get("kind", "board")
            try:
                html = _fetch(f"https://t.me/s/{channel}")
            except Exception as e:
                logger.warning("[telegram_channels] %s: fetch failed: %s", channel, e)
                continue
            try:
                posts = _parse_posts(html, channel)
            except Exception as e:
                logger.warning("[telegram_channels] %s: parse failed: %s", channel, e)
                continue
            for post in posts:
                if not post.has_text:
                    continue
                if not passes_prefilter(post.text, kind):
                    continue
                jobs.append(build_job(post, kind, self.name))
            if i < len(channels) - 1 and TELEGRAM_CHANNELS_DELAY_SEC > 0:
                time.sleep(TELEGRAM_CHANNELS_DELAY_SEC)
        return jobs
