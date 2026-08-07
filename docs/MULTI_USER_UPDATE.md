# Multi-User Update — bot repo work order

Self-contained work order for converting the `job-hunter` Python bot to multi-tenant.
Companion files exist in the sibling repos (`../api/docs/MULTI_USER_UPDATE.md`,
`../site/docs/MULTI_USER_UPDATE.md`) — each repo is worked on by its own agent.
**The "Shared contract" section below is duplicated in all three files and must stay
in sync. Do not change contract details unilaterally — flag mismatches to the user.**

Before starting: check the existing worktree `.claude/worktrees/candidate-yaml-multi-user`
and docs `docs/quality/08-multi-user-configurability.md` (16+ modules with hardcoded
identity), `docs/WEB_APP_PLAN.md:364-398` (earlier per-user sketch),
`docs/CANDIDATE_YAML_PLAN.md`. Reuse what already exists.

## Goal

Full multi-tenant: the API/site handle registration and per-user storage; this repo
makes the bot serve multiple users — per-user Telegram binding, per-user pipeline
identity (candidate.yaml), per-user dedup/queue/notifications/output folders.
Scraping stays shared (25 boards are fetched once per cycle, results fan out per user).

## Shared contract (identical in all three repos)

### Storage layout (host: `/home/deploy/job-hunter/users/`, env `USERS_ROOT`)

```
users/{userId}/
  candidate/            # candidate.yaml, candidate_profile.md, base_cv_*.md, examples/
  Applications/         # generated docs, {YYYY-MM-DD}/{Company}[_N]/
  templates/            # resume/cover-letter templates + manifest.json
```

`userId` is the `users.id` TEXT primary key from the API's app.sqlite.

### tracker.db shared-table DDL (API owns schema; this repo mirrors idempotently in `hunter/db.py`)

```sql
-- applications: add user scoping (backfill existing rows with the owner's id)
ALTER TABLE applications ADD COLUMN user_id TEXT NOT NULL DEFAULT '';
UPDATE applications SET user_id = '<ownerId>' WHERE user_id = '';
DROP INDEX IF EXISTS idx_url_norm;
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_url_norm
  ON applications(user_id, url_norm) WHERE url_norm != '';
CREATE INDEX IF NOT EXISTS idx_user_ats ON applications(user_id, ats_status);

CREATE TABLE IF NOT EXISTS user_settings (
  user_id TEXT NOT NULL, key TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '', updated_at TEXT,
  PRIMARY KEY (user_id, key));

CREATE TABLE IF NOT EXISTS telegram_links (
  chat_id INTEGER PRIMARY KEY, user_id TEXT UNIQUE NOT NULL, linked_at TEXT);

CREATE TABLE IF NOT EXISTS telegram_link_codes (
  code TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL);
```

### Per-user vs global settings (whitelist)

Per-user (read from `user_settings` via a new `user_setting(user_id, key, default)`
helper): `AUTO_APPLY`, `MAX_JOBS_PER_RUN`, `APPLY_DELAY_SEC`, `CANDIDATE_TRACKS`,
`CV_GDPR_CLAUSE`, `TELEGRAM_SEND_DOCS`, source enable toggles, `hunting_enabled`.
Global (stay in .env): `TELEGRAM_BOT_TOKEN`, all LLM/JUDGE/TRANSLATE API keys,
schedule times, scraper infrastructure settings.
`GSHEETS_*` / `GDRIVE_*` / Gmail source: **owner-only** — force-disabled for any
`user_id` other than the owner's.

### Telegram binding

One shared bot. The site/API generate a 6-char code (`telegram_link_codes`, 10-min
expiry); the user sends `/link CODE` to the bot; the bot validates, writes
`telegram_links(chat_id → user_id)`, deletes the code. `/unlink` removes the row.
`TELEGRAM_CHAT_ID` env survives ONLY as the admin chat for system/health alerts.

## Current state in THIS repo (verified 2026-08-06)

- `hunter/config.py`: everything is a module-level constant at import time —
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (single chat = auth check AND
  notification destination), `TRACKER_DB_PATH`, `APPLICATIONS_DIR` (env-overridable).
  Single `.env` via `load_dotenv`.
- `hunter/candidate.py`: candidate.yaml loader, `@lru_cache(maxsize=1)` — one
  candidate per process. Env override `CANDIDATE_YAML_PATH`. Hardcoded fallback
  name "Ihar Petrasheuski".
- `hunter/apply_shared.py:60-61`: `PROMPTS_DIR`, `CANDIDATE_DIR` module constants.
  Consumers of `CANDIDATE_DIR/candidate_profile.md`: `apply_api.py`,
  `claim_judge.py`, `dual_apply.py`, `verdict_refine.py`, `resume_sanitizer.py`,
  `about_me_agent.py`.
- `hunter/db.py`: `applications` table has NO user column; dedup is global
  (`idx_url_norm` + `dedup_key(company,title)`). Additive migrations pattern already
  exists in `_ensure_columns()` — follow it.
