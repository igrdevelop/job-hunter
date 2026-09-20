# FILTER_FEEDBACK Plan — turn the owner's "Filter miss" labels into filter rules, safely

**Status:** draft — M0 not run (deliberately: the labels are days old; owner decision 2026-09-19 is to collect for another two weeks first)
**Date:** 2026-09-19
**Motivation:** owner report 2026-09-19 — "filtering got worse" — plus the observation
that there is now a place where the misses are recorded: the website's Applications
table writes `app_status = 'Filter miss'` + `owner_reason` (+ an optional note) on any
row the bot should have dropped and didn't (job-hunter-api PR #34, merged 2026-09-14).
The owner's ask: a mechanism that keeps improving itself — weekly, code (or an agent)
counts those labels and proposes, or sets, additional filters. This plan is how that is
done without the mechanism quietly destroying recall.

---

## Problem

**1. The feedback exists and nothing reads it.** `applications.owner_reason` /
`owner_reason_note` are API-owned columns inside the bot's own `tracker.db`. No bot code
selects them; `docs/DOMAIN_MODEL.md` §2.1a says so explicitly and defers acting on them
("acting on it needs its own plan") — deliberately, because
`docs/MARKET_MEMORY_PLAN.md`'s non-goal is "No change to `filters.py` logic or to any
gate". This is that deferred plan.

**2. Every filter fix so far has been a hand audit, and one of them had to be reverted.**

- 2026-08-08 Sent-notes audit: 250 free-text notes classified by hand, because no
  per-job filter verdict was stored (`docs/AGENT_LOG.md:78`).
- 2026-07-07 doomed-gate paste calibration: a `header_location_anti_hybrid_city` rule
  was implemented and then **reverted the same day** — it also fired on Fairmarkit, a
  real Sent row with a 98 % verdict (`docs/AGENT_LOG.md`, 2026-07-07 entry).

That revert is the most important precedent here. The check that killed the rule was
*"does it also match something I actually sent"*, performed by hand. This plan makes
that check mandatory and automatic, and refuses to propose a rule that fails it.

**3. "Worse" has at least two explanations and today nothing separates them.**
`hunter/filters.py` has had no substantive change since `19f1354` (2026-08-13). Over the
same period the input widened: `LINKEDIN_TPR` 24 h → 7 d (2026-08-12), Jobspresso moved
to per-keyword feeds (0 → 21 jobs, 2026-08-10), Telegram channels (2026-07-11),
FindMyRemote / Smart Jobs (2026-07-12/13). Unchanged rules + more listings = more junk in
absolute counts at identical precision, which *feels* like a broken filter but would be
fixed at the source, not in the rules. `postings_seen` (on master since 2026-09-13) can
now tell the two apart per source and per week; nothing looks.

**What is on hand, and since when:**

| Data | Meaning | Live since |
|---|---|---|
| `postings_seen.filter_verdict` / `_last` (+ `title`, `company`, `source`, `city`, `last_seen`) | why the bot **rejected a listing** — one of `filters.FILTER_REASONS` — or that it passed | 2026-09-13 |
| `applications.skip_reason` | why a gate wrote SKIP **after** the listing passed — `tracker.SKIP_REASON_PREFIXES` | 2026-09-13 |
| `applications.app_status` + `owner_reason` + `owner_reason_note` | the **owner's** verdict: `Skipped` (filters were right) vs `Filter miss` (they were not) + one of 16 codes | 2026-09-14 (API) |
| `applications.title` / `sent` | full history of what was actually sent — the damage check's ground truth | since the beginning |

Join key: `url_norm`, present on both tables.

**4. Only one error direction is observable.** `Filter miss` records false positives
(junk got through). False negatives — a good vacancy the filters dropped — never reach
the Applications table, so the owner cannot label them. `postings_seen` has kept every
rejected listing with its reason since 2026-09-13, so the data exists, but the review
surface does not. The owner deferred that half on 2026-09-19. **A plan that only
tightens filters therefore has a structural blind spot**, and its safety rails must
compensate for it rather than ignore it — see M4's shadow week, which is a prospective
substitute for the missing review surface.

---

## Non-goals

- **No change to how filters evaluate.** `filters.py` logic is untouched. Only the
  profile `filter_profile.load_profile()` builds gains entries, through the existing
  merge machinery.
- **No auto-activation.** Owner decision 2026-09-19: the machine may write a proposed
  rule, but only into a **shadow** state that changes nothing; it becomes real only on
  an explicit button press.
- **`Skipped` never generates a proposal.** Owner decision 2026-09-19: `Skipped` means
  the filters were right and the owner simply did not want the role — acting on it lets
  the bot decide what is interesting. It appears in the report as an observation only.
