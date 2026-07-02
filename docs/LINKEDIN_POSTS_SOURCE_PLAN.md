# LinkedIn Posts source — implementation plan (source #22)

**Status:** PLANNED (this document is the implementation spec; implementation lands in
THIS branch/PR, one commit per milestone — owner wants a single PR per task)
**Branch:** `feat/linkedin-posts-source` (from origin/master @ 1f969ed)
**Audience:** implementing agent — self-contained, no chat context needed.

---

## 1. Why

Many vacancies never reach LinkedIn Jobs: recruiters and team leads post them as
ordinary feed posts ("We're hiring an Angular developer — DM me / apply here").
Searching LinkedIn **content** for "angular" surfaces these. The owner confirmed
finding real vacancies this way manually. Goal: scrape the content-search results
on a schedule and surface hiring posts as Telegram cards.

## 2. What already exists (reuse, don't rebuild)

| Need | Existing machinery |
|---|---|
| Authenticated LinkedIn session | `LINKEDIN_STORAGE_STATE` (Playwright storage state) written by `tools/linkedin_login.py`; already consumed by `hunter/sources/linkedin.py::fetch_text` (`_storage_state_path()` helper) |
| Playwright scraping pattern | `linkedin.py::fetch_text` (sync API, login-redirect detection, innerText extraction) and `inhire.py::search` (Playwright-driven search) |
| Source plumbing | `BaseSource` ABC (`search`/`matches_url`/`fetch_text`), `ALL_SOURCES` registry + `fetch_job_text(url)` dispatcher in `hunter/sources/__init__.py`, per-source config toggle pattern in `hunter/config.py` |
| Dedup | URL-based dedup in tracker (`normalize_url`); the post permalink is a stable unique URL |
| Health monitoring | `source_health.record_run` wraps every `source.search()` automatically |
| CV generation from messy text | The apply pipeline takes whatever `fetch_text(url)` returns — a post's raw text works like any fetched posting |

**Key simplification discovered during design:** every post has a permanent permalink
(`https://www.linkedin.com/feed/update/urn:li:activity:<ID>/`). So the Apply button
works through the NORMAL flow — `_handle_apply` → apply subprocess → `fetch_job_text(url)`
→ our `fetch_text` re-fetches the post text. No paste-flow plumbing needed.

## 3. Product decisions (agreed with owner)

- **Posts are never auto-applied.** "Apply" to a post usually means DM'ing the author;
  the pipeline's job is to generate the tailored CV so the owner can attach it. Posts
  therefore ALWAYS go to Telegram cards (Apply/Skip), even when `AUTO_APPLY=true`.
- **Off by default.** `LINKEDIN_POSTS_ENABLED=false` — authenticated feed scraping is
  the most ban-prone scraper in the roster; the owner opts in explicitly.
- **Low volume.** ~20 newest posts per keyword per run, past-week filter, 2–3 scroll
  iterations max. Volume discipline is the main anti-flagging lever.

## 4. Design

### 4.1 New file `hunter/sources/linkedin_posts.py`

```python
class LinkedInPostsSource(BaseSource):
    name = "linkedin_posts"
    manual_only = True          # see 4.4 — never auto-applied
```

**`search()`** — Playwright (sync API, mirroring `linkedin.py::fetch_text` style):

1. Bail out early (return `[]`, log why) when: playwright not installed, or
   `_storage_state_path()` (import it from `hunter.sources.linkedin`) returns None.
2. For each keyword in `LINKEDIN_POSTS_KEYWORDS` (default:
   `angular hiring,angular developer,frontend developer hiring`):
   open `https://www.linkedin.com/search/results/content/?keywords={kw}&sortBy=%22date_posted%22&datePosted=%22past-week%22`
   (verify the exact param spelling live — LinkedIn quotes enum values in the URL).
3. Detect login redirect exactly like `linkedin.py` (`/login` or `/checkpoint` in
   `page.url` → log "session expired, re-run tools/linkedin_login.py", return `[]`).
