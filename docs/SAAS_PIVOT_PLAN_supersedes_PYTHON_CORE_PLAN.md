# SaaS Pivot Plan — supersedes PYTHON_CORE_PLAN.md

**Why this document replaces the previous one.** `PYTHON_CORE_PLAN.md` was written
to answer a technical question: "the Python logic lives in the bot — should it be
extracted?" A design interview then established that the actual goal is different
and much larger: **the project is becoming a product that is sold to other people.**
Once that was on the table, most of the old plan's conclusions inverted — its last
stage became blocking, its notifier design was too narrow, and it was missing a
database migration and an entire product pipeline. The old file is kept for history
and marked superseded; this document is the current plan.

Written 2026-08-12. Every codebase fact below was verified against the working tree
at `bot@978eb5b` / `api@feat/filters-api` — file and line references are included so
the next reader can re-check rather than trust.

---

## 1. What is being sold

The product is **resume tailoring**, not job search. Job search stays a personal
feature of the owner for now, and enters the product later.

The customer journey:

1. **Onboarding (one-off payment).** The customer uploads their existing CV
   (docx/pdf). The system parses it, rebuilds it into a single ATS-optimized layout,
   and shows the customer a confirmation screen to correct anything the parser got
   wrong. The output of this step is the customer's *base profile* — the equivalent
   of the owner's `candidate/base_cv_*.md` + `candidate.yaml`.
2. **Per-vacancy tailoring (credits).** The customer supplies a vacancy — by URL, by
   pasted text, or both — and receives a CV and cover letter tailored to it, plus a
   match percentage.
3. **Market-based optimization (separate paid tier).** The CV is optimized against an
   aggregated demand profile for the customer's role and region, computed from the
   owner's scraper across 25 job boards. This is the differentiator no competitor with
   a single LLM prompt can copy.

**Conversion hook:** the match score can be computed with **zero API cost**
(`hunter/ats_checker.py` runs at 75% keyword match + 25% TF-IDF when no LLM is
available). So the score is shown *before* payment — "your CV matches this vacancy
34%" — and after generation the customer sees the independent judge's verdict
(`applications.ats_verdict`, a Haiku call over the *rendered PDF text*, where the
judge does not know it is grading our own output). The before/after pair is the proof
of value the customer paid for.

**Explicitly not sold:** mailbox reading (Gmail), Google Sheets, Google Drive, job
search with personal filters. See §3.4 for why.

---

## 2. Target architecture

```
Angular site ──┐
               ├─► NestJS API ──(internal network + shared secret)──► Python service
Telegram bot ──┘   auth, users,                                       apply/tailor
                   settings, billing                                  sole executor
                        │                                                   │
                        └──────────────► database ◄────────────────────────┘
                              NestJS owns users/auth/settings
                              service owns applications/apply
                                          │
                                   outbox table
                                          │
                              delivery worker → email / telegram
```

**The Python service is the sole executor of apply/tailor work.** The Telegram bot
becomes a thin frontend that calls the same service over HTTP, exactly as NestJS
does. This is the decision that pays for the HTTP boundary: today apply work runs
inside the Telegram process, so restarting the bot for a Telegram fix kills in-flight
jobs and workers cannot be scaled. For a personal bot that is irrelevant; for paying
customers it is not.

**Customers reach the product through two surfaces.** The website is the full
account: profile, balance, CV upload, history, downloads. Telegram is deliberately
narrow — send a vacancy, receive the documents, receive notifications. Payment and
personal data never go through the chat. One shared bot serves all customers via the
existing link mechanism (`telegram_links`, `/link`, link codes); per-customer bot
tokens were rejected because every new customer would require a manual BotFather
operation and its own polling process.

**Notification delivery goes through an outbox table.** The service finishes a job
and writes a row; a delivery worker drains it into email or Telegram. This was chosen
over the service calling the bot directly (which would make the service depend on the
bot being alive right now, and force retry logic into the service) and over the bot
polling the applications table (an expensive scan for rare events). The outbox gives
retries, delivery history, and one mechanism for both channels — which matters because
there are already two channels and email is needed for registration anyway.

**Trust between processes.** Both containers sit on the same host in one internal
compose network; the service has no public port. NestJS sends a shared secret header,
and — critically — the service takes `user_id` **from NestJS, not from the caller**,
and refuses to serve any request without the correct secret. Forging a request to the
service means free generation billed to the owner, so this is a money boundary, not
just a hygiene one. Mutual TLS was rejected as solving a problem that does not exist
between two containers on one host.

