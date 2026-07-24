# WEB APP PLAN — Job Hunter as a web application

> **Goal:** Turn the single-user Python bot into a multi-user product with a
> web UI, so a friend (or anyone) can register, fill in their profile, and
> use the full scraping + LLM pipeline — without touching source code.
>
> **Stack decision:** Angular (frontend) + NestJS (backend) + PostgreSQL.
> Python bot stays as the "engine" — scraping + LLM pipeline. No rewrite.
>
> **Repos:**
> - `job-hunter-api` — NestJS backend (new)
> - `job-hunter-web` — Angular frontend (new)
> - `job-hunter` — Python bot (existing, adapted)

---

## Architecture

```
┌───────────────────────────┐
│   job-hunter-web          │
│   (Angular)               │
│                           │
│  /login, /register        │
│  /dashboard   — jobs list │
│  /profile     — candidate │
│  /settings    — filters,  │
│                 Telegram,  │
│                 LLM keys   │
│  /applications — tracker  │
│  /funnel       — stats    │
└──────────┬────────────────┘
           │ REST / WebSocket
           ▼
┌───────────────────────────┐
│   job-hunter-api          │
│   (NestJS)                │
│                           │
│  AuthModule    — JWT      │
│  UsersModule   — CRUD     │
│  ProfileModule — candidate│
│                 data      │
│  JobsModule    — vacancies│
│  ApplyModule   — tracker  │
│  TelegramModule— bot link │
│  BotBridgeModule — sends  │
│    tasks to Python bot    │
└──────────┬────────────────┘
           │ reads/writes
           ▼
┌───────────────────────────┐
│      PostgreSQL           │
│                           │
│  users                    │
│  user_profiles            │
│  user_filters             │
│  user_employers           │
│  jobs                     │
│  applications             │
│  source_runs              │
│  subsystem_health         │
│  config_kv                │
└──────────┬────────────────┘
           │ reads/writes
           ▼
┌───────────────────────────┐
│   job-hunter (Python bot) │
│                           │
│  Per-user scraping loop   │
│  Per-user LLM pipeline    │
│  Per-user Telegram notify │
│  Per-user Applications/   │
└───────────────────────────┘
```

### Integration: NestJS ↔ Python Bot

**Shared PostgreSQL** — simplest approach for a learning project:
- NestJS owns the schema (TypeORM migrations)
- Python bot reads user config + writes jobs/applications via the same DB
- `hunter/db.py` switches from SQLite to PostgreSQL (psycopg2/asyncpg)
- No message queue, no REST between services — just a shared DB

**Per-user data isolation:**
- Every table with user data has a `user_id` FK
- Bot queries filter by `user_id`
- `prompts/` files generated from DB into per-user temp dirs at apply time

---

## PostgreSQL Schema (core tables)