4. Scroll 2–3× (`page.mouse.wheel` + small waits) to load ~20 results, then parse
   rendered DOM. Primary selectors (verify live, they churn):
   - result container: `div[data-urn^="urn:li:activity"]` (fallback:
     `.feed-shared-update-v2`, which carries `data-urn`)
   - post text: `.update-components-text` (innerText)
   - author: `.update-components-actor__title` (first line of innerText)
   The `data-urn` attribute gives the activity URN → permalink
   `https://www.linkedin.com/feed/update/{urn}/`.
   **Fallback strategy if DOM parsing proves too unstable during live testing:**
   intercept Voyager responses instead (`page.on("response")`, filter URLs containing
   `/voyager/api/graphql` + `search` and mine `included[]` for `updateV2`/actor
   entities). More stable data, less stable endpoint — pick whichever survives a live
   session, document the choice in the module docstring and the scraper-health table.
5. Filter each post through the **hiring heuristic** (4.2). Survivors become Jobs:
   - `title`: `"[LI post] " + first ~70 chars of the post text` (single line, ellipsis)
   - `company`: author name (person or company page)
   - `location`: `""` (posts rarely state one — see 4.4 filter bypass)
   - `salary`: None; `url`: permalink; `source`: `"linkedin_posts"`
   - `raw`: `{"post_text": full_text, "author": author}` (debugging + tests)
6. Per-keyword de-dup within the run (same URN can match several keywords).

**`matches_url(url)`** — `"linkedin.com" in host AND "/feed/update/" in path`.
**Dispatcher precedence (critical):** `linkedin.py::matches_url` claims ALL
linkedin.com URLs. Register `LinkedInPostsSource` BEFORE `LinkedInSource` in
`hunter/sources/__init__.py` (both in `ALL_SOURCES` and in the `fetch_job_text`
dispatch order) so `/feed/update/` URLs route to the posts fetcher. Add a test
pinning this precedence.

**`fetch_text(url)`** — Playwright + session (same skeleton as `linkedin.py::fetch_text`):
open the permalink, wait for `.update-components-text`, return
`f"LinkedIn post by {author}\n\n{post_text}"`. Raise `RuntimeError` on login redirect
or <100 chars (the apply pipeline's too-short gate then aborts cleanly). No
html_fallback — a logged-out permalink returns a stub, better to fail loudly.

### 4.2 Hiring heuristic (pure function, unit-testable, no Playwright)

`_is_hiring_post(text: str) -> bool` in the same module:

- MUST match a stack keyword: `angular` (case-insensitive; keep configurable list in
  sync with `LINKEDIN_POSTS_KEYWORDS` stems).
- MUST match a hiring signal (EN+PL):
  `hiring|we're looking for|looking for a|open role|open position|vacancy|join (our|the) team|#hiring|#rekrutacja|szukamy|poszukujemy|zatrudnimy|praca dla`
- MUST NOT match candidate-side signals (people announcing THEY seek work):
  `open to work|looking for (a )?new (opportunity|role)|szukam pracy|#opentowork`
- MUST NOT match obvious course/ad spam: `course|webinar|bootcamp|szkolenie|kurs`
  (tune against live data; start narrow).

Keep every regex list a module-level tuple so tests can pin behavior; follow the
`filters.py` style.

**Location policy (owner requirement: remote | hybrid Wrocław | office Wrocław).**
Posts rarely state a location, so the gate is three-way, applied to the POST TEXT:

1. *Explicit match* — text mentions remote (`remote|zdalnie|praca zdalna|fully remote`)
   or Wrocław in any arrangement → keep.
2. *Explicit mismatch* — on-site/hybrid signal tied to a non-Wrocław city → reject.
   Do NOT reimplement this: reuse the existing body-level machinery in
   `hunter/filters.py` (`_is_unwanted_onsite_location` + the anti-hybrid city set,
   incl. the Warsaw/Kraków weekly-hybrid exception) by running the post text through
   `filters.screen_job_text(post_text)`-style checks inside the source.
3. *Unknown* — no location info at all → KEEP and send the card. "Unknown" is the
   normal case for recruiter posts ("hiring Angular devs — DM me"); auto-rejecting it
   would drop most real finds. The human decides at the Apply/Skip card.

This also shapes the default queries: add Polish ones that surface the
Wrocław/remote-PL segment the English query misses —
`LINKEDIN_POSTS_KEYWORDS` default becomes
`angular hiring,angular developer,angular praca zdalna,angular Wrocław`.

### 4.3 Config (`hunter/config.py` + `.env.example` + CLAUDE.md table)

| Var | Default | Meaning |
|---|---|---|
| `LINKEDIN_POSTS_ENABLED` | `false` | master toggle (off — ban-prone, opt-in) |
| `LINKEDIN_POSTS_KEYWORDS` | `angular hiring,angular developer,frontend developer hiring` | comma-separated content-search queries |
| `LINKEDIN_POSTS_MAX_PER_KEYWORD` | `20` | cap parsed posts per query per run |

Session config is shared: reuses `LINKEDIN_STORAGE_STATE` (do NOT add a second var).

### 4.4 Routing changes (small, surgical)

1. **Manual-only in the hunt loop** (`hunter/main.py`, ACT step ~line 284): posts must
   go to cards even when `AUTO_APPLY=true`. Mechanism — a `manual_only: bool = False`
   class attribute on `BaseSource`, set `True` on `LinkedInPostsSource`; in the ACT
   step partition `new_jobs` by the originating source's flag (source name → source
   object lookup via the ALL_SOURCES roster): manual-only jobs → `send_job_cards`,
   the rest → existing AUTO branch. Attribute on the source (not a name check in
   main.py) so the next posts-like source gets it for free.
