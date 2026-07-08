# Plan: Move `linkedin_scout/` to Its Own Private Repository

**Status:** Phase 0 DONE (2026-07-08, branch refactor/scout-decouple-phase0) — Phases 1-4 pending.
**Owner action required:** create the private GitHub repo, desktop cutover (Phase 2).

---

## 1. Motivation

1. **This repo is going public** (docs/PUBLIC_RELEASE_CHECKLIST.md). `linkedin_scout/`
   is a LinkedIn scraper with stealth flags, shadow-DOM extraction and an anti-bot
   circuit breaker — publishing it under the owner's name, in a repo tied to the
   same LinkedIn account it scrapes with, is a ToS/reputational liability. It must
   stay private regardless of anything else.
2. **Different machine, different lifecycle.** The scout runs on the owner's
   Windows desktop via Task Scheduler; the bot auto-deploys to its own server on
   every merge. Today a scout-only change pointlessly triggers a bot redeploy, and
   the desktop carries a full checkout of the whole project for one folder.
3. **The coupling is already thin.** The only bridge between the two machines is
   the `/scoutfound <base64(json)>` Telegram command (Telethon, owner's user
   session). Code-level imports from `hunter` are exactly four lines (see §3).

The receiving side (`hunter/commands/scoutfound.py` +
`hunter/sources/linkedin_scout_relay.py`) does zero scraping and **stays in this
repo** — it is part of the bot.

---

## 2. Target state

```
igrdevelop/<main repo>  (public)          igrdevelop/linkedin-scout  (PRIVATE)
  hunter/commands/scoutfound.py             linkedin_scout/   (package, same name)
  hunter/sources/linkedin_scout_relay.py    tests/            (7 scout test files)
  tests/test_scoutfound_command.py          tests/fixtures/shadow_dom/
  tests/test_linkedin_scout_relay_source.py tools/telegram_user_login.py
  tests/fixtures/scout_payload_v1.json      tools/linkedin_login.py   (copy)
        ▲                                   tests/fixtures/scout_payload_v1.json
        │            Telegram                     │
        └── /scoutfound  ◄────────  telegram_relay.send_candidates()
             (payload contract v1 — §5)
```

---

## 3. Inventory

### Moves to the new repo
| Item | Notes |
|---|---|
| `linkedin_scout/` (9 modules + README.md) | package keeps its name → schtasks command changes are path-only |
| `tests/test_linkedin_scout.py` | heuristics/parser/seen_store |
| `tests/test_linkedin_scout_browser.py` | |
| `tests/test_linkedin_scout_extract_integration.py` | real-Chrome; auto-skips without Chrome |
| `tests/test_linkedin_scout_notify.py` | |
| `tests/test_linkedin_scout_run.py` | |
| `tests/test_linkedin_scout_state.py` | |
| `tests/test_linkedin_scout_telegram_relay.py` | |
| `tests/fixtures/linkedin_scout/shadow_dom/` (incl. `infinite_scroll.html`) | |
| `tools/telegram_user_login.py` | Telethon login — only the scout uses it |

### Copied (exists in both repos afterwards)
| Item | Why |
|---|---|
| `tools/linkedin_login.py` | creates `LINKEDIN_STORAGE_STATE`; the bot's own LinkedIn detail fetches also need it, so the original stays here |
| `tests/fixtures/scout_payload_v1.json` (new, §5) | golden contract fixture, tested on both sides |

### Stays in this repo (unchanged)
- `hunter/commands/scoutfound.py`, `hunter/sources/linkedin_scout_relay.py`,
  `LINKEDIN_SCOUT_RELAY_ENABLED` in config, `BaseSource.manual_only` machinery,
  the paste-flow wiring in `apply_service.py` / `url_message.py`.
- `tests/test_scoutfound_command.py`, `tests/test_linkedin_scout_relay_source.py`.
- `QUEUE_PATH = PROJECT_DIR / "linkedin_scout" / "pending_candidates.json"` —
  `append_to_queue()` already `mkdir(parents=True)`s, so it keeps working after the
  package dir is deleted. Optional cosmetic rename to `data/scout_queue.json`
  later; not part of this plan (zero-risk to leave as is).

