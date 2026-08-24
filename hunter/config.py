import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Multi-user ────────────────────────────────────────────────────────────────
# Owner's user id (matches users.id in the API's app.sqlite). Required for B1
# so every tracker write is stamped and dedup is scoped correctly. Until Phase
# B3 (full multi-user runtime), this is the only user the bot knows about.
# Leave unset only in single-user dev setups; the bot degrades gracefully
# (stamps user_id='' everywhere, still functions for one user).
DEFAULT_USER_ID: str = os.getenv("DEFAULT_USER_ID", "")

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

# ── Outreach draft after each successful apply (issue #138) ──────────────────
# Writes outreach.md (recruiter contact parsed from the posting + a ≤300-char
# ready-to-paste LinkedIn message) into the application folder next to the CV.
# Best-effort, one JUDGE_MODEL call; the bot never sends anything itself.
OUTREACH_ENABLED: bool = os.getenv("OUTREACH_ENABLED", "true").lower() in ("true", "1", "yes")

# ── PL/EN translation calls (docs/LLM_COST_REDUCTION_PLAN.md M5) ─────────────
# _translate_resume / _translate_plain (hunter.apply_shared) do mechanical
# PL<->EN translation — a Haiku-tier task, not a $15/M-output Sonnet one. The
# result is already guarded by the caller (role-count guard + a repeat
# language-gate scan), so a cheaper model is safe here. Defaults to the same
# model as the judge (Haiku) via the same resolve chain; falls back to the
# main LLM profile (never a translation failure) if no key resolves.
TRANSLATE_PROVIDER: str = os.getenv("TRANSLATE_PROVIDER", "anthropic")
TRANSLATE_MODEL: str = os.getenv("TRANSLATE_MODEL", JUDGE_MODEL)
TRANSLATE_API_KEY: str = (
    os.getenv("TRANSLATE_API_KEY", "")
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
# own feedback (up to ATS_VERDICT_MAX_REFINES escalating rounds — rounds 1-3
# honest visibility passes, round 4+ stretch: openly adds posting tech absent
# from the profile, tracked in To Learn), re-render, and re-verdict, keeping
# only strict improvements. 0 = disabled (byte-for-byte the old one-shot
# behaviour); 5 (default) = honest ×3 + stretch ×2 (owner decision 2026-08-10:
# prod now serves refine calls through the flat-cost CLI subscription, so
# extra rounds are ~free, and CLI-served runs were landing well short of
# ATS_VERDICT_TARGET; supersedes the 3-round default of 2026-07-07).
# See docs/VERDICT_REFINE_PLAN.md.
ATS_VERDICT_TARGET: float = float(os.getenv("ATS_VERDICT_TARGET", "95"))
ATS_VERDICT_MAX_REFINES: int = int(os.getenv("ATS_VERDICT_MAX_REFINES", "5"))

# ── Doomed-vacancy gate (docs/DOOMED_GATE_PLAN.md) ───────────────────────────
# Deterministic (regex-only, zero LLM cost) full-text screen run right after
# expired-check, before the first LLM call in both pipelines
# (`hunter.apply_shared.run_doomed_gate` → `hunter.filters.assess_job_text`).
# HARD findings (high precision — non-Poland onsite/hybrid, non-EU work
# authorization, unsupported required language) write a SKIP tracker row and
# abort generation for $0.00; SOFT findings (e.g. stack mismatch) just warn in
# Telegram and generation continues. Force-mode/manual-paste always degrades
# HARD to warn (the owner explicitly asked to generate this one).
DOOMED_GATE_ENABLED: bool = os.getenv("DOOMED_GATE_ENABLED", "true").lower() in ("true", "1", "yes")
# "skip" (default) aborts generation on a HARD finding; "warn" is an emergency
# lever to downgrade every HARD finding to a warning without disabling the
# gate entirely, e.g. if live-data precision turns out worse than calibration.
DOOMED_GATE_HARD_ACTION: str = os.getenv("DOOMED_GATE_HARD_ACTION", "skip").strip().lower()

# Re-post gate (hunter/repost_gate.py, Step 1.5g): when a freshly fetched
# posting is a near-verbatim re-post of a vacancy applied to in the last
# REPOST_WINDOW_DAYS days (new URL — re-listed after expiry, cross-board
# duplicate, agency re-post under a name variation), REUSE the existing CV:
# copy the donor folder's docs, write a Re-application tracker row at $0,
# skip generation entirely. Thresholds live in repost_gate.py (calibrated
# 2026-07-20 on the real corpus). `/force` bypasses the gate.
# ── Stack pre-screen (docs/STACK_PRESCREEN_PLAN.md M4) ───────────────────────
# One cheap-model call after the free deterministic gates and before the first
# generation call, describing which framework the posting is actually for. It
# exists because `is_react_only_job_text` is blind by contract to a react-first
# posting that mentions Angular in passing: over the seven August postings that
# reached generation on a React stack it would have caught zero.
PRESCREEN_ENABLED: bool = os.getenv("PRESCREEN_ENABLED", "true").lower() in ("true", "1", "yes")
# report -> log only · warn -> + Telegram · skip -> SKIP row and no generation.
# Starts at `report`: the calibration is offline evidence, and a week of live
# verdicts alongside real outcomes is what earns the flip (owner decision
# 2026-08-24 -- a week of `warn`, then `skip`).
PRESCREEN_MODE: str = os.getenv("PRESCREEN_MODE", "report").strip().lower()
# Every skip in the 81-posting calibration scored >= 0.95, so this floor costs
# nothing today and refuses a shakier verdict tomorrow.
PRESCREEN_MIN_CONFIDENCE: float = float(os.getenv("PRESCREEN_MIN_CONFIDENCE", "0.9"))

REPOST_GATE_ENABLED: bool = os.getenv("REPOST_GATE_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
REPOST_WINDOW_DAYS: int = int(os.getenv("REPOST_WINDOW_DAYS", "60"))

# ── Resume generation ─────────────────────────────────────────────────────────
GENERATE_PL_RESUME: bool = os.getenv("GENERATE_PL_RESUME", "false").lower() in ("true", "1", "yes")
GENERATE_ABOUT_ME_PL: bool = os.getenv("GENERATE_ABOUT_ME_PL", "true").lower() in (
    "true",
    "1",
    "yes",
)
# Skip generating the _pl fields (resume_pl/cover_letter_pl/about_me_pl) on the
# FIRST generation call for an English-language posting in short mode — they
# are ~40-50% of that call's output tokens and short mode never delivers them
# for an EN posting anyway (see hunter.apply_shared.compute_output_folder /
# generate_docs short-mode routing). A PL posting (primary_lang == "PL") or a
# full-mode run (--full) is unaffected and always gets the full bilingual set.
# docs/LLM_COST_REDUCTION_PLAN.md M4.
GEN_SKIP_PL_FOR_EN: bool = os.getenv("GEN_SKIP_PL_FOR_EN", "true").lower() in ("true", "1", "yes")
# GDPR/RODO consent clause appended at the bottom of the CV body (not in a footer,
# so ATS parsers still read it). "both" = PL + EN CVs, "pl" = PL CV only, "none" = off.
CV_GDPR_CLAUSE: str = os.getenv("CV_GDPR_CLAUSE", "both").strip().lower()

# ── Resilience ────────────────────────────────────────────────────────────────
APPLY_DELAY_SEC: int = int(os.getenv("APPLY_DELAY_SEC", "30"))
MAX_JOBS_PER_RUN: int = int(os.getenv("MAX_JOBS_PER_RUN", "40"))
APPLY_AGENT_TIMEOUT_SEC: int = int(os.getenv("APPLY_AGENT_TIMEOUT_SEC", "900"))
# Wall-clock cap when the run may go through the Claude CLI: explicit CLI mode
# (APPLY_USE_CLI) or a CLI login present (the outage fallback can fire mid-run,
# and the parent process cannot know in advance whether it will). A CLI-served
# vacancy spawns ~10-20 sequential `claude -p` calls (M4b) — far past the
# 15-minute API budget; killing it at 900s would turn a slow-but-WORKING
# subscription apply into the very FAIL row the outage work eliminates.
# apply_service picks max(APPLY_AGENT_TIMEOUT_SEC, this) in that case.
# Default 10800 (was 2700, then 5400): the 5-round refine loop alone can burn
# 5 × (rewrite ≤600s + re-render + re-verdict ≤600s) ≈ 110 min of CLI calls
# on top of generation (per-call cap llm_client.CLI_CALL_TIMEOUT_SEC=600).
# Owner decision 2026-08-10: "время есть, пускай ковыряется" — a slow
# subscription-served run is fine. The trade-off — a genuinely hung run holds
# _hunt_lock up to 3 h instead of 15 min — is bounded by the FIFO hunt queue
# (waiting slots run late, never skip).
APPLY_AGENT_CLI_TIMEOUT_SEC: int = int(os.getenv("APPLY_AGENT_CLI_TIMEOUT_SEC", "10800"))
# Hard wall-clock cap for the detached dual-apply shadow run (its own budget,
# independent of the primary's APPLY_AGENT_TIMEOUT_SEC). A watchdog force-exits
# the detached shadow process after this many seconds. Default 3600 (was 1800,
# before that 900): the shadow mirrors the full boevoy pipeline incl. the
# verdict refine loop (up to ATS_VERDICT_MAX_REFINES rewrite+render+re-verdict
# rounds — 5 since 2026-08-10, was 3), which can legitimately push a slow
# OpenRouter model past the old budget.
DUAL_SHADOW_TIMEOUT_SEC: int = int(os.getenv("DUAL_SHADOW_TIMEOUT_SEC", "3600"))
CLI_MAX_RETRIES: int = int(os.getenv("CLI_MAX_RETRIES", "5"))
CLI_RETRY_DELAY: int = int(os.getenv("CLI_RETRY_DELAY", "60"))

# Hunt / apply split (docs/HUNT_APPLY_SPLIT_PLAN.md M1): feature-gated so the
# old same-loop behavior is the default. When true, the hunt loop writes new
# jobs to a PENDING queue in tracker.db (ats_status='PENDING') instead of
# applying inline under _hunt_lock, and a separate apply_worker_loop drains
# that queue on its own schedule — a long apply batch no longer blocks hunts.
APPLY_QUEUE_ENABLED: bool = os.getenv("APPLY_QUEUE_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
# A PENDING row claimed (ats_status -> IN_PROGRESS, claimed_at stamped) but
# never resolved within this many minutes means the worker that claimed it
# crashed/was killed — the periodic stale-claim sweep resets it back to
# PENDING so it isn't stuck forever.
APPLY_CLAIM_TIMEOUT_MIN: int = int(os.getenv("APPLY_CLAIM_TIMEOUT_MIN", "60"))

# ── Scraper health monitoring ─────────────────────────────────────────────────
# Track per-source raw yield per hunt run; alert when a source that used to
# produce jobs goes dry for N consecutive runs (broken selector / renamed field).
SOURCE_HEALTH_ENABLED: bool = os.getenv("SOURCE_HEALTH_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
# Consecutive zero/error runs (for a previously-working source) before alerting.
SOURCE_HEALTH_ALERT_STREAK: int = int(os.getenv("SOURCE_HEALTH_ALERT_STREAK", "3"))
# Rows retained per source (ring buffer; older runs pruned).
SOURCE_HEALTH_KEEP: int = int(os.getenv("SOURCE_HEALTH_KEEP", "50"))

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).parent.parent
TRACKER_PATH = PROJECT_DIR / "tracker.xlsx"
# Env-overridable so Docker can point at a DIRECTORY-mounted db (shared WAL
# sidecars with job-hunter-api; a single-file bind mount gives each container
# its own -wal/-shm, which diverges and corrupts the db — 2026-08-07 incident).
TRACKER_DB_PATH: Path = Path(
    os.getenv("TRACKER_DB_PATH", str(PROJECT_DIR / "tracker.db"))
).expanduser()
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
# Per-user storage root (multi-user, docs/MULTI_USER_UPDATE.md shared
# contract): users/{userId}/{candidate,Applications,templates}. Shared with
# job-hunter-api via the ./users volume mount.
USERS_ROOT: Path = Path(os.getenv("USERS_ROOT", str(PROJECT_DIR / "users")))
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
# Socket-level timeout (seconds) on the httplib2.Http underlying the Drive
# service. Without this, a hung read can block a worker thread — and the
# shared TLS socket it holds — indefinitely (docs/GDRIVE_SSL_RACE_PLAN.md M2).
GDRIVE_HTTP_TIMEOUT_SEC: int = int(os.getenv("GDRIVE_HTTP_TIMEOUT_SEC", "60"))

# ── Search schedule (Warsaw time, 24h format) ─────────────────────────────────
# Base trigger times — each source is offset by SCHEDULE_SOURCE_OFFSET_MIN minutes.
# E.g. with times ["08:00","13:00"] and offset 40 min, 7 sources run at:
#   08:00 / 08:40 / 09:20 / 10:00 / 10:40 / 11:20 / 12:00
#   13:00 / 13:40 / 14:20 / 15:00 / 15:40 / 16:20 / 17:00
#
# Night-weighted since 2026-08-16 (owner: "добавим запусков ночью, а запусков
# с 18 по 00.00 вообще не будем делать"). Two of the four base cycles start
# inside 02:00-08:00, and the old 19:00 base is gone. With APPLY_QUEUE_ENABLED
# the apply worker claims a PENDING row within ~15 s of the hunt that wrote it,
# so the hunt grid IS the generation grid — moving one moves the other.
SCHEDULE_TIMES: list[str] = [
    t.strip()
    for t in os.getenv("SCHEDULE_TIMES", "02:00,05:00,08:00,13:00").split(",")
    if t.strip()
]
SCHEDULE_SOURCE_OFFSET_MIN: int = int(os.getenv("SCHEDULE_SOURCE_OFFSET_MIN", "40"))

# Quiet hours: no hunt slot ever fires inside this window. Dropping a base time
# is NOT enough on its own — a cycle of 25 sources at 40 min spans 16h40m, so
# the tail of an 08:00 or 13:00 cycle used to run deep into the evening and past
# midnight. The grid builder (hunter/schedules/grid.py) walks the per-source
# offsets through ALLOWED minutes only, jumping over this window, which keeps
# every source scheduled (skipping slots would silently starve the sources whose
# index happens to land in the gap) and preserves their relative order.
# Format "HH:MM-HH:MM", may wrap midnight ("22:00-06:00"); an end of "00:00"
# means end-of-day. Empty disables the blackout (pre-2026-08-16 behavior).
SCHEDULE_BLACKOUT: str = os.getenv("SCHEDULE_BLACKOUT", "18:00-00:00")
TIMEZONE = "Europe/Warsaw"

# When to retry FAILed tracker rows (comma-separated HH:MM, same timezone).
# Used to run after EVERY per-source hunt (72×/day) — that kept _hunt_lock busy
# past the 40-min slot spacing. Minutes :45 never collide with the hunt grid,
# which only fires at :00/:20/:40 (base minute 00 + multiples of 40 — the
# blackout jump lands on a segment boundary, which is :00, so it stays on grid).
# Both slots sit inside the night window: a retry runs the same apply pipeline
# as a hunt, and the old 18:45 slot fell squarely in the blackout.
RETRY_FAILED_TIMES: list[str] = [
    t.strip() for t in os.getenv("RETRY_FAILED_TIMES", "02:45,07:45").split(",") if t.strip()
]

# How often the Drive backfill job re-checks for application folders that never
# got their immediate post-apply upload (idempotent; was hardcoded to 3 h).
GDRIVE_UPLOAD_MISSING_INTERVAL_MIN: int = int(os.getenv("GDRIVE_UPLOAD_MISSING_INTERVAL_MIN", "30"))

# How long auto-apply pauses after an LLM account outage (drained balance /
# bad key — llm_client.LLMOutageError → exit 46). Time-boxed, not sticky: after
# it expires the next slot probes with ONE job/API call; still dead → M1 fires
# again and re-arms. A top-up heals the bot without owner action. Manual clear:
# /llm outage clear. See docs/LLM_OUTAGE_RESILIENCE_PLAN.md M2.
LLM_OUTAGE_PAUSE_MIN: int = int(os.getenv("LLM_OUTAGE_PAUSE_MIN", "60"))

# ── Job filters ───────────────────────────────────────────────────────────────
# Values live in hunter/filter_profile.builtin_defaults(); filter_config.py is a
# shim (`FILTER = load_profile()`). Re-imported here so every existing
# `from hunter.config import FILTER` keeps working. See docs/FILTERS_YAML_PLAN.md.
from hunter.filter_config import FILTER  # noqa: E402,F401


# ── Candidate tracks (docs/quality/09-multi-track-react.md) ───────────────────
# Which stacks the candidate is actively applying for. Default is exactly
# today's behavior (Angular-only — React-only vacancies are filtered out at
# three levels: listing filters, apply Step 1.5c pre-LLM check, apply Step 4.5
# post-generation check). Adding "react" turns those three filters into
# no-ops for React-only postings without deleting them — they keep working as
# classifiers/statistics, and `--force` still bypasses them either way.
def _parse_tracks(value: str) -> frozenset[str]:
    """Parse a comma-separated track list; blank/empty always falls back to
    angular-only. Pure function (no env/DB access) so it's directly testable
    without reloading hunter.config (which dozens of other modules import)."""
    parsed = frozenset(t.strip().lower() for t in value.split(",") if t.strip())
    return parsed or frozenset({"angular"})


TRACKS: frozenset[str] = _parse_tracks(os.getenv("CANDIDATE_TRACKS", "angular"))

_TRACKS_DB_KEY = "tracks_enabled"


def active_tracks() -> frozenset[str]:
    """Active candidate tracks for the CURRENT user.

    Resolution (multi-user B3.7): per-user `user_settings` row
    (`tracks_enabled` for current_user_id()) → legacy global `config` KV row
    (pre-B3 data written by /tracks) → CANDIDATE_TRACKS env. DB choices win
    over env so switching via Telegram takes effect without a restart.
    """
    import sqlite3

    uid = current_user_id()
    if uid:
        value = user_setting(uid, _TRACKS_DB_KEY, "")
        if value.strip():
            parsed = _parse_tracks(value)
            if parsed:
                return parsed
    try:
        with sqlite3.connect(TRACKER_DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT value FROM config WHERE key = ?", (_TRACKS_DB_KEY,)
            ).fetchone()
        if row and row[0].strip():
            parsed = frozenset(t.strip().lower() for t in row[0].split(",") if t.strip())
            if parsed:
                return parsed
    except Exception:  # noqa: BLE001 — best-effort, env default always available
        pass
    return TRACKS


def set_active_tracks(tracks: frozenset[str] | set[str]) -> None:
    """Persist a `/tracks` choice for the CURRENT user (wins over
    CANDIDATE_TRACKS until changed). Falls back to the legacy global config
    row only when no user id is configured (single-user dev setup)."""
    import sqlite3

    value = ",".join(sorted(t.strip().lower() for t in tracks if t.strip()))
    uid = current_user_id()
    if uid:
        set_user_setting(uid, _TRACKS_DB_KEY, value)
        return
    with sqlite3.connect(TRACKER_DB_PATH) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_TRACKS_DB_KEY, value),
        )


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
WORKINGNOMADS_ENABLED: bool = os.getenv("WORKINGNOMADS_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

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

# ── FindMyRemote source config ────────────────────────────────────────────────
# Public JSON API (findmyremote.ai/api/jobs); ~21 freshest per query, no auth.
# Also owns detail-page fetch for findmyremote.ai links relayed by the
# findmyremote_frontend Telegram channel (HTML pages are RSC shells / 404).
FINDMYREMOTE_ENABLED: bool = os.getenv("FINDMYREMOTE_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ── Smart Jobs (thesmartjobs.com) source config ──────────────────────────────
# Polish IT job board on the Traffit ATS. Public JSON API (thesmartjobs.com/
# api/jobs/search), no auth/Cloudflare; also owns detail-page fetch for
# thesmartjobs.com links (deleted postings 404 -> clean EXPIRED skip).
THESMARTJOBS_ENABLED: bool = os.getenv("THESMARTJOBS_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

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

# ── LinkedIn Scout relay (standalone scout, external private repo -> bot) ──
# Drains the pending-candidates queue file (fed by /scoutfound commands from
# the standalone LinkedIn posts scout — separate PRIVATE repo, owner's
# desktop, own Task Scheduler cadence) into normal Job cards on the bot's own
# hunt schedule. No scraping happens here — see
# hunter/sources/linkedin_scout_relay.py.
LINKEDIN_SCOUT_RELAY_ENABLED: bool = os.getenv("LINKEDIN_SCOUT_RELAY_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# ── Telegram channels source config ──────────────────────────────────────────
# Reads public t.me/s/{channel} previews (no auth, no MTProto) for an
# owner-curated channel list. See docs/TELEGRAM_CHANNELS_SOURCE_PLAN.md +
# hunter/sources/telegram_channels.py.
TELEGRAM_CHANNELS_ENABLED: bool = os.getenv("TELEGRAM_CHANNELS_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
TELEGRAM_CHANNELS_FILE: Path = Path(
    os.getenv("TELEGRAM_CHANNELS_FILE", str(PROJECT_DIR / "telegram_channels.json"))
)
# Polite pause between per-channel fetches (5 channels x 3 cycles/day is negligible).
TELEGRAM_CHANNELS_DELAY_SEC: float = float(os.getenv("TELEGRAM_CHANNELS_DELAY_SEC", "1.5"))

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
GMAIL_ENRICH_ENABLED: bool = os.getenv("GMAIL_ENRICH_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)
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
GMAIL_LABEL_PROCESSED: bool = os.getenv("GMAIL_LABEL_PROCESSED", "true").lower() in (
    "true",
    "1",
    "yes",
)
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


# ── Per-user settings helpers (Phase B2/B3) ──────────────────────────────────
def current_user_id() -> str:
    """User id the current PROCESS acts for.

    JOB_HUNTER_USER_ID (injected into per-user apply subprocesses by
    hunter.users.user_env) wins over DEFAULT_USER_ID (the owner). Empty
    string = single-user dev setup with no user configured.
    """
    return os.environ.get("JOB_HUNTER_USER_ID") or DEFAULT_USER_ID


def user_setting(user_id: str, key: str, default: str = "") -> str:
    """Read one key from user_settings for user_id; return default if absent.

    Reads directly from the DB on every call — caching per hunt cycle is the
    caller's responsibility if needed. Never raises (DB access is best-effort).
    Not yet wired into flow control; that happens in Phase B3.
    """
    try:
        from hunter.db import get_db

        with get_db(TRACKER_DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM user_settings WHERE user_id=? AND key=? LIMIT 1",
                (user_id, key),
            ).fetchone()
        return row["value"] if row else default
    except Exception:
        return default


def set_user_setting(user_id: str, key: str, value: str) -> None:
    """Upsert one per-user setting (B3.7 — /tracks, /dual live here now).

    Best-effort like user_setting: a DB failure logs and returns rather than
    breaking the calling Telegram command.
    """
    from datetime import datetime, timezone

    try:
        from hunter.db import get_db

        with get_db(TRACKER_DB_PATH) as conn:
            conn.execute(
                "INSERT INTO user_settings (user_id, key, value, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(user_id, key) DO UPDATE SET"
                " value = excluded.value, updated_at = excluded.updated_at",
                (user_id, key, value, datetime.now(timezone.utc).isoformat()),
            )
    except Exception as e:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("set_user_setting(%s, %s) failed: %s", user_id, key, e)
