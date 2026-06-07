# BOT_MULTIUSER_PLAN.md — Multi-Tenant Telegram Bot (no website yet)

> Turn the current **single-owner** Telegram bot into a **multi-user public bot**:
> anyone can `/start`, upload their CV, set their dream-job search, and receive
> tailored CV + cover letter + notifications — all inside Telegram. **No website.**
>
> Decisions (owner-confirmed):
> - **Path B** — proper multi-tenant: one shared scraped `jobs` pool + `user_id`
>   on all per-user data. Scales, and is the same foundation a website would sit on later.
> - **Scale target:** tens-to-hundreds of users.
> - **Apply model:** assist (generate docs, user applies himself).
> - **Monetization:** free tier + paid tier.
> - **UI = Telegram.** A website ([SAAS_PLAN.md](SAAS_PLAN.md)) is a later skin over the
>   same DB; nothing here is thrown away when it's built.
>
> Status: **PLAN — not implemented.** The current single-owner bot keeps running
> untouched until each stage lands behind it.

---

## 0. Why the bot is enough (no website needed)

Telegram already gives us, for free, everything the website was being built for:

| Need | Website would build | Telegram gives free |
|---|---|---|
| Identity / auth | login, JWT, OAuth | `chat_id` = unique user |
| Onboarding (upload CV) | upload form | user sends CV as a document |
| Set "dream job" | form | `/setsearch` message |
| Deliver results | results table + download | PDF/DOCX sent into the chat |
| Notifications | email / push | native push into the chat |
| Billing | Stripe Checkout | Telegram Payments (or Stripe link) |

So the **only** thing a website saves us is a nicer table view — deferrable for a long
time. The expensive parts (multi-tenant data layer + central scraping) are needed either
way, and we build them now.

## 0b. The one thing that does NOT come free: scraping at scale

Tens-to-hundreds of users means we **cannot** scrape 17 boards per user (instant bans /
HTTP 429 — already a problem for one user at pracuj.pl). So even for a bot, we flip to:

```
scrape 17 boards ONCE → shared `jobs` pool (DB)
  → per-user matcher applies each user's saved criteria → per-user `applications`
```

Scraping is a shared background worker, independent of how many users exist.

---

## 1. Target shape (bot-first)

```
                Telegram (the UI)
                      │
            ┌─────────▼──────────┐
            │  Bot process        │  routes every update by chat_id → user
            │  (python-telegram-  │  /start /profile /setsearch /jobs /apply /billing
            │   bot, async)       │  per-user notifications (no hardcoded chat_id)
            └─────────┬──────────┘
                      │
        ┌─────────────▼──────────────┐
        │  Postgres (multi-tenant)   │  users · profiles · search_queries
        │  shared `jobs` pool         │  applications(user_id) · documents · usage
        └──────┬──────────────┬──────┘
               │              │
   ┌───────────▼──┐    ┌──────▼─────────────────────────┐
   │ Scheduler /  │    │ Workers (async tasks / JobQueue │
   │ JobQueue     │───▶│  or Arq+Redis if load grows):   │
   └──────────────┘    │  • scraper  (cron → shared pool) │ ← reuse hunter/sources/*
                       │  • matcher  (per-user filter)     │ ← reuse hunter/filters as engine
                       │  • apply    (LLM → CV/CL → PDF)   │ ← reuse apply_api/generate_docs
                       │  • notifier (Telegram per user)   │ ← reuse bot/notifications pattern
                       └───────────────────────────────────┘
        generated docs → object storage (R2/S3) or local disk (MVP) → sent via Telegram
```

**Reuse vs rebuild** is the same as SAAS_PLAN §1 — apply-core and scrapers survive;
`tracker.db`/`gsheets`/`gdrive` and the global `FILTER`/`candidate_profile.md` get replaced.
The difference from SAAS_PLAN: **no React frontend, no web auth — the bot is the client.**

---

## 2. Data model

Same Postgres schema as [SAAS_PLAN.md](SAAS_PLAN.md) §2, with one bot-specific addition:
`users.telegram_chat_id` is the primary identity key (no email/password needed for MVP).

```sql
users(
  id               uuid pk,
  telegram_chat_id bigint unique not null,   -- THE identity for the bot
  telegram_username text,
  email            citext,                    -- optional, for billing receipts
  tier             text default 'free',       -- 'free' | 'pro'
  created_at       timestamptz,
  deleted_at       timestamptz                -- GDPR soft-delete
)
profiles(user_id uuid unique, raw_cv_text, structured jsonb, extra_info, ...)
search_queries(user_id uuid, title, keywords[], exclude_patterns[], locations[], seniority[], active)
jobs(id, source, url, url_norm unique, company, title, location, description, posted_at, expired)  -- SHARED
applications(user_id, job_id, status, match_score, cv_doc_id, cl_doc_id, applied_at, unique(user_id,job_id))
documents(user_id, kind, storage_key, filename)
usage_counters(user_id, period date, generations int, primary key(user_id, period))
```

Two-level dedup: pool-level `jobs.url_norm` (scraped once) + per-user
`applications(user_id, job_id)`. Reuse `normalize_url` / `dedup_key` from current `tracker.py`.

---

## 3. Roadmap (bot-first, staged)

Each stage is shippable and testable in Telegram; no stage requires a website.

