# LLM Outage Resilience Plan

Branch: `fix/llm-billing-outage-resilience`
Owner report (2026-07-17): *"что происходит, если закончились деньги на апи? я сейчас
вижу в таблице кучу вакансий с fail. будут ли они обработаны потом еще раз?"* — plus two
adjacent questions (a processing queue / microservice, and falling back to the Claude Pro
subscription when the API budget is gone).

This file is the development contract for the branch. Each milestone is one commit with
tests. Update this file in the same commit when a decision changes.

---

## 1. Problem statement

The apply pipeline treats **every** non-zero exit of `apply_agent.py` as a permanent
property of the vacancy. A drained API balance is not that — it is a global, temporary
state of the whole bot. Today it is laundered into per-row permanent damage:

1. Anthropic answers an exhausted balance with HTTP **400 `invalid_request_error`**
   ("Your credit balance is too low…"). 400 is not in `llm_client._RETRYABLE`
   (`{429, 500, 502, 503, 529}`), so `call_llm` does not retry — correct, retrying a
   billing error is pointless.
2. `LLMError` reaches [`hunter/apply_api.py:414`](../hunter/apply_api.py) → one
   "❌ LLM failed" Telegram message → `sys.exit(1)`.
3. `apply_service.run_apply_agent_subprocess` maps exit 1 → `"fail"`.
4. `hunter/main.py::_auto_apply_all` → `add_failed(job)` → a **FAIL row** in the tracker.
   This is the "куча вакансий с fail" in the sheet.
5. `run_retry_failed` (07:45 / 18:45) retries the row. While the balance is still empty
   each retry calls `increment_fail_count`, and at `MAX_FAIL_RETRIES = 3`
   ([`hunter/tracker.py:448`](../hunter/tracker.py)) the row is declared "🚫 Giving up"
   and permanently filtered out of `get_failed_jobs`. **There is no way to reset
   `fail_count`** — no command, no tool. The vacancy is dead forever.

`_CONSECUTIVE_FAIL_LIMIT = 3` limits the blast radius per slot (a batch stops after three
failures in a row), so an outage burns ~3 rows per retry slot rather than the whole list —
but across a two-day outage that is still a permanent, silent loss.

The precedent for the correct shape already exists in this codebase: a transient HTTP 429
during **fetch** has its own exit code (`APPLY_RATE_LIMITED_EXIT_CODE = 45`), its own
outcome (`"rate_limited"`), and deliberately does **not** escalate the permanent counter
([`hunter/main.py:538`](../hunter/main.py)). An LLM billing/auth outage belongs in exactly
that class. This plan extends the pattern to the LLM boundary.

### Out of scope (decided, do not build)

- **A queue microservice** (owner question 1). There is no queue and none is needed. The
  hunt is `fetch → filter → dedup → capped = jobs[:MAX_JOBS_PER_RUN] → sequential apply`
  in one process ([`hunter/main.py:355`](../hunter/main.py)). Overflow beyond the cap is
  dropped without a tracker row, so the **next** hunt of that source re-fetches it and it
  passes dedup again — the job-board listing *is* the queue. The cap is per source (each
  source gets its own staggered slot), so at 40/source it effectively never fires. Volume
  is tens of vacancies/day; state is already durable (SQLite) and serialized
  (`_hunt_lock`). A microservice would add deploy, network and schema-drift surface for
  zero gain. Revisit only if the `⚠️ Capped to N (skipped M)` message becomes routine —
  and then the cheap answer is persisting overflow as a tracker row, not a service.
- **Retrying billing errors inside `call_llm`.** A 400 on an empty balance is not
  transient at call scale; burning the 6-attempt backoff ladder (~10 min) per vacancy
  makes the outage slower to detect, not survivable.

---

## 2. Milestones

### M0 — Confirm the diagnosis (no code)

The FAIL rows the owner sees are **assumed** to be billing; the analysis above is derived
from the code path, not from prod data. Before shipping M1, read
`logs/hunter_errors.log` (or the Drive copy) and grep the actual `[apply_agent] LLM ERROR:`
lines. Three plausible causes with different fixes:

| Signature | Cause | Covered by |
|---|---|---|
| `credit balance is too low` / `billing` | drained balance | M1 |
| `401` / `authentication_error` / `invalid x-api-key` | bad/rotated key | M1 (same class) |
| `429` / `rate_limit_error` after 6 retries | genuine throttling | already handled upstream; M1 must NOT swallow it |
| fetch errors, timeouts | unrelated to this plan | — |

Record the finding here before writing code. If the FAILs are *not* billing, M1 still
stands on its own merits but the priority order may change.

**FINDING (2026-07-17, from the Drive log mirror `G:\My Drive\Job Hunter\Logs*`,
retention 2026-05-28 → 2026-07-17):**

- **Zero LLM errors of any kind in every retained log.** `grep "credit balance|LLM
  ERROR"` across all ~50 log files matches nothing. A billing outage has never actually
  happened (within retention) — the FAIL rows are **not** billing.