```sql
-- Auth & identity
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT UNIQUE NOT NULL,
    password    TEXT NOT NULL,  -- bcrypt hash
    created_at  TIMESTAMPTZ DEFAULT now(),
    is_active   BOOLEAN DEFAULT true
);

-- What today lives in candidate.yaml + candidate_profile.md
CREATE TABLE user_profiles (
    user_id         UUID PRIMARY KEY REFERENCES users(id),
    full_name       TEXT NOT NULL,
    cv_filename_prefix TEXT,  -- "Ihar_Petrasheuski_CV"
    also_known_as   TEXT,     -- "also known as Igor Pietraszewski"
    phone           TEXT,
    email_contact   TEXT,
    linkedin_url    TEXT,
    home_city       TEXT NOT NULL,        -- "Wrocław"
    home_city_aliases TEXT[],             -- ["wroclaw", "vrotslav"]
    acceptable_hybrid TEXT[],             -- ["Wrocław"]
    acceptable_weekly_hybrid TEXT[],      -- ["Warszawa", "Kraków"]
    work_authorization TEXT DEFAULT 'EU', -- feeds doomed gate
    spoken_languages TEXT[] DEFAULT '{en}',
    cv_languages TEXT[] DEFAULT '{en}',
    timezone TEXT DEFAULT 'Europe/Warsaw',
    -- LLM prompt content (replaces candidate_profile.md)
    candidate_profile_md TEXT,
    -- Per-track base CVs (replaces base_cv_*.md files)
    base_cv JSONB DEFAULT '{}',  -- {"angular": "..md content..", "react": "..."}
    tracks_enabled TEXT[] DEFAULT '{angular}',
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Replaces hardcoded employer lists in verdict_refine/content_qa
CREATE TABLE user_employers (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID REFERENCES users(id),
    company     TEXT NOT NULL,
    period      TEXT,           -- "2018-2022"
    is_verifiable BOOLEAN DEFAULT true,  -- protected from stretch edits
    is_flexible BOOLEAN DEFAULT false,   -- Altoros-like, can weave projects
    flexible_projects TEXT[],            -- ["E-commerce", "Insurance"]
    sort_order  INT DEFAULT 0,
    UNIQUE(user_id, company)
);

-- Per-user filter configuration
CREATE TABLE user_filters (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    -- What today lives in filter_config.py
    title_keywords TEXT[],         -- ["angular", "frontend", "react"]
    exclude_title_patterns TEXT[], -- regex patterns
    location_whitelist TEXT[],     -- ["remote", "zdalnie", "wroclaw"]
    exclude_languages TEXT[],      -- ["german"] (replaces exclude_german flag)
    exclude_companies TEXT[],
    min_seniority TEXT DEFAULT 'mid',
    custom_rules JSONB DEFAULT '{}'
);

-- Per-user Telegram + API keys
CREATE TABLE user_settings (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    telegram_chat_id BIGINT,
    telegram_bot_token TEXT,     -- each user brings their own bot, OR
    use_shared_bot BOOLEAN DEFAULT true,  -- uses the platform bot
    -- LLM
    llm_provider TEXT DEFAULT 'anthropic',
    llm_model TEXT DEFAULT 'claude-sonnet-4-6',
    llm_api_key_encrypted TEXT,  -- encrypted at rest
    -- Google integrations (per-user OAuth)
    gsheets_enabled BOOLEAN DEFAULT false,
    gdrive_enabled BOOLEAN DEFAULT false,
    google_oauth_token_encrypted TEXT,
    -- Behavior
    auto_apply BOOLEAN DEFAULT false,
    max_jobs_per_run INT DEFAULT 40,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Jobs (scraped vacancies) — shared pool + per-user applications
CREATE TABLE jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    url_norm    TEXT NOT NULL,
    company     TEXT,
    title       TEXT,
    location    TEXT,
    stack       TEXT,
    description TEXT,
    scraped_at  TIMESTAMPTZ DEFAULT now(),
    expired_at  TIMESTAMPTZ,
    UNIQUE(url_norm)
);

-- Per-user applications (replaces tracker.db applications table)
CREATE TABLE applications (
    id          TEXT PRIMARY KEY,   -- 8-char hex
    user_id     UUID REFERENCES users(id) NOT NULL,
    job_id      UUID REFERENCES jobs(id),
    date        TEXT,
    company     TEXT,
    title       TEXT,
    stack       TEXT,
    ats_status  TEXT,
    url         TEXT,
    url_norm    TEXT,
    folder      TEXT,
    sent        TEXT,
    reapplication TEXT,
    to_learn    TEXT,
    drive_url   TEXT,
    confirmation TEXT,
    answer      TEXT,
    cost_usd    REAL,
    ats_verdict INT,
    fail_count  INT DEFAULT 0,
    -- Sheets sync
    sheets_row  INT,
    sheets_dirty INT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_applications_user ON applications(user_id);
CREATE INDEX idx_applications_url ON applications(url_norm);
```

---

## NestJS Backend — Module Structure

```
job-hunter-api/
├── src/
│   ├── auth/              # JWT, registration, login
│   │   ├── auth.module.ts
│   │   ├── auth.controller.ts
│   │   ├── auth.service.ts
│   │   ├── jwt.strategy.ts
│   │   └── dto/
│   ├── users/             # User CRUD
│   │   ├── users.module.ts
│   │   ├── users.controller.ts
│   │   ├── users.service.ts
│   │   └── entities/user.entity.ts
│   ├── profile/           # Candidate profile + employers
│   │   ├── profile.module.ts
│   │   ├── profile.controller.ts
│   │   ├── profile.service.ts
│   │   └── entities/
│   ├── filters/           # Per-user filter config
│   │   ├── filters.module.ts
│   │   ├── filters.controller.ts
│   │   └── filters.service.ts
│   ├── jobs/              # Scraped vacancies
│   │   ├── jobs.module.ts
│   │   ├── jobs.controller.ts   # GET /jobs, GET /jobs/:id
│   │   ├── jobs.service.ts
│   │   └── jobs.gateway.ts      # WebSocket for live updates
│   ├── applications/      # Per-user tracker
│   │   ├── applications.module.ts
│   │   ├── applications.controller.ts
│   │   └── applications.service.ts
│   ├── telegram/          # Bot linking
│   │   ├── telegram.module.ts
│   │   └── telegram.service.ts  # generates link code
│   ├── settings/          # Per-user settings
│   │   ├── settings.module.ts
│   │   ├── settings.controller.ts
│   │   └── settings.service.ts
│   ├── analytics/         # Funnel, stats
│   │   ├── analytics.module.ts
│   │   └── analytics.service.ts
│   └── app.module.ts
├── migrations/            # TypeORM migrations
├── test/
├── .env.example
├── nest-cli.json
├── tsconfig.json
└── package.json
```

