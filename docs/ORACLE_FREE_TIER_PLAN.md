# ORACLE_FREE_TIER Plan — move bot + api off the paid VPS onto Oracle Always Free

**Status:** draft — M0 not run
**Date:** 2026-09-09
**Motivation:** Owner shared a Threads post (@mikheenkov, 2026-09) on Oracle Cloud's
Always Free tier as a way to cut hosting spend for AI/agent side projects. The bot
and the api run on one Hetzner CX22 (2 shared x86 vCPU, 4 GB RAM, €4.35/month,
docs/DEPLOY.md §2.2) plus a 75 GB volume. The interesting part is NOT the €52/year —
it is that the free tier's compute allocation (1,500 OCPU-hours + 9,000 GB-hours
per month of Ampere A1 — for an Always Free tenancy, 2 OCPUs + 12 GB RAM as one
instance or two 1-OCPU ones — plus 200 GB block storage) gives the same core
count and 3× the RAM of the box we run on, and the box is already tight: on 2026-08-29 the deploy reported success for two days while the 75 GB
volume sat at 100 % (63.92 GB of untagged deploy images, 271 MB free —
docs/AGENT_LOG.md 2026-08-29). This plan asks whether the free tier is a real
upgrade for THIS workload or a trap, and answers it with one evening of
measurement before anything moves.

> Free-tier limits quoted here were re-read from Oracle's Always Free page on
> 2026-09-10 (the first draft said 4 OCPUs / 24 GB from memory — wrong, and caught
> in review). They are exactly the kind of thing a provider changes without
> notice; M0 step 0 is to re-read that page again, because a changed limit
> changes the arithmetic, not the method. Two constraints from the same page that
> shape M2: Always Free compute must live in the tenancy's **home region** (chosen
> at signup, cannot be changed — pick Frankfurt), and an instance idle for 7 days
> (CPU 95th percentile < 20 % AND network < 20 % AND, on A1, memory < 20 %) may be
> reclaimed.

## Problem

Three facts, only the first of which is a cost problem.

1. **Cost.** €4.35/month for the CX22 + the volume. Small in absolute terms; the
   Threads post's framing ("several servers add up") applies weakly — this project
   has one.
2. **Capacity.** 4 GB RAM is shared by the bot (LibreOffice headless spawns per
   render, Chromium via Playwright for 5 sources + the LinkedIn session fetch, the
   Claude CLI subprocess for the outage fallback) AND the sibling api/site
   containers ("this host also runs sibling projects", `.github/workflows/deploy.yml`).
   The disk incident above is the visible symptom; RAM pressure during a CLI-served
   apply concurrent with a hunt is the invisible one — nothing measures it today.
