# WEB APP PLAN — Job Hunter as a web application

> **Goal:** Replace Google Sheets and Google Drive with a self-hosted web UI.
> The user logs in, sees their applications table, downloads generated files,
> views statistics — all in the browser instead of Google products.
> Multi-user support comes in Phase B, after the core UI works.
>
> **Stack:** Angular (frontend) + NestJS (backend) + SQLite (Phase A) →
> PostgreSQL (Phase B). Python bot stays as the "engine" — scraping + LLM
> pipeline. No rewrite.
>
> **Repos:**
> - `job-hunter-site` — Angular frontend (existing repo, igrflex.work)
> - `job-hunter-api` — NestJS backend (new repo)
> - `job-hunter` — Python bot (existing, adapted)
>
> **Decisions already made:**
> - Telegram stays as notification/command channel alongside the web app
> - NestJS runs in Docker on the same VPS as the bot (178.105.131.107,
>   Ubuntu 24.04, hostname `job-hunter`)
> - NestJS serves BOTH the Angular static files AND the API — one process,
>   one domain, no CORS. No Cloudflare Pages needed.
> - Cloudflare Tunnel exposes the VPS as `job-hunter.igrflex.work` (HTTPS auto)
> - Email + password auth from day one (ready for multi-user in Phase B)
>
> **Domains:**
> - `job-hunter.igrflex.work` — the web app (NestJS + Angular, via Tunnel)
> - `igrflex.work` — future CV landing page (separate project, not this plan)
>
> `job-hunter.igrflex.work` currently points to a Cloudflare Pages default
> starter. When the tunnel goes live, switch its DNS from Pages to the tunnel
> CNAME (one click in the Cloudflare dashboard, igrflex@gmail.com account).

---

## Two-phase approach

The old plan tried to do three things at once (replace Google, add multi-user,
learn NestJS). That's three separate projects with compounding risk. Instead:

**Phase A — "Own UI instead of Google" (single-user, ~12-16 days)**
NestJS reads the EXISTING `tracker.db` (SQLite) and serves files from the
EXISTING `Applications/` folder. Angular shows: applications table (= Sheets),
file browser (= Drive), statistics (= /funnel). The Python bot writes to
tracker.db as before — zero migration. Google Sheets/Drive are disabled.

**Phase B — "Multi-user" (later, ~15-20 days)**
SQLite → PostgreSQL. `user_id` on every table. Registration, per-user config,
per-user bot schedule. File storage moves to Cloudflare R2 or stays local
with per-user subdirectories.

Phase A alone already delivers a working product that replaces Google.

---

## Phase A architecture

