# PER_USER_EMAIL Plan

**Status:** draft
**Date:** 2026-08-21
**Motivation:** Owner question (2026-08-21): "can users configure it themselves so it
checks THEIR mail?" Today both email features — job alerts as a source
(`hunter/sources/gmail.py`) and employer-reply detection
(`hunter/email_response_checker.py`) — read exactly one inbox: the owner's, through one
process-global token file. Per-user Gmail OAuth is the obvious answer and the wrong one
(see Problem). This plan proposes the inverse: users forward the mail they want checked
to an address **we** own, so no user ever grants us access to their mailbox.

## Problem

Two independent facts.

**1. The code is single-tenant by construction.** `hunter/gmail_client.py:9` resolves
`TOKEN_PATH = <repo>/gmail_token.json` at module import — one token per process. Every
caller (`GmailSource.search`, `fetch_confirmation_emails`, `run_confirmation_check`)
goes through `get_gmail_service()` with no user parameter, and both features are
force-disabled for non-owner users by the B3 owner-only guard
(docs/MULTI_USER_UPDATE.md §B3 item 8). That part is mechanical to fix.

**2. Per-user Gmail OAuth is not mechanical.** `gmail.readonly` / `gmail.modify` are
Google **restricted** scopes. For external users that means OAuth verification plus a
recurring paid CASA security assessment; without it the OAuth client stays in *Testing*,
which caps at ~100 users and **expires refresh tokens after 7 days** — every user would
reconnect their mailbox weekly. docs/SAAS_PIVOT_PLAN §3.4 already recorded the
conclusion ("mailbox reading is not sold at all… may return later as an optional
per-customer OAuth connection") and shaped the product boundary around it.

Forwarding removes the blocker entirely: the only Google account involved is **ours**,
used by **us**, so no external grant and no verification is in scope. The user changes a
setting in their own mail client instead of granting an app access to everything in it —
which is also the more honest privacy story.

Third fact worth stating: a forwarding filter is keyed on *senders*, so it sees strictly
less than a full-inbox scan. Whether "less" is "meaningfully less" is unknown, and that
is what M0 measures.

## Non-goals

- **No per-user OAuth.** Not in this plan, in any milestone. If it ever ships it is a
  separate decision with a separate budget.
- **No reading of a user's whole mailbox.** Ingest applies a sender allowlist; anything
  else is dropped unparsed. A user who forwards everything must still not have their
  private mail stored or read by us.
- **No sending mail on a user's behalf.** Outreach stays what it is today: a drafted
  `outreach.md` the owner copies manually (`hunter/outreach.py`).
- **No change to the owner's current setup** until M4. The owner's direct Gmail OAuth
  keeps running exactly as it does now while the alias path is proven on real mail.
- **No hunt fan-out.** Per-user *alerts as a job source* depends on Phase B3.5
  (docs/MULTI_USER_UPDATE.md); this plan delivers per-user *reply detection* first,
  which does not.

## M0 — Measure

**Question:** if we only ever see mail that a sender-keyed forwarding filter would
match, how much of what we detect today do we lose?

The measurement is a set comparison on the owner's own inbox, over the last 90 days,
using the Gmail API `q=` syntax — which is the same query language Gmail *filters* use,
so the query below is a faithful stand-in for the filter we would ship to users:

- **Set A (today):** the emails `hunter/email_response_checker.fetch_confirmation_emails`
  actually parses into a `ConfirmationEmail`, plus the alert emails `GmailSource` parses
  into `Job`s.
- **Set B (forwarded):** the emails matching
  `from:(erecruiter.pl OR smartrecruiters.com OR … )` built from
  `email_response_checker._CONFIRMATION_SENDERS` + `gmail_parsers.PARSERS` keys.
- **Report:** `|A ∩ B| / |A|` overall and split by feature (replies vs alerts), plus the
  sender domains of every `A \ B` message — the misses are the interesting output, since
  a miss from a recurring domain is fixable by adding it to the list, while a miss from
  a one-off company mailbox is not.

The `_parse_direct` branch of the reply checker (direct company mail, caught by subject
keywords rather than sender) is the expected loss centre. Gmail filters can also key on
subject, so if the misses are subject-shaped the filter grows a second clause and the
loss shrinks — M0 must report which shape the misses are.

**Command** (the only code M0 adds is this read-only script — no writes, no LLM, no
network beyond the Gmail read the bot already does):

```bash
docker compose exec job-hunter python tools/mail_filter_coverage.py --days 90
```

**Decision rule, stated before the run:**

- recall ≥ 85 % on replies → build as planned;
- 60–85 % → build, but M5 ships the subject-clause filter too and the UI states plainly
  that direct-from-company replies may be missed;
- < 60 % → **close this plan.** At that recall the feature would silently under-report
  employer responses, and a wrong "no answer yet" is worse than an honest "not
  supported". Reply detection stays owner-only and the product boundary of
  SAAS_PIVOT_PLAN §3.4 stands.

Alerts have no lower bound: an alert we never see is a vacancy found by the other 24
sources or not at all, and the user can also point their board subscriptions *directly*
at the alias, which makes recall 100 % by construction for anything they re-subscribe.

## M1 — Inbound plumbing + attribution probe

`mail.igrflex.work` (domain already on Cloudflare) with Email Routing: one rule per
user, `u-<token>@mail.igrflex.work` → `<botinbox>+u-<token>@gmail.com`, on a **dedicated
bot-owned Google account** (not the owner's personal one). Rules are created through the
Cloudflare API from the API repo; the token is random and unguessable, because possession
of the address is what authorises writes to that user's rows.

**One mailbox, N addresses** — the point worth being explicit about, since it is the
part that sounds like it should not work:

```
user A → u-7f3a91c2@mail.igrflex.work ─┐
user B → u-b40e2d19@mail.igrflex.work ─┼→ Cloudflare Email Routing
user C → u-e12c8a55@mail.igrflex.work ─┘         │
                                                  ▼
                                    botinbox+u-7f3a91c2@gmail.com
                                    botinbox+u-b40e2d19@gmail.com   ← ONE
                                    botinbox+u-e12c8a55@gmail.com     inbox
```

Gmail treats `botinbox+anything@gmail.com` as the same single mailbox but records the
address it was delivered to in `Delivered-To`. So the bot reads **one** inbox with
**one** token — mechanically what it does today with the owner's — and learns whose mail
it is from that header. Onboarding a user is one Cloudflare API call and zero Google
operations; no user ever gets their own mailbox or their own token.

Why plus-addressing on a Gmail we own, rather than a Cloudflare Email Worker posting to
the API: Gmail preserves the plus-address in `Delivered-To`, which gives reliable
per-user attribution, and it keeps **every existing parser, the whole `gmail_client`
path and the existing polling schedule unchanged**. An Email Worker removes Gmail from
the loop but adds a new parsing+storage surface and a new failure domain for no benefit
we can name today. Revisit if the shared mailbox hits Gmail's limits.

- **Ships:** the Cloudflare zone config, the bot mailbox, and a header probe — send a
  real alert and a real ATS reply through the chain (user mailbox → forward → CF →
  bot inbox) and dump `Delivered-To` / `To` / `X-Forwarded-To` / `Received`.
- **Test:** the probe's captured headers become a fixture in
  `tests/fixtures/inbound_mail/`.
- **Why a probe and not an assumption:** attribution is the single load-bearing
  assumption of the whole design. If `Delivered-To` does not survive the chain, the
  fallback is one CF rule → one *distinct destination address* per user, which is more
  ops but equally reliable. Better to learn this before M2 exists.
- **Rollback:** delete the routing rules; nothing in the bot references them yet.

## M2 — Alias registry + attribution in the bot

- **API repo** owns the schema (shared contract, docs/MULTI_USER_UPDATE.md §"tracker.db
  shared-table DDL"): `user_email_aliases(user_id PK, alias, token, created_at,
  revoked_at)`; the bot mirrors the DDL idempotently in `hunter/db.py::_ensure_columns`
  as it does for `telegram_links`.
- **Bot:** new `hunter/inbound_mail.py` — `resolve_alias_user(headers) -> user_id | None`
  keyed on the `Delivered-To` plus-address, plus `SENDER_ALLOWLIST` (the M0 union of
  `_CONFIRMATION_SENDERS` and `PARSERS`). Mail whose sender is not on the allowlist is
  dropped **before** the body is parsed or logged — that is the "no private mail" rail,
  and it is enforced in code, not in the setup instructions.
- **Test:** attribution from the M1 fixtures, unknown alias → `None`, revoked alias →
  `None`, non-allowlisted sender → dropped with no body access.
- **Rollback:** module is not called by anything yet.

## M3 — Per-user reply detection

`run_confirmation_check` gains a `user_id` and stops being a single global sweep:
`hunter/schedules/email_responses.py` loops `users.list_active_users()`,
`match_email` / `lookup_by_company_and_title` scope by that user (same explicit-`user_id`
plumbing B3 already added to `hunter/tracker.py`), and the report goes to that user's
chat via `users.resolve_chat`. Alias-attributed mail is checked against the aliased
user's rows only; the owner's directly-polled mail keeps its current path.

- **Test:** two users, two aliases, one reply each — each `confirmation` stamp lands on
  the right row and neither user's rows are visible to the other's match.
- **Rollback:** `PER_USER_EMAIL_ENABLED=false` restores the single-sweep behaviour;
  the loop degrades to `[owner]`.
- Ingest wraps in `best_effort("inbound_mail.ingest")` — a dead bot mailbox must alert
  at the threshold rather than silently stop confirming replies for everyone.

## M4 — Per-user alerts as a job source

Depends on B3.5 (hunt fan-out). `GmailSource` yields `Job`s tagged with the alias user,
which the fan-out already knows how to route. The owner migrates from direct OAuth to
the alias path here, once M3 has proven attribution on real mail for weeks.

- **Rollback:** owner keeps `gmail_token.json`; the two paths coexist by design.

## M5 — Self-service setup UI (site + API)

The user-facing half, and the part that decides whether anyone actually turns this on:
the settings page shows the alias, a copy button, and two paths —

1. **Alerts:** "subscribe to LinkedIn/Pracuj/NoFluff alerts at this address" — no mail
   client configuration at all.
2. **Replies:** a generated `mailFilters.xml` (Gmail: Settings → Filters → Import) with
   the sender allowlist and the forward-to alias pre-filled, so it is an import rather
   than fifteen hand-typed conditions. Gmail requires the forwarding address to be
   confirmed first, and the confirmation code arrives in *our* inbox — the page surfaces
   it (or auto-confirms) instead of leaving the user stuck.

- **Test:** the generated XML imports cleanly into a real Gmail account (manual, once,
  recorded in the work log).

## M6 — Retention & erasure

The database never stores raw MIME — only the derived match (company, title, date,
message id). The mail itself does physically sit in the bot inbox, so that is where
retention is enforced: **auto-delete after 14 days** (owner decision, see Decisions).
Immediate deletion was considered and rejected — a parser miss is undebuggable without
the source email, and 14 days covers the "user notices → owner looks" cycle.

Alias revocation deletes the CF rule; account deletion deletes the alias row and every
derived record — the single-operation erasure SAAS_PIVOT_PLAN §3.3 already committed to.
Privacy policy gains a paragraph naming what we receive, why, and how long we keep it.

## Risks

| Risk | Rail |
|---|---|
| `Delivered-To` does not survive the forward chain → wrong user attribution | M1 probe runs **before** any code depends on it; fallback = one distinct destination per user. Attribution failure means `None`, never a guess — a `None` is dropped, never written to a default user |
| A user forwards their entire mailbox to us | Sender allowlist at ingest, enforced before body parse (M2); parse-and-drop retention (M6) |
| Forwarded mail fails SPF and lands in the bot inbox's spam | Gmail filter "never send to spam" on the alias; M0's query already tells us the volume to expect |
| Someone guesses another user's alias and forges a confirmation | Alias is a random token, revocable; worst case is a false `confirmation` stamp on one row — informational, it blocks nothing in the pipeline |
| Shared inbox hits Gmail rate/volume limits as users grow | Volume is bounded by the allowlist; the Email Worker variant (M1 rationale) is the documented escape hatch |
| Reply recall quietly degrades as ATS platforms change senders | The existing "HOW TO ADD A NEW PLATFORM" runbook in `email_response_checker.py:32-47` already covers it; M0's script re-runs as a periodic audit |

## Cost

**Zero LLM calls.** Every stage is deterministic: header attribution, a sender
allowlist, and the existing regex parsers. Infrastructure is one Cloudflare Email
Routing zone (free tier) and one Google account. This is the same shape as the doomed
gate and the re-post gate — a real decision changed at $0.00.

## Decisions (owner, 2026-08-23)

1. **Alias domain: `mail.igrflex.work`** — a subdomain of the existing Cloudflare zone,
   kept separate from `job-hunter.igrflex.work`, which the API sends verification mail
   from. Inbound forwarding and outbound delivery do not share reputation.
2. **Bot inbox: a new dedicated Google account**, not the owner's. Users' mail never
   lands in a personal mailbox; the 14-day purge, a GDPR erasure request and a
   hypothetical spam suspension all stay contained to an account that exists only for
   this.
3. **Sender allowlist lives in the bot**, applied before the body is parsed. Cloudflare
   Email Routing rules match on the *recipient* address and cannot see the sender, so
   edge rejection would require an Email Worker — the variant M1 already declined. One
   allowlist, in one place, covered by tests.
4. **Raw mail is auto-deleted from the bot inbox after 14 days**; the DB never holds it
   at all (M6).

## Open questions

1. Does the owner's own setup migrate to the alias path in M4, or keep direct Gmail
   OAuth permanently as the reference path? (Decidable at M4 — both paths coexist by
   design until then.)
