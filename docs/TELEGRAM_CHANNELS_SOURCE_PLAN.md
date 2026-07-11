# Telegram Channels Source — Implementation Plan

**Status:** APPROVED, ready to implement
**Branch:** `feat/telegram-channels-source` (created from `origin/master`)
**Owner request (2026-07-10/11):** add Telegram job channels as a new source. Idea came
from https://github.com/strelov1/freehire (`docs/telegram-channels.md`,
`internal/telegram/`), but a live probe (§1.2) flipped the channel list: freehire's
RU-market channels yield ≈0 relevant roles; frontend/EU channels NOT in their list are
the real target.

**For the implementing agent:** this plan is self-contained. Follow the repo rules in
CLAUDE.md ("Important Rules for Agents"): one milestone per commit, tests in the same
commit, `ruff check .` + `python -m compileall .` + `pytest tests/` green after every
milestone, update CLAUDE.md in the final commit (sources table 22→23, config table,
repo layout, Scraper Health row, Agent Work Log entry). No live HTTP in tests —
fixtures/mocks only (repo convention). M4 is the only step that touches the network,
and only from the dev machine, read-only except ONE deliberately-approved apply run.

---

## 1. Background

### 1.1 What freehire does (verified 2026-07-10)

- **Transport:** plain HTTP GET on the **public web preview** `https://t.me/s/{channel}`
  — no MTProto, no Bot API, no login, no cookies, no anti-bot wall. Their `fetch.go`
  calls this "the single transport boundary". Custom UA, 15s timeout, 8 MiB body cap.
- **Config:** flat YAML, two fields per entry: `channel` + `kind`
  (`board` = one vacancy per post, `authored` = editorial digest).
- **Pipeline:** fetch → HTML parse (each post is a `.tgme_widget_message` block) →
  cheap prefilter → LLM extraction.
- We copy the transport + parse idea; we do NOT copy their channel list or their LLM
  extraction step (§2.8).

**Key consequence:** unlike LinkedIn, this needs no session and no desktop-side
component. The source lives **inside the bot process / Docker image**, on the normal
staggered hunt schedule, as an ordinary 23rd source. No relay, no scout-style split.

### 1.2 Live probe findings (2026-07-11) — drives the channel list

Checked real `/s/` previews (~20 latest posts each):

