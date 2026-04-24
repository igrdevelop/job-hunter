import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: int = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
# After apply_agent success, also send .pdf/.docx via sendDocument (Bot API 50MB/file cap)
TELEGRAM_SEND_DOCS: bool = os.getenv("TELEGRAM_SEND_DOCS", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ── Auto-apply ────────────────────────────────────────────────────────────────
AUTO_APPLY: bool = os.getenv("AUTO_APPLY", "false").lower() in ("true", "1", "yes")

# ── LLM config (used by apply_agent.py in API mode) ──────────────────────────
LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "anthropic")
LLM_MODEL: str = os.getenv("LLM_MODEL", "claude-3-5-haiku-20241022")
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
APPLY_USE_CLI: bool = os.getenv("APPLY_USE_CLI", "false").lower() in ("true", "1", "yes")

# ── Resume generation ─────────────────────────────────────────────────────────
GENERATE_PL_RESUME: bool = os.getenv("GENERATE_PL_RESUME", "false").lower() in ("true", "1", "yes")

# ── Resilience ────────────────────────────────────────────────────────────────
APPLY_DELAY_SEC: int = int(os.getenv("APPLY_DELAY_SEC", "30"))
MAX_JOBS_PER_RUN: int = int(os.getenv("MAX_JOBS_PER_RUN", "10"))
APPLY_AGENT_TIMEOUT_SEC: int = int(os.getenv("APPLY_AGENT_TIMEOUT_SEC", "900"))
CLI_MAX_RETRIES: int = int(os.getenv("CLI_MAX_RETRIES", "3"))
CLI_RETRY_DELAY: int = int(os.getenv("CLI_RETRY_DELAY", "30"))

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).parent.parent
TRACKER_PATH = PROJECT_DIR / "tracker.xlsx"
TO_SEND_PATH = PROJECT_DIR / "to_send.xlsx"
APPLICATIONS_DIR = PROJECT_DIR / "Applications"
APPLY_AGENT_PATH = PROJECT_DIR / "apply_agent.py"
GENERATE_DOCS_PATH = PROJECT_DIR / "generate_docs.py"
APPLY_MD_PATH = PROJECT_DIR / ".claude" / "commands" / "apply.md"

# ── Search schedule (Warsaw time, 24h format) ─────────────────────────────────
# Base trigger times — each source is offset by SCHEDULE_SOURCE_OFFSET_MIN minutes.
# E.g. with times ["08:00","13:00","19:00"] and offset 40 min, 7 sources run at:
#   08:00 / 08:40 / 09:20 / 10:00 / 10:40 / 11:20 / 12:00
#   13:00 / 13:40 / 14:20 / 15:00 / 15:40 / 16:20 / 17:00
#   19:00 / 19:40 / 20:20 / 21:00 / 21:40 / 22:20 / 23:00
SCHEDULE_TIMES = ["08:00", "13:00", "19:00"]
SCHEDULE_SOURCE_OFFSET_MIN: int = int(os.getenv("SCHEDULE_SOURCE_OFFSET_MIN", "40"))
TIMEZONE = "Europe/Warsaw"

# ── Job filters ───────────────────────────────────────────────────────────────
# Angular-only: title must match at least one keyword AND contain "angular"
# (unless it's a generic "frontend"/"typescript" title — require_angular catches those)
FILTER = {
    # Title must contain at least ONE of these (case-insensitive)
    "title_keywords": [
        "angular",
        "frontend",
        "front-end",
        "javascript",
        "typescript",
    ],

    # Angular not required in title — many Angular jobs are titled just "Frontend Developer"
    "require_angular": False,

    "exclude_levels": [
        "junior",
        "intern",
        "internship",
        "trainee",
        "stażysta",
        "praktykant",
        "staz",
    ],

    "locations": [
        # Always accept: fully remote regardless of city
        "remote",
        "zdalnie",
        "zdalna",
        # Accept Wrocław (on-site OR hybrid — hybrid elsewhere is rejected)
        "wrocław",
        "wroclaw",
    ],

    # Title matching ANY regex → skip
    "exclude_patterns": [
        r"\bjava\b",
        r"\.net",
        r"\bc#\b",
        r"\bphp\b",
        r"\bqa\b",
        r"\bsdet\b",
        r"quality\s+assurance",
        r"test\s+automation",
        r"\bfullstack\b",
        r"\bfull-stack\b",
        r"\bfull stack\b",
        r"\bbackend\b",
        r"\bback-end\b",
        r"\bvue\b",
        r"\bnuxt\b",
        r"\bmagento\b",
        r"\bruby\b",
    ],

    # Skip jobs that mention React but NOT Angular (React-only roles)
    "exclude_react_without_angular": True,

    # Drop roles that require German (checked in title + location + raw description-like fields).
    # Set false if you speak German or use boards where this produces false positives.
    "exclude_german_language_required": True,
}

# ── LinkedIn source config ────────────────────────────────────────────────────
LINKEDIN_ENABLED: bool = os.getenv("LINKEDIN_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Bulldogjob source config ──────────────────────────────────────────────────
BULLDOGJOB_ENABLED: bool = os.getenv("BULLDOGJOB_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Pracuj.pl source config ──────────────────────────────────────────────────
PRACUJ_ENABLED: bool = os.getenv("PRACUJ_ENABLED", "true").lower() in ("true", "1", "yes")

# ── theprotocol.it source config ─────────────────────────────────────────────
# Disabled by default: site is a full SPA behind Cloudflare, listing scraper
# cannot extract data without a headless browser. Manual URL fetch still works.
THEPROTOCOL_ENABLED: bool = os.getenv("THEPROTOCOL_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Solid.Jobs source config ─────────────────────────────────────────────────
SOLIDJOBS_ENABLED: bool = os.getenv("SOLIDJOBS_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Inhire.io source config ───────────────────────────────────────────────────
# Requires Playwright: pip install playwright && python -m playwright install chromium
INHIRE_ENABLED: bool = os.getenv("INHIRE_ENABLED", "true").lower() in ("true", "1", "yes")

# ── JobLeads source config ────────────────────────────────────────────────────
# Detail pages are often Cloudflare-blocked; apply_agent then writes MANUAL tracker
# rows + stub job_posting.txt — paste description and re-run apply on the same URL.
JOBLEADS_ENABLED: bool = os.getenv("JOBLEADS_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Arbeitnow source config ───────────────────────────────────────────────────
ARBEITNOW_ENABLED: bool = os.getenv("ARBEITNOW_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Remotive source config ────────────────────────────────────────────────────
REMOTIVE_ENABLED: bool = os.getenv("REMOTIVE_ENABLED", "true").lower() in ("true", "1", "yes")

# ── JustJoin source config ────────────────────────────────────────────────────
JUSTJOIN_MARKER_ICONS = [
    "angular",
    "javascript",
    "html",
]