- The actual FAIL causes, by volume:
  1. **findmyremote.ai link-rot 404s** (bulk of the recent rows): stale
     `findmyremote.ai/companies/{c}/jobs/{slug}` permalinks relayed by the
     `findmyremote_frontend` Telegram channel 404 once the job is deleted →
     `FETCH ERROR: 404 Client Error` → FAIL. The root cause was already fixed
     2026-07-12 (findmyremote fetch now goes through the JSON API and returns a clean
     EXPIRED marker), **but** the rows created 2026-07-11 burned `fail_count` to 3 in the
     07-12 01:08 / 02:20 retry slots — the log shows the "Giving up … after 3 failures"
     lines — *before* the fixed code could reach them. They are now permanently dead
     (§1 step 5), even though a single retry through today's code would resolve them as
     $0 EXPIRED.
  2. **LinkedIn fetches with `LINKEDIN_STORAGE_STATE` unset** — known, documented ops
     item (CLAUDE.md calls it "the single biggest source of FAIL rows"). Config, not code.
  3. Occasional **too-little-text** fetches (e.g. Ashby JS shell, 79 chars).
- **Priority change:** M3 (`/retry_reset`) is promoted — it is the fix for the "куча
  FAIL" the owner actually sees: reviving the dead rows lets the next retry slot turn
  the 404-rotten ones into clean EXPIRED via the already-fixed fetch path. M1/M2 remain
  valid as *proactive* hardening (the failure mode is real, just hasn't fired yet), and
  M1 is still a prerequisite for M4, which the owner explicitly requested (question 3).
  New order: **M3 → M1 → M2 → M4**.
- Side observation, out of scope for this branch: the Drive `Logs` folder itself is
  duplicated (`Logs`, `Logs (1)` … `Logs (7)`) — the same same-named-siblings race fixed
  for date folders in PR #163, but `upload_log_file`'s "Logs" folder predates the fix and
  `tools/dedup_drive_folders.py` only walks date folders. Flagged as a separate task.

### M1 — Classify LLM outages as a distinct, non-escalating outcome

The core fix. New error class → new exit code → new outcome → no permanent damage.

1. **`llm_client.py`**: add `LLMOutageError(LLMError)`. Raise it from `_call_anthropic` /
   `_call_openai` / `_call_openrouter` when a **non-retryable** `APIStatusError` looks like
   an account-level problem rather than a request-level one:
   - status `400` **and** message matches credit/billing/quota patterns, or
   - status `401` / `403` (authentication / permission — a key problem, never the
     vacancy's fault), or
   - status `402`.

   Detection lives in one shared helper (`_is_outage_status(status, message)`) so all three
   providers classify identically. Be conservative: a 400 that is *not* billing-shaped
   (e.g. a genuine malformed request, a model that rejects a param) must stay a plain
   `LLMError` — misclassifying a code bug as an outage would silently retry it forever.
   `LLMOutageError` is a subclass of `LLMError`, so every existing `except LLMError`
   caller keeps working unchanged unless it opts in.

2. **`hunter/apply_shared.py`**: `APPLY_LLM_OUTAGE_EXIT_CODE = 46` (44 = MANUAL, 45 =
   rate-limited; 46 is the next free code). Document it next to the other two.

3. **`hunter/apply_api.py`**: catch `LLMOutageError` **before** the existing
   `except LLMError` at line 414 → its own Telegram message ("💳 LLM outage — no docs
   generated, vacancy left untouched") → `sys.exit(APPLY_LLM_OUTAGE_EXIT_CODE)`. Apply the
   same handling at every other `call_llm` site that can abort the run. The judge, verdict,
   refine and outreach stages are already best-effort and must stay that way — an outage
   there degrades the run, it does not fail it.

4. **`hunter/services/apply_service.py`**: `_APPLY_LLM_OUTAGE_EXIT_CODE = 46`; extend
   `ApplyOutcome` with `"llm_outage"`; map exit 46 in **both**
   `run_apply_agent_subprocess` and `run_apply_agent_for_url`.

5. **`hunter/main.py`** — the payoff:
   - `_auto_apply_all`: on `"llm_outage"` do **not** call `add_failed`. Leave the job with
     no tracker row at all, so the next hunt re-fetches it and it passes dedup again (the
     "listing is the queue" model above). Count it separately in the batch summary.
   - `_retry_failed`: on `"llm_outage"` do **not** call `increment_fail_count` — the row
     stays retryable at its current count, exactly like the `"rate_limited"` branch.
   - Both loops: **break immediately** on the first `llm_outage` (not after
     `_CONSECUTIVE_FAIL_LIMIT`). Every further job in the batch costs a real fetch (and
     the anti-bot budget that goes with it) to hit the same wall.
   - One Telegram alert, not one per job: "💳 LLM outage — apply paused", with the
     provider/model and the error snippet.

**Tests** (`tests/test_llm_outage.py`): classification table (billing-400 → outage;
non-billing 400 → plain LLMError; 401/403 → outage; 429 → still `LLMRateLimitError`,
i.e. retried, not misfiled as an outage); exit-code plumbing 46 → `"llm_outage"` for both
service entry points; `_auto_apply_all` writes no FAIL row and stops at the first outage;
`_retry_failed` leaves `fail_count` untouched. The `fail_count`-untouched test is the one
that must fail if M1 is reverted — verify that by hand-mutating.

### M2 — Pause auto-apply for the duration of the outage

M1 stops one batch. Without M2, the next source slot (~40 min later, 25 sources × 3 base
cycles/day) starts fetching again and dies on the same wall — the fetch cost and the
alert repeat all day.

- New DB config key `llm_outage_until` (unix ts) in the existing `config` key-value table
  (same pattern as `dual_apply_enabled` / `dual_shadow_profile`,
  [`hunter/llm_profiles.py:114`](../hunter/llm_profiles.py)). The DB, not a module global:
  the apply pipeline runs in a **subprocess**, so a process-local flag cannot be seen by
  the bot — the same reason `source_health`'s counters live in SQLite.
- `main.run_hunt`'s AUTO branch and `run_retry_failed` consult it via the existing
  `_check_apply_ready` seam (which already returns "why apply can't run" and already
  reports it to Telegram) and skip the apply step while it is in the future. **Fetch,
  filter and dedup still run** — the hunt keeps reporting what it found, only generation
  is paused.
- Cooldown: `LLM_OUTAGE_PAUSE_MIN`, default **60**. Not indefinite — a top-up should heal
  the bot on its own, without the owner remembering to clear a flag. After it expires the
  next slot probes naturally: one job, one API call. If the balance is still empty, M1
  fires again and re-arms the pause. Manual clear: `/llm outage clear`.
- The alert is sent when the pause is **armed**, not per skipped slot — reuse
  `oauth_alert`'s cooldown-deduplicated shape.
- `/status` shows the pause when armed.

### M3 — Revive rows that already gave up ✅ DONE (2026-07-17)

The rows already at `fail_count >= 3` are invisible to `get_failed_jobs` forever, and the
owner's sheet already contains some. M1 stops the bleeding; it does not heal what is
already dead.

- `tracker.reset_fail_counts(urls: list[str] | None = None) -> int` — reset to 0 for the
  given URLs, or for every FAIL row when `None`. One function, no command-layer logic.
- `/retry_reset` Telegram command: with no args, report how many rows are in the
  gave-up state; `/retry_reset all` resets them; `/retry_reset <url>` resets one. The
  report-first default matters — this re-queues real LLM spend.
- The next `RETRY_FAILED_TIMES` slot then picks them up through the existing loop; no
  change to the retry machinery itself.

### M4 — CLI (Pro subscription) fallback — **opt-in, ship last**

Owner question 3. Half of this already exists in the opposite direction:
[`apply_agent.py:82`](../apply_agent.py) tries the **CLI first** when `claude` is on PATH
and falls back to the API when the CLI fails. In the deploy image `claude` is not
installed (nothing in the `Dockerfile`), so prod is API-only and there is no path back.

Two independent pieces, both required:

1. **Image + auth.** Install the Claude CLI in the `Dockerfile` and mount a logged-in
   `~/.claude` credentials volume. The login is interactive and issues an OAuth token tied
   to the owner's **personal** subscription — it must be done once from the owner's own
   machine and mounted, exactly like `gsheets_token.json`. Never commit it. This is a
   deliberate ops decision, not a code detail: a personal subscription token on the
   server is a real trade-off and the owner signs off before this ships.
2. **Wiring.** In `apply_agent.main`'s API branch, catch `LLMOutageError` (M1) and retry
   once through `main_cli` instead of exiting 46, gated by `LLM_OUTAGE_FALLBACK_CLI`
   (default **false**). On CLI success the run is a normal success — no FAIL row, no
   outage pause. On CLI failure, exit 46 as usual and let M1/M2 take over.

Ordering: M4 depends on M1's error class and reuses M2's pause as its own failure path, so
it lands last. It is also the only milestone the owner may reasonably decline — M1–M3
stand alone and already answer *"будут ли они обработаны потом еще раз?"* with a yes.

---

## 3. Decisions log

| Date | Decision |
|---|---|
| 2026-07-17 | No queue microservice. The listing is the queue; the cap is per source and effectively never fires. Documented in §1 "Out of scope". |
| 2026-07-17 | Billing/auth errors get the `rate_limited` treatment (no `fail_count` escalation), not a new retry ladder inside `call_llm`. |
| 2026-07-17 | An outage leaves **no tracker row** for a new job (re-fetched next hunt) and leaves an existing FAIL row's count untouched. |
| 2026-07-17 | The outage pause is time-boxed (`LLM_OUTAGE_PAUSE_MIN`, default 60), not sticky — a top-up must heal the bot without owner action. |
| 2026-07-17 | CLI-subscription fallback is opt-in (`LLM_OUTAGE_FALLBACK_CLI=false`) and ships last; it needs an ops decision about a personal token on the server. |
| 2026-07-17 | M0 verdict: no billing outage has ever occurred (log retention 05-28→07-17); the visible FAIL rows are findmyremote link-rot (dead at fail_count=3, root cause already fixed 07-12) + LinkedIn no-session. Milestone order changed to **M3 → M1 → M2 → M4**. |
