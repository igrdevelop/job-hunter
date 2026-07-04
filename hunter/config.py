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
LLM_MODEL: str = os.getenv("LLM_MODEL", "claude-sonnet-4-6")
# LLM_API_KEY wins if set; otherwise we accept provider-specific env names so a
# .env can carry keys for several providers simultaneously (needed for phase B
# runtime profile switching — see docs/DEEPSEEK_PROVIDER_PLAN.md).
LLM_API_KEY: str = (
    os.getenv("LLM_API_KEY", "")
    or os.getenv("ANTHROPIC_API_KEY", "")
    or os.getenv("OPENROUTER_API_KEY", "")
    or os.getenv("OPENAI_API_KEY", "")
)
APPLY_USE_CLI: bool = os.getenv("APPLY_USE_CLI", "false").lower() in ("true", "1", "yes")

# ── Claim judge (LLM-as-judge CV verification pass) ──────────────────────────
# A second, cheap model verifies every generated claim against the candidate
# profile + job posting and returns a structured violations list. Runs after the
# deterministic scrubs and before the language gate in both pipelines.
# JUDGE_MODE rollout stages: "report" (write judge_report.json only),
# "warn" (also Telegram-notify on findings), "block" (additionally abort
# delivery when a fabrication survives repair — mirrors the language gate).
JUDGE_ENABLED: bool = os.getenv("JUDGE_ENABLED", "true").lower() in ("true", "1", "yes")
JUDGE_MODEL: str = os.getenv("JUDGE_MODEL", "claude-haiku-4-5-20251001")
JUDGE_MODE: str = os.getenv("JUDGE_MODE", "warn").strip().lower()
JUDGE_MAX_REPAIR_ROUNDS: int = int(os.getenv("JUDGE_MAX_REPAIR_ROUNDS", "1"))
# The judge always uses a cheap Anthropic model (Haiku), independent of the main
# LLM provider. When LLM_PROVIDER=openrouter, the main key is an OpenRouter key
# which doesn't accept Anthropic model IDs — so the judge needs its own provider
# + key. JUDGE_API_KEY reads ANTHROPIC_API_KEY first so a dual-provider .env
# (ANTHROPIC_API_KEY + OPENROUTER_API_KEY) just works without extra config.
JUDGE_PROVIDER: str = os.getenv("JUDGE_PROVIDER", "anthropic")
JUDGE_API_KEY: str = (
    os.getenv("JUDGE_API_KEY", "")
    or os.getenv("ANTHROPIC_API_KEY", "")
    or LLM_API_KEY  # last resort: if only one key is configured
)

# Final independent ATS verdict: after generate_docs, ONE cheap-LLM call
# (JUDGE_MODEL/JUDGE_PROVIDER/JUDGE_API_KEY) scores the text extracted from
# the rendered EN CV PDF — i.e. what a real ATS actually parses — against the
# job posting. Informational only (shown in Telegram, stored on content.json),
# never blocks delivery. The in-loop LLM review it replaces was removed from
# _ats_check_loop.
ATS_VERDICT_ENABLED: bool = os.getenv("ATS_VERDICT_ENABLED", "true").lower() in ("true", "1", "yes")

# Verdict refine loop (hunter.verdict_refine): when the independent verdict
# score is below ATS_VERDICT_TARGET, rewrite resume_en against the verdict's
# own feedback (up to ATS_VERDICT_MAX_REFINES escalating rounds — round 1
# honest, round 2+ stretch), re-render, and re-verdict, keeping only strict
# improvements. 0 = disabled (byte-for-byte the old one-shot behaviour); 1 =
# honest round only. See docs/VERDICT_REFINE_PLAN.md.
ATS_VERDICT_TARGET: float = float(os.getenv("ATS_VERDICT_TARGET", "95"))
ATS_VERDICT_MAX_REFINES: int = int(os.getenv("ATS_VERDICT_MAX_REFINES", "1"))

