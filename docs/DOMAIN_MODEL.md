# Domain Model — target entities, the `applications` mapping, DDL v1, module boundaries

**Status:** proposed, pending owner answers to `improvement-2026-09/03-ARCHITECTURE_PLAN.md`'s
open questions 1–4.
**Scope:** M1 of that plan — "the domain model as a document and contract, 1 commit, 0 code".
Records the target entities and where every existing column/table lands, so later milestones
(M2 `Settings`, M3 `core.tailor()`, M4 service+Postgres, …) have one place to check instead of
re-deriving the mapping from `hunter/db.py` each time. **Nothing here is implemented yet** —
§1 and §3 describe the target; §2 describes what exists today, verified against the actual code
in both repos.

---

## 1. Target entities

Named exactly as in `03-ARCHITECTURE_PLAN.md`'s "Целевая доменная модель" — nothing added or
renamed beyond that list.

| Entity | Owner | Key fields | Lifecycle | Invariants |
|---|---|---|---|---|
| **Account** | NestJS | id, email, password_hash, role, email_verified, disabled, telegram_chat_id, settings, created_at | registered → email_verified → (disabled \| active); Telegram binding: unlinked → linked (via a 10-min code) → unlinked | One `chat_id` maps to ≤ 1 account; a disabled account cannot enqueue a `Job` |
| **Profile** | NestJS write / service read | account_id, document (JSONB, `hunter/profile_schema.py::Profile`), schema_version, revision, updated_at | created on first `PUT /api/profile` → revised on every later PUT (rev++, old JSON kept, pruned at 20) | Must pass `profile_schema.validate()` (`REQUIRED_IDENTITY_FIELDS`) before it is renderable |
| **ProfileRender** | service (cache) | account_id, revision (which Profile.revision this reflects), file storage keys, rendered_at | stale (behind Profile.revision, or absent) → rendering → current | Deterministic function of exactly one Profile revision; `render_all` is a full overwrite, never a merge, including deleting files the profile no longer needs |
| **Vacancy** | service | url, url_norm, source, company, title, posting_text, lang, fetched_at, expired_at | discovered (url known) → fetched (posting_text present) → active \| expired | `url_norm` deterministic from `url`; **not tenant-scoped** — one posting shared by every account that applies to it (scraping stays shared, non-goal) |
| **Tailoring** | service | account_id, vacancy_id, profile_revision, track, status, prescore, verdict, cost_usd, refine_rounds, retry_count, is_reapplication, started/finished_at | pending → running (claimed) → applied \| skip \| fail \| expired \| manual | ≤ 1 non-abandoned Tailoring per (account_id, vacancy_id) — successor of today's unique `(user_id, url_norm)`; `pending`/`running` queue state belongs to `Job`, not here |
| **Document** | service | account_id, tailoring_id, kind (cv_en/cv_pl/cl_en/cl_pl/about_me/outreach), storage_key, sha256, rendered_at | rendered → delivered (via Outbox) \| superseded by a later refine round | storage_key resolved through one `DocumentStore` abstraction, never a raw filesystem path |
| **QualityReport** | service | account_id, tailoring_id, judge_report, lang_gate, scrubs, ats_check, verdict_score, skills_gap, created_at | one row per completed Tailoring; only the verdict-refine loop's best round is persisted | `verdict_score` is always the independent judge's score, never the generator's self-score |
| **Outcome** | service | tailoring_id (1:1), account_id, sent_at, confirmed_at, response, reported_by (`user`\|`gmail`), updated_at | unsent → sent → (confirmed) → no_reply \| rejection \| interview \| offer | `reported_by='gmail'` is owner-only (personal-inbox read, not sold — §3.4 of the pivot plan) |
| **Job** | service | account_id, kind (tailor/parse/render/preview), payload, status, result, error, claimed_at, created/updated_at | pending → running (atomic claim, `UPDATE … RETURNING`) → done \| error \| deferred | A stale `running` row resets to `pending` for the *same* id (crash recovery); `error` is terminal, a retry is a new row |
| **Outbox** | service | account_id, channel (email/telegram), recipient, payload_ref, payload, state, attempts, created_at, sent_at | pending → sent \| failed (bounded retries) | Only the drain worker writes `state`/`sent_at`/`attempts`; the producer only inserts |
| **Event** | service | id, account_id (nullable), type, props (JSONB), ts | append-only, no update/delete | `account_id IS NULL` is a legitimate system-level event, not a data-quality bug |
| **MarketSnapshot** | service (Stage 6) | role, region, period, aggregates (JSONB), computed_at | computed nightly; a period's row is replaced wholesale, never patched | **Not tenant-scoped**, same reasoning as Vacancy — does not exist in code yet |

