# Project Review — June 2026

Snapshot at review time: 21 job sources, 1283 tests, ~26K lines in `hunter/`,
ruff CI gate, clean PR-based git history. Reviewed on branch from `origin/master`
(b21cfe3).

---

## What is good (keep doing this)

**1. Architectural discipline.** All six refactoring phases were finished, not
abandoned mid-way: the 1967-line `telegram_bot.py` monolith was split into
`bot/` + `commands/` + `schedules/`, `job_fetch/` was merged into `sources/`,
the tracker migrated to SQLite, the project is pip-installable.

**2. Root-cause-analysis culture.** The work log shows a consistent pattern:
every fix starts with tracing the cause in production (false-EXPIRED from an
expired OAuth token, HTTP 429 from parallel gmail_enricher fetches, Polish text
in EN resumes). Fixes are targeted and ship with protective invariants
(`_RECONCILE_MIN_RATIO`, `sheets_row IS NOT NULL`), not symptomatic patches.

**3. Defense in depth for LLM output quality.** The most mature part of the
project: prompt rules (RED LINES) → sanitizer → compliance/prestige scrubs →
skills-gloss dedup → language enforce-gate that **blocks delivery** of a broken
document. The insight that prompts cannot be trusted and deterministic
post-controls are required is correct and rare.

**4. Test coverage grows with the code.** 1283 tests; every fix brings its own.
Real job-posting fixtures per CV track.

**5. Operational resilience.** Best-effort integrations (Sheets/Drive failures
never break the pipeline), dedup self-heal after DB loss, rate limiter with
per-host overrides, circuit breaker on retries, daily tracker backups.

---

## What could be better

**1. `apply_shared.py` is the new growing monolith (1320 lines).** The original
`apply_agent.py` was split, but "shared helpers" became a dumping ground:
constants, Telegram notifications, the CL review loop, validation, all content
scrubs, the language gate. Every new CV-quality fix adds 100–200 lines there.

**2. The scrubs are whack-a-mole.** `_strip_compliance_claims`,
`_strip_prestige_claims`, `_dedup_skill_glosses` — each appeared after one
specific broken CV from production. Tempered-clause regexes are fragile and
will keep breaking on new LLM phrasings. The systemic alternative is an
**LLM-as-judge verification pass** (see `CV_JUDGE_PLAN.md`): one cheap call
that checks every claim in the generated CV against `candidate_profile.md` and
the job posting and returns a violations list — closing the whole class of
fabrications instead of one phrasing at a time.

**3. No feedback loop on outcomes.** The bot applies well but doesn't learn:
which sources produce responses? Does the ATS score correlate with replies?
Which tracks (Angular vs React) convert? The data already exists (`Sent`,
`/check_responses`, Sheets column L) — only the analytics layer is missing.

**4. `tracker.py` is still ~1050 lines** and Excel-export write paths re-open
the workbook per call (Known Issue #7 partially current).

**5. ATS score is unvalidated.** `scikit-learn` is a dependency for scoring,
but without response data (item 3) there is no evidence the number predicts
anything.

**6. Scraper breakage is silent.** A source returning 0 jobs is
indistinguishable from "no new vacancies". The health table in CLAUDE.md is
maintained by hand; the `scraper-health-checker` agent is manual.

**7. Typing.** A mypy config exists but is not a CI gate (only ruff is).

---

## Development roadmap

### Phase A — Code hygiene (low risk, 1–2 sessions)

- **A.1** Split `apply_shared.py` into `apply_scrubs.py` (compliance/prestige/
  glosses), `apply_review.py` (CL review loop), `apply_notify.py` (Telegram),
  keeping only constants/validation/`ApplyError` in shared. Mechanical move;
  tests already exist.
- **A.2** Widen the ruff gate to `tests/` and `tools/` (pyproject already says
  "widen coverage over time").
- **A.3** Add mypy to CI at least for `hunter/sources/` and `hunter/tracker.py`.

### Phase B — Scraper health monitoring (high value, medium effort)

- **B.1** Per-source baseline: store per-run yield (`source.search()` count)
  history in SQLite.
- **B.2** Alert heuristic: a source that consistently yielded N>0 returning 0
  for three consecutive runs → Telegram alert "source X looks broken".
- **B.3** `/health` command: source / last successful run / average yield /
  status. Replaces the hand-maintained CLAUDE.md table.

### Phase C — CV verification by a second LLM pass (main quality investment)

See **`docs/CV_JUDGE_PLAN.md`** for the full implementation plan.

- **C.1** After generation and the existing scrubs: a judge call (Haiku, cheap)
  — "here is the candidate profile, the job posting, and the CV — list claims
  present in neither". JSON response.
- **C.2** Violations → automatic targeted repair, or a warning in the Telegram
  card with the documents.
- **C.3** Existing regex scrubs stay as the fast first echelon; the judge
  catches what they miss. Metrics will show over time whether the regexes can
  be simplified.

### Phase D — Funnel analytics (turn data into decisions)

- **D.1** `/funnel` command: per period — found → filtered → applied →
  responded, by source. Data already in tracker.db + Sheets column L.
- **D.2** Link `/check_responses` to the tracker: stamp a "Response" marker on
  the matched row.
- **D.3** After a month of data: decide which sources/tracks actually convert
  and whether all 21 sources are worth their maintenance surface.
- **D.4** Optional: extend the Sheets Stats tab with per-source QUERYs.

### Phase E — Operations (as needed)

- **E.1** Rebuild the production image so Inhire/Playwright works in prod
  (noted in Known Issues, not yet done).
- **E.2** Re-check Known Issue #7 post-SQLite: most of it may be stale; clean
  up CLAUDE.md accordingly.
- **E.3** OAuth-token expiry alerting (Sheets/Gmail): `invalid_grant` already
  caused a false-EXPIRED cascade once — a Telegram "token is dead" alert beats
  cleaning up consequences.

### Explicit non-goals

- **No new sources for volume's sake** — Queue 3 recon correctly concluded
  "low ROI". 21 sources is already a large maintenance surface; Phase D will
  show which ones actually feed the funnel.
- **No filter rewrite** (Known Issue #6): 452 lines with German-language
  regexes are ugly but work and are tested — touch only on real false
  negatives.
- **No web UI**: Telegram + Sheets cover the single-user workflow.

### Priority order

**C (judge verification) > B (health monitoring) > D (funnel analytics) >
A (hygiene) > E (ops).** Phase C directly affects what employers see; B and D
keep the system from degrading silently and direct effort where responses
actually come from.