# ── Resume generation ─────────────────────────────────────────────────────────
GENERATE_PL_RESUME: bool = os.getenv("GENERATE_PL_RESUME", "false").lower() in ("true", "1", "yes")
GENERATE_ABOUT_ME_PL: bool = os.getenv("GENERATE_ABOUT_ME_PL", "true").lower() in ("true", "1", "yes")
# GDPR/RODO consent clause appended at the bottom of the CV body (not in a footer,
# so ATS parsers still read it). "both" = PL + EN CVs, "pl" = PL CV only, "none" = off.
CV_GDPR_CLAUSE: str = os.getenv("CV_GDPR_CLAUSE", "both").strip().lower()

# ── Resilience ────────────────────────────────────────────────────────────────
APPLY_DELAY_SEC: int = int(os.getenv("APPLY_DELAY_SEC", "30"))
MAX_JOBS_PER_RUN: int = int(os.getenv("MAX_JOBS_PER_RUN", "20"))
APPLY_AGENT_TIMEOUT_SEC: int = int(os.getenv("APPLY_AGENT_TIMEOUT_SEC", "900"))
# Hard wall-clock cap for the detached dual-apply shadow run (its own budget,
# independent of the primary's APPLY_AGENT_TIMEOUT_SEC). A watchdog force-exits
# the detached shadow process after this many seconds.
DUAL_SHADOW_TIMEOUT_SEC: int = int(os.getenv("DUAL_SHADOW_TIMEOUT_SEC", "900"))
CLI_MAX_RETRIES: int = int(os.getenv("CLI_MAX_RETRIES", "5"))
CLI_RETRY_DELAY: int = int(os.getenv("CLI_RETRY_DELAY", "60"))