- **No false-negative ("wrongly rejected") review loop here.** Deferred by the owner; a
  separate plan, with `postings_seen` as its data source.
- **No Layer-0 regex changes.** Codes that point at detection regexes (`language`,
  `work_authorization`, `contract`, `relocation`, `russia`) and at the doomed gate are
  code, not settings (`docs/FILTERS_YAML_PLAN.md` knob table). The report names them; a
  human writes the fix.
- **No LLM calls in M0–M4.** M5 is optional and gated by its own evidence.
- **Single user first.** Per-user resolution rides `FILTERS_YAML_PLAN` M4, blocked on
  ROADMAP 3.3; nothing here makes that worse.

---

## M0 — Measure

`tools/filter_feedback_m0.py`: read-only, `$0`, no LLM, no network, no writes. Run on
the deploy host — the labels and `postings_seen` only exist there:

```
docker compose exec job-hunter python tools/filter_feedback_m0.py --weeks 4 [--json]
```

**(a) Volume of labels.** Rows by `app_status`; `Filter miss` count and date range;
distribution by `owner_reason`; how many carry a free-text `owner_reason_note`. Column
presence is probed via `PRAGMA table_info(applications)` first — precedent
`hunter/funnel.py:225` — so a DB without the API migrations reports "no owner labels"
instead of crashing.

**(b) Actionability buckets.** Every `owner_reason` code mapped to where a fix would
have to live:

| Bucket | Codes | Fixable by a rule this plan can write |
|---|---|---|
| title regex | `stack`, `fullstack_backend`, `level`, `title` | yes → `extra_exclude_patterns` |
| company | `company` | yes → `exclude_companies` (already `extend`) |
| city | `location` | yes → `extra_anti_hybrid_cities` (already `extend`) |
| Layer-0 detection | `language`, `work_authorization`, `contract`, `relocation`, `russia` | no — the toggle is already on, the *detection regex* missed; report only |
| not a filter problem | `duplicate`, `expired` | no — a dedup / expired-check signal; report only |
| never actionable | `salary`, `not_interesting` | no — `Skipped`-only by construction (the bot has no salary gate) |
| unclassified | `other` | no — read the notes |

**(c) Coverage.** Share of `Filter miss` rows that have a `postings_seen` row — i.e.
came from a hunt, not a manual paste (pastes never pass through hunt Step 2.5).
Informational: it bounds how much of the backtest can use listing titles rather than
LLM-extracted ones.

**(d) Candidate dry run.** For the largest title-regex code: normalized 1–3-word n-grams
over the miss titles (preferring `postings_seen.title`, falling back to
`applications.title`), each scored as

- `hits` — how many `Filter miss` rows it matches,
- `damage` — how many rows in `applications` with `sent_parse.classify(sent) == "applied"`
  it *also* matches, over the entire history,
- `radius` — how many `postings_seen` rows of the last 60 days it would newly reject.

**(e) Volume hypothesis.** Per source: listings seen and share passed per week from
`postings_seen` (short history), next to applications per week over 12 weeks (long
history). Answers "did precision fall, or did volume rise".

### Decision rules — stated before the run

| # | If | Then |
|---|---|---|
| R1 | fewer than **15** `Filter miss` rows total | build nothing; re-run in two weeks. A weekly report over one or two marks is noise, and an n-gram over three titles is superstition |
| R2 | the three fixable buckets together are **< 50 %** of `Filter miss` rows | build **M1 only** (the report). The proposer would automate the minority while the majority needs code changes |
| R3 | for the top code, **no** candidate has `hits ≥ 3` **and** `damage = 0` | build **M1 only**. The misses are not separable by title; a rule catching them also kills work the owner sent |
| R4 | (e) shows per-source passed-share flat while absolute volume rose | the report says so in one line, and the first action is a source-level decision (disable / narrow a source), not a filter rule |

R1–R3 are cumulative gates on M3–M4, not on M1.

---

## M1 — The weekly report (read-only)

**Files:** `hunter/filter_feedback.py` (pure: DB in, rendered text out),
`hunter/commands/filtermiss.py` (`/filtermiss [weeks]`, default 4),
`hunter/schedules/filter_feedback.py` + one registration in `schedules/__init__.py`.

Monday 09:30 Europe/Warsaw via `job_queue.run_daily(..., days=(1,))` — **PTB 22.7 maps
`days` 0–6 to Sunday–Saturday**, so Monday is `1`, not `0`. Owner decision 2026-09-19:
weekly digest **and** the on-demand command.

Content: `Filter miss` counts by reason for this window and the previous one; the bucket
table from M0(b); a "these need a code fix" line for the Layer-0 codes; a "these look
like a dedup/expired bug" line for `duplicate`/`expired`; `Skipped` counts on a separate
line, explicitly marked as *not* a filter error; per-source passed-share once
`postings_seen` has ≥ 2 weeks of rows.