### Runtime state on the desktop (NOT in git, must be carried over in Phase 2)
All live inside the current checkout's `linkedin_scout/` dir (`run.py:72-76`):
`.profile_search/`, `.profile_feed/` (persistent Chrome profiles — **losing them
changes the browser fingerprint and drops the session**, the single most
anti-bot-sensitive asset), `search_state.json`, `feed_state.json`,
`seen_posts.json` (losing it → re-relay of old posts; bot-side tracker dedup
absorbs it, but noisy).

---

## 4. Decoupling the four `hunter` imports

Do this **in this repo first** (Phase 0), while one test suite still proves
everything — the split then becomes a pure file move.

1. `linkedin_scout/notify.py:14` and `browser.py:50` —
   `from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID`
   → new `linkedin_scout/config.py`: reads `.env` (repo root) / `os.environ`
   directly for `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. ~20 lines, no deps
   (or `python-dotenv`, already transitively available).
2. `linkedin_scout/heuristics.py:18-19` —
   `from hunter.filters import _is_unwanted_onsite_location` + `hunter.models.Job`
   → vendor a simplified copy as `linkedin_scout/location_gate.py`: the
   anti-hybrid city list, the fully-remote veto regexes, the 120-char proximity
   window, the Wrocław veto. Drop the `Job` construction entirely (take plain
   text). **Why vendoring is safe:** the scout's gate is only a noise filter —
   every relayed candidate still goes through the bot's central filters + the
   doomed-vacancy gate, which remain the single authoritative screen. Drift in
   the vendored city list costs a few wasted relays, never a wrong application.
   Mark the copy with its origin (`hunter/filters.py::_is_unwanted_onsite_location`)
   and re-sync opportunistically.

Acceptance for Phase 0: `grep -r "from hunter" linkedin_scout/` → empty;
full suite green.

---

## 5. Payload contract v1 (the one real split risk)

`telegram_relay.build_payload()` (scout) and `cmd_scoutfound`'s decoder (bot) are
today tested in one suite; after the split the schema lives in two repos and can
drift silently. Mitigation, done in Phase 0 while still atomic:

1. Add `"v": 1` to the payload dict in `build_payload()`.
2. Decoder in `scoutfound.py`: unknown keys already ignored; add an explicit
   check — `v` present and `> 1` → reject with a clear Telegram error
   ("scout payload v{n} not supported, update the bot"); `v` absent → treat as 1
   (backward compatible with anything already queued).
3. New golden fixture `tests/fixtures/scout_payload_v1.json` (one full candidate:
   author, body, permalink, author_profile_url, v). Two tests:
   - scout side: `build_payload()` output for the fixture candidate round-trips
     to exactly the fixture JSON;
   - bot side: decoding the fixture yields the expected queue record.
   The fixture file is byte-identical in both repos; any schema change bumps `v`
   and updates both fixtures deliberately.

---

## 6. Migration phases

### Phase 0 — decouple in place (this repo, 1 PR) ✅ DONE (2026-07-08, PR TBD)
- [x] `linkedin_scout/config.py` (env-based) — rewire `notify.py`, `browser.py`.
- [x] `linkedin_scout/location_gate.py` (vendored) — rewire `heuristics.py`,
      drop `hunter.filters` / `hunter.models` imports; port the relevant
      `test_linkedin_scout.py` location cases to the vendored gate. (In
      practice no test edits were needed — the existing cases exercise
      `check_location()`'s public API, which is unchanged.)
- [x] Payload `v:1` + tolerant decoder + golden fixture + 2 contract tests (§5).
- [x] `grep -r "from hunter" linkedin_scout/` empty (real imports); `pytest`,
      `ruff`, `compileall` all green.

### Phase 1 — create the private repo
- [ ] Owner creates **private** `igrdevelop/linkedin-scout`.
- [ ] Scaffold: `pyproject.toml` (deps: `playwright`, `telethon`, `requests`;
      dev: `pytest`, `ruff`), `.gitignore` (`.profile_*/`, `*_state.json`,
      `seen_posts.json`, `pending_candidates.json`, `.env`, `storage_state*.json`,
      `__pycache__/`), `.env.example` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
      `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_USER_SESSION`,
      `TELEGRAM_BOT_USERNAME`, `LINKEDIN_STORAGE_STATE`,
      `LINKEDIN_SCOUT_KEYWORDS`, skip-chance/jitter vars per current README).
- [ ] Copy the §3 "moves" list (plain copy, fresh history — the old history stays
      in this repo until the optional Phase 4 scrub; nothing in it is secret).
      Promote `linkedin_scout/README.md` to the repo root README.
- [ ] Fix `run.py` root-relative paths (`_REPO_ROOT`) → new repo root for `.env`.
- [ ] CI: `ruff check .` + `pytest` (the real-Chrome integration test already
      auto-skips headless CI).

### Phase 2 — desktop cutover (owner's machine)
- [ ] Clone `linkedin-scout`, create venv, `pip install -e .`
      (no `playwright install` needed — the scout uses `channel="chrome"`,
      the system Chrome).
- [ ] **Stop/disable the 4 existing schtasks first**, make sure no scout Chrome
      is running, then copy runtime state from the old checkout's
      `linkedin_scout/` into the new one: `.profile_search/`, `.profile_feed/`,
      `search_state.json`, `feed_state.json`, `seen_posts.json`. Same drive
      preferred (Chrome profiles occasionally embed absolute paths).
- [ ] `.env` from `.env.example` + existing values.
- [ ] Re-register the 4 Task Scheduler entries (`LinkedInScout-Search`,
      `-Feed-Night`, `-Feed-Day1`, `-Feed-Day2`) with the new working directory /
      script path — commands per the repo README, only paths change.
- [ ] Verify: `--dry-run --no-jitter` on both tracks; then one live search run;
      confirm the bot logs `/scoutfound` receipt and the next hunt cycle drains
      the queue.

### Phase 3 — cleanup in this repo (1 PR, only after Phase 2 verified)
- [ ] Delete `linkedin_scout/`, the 7 scout test files,
      `tests/fixtures/linkedin_scout/`, `tools/telegram_user_login.py`.
- [ ] Remove `telethon` from requirements **iff** nothing else imports it
      (`grep -r telethon hunter/ tools/ tests/`).
- [ ] `.env.example`: drop scout-only vars (`LINKEDIN_SCOUT_KEYWORDS`,
      `TELEGRAM_API_ID/HASH/USER_SESSION/BOT_USERNAME`, skip/jitter); **keep**
      `LINKEDIN_STORAGE_STATE` (the bot's own LinkedIn fetches use it).
- [ ] CLAUDE.md: replace the "LinkedIn Posts Scout" section with a short
      "external private component" paragraph (repo link, payload contract v1,
      pointer to the golden fixture); keep the relay-source rows in the source
      table / config table; fix the stale `docs/LINKEDIN_POSTS_SCOUT_TASK.md`
      references (that file does not exist in this repo — referenced from
      CLAUDE.md and `heuristics.py`'s docstring but never committed).
- [ ] `pytest`, `ruff check .`, `compileall` — relay/scoutfound/contract tests
      must still pass untouched.

### Phase 4 (optional) — history scrub
Only needed if the scout should vanish from the *public* history too. Fold into
the already-planned `git filter-repo` pass for `prompts/`
(docs/PUBLIC_RELEASE_CHECKLIST.md §history): add
`--path linkedin_scout --path tools/telegram_user_login.py
--path-glob 'tests/test_linkedin_scout*' --path tests/fixtures/linkedin_scout
--invert-paths`. Same force-push caveats as the prompts scrub; do both in one
rewrite, not two.

---

## 7. Rollback

Until Phase 3 merges, this repo still contains a fully working scout — rollback
is: re-enable the old schtasks entries (old paths), disable the new ones. The
runtime state was *copied*, not moved, so the old checkout resumes where it left
off (minus whatever `seen_posts.json` entries the new checkout added — worst
case a few duplicate relays, absorbed by bot-side dedup).

## 8. Explicitly out of scope

- Any change to the bot-side relay behaviour, filters, or the doomed gate.
- `QUEUE_PATH` rename (works as is; cosmetic).
- Publishing the scout repo (it stays private, permanently).
