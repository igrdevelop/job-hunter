# Apply Failure Queues Plan

**Status:** in progress. M0 run on prod 2026-09-21 → verdict "M1 + M4 only";
M2/M3 closed. M1 shipped. M4 open. See "M0 result" below; it corrects the
Problem section's picture of this particular incident.
**Date:** 2026-09-21
**Motivation:** From 2026-09-10 to 2026-09-21, every apply that went through
the full CLI pipeline (`hunter/apply_cli.py::main_cli`) died before doing any
work. PR #262 (2026-09-10 ~20:20, security M1) replaced
`--dangerously-skip-permissions` with `--allowedTools`/`--disallowedTools`
and left the `/apply …` prompt as the LAST argv element. Both flags are
variadic in the `claude` CLI, so the prompt was split into "deny rules"
(`Permission deny rule "/apply" matches no known tool`), `claude` got no
prompt and exited 1. Fixed by #284. The bug itself is ordinary. The real
problem is that **eleven days of a 100% systemic failure looked exactly like
eleven days of normal per-vacancy failures**, and nobody (owner or bot) could
tell how many vacancies it cost.

## Problem

Today the apply pipeline has one bucket for "it didn't work": a `FAIL` row.
Everything that follows treats that row as the vacancy's own fault:

1. **Same message for both classes.** A broken argv, a missing module, a
   revoked CLI login and a posting that 404s all produce the same Telegram
   line: `❌ Failed: <company> — <title>` (`hunter/apply_worker.py:310`).
   A few of those arrive every day anyway, so a flood of them reads as noise.
2. **The breaker only naps.** `_CONSECUTIVE_FAIL_LIMIT = 3` →
   `🛑 3 consecutive failures — pausing 5 min` → the worker resumes and burns
   the next three jobs (`hunter/apply_worker.py:449-458`). Over a systemic
   outage it converts the whole `PENDING` queue into `FAIL` rows, three at a
   time, forever.
3. **Retries spend the vacancy's budget on our bug.** `_retry_failed`
   (`hunter/main.py:655`) runs twice a day and bumps `fail_count` on every
   failed retry. At `MAX_FAIL_RETRIES = 3` (`hunter/tracker.py:628`) the row
   drops out of the retry loop for good. A vacancy caught by a systemic bug
   is dead within ~1.5 days, and only `/retry_reset` (manual, all-or-one-URL)
   brings it back.
4. **No post-deploy check.** `tests/test_apply_cli_cmd.py` checked the argv
   list, and even pinned the broken order (`cmd[-1] == "/apply …"`). Nothing
   ever ran the real `claude` binary on the real argv after a deploy.
5. **We can't count the damage.** `logs/apply_failures.jsonl` holds the
   evidence, but nothing groups it, and its `error` field is inconsistent:
   the worker path stores the HEAD of the output (`log_apply_failure`
   truncates to the first 500 chars), while the manual path stores the
   TAIL (`snippet = detail[-600:]`, then the first 500 of that).

This is not the first time. 2026-07-12: every Himalayas job FAILed at
Step 1 (403 on the detail page) until someone noticed "recurring FAILs"
(AGENT_LOG). That one was systemic for one **source**, not for the whole
pipeline, which matters for how wide a block should be (M3).

## Non-goals

- **No LLM anywhere.** Classification is deterministic: signature
  normalisation, a pattern list, recurrence counting.
- **No change to vacancy-class behaviour.** A 404 posting, a too-short text,
  malformed model JSON: all still become `FAIL` with a retry budget, exactly
  as today. Ambiguous means `vacancy`. The new path only activates when a
  failure is clearly ours.
- **No new process or broker.** The queue stays the `applications` table
  (same stance as docs/HUNT_APPLY_SPLIT_PLAN.md: the job-board listing and
  the tracker ARE the queue).
- **API-path outages are out of scope.** An account/billing outage already
  has its own class (`llm_outage`, exit 46, M2 pause —
  docs/LLM_OUTAGE_RESILIENCE_PLAN.md). This plan copies its shape; it does
  not touch it.
- **No automatic mass-revive of history.** M0 reports; reviving the rows the
  2026-09-10 bug killed is a one-off owner action (`/retry_reset`).

## M0 — Measure (read-only, $0)

`tools/fail_signatures.py` over `logs/apply_failures.jsonl` (+ its rotated
`.1`–`.5` siblings), optionally joined with `tracker.db` opened read-only.