Notes on entities with no code yet: **ProfileRender** exists today only as a filesystem side
effect of draining `profile_jobs(kind='render')` (`hunter/profile_render.py::render_all`), with
no DB row of its own. **Job** has one real instance today (`profile_jobs`, kinds
`render`/`parse`/`preview`) and one pre-entity instance (the apply queue's `PENDING`/
`IN_PROGRESS` state embedded in `applications.ats_status`, `HUNT_APPLY_SPLIT_PLAN.md` M1) that
the target model folds into the same shape. **Outbox**, **Event**, and **MarketSnapshot** do not
exist in either repo yet; closest analogues today are `hunter/delivery.py` (synchronous, no
queue), `hunter/source_health.py`'s `source_runs` / `hunter/best_effort.py`'s `subsystem_health`
(operational telemetry, not product analytics — deliberately *not* mapped onto Event, see §2.1e),
and nothing, respectively.

---

## 2. Mapping: every existing column/table → target entity

**Keep this section true:** a PR that adds/removes a column on `applications`, `profile_jobs`,
`telegram_links`/`telegram_link_codes`/`user_settings`, or the API's `users`/`profiles`/
`profile_revisions` updates this mapping in the same PR — the same discipline CLAUDE.md already
requires for `hunter/tracker.py`'s column-index constants.

### 2.1a `applications` (`hunter/db.py` — base `_DDL` + `_ensure_columns` migrations)