2. **Location filter bypass** (`hunter/filters.py`): posts carry `location=""`. Verify
   what `classify_job` does with an empty location; if it rejects, add an explicit
   exemption: empty location is allowed when `job.source == "linkedin_posts"`
   (comment: post geography is unknowable pre-read; the human filters at the card).
   Title-keyword filters WILL likely reject `"[LI post] …"` titles that lack "angular"
   — that's fine and intended (the heuristic already required a stack keyword in the
   text, and the title embeds the text head). Verify with a unit test which central
   filters fire on a representative post-Job and exempt ONLY what's provably wrong
   for posts (each exemption gets its own test + comment).
3. **No tracker changes.** Dedup by permalink URL works as-is. ATS/verdict pipeline
   untouched — a post that gets Applied flows through the standard apply path (fetch
   → LLM → docs → verdict) like any vacancy.

### 4.5 Schedule

Nothing to do: registering in `ALL_SOURCES` + the config toggle wires it into the
staggered JobQueue automatically (3 runs/day like every source). Volume caps (4.1)
are the throttle. If the session gets flagged in practice, a follow-up can add a
runs-per-day limit — out of scope here.

## 4.6 LIVE PROBE FINDINGS (2026-07-02/03) — read before touching selectors

A live probe with the owner's real session established facts that OVERRIDE the
selector guidance in 4.1:

1. **The goal is real.** The very first rendered result for `angular hiring` was a
   genuine hiring post (Deloitte TA, Java/React/Angular, "let's schedule a call").
2. **The classic selectors are DEAD on the content-search surface.** The rebuilt
   LinkedIn UI ("Chameleon") ships hashed CSS classes (`_54361ba7 …`), no
   `data-urn`, no `.feed-shared-update-v2`, no `.update-components-text`.