# ── Scraper health monitoring ─────────────────────────────────────────────────
# Track per-source raw yield per hunt run; alert when a source that used to
# produce jobs goes dry for N consecutive runs (broken selector / renamed field).
SOURCE_HEALTH_ENABLED: bool = os.getenv("SOURCE_HEALTH_ENABLED", "true").lower() in (
    "true", "1", "yes",
)
# Consecutive zero/error runs (for a previously-working source) before alerting.
SOURCE_HEALTH_ALERT_STREAK: int = int(os.getenv("SOURCE_HEALTH_ALERT_STREAK", "3"))
# Rows retained per source (ring buffer; older runs pruned).
SOURCE_HEALTH_KEEP: int = int(os.getenv("SOURCE_HEALTH_KEEP", "50"))

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).parent.parent
TRACKER_PATH = PROJECT_DIR / "tracker.xlsx"
TRACKER_DB_PATH = PROJECT_DIR / "tracker.db"
# Daily snapshot of workbook(s) — see hunter/tracker_backup.py and tools/backup_tracker.py
TRACKER_BACKUP_ENABLED: bool = os.getenv("TRACKER_BACKUP_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
TRACKER_BACKUP_DIR: Path = Path(
    os.getenv("TRACKER_BACKUP_DIR", str(PROJECT_DIR / "backups"))
).expanduser()
TRACKER_BACKUP_KEEP_FILES: int = max(0, int(os.getenv("TRACKER_BACKUP_KEEP_FILES", "90")))
TRACKER_BACKUP_TIME: str = os.getenv("TRACKER_BACKUP_TIME", "06:05")
APPLICATIONS_DIR: Path = Path(
    os.getenv("APPLICATIONS_DIR", str(PROJECT_DIR / "Applications"))
).expanduser()
APPLY_AGENT_PATH = PROJECT_DIR / "apply_agent.py"
GENERATE_DOCS_PATH = PROJECT_DIR / "generate_docs.py"
APPLY_MD_PATH = PROJECT_DIR / ".claude" / "commands" / "apply.md"
ATS_COMPANIES_PATH = PROJECT_DIR / "hunter" / "ats_companies.json"

# ── Google Sheets integration ─────────────────────────────────────────────────
GSHEETS_ENABLED: bool = os.getenv("GSHEETS_ENABLED", "false").lower() in ("true", "1", "yes")
# Spreadsheet ID — set after first run (bot creates the sheet and sends you the ID)
GSHEETS_TRACKER_ID: str = os.getenv("GSHEETS_TRACKER_ID", "")
# How often (minutes) to pull Sheets → Excel to pick up user edits
GSHEETS_REFRESH_INTERVAL_MIN: int = int(os.getenv("GSHEETS_REFRESH_INTERVAL_MIN", "30"))
GSHEETS_CREDENTIALS_FILE: "Path" = PROJECT_DIR / "gsheets_credentials.json"
GSHEETS_TOKEN_FILE: "Path" = PROJECT_DIR / "gsheets_token.json"
GSHEETS_STATE_FILE: "Path" = PROJECT_DIR / "gsheets_state.json"

# ── Google Drive integration ──────────────────────────────────────────────────
GDRIVE_ENABLED: bool = os.getenv("GDRIVE_ENABLED", "false").lower() in ("true", "1", "yes")
# Optional: ID of an existing Drive folder to upload into (skips auto-create of root)
GDRIVE_ROOT_FOLDER_ID: str = os.getenv("GDRIVE_ROOT_FOLDER_ID", "")
# Name of the root folder created automatically when GDRIVE_ROOT_FOLDER_ID is not set
GDRIVE_ROOT_FOLDER_NAME: str = os.getenv("GDRIVE_ROOT_FOLDER_NAME", "Job Hunter")

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
        # P-8.1: management / leadership / non-IC roles
        "tech lead",
        "tech-lead",
        "techlead",
        "project lead",
        "engineering manager",
        "head of engineering",
        "vp of engineering",
        "cto",
        # Part-time — not relevant for full-time search
        "part-time",
        "part time",
        "parttime",
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
        # NOTE: trailing \b after "#" never matches ("#" is a non-word char, so
        # there is no word boundary between "#" and the following space). Use a
        # leading boundary only so "C#", "(C#", "C#/Angular" are all caught.
        r"\bc#",
        r"\bphp\b",
        r"\bqa\b",
        r"\bsdet\b",
        r"quality\s+assurance",
        r"test\s+automation",
        # fullstack WITHOUT angular is handled by _is_fullstack_without_angular()
        # in filters.py — we don't put it in exclude_patterns so Angular fullstack passes.
        r"\bbackend\b",
        r"\bback-end\b",
        r"\bvue\b",
        r"\bnuxt\b",
        r"\bmagento\b",
        r"\bruby\b",
        # P-3.3: React Native — mobile-only, not FE web
        r"\breact\s+native\b",
        r"\breact[- ]native\b",
        # P-4.1: eCommerce/CMS platforms — not web-FE stack
        r"\bhyv[äa]\b",           # Hyva (Magento theme) — Finnish spelling variants
        r"\badobe\s+commerce\b",   # Adobe Commerce = Magento rebranded
        r"\bpwa\s+studio\b",       # Magento PWA Studio
        r"\bshopware\b",
        r"\bshopify\b",
        r"\bbigcommerce\b",
        r"\bwoocommerce\b",
        r"\bdrupal\b",
        r"\bwordpress\b",
        r"\bsharepoint\b",
        r"\bsap\b",
        # P-7.1: Salesforce / DevOps / SRE / mobile / test-automation roles
        r"\bsalesforce\b",
        r"\bdevops\b",
        r"\bdev-ops\b",
        r"\bsre\b",                    # Site Reliability Engineer
        r"\bplatform\s+engineer\b",
        r"\bcloud\s+engineer\b",
        r"\binfrastructure\s+engineer\b",
        r"\bandroid\b",
        r"\bios\s+developer\b",
        r"\bswift\s+developer\b",
        r"\bkotlin\s+developer\b",
        r"\bflutter\b",
        r"\bautomation\s+engineer\b",
        r"\btesting\s+engineer\b",
        # P-8.1: management / non-IC roles (regex for mixed-case not caught by exclude_levels)
        r"\btech\s+lead\b",
        r"\bproject\s+lead\b",
        r"\bpart[- ]?time\b",
        # Low-code / non-web-FE platforms and niche roles the candidate skips
        r"\bmendix\b",
        r"\boutsystems\b",
        r"\blow[-\s]?code\b",
        r"\bemail\s+developer\b",
        r"\bui\s+designer\b",
        # AI data-labeling / "AI training" gig roles (not real FE engineering)
        r"\bai\s+train(?:ing|er)\b",
        r"\bai\s+tutor\b",
        r"\bdata\s+annotat\w*\b",
        r"\bdata\s+label(?:l)?ing\b",
    ],

    # Skip jobs that mention React but NOT Angular (React-only roles)
    "exclude_react_without_angular": True,

    # Fullstack policy: a "Full Stack / Fullstack" title with NO Angular is always
    # blocked (handled in filters._is_unwanted_fullstack). When Angular IS present
    # the role is blocked only if it is paired with a *heavy backend* stack below
    # (checked in title AND body). Node/Nuxt are deliberately NOT in this list, so a
    # JS/Node fullstack-with-Angular role still passes (per owner's preference).
    "exclude_fullstack_with_backend": True,
    "fullstack_backend_stacks": [
        r"\bjava\b",
        r"\bspring(?:\s+boot)?\b",
        r"\.net\b",
        r"\basp\.net\b",
        r"\bc#",
        r"\bpython\b",
        r"\bdjango\b",
        r"\bgolang\b",
        r"\bphp\b",
        r"\bruby\s+on\s+rails\b",
    ],

    # Disqualifiers hidden in the job BODY (title looks like clean FE, but the
    # description reveals a stack/platform the candidate doesn't want). Checked
    # against the full job text blob, mirroring the German/contract/relocation gates.
    "exclude_body_disqualifiers": True,
    "body_exclude_patterns": [
        r"\bblazor\b",
        r"\bmendix\b",
        r"\boutsystems\b",
        r"\blow[-\s]?code\b",
        r"\bwordpress\b",
        r"\bdrupal\b",
        r"\bmagento\b",
        r"\bsharepoint\b",
    ],

    # Reject when the BODY couples an on-site / hybrid signal with a city outside the
    # Wrocław area (the listing's location field frequently says "remote"/"Poland"
    # while the description demands N days/week in a Kraków/Warsaw/foreign office).
    "exclude_body_onsite_city": True,

    # Exception to the two location gates above: KEEP a hybrid role that only needs
    # the office ~1 day/week, but ONLY for Warsaw / Kraków (commutable from Wrocław
    # once a week). Detected from the body frequency phrasing. More than 1 day/week,
    # an unspecified frequency, or any other far city → still rejected.
    "allow_weekly_hybrid_warsaw_krakow": True,

    # Reject AI-data-labeling / staffing-mill roles by company name (titles are often
    # clean "Angular Developer" so only the company gives them away — micro1 fronts).
    "exclude_ai_training": True,
    "exclude_companies": [
        "micro1",
        "alignerr",
        "quikhire",
        "hirefeed",
        "mercor",
        "outlier ai",
    ],

    # Drop roles that require German (checked in title + location + raw description-like fields).
    # Set false if you speak German or use boards where this produces false positives.
    "exclude_german_language_required": True,

    # Drop part-time / very short contract roles (checked in full job text, not only title).
    # Catches cases where "part-time" appears in the description but not the job title.
    "exclude_unacceptable_contract": True,

    # Drop jobs that explicitly require relocation outside Poland / outside Wrocław region.
    # Catches "hybrid Helsinki", "relocation to Barcelona required", etc. in the full text.
    "exclude_relocation_required": True,

    # Extra anti-hybrid cities appended to _ANTI_HYBRID_CITIES in filters.py.
    # These are non-Polish cities that appeared as hybrid requirements in the tracker.
    "extra_anti_hybrid_cities": [
        # EU cities outside Poland that appeared in hybrid job descriptions
        "helsinki", "helsingfors",
        "barcelona", "madrid", "lisbon", "lisboa",
        "berlin", "munich", "münchen", "hamburg", "frankfurt",
        "amsterdam", "rotterdam",
        "prague", "brno",
        "bratislava",
        "budapest",
        "bucharest",
        "sofia",
        "zagreb",
        # Cyprus (recruiter posts / XM, GRS — hybrid in Limassol/Nicosia/Larnaca)
        "limassol", "nicosia", "larnaca", "larnaka", "paphos", "pafos",
        # Non-EU / remote-but-actually-not regions
        "islamabad", "karachi", "lahore",   # Pakistan
        "bangalore", "mumbai", "delhi",     # India
        "singapore",
        "dubai", "abu dhabi",
        "hong kong",
        "tokyo",
    ],
}

