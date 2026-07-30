---
name: cost-audit
description: Show where the LLM money actually goes. Reads cost_usd from tracker.db and the per-application artifacts, reports total and average spend per vacancy, the most expensive runs, spend by ATS verdict band, and whether the verdict-refine loop is earning its cost. Use when deciding whether a model, a pipeline stage, or a refine round is worth keeping, or before changing ATS_VERDICT_MAX_REFINES / a model profile.
---

You are auditing the LLM spend of the Job Hunter Bot.

**Scope:** $ARGUMENTS — a number of days (default: all rows), or a company /
profile name to narrow to.

Everything here is **read-only**: open `tracker.db` with `mode=ro`, never write,
never re-run an apply, never call an LLM. The whole audit costs $0.00.

## What the numbers mean before you read them

- `cost_usd` is the per-vacancy total USD, written at row creation with the
  pre-verdict figure and then **re-stamped** after the verdict + refine loop
  (`tracker.set_cost`). A row that was never re-stamped understates the run.
- `NULL` means *not measured*, not free: either a pre-cost-tracking row, or a
  CLI-mode run (Pro subscription — no per-token visibility), or a run served by
  the CLI outage fallback. Count NULLs separately; never average them as zero.
- `$0.00` with a `_reused_` folder is a **re-post gate** reuse — a real
  application at genuinely zero LLM cost. That is a win, not missing data.
- `ats_verdict` is the independent judge score on the rendered PDF, not the
  generator's self-score.

## Step 0 — Check the DB is actually the one with the data

`cost_usd` and `ats_verdict` arrive through `hunter/db.py`'s migration list, which
runs at **bot startup**. A tracker.db that has never had a current bot open it —
a stale local copy, a fresh test DB — simply has no such columns, and a query
naming them dies with `no such column`. Always probe first, and always name the
DB you are reading:

```bash
python -c "
import sqlite3, sys
p = sys.argv[1] if len(sys.argv) > 1 else 'tracker.db'
c = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
cols = {r[1] for r in c.execute('PRAGMA table_info(applications)')}
n = c.execute('SELECT COUNT(*) FROM applications').fetchone()[0]
print(p, '| rows:', n)
print('missing:', {'cost_usd','ats_verdict','fail_count'} - cols or 'none')
" tracker.db
```

If columns are missing or the row count is tiny, you are on the wrong copy. The
live data lives on the deploy host — run there:
`docker compose exec job-hunter python - <<'PY' ... PY`. Say which DB you read in
the report; never present numbers from a stale copy as current spend.

## Step 1 — Headline numbers

Select `*` and read defensively, so a missing column degrades to "unmeasured"
instead of crashing:

```bash
python -c "
import sqlite3
c = sqlite3.connect('file:tracker.db?mode=ro', uri=True); c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute('SELECT * FROM applications')]
priced = [r for r in rows if r.get('cost_usd') is not None]
paid   = [r for r in priced if r['cost_usd'] > 0]
print('rows total       :', len(rows))
print('with cost        :', len(priced), ' NULL/unmeasured:', len(rows) - len(priced))
print('zero-cost (reuse):', len(priced) - len(paid))
if paid:
    v = sorted(r['cost_usd'] for r in paid)
    print('total USD        : %.2f' % sum(v))
    print('avg / vacancy    : %.3f' % (sum(v)/len(v)))
    print('median           : %.3f' % v[len(v)//2])
    print('max              : %.3f' % v[-1])
else:
    print('no priced rows — wrong DB copy, or CLI-mode only')
"
```

## Step 2 — The expensive tail

List the 10 priciest runs with company, verdict and folder. The interesting
question is not "what was expensive" but **"did the expensive ones score
better?"** — a $6 run that ended at verdict 88 and a $1 run that ended at 91
is the argument for cutting refine rounds.

## Step 3 — Spend vs. outcome

Bucket rows by `ats_verdict` band (`<80`, `80-84`, `85-89`, `90-94`, `95+`) and
report average cost and row count per band. Then, for the same bands, reuse the
funnel classification (`hunter.funnel` — sent / confirmed / answered) to answer
the only question that matters: **does a more expensive, higher-scoring CV
actually get more replies?**

`tools/verdict_funnel_corr.py` already does the verdict↔funnel half of this
read-only — run it rather than reimplementing, and layer cost on top.

## Step 4 — Where inside a run the money goes

For the priciest few, look inside `Applications/<date>/<Company>/`:
- `content.json` — `ats_verdict`, `to_learn` (stretch-round additions signal the
  refine loop ran to round 3)
- `judge_report.json` — how many violations the judge had to repair
- a `<profile>/` subfolder — a dual-apply shadow ran; the shadow's own generation
  + judge + verdict + refine is **not** in `cost_usd` for that row if it ran
  detached, so note it as unmeasured spend

The known amplifier is the **verdict refine loop**: up to
`ATS_VERDICT_MAX_REFINES` (default 3) rewrite + re-render + re-verdict rounds,
and rolled-back rounds cost money too. If most expensive rows show refine
activity with a small final verdict gain, that is the lever.

## Step 5 — Report

```
Период:        <all / N дней>
Заявок:        <n>  (с ценой <n>, NULL <n>, reuse за $0 <n>)
Всего:         $<x>     Средняя: $<x>   Медиана: $<x>
Топ-5 дорогих: <company $x → verdict N> ...

По бэндам вердикта:
  <80    n=..  avg $..   sent/confirmed/answered ..
  90-94  n=..  avg $..   ...

Вывод: <одна-две фразы — что дорого и окупается ли>
Рычаг: <конкретный: ATS_VERDICT_MAX_REFINES, модель профиля, GEN_SKIP_PL_FOR_EN, ...>
```

State the recommendation as a measurement, not a hunch — and if the data does
not support changing anything, say that. `docs/LLM_COST_REDUCTION_PLAN.md` is
the running record of what has already been tried; check it before proposing
something that was measured and rejected.