3. **Network interception found NOTHING.** No Voyager/GraphQL XHR carries the posts —
   the results arrive server-side-rendered into **shadow DOM**. `page.content()`
   does not serialize it; `document.body.innerText` DOES expose the post text.
   → Extraction strategy: a recursive **shadow-root walker** in `page.evaluate`
   (descend `el.shadowRoot`, collect `a[href*="/feed/update/"]` for permalinks +
   per-card composed innerText), NOT class selectors, NOT response interception.
4. **Anti-bot is aggressive.** Naive headless Chromium was flagged within 2–3 page
   loads: `li.protechts.net … uc=scraping` + reCAPTCHA, and LinkedIn **invalidated
   the whole session** — which also breaks the production LinkedIn detail fetches
   (shared `LINKEDIN_STORAGE_STATE`). Hard requirements for the implementation:
   - launch the REAL installed Chrome (`channel="chrome"`), **headed** where
     possible; `--disable-blink-features=AutomationControlled`; hide
     `navigator.webdriver` via init script;
   - ONE page load per keyword per run, human-pace waits, no parallelism;
   - treat a login/checkpoint redirect as "session burned": log loudly, return [],
     and Telegram-notify the owner to re-run `tools/linkedin_login.py` (reuse the
     session-expired messaging pattern from `linkedin.py::fetch_text`);
   - if headed real-Chrome still gets flagged during M5 live verification, demote
     the feature: manual `/hunt linkedin_posts` only (no schedule), or shelve —
     the shared session powering prod fetches is worth more than source #22.

**Probe round 2 (headed real Chrome — SURVIVED 3 consecutive runs, stealth approach
validated). Additional findings that change the design:**