| Channel | Verdict |
|---|---|
| `Remoteit`, `it_vakansii_jobs`, `geekjobs` (freehire tier-1) | ~50 posts total: **0 Angular**, 1 Vue-fullstack (RU), 1 JS-fullstack (GMT+1). RU-market, RUB salaries, relocation to Cyprus/Armenia. Expected yield ≈ 1–3 candidates/**month**, most dying at filters. |
| `findmyremote_frontend` (NOT in freehire's list) | Dedicated frontend channel, daily posts: Senior Frontend **(Angular)** @ Accesa, Team Lead Frontend @ Miratech (**Poland**), Senior Front-End (React) @ Software Mind — 2-3 relevant posts/**week** in the current window. Posts are short aggregator-style: role @ company \| location + **outbound link** to the real posting. |
| `IT_job_Poland`, `rabotafrontend` | Mixed bags (RU-language, PL-diaspora market), occasional React/Angular. Worth including, low expectations. |

Two conclusions baked into this plan:
1. **Starter list = frontend/EU channels first** (§6), RU boards only as a small
   experiment.
2. **Outbound-link resolution is v1, not v2**: aggregator posts are often 3 lines +
   a link — generating a CV from the post text alone would be garbage; the link
   target is the real posting.

---

## 2. Design decisions

### 2.1 `job.url` — resolved external link when present, else the post permalink

Every post has a stable public permalink `https://t.me/{channel}/{msg_id}` (href of
the post's date link on the `/s/` page). Additionally, many posts carry an outbound
link to the actual posting (job board / ATS / aggregator page).

Rule, per post:
- **If the post has an external job link** (§2.2): `job.url` = that link, cleaned via
  `hunter.sources.html_fallback.clean_url`. Benefits: cross-source dedup (the same
  vacancy arriving later via another board dedups correctly), normal fetch through
  the existing `fetch_job_text()` dispatcher (a nofluffjobs link goes to the
  NoFluffJobs fetcher, an unknown ATS host falls back to `html_fallback`), real
  employer URL on the tracker row.
- **Else** (self-contained text post): `job.url` = the `t.me/{channel}/{msg_id}`
  permalink; our own `fetch_text()` (§2.3) serves it.

Keep the t.me permalink in `job.raw["tg_permalink"]` in BOTH cases (convenience, shown
nowhere special in v1 — `hunter/main.py::_auto_apply_all` already surfaces
`raw["permalink"]` generically, so name it `permalink` to get that Telegram
notification line for free).

### 2.2 Outbound-link extraction

From the post's HTML (anchors inside `.tgme_widget_message_text`):
- collect `href`s; drop `t.me`/`telegram.me`/`tg://` links, `mailto:`, bare hashtag
  and mention links;
- Telegram wraps external links in its own redirect sometimes — on the `/s/` preview
  they are plain hrefs; still, strip known wrappers if seen in fixtures;
- take the **first** surviving http(s) link (aggregator posts put the apply link
  first/only). If several, first wins — precision over cleverness;
- run through `clean_url()` (strips utm/tracking).

No HEAD-request resolution of shorteners in v1 (adds network cost at search time for
little gain — none of the probed channels used shorteners). If a fixture shows one,
note it and still take the URL as-is; the apply-time fetch follows redirects anyway.

### 2.3 Fetch path — uniform, NO paste flow

For permalink-URL jobs implement `fetch_text(url)` via the single-post embed page:
`GET https://t.me/{channel}/{msg_id}?embed=1&mode=tme`, parse the same
`.tgme_widget_message_text` markup (one shared parser with `search()`). Raise on
empty/deleted post (caller's normal FAIL/retry machinery handles it).

**Do NOT set `job.raw["post_text"]`.** That key triggers the scout-relay paste flow in
`hunter/services/apply_service.py:63` (generic presence check!) and would bypass the
normal fetch. The scout needed it only because LinkedIn feed posts have no fetchable
URL; Telegram posts have one. Riding the normal fetch path means retries of FAILed
jobs work (`tracker.get_failed_jobs` rebuilds a bare Job — scout bug #142 does not
apply here), `expired_marker` can re-check rows, and `url_message.py`'s
scout-specific branch (line ~82, gated on `job.source == "linkedin_scout_relay"`)
stays untouched.

`matches_url`: claim URLs whose host is `t.me` or `telegram.me` (both
`t.me/{ch}/{id}` and `t.me/s/{ch}/{id}` forms; ignore query). No existing source
claims these hosts. Register in BOTH `ALL_SOURCES` and `_fetch_roster()`
(`hunter/sources/__init__.py` — roster is independent of the ENABLED flag). Bonus:
the owner can manually paste any `t.me/...` post URL into the bot and it will fetch.

### 2.4 Title synthesis — critical, the central filter checks title only

`filters.classify_job` enforces `title_keywords` (and `require_angular` when on)
against **`job.title` only** (`hunter/filters.py:62-71` + `:713`). Free-text posts
have no title field.

Rule: `title` = first non-empty text line of the post, trimmed to 90 chars. If the
source-level prefilter matched a tech keyword that is NOT in that first line, append
it: `f"{first_line} · {matched_kw}"`. This keeps the central whitelist honest without
bypassing it (the gmail title-bypass was removed for cause — see
`apply_filters_with_stats` docstring).

Other Job fields:
- `company`: best-effort — aggregator posts often have a `... @ Company` or
  `Company |` token near the top; if extraction is not trivially reliable, use the
  channel name (e.g. `@findmyremote_frontend`). The LLM extracts the real company at
  generation time, same as gmail stubs. Do not over-engineer.
- `location`: `"Remote"` if a remote token appears in the text (EN/PL/RU:
  remote/zdaln/удалёнк/удаленн/дистанционн), else `""` (scout convention).
- `salary`: `None` (whatever is in the text reaches the LLM anyway).
- `source`: `"telegram_channels"`.

### 2.5 Source-level prefilter (cheap, before central filters)

The `/s/` page returns ~20 latest posts per channel; most are noise. Per post, in
order:
1. skip posts with no text (media-only, service messages);
2. must contain a `FILTER["title_keywords"]` keyword anywhere in the text — reuse
   `BaseSource.matches_coarse_prefilter(title, context_text=full_text)` (it already
   applies exclude_patterns too);
3. must contain a hiring signal (EN/PL/RU). Adapt the pattern families from
   `linkedin_scout/heuristics.py` (hiring: ищем/требуется/вакансия/hiring/looking
   for/zatrudnimy/we're hiring/apply; **vendor a small pattern set into the new
   module — do NOT import `linkedin_scout`**, that package is leaving the repo per
   docs/SCOUT_REPO_SPLIT_PLAN.md). For `board`-kind channels where EVERY post is a
   vacancy (e.g. `findmyremote_frontend`), the hiring-signal check may be skipped —
   make it conditional on `kind == "authored"` to avoid dropping terse board posts
   like "Senior Frontend Engineer @ X | Remote | <link>";
4. negative signals: candidate-side posts (ищу работу/open to work/szukam pracy —
   note `szukam`≠`szukamy`), courses/webinars/bootcamps (курс/вебинар/буткемп/
   webinar).

Everything that passes goes through central filters + doomed gate + tracker dedup —
same "no human card, the pipeline is the gate" contract as the scout relay (owner
decision 2026-07-08). `manual_only` stays `False`.

### 2.6 Short-post floor

`validation.MIN_JOB_TEXT_LEN=300` would reject legit short posts fetched via the
embed page (same issue as scout posts, #143). Extend
`validation.min_job_text_len_for(url)` with `TELEGRAM_POST_URL_MARKER = "//t.me/"`
→ reuse `MIN_SCOUT_TEXT_LEN` (80). `validation.py` stays a leaf module (string
constant only, no imports). External-link jobs keep the normal 300 floor
automatically (their URL isn't t.me). Add a drift-guard test asserting the marker is
a substring of permalinks the source actually produces (same pattern as the scout
marker test).

### 2.7 Cyrillic guard — ships BEFORE the source goes live (M3)

RU-language posts create a real gap: the ATS keyword loop mirrors posting keywords
verbatim into `resume_en`, and `hunter/lang_guard.py` detects only Polish-in-English.
**Cyrillic injected into `resume_en` would pass every gate today.**

Fix in `lang_guard`: any Cyrillic codepoint (`[Ѐ-ӿ]`) in an `_en`/`_pl`
field ⇒ contamination finding, surfaced through the existing
`enforce_language_separation` scan/repair/block shape in `apply_shared`
(repair: strip/translate offending tokens via the existing `TRANSLATE_*` path; block
if strong contamination survives — same contract as Polish-in-EN). No allowlist
needed — no legitimate Cyrillic ever belongs in `_en`/`_pl` fields.

Also: `detect_posting_language` knows only PL/EN; a RU posting detects as EN and
produces EN docs. That is CORRECT behavior (an EN CV is the right artifact; we are
not adding RU CV generation) — just don't "fix" it.

### 2.8 What we deliberately do NOT build

- **LLM extraction step** (freehire's `tg-extract`): at our volume the doomed gate +
  generation LLM already read the full text; a separate extraction model changes no
  real decision (owner's standing rule — no speculative LLM layers). Multi-vacancy
  `authored` digests: take the post if it passes the prefilter; if generation
  targets the wrong role in practice, drop those channels from the JSON.
- **MTProto/Telethon reader**: `t.me/s/` suffices; revisit only if Telegram blocks
  the preview.
- **Per-channel seen-store**: tracker URL dedup + central filters make re-listing
  the same ~20 posts each cycle a no-op, like every board source.
- **RU CV generation / RU `primary_lang`.**
- **Auto-discovery of channels** — the JSON is owner-curated.

### 2.9 Config

Follow the `ats_companies.json` precedent (JSON, not YAML — no new dependency):

- `telegram_channels.json` in repo root:
  `[{"channel": "findmyremote_frontend", "kind": "board", "note": "frontend aggregator, daily"}, ...]`
- `hunter/config.py`: `TELEGRAM_CHANNELS_ENABLED` (default `true`),
  `TELEGRAM_CHANNELS_FILE` (default repo-root JSON), `TELEGRAM_CHANNELS_DELAY_SEC`
  (default `1.5` — polite pause between channel fetches).
- `.env.example` block.
- Requests: plain `requests` with a UA + 15s timeout; no cloudscraper needed.

---

## 3. New/changed files

| File | Change |
|---|---|
| `hunter/sources/telegram_channels.py` | NEW — `TelegramChannelsSource(BaseSource)`: `search()`, `matches_url()`, `fetch_text()` (embed page), shared widget-HTML parser, outbound-link extraction, EN/PL/RU prefilter patterns (vendored), channel-list loader |
| `telegram_channels.json` | NEW — starter channel list (§6) |
| `hunter/config.py` | `TELEGRAM_CHANNELS_ENABLED` / `_FILE` / `_DELAY_SEC` |
| `hunter/sources/__init__.py` | register in `ALL_SOURCES` + `_fetch_roster()` (22→23) |
| `hunter/validation.py` | `TELEGRAM_POST_URL_MARKER` + floor in `min_job_text_len_for` |
| `hunter/lang_guard.py` | Cyrillic-in-EN/PL contamination detection (M3) |
| `hunter/apply_shared.py` | enforce-gate picks up the new contamination kind (aim for zero/minimal change — extend the scan result lang_guard already returns) |
| `.env.example` | new config block |
| `CLAUDE.md` | sources table 22→23, config table, repo layout, Scraper Health row, Agent Work Log entry (final commit) |
| `tests/test_telegram_channels_source.py` | NEW (§5) |
| `tests/fixtures/telegram_channels/` | NEW — saved real `t.me/s/` + `?embed=1` HTML |
| existing fetch-roster-count tests | 22→23 fixups (grep for the count — the scout-relay PR did the same 21→22) |

---

## 4. Milestones (one commit each, tests in the same commit)

### M1 — Parser + prefilter + Source skeleton (no registration yet)
- Save real fixtures (sanitize personal data): a `t.me/s/findmyremote_frontend`-style
  page (posts WITH outbound links), a text-only channel page, one media-only post,
  one long post (verify whether `/s/` truncates — §7.2), one `?embed=1` single-post
  page, one deleted/empty post page.
- `_parse_channel_page(html) -> list[TgPost]` where `TgPost` = `msg_id`, `permalink`,
  `text` (with `<br>` → `\n` — first line matters for title synthesis), `links`
  (external hrefs, §2.2), `has_text`. Parse `.tgme_widget_message` /
  `.tgme_widget_message_text` blocks with BeautifulSoup.
- Prefilter (§2.5) + title synthesis (§2.4) + Job assembly (§2.1) in `search()`:
  iterate channels from JSON, GET with UA/timeout/delay, per-channel errors logged
  and swallowed (return what other channels yielded — BaseSource contract).
- **Acceptance:** fixture tests green — parser (splitting, permalink, links,
  br-handling, media-only skip), prefilter (RU/EN/PL positives; candidate-side,
  course, no-keyword, exclude-pattern negatives; board-kind skips hiring-signal),
  title synthesis (keyword-in-first-line / keyword-appended / 90-char cap),
  URL choice (external link wins, permalink fallback, clean_url applied).

### M2 — Fetch path + registration + validation floor
- `matches_url` + `fetch_text` (embed page; raises on empty/deleted).
- `validation` marker + floor + drift-guard test.
- Register in `ALL_SOURCES` + `_fetch_roster()`; config + `.env.example`; fix
  roster-count tests 22→23.
- **Acceptance:** full suite green; unit test proves `fetch_job_text("https://t.me/...")`
  dispatches to the new source (mocked HTTP), and that a non-t.me external-link job
  is NOT claimed by it.

### M3 — Cyrillic guard (blocker for AUTO_APPLY exposure)
- `lang_guard` Cyrillic detection + wiring through `enforce_language_separation`
  (§2.7).
- **Acceptance:** unit tests — RU keyword injected into `resume_en` is repaired via
  the translate path; wholly-Cyrillic field blocks delivery; clean EN/PL content
  produces zero findings (run the existing lang_guard test corpus through the new
  check to prove no false positives).

### M4 — Live verification + calibration (dev machine, real HTTP — not in tests)
- Run `search()` live against the starter channels; record per-channel: posts seen,
  prefilter pass-rate, central-filter survivors, how many carry external links.
- ONE full apply end-to-end on one real relevant post (real LLM spend, single run):
  fetch → doomed gate → generation → verdict. Use `/debug_url` or paste the URL.
- Trim/extend `telegram_channels.json` from results. Expect most RU channels at 0 —
  fine; `source_health` shows them IDLE, not BROKEN (never `ever_positive`).
- **Acceptance:** findings appended to this doc (§9 Calibration log); Scraper Health
  row drafted.

### M5 — Docs + wrap-up
- CLAUDE.md updates (§3 table); Agent Work Log entry; `ruff` + `compileall` + full
  `pytest`.
- PR to `master` (repo's current flow — see recent PRs #146/#147).

---

## 5. Test plan (all fixture/mock, no live HTTP)

- **Parser:** post splitting, permalink/msg_id extraction, `<br>`→newline, links
  extraction (t.me/mailto/hashtag dropped, first external kept), media-only posts,
  emoji survive.
- **Prefilter:** RU ("Ищем Angular-разработчика, удалёнка"), EN ("We're hiring a
  Senior Frontend (Angular)"), PL positives; negatives: "ищу работу", "вебинар по
  Angular", keyword-less posts, ".NET Developer" (exclude_pattern); `board` kind
  passes terse "Senior Frontend @ X | Remote | link".
- **Job contract:** external-link URL preferred + cleaned; permalink fallback;
  `raw["permalink"]` always set; location Remote/"" logic; source name.
- **fetch_text:** embed fixture → full text; deleted-post fixture → raises.
- **Registration/dispatch:** roster count 23; `matches_url` claims t.me forms and
  nothing else; `fetch_job_text` dispatch.
- **Validation floor:** t.me URLs → 80; external URLs → 300; drift-guard marker⊂permalink.
- **lang_guard:** repair / block / zero false positives on existing corpus.

---

## 6. Starter channel list (owner-curated; flipped vs freehire after live probe §1.2)

| channel | kind | expectation |
|---|---|---|
| `findmyremote_frontend` | board | **primary** — daily frontend roles incl. Angular/EU, outbound links |
| `rabotafrontend` | board | frontend-specific, RU-language |
| `IT_job_Poland` | board | PL-market mixed bag, occasional FE |
| `Remoteit` | board | RU remote experiment — prune if 0 after 2-3 weeks |
| `it_vakansii_jobs` | board | RU general experiment — prune if 0 |

Judge by `/funnel` + `/health` after 2-3 weeks; prune freely. Follow-up worth more
than more RU channels: find 3-5 additional Polish/EU tech channels (absent from
freehire's list entirely).

---

## 7. Risks / gotchas for the implementer

1. **`/s/` preview can be disabled per-channel** (page renders a "Preview channel"
   stub, zero `.tgme_widget_message` blocks). Must not crash: log + yield 0 for that
   channel (`record_run` in the hunt loop then records ok/0). Verify each starter
   channel actually exposes `/s/` during M4.
2. **Long-post truncation on `/s/`:** check in M1 fixtures whether the preview
   carries full text (believed yes). Even if truncated, prefilter on truncated text
   is fine (signal is in the first lines) and apply-time fetch uses the embed page /
   external link.
3. **`url_message.py:82` / `apply_service.py:63`:** do not set `raw["post_text"]`
   (§2.3) or you silently reroute applies through the paste flow.
4. **Title filter is title-only** (`filters.py:713`): skipping title synthesis §2.4
   makes every job die at `title_kw` and the source will look "broken" while being
   merely mis-titled.
5. **Doomed gate on RU text:** HARD rules are EN/PL-calibrated; "офис в Москве"
   won't trip them. v1 accepts this (RU onsite roles mostly die at the central
   location/title filters anyway); do NOT attempt a RU onsite rule without
   calibration data — note real examples in M4 findings instead.
6. **Funnel attribution:** external-link jobs attribute to the link's domain (or
   another source via `matches_url`) in `/funnel`, not to `telegram_channels`.
   Accepted for v1 — do not add a source column to the tracker for this.
7. **Rate limits:** none expected (freehire saw none). Keep the delay + UA anyway;
   5 channels × 3 cycles/day is negligible.
8. **Windows/encoding:** posts are full of emoji/Cyrillic — read/write fixtures with
   explicit `encoding="utf-8"` (repo convention; cp1252 crashes happened before).

---

## 8. Explicitly out of scope (v1)

- MTProto/Telethon; LLM per-post extraction; RU CV generation; channel
  auto-discovery; shortener resolution at search time; any Telegram-side posting.

## 9. Calibration log (fill in M4)

_(empty — M4 appends per-channel yield + apply run findings here)_