---

## 3. Decision register

Every decision below was made explicitly during the design interview. Rationale is
recorded so that a future reader can tell a considered choice from an accident.

### 3.1 Purpose and scope

| Decision | Rationale |
|---|---|
| The goal is a sellable product, not architectural cleanliness | Optimize for "can add a paying user without a rewrite", not for a beautiful core |
| MVP is tailoring only; job search comes later | One reproducible scenario beats a showcase; every sold feature is support load |
| Telegram stays a co-equal surface, not a legacy one | Owner uses it as a phone-side console; customers get a narrow version of it |
| Google Sheets is legacy — keep mirroring for now, off for customers | Data divergence is worse than a redundant mirror, but customer data must never land in the owner's personal spreadsheet |

### 3.2 Integration and execution

| Decision | Rationale |
|---|---|
| HTTP service (FastAPI), not DB-row handoff | Apply must not die with the Telegram process; workers must be scalable |
| Service is the **sole** apply executor; bot delegates to it | Two executors = two copies of the logic, claim races, and the owner stops dogfooding what customers get |
| Extract a shared core out of `main_api`, build thin scenarios on top | Adding flags to `main_api` recreates the ten-flag function it was designed to avoid; a parallel pipeline would diverge at the first prompt improvement |
| Strangler migration: owner's own flow moves to the service **first** | Only way to find the service's problems before someone has paid for them; cost of failure is zero while there are no customers |
| Outbox table for notifications | Retries, history, one mechanism for email + Telegram |
| Internal network + shared secret; `user_id` supplied by NestJS | It is a money boundary — forged requests mean free generation |

### 3.3 Data

| Decision | Rationale |
|---|---|
| Domain-split ownership: NestJS owns users/auth/settings, service owns applications/apply | Single owner per table. The current absence of an owner already produced a live bug (§4.1) |
| Move to **PostgreSQL**, deferred to the last step before the first paying customer | Chosen over libSQL despite the higher migration cost (no ORM on either side — raw `better-sqlite3` in NestJS, raw `sqlite3` in ~1500 lines of `tracker.py`). Deferring keeps the working bot safe while product work proceeds |
| Customer profile (role, region, stack) lives in the `user_settings` **table**, not `filters.yaml` | A file per customer has the same problems as a database per customer: migrations, backups, concurrent writes. `feat/filters-api` remains valid for the owner's own profile |
| `user_id` isolation enforced by **tests**, not discipline | One forgotten `WHERE user_id` in a multi-tenant system leaks someone's CV — that is a regulator-notification incident, not a bug |
| Documents on the VPS filesystem now, object storage (R2) later with the DB move | `FilesModule` already serves files. Two obligations attach immediately: back up `Applications/` (today only `tracker.db` is backed up), and make customer deletion a single operation (right to erasure) |

### 3.4 Google and the reduced product

The blocker that shaped the whole product boundary: all three Google integrations run
on the owner's personal account, wired to single process-global files.
`hunter/gmail_client.py:9` resolves `gmail_token.json` at module level, and
`hunter/gdrive_client.py` reuses `gsheets_token.json`. The bot reads **the owner's**
inbox to detect employer replies, uploads to **the owner's** Drive, mirrors into
**the owner's** spreadsheet.

Therefore: customers get documents by download from our own storage; the Sheets
mirror is disabled for customer rows; and mailbox reading is **not sold at all**. It
is the most valuable feature and the most expensive to legalize — Gmail read access is
a Google restricted scope requiring verification and a recurring paid security
assessment. It remains a personal feature of the owner, and may return later as an
optional per-customer OAuth connection.

### 3.5 Product mechanics

