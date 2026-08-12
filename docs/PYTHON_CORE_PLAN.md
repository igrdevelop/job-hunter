# Python Core Extraction & Web API Plan

> **SUPERSEDED — kept for history.** A design interview established that the goal is
> selling the product to other people, not extracting a core. That inverted most of
> the conclusions below: the last stage here (per-user CV files) is blocking for the
> MVP, the notifier design was too narrow, and a database migration and a whole
> product pipeline were missing. Current plan:
> [SAAS_PIVOT_PLAN_supersedes_PYTHON_CORE_PLAN.md](SAAS_PIVOT_PLAN_supersedes_PYTHON_CORE_PLAN.md).

Goal: let the web frontend (job-hunter-site → job-hunter-api) use the bot's
Python logic. Verified conclusion: **no separate pip package** — the boundary
is a clean `hunter` core plus a second Python process (FastAPI) in this repo,
proxied by NestJS. This document is the work plan, ordered by dependency.

Verified facts this plan is built on (2026-08-12):

- Telegram coupling in the core is limited to **two files**:
  `hunter/main.py` (7 functions take `context: ContextTypes.DEFAULT_TYPE`,
  call `send_text` / `send_job_cards`) and `hunter/apply_worker.py`
  (~15 `send_text` calls + `hunter/bot/state`). Everything else that imports
  `telegram` is the interface layer itself (`telegram_bot.py`, `hunter/bot/`,
  `hunter/commands/`, `hunter/schedules/` — the latter is PTB JobQueue-bound).
- The apply pipeline is already import-safe and telegram-free by design:
  `hunter/apply_api.py` ("no module-level state"), `hunter/services/`
  (`apply_service.py`, `tracker_service.py`).
- `tracker.db` runs in WAL mode with `busy_timeout` (`hunter/db.py:152`), and
  the NestJS API **already writes** to it (`PATCH /api/applications/:id` →
  `UPDATE applications SET <col>=? WHERE id=? AND user_id=?` in
  `api/src/tracker/tracker.service.ts:182`). Two writers already exist and WAL
  covers them.
- **Confirmed gap:** the API's UPDATE does not set `sheets_dirty=1`, so web
  edits never reach the Google Sheets mirror. The bot mirrors only its own
  changes (`gsheets_sync.mirror_*`) plus `resync_dirty()` for rows flagged via
  `tracker.mark_sheets_dirty()` (`hunter/tracker.py:1989`).
- The api repo is ahead of the old snapshot: active `feat/filters-api` branch
  (GET/PUT `/api/filters` with a **shared contract fixture** against the bot's
  `filters.yaml`), plus `src/users`, `user_id` scoping in queries, `src/mail`,
  `src/telegram`, `src/admin` modules. The "settings from the web" pattern is
  effectively being prototyped there — reuse it, don't invent a new one.
- Multi-user work is in flight in bot worktrees (`feat/multi-user-config`,
  `candidate-yaml-multi-user`); PR #204 just moved owner personal data out of
  shared code. Multi-user CV files must build on that, not fork it.

---

## Stage 1 — Mirror web edits to Google Sheets (api repo, small, independent)

The only already-working frontend feature (status change via
`PATCH /api/applications/:id`) silently diverges from the Sheets mirror.

- In `api/src/tracker/tracker.service.ts`, make the UPDATE also set
  `sheets_dirty=1` (except when the touched column is itself sheets-metadata).
- Verify the bot picks it up: confirm `resync_dirty()` runs on a schedule
  (`hunter/schedules/gsheets.py`), not only via manual `/gsheets_resync`.
  If schedule-only-on-failure, add a periodic resync tick.
- Test: patch a row via API, run resync, assert the Sheets cell changed
  (or unit-level: assert dirty flag set / cleared).

Deliverable: web status edits reach Google Sheets within one resync interval.

## Stage 2 — Notifier seam: make the core telegram-free (bot repo)

Introduce a small notifier protocol so orchestrators stop importing telegram.

- Define `Notifier` (protocol/dataclass): `send_text(text)`,
  `send_job_cards(jobs)` — async, minimal surface, lives in core
  (e.g. `hunter/notify.py`).
- `hunter/main.py`: replace `context: ContextTypes.DEFAULT_TYPE` parameters
  with `notifier: Notifier`; the telegram layer builds a PTB-backed notifier
  and passes it in. `hunter/bot/state` usage moves behind the same seam or
  into `hunter/services/`.
