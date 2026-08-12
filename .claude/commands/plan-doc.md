---
description: Write docs/<NAME>_PLAN.md before any code — check AGENT_LOG for an already-rejected version of the idea, then open with M0, a free read-only measurement whose decision rule is stated up front.
argument-hint: <feature, fix or idea to plan>
---

Write a plan document for a feature or fix, before any code is written.

## Input
$ARGUMENTS — the feature, fix or idea to plan.

---

## Why this command exists

Every non-trivial change in this repo starts as `docs/<NAME>_PLAN.md` — there are 25+ of them, and they are the reason the work log can say *why* something was built one way and not another. The plan is committed **first**, in its own commit, so the reasoning survives even if the implementation changes shape later.

The plan is not a task list. It is an argument: here is what we believe, here is how we will find out if it is true, here is what we will build if it is.

---

## Step 1 - Read the ground truth first

Do not plan from the prompt alone. Read, in this order:

1. `CLAUDE.md` — architecture, pipeline order, config table, the rules section
2. `docs/AGENT_LOG.md` — search it for this subsystem. **Rejected alternatives live here.** If this idea was already tried and dropped, the plan is a one-liner saying so, with the date and the reason.
3. The actual code paths the change would touch
4. Any existing `docs/*_PLAN.md` on an adjacent subsystem

If the idea contradicts something already measured and rejected, say that up front and stop. That is a successful outcome of this command.

---

## Step 2 - M0 is always "measure"

The first milestone is never code. It is: **what data would tell us this is worth building?**

This is the house rule, and it has paid off repeatedly:
- the LLM-outage plan's M0 log audit found zero billing errors ever, and reordered the whole plan
- the CV-reuse calibration killed the general warm-start idea and exposed a narrow ~14% slice that was worth shipping instead

M0 must be **free or nearly free**: a read-only script over `tracker.db`, a grep over `logs/`, an offline replay over `Applications/`, a live probe of one endpoint. No LLM calls, no writes, no network side effects where avoidable. It must have a stated decision rule *before* it runs: "if the hit rate is under X, we do not build this."

If you cannot state what measurement would change the decision, the plan is not ready — say so.

---

## Step 3 - Structure

```markdown
# <NAME> Plan

**Status:** draft | in progress | shipped | rejected
**Date:** YYYY-MM-DD
**Motivation:** <the incident, owner report, or observation — one paragraph>

## Problem

<what actually goes wrong today, with evidence: a log line, a tracker row,
a number. Not "it would be nice if".>

## Non-goals

<what this deliberately does not do — the scope fence>

## M0 — Measure

<the read-only check, the exact command, and the decision rule:
"if <metric> < <threshold>, close this plan as not worth building">

## M1..Mn — Milestones

<one milestone per commit. Each states: what changes, which files, what test
proves it, and what the rollback is if it misbehaves in prod.>

## Risks

<what could break, and which existing safety rail catches it — the doomed gate,
the judge, the language gate, the keep-best guard, best_effort() alerting>

## Cost

<if it adds LLM calls: which model tier, per-vacancy delta in USD, and what real
decision each call changes. A call that only produces a number nobody acts on
does not ship — that is a standing rule here.>

## Open questions

<things needing an owner decision, phrased so a yes/no answers them>
```

---

## Step 4 - Constraints to respect while planning

- **No speculative LLM layers.** At this volume every LLM step must change a real decision. Prefer deterministic (regex, TF-IDF, comparison) — the doomed gate and the re-post gate both run at $0.00 and catch real cases.
- **Cheap model by default.** Judge, translation, outreach and verdict all run on `JUDGE_MODEL` (Haiku tier). Only generation earns the main profile.
- **Best-effort by default for anything peripheral** — Sheets, Drive, Telegram, writers, shadows never break an apply. New ones wrap in `best_effort("subsystem.name")`.
- **Nothing blocks delivery** except the language gate and, in `block` mode, a surviving fabrication.
- Prefer extending an existing stage over adding a pipeline step.

---

## Step 5 - Write and stop

Write `docs/<NAME>_PLAN.md`. Do **not** start implementing — the plan lands as its own commit first, so it can be argued with before code exists.

Finish with: the file path, the M0 command the human can run right now, and the decision rule that M0 will answer.