| Decision | Rationale |
|---|---|
| Vacancy input: URL **and** paste, URL with paste fallback | Some boards sit behind Cloudflare and logins. For the owner a failed fetch is an annoyance; for a paying customer it is a refund |
| CV input: upload → parse → our standard structure, with a confirmation screen | A 40-field form kills conversion, nobody writes markdown, and the parser *will* be wrong on unusual layouts — so the "check what we understood" step is part of the product, not optional |
| One output layout, sold as "ATS-optimized template" | The layout is hardcoded in `generate_docs.py` via python-docx (fonts, spacing, colors set programmatically; `candidate/templates` is empty). Turning that into a template system is a separate large project. "Our template provably parses in ATS" is an honest selling point backed by `ats_pdf_roundtrip` |
| One base profile per customer; multiple profiles are a paid upsell | Five stack-specific base CVs is the owner's personal optimization across markets; for a new customer it is complexity without value |
| Free pre-payment score; independent verdict after generation | Costs nothing to compute, demonstrates the pain, and the before/after pair proves the value |
| Standalone optimizer gets its **own pipeline and agent** | Customers are expected to arrive wanting an optimized CV without any vacancy — this is a product in its own right, not a byproduct of onboarding |
| Standalone optimization targets general ATS rules; market-aggregate targeting is the paid tier | Free/basic tier stays cheap to run; the market profile is the differentiator worth charging for |
| Telegram for customers: submit vacancy + receive documents + notifications only | "Saw a vacancy on the phone, threw it at the bot, got documents five minutes later" is the scenario worth making excellent; billing and personal data in chat are an unnecessary sensitive surface |
| Market aggregates precomputed nightly into a table | Resolves the conflict in §6.7: a paid feature would otherwise depend on the bot process being alive at request time |

---

## 4. Work stages

Ordered by dependency. Each stage names its deliverable and how it is verified.

### Stage 0 — Fix the live mirror bug (api repo, small, independent)

The one customer-facing feature that already works — changing an application's status
from the web — silently diverges from the Sheets mirror.

- `api/src/tracker/tracker.service.ts:182` issues
  `UPDATE applications SET <col>=? WHERE id=? AND user_id=?` without setting
  `sheets_dirty=1`, so the bot's `resync_dirty()` never learns the row changed.
  The bot's own writes go through `tracker.mark_sheets_dirty()`
  (`hunter/tracker.py:1989`); the API simply does not use the mechanism that exists.
- Set the dirty flag on update, excluding columns that are themselves sheets metadata
  (`sheets_row`, `sheets_dirty`).
- Confirm `resync_dirty()` runs on a schedule (`hunter/schedules/gsheets.py`) and not
  only after a failed mirror write; add a periodic tick if it does not.
- Note for later: once customers exist, this mirroring must be **skipped** for
  customer-owned rows (§3.4).

*Verification:* patch a row through the API, run resync, assert the spreadsheet cell
changed.

*Why first:* it is a real bug affecting real data today, it is small, and it is
independent of everything else.

### Stage 1 — Extract the generation core from `main_api`

`hunter/apply_api.main_api()` is today "fetch job text → LLM → content.json →
generate_docs", entangled with tracker dedup, Drive, Sheets, `outreach.md` and reply
tracking. Three scenarios need to be built on top of it, so the shared part must come
out first.

- Extract a core function: **(vacancy text | none) + profile → content.json →
  documents + score**. No tracker writes, no Drive, no Sheets, no Telegram inside it.
- Rebuild the owner's existing flow as a thin scenario over the core (dedup, tracker
  row, Drive, Sheets, outreach) so behavior is unchanged.
- Keep the existing `notify(...)` seam already present inside `apply_api` — it becomes
  the hook the outbox writes to in Stage 3.
- Do **not** add `user_id`/`no_drive`/`no_sheets` flags to `main_api`; that is the
  ten-flag function its own docstring was written to avoid.

*Verification:* the owner's apply flow produces byte-identical documents and identical
tracker rows before and after the refactor.

### Stage 2 — Stand up the Python service and move the owner's own flow onto it

- New FastAPI entry point in the bot repository (same repo, same code, second
  process — this is *not* a separate package; see §5).
- Endpoints: submit tailoring job (returns task id), poll task status, health.
  Long-running by nature — LLM generation takes minutes — so never synchronous.
- New container in the bot's compose project. **No public port.** Shared-secret header
  required; `user_id` accepted only from trusted callers.
- The Telegram bot stops executing apply work and calls the service instead. The
  existing queue semantics (`PENDING`/`IN_PROGRESS` in `ats_status`, claim capture,
  the stale-claim sweep in `hunter/schedules/apply_queue.py`) move behind the service.
- Leave a **balance-check hook** at the point where a job is enqueued, even though
  billing mechanics are deferred (§7). Checking after generation is checking after the
  money is spent.

*Verification:* the owner runs entirely on the service for a week with no regression
in the Telegram experience. This is the dogfooding step — it is the whole point of the
strangler order.

### Stage 3 — Outbox and delivery

- Outbox table: recipient, channel, payload/reference, attempts, state, timestamps.
- Delivery worker drains it into email (the API already has `src/mail`) and Telegram
  (through the bot, which owns the PTB application).