### Stage 0 — Infra
- [ ] Add Postgres (Docker Compose service); keep current bot running separately during build.
- [ ] Alembic migrations; CI runs them + tests.
- [ ] Extract the apply-core (`apply_api`, `apply_shared`, `generate_docs`, `llm_client`,
      `prompts/`) into an importable package usable by workers with **no module globals**
      (pass `profile` + `job` + `search_query` as params — partly done in Phase 4).

### Stage 1 — Multi-tenant data foundation **(do first)**
- [ ] Postgres schema above.
- [ ] Repository layer with `user_id` scoping for `users/profiles/search_queries/applications/documents`.
- [ ] Port dedup (`url_norm`) to pool-level + per-user.
- [ ] **Retire `tracker.db` / `gsheets_*` / `gdrive_*` for the service path** (keep old bot intact).

### Stage 2 — Bot identity & onboarding (Telegram UX)
- [ ] Remove the single-`TELEGRAM_CHAT_ID` gate; `/start` upserts a `users` row by `chat_id`.
- [ ] `UserContext` resolved from `chat_id` on every handler (replaces global config reads).
- [ ] Onboarding: user sends CV as a document → download → LLM parse → `profiles.structured`
      → bot echoes parsed summary → user confirms/edits (`/profile`).
- [ ] `/setsearch` flow → `search_queries` (title, keywords, locations, excludes).

### Stage 3 — Shared scraper + per-user matcher
- [ ] `scraper` worker on a schedule → writes shared `jobs` (dedup by `url_norm`),
      reuse `hunter/sources/*` + `DomainLimiter` (429 safety) + central expiry check.
- [ ] Refactor `filters.py` into an engine `match(job, search_query) -> score|None`
      (today it reads the module-global `FILTER`).
- [ ] `matcher` worker: for each active `search_query`, scan new `jobs` →
      insert `applications(status='new')`; per-user dedup.

### Stage 4 — Apply worker + delivery (assist)
- [ ] `apply` worker consumes `applications(status='new')` → generate tailored CV+CL
      (existing core, per-user profile) → store docs (local disk MVP, R2/S3 later) → `status='ready'`.
- [ ] `notifier`: push to the right user's chat — job card + PDF/DOCX files + "mark applied" button.
- [ ] `/jobs` lists the user's ready/new applications; `/apply <id>` triggers generation on demand.

### Stage 5 — Quotas & billing
- [ ] `usage_counters` enforcement: free-tier cap on generations/month → friendly "upgrade" message.
- [ ] Paid tier via Telegram Payments or a Stripe payment link → `users.tier='pro'`.

### Stage 6 — GDPR & ops
- [ ] Consent on `/start` (link to privacy policy + ToS), `/deleteme` → hard-delete docs + rows.
- [ ] Encryption at rest; DPA review with LLM provider (CV PII goes to the model).
- [ ] Per-user error isolation: one user's scraper/LLM failure must not kill the shared loop.

> A website ([SAAS_PLAN.md](SAAS_PLAN.md) Stage 4) becomes an **optional** add-on after
> Stage 4 here — it reads the exact same Postgres tables. Building the bot first does not
> create throwaway work.

---

## 4. What changes in the current code (concrete coupling points to break)

| Current single-tenant assumption | File | Becomes |
|---|---|---|
| One `TELEGRAM_CHAT_ID` auth + all notifications | `hunter/config.py:9`, `hunter/bot/notifications.py:30` | `users` registry; notify by row owner's `chat_id` |
| Global `FILTER` dict | `hunter/config.py:90` | per-user `search_queries`, matching engine takes criteria param |
| `candidate_profile.md` single file | `hunter/apply_api.py:216` | `profiles` row per user, passed as param |
| `tracker.db` (no `user_id`) | `hunter/tracker.py`, `tracker_cache.py` | Postgres `applications` scoped by `user_id` |
| Single Google Sheet/Drive | `hunter/gsheets_*`, `gdrive_*` | DB tables + Telegram delivery (+ object storage) |
| One global hunt JobQueue | `hunter/schedules/hunt.py` | shared scraper cron + per-user matcher |

---

## 5. Risks (unchanged from SAAS_PLAN — still apply to a public bot)

1. **Legal — scraping at commercial scale** (even assist-only). Prefer official APIs,
   low central rate, be ready to drop a source. Get legal advice before public launch.
2. **GDPR** — storing third parties' CVs in the EU. Consent, deletion, encryption, LLM DPA.
   Launch blocker if skipped.
3. **LLM cost / abuse** — quotas + rate limits from day one; BYO-key as an escape hatch if needed.
4. **Scraper fragility** — 17 boards break independently; central pool + per-source graceful
   degradation + the `scraper-health-checker` discipline.
5. **Cold-start** — keep the shared pool continuously fresh so a new user sees matches immediately.

---

## 6. Open questions (revisit before Stage 3)

- Worker engine: start on **python-telegram-bot JobQueue / asyncio tasks** (no new infra),
  move to **Arq + Redis** only when load needs it.
- Doc storage: local disk for MVP (sent via Telegram anyway) → R2/S3 when a website needs links.
- Postgres hosting: managed (Fly/Render/Supabase) vs self-hosted on the current VPS.
- Do we keep a per-user Google Sheet as an *optional* export, or drop Sheets entirely? — drop for MVP.

---

_Author: AI agent, 2026-06-07. This plan supersedes SAAS_PLAN.md as the **near-term** path;
SAAS_PLAN.md remains the long-term website vision over the same data model. Mirror schema/flow
changes into CLAUDE.md once implementation starts._