3. **The scrapers ARE the product, and they are IP-sensitive.** Five sources go
   through `cloudscraper` (pracuj, theprotocol, builtin, jobleads, inhire), LinkedIn
   is a guest HTML API that already 429s on datacenter IPs without a session
   (docs/review-2026-07/02-next-steps.md P0 #1). Oracle Cloud address ranges are
   flagged by anti-bot vendors more aggressively than a generic European VPS
   provider's. A migration that saves €52/year and turns pracuj/theprotocol into
   permanent 403 is a net loss by any measure — and it would show up only as a
   slow `source_health` decline, days after the cutover.

So the decision is not "is free better than paid" — it is "does THIS workload's
yield survive an IP move to a hyperscaler range, and does the arm64 rebuild cost
less than the capacity is worth".

## Non-goals

- **Not a multi-cloud / HA setup.** One box replaces one box. No failover, no
  second region, no load balancer.
- **Not a Kubernetes / Terraform rewrite.** `docker compose` + the existing
  GHCR→SSH deploy stay; only the target host and the image architecture change.
- **Not the site.** `job-hunter-site` is already on Cloudflare Pages (free); it does
  not move. Only `job-hunter` (bot) and `job-hunter-api` are in scope.
- **Not a residential-IP / proxy layer for scrapers.** If the Oracle IP fails the M0
  probe, the answer is "stay on Hetzner", not "add a proxy" — a proxy is a new
  paid dependency that would eat the saving and add a failure mode.
- **Not the LinkedIn Scout.** It runs on the owner's desktop for exactly the IP
  reason above (residential IP, docs/SCOUT_REPO_SPLIT_PLAN.md) and is unaffected.
- **No changes to the bot's Python code** beyond what an arm64 build forces
  (expected: none — see M1).

## M0 — Measure

Two independent questions, both answerable without touching prod and without a
single tracker write. Either one failing closes the plan.

### M0a — Does the yield survive the IP? (the one that matters)

**Baseline is already recorded.** `source_runs` (hunter/source_health.py) holds the
last `SOURCE_HEALTH_KEEP=50` runs per source on prod: yield count, ok flag, error
string. `/health` in Telegram renders it. That is the "from the Hetzner IP" side of
the comparison, for free, over weeks of runs, not one lucky sample.

**The other side:** an Always Free A1 instance (or, if A1 capacity is unavailable,
the AMD micro — same IP range, which is what is being tested) with a plain
`git clone` + `pip install -r requirements.lock` + `playwright install chromium
--with-deps`. No Docker, no `.env` beyond `TELEGRAM_*` left EMPTY (the sources do
not need it; `hunter.config` validation is only run by `hunter.py`, not by importing
`hunter.sources`). Then, from a shell on that box, call the same `search()` methods
the hunt loop calls — read-only HTTP, nothing written anywhere:

```bash
python - <<'PY'
from hunter.sources import ALL_SOURCES
PROBE = {"pracuj", "theprotocol", "builtin", "linkedin", "jobleads", "inhire", "justjoin", "nofluffjobs"}
for src in ALL_SOURCES:
    if src.name not in PROBE:
        continue
    try:  # one source's 403/timeout must not abort the rest — same boundary the hunt loop keeps
        print(f"{src.name:14s} {len(src.search()):4d}")
    except Exception as exc:  # noqa: BLE001 — a probe records the error, it does not handle it
        print(f"{src.name:14s} ERR  {type(exc).__name__}: {str(exc)[:120]}")
PY
```

Run it three times, ~20 minutes apart (Cloudflare decisions are per-session and a
single run proves nothing either way — the same discipline as the LinkedIn page-size
measurement of 2026-08-12, "control-verified: 3 identical requests → identical id
sets"). justjoin/nofluffjobs are in the list as CONTROLS: plain JSON APIs with no
anti-bot layer; if THEY drop to zero the box has a network problem, not an IP
reputation problem, and the run is void.

**Decision rule, stated before the run:**

- For each of pracuj, theprotocol, linkedin (the three highest-yield IP-sensitive
  sources): Oracle yield ≥ 70 % of the prod `/health` 20-run median on **all three
  runs** → pass. One run below → re-run once more; two runs below → **fail**.
- builtin / jobleads / inhire: informational. They are low-yield on prod already
  (jobleads is MANUAL-flow, inhire is Playwright-bound) and do not decide anything.
- **Any fail → close this plan as "IP range unsuitable", record the numbers in
  AGENT_LOG, stay on Hetzner.** No proxy, no retry with a different Oracle region
  unless the owner explicitly asks — regions share the reputation problem, and a
  region hunt is the sunk-cost path.

Cost of M0a: an Oracle account (needs a card on file, charged nothing on the free
tier), ~1 hour. Zero LLM calls, zero writes.

### M0b — Does the image build for arm64?

Independent of M0a and runnable on the owner's desktop today (Docker Desktop ships
QEMU/binfmt). The question is whether `Dockerfile` builds unchanged for
`linux/arm64`:

```bash
docker buildx build --platform linux/arm64 -t job-hunter:arm64-probe --load .
```

Known-good on arm64 Debian bookworm: `python:3.11-slim` (multi-arch),
`libreoffice` (Debian ships it), `nodejs`/`npm`, `@anthropic-ai/claude-code`
(publishes linux-arm64). The only step with real doubt is `playwright install
chromium --with-deps`: Playwright lists Debian 12 arm64 as supported for Chromium,
but the `--with-deps` package list has drifted between releases — this is a
"try it, read the error" item, not a research item.

**Decision rule:** the build finishes AND `docker run --platform linux/arm64
job-hunter:arm64-probe python -c "from playwright.sync_api import sync_playwright;
p=sync_playwright().start(); b=p.chromium.launch(); print(b.version); b.close()"`
prints a version → pass. Playwright fails → **not a plan-closer**, but M1 gains a
step (switch the base image to `ubuntu:24.04` + `python3.11`, or install Debian's
system `chromium` and point `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` at it) and the
plan's effort estimate roughly doubles. Note the QEMU build time too: LibreOffice +
Chromium under emulation is expected to take 20–40 min, which decides M2's CI shape.

### M0c — What are we actually paying for? (five minutes)

Read the Hetzner invoice: server + volume + any snapshot/backup add-on, per month.
And `docker stats` + `free -m` on the host during one CLI-served apply overlapping a
hunt slot — the one RAM number nobody has looked at. If peak RSS across containers
is comfortably under 4 GB, "capacity" drops out of the motivation and this becomes
a pure €52/year question, which is not worth M1–M4's risk on its own. **Owner call
at that point, with the number in hand.**

## M1..M4 — Milestones (only if M0a passes)

Each one is its own commit/PR, each has a rollback that is "don't switch DNS/SSH
target", since prod stays on Hetzner until M4.

**M1 — Multi-arch image in CI.** `deploy.yml` `build-and-push-action` gains
`platforms: linux/amd64,linux/arm64` via `docker/setup-qemu-action` +
`docker/setup-buildx-action`. If M0b measured the QEMU build over ~25 min, use a
GitHub-hosted arm64 runner for the arm64 half (a matrix job + `docker buildx
imagetools create` to merge the manifest) instead — CI time is developer time.
Files: `.github/workflows/deploy.yml`, possibly `Dockerfile` (M0b outcome).
Test: the pushed manifest lists both architectures (`docker buildx imagetools
inspect ghcr.io/igrdevelop/job-hunter:latest`). Rollback: revert the workflow; the
amd64 image is unchanged by this step. Prod does not notice M1 at all.

**M2 — Oracle host provisioned like the Hetzner one, in parallel.** Same
`docker compose` layout, same volumes (`users/`, `tracker.db`, `.claude-cli/`,
token files, `gsheets_state.json`), same `deploy` user + SSH key, same host-cron
from docs/DEPLOY.md (the 2026-08-29 prune + free-space gate). Ubuntu 24.04 aarch64
image, in the tenancy's home region (Always Free compute cannot be created
anywhere else). Firewall: Oracle's VCN security list blocks everything inbound by
default — open 22 only; the api's port is whatever the sibling compose exposes
today, mirror it.