For every failure record it computes a **signature**: the error text with
URLs, UUIDs/hex ids, numbers, quoted strings and paths replaced by
placeholders, consecutive duplicate lines collapsed, the first two distinct
lines kept. The normaliser lives in `hunter/failure_signature.py` so M2
reuses byte-identical logic; a separate copy in the tool would drift from
the classifier it is meant to calibrate.

Per signature it reports: record count, **distinct vacancies** (by
`normalize_url`), first/last seen, max distinct vacancies in any 6-hour
window, `cli_mode` share, exit codes, and the source-domain spread (one
domain means source-scoped, many means global). With `--db` it adds the
current tracker state of those URLs: still `FAIL` and retryable, `FAIL`
and given up (`fail_count >= MAX_FAIL_RETRIES`), or later applied/skipped.
That last number is the damage count for 2026-09-10.

```bash
docker exec job-hunter python tools/fail_signatures.py --db /app/db/tracker.db
docker exec job-hunter python tools/fail_signatures.py --db /app/db/tracker.db --json > /tmp/sigs.json
```

**Decision rules (fixed before the run):**

1. **Build M2+M3 (classifier + BLOCKED queue)** if, EXCLUDING the
   `Permission deny rule …` signature, the covered window holds at least one
   signature with ≥ 5 distinct vacancies AND a 6-hour peak of ≥ 3 distinct
   vacancies. That means systemic failures recur, and the 2026-09-10 incident
   was not a one-off class. If no such signature exists, ship M1 (canary) and
   M4 (rate alert) only, and close M2/M3 as not worth the machinery.
2. **Recurrence threshold for M2.** List every signature that crosses
   "≥ 3 distinct vacancies in 6 h" and label each one by hand
   (system/vacancy). If any **vacancy**-class signature crosses it (e.g. a
   generic "too short" message), the M2 threshold must rise until none does,
   or that signature goes on an explicit vacancy-allowlist. Zero false
   BLOCKs on the historical window is the bar.
3. **Scope.** If ≥ 1 real systemic signature is confined to one source
   domain (the Himalayas shape), M3 needs per-source blocking. If every
   systemic signature is cross-source, M3 ships global-only.
4. **Unmeasurable.** If the retained log covers < 7 days (rotation, or the
   `logs/` volume was reset), say so and fall back to `logs/apply_stdout/`
   transcripts (7-day retention). Do not decide from a window that small.

## M0 result (prod, 2026-09-21)

`docker exec job-hunter python tools/fail_signatures.py --db /app/db/tracker.db`:
**14 records over 42.3 days, 4 signatures, 0 uninformative.**

| Signature | Vacancies | 6 h peak | Now |
|---|---|---|---|
| `[apply_agent] FETCH ERROR: Page at <url> returned too little text` | 4 (3× `jobs.ashbyhq.com`) | 1 | 3 given up, 1 absent |
| `Traceback (most recent call last):` | 2 | 1 | 1 skipped, 1 applied |
| `(empty error text)` (rate_limited, exit 45) | 1 | 1 | skipped |
| `[solidjobs] HTTP fetch failed (500 …)` | 1 | 1 | retryable |

Rules: (1) no recurring non-incident signature; (2) nothing crosses 3 in 6 h;
(3) n/a; (4) window 42 days, measurable. **Verdict: ship M1 + M4, close M2/M3.**

**The incident is not in the log at all, and that corrects the Problem
section.** Prod runs with `LLM_API_KEY` set, so the paid API is the primary
path; `main_cli` only runs as the fallback after an API account outage
(`apply_agent.py:98-132`). When that fallback failed, `apply_agent` exited 46,
`llm_outage`: no FAIL row, no `fail_count` bump, the claim released back to
`PENDING`, and by design no line in `apply_failures.jsonl`. So for THIS
incident:

- **No vacancy was lost or given up.** Affected jobs were delayed until the
  API recovered, not burned. Problem items 2–3 describe what the pipeline
  does to a systemic failure that lands in `fail`. This one didn't.
- **The real damage was a dead safety net.** For 11 days an API outage meant
  no generation at all, and the broken fallback looked exactly like the
  outage itself. It surfaced only when the API went down again on 2026-09-21.