- `hunter/apply_worker.py`: same treatment for its `send_text` calls.
- Enforce the boundary so it cannot regress: import-linter contract or a
  simple test — modules outside `hunter/{bot,commands,schedules}` +
  `telegram_bot.py` must not import `telegram` or the bot layer.
- No behavior change: telegram bot output stays identical (assert via existing
  tests; add a notifier-capture test double).

Deliverable: `grep -rl "^from telegram" hunter/` matches only the interface
layer; CI guard in place.

## Stage 3 — FastAPI service: submit vacancies from the web (bot repo + api repo)

Second entry point over the same code, same repo, same compose project.

Bot repo:
- `web_api.py` (or `hunter/web/app.py`): FastAPI app importing
  `hunter.services.apply_service` / `hunter.apply_api`.
- Endpoints (first slice):
  - `POST /jobs` — body: url + optional paste text → enqueue, return task id.
    Long-running (LLM CV generation takes minutes) — never synchronous.
  - `GET /jobs/{task_id}` — status/outcome (reuse `ApplyOutcome` values).
  - `GET /health`.
- Queue: reuse the existing apply-queue machinery where possible; the web
  submission should land in the same pipeline as a Telegram URL paste, with a
  non-telegram notifier (Stage 2) writing progress to the task record.
- New container in the bot's docker-compose, internal network only (no public
  port) — reachable solely from the api container.

Api repo:
- Proxy module (e.g. `src/bot-gateway/`): JWT-guarded routes
  `POST /api/jobs`, `GET /api/jobs/:id` forwarding to the Python service.
  Auth stays in NestJS only; the Python service trusts the internal network
  (plus a shared-secret header for defense in depth).
- Contract fixture shared with the bot, following the `feat/filters-api`
  pattern.

Deliverable: frontend can submit a vacancy URL and watch it progress to a
generated application.

## Stage 4 — Writable settings from the web

Build directly on `feat/filters-api` (GET/PUT `/api/filters`) — that branch
already solves "web edits a bot-owned config file with a shared contract".

- Land/finish `feat/filters-api` first; treat it as the template.
- Inventory which `.env` settings actually need web editing; split them into
  a hot-reloadable store (yaml/db table the bot re-reads per cycle — as
  `filters.yaml` already is) vs. restart-required (stay in `.env`, read-only
  in the web UI as today via SettingsModule).
- Validation lives in Python (the code that consumes the values); expose it
  via the FastAPI service (`POST /settings/validate` or PUT with 422s),
  NestJS proxies.

Deliverable: the settings page shows editable hot-reload settings and
read-only `.env` ones.

## Stage 5 — Multi-user candidate/CV files (largest; last)

Blocked on the in-flight multi-user config work (`feat/multi-user-config`,
`candidate-yaml-multi-user` worktrees, PR #204 identity gating). Do not start
until that lands — this stage extends it, it must not fork it.

- Per-user candidate layout (`candidate/<user>/...`) consumed by the apply
  pipeline: thread `user_id` from job submission through
  `apply_api`/`generate_docs` to file resolution.
- Api repo: FilesModule/TemplatesModule scoped per user (queries already carry
  `user_id`; extend to file paths).
- Upload of CV source files per user via existing FilesModule upload,
  validated against the candidate-yaml schema from the multi-user branch.

Deliverable: a second linked user can upload their CV sources from the web
and submit vacancies that generate documents from *their* files.

---

## Explicitly rejected

- **Separate pip package / repo for the core** — only one repo consumes the
  Python code; packaging adds versioning drag with zero payoff. Revisit only
  if a Python consumer appears outside this repo.
- **Frontend calling the Python service directly** — NestJS remains the single
  auth/routing point; the Python service is internal-only.
- **Synchronous apply endpoint** — CV generation is minutes-long; always
  task-queue + polling.

## Risks / open questions

- `hunter/schedules/` stays PTB-bound (JobQueue). Fine while the bot process
  owns all scheduling; if the web service ever needs schedules, that's a new
  decision (APScheduler in the FastAPI process), not part of this plan.
- Apply queue single-flight: verify the URL in-flight lock (#201) is enforced
  at queue level, not telegram level, so web + telegram submissions of the
  same URL cannot race.
- `resync_dirty()` cadence: if it only runs after failed mirror writes,
  Stage 1 needs a periodic tick added.