```
                    Browser
                       │
                       │ HTTPS
                       ▼
              ┌─────────────────┐
              │ Cloudflare CDN  │
              │ job-hunter.igrflex.work│
              └────────┬────────┘
                       │ Tunnel (cloudflared)
                       ▼
┌──────────────────────────────────────────────────┐
│   VPS 178.105.131.107 (Ubuntu 24.04)             │
│   docker-compose                                 │
│                                                  │
│  ┌────────────────────────────────────────────┐  │
│  │  job-hunter-api (NestJS) :3000             │  │
│  │                                            │  │
│  │  Serves Angular static files (dist/)       │  │
│  │  + REST API:                               │  │
│  │    /auth/*          — JWT login            │  │
│  │    /api/applications — tracker CRUD        │  │
│  │    /api/files/*     — serve Applications/  │  │
│  │    /api/analytics/* — funnel, stats        │  │
│  │  All other routes → index.html (SPA)       │  │
│  └──────┬───────────────────┬─────────────────┘  │
│         │ reads/writes      │ reads              │
│         ▼                   ▼                    │
│  ┌──────────────┐  ┌─────────────────────┐       │
│  │  tracker.db  │  │   Applications/     │       │
│  │  (SQLite)    │  │   {date}/{company}/ │       │
│  │  WAL mode    │  │   PDFs, DOCXs, etc  │       │
│  └──────┬───────┘  └──────────┬──────────┘       │
│         │ writes              │ writes           │
│         ▼                     ▼                  │
│  ┌────────────────────────────────────────────┐  │
│  │  job-hunter (Python bot)                   │  │
│  │  Scraping → Filtering → LLM pipeline       │  │
│  │  Telegram notifications (unchanged)        │  │
│  └────────────────────────────────────────────┘  │
│                                                  │
│  ┌────────────────────────────────────────────┐  │
│  │  cloudflared (tunnel agent)                │  │
│  │  Routes job-hunter.igrflex.work → localhost:3000  │  │
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

### Why this works without migration

- **SQLite WAL mode** supports concurrent readers + one writer across
  processes. The bot is the primary writer; NestJS mostly reads. The few
  writes NestJS does (Sent, To Learn, Re-application edits) are low-frequency
  and serialized by SQLite's own locking — identical to what the Google Sheets
  pull already does today (`_apply_pull_delta_db`).
- **Applications/ folder** is a Docker volume mounted into both containers.
  NestJS serves files via a static controller. No upload/sync logic needed.
- **No bot code changes** for Phase A. The bot doesn't know the web app
  exists — it writes to tracker.db and Applications/ as before.
- **One origin, no CORS.** NestJS serves both the Angular SPA (`dist/`
  as static files, all non-API routes → `index.html`) and the REST API
  (`/api/*`, `/auth/*`). The browser sees one domain (`job-hunter.igrflex.work`),
  so there are zero cross-origin issues.
- **Cloudflare Tunnel** gives HTTPS for free with no open ports, no nginx,
  no Let's Encrypt. `cloudflared` runs as a Docker service alongside the
  bot and NestJS.

### What Google code becomes dead

Once the web app replaces Sheets/Drive, these modules are no longer needed
(disable first via `GSHEETS_ENABLED=false`, `GDRIVE_ENABLED=false`; delete
in a later cleanup):

| Module | What it did | Replaced by |
|--------|-------------|-------------|
| `hunter/gsheets_sync.py` | Mirror tracker rows to Sheets | Web app reads tracker.db directly |
| `hunter/gsheets_client.py` | Sheets API v4 wrapper | — |
| `hunter/gdrive_sync.py` | Upload folders to Drive | NestJS serves Applications/ directly |
| `hunter/gdrive_client.py` | Drive API v3 wrapper | — |
| `hunter/drive_ledger.py` | Shadow upload dedup | — |
| `hunter/sent_normalizer.py` | Parse Sent → Sheets col L | Web app shows Sent directly from DB |
| `hunter/cost_writer.py` | Write cost to Sheets col M | Web app shows cost_usd from DB |
| `hunter/verdict_writer.py` | Write verdict to Sheets col N | Web app shows ats_verdict from DB |
| `hunter/oauth_alert.py` | Google OAuth token expiry | No more Google OAuth |
| `hunter/schedules/gsheets.py` | Sheets resync/pull schedule | — |
| `hunter/schedules/gdrive.py` | Drive upload schedule | — |
| `hunter/schedules/normalize_sent.py` | Sheets col L refresh | — |
| `hunter/commands/gsheets.py` | /gsheets_status, /gsheets_push_* | — |
| `hunter/commands/gdrive.py` | /gdrive_upload_missing | — |
| `hunter/commands/normalize.py` | /normalize | — |
| `hunter/delivery.py` | Post-apply Sheets+Drive push | Bot writes DB; web app reads it |

Also unnecessary: `gsheets_credentials.json`, `gsheets_token.json`,
`gsheets_state.json` (all Google OAuth artifacts).

**Keep:** `gmail_client.py` / `gmail_token.json` — Gmail job alert source is
independent from Sheets/Drive.

---

## Phase A — Development Steps

### A0: Infrastructure (1-2 days)

**Goal:** NestJS skeleton running in Docker alongside the bot, serving
Angular static files + API, reachable via Cloudflare Tunnel.

1. Create `job-hunter-api` repo: `nest new job-hunter-api`
2. Configure NestJS to serve Angular dist as static files:
   ```typescript
   // main.ts — serve Angular SPA
   app.useStaticAssets(join(__dirname, '..', 'public'));
   // All non-API routes → index.html (SPA fallback)
   ```
3. Dockerfile: multi-stage build
   - Stage 1: `npm run build` Angular → `dist/job-hunter-site/browser/`
   - Stage 2: `npm run build` NestJS → `dist/`
   - Stage 3: production image, copy both dists
   (Angular source lives in `job-hunter-site` repo; either git-submodule,
   or CI clones both repos. Decide at implementation time.)
4. Add to existing `docker-compose.yml`:
   ```yaml
   job-hunter-api:
     build: ../job-hunter-api
     volumes:
       - ./tracker.db:/app/data/tracker.db    # read-write
       - ./Applications:/app/data/Applications:ro
     environment:
       - DB_PATH=/app/data/tracker.db
       - FILES_PATH=/app/data/Applications
       - JWT_SECRET=${JWT_SECRET}
     # No ports exposed — cloudflared handles routing

   cloudflared:
     image: cloudflare/cloudflared:latest
     command: tunnel run
     environment:
       - TUNNEL_TOKEN=${CLOUDFLARE_TUNNEL_TOKEN}
     # Routes job-hunter.igrflex.work → job-hunter-api:3000
   ```
5. Set up Cloudflare Tunnel (one-time, in igrflex@gmail.com dashboard):
   - Zero Trust → Networks → Tunnels → Create
   - Copy tunnel token → `CLOUDFLARE_TUNNEL_TOKEN` in `.env`
   - Add public hostname: `job-hunter.igrflex.work` → `http://job-hunter-api:3000`
   - DNS CNAME auto-created by Cloudflare
6. Verify: `curl https://job-hunter.igrflex.work/health` returns 200

**Deliverable:** `GET /health` + Angular starter page at `job-hunter.igrflex.work`

### A1: Auth module (1-2 days)

**Learning goal:** NestJS modules, services, guards, JWT, bcrypt

1. `users` table via TypeORM (SQLite driver — NestJS has its OWN SQLite DB
   for users, separate from tracker.db; no write contention):
   ```typescript
   @Entity()
   export class User {
     @PrimaryGeneratedColumn('uuid') id: string;
     @Column({ unique: true }) email: string;
     @Column() password: string; // bcrypt
     @CreateDateColumn() createdAt: Date;
   }
   ```
2. AuthModule: register, login (returns JWT), `/auth/me` (JWT guard)
3. Seed the owner's account via env var or migration
4. CORS config for `igrflex.work`

**Deliverable:** `POST /auth/register`, `POST /auth/login`, `GET /auth/me`

### A2: Applications API — read tracker (2-3 days)

**Learning goal:** TypeORM raw queries (reading a foreign SQLite DB),
pagination, sorting, filtering

NestJS connects to the bot's tracker.db as a SECOND database (read-only
connection). This is the critical integration point.

1. TrackerModule: read-only service over tracker.db
   ```typescript
   // TypeORM can open a second SQLite connection alongside its own DB
   @Module({
     imports: [
       TypeOrmModule.forRoot({
         name: 'tracker',
         type: 'better-sqlite3',
         database: process.env.DB_PATH,
         // No entities — raw queries only (bot owns the schema)
       }),
     ],
   })
   ```
2. Endpoints:
   ```
   GET /applications          — paginated list (sort, filter by status/date/company)
   GET /applications/:id      — single row detail
   PATCH /applications/:id    — update Sent, To Learn, Re-application (the 3 user-editable fields)
   GET /applications/stats    — counts by status (applied/sent/failed/expired/pending)
   GET /applications/funnel   — funnel data (same logic as hunter/funnel.py)
   ```
3. The PATCH endpoint writes to tracker.db (upgrade mount to read-write).
   Same fields that Google Sheets pull currently updates — no new writes.

**Deliverable:** `GET /applications` returns the full tracker in JSON

### A3: Files API — serve Applications/ (1-2 days)

**Learning goal:** NestJS file streaming, static assets, path security

1. FilesModule: controller that serves files from `Applications/`
   ```
   GET /files                         — list date folders
   GET /files/:date                   — list company folders for a date
   GET /files/:date/:company          — list files in a company folder
   GET /files/:date/:company/:file    — download/stream a file
   ```
2. Path traversal protection (sanitize `..` and absolute paths)
3. Content-Type headers for PDF/DOCX/TXT/JSON
4. Optional: PDF inline preview (Content-Disposition: inline)

**Deliverable:** browser can list and download any generated file

### A4: Analytics API (1 day)

**Learning goal:** SQL aggregation queries

1. AnalyticsModule: replicates `hunter/funnel.py` logic in SQL
   ```
   GET /analytics/funnel?days=30      — tracked→generated→sent→confirmed→answered
   GET /analytics/per-source          — breakdown by source
   GET /analytics/cost?days=30        — total/median/tail cost_usd
   GET /analytics/timeline?days=90    — applications per day/week
   ```

**Deliverable:** analytics data available as JSON

### A5: Angular — Login + Applications table (3-4 days)

**Learning goal:** Angular 19 features (signals, standalone components),
HttpClient, reactive forms, route guards

This is the BIG frontend step — the applications table that replaces
Google Sheets.

1. Core infrastructure in `job-hunter-site`:
   - `core/auth/` — AuthService (JWT in localStorage), AuthGuard,
     AuthInterceptor (attach JWT to requests)
   - `core/api/` — ApiService (base URL: `/api` — same origin, no CORS)
   - `environments/` — apiUrl per env (dev: `localhost:3000`, prod: `/api`)
2. Login page (`/login`) — email + password form
3. Applications table (`/applications`):
   - Full data table with all tracker columns
   - Sortable columns (Date, Company, ATS %, Verdict)
   - Filter by status (Applied/Sent/Failed/Expired/Pending)
   - Search by company/title
   - **Inline edit** for Sent, To Learn, Re-application (click cell → input → save)
   - Status badges with colors (green=sent, red=failed, grey=expired)
   - Click URL → opens job page in new tab
   - Click Folder → navigates to `/files/{date}/{company}`
**UI framework:** Angular Material (familiar from Angular ecosystem) or
PrimeNG (richer data table). Decision at implementation time.

**SPA routing:** NestJS handles this — all non-`/api/*` and non-`/auth/*`
routes serve `index.html`, Angular router takes over client-side.

**Deliverable:** user logs in, sees their tracker table, edits Sent dates

### A6: Angular — Files browser (2-3 days)

**Learning goal:** File tree UI, PDF preview, routing

1. Files page (`/files`):
   - Tree view: dates → companies → files
   - File list with icons (PDF, DOCX, TXT, JSON)
   - Click file → download (DOCX) or inline preview (PDF)
   - Per-company folder view with all generated docs
   - Link from Applications table Folder column → jump to company's files
2. PDF preview component (embedded `<iframe>` or pdf.js)

**Deliverable:** user browses and downloads all generated documents

### A7: Angular — Statistics (1-2 days)

**Learning goal:** Charts (Chart.js or ngx-charts)

1. Stats page (`/stats`):
   - Funnel chart (tracked → generated → sent → confirmed → answered)
   - Per-source breakdown table
   - Cost summary (total, median, last 7/30 days)
   - Timeline chart (applications per day/week)
2. Dashboard cards on the main page after login

**Deliverable:** user sees application funnel and cost analytics

### A8: Google removal + deploy (1-2 days)

1. Set `GSHEETS_ENABLED=false`, `GDRIVE_ENABLED=false` in bot's `.env`
2. Verify bot still works without Google (it should — both are best-effort)
3. Full docker-compose with all three services:
   ```yaml
   services:
     job-hunter:         # Python bot (existing)
     job-hunter-api:     # NestJS + Angular static files
     cloudflared:        # Tunnel → job-hunter.igrflex.work
   ```
4. Smoke test: bot applies → row appears in web table → files visible
5. Switch `job-hunter.igrflex.work` DNS from Cloudflare Pages CNAME to the
   tunnel CNAME (if not already done in A0)
6. Delete the old Cloudflare Pages project `job-hunter-site` (optional cleanup)

**Deliverable:** Google Sheets and Drive fully replaced, app live at
`job-hunter.igrflex.work`

---

## Phase A summary

| Step | Days | Deliverable |
|------|------|-------------|
| A0: Infrastructure | 1 | NestJS in Docker + Tunnel |
| A1: Auth | 1-2 | Login/register API |
| A2: Applications API | 2-3 | Tracker read/write API |
| A3: Files API | 1-2 | File listing + download |
| A4: Analytics API | 1 | Funnel/cost/timeline data |
| A5: Angular table | 3-4 | Applications table (= Sheets) |
| A6: Angular files | 2-3 | File browser (= Drive) |
| A7: Angular stats | 1-2 | Charts + funnel |
| A8: Google removal | 1-2 | Disable Sheets/Drive |
| **Total** | **13-20** | **Google fully replaced** |

### What the user gets after Phase A

1. Opens `igrflex.work` → logs in with email/password
2. **Applications page** — same tracker table as Google Sheets, but:
   - Faster (no Sheets API latency, no sync delays)
   - Editable inline (Sent, To Learn, Re-application)
   - Always up-to-date (reads tracker.db directly)
   - Sortable, filterable, searchable
3. **Files page** — browse `Applications/` folder, preview PDFs, download DOCXs
4. **Stats page** — funnel chart, per-source breakdown, cost summary
5. Telegram keeps working exactly as before (notifications + commands)
6. No more Google account dependency

---

## Phase B — Multi-user (outline, detailed later)

Phase B adds support for multiple users. Only start after Phase A is stable
and the owner has used the web UI daily for at least a week.

### B0: PostgreSQL migration (3-5 days)

- Add PostgreSQL to docker-compose
- NestJS switches its own DB from SQLite to PostgreSQL (TypeORM config change)
- Migrate bot's tracker.db schema to PostgreSQL:
  - New `hunter/db_postgres.py` (or adapt `hunter/db.py` with a driver switch)
  - Import existing data
  - All bot queries gain `WHERE user_id = $1`
- NestJS reads/writes the SAME PostgreSQL (no more second-DB hack)

### B1: User model + per-user data (3-4 days)

- `user_profiles` table (replaces candidate.yaml — already done in bot as
  `hunter/candidate.py`, but currently file-based)
- `user_filters` table (replaces filter_config.py)
- `user_employers` table (replaces hardcoded employer lists)
- `user_settings` table (LLM keys, Telegram chat_id, behavior flags)
- Bot's `candidate.load()` gains a PostgreSQL backend when `DATABASE_URL` set

### B2: Per-user bot schedule (2-3 days)

- `_hunt_lock` becomes per-user (Dict[user_id, asyncio.Lock])
- Schedule stagger per user (not just per source)
- `config_kv` table per-user for runtime toggles (/llm, /dual, /tracks)

### B3: File storage (2-3 days)

Options (decide at B-time):
- **Local disk per-user**: `Applications/{user_id}/{date}/{company}/` —
  simplest, works with the existing NestJS files controller
- **Cloudflare R2**: object storage, S3-compatible. Better for scale but
  adds complexity. Worth it only if 5+ users.

### B4: Telegram per-user routing (2-3 days)

- Link code flow: web UI → NestJS generates code → user sends `/link CODE`
  to bot → bound
- Bot routes all notifications by `chat_id → user_id`
- Commands respect user context

### B5: Angular multi-user pages (2-3 days)

- Registration page (currently seeded, now open)
- Profile editor (name, city, employers, candidate_profile.md, base CVs)
- Filter settings editor
- LLM provider/model/key settings

### Phase B summary

| Step | Days | Deliverable |
|------|------|-------------|
| B0: PostgreSQL | 3-5 | Bot + NestJS on PostgreSQL |
| B1: User model | 3-4 | Per-user profiles/filters/settings |
| B2: Per-user bot | 2-3 | Per-user hunt schedule + locks |
| B3: File storage | 2-3 | Per-user file isolation |
| B4: Telegram routing | 2-3 | Per-user notifications |
| B5: Angular pages | 2-3 | Profile/settings editors |
| **Total** | **15-21** | **Multi-user working** |

### What the friend gets after Phase B

1. Opens `igrflex.work` → registers with email/password
2. Fills in their profile: name, city, stack, languages, employers
3. Writes their candidate_profile.md and base_cv in the web editor
4. Configures filters (keywords, locations, exclusions)
5. Links their Telegram (sends `/link CODE` to the bot)
6. Bot starts hunting for them on the next cycle
7. Gets Telegram notifications + browses jobs/applications in web UI
8. Sees their own applications, funnel stats, generated files

---

## Already done (from old plan)

These items from the original plan are already implemented in the bot:

- **candidate.yaml / hunter/candidate.py** — fully implemented with
  `load()`, `get(dotpath, default)`, per-module consumption. Phase 3 of
  the old plan estimated 3-4 days; this is done. For Phase B, the only
  change is adding a PostgreSQL backend to `candidate.load()`.
- **filter_config.py** — split out of config.py, structured FILTER dict.
  For Phase B, moves to user_filters DB table.
- **APPLY_QUEUE_ENABLED / apply_worker** — PENDING/IN_PROGRESS queue
  already in tracker.db. For Phase B, needs `user_id` on pending rows.

## What stays OUT of scope

- Payment/billing — this is for friends, not a SaaS
- Admin panel — overkill for 2-3 users
- Mobile app — Angular PWA is enough
- Rewriting the Python scraping/LLM engine in TypeScript
- Google Sheets/Drive as a feature (once replaced, no going back)
- Real-time WebSocket for Phase A (polling every 30s is enough; add
  WebSocket in Phase B if needed)
- Cloudflare Pages deployment — NestJS serves everything from the VPS
- Separate frontend domain — one origin, no CORS

---

## Key risks and mitigations

| Risk | Mitigation |
|------|-----------|
| SQLite concurrent access (bot writes + NestJS reads/writes) | WAL mode handles this. NestJS writes are rare (3 editable fields). Same as Sheets pull today. |
| Cloudflare Tunnel reliability | Tunnel is production-grade (Cloudflare uses it themselves). Fallback: A-record `job-hunter.igrflex.work` → `178.105.131.107` + nginx + Let's Encrypt. |
| NestJS learning curve slows Phase A | NestJS is Angular-shaped (decorators, DI, modules). Auth + CRUD is day-1 NestJS territory. The hard parts (PostgreSQL migration, per-user isolation) are deferred to Phase B. |
| Phase A "good enough" kills motivation for Phase B | Phase A is genuinely useful on its own. Phase B only starts when there's a real second user waiting. |
| Bot code changes break during Phase B | Phase A requires ZERO bot changes. Phase B's bot changes are isolated behind `DATABASE_URL` — SQLite path stays as fallback. |

---

## NestJS module structure (Phase A, minimal)

```
job-hunter-api/
├── src/
│   ├── auth/                # JWT, login, register
│   │   ├── auth.module.ts
│   │   ├── auth.controller.ts
│   │   ├── auth.service.ts
│   │   ├── jwt.strategy.ts
│   │   └── dto/
│   ├── applications/        # Read/edit tracker.db
│   │   ├── applications.module.ts
│   │   ├── applications.controller.ts
│   │   └── applications.service.ts
│   ├── files/               # Serve Applications/ folder
│   │   ├── files.module.ts
│   │   └── files.controller.ts
│   ├── analytics/           # Funnel, stats, cost
│   │   ├── analytics.module.ts
│   │   └── analytics.service.ts
│   ├── health/              # Health check endpoint
│   │   └── health.controller.ts
│   └── app.module.ts
├── test/
├── Dockerfile
├── .env.example
├── nest-cli.json
├── tsconfig.json
└── package.json
```

Only 4 feature modules for Phase A. Profile, filters, settings, telegram,
jobs, users — all deferred to Phase B.

## Angular page structure (Phase A, in job-hunter-site)

```
job-hunter-site/src/app/
├── core/
│   ├── auth/
│   │   ├── auth.service.ts        # JWT, login/logout
│   │   ├── auth.guard.ts          # Route protection
│   │   └── auth.interceptor.ts    # Attach JWT to requests
│   └── api/
│       └── api.service.ts         # Base HTTP client
├── features/
│   ├── login/                     # Login page
│   │   └── login.component.ts
│   ├── applications/              # Tracker table (= Sheets)
│   │   ├── applications.component.ts
│   │   └── application-row/       # Inline-editable row
│   ├── files/                     # File browser (= Drive)
│   │   ├── files.component.ts
│   │   └── file-preview/          # PDF inline viewer
│   └── stats/                     # Funnel + analytics
│       ├── stats.component.ts
│       ├── funnel-chart/
│       └── cost-summary/
├── shared/                        # Common UI components
├── app.routes.ts
├── app.config.ts
└── app.ts
```

4 pages: login, applications, files, stats. No dashboard, no profile editor,
no settings — all Phase B.

## API endpoints (Phase A)

```
# Auth
POST   /auth/register              — email + password → user created
POST   /auth/login                 — email + password → JWT
GET    /auth/me                    — current user (JWT required)

# Applications (tracker table)
GET    /applications               — paginated list
       ?page=1&limit=50
       &sort=date&order=desc
       &status=applied|sent|failed|expired
       &search=company+name
GET    /applications/:id           — single row
PATCH  /applications/:id           — update Sent | To Learn | Re-application
GET    /applications/stats         — {total, applied, sent, failed, expired, pending}
GET    /applications/funnel        — funnel data
       ?days=30

# Files
GET    /files                      — list date folders [{name, count, date}]
GET    /files/:date                — list company folders [{name, files_count}]
GET    /files/:date/:company       — list files [{name, size, type}]
GET    /files/:date/:company/:file — download/stream file

# Analytics
GET    /analytics/funnel?days=30   — tracked→generated→sent→confirmed→answered
GET    /analytics/per-source       — per-source breakdown
GET    /analytics/cost?days=30     — total, median, p95 cost_usd
GET    /analytics/timeline?days=90 — applications per day

# Health
GET    /health                     — {status: "ok", db: "connected"}
```
