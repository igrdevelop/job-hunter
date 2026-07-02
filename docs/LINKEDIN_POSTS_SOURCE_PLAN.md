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

## 6. Milestones (one commit each, tests in the same commit)

| M | Scope | Files |
|---|---|---|
| M1 | Pure logic: hiring heuristic + URN→permalink + title builder + `matches_url`; module skeleton with search() stub returning [] | `hunter/sources/linkedin_posts.py`, `tests/test_linkedin_posts.py` |
| M2 | Playwright `search()` (DOM parse, scroll, login-redirect guard) + fixture-based parse tests (parse function takes HTML/eval output, testable without Playwright) | same |
| M3 | `fetch_text()` for permalinks + dispatcher registration BEFORE LinkedInSource + precedence test | `hunter/sources/__init__.py` |
| M4 | Routing: `manual_only` on BaseSource + main.py partition + filters exemption(s), each with a test | `hunter/sources/base.py`, `hunter/main.py`, `hunter/filters.py` |
| M5 | Config vars + `.env.example` + CLAUDE.md (sources table 21→22, config table, Scraper Health row, work log) + live verification with the owner's real session | config, docs |

**Live verification (M5, needs the owner's machine/session):** run
`python -c "from hunter.sources.linkedin_posts import LinkedInPostsSource; print(LinkedInPostsSource().search())"`
with a fresh `tools/linkedin_login.py` session; confirm ≥1 real hiring post parsed;
then `/hunt linkedin_posts` in Telegram and confirm the card renders and Apply
generates docs from the permalink. Update the Scraper Health table with the verified
date and the chosen parse strategy (DOM vs Voyager interception).

## 7. Definition of done

- `pytest tests/` fully green, `ruff check .` clean, `python -m compileall .` clean.
- Source disabled by default; enabling without a session degrades to `[]` + log line.
- A `/hunt linkedin_posts` run on the owner's machine produced at least one card and
  one successful Apply → docs generated from a post permalink.
- CLAUDE.md updated in the same PR (sources count, tables, work log).