| Column | Owner today | Target entity.field | Notes |
|---|---|---|---|
| `id` | bot | `Tailoring.id` | 8-char hex UUID; doubles as the Sheets sync key |
| `date` | bot | `Tailoring.finished_at` (date part) | |
| `user_id` | bot (B1) | `Tailoring.account_id` | Backfilled from `DEFAULT_USER_ID` for pre-B1 rows |
| `company` | bot | `Vacancy.company` | Extracted by the generation LLM, not the scraper |
| `title` | bot | `Vacancy.title` | Same as above |
| `stack` | bot | `Tailoring.track` (ambiguous) | "Tech stack (from LLM)" — a generation-time observation, not scraper-intrinsic; could become `Vacancy`'s if a Vacancy-side stack-classification step is ever added |
| `ats_status` | bot | **overloaded — split 3 ways**, see below | |
| `url` | bot | `Vacancy.url` | |
| `url_norm` | bot | `Vacancy.url_norm` | Becomes the unique `(account_id, vacancy_id)` constraint on `Tailoring` |
| `folder` | bot | `Document.storage_key` (root) | |
| `sent` | bot | **overloaded**: `Outcome.sent_at` + `Vacancy`/`Tailoring` state | Free-text in practice: a real date, `EXPIRED` (Vacancy state leaking in), or `—` (SKIP/FAIL's "deliberately not sent", i.e. `Tailoring.status` leaking in) |
| `reapplication` | bot | `Tailoring.is_reapplication` | `+` flag from the re-post gate |
| `to_learn` | bot | `QualityReport.skills_gap` | |
| `drive_url` | bot | ReadModel/SheetsMirror bucket | Owner-only legacy Google Drive integration (§3.4), not part of target `Document`/storage |
| `confirmation` | bot | `Outcome.confirmed_at` | |
| `answer` | bot | `Outcome.response` | |
| `outcome_label` | bot (08-DATA_EVAL M1, `/outcome`) | `Outcome` lifecycle state (`no_reply \| rejection \| interview \| offer` in §1) | Today's labels are `tracker.OUTCOME_LABELS` = `interview`/`rejected`/`offer`/`silence`, written by `tracker.set_outcome()` (user-scoped); `silence` is an OBSERVED outcome but not a reply — `funnel._is_answered` counts only `OUTCOME_REPLY_LABELS`, `funnel._has_outcome` all four, which is what tells "zero replies" apart from "nothing recorded". Empty = not recorded (pre-M1 rows, or never labelled) |
| `outcome_at` | bot (08-DATA_EVAL M1) | `Outcome.updated_at` | Stamped by `set_outcome()` alongside the label; NULL until a label is recorded |
| `sheets_row` | bot | ReadModel/SheetsMirror bucket | |
| `sheets_dirty` | bot | ReadModel/SheetsMirror bucket | |
| `fail_count` | bot | `Tailoring.retry_count` | |
| `cost_usd` | bot | `Tailoring.cost_usd` | Re-stamped post-hoc after verdict/refine |
| `ats_verdict` | bot | `QualityReport.verdict_score` | Independent judge score, never the self-score |
| `claimed_at` | bot (M1 apply-queue) | `Job.claimed_at` | Pre-`Job`-entity primitive living on the wrong table today |
| `claimed_by` | bot (06-OPS M3 graceful stop) | `Job.claimed_by` (not yet in §1's `Job` field list — add it there when the entity is built) | `hostname:pid` of the worker holding an `IN_PROGRESS` claim (`hunter.apply_worker.claimed_by_tag()`), stamped in the same atomic UPDATE as `claimed_at`; read once at startup by `tracker.release_claims_by_host()` so a restarted container frees its own claims immediately instead of waiting out `APPLY_CLAIM_TIMEOUT_MIN`. Empty when never claimed or since released |
| `pending_meta` | bot (M1 apply-queue) | `Job.payload` | Full serialized `Job` dataclass, JSON |
| `skip_reason` | bot (MARKET_MEMORY M2) | `Tailoring.skip_reason` | `<prefix>[:<detail>]` over `tracker.SKIP_REASON_PREFIXES` — why a `SKIP` `Tailoring.status` was reached; empty on pre-M2 rows; never mirrored to the Sheet |
| `source` | bot (MARKET_MEMORY M3) | `Vacancy.source` | Which hunt source surfaced the vacancy, written at INSERT (`Job.source` when it is a registered source name, else the `postings_seen` row for the same `url_norm`); empty on pre-M3 rows (no backfill, owner decision 2026-09-12) — `hunter/funnel.py` falls back to its URL guess for blanks; never mirrored to the Sheet |
| `app_status` | **API** (`tracker-migrations.ts`, absent from `hunter/db.py`) | `Outcome.response` (parallel input) | Manual status set from the website dropdown; bot never reads/writes it — a bot-only column scan would miss this one |

**The `ats_status` overload:** (1) a real score (`"85%"`) → `QualityReport.verdict_score` /
`Tailoring.status='applied'`; (2) `SKIP`/`FAIL`/`MANUAL`/`EXPIRED` → `Tailoring.status`
(terminal); (3) `PENDING`/`IN_PROGRESS` → `Job.status` — an apply-queue Job's state stored on the
Tailoring row for lack of a `Job` entity. Splitting `Job` out (M4) is what finally lets this
column mean only "how did the application conclude".

### 2.1b `profile_jobs` (mirrored from the API's `tracker-migrations.ts`; API writes, bot drains
via `hunter/profile_jobs.py`)

| Column | Target | | Column | Target |
|---|---|---|---|---|
| `id` | `Job.id` | | `result` | `Job.result` |
| `user_id` | `Job.account_id` | | `error` | `Job.error` |
| `kind` (render/parse/preview) | `Job.kind` | | `created_at` | `Job.created_at` |
| `payload` | `Job.payload` | | `updated_at` | `Job.updated_at` |
| `status` | `Job.status` | | | |

Already matches the target `Job` shape almost field-for-field — the Postgres `jobs` table (§3)
generalizes it to a fourth `kind='tailor'`, not a new invention.

### 2.1c `telegram_links` / `telegram_link_codes` / `user_settings`

| Table.column | Target |
|---|---|
| `telegram_links.chat_id` | `Account.telegram_chat_id` (also an `Outbox` recipient for channel `telegram`) |
| `telegram_links.user_id` | `Account.id` |
| `telegram_links.linked_at` | `Account` metadata |
| `telegram_link_codes.*` | `Account` onboarding — ephemeral, no lasting footprint once linked |
| `user_settings.user_id/key/value/updated_at` | `Account.settings` (per-account override namespace: `AUTO_APPLY`, `CANDIDATE_TRACKS`, `hunting_enabled`, source toggles) |

### 2.1d `config` (global key/value, `hunter/config.py`/`hunter/llm_profiles.py` — no `user_id`,
not part of the multi-user contract)