# ── JustJoin.it source config ────────────────────────────────────────────────
JUSTJOIN_ENABLED: bool = os.getenv("JUSTJOIN_ENABLED", "true").lower() in ("true", "1", "yes")
# Pages per workplaceType (remote/hybrid/office). 1 page = 100 items.
# Default 3 → up to 900 items per type, ~2700 total (pre-filter reduces to ~tens).
JUSTJOIN_MAX_PAGES: int = int(os.getenv("JUSTJOIN_MAX_PAGES", "3"))

# ── NoFluffJobs source config ─────────────────────────────────────────────────
NOFLUFFJOBS_ENABLED: bool = os.getenv("NOFLUFFJOBS_ENABLED", "true").lower() in ("true", "1", "yes")

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

# ── Working Nomads source config ──────────────────────────────────────────────
# Public Elasticsearch index (jobsapi/_search); JSON, no auth.
WORKINGNOMADS_ENABLED: bool = os.getenv("WORKINGNOMADS_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Jobspresso source config ──────────────────────────────────────────────────
# WP Job Manager RSS feed (~10 most recent listings, no pagination).
JOBSPRESSO_ENABLED: bool = os.getenv("JOBSPRESSO_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Built In source config ────────────────────────────────────────────────────
# US/remote tech board behind Cloudflare; DOM scrape via cloudscraper.
BUILTIN_ENABLED: bool = os.getenv("BUILTIN_ENABLED", "true").lower() in ("true", "1", "yes")

# ── JustRemote source config ──────────────────────────────────────────────────
# Public JSON API (justremote-api.herokuapp.com); ~10 newest dev roles, trickle.
JUSTREMOTE_ENABLED: bool = os.getenv("JUSTREMOTE_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Remote OK source config ───────────────────────────────────────────────────
REMOTEOK_ENABLED: bool = os.getenv("REMOTEOK_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Himalayas source config ───────────────────────────────────────────────────
HIMALAYAS_ENABLED: bool = os.getenv("HIMALAYAS_ENABLED", "true").lower() in ("true", "1", "yes")

# ── 4dayweek.io source config ───────────────────────────────────────────────
FOURDAYWEEK_ENABLED: bool = os.getenv("FOURDAYWEEK_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ── We Work Remotely source config ────────────────────────────────────────────
WEWORKREMOTELY_ENABLED: bool = os.getenv("WEWORKREMOTELY_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ── RemoteLeaf source config ─────────────────────────────────────────────────
# HTML listing parser — set false if site layout changes and scraper breaks.
REMOTELEAF_ENABLED: bool = os.getenv("REMOTELEAF_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ── ATS Aggregator source config ─────────────────────────────────────────────
# Reads career pages of companies listed in hunter/ats_companies.json through
# their ATS provider's public JSON API (Workable / Greenhouse / Lever / …).
ATS_AGGREGATOR_ENABLED: bool = os.getenv("ATS_AGGREGATOR_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ── Gmail source config ───────────────────────────────────────────────────────
# Reads job alert emails from LinkedIn, NoFluffJobs, JustJoin, Bulldogjob, Pracuj.
# Requires one-time setup: python tools/gmail_auth.py
GMAIL_ENABLED: bool = os.getenv("GMAIL_ENABLED", "false").lower() in ("true", "1", "yes")
# How far back to scan the inbox for job-alert emails (hours). Slightly over one
# day bridges the gap between scheduled runs; widen if a run can be skipped.
GMAIL_LOOKBACK_HOURS: int = int(os.getenv("GMAIL_LOOKBACK_HOURS", "25"))
# Max alert emails fetched per scan. If a scan hits this ceiling the hunt report
# warns that emails were truncated (raise this if you subscribe to many alerts).
GMAIL_MAX_RESULTS: int = int(os.getenv("GMAIL_MAX_RESULTS", "100"))
# Fetch real title/company/location/salary for each URL extracted from alert emails.
GMAIL_ENRICH_ENABLED: bool = os.getenv("GMAIL_ENRICH_ENABLED", "true").lower() in ("true", "1", "yes")
# Max parallel HTTP requests during enrichment (global cap, across all hosts)
GMAIL_ENRICH_CONCURRENCY: int = int(os.getenv("GMAIL_ENRICH_CONCURRENCY", "5"))
# Per-job HTTP timeout (seconds) for enrichment fetches
GMAIL_ENRICH_TIMEOUT: int = int(os.getenv("GMAIL_ENRICH_TIMEOUT", "15"))
# Default per-host caps during enrichment (avoids hammering one board with a burst).
GMAIL_ENRICH_DOMAIN_LIMIT: int = int(os.getenv("GMAIL_ENRICH_DOMAIN_LIMIT", "2"))
GMAIL_ENRICH_DOMAIN_DELAY: float = float(os.getenv("GMAIL_ENRICH_DOMAIN_DELAY", "0.0"))
# pracuj.pl is Cloudflare-rate-limited: a burst of parallel detail fetches returns
# HTTP 429. Throttle it harder than other hosts (override on top of the defaults).
PRACUJ_HOST_CONCURRENCY: int = int(os.getenv("PRACUJ_HOST_CONCURRENCY", "2"))
PRACUJ_HOST_DELAY_SEC: float = float(os.getenv("PRACUJ_HOST_DELAY_SEC", "1.0"))
# Hosts that systematically hard-block enrichment detail fetches (HTTP 429/403)
# and so are NOT worth enriching during the hunt — fetching them only wastes
# requests and poisons the shared rate budget for everyone else. The Gmail stub
# (title/company parsed from the alert email) is kept instead. LinkedIn 429s
# without a logged-in session (see LINKEDIN_STORAGE_STATE); pracuj Cloudflares.
# Comma-separated host substrings. Remove a host here once it can be fetched
# reliably (e.g. after providing a LinkedIn session).
GMAIL_ENRICH_SKIP_HOSTS: list[str] = [
    h.strip().lower()
    for h in os.getenv("GMAIL_ENRICH_SKIP_HOSTS", "linkedin.com,pracuj.pl").split(",")
    if h.strip()
]

# ── Email response checker ────────────────────────────────────────────────────
# Default look-back window for /check_responses (and the daily scheduled run).
# Pass a larger number directly to the command: /check_responses 60
EMAIL_RESPONSE_LOOKBACK_DAYS: int = int(os.getenv("EMAIL_RESPONSE_LOOKBACK_DAYS", "2"))
# Time of day (Warsaw) for the daily automatic confirmation check
EMAIL_RESPONSE_CHECK_TIME: str = os.getenv("EMAIL_RESPONSE_CHECK_TIME", "09:00")

# ── Expired check schedule ───────────────────────────────────────────────────
EXPIRED_CHECK_TIME: str = os.getenv("EXPIRED_CHECK_TIME", "00:00")

# ── Expired check concurrency ────────────────────────────────────────────────
# Global max parallel requests during /check_expired
EXPIRED_CHECK_CONCURRENCY: int = int(os.getenv("EXPIRED_CHECK_CONCURRENCY", "10"))
# Max simultaneous requests to the same domain
EXPIRED_CHECK_DOMAIN_LIMIT: int = int(os.getenv("EXPIRED_CHECK_DOMAIN_LIMIT", "2"))
# Delay (sec) between requests to the same domain
EXPIRED_CHECK_DOMAIN_DELAY: float = float(os.getenv("EXPIRED_CHECK_DOMAIN_DELAY", "1.0"))
# Hard asyncio-level timeout (sec) per URL fetch — guards against TCP hangs
EXPIRED_CHECK_FETCH_TIMEOUT: float = float(os.getenv("EXPIRED_CHECK_FETCH_TIMEOUT", "35.0"))

# ── LibreOffice ───────────────────────────────────────────────────────────────
SOFFICE_PATH: str = os.getenv(
    "SOFFICE_PATH",
    "libreoffice",  # Linux/Docker default; Windows: set SOFFICE_PATH in .env
)

# ── JustJoin source config ────────────────────────────────────────────────────
JUSTJOIN_MARKER_ICONS = [
    "angular",
    "javascript",
    "html",
]