- **Blind spot:** "the fallback failed too" is folded into `llm_outage` and
  is invisible to the failure log, which is why no log-based design (M2/M3)
  could have caught it. That is the argument for M1 (probe the path directly)
  and for M4's new first item below.

Side finding, out of scope: `jobs.ashbyhq.com` detail pages return a
near-empty shell to a plain HTTP fetch (JavaScript-rendered), so every Ashby
posting fails at Step 1 and ends up given up. That is a source-level fetch
bug, 3 vacancies in 5 weeks. The fix is the Ashby public posting API, the
same move already made for Lever. Tracked separately.

## M1 — Post-deploy CLI canary (SHIPPED 2026-09-21)

As built: `hunter/cli_canary.py`, started from `_post_init` as a plain
asyncio task. It is gated by `CLI_CANARY_ENABLED` (default true) and by
`llm_client.cli_credentials_present()`. A transient failure (non-zero exit,
timeout, unexpected reply) is retried once after 60 s. A deterministic one
(argv rejected, binary missing) alerts at once. A pass is logged only.
Nothing is blocked, since M3 is closed. The design sketch below is kept for
the record.


**What:** Once at bot start (`telegram_bot._post_init`), when
`llm_client.cli_credentials_present()` is true, run one `claude -p` probe
through **the same argv builder** the apply pipeline uses, with a trivial
prompt ("Reply with exactly OK. Do not use any tools."). It passes if the
exit code is 0, stdout contains `OK`, and stderr has no
`matches no known tool` line. It fails with one Telegram alert quoting the
stderr head. Until M3 lands, a failure only alerts; after M3 it also opens
the global block.

**Files:** `hunter/apply_cli.py` (split `_cli_argv(prompt)` out of
`_build_cli_command`, which becomes `_cli_argv(f"/apply {input}")`, so the
canary exercises the exact flag order), new `hunter/cli_canary.py`,
`hunter/telegram_bot.py` (`_post_init` schedules it via `app.create_task`,
never blocking startup), wrapped in `best_effort("apply.cli_canary")`.

**Test:** `tests/test_cli_canary.py` with a fake subprocess covering pass,
exit ≠ 0, a deny-rule warning on stderr, missing binary and timeout. Plus a
guard that the canary argv comes from `_cli_argv` (a mutation test: put the
prompt last again, and the canary argv test must fail).

**Rollback:** `CLI_CANARY_ENABLED=false`.

**Why first:** it is the cheapest milestone and catches exactly this class
(a broken invocation) within a minute of the deploy that introduces it,
regardless of what M0 says.

## M2 — Classify every failure; record the signature (CLOSED by M0, 2026-09-21)

> Closed: no recurring systemic signature in 42 days of prod log (M0 rule 1).
> Kept for the record. The head-vs-tail inconsistency in the `error` field
> (Problem #5) is still real; fix it if the log is ever used for decisions.

**What:** new `hunter/failure_class.py::classify(outcome, exit_code, error,
url)` returns `"system" | "vacancy"`:

- **`system` by pattern:** a short, explicit list, e.g.
  `matches no known tool`, `unrecognized arguments`, `ModuleNotFoundError`,
  `ImportError`, `SyntaxError`, `No such file or directory: 'claude'`,
  `command not found`, `Failed to authenticate` (CLI login dead while the
  pipeline is on the CLI path). The list is calibrated from M0, not guessed.
- **`system` by recurrence:** the same signature on ≥ N distinct vacancies
  within 6 h (N from M0 rule 2, default 3). Counted in a small SQLite table
  (`failure_signatures`, lazy-ensure like `subsystem_health`) because the
  classifier must survive a bot restart mid-incident.
- **Everything else:** `vacancy`.

`log_apply_failure` gains `signature`, `failure_class`, and stores both the
head and the tail of the output, which fixes the head-vs-tail inconsistency
from Problem #5. `/fails` shows the class.

**Test:** table-driven classifier tests, including the real 2026-09-10 stderr
and the real 2026-07-12 Himalayas error as fixtures, and a recurrence test
across a simulated restart.

**Rollback:** `FAILURE_CLASSIFY_ENABLED=false` makes `classify()` always
return `vacancy`, which is today's behaviour byte-for-byte.

## M3 — `BLOCKED` queue and a real stop (CLOSED by M0, 2026-09-21)

> Closed together with M2, which it depends on.

Built on M2. Same shape as the `llm_outage` pause, which already works.

- **System-class failure** → no `FAIL` row, no `fail_count` bump. The job is
  parked as a `BLOCKED` placeholder (like `PENDING`: `pending_meta` holds the
  Job, `is_known()` still dedups it, and it joins
  `_COOLDOWN_SKIP_STATUSES`/`iter_unsent_rows`/`read_all_tracker_rows`
  exclusions exactly like `PENDING`, so it stays invisible downstream).
  `blocked_signature` records why.
- **The worker stops**, instead of napping 5 min. Global scope for a global
  signature. For a source-scoped one (M0 rule 3), only that source's jobs
  are parked and the rest of `PENDING` keeps draining.
- **One alert**, streak-suppressed like `llm_outage`:
  `🚨 Apply stopped: system error <signature> — N vacancies in BLOCKED since <ts>`.
- **Resume:** `/queue resume` (manual), or automatically when the M1 canary
  passes on a bot start. The first job after a resume is a probe: if it
  passes, every `BLOCKED` row goes back to `PENDING` in FIFO order.
- **`_retry_failed`** applies the same class: a system-class retry failure
  does not bump `fail_count`.
- **Manual paste / Apply button** don't park anything (there is no queue
  row). They reply "system error, not recorded as FAIL" with the signature.
