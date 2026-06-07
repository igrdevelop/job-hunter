# SAAS_PLAN.md — Job Hunter as a Public Multi-Tenant Service

> Migration plan from the current **single-tenant Telegram bot** (one owner, one
> `tracker.db`, one Google account, hardcoded `FILTER`) to a **public SaaS**:
> many users, web auth, results tables in our own DB (not Google Sheets), per-user
> tailored CV/cover-letter generation.
>
> Status: **PLAN — nothing implemented yet.** The current bot keeps working
> untouched until each stage lands behind it.

---

## 0. Fixed product decisions (owner-confirmed)

| Decision | Choice | Consequence |
|---|---|---|
| Apply model | **Assist** — we generate CV+CL, user applies himself | No form-automation, much lower legal risk, no captcha-solving |
| Monetization | **Free tier + paid tier** (Stripe) | Per-user quota + billing required from MVP, protects LLM budget |
| Scraping model | **Central** — scrape once into a shared pool, match per user | Scraping is a background service, NOT a per-user action |
| Primary UI | **Website** (auth + results tables); Telegram becomes optional notify channel | Google Sheets/Drive are replaced, not extended |
| Data store | **Postgres** (multi-tenant), object storage for PDFs | SQLite `tracker.db` retired for the service |

### The one architectural decision everything hinges on

The current bot scrapes 17 boards **on behalf of the owner**. Doing that *per user*
in a public service multiplies scraping load by the user count → instant IP bans,
Cloudflare, HTTP 429 (already a problem for **one** user at pracuj.pl). LinkedIn
litigates against it.

**Flip the model:**

```
NOW:   for each user → scrape 17 boards → filter for him
SAAS:  scrape 17 boards ONCE → shared `jobs` pool (DB)
       → for each user, match the pool against his saved criteria → per-user `applications`
```

Scraping becomes a shared cron-driven worker. Everything else is built around this.

---

## 1. Target architecture

```
┌─ Web frontend (Next.js / React) ───────────────┐   auth · onboarding (upload CV)
│  login · profile · search queries              │   results table (replaces Sheets)
│  jobs table · generated CV/CL download          │   download PDF (replaces Drive)
└───────────────────────┬─────────────────────────┘
                        │ HTTPS REST/JSON
┌───────────────────────▼─────────────────────────┐
│  Backend API (FastAPI)                          │   auth (JWT/OAuth), CRUD,
│  users · profiles · search_queries · billing    │   quota enforcement,
│  issues signed URLs for stored docs             │   enqueues apply jobs
└──────┬───────────────────────────┬──────────────┘
       │                           │
┌──────▼──────────┐        ┌───────▼───────────────┐
│ Postgres        │        │ Task queue (Arq/Celery)│
│ multi-tenant    │        │  + Redis broker        │
│ user_id on all  │        └───────┬────────────────┘
│ per-user tables │                │
│ + shared `jobs` │        ┌───────▼─────────────────────────────┐
└─────────────────┘        │ Workers:                            │
                           │  • scraper  (cron, shared pool)     │ → jobs
┌──────────────────┐       │  • matcher  (per-user filter)        │ → applications(new)
│ Object storage   │◄──────┤  • apply    (LLM → CV/CL → PDF)      │ ← REUSES current core
│ (S3 / R2 / MinIO)│       │  • notifier (email / Telegram)       │
│  generated docs  │       └─────────────────────────────────────┘
└──────────────────┘
```

### What survives from the current codebase vs what is rebuilt

| Current module | Fate |
|---|---|
| `apply_api.py`, `apply_shared.py`, `generate_docs.py`, `llm_client.py`, `prompts/*` | ✅ **Reused as the apply-worker core** — already clean & parameterized after Phase 4 refactor |
| `hunter/sources/*` (17 scrapers), `hunter/rate_limiter.py` | ✅ Reused, but driven by the **shared scraper worker**, not per-user |
| `hunter/filters.py` + `FILTER` dict | 🔧 Becomes a **matching engine** taking the user's criteria as a parameter (today it's a module-global constant) |
| `hunter/tracker.py`, `tracker.db`, `tracker_cache.py` | ❌ Replaced by Postgres (`jobs` shared + `applications` per-user) |
| `hunter/gsheets_*` | ❌ Replaced by DB tables + web UI |
| `hunter/gdrive_*` | ❌ Replaced by object storage + signed URLs |
| `hunter/telegram_bot.py`, `commands/`, `schedules/` | 🔧 Telegram demoted to an **optional notification channel**; web is the primary UI |
| `candidate_profile.md` (single file) | ❌ → `profiles` table per user, populated by onboarding (upload CV → LLM parse → confirm) |

