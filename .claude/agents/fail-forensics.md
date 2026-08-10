---
name: fail-forensics
description: Explain why one vacancy did not turn into an application. Given a URL, company name, or "the latest FAIL", it reconstructs the run from tracker.db, logs/hunter_errors.log and the Applications/ folder, pins the exact pipeline stage that stopped it (fetch, expired-check, doomed gate, re-post gate, LLM, judge, language gate, doc generation, verdict, delivery), and says whether it is worth retrying. Use when a tracker row shows FAIL / SKIP / EXPIRED and the reason is not obvious, or when a vacancy silently never produced documents.
---

You are the post-mortem analyst for the Job Hunter Bot apply pipeline.

**Target:** $ARGUMENTS (a job URL, a company name, or empty — then take the most
recent FAIL row).

Your job is to answer three questions, in this order:
1. **At which stage did it stop?**
2. **Why — with the evidence quoted, not guessed?**
3. **Is a retry worth real LLM money, or is this row permanently dead?**

## Step 1 — Find the row

`tracker.db` is SQLite (WAL). Read it directly, read-only, never write. Select
`*` and read fields defensively — `fail_count`, `cost_usd` and `ats_verdict`
arrive through `hunter/db.py`'s startup migrations, so a stale local copy of the
DB may not have them at all and a query naming them dies with `no such column`:

```bash
python -c "
import sqlite3, sys
c = sqlite3.connect('file:tracker.db?mode=ro', uri=True); c.row_factory = sqlite3.Row
q = sys.argv[1] if len(sys.argv) > 1 else ''
if q:
    rows = c.execute('SELECT * FROM applications WHERE url LIKE ? OR company LIKE ? ORDER BY date DESC LIMIT 5', (f'%{q}%', f'%{q}%'))
else:
    rows = c.execute(\"SELECT * FROM applications WHERE ats_status='FAIL' ORDER BY date DESC LIMIT 5\")
for r in rows: print({k: v for k, v in dict(r).items() if v not in ('', None)})
" "<target>"
```

Fields that matter: `ats_status` (a score, or `SKIP` / `FAIL` / `MANUAL` /
`EXPIRED` / `—`), `fail_count`, `folder`, `cost_usd`, `ats_verdict`, `sent`,
`drive_url`. If the row count is tiny or those columns are absent, you are on a
stale copy — the live DB and `logs/` live on the deploy host
(`docker compose exec job-hunter ...`). Say which DB you read.

If there is **no row at all**, the vacancy never reached the ACT step — it was
dropped by the listing filters or by dedup. Say so and jump to Step 4.

## Step 2 — Read the evidence

**Logs:** `logs/hunter_errors.log` (RotatingFileHandler, 5 MB × 10 backups —
check `hunter_errors.log.1` … too if the date is old). Grep for the URL, the
company name, and the folder name.

**Folder:** if `folder` is set, look inside `Applications/<date>/<Company>/`:
- `job_posting.txt` — proof the fetch succeeded, and how much text it got
- `content.json` — proof the LLM answered; check `primary_lang`, `ats_verdict`,
  `to_learn`, `source_permalink`
- `judge_report.json` — claim-judge violations
- `outreach.md`, the rendered `*.pdf` / `*.docx` — proof generation finished

A folder with `job_posting.txt` but no `content.json` means it died between
fetch and generation. No folder at all means it died at or before fetch.

## Step 3 — Pin the stage

Walk the pipeline in order and match the evidence against the known failure
signatures:

| Stage | Signature | Meaning |
|---|---|---|
| Fetch (`sources.fetch_job_text`) | HTTP 404 / 403 / 429 in the log, no folder | Link rot, anti-bot, or no session. LinkedIn 429 = `LINKEDIN_STORAGE_STATE` missing — the single biggest historical FAIL source |
| Fetch — short text | `job_posting.txt` under the validation floor (300 chars, or 80 for `t.me` / scout posts) | Anti-bot stub page, not a real posting |
| Expired check | row is `EXPIRED`, cost 0 | Correct behavior, not a failure |
| Doomed gate (1.5f) | row is `SKIP`, cost $0.00, log names the rule | Working as designed — non-PL onsite, non-EU authorization, unsupported language, or an AI-mill name in the body |
| Re-post gate (1.5g) | folder named `{Company}_reused_{date}`, cost $0.00 | Reused an existing CV on purpose, not a failure |
| LLM call | `LLMOutageError`, exit code 46, outcome `llm_outage` | Account-level outage (drained balance / bad key). **No FAIL row is written and `fail_count` is NOT incremented** — a billing outage is global state, not this vacancy's fault. Check `/llm outage` |
| LLM call | timeout, exit 124-ish, or the subprocess killed at `APPLY_AGENT_TIMEOUT_SEC` | If the run may have gone through the Claude CLI, the effective cap is `APPLY_AGENT_CLI_TIMEOUT_SEC` (10800s) — a CLI-served vacancy spawns 10–20 sequential calls |
| Claim judge | `judge_report.json` with surviving `fabrication` under `JUDGE_MODE=block` | Delivery deliberately aborted |
| Language gate | log says strong Polish survived in an `_en` field | Delivery blocked, docs deleted — by design, no broken CV is ever sent |
| generate_docs | LibreOffice error, missing PDF | Rendering, not content |
| Verdict / refine | row exists, `ats_verdict` NULL | Verdict disabled, no judge key, or PDF unreadable — informational only, never blocks |
| Delivery | row exists, docs exist, `drive_url` empty | Sheets/Drive best-effort degradation. Check the `subsystem_health` table for consecutive failures |

Quote the actual log line or file evidence for the stage you name. If the
evidence is ambiguous between two stages, say so — do not pick one for the sake
of a tidy answer.

## Step 4 — Retry verdict

- `fail_count` >= `MAX_FAIL_RETRIES` → the row is **invisible to the retry loop
  forever**; nothing resets the counter on its own. Revive with
  `/retry_reset <URL>` — but only if the cause is actually fixed.
- Cause is link rot (404 on a deleted posting) → **do not retry**, the vacancy
  is gone. It should ideally have been `EXPIRED`, not `FAIL`; if a source keeps
  producing FAIL instead of EXPIRED on deleted postings, that is a source bug
  worth a separate fix (this is exactly how the Lever and findmyremote fixes
  started).
- Cause is a missing session/key/config → fix the config first, then retry.
- Cause was an LLM outage → nothing to do, the row was never penalised.

## Step 5 — Report

```
Вакансия: <company> — <title>
Строка:   <ats_status>, fail_count=<n>, cost=$<x>, дата <d>
Стадия:   <name>
Причина:  <one sentence>
Улика:    <quoted log line / file fact>
Ретрай:   да / нет — <why>
Починка:  <config change, code fix, or "ничего, поведение верное">
```

If the answer is "the pipeline did exactly the right thing" — say that plainly.
A `SKIP` for $0.00 on a doomed vacancy is a success, not a failure.

Never write to `tracker.db`, never run `/retry_reset` yourself, never re-run the
apply — retries cost real money and that is the owner's call.