5. **No permalinks exist in the rendered page.** 138 anchors collected across all
   shadow roots — zero contain `/feed/update/`, `activity`, or `ugcPost`. Post
   permalinks are only reachable via the "⋯ → Copy link" JS menu (clicking it per
   post = more bot surface; rejected). **Design change — the permalink-based Apply
   flow (4.1 "key simplification") is DEAD:**
   - `Job.url` becomes a synthetic stable key: `https://linkedin.com/posts/#p{md5(author + text_head)[:16]}`
     — good enough for tracker URL-dedup; reposts with edited text create a new key
     (acceptable; company+title dedup won't help here either).
   - The full post text is captured AT SEARCH TIME into `Job.raw["post_text"]`.
   - **Apply routes through the existing paste flow**, not fetch: the Apply handler
     for `linkedin_posts` jobs writes `raw["post_text"]` to a temp file and invokes
     the apply subprocess with `paste_file` (the exact mechanism
     `bot/apply_runner.py::_handle_paste` already uses). `fetch_text()` on the
     synthetic URL raises with a clear message (nothing to fetch).
   - `matches_url` matches only the synthetic prefix — the dispatcher-precedence
     concern vs `linkedin.py` disappears (no real linkedin.com post URLs flow).
6. **EN query noise profile.** `angular hiring` (global, sorted by date) is dominated
   by US staffing-mill posts (Java/.NET fullstack, W2/C2C/H1B, US on-site cities)
   that mention Angular only inside a stack dump. Live calibration: 9/13 passed the
   naive heuristic but only ~2 were genuinely relevant. Required extra negatives:
   `\b(W2|C2C|H1B|USC|GC|green card)\b`, US-state on-site patterns
   (`\bon-?site\b.*\b(VA|NJ|NY|TX|SC|CA|GA|FL|IL)\b` and full-city forms), and an
   **Angular-prominence gate**: `angular` must appear in the first ~200 chars OR the
   text must match `angular (developer|engineer|frontend)` — a bare stack-dump
   mention is not enough. Reuse `filters.py` body-level gates (backend-primary,
   React-only) on the post text as well.
7. **PL query works as intended.** `angular praca zdalna` returned mostly
   candidate-side posts ("Szukam nowego projektu…" — correctly rejected: no
   `szukamy/zatrudnimy` hiring signal, singular `szukam` ≠ plural `szukamy`) plus a
   genuine hiring post ("Szukamy osoby od frontendu na rolę Senior Frontend
   Engineer…" — correctly passed). The singular/plural distinction is load-bearing;
   pin it with tests.
8. **Author extraction from innerText blocks**: posts are separated by `Feed post`
   marker lines in `document.body.innerText`; author = the next non-empty line;
   the `• 3rd+ … • Follow` header noise between author and body must be stripped
   (drop lines up to and including the `Follow`/`Connect` line).

## 4.7 DEPLOYMENT ARCHITECTURE — Variant A (server-side), owner's choice

The probes ran on the owner's Windows desktop (headed real Chrome, residential IP)
and survived. Production is the Docker bot on a server — three stealth factors
degrade there (no display, datacenter IP, container fingerprint). The owner chose
**Variant A: run the scraper inside the bot container** (rejected for now:
B = Windows-side scout writing JSON to Drive; C = digest-only). Variant A ships
with MANDATORY safety rails; if they trip repeatedly, demote to B (see below).

### A.1 Infra (Dockerfile + entrypoint)

- Install `google-chrome-stable` (Playwright `channel="chrome"` requires the real
  Chrome binary — NOT the bundled chromium), `xvfb`, `fonts-liberation` +
  `fonts-noto` (fingerprint: a font-poor container is a tell), `--no-install-recommends`.
- Entrypoint starts `Xvfb :99 -screen 0 1920x1080x24` as a background process and
  exports `DISPLAY=:99` before launching the bot. Keep it running for the container
  lifetime (cheap); do NOT wrap the whole bot in `xvfb-run` (signal handling).
- Container env: `TZ=Europe/Warsaw` and Chrome launched with `--lang=en-US` or
  `pl-PL` consistent with the owner's real browser. Timezone mismatch vs the
  session's origin is a flag signal.
- **Persistent browser profile**: mount a volume for `~/.li-posts-profile` and use
  `playwright.chromium.launch_persistent_context(user_data_dir=..., channel="chrome",
  headless=False, args=[...])`. A browser with history/localStorage looks real; a
  fresh context every run does not. Seed the profile ONCE by importing cookies from
  `LINKEDIN_STORAGE_STATE` on first run (copy li_at etc. into the context), then let
  the profile own them.
- RAM: budget ~600MB per run; runs are serialized (one keyword at a time, one run
  per day) so no concurrent browsers.

### A.2 Safety rails (non-negotiable, implement in M2)

1. **Circuit breaker with auto-disable.** On ANY login/checkpoint/authwall redirect
   or a `protechts.net`/captcha response during a run: abort immediately, write
   DB config key `linkedin_posts_tripped = <iso timestamp>`, Telegram-alert the
   owner ("LinkedIn flagged the server browser — source auto-disabled; prod detail
   fetches may be affected; re-run tools/linkedin_login.py and /linkedin_posts_reset
   to re-enable"). `search()` returns [] while tripped — no retries, ever, until
   the owner explicitly resets.
2. **Self-throttle: max ONE run per 20h** regardless of the 3×/day source schedule —
   `search()` checks its last-run timestamp (own DB config key) and no-ops otherwise,
   with a jittered ±90min window so runs don't land at identical times daily.
3. **One keyword per run**, rotating through `LINKEDIN_POSTS_KEYWORDS` round-robin
   (persisted index) — minimal surface per day, full coverage over the week.
4. **Two trips in 14 days → permanent demotion**: the second trip sets
   `linkedin_posts_demoted=true`; the source stays off and the Telegram alert says
   "switching to Variant B (Windows scout) is recommended — see plan §4.7". This
   criterion is agreed with the owner in advance; no re-litigating at 2 a.m.
5. **Staged rollout**: after deploy, the first runs are MANUAL ONLY
   (`/hunt linkedin_posts` while watching logs); enable the schedule only after
   3 consecutive clean manual runs on the server.

### A.3 Accepted residual risks (owner sign-off)

- Datacenter IP + geo-jump from the session's origin can trip an account-security
  checkpoint independent of scraping behavior.
- A flag kills the SHARED session → prod LinkedIn detail fetches fail until the
  owner re-logs in from the desktop. The circuit breaker limits repetition but
  cannot prevent the first hit.
- Repeated flags can escalate to an account restriction on the owner's primary
  professional profile. This is the owner's explicit call.

## 5. Risks — read before implementing

- **ToS / ban risk:** authenticated feed scraping is against LinkedIn ToS; the
  session (`li_at` cookie) can get flagged. Mitigations: off by default, tiny volume,
  no parallelism, human-like waits between scrolls (1–2 s), reuse of one browser
  context per run. The owner accepts the risk knowingly (same session already does
  authenticated detail fetches).
- **DOM churn:** the feed DOM is obfuscated and changes often. This WILL be the most
  fragile of the 22 scrapers. `source_health` + `/health` catch silent breakage;
  keep ALL selectors in module-level constants; save a fixture snapshot of the
  rendered results DOM into `tests/fixtures/` during live verification.
- **False positives:** heuristic noise is acceptable — cards are cheap to Skip. Tune
  the negative lists against the first live batches rather than over-engineering now.
- **Empty results ≠ broken:** without `LINKEDIN_STORAGE_STATE` the source logs and
  returns `[]` — never raises (mirrors Inhire-without-Playwright behavior).

## 6. Milestones (one commit each, tests in the same commit) — Variant A revision

| M | Scope | Files |
|---|---|---|
| M1 | Pure logic: hiring heuristic (incl. US-staffing negatives + Angular-prominence gate + szukam/szukamy distinction), innerText block parser ("Feed post" splitter, author extraction, header-noise strip), synthetic URL builder, location three-way gate; module skeleton with search() stub | `hunter/sources/linkedin_posts.py`, `tests/test_linkedin_posts.py` (fixtures from the live probes: `tests/fixtures/linkedin_posts/*.txt`) |
| M2 | Playwright `search()`: persistent-context launch (channel=chrome, headed under Xvfb), shadow-walker innerText capture, circuit breaker + auto-disable + trip alerts + self-throttle + keyword rotation (A.2) | same + DB config keys |
| M3 | Apply routing: `manual_only` on BaseSource + main.py partition; paste-flow Apply for posts jobs (post text from Job.raw → paste_file); `matches_url`/`fetch_text` for the synthetic URL (raise with clear message); filters exemption(s) with tests | `hunter/sources/base.py`, `hunter/main.py`, `hunter/commands/url_message.py` or `bot/apply_runner.py`, `hunter/filters.py` |
| M4 | Infra: Dockerfile (google-chrome-stable, xvfb, fonts), entrypoint Xvfb + DISPLAY, profile volume in docker-compose, profile seeding from LINKEDIN_STORAGE_STATE | `Dockerfile`, `docker-compose.yml`, entrypoint |
| M5 | Config vars + `.env.example` + CLAUDE.md (sources 21→22, config table, Scraper Health row, work log) + staged live verification: dev-machine run first, then 3 clean MANUAL `/hunt linkedin_posts` runs on the server before enabling the schedule | config, docs |

**Live verification (M5):** dev first —
`python -c "from hunter.sources.linkedin_posts import LinkedInPostsSource; print(len(LinkedInPostsSource().search()))"`
on the owner's Windows machine (headed, no Xvfb); then deploy, seed the profile,
and do the staged server rollout per A.2.5. Update the Scraper Health table with
the verified date and note "Variant A + rails; demotion criterion active".

## 7. Definition of done

- `pytest tests/` fully green, `ruff check .` clean, `python -m compileall .` clean.
- Source disabled by default; enabling without a session degrades to `[]` + log line.
- A `/hunt linkedin_posts` run on the owner's machine produced at least one card and
  one successful Apply → docs generated from a post permalink.
- CLAUDE.md updated in the same PR (sources count, tables, work log).