Rates carry `n`; anything with `n < 10` prints the count instead of a percentage (same
posture as `tools/funnel_sources.py`).

Wrapped in `best_effort("filter.feedback")` — the report is peripheral, and three
consecutive failures should alert rather than degrade silently.

**Tests:** fixture DB → snapshot of the rendered report; a second fixture with the API
columns absent → the "no owner labels" degradation; a window with zero misses → a
one-line "nothing to report" (a digest that says nothing must still say it briefly).

**Rollback:** drop the schedule registration; the command is read-only.

---

## M2 — A machine-owned rule file

**File:** `filters.auto.yaml`, resolved next to `filters.yaml` by the same
`filter_profile._resolve_path` logic, so it lands in the per-user candidate dir in prod
exactly like `filters.yaml`. Owner decision 2026-09-19: a **separate** file, not the
owner's own `filters.yaml`.

Why separate, and why a new key rather than appending to `exclude_patterns`:
`exclude_patterns` is `replace` by design — the knob table in
`docs/FILTERS_YAML_PLAN.md` says "these encode the OWNER's stack; a Java-seeker
empties/replaces them". A machine appending one pattern there would have to write out
all ~60 builtin patterns and would freeze them against future builtin updates, and a
YAML rewrite destroys the owner's comments. A separate file with an additive key keeps
the builtins live, keeps machine rules visually distinct from hand-written ones, and
makes rollback a single `rm`.

**Shape:**

```yaml
# filters.auto.yaml - written by the bot. Hand edits are allowed but may be
# rewritten; put your own rules in filters.yaml.
active:
  extra_exclude_patterns:
    - pattern: '\bfull[- ]?stack\b'
      created: '2026-10-06'
      reason: stack             # the owner_reason code it came from
      hits: 5                   # Filter miss rows it matched at creation
      damage: 0                 # sent rows it matched at creation - must be 0
      from: ['<url>', '<url>']  # provenance
  exclude_companies: []
  extra_anti_hybrid_cities: []
shadow: {}                      # same shape; evaluated, never enforced
rejected: {}                    # same shape + rejected: <date>; never proposed again
```

**Loader change** (`hunter/filter_profile.py`, the only production file M2 touches):
after the existing user merge, entries under `active.extra_exclude_patterns` are
appended to `exclude_patterns`; `active.exclude_companies` and
`active.extra_anti_hybrid_cities` go through the existing `_extend_list`. `shadow` and
`rejected` are read by the reporter only and never reach the profile. The cache key
grows from `(filters, mtime, candidate, mtime)` to include the auto file and its mtime —
**without this a freshly written rule is not picked up**, which is the entire point of
the existing mtime design. Patterns run through the existing `_validate_patterns`, so a
bad regex is dropped with a warning and never crashes a hunt.

**Writes are atomic** (temp file in the same directory + `os.replace`). Precedent: the
2026-09-12 disk-full incident truncated `gsheets_token.json` to 0 bytes because the open
succeeded and the write hit ENOSPC.

**Tests:** absent file ⇒ profile byte-identical to today; `active` merges; `shadow` does
not; unknown key warns and is ignored; invalid regex dropped; an mtime change reloads
without a restart; a simulated write failure leaves no partial file.

**Rollback:** delete `filters.auto.yaml` → previous behaviour byte-for-byte, pinned by
the first test.

---

## M3 — The proposer (writes `shadow` only)

Runs inside the weekly job, after the report. Deterministic, `$0`.

For each fixable reason code with at least `FILTER_FEEDBACK_MIN_HITS` (default 3)
`Filter miss` rows in the window:

1. **Candidates** — normalized 1–3-word n-grams from the miss titles (listing title
   preferred), stopwords and the candidate's own `title_keywords` removed, escaped into
   `\b…\b`. For `company` the candidate is the normalized company name; for `location`
   the parsed city (`postings_seen.city`).
2. **Score** — `hits` (≥ MIN_HITS required), `damage` (**must be 0**, computed over every
   `applications` row whose `sent` classifies as `applied`, full history, no window),
   `radius` (last 60 days of `postings_seen`, informational).
3. **Pick** the max-`hits` candidate, tie-break on the shortest pattern — the shorter
   pattern is the more honest generalization and the easier one to read in a message.
4. **De-duplicate** against `active`, `shadow` and `rejected`, so a rule the owner
   already rejected is never proposed again.
5. **Write** it to `shadow` with full provenance and send **one** Telegram message: the
   pattern in plain words, `hits`, `damage: 0`, `radius`, the miss titles it came from,
   and two buttons.

Callback pattern `^fflt:`; its `CallbackQueryHandler` **must be registered before the
pattern-less `button_callback`**, which would otherwise answer the press as an
Apply/Skip on an unknown job — precedent and pinning test already exist for `/outcome`
(`hunter/telegram_bot.py:404-409`).