- The service never talks to Telegram directly — it writes an outbox row.
- This replaces the "notifier seam" from the old plan: the same mechanism serves both
  channels, so the core needs no Telegram-shaped abstraction at all.

*Verification:* kill the bot, complete a job, restart the bot — the notification still
arrives.

### Stage 4 — Customer profile: upload, parse, confirm, store

This is the stage the old plan wrongly scheduled last. Nothing can be sold without it.

- Upload of docx/pdf through the existing `FilesModule` path, scoped per user.
- Parse into the structure the generator consumes — the equivalent of
  `candidate/base_cv_*.md` + `candidate.yaml` + `candidate_profile.md`, but per user.
  `hunter/about_me_agent.py` and `hunter/content_qa.py` are the existing building
  blocks.
- **Mandatory confirmation screen.** The parser will misread unusual layouts; letting
  a customer correct it is part of the product, not a fallback.
- Role, region and stack go into `user_settings` (§3.3), not into a per-customer file.
- Per-user file layout under the storage root, with deletion of a customer's data as a
  single operation.
- `user_id` isolation tests land here, alongside the first real multi-tenant data.

*Blocked by / builds on:* the in-flight multi-user work in the bot worktrees
(`feat/multi-user-config`, `candidate-yaml-multi-user`) and PR #204, which moved the
owner's personal data out of shared code. Extend that work — do not fork it.

*Verification:* a second account uploads a CV and gets a correct base profile that the
generator can consume, with no access to the owner's files.

### Stage 5 — Customer tailoring, end to end

- Vacancy in by URL (`sources.fetch_job_text()`), by pasted text, or both with URL
  falling back to paste.
- Free pre-payment score via `ats_checker` in its no-LLM mode (75% keyword +
  25% TF-IDF, zero API cost) — surfaced before the customer pays.
- Generation over the Stage 1 core with the customer's profile.
- Post-generation score from the independent judge (`ats_verdict`), presented as the
  before/after pair.
- Documents downloadable from the site; Telegram delivery for linked customers.
- Dedup is already per-user (`is_known()` scopes by `_uid()`,
  `hunter/tracker.py:377`), so two customers may target the same vacancy — this branch
  is already safe.

*Verification:* a paying-shaped account completes upload → vacancy → documents →
download, and the owner's own data is untouched throughout.

### Stage 6 — Standalone optimizer and market aggregates

- **New pipeline and agent** for optimizing a CV with no vacancy attached, targeting
  general ATS rules: structure, action verbs, quantified results, keyword density.
- **Paid tier:** optimization against an aggregated demand profile. Nightly job reads
  recently scraped vacancies, extracts frequent requirements per role/region, and
  writes an aggregates table. Customer requests read the precomputed table.
- The nightly precompute is what keeps `hunt` out of the paid path (§6.7).

*Verification:* two runs for the same role produce a stable aggregate; the optimizer's
output measurably raises the no-LLM score against sampled vacancies for that role.

### Stage 7 — Billing

Mechanics deferred (§7). Whatever the shape, the enqueue-time balance check from
Stage 2 is where it plugs in.

### Stage 8 — PostgreSQL and object storage

Last, immediately before the first paying customer.

- Migration touches **both** sides: raw `sqlite3` across ~1500 lines of `tracker.py`
  plus `hunter/db.py`, and raw `better-sqlite3` in `api/src/db/migrations.ts` and
  `tracker-migrations.ts`. There is no ORM to absorb the change.
- Move documents to R2 at the same time; add backups for the documents tree, not just
  the database.

*Why last:* while there are no customers, the migration delivers nothing except the
risk of breaking the only thing that currently works.

### Later, outside this plan