Not one of the eleven entities, and deliberately not forced onto one: `tracks_enabled` /
`dual_apply_enabled` / `dual_shadow_profile` are choice-shaped and belong in `Account.settings`
once this table is per-account; `llm_outage_until`/`llm_outage_streak_since` are an LLM-provider
circuit breaker — service-level operational state, not domain data for any one account. Flagged
so a future PR does not silently drop it while migrating `applications`.

### 2.1e `subsystem_health`, `source_runs`, `drive_uploads` (own lazy `CREATE TABLE IF NOT
EXISTS`, outside `hunter/db.py::init_db()`)

None map onto a target entity — infrastructure telemetry (best-effort failure counters, scraper
yield history, a Drive-upload dedup ledger) that stays local operational state regardless of the
Postgres migration. `source_runs` stays outside `Event` on purpose: `Event` is product analytics
keyed by `account_id`; these three are keyed by subsystem/source/path and answer "is our
infrastructure healthy", not "what did an account do". `drive_uploads` retires with the Google
integration for customers (§3.4).

### 2.2 `hunter/profile_schema.py::Profile` (a JSON document, not a table)

Maps onto `Profile.document` wholesale — the whole dataclass tree (`core.*`, `variants`,
`leftovers`, `uploads`) is opaque JSONB, matching `RESUME_PROFILE_STORE.md`'s own "the API
treats the document as opaque beyond the structural checks" — stays opaque in the target model.

### 2.3 Cross-check: `hunter/db.py` vs `docs/MULTI_USER_UPDATE.md`'s "Shared contract"

Verified byte-for-byte identical for `user_settings`/`telegram_links`/`telegram_link_codes` and
the `applications.user_id` migration + index. No drift found as of this pass.

### 2.4 API repo's `app.sqlite` (`src/db/migrations.ts`)

| Table.column | Target |
|---|---|
| `users.id/email/password/role/email_verified/disabled/created_at` | `Account.id/email/password_hash/role/email_verified/disabled/created_at` |
| `email_verification_tokens.*` | `Account` onboarding — ephemeral, same treatment as `telegram_link_codes` |
| `profiles.user_id` | `Profile.account_id` (PK — one live Profile per account today) |
| `profiles.json/schema_version/revision/updated_at` | `Profile.document/schema_version/revision/updated_at` |
| `profile_revisions.user_id/rev/json/created_at` | `Profile` revision history (append-only, pruned to 20) |

---

## 3. Proposed PostgreSQL DDL v1

**Proposed, pending owner answers to open questions 1–4.** Every tenant-scoped table carries
`account_id`; the two deliberate exceptions (`vacancies`, `market_snapshots`) match the
"shared, not per-account" reasoning from §1. Unique constraints reproduce today's SQLite
indexes — `idx_user_url_norm` → `ux_tailorings_account_vacancy`.

```sql
-- Account (NestJS-owned; listed here per the П3 amendment — single migration owner via
-- Alembic in the bot repo. NestJS keeps writing this table, just doesn't migrate it.)
CREATE TABLE accounts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email             TEXT NOT NULL UNIQUE,
    password_hash     TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'user',
    email_verified    BOOLEAN NOT NULL DEFAULT FALSE,
    disabled          BOOLEAN NOT NULL DEFAULT FALSE,
    telegram_chat_id  BIGINT UNIQUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```sql
-- Profile (+ProfileRender)
CREATE TABLE profiles (
    account_id      UUID PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    document        JSONB NOT NULL,      -- hunter/profile_schema.py Profile, opaque here
    schema_version  INTEGER NOT NULL,
    revision        INTEGER NOT NULL DEFAULT 1,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE profile_revisions (
    account_id  UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    rev         INTEGER NOT NULL,
    document    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, rev)
);
CREATE TABLE profile_renders (
    account_id   UUID PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    revision     INTEGER NOT NULL,       -- which Profile.revision this cache reflects
    files        JSONB NOT NULL,         -- {"candidate.yaml": storage_key, ...}
    rendered_at  TIMESTAMPTZ NOT NULL
);
```

```sql
-- Vacancy — deliberately NOT tenant-scoped: one posting, shared across every account
-- that applies to it (scraping stays shared, per the plan's non-goals).
CREATE TABLE vacancies (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url           TEXT NOT NULL,
    url_norm      TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT '',
    company       TEXT NOT NULL DEFAULT '',
    title         TEXT NOT NULL DEFAULT '',
    posting_text  TEXT,
    lang          TEXT,
    fetched_at    TIMESTAMPTZ,
    expired_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_vacancies_url_norm ON vacancies(url_norm) WHERE url_norm != '';
```

```sql
-- Tailoring — direct successor of today's `applications` row once Vacancy/Document/
-- QualityReport/Outcome are split out of it.
CREATE TABLE tailorings (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id        UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    vacancy_id        UUID NOT NULL REFERENCES vacancies(id),
    profile_revision  INTEGER NOT NULL,
    track             TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|skip|fail|expired|manual
    prescore          NUMERIC(5,2),
    verdict           NUMERIC(5,2),
    cost_usd          NUMERIC(10,4),
    refine_rounds     INTEGER NOT NULL DEFAULT 0,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    is_reapplication  BOOLEAN NOT NULL DEFAULT FALSE,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Direct successor of today's idx_user_url_norm (user_id, url_norm):
CREATE UNIQUE INDEX ux_tailorings_account_vacancy ON tailorings(account_id, vacancy_id);
CREATE INDEX ix_tailorings_account_status ON tailorings(account_id, status);
```

```sql
-- Document
CREATE TABLE documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tailoring_id  UUID NOT NULL REFERENCES tailorings(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,       -- cv_en|cv_pl|cl_en|cl_pl|about_me|outreach
    storage_key   TEXT NOT NULL,
    sha256        TEXT,
    rendered_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_documents_tailoring ON documents(tailoring_id);
```

```sql
-- QualityReport
CREATE TABLE quality_reports (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id     UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    tailoring_id   UUID NOT NULL REFERENCES tailorings(id) ON DELETE CASCADE,
    judge_report   JSONB,
    lang_gate      JSONB,
    scrubs         JSONB,
    ats_check      JSONB,
    verdict_score  NUMERIC(5,2),
    skills_gap     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```sql
-- Outcome
CREATE TABLE outcomes (
    tailoring_id  UUID PRIMARY KEY REFERENCES tailorings(id) ON DELETE CASCADE,
    account_id    UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    sent_at       TIMESTAMPTZ,
    confirmed_at  TIMESTAMPTZ,
    response      TEXT NOT NULL DEFAULT '',   -- rejection|interview|offer|no_reply
    reported_by   TEXT NOT NULL DEFAULT '',   -- 'user' | 'gmail' (gmail = owner-only, §3.4)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```sql
-- Job — generalizes today's profile_jobs (render/parse/preview) plus the apply queue's
-- PENDING/IN_PROGRESS state (today embedded in applications.ats_status) into kind='tailor'.
CREATE TABLE jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,        -- tailor|parse|render|preview
    payload     JSONB NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error|deferred
    result      JSONB,
    error       TEXT,
    claimed_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ
);
CREATE INDEX ix_jobs_status_created ON jobs(status, created_at);
-- status='deferred' is the П2 amendment's outcome: a non-owner account's LLMOutageError
-- becomes a scheduled retry instead of a CLI-subscription fallback.
```

```sql
-- Outbox
CREATE TABLE outbox (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id   UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    channel      TEXT NOT NULL,        -- email|telegram
    recipient    TEXT NOT NULL,
    payload_ref  UUID,                 -- e.g. tailoring_id or document_id
    payload      JSONB NOT NULL DEFAULT '{}',
    state        TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at      TIMESTAMPTZ
);
CREATE INDEX ix_outbox_state ON outbox(state, created_at);
```

```sql
-- Event
CREATE TABLE events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  UUID REFERENCES accounts(id) ON DELETE SET NULL,  -- NULL = system-level event
    type        TEXT NOT NULL,
    props       JSONB NOT NULL DEFAULT '{}',
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_events_account_ts ON events(account_id, ts);
CREATE INDEX ix_events_type_ts ON events(type, ts);
```

```sql
-- MarketSnapshot — deliberately NOT tenant-scoped, same reasoning as Vacancy.
CREATE TABLE market_snapshots (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role         TEXT NOT NULL,
    region       TEXT NOT NULL,
    period       DATE NOT NULL,
    aggregates   JSONB NOT NULL,
    computed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_market_snapshots_role_region_period
    ON market_snapshots(role, region, period);
```

### The three ordering amendments, restated as decisions

| # | Decision | Rationale |
|---|---|---|
| П1 | **Postgres at Stage 2, not Stage 8.** Bot stays on SQLite through Stage 2; the new service is Postgres-native from day one; the only bridge is `Job` (bot enqueues over HTTP, never reads Postgres). `applications` in SQLite stays the owner's read-model until Stage 5. | Stage 2 already adds a third writer (the service) to the shared SQLite file; doing this on SQLite first and moving three writers to Postgres later is two migrations instead of one — `tracker.py` needs an ORM-capable rewrite regardless. |
| П2 | **API-only LLM path for any `account_id` other than the owner's.** `llm_client.py`'s CLI-subscription fallback on `LLMOutageError` is gated to `current_user_id() == DEFAULT_USER_ID`; for anyone else it becomes `Job.status='deferred'` with a scheduled retry + Outbox notification. | The CLI fallback runs on the owner's personal Claude subscription — serving a customer through it is commercial misuse of that subscription and a second pipeline that drifts from the API path. |
| П3 | **Single schema owner: all Postgres migrations live in one place (Alembic, bot repo), including NestJS-owned tables.** NestJS gets read access where needed and writes only its own tables, but does not migrate them. | Today's "idempotent DDL mirrored in two repos" (`hunter/db.py` mirroring `tracker-migrations.ts`) is drift by construction — `profile_jobs`/`telegram_link_codes` already depend on both sides staying manually in sync. |

---

## 4. Module boundaries (target)

```
hunter/
  core/         pure generation: (posting_text | None) + Profile + track → content.json + docs + scores
                NOTHING FROM: tracker, Telegram, Drive, Sheets, env, subprocess
  pipeline/     stages as today (gates, ats, scrubs, lang, abort) — called from core
  scenarios/    owner_apply (dedup, tracker row, Drive, Sheets, outreach)
                customer_tailor (credits check, Tailoring row, outbox)
                standalone_optimize (Stage 6)
  service/      FastAPI: POST /jobs, GET /jobs/{id}, /health; shared-secret auth;
                account_id from the trusted caller's header, never the request body
  workers/      job runner (claim → scenario → finish), outbox drainer, nightly market aggregates
  storage/      DocumentStore — one put/get/delete/list abstraction (fs today, R2 later)
  sources/      unchanged
  bot/, commands/, schedules/   Telegram — thin client of the service
```

| Today | Target | Note |
|---|---|---|
| `hunter/apply_api.py` (`main_api`) | `core/tailor()` + `scenarios/owner_apply.py` | M3: core extracted first; `main_api` stays a second path behind a flag until M4 |
| `hunter/apply_cli.py` | retired at M8, folds into `core` as an `LLMClient` CLI variant | Two hand-mirrored pipelines is CLAUDE.md's documented four-incidents-in-five-weeks problem |
| `hunter/pipeline/*` | unchanged | Already isolated (wave 1); `core.tailor()` calls it in the same order |
| `hunter/verdict_refine.py`, `claim_judge.py`, `ats_pdf_roundtrip.py` | called from `core/tailor()` | Produce `QualityReport` fields |
| `hunter/generate_docs.py` | `core`/`pipeline`, writing through `storage/DocumentStore` | M6: absolute `Applications/` paths become storage-key puts |
| `hunter/tracker.py` | `storage/` SQLite read-model + service's Postgres access layer | Dedup becomes `ux_tailorings_account_vacancy`; `applications` survives as read-model until M9 |
| `hunter/db.py` | Alembic migrations in the bot repo (П3) | SQLite DDL retires once Postgres is the source of truth (M9) |
| `hunter/config.py` | `hunter/settings.py` facade (M2), then injected `Settings` for `core`-candidates | Two-commit migration: facade first, injection second |
| `hunter/candidate.py`, `hunter/profile_render.py` | `core` takes a `Profile` object, not a `candidate.yaml` path | `profile_render.py` stays as cache-generation logic, fed by a DB-sourced `Profile` |
| `hunter/profile_schema.py` | unchanged | Already the canonical `Profile.document` contract |
| `hunter/profile_jobs.py` + `hunter/schedules/profile_jobs.py` | `workers/` (Job runner), generalized to `kind='tailor'` | First real instance of the target `Job` entity |
| `hunter/apply_worker.py` | `workers/` — stops executing inline, delegates to the service | M4: claim loop becomes an HTTP call into `POST /v1/jobs` |
| `hunter/delivery.py` | replaced by the Outbox drain worker (M5) | Synchronous Sheets/Drive calls become queued and retryable |
| `hunter/gsheets_sync.py`, `gdrive_sync.py`, `gmail_client.py` | ReadModel/SheetsMirror bucket, owner-only | Not part of the target domain model — legacy Google integrations, kept as-is until deprecated (§3.4) |
| `hunter/funnel.py` | projection/read-model query over `Tailoring` + `Outcome` | M9, once those tables exist |
| `hunter/llm_client.py` | `core`'s `LLMClient` abstraction; CLI fallback gated owner-only | M7, П2 |
| `hunter/bot/`, `commands/`, `schedules/` | largely unchanged | Thin client calling the service instead of running `apply_worker_loop` in-process |

---

## 5. How to keep this document true

Same rule CLAUDE.md applies to `hunter/tracker.py`'s column-index constants: **any PR that
adds, removes, or repurposes a column on `applications`, `profile_jobs`,
`telegram_links`/`telegram_link_codes`/`user_settings`, or the API's `users`/`profiles`/
`profile_revisions` updates §2's mapping in the same PR.** A PR that adds a real Postgres table
once the service exists adds its DDL to §3 and its module to §4's "Today → target" table. This
PR's throwaway self-check (a script asserting every `hunter/db.py` column appears in this file)
is a candidate for a real CI gate once the service work starts; until then it is a process rule.