- `/status` and `/queue` show three counts: PENDING / BLOCKED (+ signature) / FAIL.
- Alert if the oldest `BLOCKED` row is older than 24 h. A block nobody
  resolves must not become a silent graveyard.

**Test:** worker-loop tests with a fake subprocess: a system failure parks
the job and stops the loop; resume + a passing probe releases FIFO; a
source-scoped block leaves other sources draining; `fail_count` is
untouched throughout.

**Rollback:** the flag from M2 (no system class means nothing is ever
parked). Any `BLOCKED` rows left over are released by `/queue resume`.

## M4 — Success rate in the daily summary + rate alert

- **New, from the M0 blind spot:** count "API outage AND the CLI fallback
  failed too" separately from a plain outage. Today `apply_agent.py` turns
  both into exit 46, so a broken fallback reads as one more API outage. A
  distinct marker (log line + a counter the daily summary shows) is enough;
  it must not change the `llm_outage` semantics (no FAIL row, no
  `fail_count` bump).

- `scheduled_daily_summary` gets one line:
  `applies 24h: 12 ok / 3 fail (API 10/1, CLI 2/2) · top: <signature> ×2`.
  The "ok" count comes from tracker rows written in the window, the fail
  count and signatures from `apply_failures.jsonl`.
- A rate alert (via `best_effort`'s existing threshold/cooldown machinery):
  ≥ 50% failures over ≥ 5 attempts in 6 h. It is the net under M2's pattern
  list. A new systemic failure nobody has seen before still surfaces within
  hours, not days.

**Rollback:** it's a report line and an alert; remove or silence it.

## Risks

- **Misclassifying a vacancy failure as system** (the worst case): it stops
  the worker for everyone. Caught by M0 rule 2 (zero false BLOCKs on
  history), source-scoping, the 24 h stale-BLOCKED alert, and `/queue resume`.
  Rollback is a single flag.
- **Misclassifying system as vacancy:** today's behaviour, no regression,
  and M4's rate alert is the net.
- **Canary flake** (CLI 529 overloaded, transient network): one retry after
  60 s before it alerts. Before M3 a flake costs one message. After M3 it
  would block, so the canary only opens a block on a signature-class
  failure (deny-rule, missing binary, auth), not on a timeout or a 529.
- **Canary cost:** one subscription call per bot start, flat. No API spend.

## Cost

$0 in API spend. No LLM call is added anywhere. The canary uses the flat
CLI subscription once per restart. Every milestone changes a real decision:
whether the worker keeps burning jobs, whether a vacancy keeps its retry
budget, whether the owner hears about a broken deploy in a minute or in
eleven days.

## Open questions

1. Does the prod image expose a build SHA (env var or label) the bot can
   read? If yes, run the canary only when the SHA changes rather than on
   every restart. If no, every restart (cheap either way).
2. Per-source blocking: worth the extra state, or is a global stop enough?
   (M0 rule 3 answers this with data. The question is whether you want it
   regardless.)
3. After M0: should `/retry_reset` gain a `sig <signature-id>` form to
   revive exactly the rows one signature killed, instead of `all`?
