# ROADMAP — every open plan in one place

**Updated:** 2026-09-09. This is the index, not the argument: each row links to the
plan document that holds the evidence, the M0 measurement and the rejected
alternatives. Update the row when a milestone ships, and move a plan to the
"Shipped" table when its last milestone lands — a stale Status line in a plan file
is exactly what this page exists to prevent (three were found stale on 2026-09-09).

The two older overviews are kept as history and are NOT maintained anymore:
`docs/QUALITY_ROADMAP.md` (2026-07-15, quality workstreams 01–09 — mostly shipped)
and `docs/review-2026-07/02-next-steps.md` (2026-07 project review). Everything
still open from both is folded into the tables below.

Standing anti-goals (unchanged since the June/July reviews, re-confirmed 2026-08-29):
no new sources for volume until `/funnel` says which ones live; no `filters.py`
rewrite; no speculative LLM layers (every call must change a real decision); no
pipeline DSL; do not touch `tracker.py`/`filters.py` under a generation change.

---

## 0. Improvement series 2026-09 — the analysis layer above this index

[improvement-2026-09/README.md](improvement-2026-09/README.md) is an eight-perspective
review (product, marketing, architecture, engineering + security / ops / compliance /
data audits) with one 26-step execution order and a dependency map. It does not
replace the rows below — it says in which order to take them and adds what no plan
here covered. Its own "week 0" items, unblocked and independent of every owner decision:

| # | Item | Plan | Next step | Size |
|---|------|------|-----------|------|
| 0.1 | **Isolate the CLI agent** (`claude -p --dangerously-skip-permissions` with scraped job text in the prompt, root, secrets mounted) | [05-SECURITY_PLAN M1](improvement-2026-09/05-SECURITY_PLAN.md) | drop the flag, `--disallowedTools WebFetch,WebSearch` + tool allowlist, job text via file, non-root `USER`; golden CLI E2E with an injection fixture | one day |
| 0.2 | **Back up the real data** (`tracker.db`, `app.sqlite`, `users/` — today only the `/export` xlsx is snapshotted) | [06-OPS_PLAN M1](improvement-2026-09/06-OPS_PLAN.md) | `Connection.backup()` + restic off-host, restore drill | one day |
| 0.3 | **Run the two July measurements** | [08-DATA_EVAL_PLAN M0](improvement-2026-09/08-DATA_EVAL_PLAN.md) | same questions as rows 4.1 / 4.3, now with pre-stated decision rules and sample-size caveats; ≈ $1 | half a day on the VPS |
| 0.4 | **Concierge test on 5 external users** | [01-PRODUCT_PLAN M0.3](improvement-2026-09/01-PRODUCT_PLAN.md) | stop rule: < 3 of 5 send a generated CV → freeze pivot Stages 1–8, work on generation quality instead | two weeks, no code |
| 0.5 | **Domain model documented (M1)** — target entities, `applications`/`profile_jobs`/API-table mapping, proposed Postgres DDL v1, module boundaries | [03-ARCHITECTURE_PLAN M1](improvement-2026-09/03-ARCHITECTURE_PLAN.md) → [DOMAIN_MODEL.md](DOMAIN_MODEL.md) | Shipped, docs-only. Next: owner answers to open questions 1–4, then M2 (`Settings` facade) | done |

## 1. Actionable now — small, unblocked