### Key API Endpoints

```
POST   /auth/register          — email + password
POST   /auth/login             — returns JWT
GET    /auth/me                — current user

GET    /profile                — candidate profile
PUT    /profile                — update profile
GET    /profile/employers      — employer list
POST   /profile/employers      — add employer
PUT    /profile/employers/:id  — update employer
DELETE /profile/employers/:id  — remove employer

GET    /filters                — filter config
PUT    /filters                — update filters

GET    /jobs                   — paginated, filterable
GET    /jobs/:id               — job detail

GET    /applications           — user's applications
GET    /applications/funnel    — funnel analytics
GET    /applications/stats     — summary stats

GET    /settings               — user settings
PUT    /settings               — update settings
POST   /settings/link-telegram — generate Telegram link code

WS     /jobs/live              — real-time new job notifications
```

---

## Angular Frontend — Pages

```
job-hunter-web/
├── src/app/
│   ├── core/              # Auth interceptor, guards, services
│   │   ├── auth/
│   │   │   ├── auth.service.ts
│   │   │   ├── auth.guard.ts
│   │   │   └── auth.interceptor.ts
│   │   └── api/
│   │       └── api.service.ts
│   ├── features/
│   │   ├── auth/          # Login, Register pages
│   │   │   ├── login/
│   │   │   └── register/
│   │   ├── dashboard/     # Main page: recent jobs, stats
│   │   │   ├── dashboard.component.ts
│   │   │   ├── job-card/
│   │   │   └── stats-widget/
│   │   ├── jobs/          # Job browser with filters
│   │   │   ├── jobs-list/
│   │   │   ├── job-detail/
│   │   │   └── job-filters/
│   │   ├── applications/  # Tracker view
│   │   │   ├── applications-table/
│   │   │   └── funnel-chart/
│   │   ├── profile/       # Candidate profile editor
│   │   │   ├── profile-form/
│   │   │   ├── employers-list/
│   │   │   └── cv-editor/   # markdown editor for base CVs
│   │   └── settings/      # Filters, Telegram, API keys
│   │       ├── filter-settings/
│   │       ├── telegram-link/
│   │       └── llm-settings/
│   └── shared/            # UI components, pipes, directives
├── environments/
└── angular.json
```

### Key Screens

1. **Dashboard** — cards: new jobs today, pending applications, unsent count,
   funnel summary. Live WebSocket updates when bot finds new jobs.
2. **Jobs** — table/cards with filtering by source, date, stack. "Apply" button
   triggers bot pipeline for this user.
3. **Applications** — full tracker table (mirrors Google Sheets view). Inline
   edit for Sent/To Learn/Re-application. Funnel chart.
4. **Profile** — form for name, city, stack, languages, employers. Markdown
   editor for `candidate_profile.md` content and per-track base CVs.
5. **Settings** — filter rules, Telegram linking, LLM provider/model/key,
   Google OAuth connect, auto-apply toggle.

---

## Python Bot Adaptation

The existing bot stays as the scraping + LLM engine. Key changes:

### DB migration: SQLite → PostgreSQL
- `hunter/db.py`: replace `sqlite3` with `psycopg2` (sync) or `asyncpg` (async)
- Every query gains `WHERE user_id = %s`
- Config KV table becomes per-user
- Connection string from `DATABASE_URL` env var

### Per-user config loading
- New `hunter/candidate.py` (doc 08's candidate.yaml, but reads from PostgreSQL
  instead of a YAML file):
  ```python
  def load_candidate(user_id: str) -> CandidateConfig:
      """Load candidate profile from PostgreSQL."""
      # Returns: name, city, employers, languages, filters, etc.
  ```
- Every module that today has hardcoded values imports from `candidate.py`

### Per-user prompts
- At apply time, generate temp `candidate_profile.md` + `base_cv_*.md` from DB
  into a per-user temp directory
- `PROMPTS_DIR` becomes per-invocation, not global