Nothing is applied in M3. `FILTER_FEEDBACK_PROPOSE_ENABLED` turns the proposer off while
leaving the report on.

**Tests:** a candidate that damages a sent row is never proposed (the Fairmarkit case as
a fixture); MIN_HITS respected; a rejected rule is not re-proposed; the message renders;
handler ordering pinned.

---

## M4 — Shadow week, then promotion

A `shadow` rule changes nothing in the hunt. Once a week the report evaluates every
shadow rule against `postings_seen` rows of the last 7 days — those rows carry the
listing title, so this needs no change anywhere in the pipeline — and prints:

```
\bfull[- ]?stack\b - would have dropped 9 of 412 listings this week:
  Full Stack Developer (Java+Angular) | Full-stack Engineer | ...  (up to 10)
```

The owner reads the list and presses **Enable** (the rule moves to `active`; the
loader's mtime cache makes it live without a restart) or **Reject** (moves to `rejected`
with a date). A shadow rule with no decision is reported as pending with its age and is
**never** auto-promoted.

After activation the effect stays visible for free: `postings_seen` keeps recording
`filter_verdict = exclude_pattern` for everything the rule drops, so a rule that starts
eating more than it should shows up as a rising rejection count for that source.

**Per-rule rollback:** move the entry back to `shadow`, or delete it.

This shadow week is also the plan's answer to the missing false-negative loop: a
*prospective* version of the same review — you see what the rule would remove before it
removes anything, on real listings, in the owner's own words.

---

## M5 — LLM assist (optional, not started)

Only if M1–M4 show the deterministic proposer repeatedly producing nothing usable
(R3-shaped failures recurring on real data): one `JUDGE_MODEL` call turning
`owner_reason_note` free text + the miss titles into a candidate pattern. The call
produces a *candidate only* — the same damage check and the same shadow week decide.
Per the standing rule against speculative LLM layers, this ships only if it reaches
decisions the deterministic path cannot; it is not part of the initial build.

---

## Risks

| Risk | What catches it |
|---|---|
| A rule silently kills a whole category (the blind spot: rejected vacancies are invisible to the owner) | `damage = 0` over the **full** sent history; shadow week showing real titles before enforcement; `postings_seen` rejection counts after; one-file rollback |
| Overfitting on three examples | `MIN_HITS`, the damage gate, the shadow week, and the `rejected` memory so a bad idea does not return weekly |
| Rules pile up unreviewed | `shadow` never affects behaviour; the report lists pending rules with their age |
| A machine write corrupts the rule file | atomic temp + `os.replace`; `_load_yaml` already returns `{}` on any read error; `_validate_patterns` drops bad regexes with a warning |
| A written rule is not picked up | auto-file mtime added to the loader cache key (the mechanism already exists for `filters.yaml`), with a test |
| API columns absent (dev fixture, bot-only DB) | `PRAGMA table_info` probe, precedent `funnel.py:225`; the report degrades to "no owner labels" |
| The report or proposer breaks something | `best_effort("filter.feedback")`; nothing in the hunt or apply path calls it |
| A `Filter miss` row has no listing title (manual paste) | fall back to `applications.title`; M0(c) reports how often |
| Listing title vs LLM-extracted title mismatch | candidates are built from listing titles, damage is checked against `applications.title` — the stricter side; a pattern that damages there is rejected even if the mismatch caused it |
| The real cause is source volume, not rules | M0(e) + R4; the report keeps per-source passed-share next to the proposals |

---

## Cost

`$0` LLM in M0–M4. Runtime: one weekly pass over two small tables plus one regex sweep
over ≤ 60 days of `postings_seen`. Storage: one small YAML file. Owner time: one message
a week, two buttons. M5, if ever built, is one `JUDGE_MODEL` call per week.

---

## Open questions

Closed by the owner on 2026-09-19:

1. How automatic? — **the machine writes to shadow, the owner activates with a button.**
2. Where do machine rules live? — **a separate `filters.auto.yaml`.**
3. Do `Skipped` labels feed proposals? — **no, report only.**
4. Cadence? — **weekly Monday digest plus an on-demand command.**

Still open, answerable once M0 shows the distribution:

5. `FILTER_FEEDBACK_MIN_HITS = 3` — the right floor, or 4/5? Three identical mistakes is
   a pattern in a corpus this size, but M0(d) will show whether 3-hit candidates are
   specific or generic.
6. Should an `active` rule be re-validated later — a quarterly re-run of the damage check
   against newly sent rows, reported not enforced? Cheap to add, easy to forget.
7. Report window: 4 weeks rolling, or since the last digest? The first is steadier, the
   second matches "what changed this week".