---

## 2. Data model (Postgres) — multi-tenant core

Row-level isolation by `user_id` on every per-user table. `jobs` is the only
**shared** table (the scraped pool); users never see each other's `applications`.

```sql
-- Identity & auth
users(
  id            uuid pk,
  email         citext unique not null,
  password_hash text,                 -- null if OAuth-only
  oauth_provider text,                 -- 'google' | null
  tier          text not null default 'free',   -- 'free' | 'pro'
  created_at    timestamptz,
  deleted_at    timestamptz           -- GDPR soft-delete
)

-- One per user: parsed CV + extra info (replaces candidate_profile.md)
profiles(
  id          uuid pk,
  user_id     uuid fk users unique,
  full_name   text,
  headline    text,
  raw_cv_text text,                    -- original upload, for re-parsing
  structured  jsonb,                   -- parsed: experience[], skills[], education[]
  extra_info  text,                    -- free-text user notes/preferences
  updated_at  timestamptz
)

-- User's "dream job" search criteria (replaces global FILTER)
search_queries(
  id              uuid pk,
  user_id         uuid fk users,
  title           text,                -- "Senior Angular Developer"
  keywords        text[],
  exclude_patterns text[],
  locations       text[],              -- ['remote','wroclaw']
  seniority       text[],
  language_excludes text[],            -- e.g. ['german']
  active          boolean default true,
  created_at      timestamptz
)

-- SHARED scraped pool (deduped across all users)
jobs(
  id           uuid pk,
  source       text,                   -- 'justjoin' | 'linkedin' | ...
  url          text,
  url_norm     text unique,            -- dedup key (reuse normalize_url)
  company      text,
  title        text,
  location     text,
  description  text,
  posted_at    timestamptz,
  scraped_at   timestamptz,
  expired      boolean default false
)
create index on jobs(url_norm);

-- Per-user match + application state (replaces tracker rows)
applications(
  id            uuid pk,
  user_id       uuid fk users,
  job_id        uuid fk jobs,
  status        text,                  -- 'new'|'generating'|'ready'|'applied'|'skipped'|'failed'
  match_score   numeric,
  cv_doc_id     uuid fk documents,     -- generated tailored CV
  cl_doc_id     uuid fk documents,     -- generated cover letter
  applied_at    timestamptz,           -- user marks "I applied"
  created_at    timestamptz,
  unique(user_id, job_id)              -- per-user dedup
)
create index on applications(user_id, status);

-- Generated documents in object storage
documents(
  id          uuid pk,
  user_id     uuid fk users,
  kind        text,                    -- 'cv' | 'cover_letter'
  storage_key text,                    -- S3/R2 object key
  filename    text,
  created_at  timestamptz
)

-- Quota / billing
usage_counters(
  user_id      uuid fk users,
  period       date,                   -- month bucket
  generations  int default 0,         -- apply runs this period
  primary key(user_id, period)
)
```

**Dedup now has two levels:** pool-level (`jobs.url_norm` unique, scraped once) and
per-user (`applications(user_id, job_id)` unique). Reuse `normalize_url` /
`dedup_key` logic from the current `tracker.py`.

---

## 3. Roadmap (staged, each independently shippable)

Stages 1–3 are validatable via API/CLI **without a website**. The site (Stage 4)
comes once multi-tenant core already works.

### Stage 0 — Repo & infra scaffolding
- [ ] New service layout (monorepo or `/service`): `api/`, `workers/`, `web/`, `migrations/`.
- [ ] Docker Compose: Postgres + Redis + MinIO (local S3) + api + worker.
- [ ] Alembic migrations; CI runs migrations + tests.
- [ ] Move LLM/scraper core into an importable package shared by api & workers.

### Stage 1 — Multi-tenant data foundation **(do first; everything depends on it)**
- [ ] Postgres schema above via Alembic.
- [ ] Port the apply core (`apply_api`/`apply_shared`/`generate_docs`) to take
      `profile` + `job` + `search_query` as **parameters** (no module globals, no file reads).