### Per-user hunt loop
- Bot runs one event loop, but schedules per-user hunts
- Each user's hunt uses their filters, their sources config, their chat_id
- `_hunt_lock` becomes per-user (no user blocks another)

### Telegram: shared bot, per-user routing
- One bot instance, commands routed by `chat_id → user_id` lookup
- `/start` sends a link code; user enters it in the web UI to bind
- All notifications go to the user's own chat
- Each user can optionally run their own bot (token in user_settings)

---

## Development Phases

### Phase 0 — NestJS skeleton + Auth (1-2 days)
**Learning goal:** NestJS basics — modules, controllers, services, TypeORM, JWT

- `nest new job-hunter-api`
- PostgreSQL + TypeORM setup
- `users` table + AuthModule (register, login, JWT)
- Basic tests

**Deliverable:** `POST /auth/register`, `POST /auth/login`, `GET /auth/me` working

### Phase 1 — Profile & Settings API (2-3 days)
**Learning goal:** TypeORM relations, DTOs, validation

- `user_profiles`, `user_employers`, `user_filters`, `user_settings` tables
- Full CRUD for profile, employers, filters
- Validation (class-validator)

**Deliverable:** user can create/read/update their candidate profile via API

### Phase 2 — Angular frontend: Auth + Profile (3-4 days)
**Learning goal:** Angular HttpClient, reactive forms, route guards

- `ng new job-hunter-web`
- Login/register pages
- Profile editor (reactive form)
- Employer list management
- Auth interceptor + JWT storage

**Deliverable:** user can register, log in, fill in their profile in the browser

### Phase 3 — candidate.yaml / doc 08 in Python bot (3-4 days)
**Learning goal:** (Python, not NestJS — but required for integration)

- Implement `hunter/candidate.py` (doc 08)
- Replace all hardcoded values (5 waves from the doc)
- Bot reads from candidate.yaml (file) OR PostgreSQL (when `DATABASE_URL` set)
- Tests: "second user" smoke test

**Deliverable:** Python bot is configurable per-user without code edits

### Phase 4 — Jobs & Applications API (2-3 days)
**Learning goal:** pagination, WebSocket gateway, complex queries

- `jobs` + `applications` tables
- Python bot writes to PostgreSQL
- NestJS serves job list (paginated, filterable)
- Applications API (tracker view)
- WebSocket gateway for live job notifications

**Deliverable:** jobs scraped by bot appear in the NestJS API

### Phase 5 — Angular frontend: Dashboard + Jobs (3-4 days)
**Learning goal:** Angular Material/PrimeNG, WebSocket, data tables

- Dashboard page with stats widgets
- Jobs list with filters
- Applications table (inline edit)
- Live notifications via WebSocket

**Deliverable:** working dashboard showing real jobs and applications

### Phase 6 — Telegram linking + multi-user bot (2-3 days)
**Learning goal:** Telegram Bot API from NestJS side

- Link code flow: web UI → NestJS generates code → user sends to bot → bound
- Bot routes notifications by `chat_id → user_id`
- Per-user hunt schedule

**Deliverable:** friend registers on the web, links Telegram, gets job notifications

### Phase 7 — Polish & deploy (2-3 days)
- Docker compose: NestJS + Angular + PostgreSQL + Python bot
- Cloudflare Pages for Angular (or serve from NestJS)
- Environment configs
- Friend onboarding walkthrough

---

## Total estimate

~20-25 days of focused work, spread across phases. Each phase is a usable
increment — the friend can start using it after Phase 6.

The NestJS learning curve is gentle for an Angular developer — same decorators,
same DI, same module system. The real complexity is in Phase 3 (making the
Python bot multi-user) and Phase 4 (DB migration).

## What the friend gets (after Phase 6)

1. Opens the web app → registers with email/password
2. Fills in their profile: name, city, stack, languages, work history
3. Writes their candidate_profile.md and base_cv in the web editor
4. Configures filters (what keywords, what locations, what to exclude)
5. Links their Telegram (sends a code to the bot)
6. Bot starts hunting for them on the next cycle
7. Gets Telegram notifications + can browse jobs in the web UI
8. Sees their applications, funnel stats in the dashboard

## What stays OUT of scope

- Payment/billing — this is for friends, not a SaaS
- Per-user Google OAuth in the web UI (complex; Sheets/Drive optional)
- Admin panel — overkill for 2-3 users
- Mobile app — Angular PWA is enough
- Rewriting the Python scraping/LLM engine in TypeScript