| # | Item | Plan | Next step | Size |
|---|------|------|-----------|------|
| 1.1 | **Flip `PRESCREEN_MODE` warn → skip** | [STACK_PRESCREEN_PLAN M5](STACK_PRESCREEN_PLAN.md) | Owner decision 2026-08-24 was "one week of warn, then skip"; it has been two. One line in the prod `.env`, rollback is the same line. Glance at the Telegram warnings of the last two weeks first — any warn on a vacancy the owner actually sent = don't flip, open a regression test instead | minutes |
| 1.2 | **Oracle Always Free — M0 probe** | [ORACLE_FREE_TIER_PLAN](ORACLE_FREE_TIER_PLAN.md) | M0a: yield of pracuj/theprotocol/linkedin from an Oracle IP vs the prod `/health` 20-run median, three runs ~20 min apart (rule: ≥ 70 % on all three passes; one run below → one re-run; two below → fail, and any fail closes the plan). M0b: `docker buildx --platform linux/arm64` test build. M0c: the Hetzner invoice + peak RAM during a CLI apply | one evening |
| 1.3 | **Post-generation block unification (`hunter/apply_post.py`)** | [STACK_PRESCREEN_PLAN M8](STACK_PRESCREEN_PLAN.md) | Not started; file does not exist. Strangler, one stage per PR, from the tail: outreach → verdict+refine → render → gates → judge → scrubs. Golden E2E for both branches is the safety net (assertions never edited) | ~1 PR/stage |
| 1.4 | **Plan-doc hygiene** | this file | `DEPLOY.md`'s Status checklist is all unchecked while prod has been live since June. Fix the lines, don't rewrite the doc (the other three stale Status lines and `PUBLIC_RELEASE_CHECKLIST` item 1 were corrected in the PR that created this file) | minutes |
| 1.5 | **Market memory — remember every listing the hunt sees** | [MARKET_MEMORY_PLAN](MARKET_MEMORY_PLAN.md) | M0 tool + M1–M3 shipped 2026-09-13 (branch claude/nifty-sagan-g1iagx, PR pending). Next: run M0 on prod (`python tools/market_memory_m0.py --dump …` + the M0.b SQL), then M4 `/market` digest after ~4 weeks of rows | M0 on prod: one evening; M4 one PR once the data exists |
| 1.6 | **Filter feedback loop — the owner's `Filter miss` labels become filter rules** | [FILTER_FEEDBACK_PLAN](FILTER_FEEDBACK_PLAN.md) | Plan only, nothing built. The labels (`app_status`/`owner_reason`, job-hunter-api#34) have been collecting since 2026-09-14; owner decision 2026-09-19 is to wait ~2 more weeks, then run M0 (`tools/filter_feedback_m0.py --weeks 4`) on prod. R1: under 15 `Filter miss` rows → build nothing yet. Consumes the `postings_seen` data from row 1.5 | M0: one evening; M1 (weekly report) one PR; M2–M4 one PR each |
| 1.7 | **Apply failure queues — tell "our bug" from "bad vacancy"** | [APPLY_FAILURE_QUEUES_PLAN](APPLY_FAILURE_QUEUES_PLAN.md) | M0 run on prod 2026-09-21: 14 failures / 42 days, no recurring systemic signature → M2/M3 (classifier + `BLOCKED` queue) closed. The 2026-09-10 CLI-argv incident left no trace in the failure log at all: a failed CLI fallback exits 46 = `llm_outage`. M1 (post-start CLI canary, `hunter/cli_canary.py`) shipped. Next: M4, daily success-rate line + a separate counter for "API down AND the CLI fallback failed too". Side finding: Ashby detail pages need the public posting API (3 given-up vacancies) | M4 one PR; Ashby fetch one small PR |
| 1.8 | **Pipeline page — where every vacancy is right now** | [PIPELINE_VIZ_PLAN](PIPELINE_VIZ_PLAN.md) | Plan + M0 tool shipped 2026-09-22 (`tools/pipeline_snapshot.py`), not yet run on prod. Next: `python tools/pipeline_snapshot.py --db tracker.db --days 7` on the deploy host. Its five coverage rules decide which M1 instrumentation is needed (`start`/refine events, `hunt_runs`, `queued_at`, orphan-run stamping) before the API contract (M2, job-hunter-api) and the `/pipeline` page (M3, job-hunter-site) | M0 on prod: minutes; M1 one PR; M2 + M3 one PR per repo |

## 2. Blocked on ONE owner decision