**Sizing is the idle-reclamation control, not billing mode.** Oracle reclaims an
Always Free instance idle for 7 days, where idle = CPU 95th percentile < 20 % AND
network < 20 % AND (A1 only) memory < 20 %; the page says nothing about a
Pay-As-You-Go upgrade changing that, so the plan must not rely on one. The
criteria are *percentages of the instance's own size*: bot + api together sit
around 1–1.5 GB RSS (M0c measures the real number), which on the full 12 GB is
~10 % — idle by the memory criterion — but on a 1 OCPU / 6 GB instance is ~25 %,
above the threshold. So: create the instance at **1 OCPU / 6 GB** (half the
allocation, still 1.5× the CX22's RAM), confirm after the first week that the
OCI console's utilization graphs show memory above 20 %, and grow it only if
M0c's peak says it must. The residual risk — Oracle reclaiming anyway, or
tightening the rule — stays and is stated in Risks; the rollback for it is the
same redeploy path as for any host loss.

Upgrading the account to Pay-As-You-Go is still worth doing for a different,
documented reason: it unlocks more compute shapes and, anecdotally, escapes the
"out of host capacity" lottery on A1; Oracle states Always Free resources stay
free after the upgrade and only usage above the limits is charged. Set a budget
alert at $1 so a mis-sized resource is noticed before an invoice. Rollback:
terminate the instance. Prod untouched.

**M3 — Shadow run: hunt-only on Oracle for one week.** Copy NOTHING from prod
except what a hunt needs to run: the owner's `candidate.yaml` (the filters read
home-city aliases and languages from it) and a `.env` holding a SEPARATE test bot
token + the admin chat id (a Telegram bot token can be polled by one process only
— the prod token must never run on two hosts at once). No `users/**/Applications`,
no `tracker.db` (yield is `source_health`'s pre-dedup count, so dedup state does
not change the measurement), no Google/Gmail/Drive tokens, no `.secrets/`, no
`.claude-cli/` — none of them are needed with `AUTO_APPLY=false`,
`GSHEETS_ENABLED=false`, `GDRIVE_ENABLED=false`, `GMAIL_ENABLED=false`, all
schedule slots on. An empty tracker means every found job is "new" and arrives
as a card on the test bot; that noise is the point (it is the yield), and the
cards' Apply buttons hit a bot with no LLM key and no CLI login, so nothing can
be generated by accident. At the end of the week the shadow volumes are wiped
(`docker compose down -v` + delete the compose dir) — M4 starts from a fresh copy,
never from the shadow's. This exercises every scraper on the new IP for a week of real slots,
which M0a's three samples cannot — and it is the only way to see a slow
reputation decline. Read `/health` on both bots daily. Decision rule: same 70 %
median rule as M0a, over the week. Fail → stop here, terminate, record. Prod
untouched throughout.

