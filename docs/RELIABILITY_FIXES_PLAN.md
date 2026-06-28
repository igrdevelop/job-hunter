# Reliability Fixes — Apply FAIL Flood + Dual-Apply Safety

**Status:** PLAN (not yet implemented)
**Branch:** `plan/reliability-fixes` (off `origin/master` @ c4c102d)
**Author:** sonnet, 2026-06-28
**Trigger:** User reported a flood of `FAIL` rows in the Sheets tracker and suspected
PR #107 (DeepSeek/OpenRouter + dual-apply). Investigation cleared #107 and surfaced
the real, pre-existing causes plus one latent risk the dual feature introduced.

---

## 1. Diagnosis (evidence-based)

Source of truth: prod log `G:/My Drive/Job Hunter/Logs/2026-06-28.log` (full day,
00:00–19:22) + `git diff --stat 02725ed^ 02725ed` (what #107 actually changed).

### 1.1 #107 is NOT the cause

`git diff` proves #107 did **not** touch the apply-failure surface:

```
#107 changed:  llm_client.py, llm_profiles.py, apply_api/cli/shared, dual_apply,
               commands/{llm,dual}, config.py, generate_docs.py, llm_cost.py, tests
#107 did NOT touch:  hunter/tracker.py, hunter/filters.py, hunter/main.py, hunter/sources/
```

So dedup, filtering, fetching and FAIL-row writing are byte-identical to before #107.
Log corroborates: `LLM ERROR` = 0, `authentication` = 0, `TIMEOUT` = 2 (not dual),
active profile = `sonnet`. The timing was coincidental (user opened the sheet right
after the merge).

### 1.2 Real causes of the FAIL flood (all at the FETCH stage)

Every FAIL row has **no folder** → the apply died before generating docs, i.e. it
could not download the posting. Breakdown from the log:

| Cause | Log signature | Sources affected |
|---|---|---|
| **LinkedIn has no logged-in session** | `[linkedin] LINKEDIN_STORAGE_STATE not set — falling back to HTML fetch` → then `429 Too Many Requests` | every `linkedin.com/jobs/view/*` apply |
| **pracuj Cloudflare block** | `[pracuj] cloudscraper failed (403 Forbidden), trying plain requests` | pracuj.pl |
| **gmail_enricher rate-limit storm** | 237× `429 Too Many Requests` from `hunter.gmail_enricher` (+ `hunter.sources.pracuj rate-limited (429)`) | LinkedIn + pracuj detail fetches during the *hunt* |
| **Flaky remote boards** | `[auto-apply] FAIL D2B:` / `Bjak:` (empty stderr — clean non-zero exit) | remoteleaf (D2B), himalayas (Bjak), workingnomads |

Volume context: `[Hunt] After filter: 154 jobs` in a single cycle — large fan-out, so
even a moderate per-source failure rate produces many FAIL rows. Each failed apply
writes a stub row (`add_failed`) and retries 3× before "Giving up" (`MAX_FAIL_RETRIES`),
which is what fills the sheet.

### 1.3 Latent risk introduced by #107 (dual-apply) — not yet biting

The shadow (V3) runs **inside the same `apply_agent.py` subprocess** as the primary,
under the bot's single `APPLY_AGENT_TIMEOUT_SEC = 900s` wall-clock limit
(`run_apply_agent_subprocess`). Sequence when dual is ON:

```
primary (sonnet ~300–450s)  →  writes docs + tracker row (SUCCESS committed)
shadow  (V3 ~300–480s, can spike on the ATS rewrite loop)
```

If `primary + shadow > 900s`, the bot kills the whole subprocess → non-zero exit →
the caller marks an **already-successful** apply as FAIL / increments its fail count.
This violates the design contract ("the shadow must never affect the boevoy result").
Today it's dormant (`TIMEOUT` = 2 in the log, neither dual-related), but dual is now
ON in prod, so it should be removed before it bites.

---

## 2. Fixes (ranked by impact / effort)

### Fix A — Dual-apply safety (mine to fix; small, do first)

**Problem:** shadow shares the primary's 900s subprocess timeout (1.3).

**Goal:** the shadow can never change the primary's recorded outcome, timing, or exit code.

**Chosen approach — detach the shadow into its own process.**
After the primary succeeds, `apply_agent.main()` currently calls `_maybe_run_shadow()`
*inline*. Replace with a **fire-and-forget detached subprocess**:

- New tiny entry `python -m hunter.dual_apply <primary_folder> [--full]` (add a
  `__main__` block / CLI shim to `dual_apply.py`).
- `_maybe_run_shadow()` launches it via `subprocess.Popen` with **no wait**, detached
  (`start_new_session=True` on POSIX / `DETACHED_PROCESS` on Windows), stdout/stderr to
  a log file in the shadow folder. The primary subprocess then exits 0 immediately on
  the primary's own schedule.
- The detached shadow gets its **own** time budget (`DUAL_SHADOW_TIMEOUT_SEC`, default
  900) so it can't run forever.

**Why detach (vs. just shrinking the inline budget):** only detachment fully
guarantees the shadow can't touch the primary's exit code / the bot's timeout. Matches
the user's explicit "чисто тень" requirement.

**Risk:** low — best-effort already; detachment only removes coupling. Orphan process
on container stop is acceptable (short-lived, writes only into its own subfolder).

**Files:** `hunter/dual_apply.py` (add `__main__` + keep `run_shadow` importable),
`apply_agent.py` (`_maybe_run_shadow` → Popen detach), `hunter/config.py`
(`DUAL_SHADOW_TIMEOUT_SEC`), tests in `tests/test_dual_apply.py`.

**Tests:** `_maybe_run_shadow` builds the right command & does not block; guard clauses
unchanged; detached launch is mocked (assert Popen called, parent returns immediately).

---

### Fix B — gmail_enricher 429 storm (pre-existing; medium)

**Problem:** 237× 429 from `gmail_enricher` hammering LinkedIn + pracuj detail pages
during the hunt. There is already a `DomainLimiter` (`hunter/rate_limiter.py`) and
`GMAIL_ENRICH_*` knobs, but the current settings are too aggressive for these two hosts.

**Approach (config-first, then code if needed):**
1. **Tune knobs** (no code): lower `GMAIL_ENRICH_CONCURRENCY`, add per-host delay for
   `linkedin.com` + `pracuj.pl` via the existing override mechanism
   (`PRACUJ_HOST_CONCURRENCY`/`PRACUJ_HOST_DELAY_SEC` already exist — add a LinkedIn
   equivalent, or a generic per-host table).
2. **Skip enrichment for hosts that hard-block** anyway: LinkedIn guest detail pages
   429 without a session (see Fix C), pracuj Cloudflares — enriching them is mostly
   wasted requests that also poison the shared rate budget. Add a per-host "don't
   enrich, use the alert stub" allowlist.
3. **Backoff on 429** in the enricher: respect `Retry-After`, exponential backoff,
   and stop hammering a host after N consecutive 429s in a cycle (circuit breaker —
   mirror the pattern already in `pracuj._fetch_detail_html`).

**Risk:** medium — touches the hunt path. Gate behind config so it can be tuned in prod
without redeploy.

**Files:** `hunter/gmail_enricher.py`, `hunter/rate_limiter.py` (generic per-host
overrides), `hunter/config.py`, tests.

---

### Fix C — LinkedIn session (user action, biggest single win)

**Problem:** `LINKEDIN_STORAGE_STATE not set` → every LinkedIn apply falls back to
guest HTML fetch → 429 → FAIL. LinkedIn is the single largest FAIL contributor.

**Approach (no code change — ops):**
1. Run `python tools/linkedin_login.py` locally → produces a Playwright storage-state
   JSON.
2. Set `LINKEDIN_STORAGE_STATE=/path/to/state.json` in prod `.env` and mount the file
   into the container (like the Google tokens).
3. Verify: one LinkedIn apply should fetch full text instead of 429.

**Owner:** user (needs their LinkedIn login). Document the exact steps in the PR / README.

**Optional code follow-up:** if the session can't be provided, treat LinkedIn fetch
failures as `rate_limited` (no permanent FAIL escalation) instead of FAIL, so they don't
flood the sheet and "Giving up" after 3 tries — see Fix D.

---

### Fix D — Reduce FAIL-row noise (pre-existing; optional, do last)

**Problem:** every fetch failure writes a stub FAIL row + retries 3× → the sheet fills
with `[linkedin]`/`[pracuj]`/board FAIL rows that carry no useful data.

**Options (pick after A–C):**
1. **Don't persist a tracker row for pure-fetch failures** — distinguish "fetch failed"
   (transient, network/anti-bot) from "apply genuinely failed after fetch" (worth
   tracking). Only the latter gets a FAIL row; the former is retried in-memory without
   polluting the sheet.
2. **Classify anti-bot 403/429 as `rate_limited`** (already a distinct outcome that does
   NOT escalate the fail counter) for LinkedIn/pracuj/Cloudflare hosts, so they retry
   quietly instead of "Giving up" and leaving a dead row.
3. **Cleanup tool** — a `tools/` script to delete `FAIL` rows whose URL later succeeded
   or that the bot gave up on, to tidy the historical sheet.

**Risk:** medium — changes what lands in the tracker. Must preserve genuine-failure
visibility (don't hide real apply bugs).

**Files:** `hunter/main.py` (retry/outcome handling), `hunter/services/apply_service.py`
(outcome classification), `hunter/tracker.py` (maybe a `fetch_failed` status distinct
from `FAIL`), `tools/` cleanup script, tests.

---

## 3. Sequencing & PRs

| PR | Scope | Owner | Risk |
|----|-------|-------|------|
| 1 | **Fix A** — dual-apply detach + own timeout | me | low |
| 2 | **Fix B** — gmail_enricher throttle/backoff/skip | me | medium |
| 3 | **Fix C** — LinkedIn session (docs + optional fetch-fail reclassify) | user + me | low |
| 4 | **Fix D** — FAIL-row noise reduction + cleanup tool | me | medium |

One branch per PR, each off fresh `origin/master` (per project git workflow). Each PR:
`pytest tests/` green + `ruff check .` clean + a live verification note.

## 4. Out of scope / explicitly NOT doing

- Reverting or changing #107 (it's clean — confirmed by diff + log).
- Rewriting the dedup or filter logic (unrelated to this issue).
- pracuj/Cloudflare hard-bypass (known hard problem; tracked elsewhere).

## 5. Open questions for the user

1. Priority order — default is A → B → C → D. OK?
2. Fix D direction: hide fetch-failure rows entirely, or keep them but mark a distinct
   `fetch_failed` status (so you can still see what was attempted)?
3. Can you provide the LinkedIn session (Fix C)? It's the biggest single win and only
   you can log in.