| # | Item | Plan | The decision | Unblocks |
|---|------|------|--------------|----------|
| 2.1 | **`_ats_check_loop` on the CLI branch** (5th hole of wave 0.5) | [GENERATION_ARCHITECTURE_ANALYSIS §3, §5.7, §6](GENERATION_ARCHITECTURE_ANALYSIS.md) | Enable the deterministic ATS-keyword loop on CLI too, or accept that CLI relies on verdict-refine alone? Needs the §5.7 cost number: how many LLM rounds / $ go to "loop injects keyword → judge flags it → scrub cuts it" (`tools/judge_stats.py` + `cost_usd`), measured on post-#231 data only | 2.2 |
| 2.2 | **Wave 4 — full pipeline unification** | [quality/05](quality/05-unify-apply-pipeline.md) | Nothing else: the precondition (golden E2E on BOTH branches) has been met since 2026-08-24. Plan 05's inventory is stale (sizes grew; use §2/§3 of the analysis instead) | closes the "mirror of Step X" class for good |
| 2.3 | **`JUDGE_MODE` warn → block** | [CV_JUDGE_PLAN M4](CV_JUDGE_PLAN.md) | Precision review of `fabrication` findings over post-#231 `judge_report.json` files; if false positives ≈ 0, flip. Owner 2026-08-28: "судью не режем", exaggeration findings need no action | — |

## 3. Product line (SaaS pivot) — the big one

Parent: [SAAS_PIVOT_PLAN](SAAS_PIVOT_PLAN_supersedes_PYTHON_CORE_PLAN.md). Order is
dependency order, not preference.