- Output: `Applications/{YYYY-MM-DD}/{Company}[_N]/` via `compute_output_folder`
  (`apply_shared.py:1756`); candidate name baked into filenames via
  `cv_filename_prefix` from candidate.yaml.
- Apply pipeline runs as a subprocess (`apply_agent.py`) — per-user env injection at
  spawn time is the designated seam (`CANDIDATE_YAML_PATH`, `APPLICATIONS_DIR`
  already work).
- Command auth today: some commands check `chat_id != TELEGRAM_CHAT_ID`, many
  (`/hunt`, `/force`, `/status`, URL paste, Apply/Skip buttons) check NOTHING —
  anyone who finds the bot can trigger LLM spend. The `/link` model fixes this.
- Global runtime state in the `config` KV table: `tracks_enabled`,
  `dual_apply_enabled`, `dual_shadow_profile`, `llm_outage_until`.

## Work phases for this repo

### Phase B1 — schema mirror + explicit user stamping (small; deploy together with api Phase A2)

1. `hunter/db.py::_ensure_columns`: mirror the shared-contract DDL (idempotent —
   the API may have applied it first). Stop creating the old `idx_url_norm`.
2. New env `DEFAULT_USER_ID` (the owner's user id): until Phase B3, every tracker
   write stamps `user_id = DEFAULT_USER_ID`; every dedup check scopes by it.
3. `.env` on the VPS gains `APPLICATIONS_DIR=<users/{ownerId}/Applications>` and
   `CANDIDATE_YAML_PATH=<users/{ownerId}/candidate/candidate.yaml>` (data moved by
   the api repo's migration script). Verify `CANDIDATE_DIR`-relative reads
   (`candidate_profile.md`, `base_cv_*.md`) follow the yaml's directory — if
   `apply_shared.CANDIDATE_DIR` is still `PROJECT_DIR / "candidate"`, derive it from
   `CANDIDATE_YAML_PATH`'s parent (env override) instead.
4. Verify: one full hunt cycle writes rows with the owner's `user_id`, docs land in
   the new per-user path, dedup still works.

### Phase B2 — per-user settings helper (small)

`hunter/config.py`: `user_setting(user_id, key, default)` reading `user_settings`
(cache per hunt cycle is fine). Not yet wired into flow control — that's B3.

### Phase B3 — multi-user runtime (the big one)

**Scope decision (owner, 2026-08-07): in B3 hunting stays OWNER-ONLY.**
Non-owner users get Telegram linking + **manual tailoring only**: they paste a
vacancy URL (or job text) to the bot and the apply pipeline runs with THEIR
candidate.yaml, THEIR output folder, THEIR notifications. The hunt fan-out for
everyone ships in Phase B3.5 together with per-user search specs — today's
scrape queries are shaped by the owner's profile (keywords angular/frontend/
react; Wrocław city slugs in pracuj/theprotocol listing URLs), so fanning
results out to a user with a different stack/city would silently find almost
nothing for them. Enforcement: `hunting_enabled` is force-`false` for any
`user_id` other than the owner's (same pattern as the owner-only
Sheets/Drive/Gmail guard) until B3.5 lifts it.

1. **Linking:** `hunter/commands/link.py` — `/link CODE` (validate against
   `telegram_link_codes`, write `telegram_links`, delete code), `/unlink`.
2. **User registry:** new `hunter/users.py` — `resolve_user(chat_id)`,
   `list_active_users()` (linked + candidate.yaml exists + `hunting_enabled`),
   `user_paths(user_id)` mirroring the shared storage layout (bot mounts the same
   `users/` root — update `docker-compose.yml`).
3. **Command routing:** every handler resolves the caller via
   `resolve_user(update.effective_chat.id)`; unbound chats get a "not linked"
   reply and nothing else. This closes the current no-auth commands.
   Notifications (`bot/notifications.py`, `apply_shared.py` senders,
   `best_effort.py`) go to the affected user's chat; system/health alerts
   (`oauth_alert`, subsystem health) go to admin `TELEGRAM_CHAT_ID`.
4. **Candidate loader:** `hunter/candidate.py` — cache keyed by resolved path
   (not `maxsize=1`); current-user path supplied explicitly or via contextvar.
   Sweep the 16 modules from `docs/quality/08-multi-user-configurability.md` to
   ensure identity flows from candidate.yaml, not constants.