- [ ] Repository layer: `users`, `profiles`, `jobs`, `applications` CRUD with `user_id` scoping.
- [ ] Port `normalize_url` / dedup into pool-level + per-user dedup.

### Stage 2 — Scraper-as-a-service (shared pool)
- [ ] Wrap the 17 `hunter/sources/*` in a `scraper` worker, cron-driven (Arq/Celery beat).
- [ ] Write results into shared `jobs` (dedup by `url_norm`), reuse `DomainLimiter` for 429 safety.
- [ ] Central expiry check writes `jobs.expired` once (not per user).

### Stage 3 — Matcher (per-user)
- [ ] Refactor `filters.py` into an engine: `match(job, search_query) -> score|None`.
- [ ] Matcher worker: for each active `search_query`, scan new `jobs` → insert `applications(status='new')`.
- [ ] Per-user dedup via `unique(user_id, job_id)`.

### Stage 4 — Web app + Auth (replaces Sheets/Drive UI)
- [ ] FastAPI auth: email/password + Google OAuth, JWT sessions.
- [ ] Onboarding flow: **upload CV → LLM parse → `profiles.structured` → user confirms/edits**.
- [ ] Dream-job form → `search_queries`.
- [ ] Results table (the Google-Sheets replacement): jobs matched, status, download buttons.
- [ ] Frontend: Next.js/React, calls API, renders tables, signed-URL downloads.

### Stage 5 — Apply worker + storage
- [ ] `apply` worker consumes `applications(status='new')` → generates tailored CV+CL (existing core)
      → uploads PDF/DOCX to object storage → `documents` rows → `status='ready'`.
- [ ] Signed-URL download endpoint; quota check before each generation.

### Stage 6 — Billing, quotas, GDPR
- [ ] `usage_counters` enforcement; free-tier cap → 402 when exceeded.
- [ ] Stripe checkout + webhook → `users.tier='pro'`.
- [ ] GDPR: consent on signup, privacy policy + ToS, account deletion (hard-delete docs + rows),
      encryption at rest, DPA review with LLM provider (PII goes to the model).

### Stage 7 — Telegram as optional channel
- [ ] Per-user opt-in: link Telegram account → notify on new `ready` applications.
- [ ] Reuse current `bot/notifications.py` patterns, but addressed per user (no hardcoded chat_id).

---

## 4. Hard problems / risks (track these, they decide viability)

1. **Legal — scraping at commercial scale.** Even assist-only, scraping LinkedIn/boards
   commercially can violate ToS. Mitigate: prefer official/partner APIs where they exist,
   honor robots, central low-rate scraping, be ready to drop a source. **Get legal advice
   before public launch.**
2. **GDPR (EU).** Storing third parties' CVs = personal data with you in Poland/EU.
   Consent, deletion, encryption, sub-processor DPA (LLM provider sees PII) are mandatory,
   not optional. This is a launch blocker if skipped.
3. **LLM cost & abuse.** Every generation costs money. Free-tier quota + rate limit +
   abuse detection from day one. BYO-key remains a possible escape hatch if costs explode.
4. **Scraper fragility at scale.** 17 boards break independently; a per-user service makes
   outages user-visible. Central pool + the existing `scraper-health-checker` discipline +
   graceful per-source degradation.
5. **Cold-start data.** A new user wants results immediately; the shared pool must already be
   populated. Run the scraper continuously, keep a rolling window of fresh `jobs`.

---

## 5. Migration / coexistence strategy

- The current single-tenant bot is **not modified** during Stages 0–3; it keeps running
  for the owner from `master`.
- New service lives in its own branch/dir; shares the apply-core package by import.
- Owner becomes "user #1" in the new system once Stage 5 works end-to-end; the old bot is
  retired only after parity is proven.

---

## 6. Open questions (revisit before Stage 4)

- Web stack: Next.js (SSR, easy auth) vs SPA + separate API — lean Next.js.
- Hosting: single VPS (cheap, manual) vs managed (Fly/Render + managed Postgres) — start managed.
- Object storage: Cloudflare R2 (no egress fees) vs S3 — lean R2.
- Auth: roll-our-own JWT vs Auth provider (Clerk/Supabase Auth) — managed auth saves weeks.
- Do we let users edit the generated CV in-browser, or download-only for MVP? — download-only MVP.

---

_Author: AI agent, 2026-06-07. Update this doc as stages land; mirror schema/flow changes
into CLAUDE.md when implementation starts._