| # | Item | Plan | Status | Next step |
|---|------|------|--------|-----------|
| 3.1 | **Owner migration onto the rendered profile (M5)** | [RESUME_PROFILE_STORE_PLAN](RESUME_PROFILE_STORE_PLAN.md) | M1–M4 + steps 2d/4b shipped (#238–#243); **M5 is the only bot-repo milestone left** | Manual, on the VPS: build the owner's profile.json, render, diff against live files, swap (originals kept as dated backups), one real apply with an unchanged `ats_verdict` ballpark. Rollback = one `cp` |
| 3.2 | **`/profile` page: 4 tabs + variant chips** | [PROFILE_PAGE_TABS_WORKORDER](PROFILE_PAGE_TABS_WORKORDER.md) | Bot piece (`preview` job kind) shipped #246; **site/api not started** | Implement in job-hunter-site / job-hunter-api against the contract in the work order; owner-visibility flag gates tab 4 + the chip row |
| 3.3 | **Multi-user B3.5 — per-user search specs + hunt fan-out** | [MULTI_USER_UPDATE](MULTI_USER_UPDATE.md) | B1–B3 shipped; B3.5 not started | `SearchSpec` per user → union fetch plan → per-source query budget → fan-out with each user's filters/dedup → lift the `hunting_enabled` force-false |
| 3.4 | **Multi-user B4 — quotas & fairness** | [MULTI_USER_UPDATE](MULTI_USER_UPDATE.md) | after 3.3 | Per-user daily apply quota, FIFO fairness in the apply queue, `SUM(cost_usd) GROUP BY user_id` |
| 3.5 | **Per-user filters wiring (M4) + web settings page (M5)** | [FILTERS_YAML_PLAN](FILTERS_YAML_PLAN.md) | M1–M3 shipped; M4 blocked on 3.3; M5 is site/api | M4 lands with B3.5 (`filters_yaml` on `UserPaths`, `FILTERS_YAML_PATH` via `user_env()`); M5 = `GET/PUT /api/filters` per the contract in the plan |
| 3.6 | **Per-user email (forwarding, not OAuth)** | [PER_USER_EMAIL_PLAN](PER_USER_EMAIL_PLAN.md) | draft, M0 not run | M0: `tools/mail_filter_coverage.py --days 90` on the owner's inbox; rule: reply recall ≥ 85 % build / 60–85 % build with caveats / < 60 % close |
| 3.7 | **SaaS stages after the profile store** | [SAAS_PIVOT_PLAN](SAAS_PIVOT_PLAN_supersedes_PYTHON_CORE_PLAN.md) | not started | payments/credits, the $0 pre-payment match score as the conversion hook, the market-demand optimization tier |

## 4. Waiting for data (deferred by the owner 2026-08-29: "истории ещё нет, пособираем")

Post-#231 data only — the 361 older judge reports describe a generator that no
longer exists.

| # | Question | Tool | Decides |
|---|----------|------|---------|
| 4.1 | Does a higher `ats_verdict` correlate with more replies? | `tools/verdict_funnel_corr.py` ([quality/01](quality/01-verdict-data-decision.md)) | `ATS_VERDICT_TARGET` (95 → 85?) and how many refine rounds are worth paying for; also an input to 2.1 |
| 4.2 | Which model is primary — Sonnet, DeepSeek, or hybrid? | dual-apply pairs on Drive + verdict column N ([review-2026-07 path A](review-2026-07/path-A-feedback-loop.md)) | keep paying for `dual` or switch it off |
| 4.3 | Which of the 25 sources feed the funnel? | `/funnel 90` | which scrapers to stop maintaining |
| 4.4 | Judge precision on `fabrication` | `tools/judge_stats.py` | 2.3 |

## 5. Public release & infrastructure

| # | Item | Plan | Status / next step |
|---|------|------|--------------------|
| 5.1 | **Git history scrub + gitleaks** | [PUBLIC_RELEASE_CHECKLIST §3](PUBLIC_RELEASE_CHECKLIST.md), [quality/07](quality/07-public-repo-prep.md) | Working tree is clean since #235 (`tests/test_handoff_readiness.py` gates it). Remaining: gitleaks over the FULL history, the manual destructive rewrite if it finds anything, and gitleaks as a CI job (today `.github/workflows/` holds only `deploy.yml`) |
| 5.2 | **mypy → 0 and blocking; SonarCloud token** | [quality/06](quality/06-static-gates-mypy-sonar.md) | Baseline ~218 errors, `continue-on-error: true`. Rule in force: never grow it. Sonar job skips itself until `SONAR_TOKEN` exists |
| 5.3 | **N > 1 apply workers** | [HUNT_APPLY_SPLIT_PLAN M2](HUNT_APPLY_SPLIT_PLAN.md) | Deliberately deferred; `apply_worker_loop(context, worker_id=0)` already takes the id, so it is a config change when wanted. Only worth it once volume or a second user needs it |
| 5.4 | **Hosting move to Oracle Always Free** | [ORACLE_FREE_TIER_PLAN](ORACLE_FREE_TIER_PLAN.md) | see 1.2 — M1–M4 only after M0a passes |

## 6. Backlog ideas (GitHub issues)

| Issue | Idea | Gate before starting |
|-------|------|----------------------|
| [#141](https://github.com/igrdevelop/job-hunter/issues/141) | ATS application-form auto-fill (Playwright on the desktop, human clicks submit, never auto-submits) | Measure how many minutes/week form-filling really costs — the issue itself says so. Highest-effort item in the backlog |
| [#138](https://github.com/igrdevelop/job-hunter/issues/138) | Contact discovery after apply | Phase 1 steps 1, 3, 4 shipped (`contact_extract.py`, `outreach.py`, `outreach.md`). Only step 2 is open: `hunter/contact_lookup.py`, a web-search fallback when the posting names no recruiter. Narrow the issue to that or close it |
| — | New job sources | [new-sources/](new-sources/OVERVIEW.md) queues 1–3 exist, but the anti-goal stands: not before 4.3 shows which of the current 25 live |

---

## Shipped — plans whose last milestone has landed

Kept here so nobody re-plans them; each file still holds the measurements and the
rejected alternatives and is worth reading before touching its subsystem.

| Plan | Landed | Where it lives now |
|------|--------|--------------------|
| [ATS_VERDICT_PHASE2_PLAN](ATS_VERDICT_PHASE2_PLAN.md) | 2026-07 | verdict column N, shadow verdict |
| [CANDIDATE_YAML_PLAN](CANDIDATE_YAML_PLAN.md) | 2026-07/08 | `hunter/candidate.py`, neutral defaults rule |
| [CV_JUDGE_PLAN](CV_JUDGE_PLAN.md) M1–M3 | 2026-06 | `hunter/claim_judge.py` (M4 = row 2.3) |
| [DEEPSEEK_PROVIDER_PLAN](DEEPSEEK_PROVIDER_PLAN.md) | 2026-06 | `hunter/llm_profiles.py`, `/llm`, `/dual` |
| [DOOMED_GATE_PLAN](DOOMED_GATE_PLAN.md) + [PASTE](DOOMED_GATE_PASTE_PLAN.md) + calibrations | 2026-07/08 | `filters.assess_job_text`, Step 1.5f |
| [FILTERS_YAML_PLAN](FILTERS_YAML_PLAN.md) M1–M3 | 2026-08-08 | `hunter/filter_profile.py` (M4/M5 = row 3.5) |
| [GDRIVE_SSL_RACE_PLAN](GDRIVE_SSL_RACE_PLAN.md) M1–M3 | 2026-08 | `gdrive_sync._drive_call`, `drive_ledger.py` |
| [GENERATION_ARCHITECTURE_ANALYSIS](GENERATION_ARCHITECTURE_ANALYSIS.md) waves 0, 0.5 (4/5), 1, 2, 3 | 2026-08-27..29 | `hunter/pipeline/`, `gen_prompt.py`, `gen_profile.py` (wave 4 = row 2.2) |
| [HUNT_APPLY_SPLIT_PLAN](HUNT_APPLY_SPLIT_PLAN.md) M1, M3, M4 | 2026-08 | `apply_worker.py`, PENDING queue, `/queue`, `/fails` (M2 = row 5.3) |
| [HUNT_QUEUE_AND_DELIVERY_PLAN](HUNT_QUEUE_AND_DELIVERY_PLAN.md) | 2026-07 | FIFO `_hunt_lock`, `delivery.py` |
| [LINKEDIN_EXPIRED_DETECTION_PLAN](LINKEDIN_EXPIRED_DETECTION_PLAN.md) | 2026-08-22 | `linkedin.guest_html_expired()` |
| [LLM_COST_REDUCTION_PLAN](LLM_COST_REDUCTION_PLAN.md) M1–M6 | 2026-07 | `GEN_SKIP_PL_FOR_EN`, `TRANSLATE_*`, `tools/verdict_noise.py`, `tools/judge_stats.py` |
| [LLM_OUTAGE_RESILIENCE_PLAN](LLM_OUTAGE_RESILIENCE_PLAN.md) M1–M4b | 2026-07 | `LLMOutageError`, exit 46, CLI fallback, `/retry_reset` |
| [RELIABILITY_FIXES_PLAN](RELIABILITY_FIXES_PLAN.md) | 2026-06 | fail-count escalation, shadow safety |
| [RESUME_PROFILE_STORE_PLAN](RESUME_PROFILE_STORE_PLAN.md) M1–M4, 2d, 4b | 2026-08-30/31 | `profile_schema/render/parse/jobs.py` (M5 = row 3.1) |
| [SCOUT_REPO_SPLIT_PLAN](SCOUT_REPO_SPLIT_PLAN.md) | 2026-08-11 | private `igrdevelop/linkedin-scout`; relay stays here |
| [STACK_PRESCREEN_PLAN](STACK_PRESCREEN_PLAN.md) M0–M4, M6, M7 | 2026-08-24/25 | `hunter/prescreen.py`, `abort_after_generation` (M5 = row 1.1, M8 = row 1.3) |
| [TELEGRAM_CHANNELS_SOURCE_PLAN](TELEGRAM_CHANNELS_SOURCE_PLAN.md) | 2026-07-12 | `sources/telegram_channels.py` |
| [TRANSLATE_RUSSIAN_MESSAGES](TRANSLATE_RUSSIAN_MESSAGES.md) | 2026-08 | zero Cyrillic left in `hunter/commands`, `hunter/bot`, `main.py` |
| [VERDICT_REFINE_PLAN](VERDICT_REFINE_PLAN.md) | 2026-07-04 | `hunter/verdict_refine.py` |
| [quality/02](quality/02-dependency-lockfile-deploy-pinning.md), [03](quality/03-best-effort-degradation-alerts.md), [04](quality/04-coverage-and-golden-e2e.md), [08](quality/08-multi-user-configurability.md), [09](quality/09-multi-track-react.md) | 2026-07/08 | `requirements.lock`, `best_effort.py`, golden E2E ×2, `candidate.py`, `CANDIDATE_TRACKS` |
| [PYTHON_CORE_PLAN](PYTHON_CORE_PLAN.md), [WEB_APP_PLAN](WEB_APP_PLAN.md) | superseded | by SAAS_PIVOT_PLAN / MULTI_USER_UPDATE |