`hunt` moves out of the Telegram process into the service (its scheduler is currently
bound to PTB's JobQueue — everything in `hunter/schedules/` takes `ContextTypes`).
Selling job search is the trigger for that work, not this plan.

---

## 5. Rejected options

- **A separate pip package / repository for the Python core.** Nothing outside this
  repository consumes the code; packaging adds version drag for no benefit. The
  service is a second entry point in the same repository.
- **DB-row handoff instead of an HTTP service.** Genuinely tempting — the `PENDING`
  queue with claim capture already exists — but it leaves apply execution inside the
  Telegram process, which is exactly what must stop for a product.
- **The frontend calling the Python service directly.** NestJS stays the single auth
  and routing point; the service has no public port.
- **A synchronous tailoring endpoint.** Generation takes minutes; always enqueue and
  poll.
- **libSQL/Turso instead of PostgreSQL.** Would have preserved the SQLite dialect and
  most existing SQL; PostgreSQL was chosen deliberately at the higher migration cost.
- **Per-customer Telegram bots.** Manual BotFather setup per customer and a process
  per token. Reconsider only as a white-label tier.
- **Preserving the customer's original CV design.** Requires turning hardcoded
  python-docx layout into a template system — a separate project, and not what
  tailoring is about.
- **Selling mailbox reading.** Google restricted scope: verification plus a recurring
  paid security assessment.

---

## 6. Risks and things to watch

1. **Tenant isolation is the incident risk, not a bug risk.** A missing
   `WHERE user_id` leaks a stranger's CV in the EU. Tests, not care.
2. **Backups currently cover the database, not the documents.** `Applications/` is not
   backed up today; it becomes customer property in Stage 4.
3. **Right to erasure must be one operation.** Customer data will live in the
   database, the documents tree, and possibly the Sheets mirror — the last of which is
   precisely why customer rows must never be mirrored.
4. **The parser will be wrong.** The confirmation screen is the mitigation; treat its
   quality as product quality, not as an edge case.
5. **Cost runaway.** One customer with a batch of vacancies can burn the owner's LLM
   budget overnight. The enqueue-time balance check is the control; `cost_usd` records
   what was spent but only after it is spent.
6. **Single-flight on URLs.** Verify the in-flight URL lock (bot PR #201) is enforced
   at queue level, not at the Telegram layer, so web and Telegram submissions of the
   same vacancy cannot race.
7. **`hunt` feeding a paid feature.** Resolved by nightly precomputed aggregates
   (Stage 6) rather than by moving `hunt` early — but if the aggregate ever becomes
   real-time, this conflict returns.
8. **The owner's flow is the canary.** If Stage 2 dogfooding is skipped or rushed,
   customers become the first testers of the service.

---

## 7. Deliberately deferred

**Billing mechanics.** The shape is known — a one-off payment for the base optimized
CV, credits for per-vacancy tailoring, a separate tier for market-based optimization —
but amounts, packages, and zero-balance behavior are open. One requirement cannot be
deferred: **the balance is checked before a job is enqueued, never after generation.**
The bot's answer to a customer with an empty balance is a top-up link, not silent
queueing.

---

## 8. Verified codebase facts behind this plan

| Fact | Where |
|---|---|
| Telegram coupling in the core is limited to two files | `hunter/main.py` (7 functions take `ContextTypes`), `hunter/apply_worker.py` (~15 `send_text` calls) |
| The apply pipeline is already import-safe and Telegram-free by design | `hunter/apply_api.py` docstring; `hunter/services/` |
| `main_api` is fetch → LLM → content.json → generate_docs, and already calls `notify(...)` | `hunter/apply_api.py:118` |
| A DB-backed queue with claim capture already exists | `PENDING`/`IN_PROGRESS` in `ats_status`; `hunter/schedules/apply_queue.py` |
| Dedup is already per-user | `hunter/tracker.py:377` (`is_known()` scopes by `_uid()`) |
| SQLite runs in WAL with `synchronous=NORMAL` | `hunter/db.py:152` |
| NestJS already writes to the database, scoped by `user_id` | `api/src/tracker/tracker.service.ts:182`, `applications.controller.ts:45` |
| …but does not set `sheets_dirty`, so web edits never reach the mirror | Stage 0 |
| `user_settings` schema is owned by the API and mirrored by the bot | `hunter/db.py:85` comment |
| No ORM on either side | raw `sqlite3` in the bot; `better-sqlite3` in `api/src/db/migrations.ts` |
| Google integrations are process-global and personal | `hunter/gmail_client.py:9`; `hunter/gdrive_client.py` reuses `gsheets_token.json` |
| The match score exists, including a zero-cost mode | `hunter/ats_checker.py` (60/30/10 with LLM, 75/25 without) |
| An independent judge score over the rendered PDF exists | `hunter/verdict_writer.py`, `applications.ats_verdict` |
| The output layout is hardcoded, not templated | `generate_docs.py` (python-docx), `candidate/templates` is empty, PDF via LibreOffice headless |
| The owner's base profile is markdown + yaml, five stacks | `candidate/base_cv_*.md`, `candidate.yaml`, `candidate_profile.md` |
| The frontend already has the account shell | `site/src/app/features/`: signup, verify, login, profile, settings, files, templates, applications, stats, admin |
| Multi-user work is in flight and must be extended, not forked | bot worktrees `feat/multi-user-config`, `candidate-yaml-multi-user`; PR #204 |