**M4 — Cutover.** One maintenance window: stop EVERY writer on Hetzner — the bot
AND `job-hunter-api` (they share `tracker.db`, whose `profile_jobs` /
`telegram_link_codes` tables the api writes, and the `users/` mount; an rsync
under a live writer can miss WAL-backed rows or copy `tracker.db` and its `-wal` /
`-shm` sidecars from different instants). Then either `rsync` the volumes one
final time with all three SQLite files together, or take `sqlite3 tracker.db
".backup tracker.snapshot.db"` and ship the snapshot — plus `users/`, tokens,
`.secrets/`, `.claude-cli/`. Start on Oracle with the PROD token,
switch `VPS_HOST` in the GitHub secrets, verify `/status` + one `/hunt` + one
manual paste apply end-to-end (docs, Sheets row, Drive folder). Keep the Hetzner
box **stopped, not deleted, for 30 days** — the rollback is "start it again and
flip `VPS_HOST` back", with at most the 30 days of tracker rows to re-sync
(`/sync_sent` pulls them from Sheets, which is the system of record for Sent
anyway). Then update docs/DEPLOY.md, `reference_vps_server` in memory, and
CLAUDE.md's deploy notes.

## Risks

- **Slow IP-reputation decay after cutover** — M3's week is the guard; after M4
  the existing `source_health.newly_broken()` alert (3 consecutive dry runs on a
  previously-working source) is the rail, and the 30-day stopped Hetzner box is the
  rollback.
- **Instance reclaimed as idle / capacity revoked.** The documented idle rule is
  three utilization thresholds over 7 days; M2 sizes the instance so real usage
  stays above them (memory is the binding one on A1) and the first week's console
  graphs confirm it. Billing mode is NOT a mitigation — Oracle's page does not say
  a PAYG upgrade exempts Always Free instances, and the first draft of this plan
  wrongly assumed it did. Residual risk (Oracle reclaims anyway, tightens the rule,
  or acts at account level) has the same defence as a Hetzner outage today:
  `backups/`, Sheets, Drive, and the redeploy path. Nothing in this plan makes that
  worse, but nothing makes it better either — a free tier has no SLA and the plan
  should say so plainly.
- **arm64-only behaviour differences** (LibreOffice rendering, font metrics,
  Chromium). `tests/test_golden_apply_e2e.py` + the CLI golden run in CI on amd64
  only; M3's shadow week catches scraper differences but NOT rendering ones, since
  `AUTO_APPLY=false`. Add to M3: one manual paste apply on the Oracle bot into a
  throwaway `APPLICATIONS_DIR`, diff the PDF text against the same vacancy rendered
  on Hetzner (`hunter.pdf_text`). A font difference is fixable (install the same
  `fonts-*` packages); a LibreOffice crash is a plan-closer for arm64.
- **CI time doubles** if QEMU is used — M0b measures it and M1 picks the runner
  accordingly.
- **Two hosts, one Telegram token** — the M3/M4 text above is explicit about the
  separate test token because polling conflicts fail silently (updates split
  between processes). Worth a line in docs/DEPLOY.md regardless of this plan.

## Cost

Zero LLM calls anywhere in this plan. Money: −€4.35/month (+ volume) after M4;
+$0 on Oracle if resources stay within Always Free (the PAYG upgrade in M2 is a
billing-mode change, not a charge; the $1 budget alert is the tripwire). Effort:
M0 one evening; M1 half a day; M2 half a day; M3 a week of wall-clock, ~1 hour of
attention; M4 one evening. Roughly two working days total against €52/year — which
is why M0c matters: the plan is justified by capacity, or it is not justified.

## Open questions

1. Is the current box actually RAM/disk-constrained during a CLI apply overlapping
   a hunt (M0c number)? If peak usage is under ~2.5 GB, is €52/year alone worth
   two days and a new provider? (yes/no, after M0c)
2. If A1 capacity is unavailable in Frankfurt/Amsterdam for a week, do we stop, or
   accept a farther EU region and re-run M0a there? (Recommendation: stop — the plan
   should not become a capacity hunt.)
3. Does the sibling `job-hunter-api` move in the same M4 window, or first (it is
   stateless apart from `app.sqlite` and the shared `users/` mount, so it is the
   lower-risk rehearsal)? (Recommendation: api first, as the M4 dry run.)