5. **Hunt loop:** stays owner-only in B3 — keeps stamping `DEFAULT_USER_ID`
   exactly as in B1 (no fan-out yet; that's B3.5). What DOES land here:
   `tracker.py` reads/writes gain explicit `user_id` plumbing (not just the
   implicit `_uid()` default) so the manual-tailoring path can write rows for
   a non-owner user; per-user tracker cache or indexed SQL.
5b. **Manual tailoring for non-owners:** the paste/URL flow
   (`bot/apply_runner.py`, `commands/url_message.py`) resolves the sender via
   `resolve_user`, spawns the apply subprocess with THEIR env
   (`CANDIDATE_YAML_PATH`, `APPLICATIONS_DIR=users/{uid}/Applications`,
   `JOB_HUNTER_USER_ID`), stamps THEIR `user_id` on the tracker row, and
   notifies THEIR chat. This is the only pipeline non-owner users get in B3.
6. **Apply worker:** `claim_pending()` returns `user_id`; subprocess spawn injects
   `CANDIDATE_YAML_PATH`, `APPLICATIONS_DIR=users/{uid}/Applications`,
   `JOB_HUNTER_USER_ID=uid`; child stamps `user_id` on all tracker writes.
   In-flight guard key becomes `(user_id, url_norm)`.
7. **Config KV split:** `llm_outage_until` stays global; `tracks_enabled` /
   `dual_*` move to `user_settings` per user.
8. **Owner-only integrations:** Sheets/Drive/Gmail-source guarded by
   `user_id == owner`.
9. Seed the owner's `telegram_links` row manually (their existing chat_id):

   ```sql
   -- on the VPS: sqlite3 /home/deploy/job-hunter/db/tracker.db
   INSERT OR REPLACE INTO telegram_links (chat_id, user_id, linked_at)
   VALUES (<TELEGRAM_CHAT_ID from .env>, '<ownerId>', datetime('now'));
   ```

   Not strictly load-bearing — hunter/bot/auth.py keeps treating the admin
   chat (`TELEGRAM_CHAT_ID`) as the owner even without a link row, so a
   deploy before the seed never bricks the bot — but the explicit row is
   the documented end state.

**Status (2026-08-07, branch feat/multi-user-b3):** items 1–4, 5 (tracker
plumbing), 5b, 6 (per-user env spawn via the paste flow; queue worker rows
are owner-only in B3 anyway), 7, 8 (auth gates + Sheets/Drive/repost reader
scoping + hunting_enabled force-false) implemented. Item 9 is the ops step
above, to run at deploy.

### Phase B3.5 — per-user search + hunt fan-out (after B3, before B4)

Lifts the owner-only hunting restriction. Rationale: "each user's own search"
does NOT mean per-user HTTP requests — it means per-user search *specs*
compiled into one shared fetch plan.

1. **Per-user search specs:** keywords / cities / remote-preference read from
   the user's candidate.yaml (+ `user_settings` overrides where runtime-
   togglable). A `SearchSpec` dataclass carries them.
2. **Union fetch plan:** once per hunt cycle, union the specs of all
   `list_active_users()` → dedupe keywords/cities → the QUERY-DRIVEN sources
   (linkedin, findmyremote, thesmartjobs, nofluffjobs, pracuj, theprotocol,
   builtin) run one request per unique keyword/city instead of the owner's
   hardcoded set. Fetch-all sources (RSS / "all recent" JSON APIs / ATS
   aggregator / telegram channels) ignore the spec — their per-user search IS
   the fan-out filter. `BaseSource.search()` gains an optional spec parameter;
   results are deduped globally (same job found via two keywords = one Job).
3. **Per-source query budget:** cap unique queries per source per cycle so a
   user with exotic keywords can't balloon the scrape into anti-bot territory;
   overflow logged + surfaced in `/health`.
4. **Hunt fan-out** (moved here from B3): for each active user apply THEIR
   filters (their candidate.yaml), THEIR dedup (`is_known(user_id, …)`),
   enqueue PENDING with their `user_id`. `_hunt_lock` stays global (protects
   the scrape).
5. Flip the B3 force-`false` guard: `hunting_enabled` becomes a real per-user
   setting for everyone.

### Phase B4 — fairness & quotas (later, small)

Per-user daily apply quota (protects the LLM budget), FIFO fairness across users in
the apply queue (`ORDER BY` last-served), per-user cost reporting
(`SUM(cost_usd) GROUP BY user_id` — the API exposes it).

## Verification

- B1: hunt cycle green with stamped user_id; existing tests pass
  (`pytest`); no rows with empty user_id.
- B3 end-to-end: a second user links Telegram, uploads candidate files via the
  site, pastes a vacancy URL to the bot → tailored docs carry THEIR name
  (`cv_filename_prefix`), land in `users/{uid}/Applications/`, notifications go
  only to their chat, the tracker row carries their `user_id`; hunting stays
  owner-only (`hunting_enabled` force-false for non-owners); the owner's flow
  is unchanged. Use `mutation-verify` skill for regression tests where
  applicable.
- B3.5 end-to-end: the second user enables hunting → the union fetch plan
  includes their keywords/city, hunt produces rows only matching their filters,
  the same vacancy URL for two users produces two rows (no dedup collision);
  scrape request count grows by unique keywords/cities, not by user count.

## Coordination notes

- B1 must deploy in the same maintenance window as the api repo's Phase A2
  (data move + path envs). Sequence: stop bot → run api's
  `scripts/migrate-owner-data.sh` → update bot .env → deploy both.
- Do not edit the shared-contract DDL here — if it needs changing, it changes in
  all three files via the user.
- Update `CLAUDE.md` work log when phases land.
