# LinkedIn Posts Scout

Standalone script that scouts LinkedIn for Angular hiring posts made as ordinary
feed content (not LinkedIn Jobs listings). Runs on **your own desktop**, on **your
residential IP**, via **Windows Task Scheduler** — the SCRAPING is intentionally NOT
part of `hunter/`, NOT in the Docker image, and NOT on the bot's schedule. Full design
rationale: `../docs/LINKEDIN_POSTS_SCOUT_TASK.md`.

**It does not send Telegram messages as itself** (owner decision 2026-07-08 — "this is
just another job source, like the other 21"). Instead, once it finds a candidate, it
relays it to the bot as a `/scoutfound <payload>` command sent through **your own
Telegram user session** (Telethon/MTProto — see below for why), which `hunter/commands/
scoutfound.py` receives and queues into `hunter/sources/linkedin_scout_relay.py` (a
tiny, scrape-free source inside the bot). That source drains the queue on the bot's own
hunt cycle and turns each candidate into a normal Job — same central filters, same
doomed-vacancy gate, same tracker dedup, same `AUTO_APPLY` handling as any other source
(owner: "we dropped confirmation cards long ago, I never wait for them — there's
already a full check pipeline other job-board postings go through, I want these to go
through it too"). A HARD doomed-gate finding still aborts generation for $0.00, same as
any other source; it just doesn't require a human to look at a card first. Apply
(whichever code path runs) uses the saved post text automatically via the paste flow —
no manual re-paste needed; `job.url` stays a synthetic dedup key (never a real
LinkedIn URL, to avoid colliding with `LinkedInSource`'s host-based URL dispatch).

**Post permalinks** (owner discovery 2026-07-08, live-verified — an earlier probe found
none reachable, which turned out to be wrong): some posts (LinkedIn "share"-type, at
least) wrap their body text in a real `<a href="https://www.linkedin.com/feed/update/
urn:li:share:...">` already present in the DOM, no extra click needed. `browser.py`'s
extraction JS captures it when present. LinkedIn also exposes a `Copy link to post` item
in every post's `...` menu — works on every post, not just share-type ones — so for any
M1 candidate that didn't get a DOM-marker permalink, `browser._fetch_menu_permalinks()`
clicks through `...` → `Copy link to post` and reads the clipboard (capped at
`_MAX_MENU_PERMALINK_ATTEMPTS` per run — clicking is slower and adds anti-bot surface,
so it's only spent on posts that already passed the hiring-post + location gate, not
every post on the page). Either source threads through as `permalink` on
`ScoutCandidate` → the Telegram relay payload → `job.raw["permalink"]` on the bot side
(best-effort, `None` when neither source found one). It's convenience-only — never used
for dedup/fetch/routing — and shows up as an extra line in the pre-apply Telegram
notification (`hunter/main.py::_auto_apply_all`) so you can click through to the actual
post if you want to. The exact `...`-menu selectors are best-effort and unverified
against a live session (same caveat as the rest of this module's DOM assumptions) — a
failed lookup just skips that candidate's permalink, it never blocks the run.

**Why a Telegram command instead of a shared file:** an earlier version of this wrote
matches directly to a local JSON file the bot's source would read. That broke the
moment it became clear the bot auto-deploys to its own server and does NOT share a
filesystem with this script's Windows desktop — the bot could never see a file written
locally here. **Why a user session instead of the bot's own token:** Telegram never
delivers a bot's own outgoing `sendMessage` calls back to that same bot as an incoming
update, so there is no way to make the bot's polling `Application` react to something
it sent to itself. The command has to come from a genuinely different account — the
owner's own — which requires a real Telegram *user* login (Telethon/MTProto), not just
the bot token.

---

## Prerequisites

1. **A saved LinkedIn session.** From the repo root:
   ```
   python tools/linkedin_login.py
   ```
   This opens a real browser window, you log in manually (incl. 2FA), then it
   saves a `storage_state.json`. Point `.env`'s `LINKEDIN_STORAGE_STATE` at that
   path — this scout reuses the exact same variable as the main bot's LinkedIn
   detail-page fetcher.

2. **Real Google Chrome installed** (not just Playwright's bundled Chromium —
   the scout launches `channel="chrome"` specifically, since a bare Chromium
   fingerprint is one of the things that got a probe session flagged). Playwright
   itself must also be installed: `pip install playwright` (already in
   `requirements.txt`).

3. **A saved Telegram USER session** (this is the part that's different from the
   main bot's setup — read carefully):
   - Get `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` from https://my.telegram.org
     (log in with your own phone number → API development tools → create an app
     if you don't have one). These identify an *application*, not a bot.
   - Set `TELEGRAM_BOT_USERNAME` in `.env` to the bot's own `@username` (the
     send target).
   - Run `python tools/telegram_user_login.py` — interactive: your phone
     number, the login code Telegram sends you, and your 2FA password if you
     have one set. Saves a session file (path in `.env`'s
     `TELEGRAM_USER_SESSION`).
   - **Security note:** this session file grants full access to YOUR OWN
     Telegram account (read/send messages as you) to whoever holds it — treat
     it exactly like a password, same caution as `.secrets/
     linkedin_storage_state.json`. It never leaves this machine and is
     git-ignored.

4. Optional tuning in `.env` (see `.env.example` for the full block):
   `LINKEDIN_SCOUT_KEYWORDS`, `LINKEDIN_SCOUT_SKIP_CHANCE`,
   `LINKEDIN_SCOUT_JITTER_MAX_MIN`.

---

## Manual verification before scheduling anything

```
# 1. No browser at all — proves the parsing/filtering logic end-to-end against
#    a bundled fixture. Should print 2 matches, no errors.
python linkedin_scout/run.py --dry-run

# 2. A real run, once, watched — skip the skip-chance/jitter wait so you see
#    it immediately. A visible Chrome window should open (not headless).
python linkedin_scout/run.py --track search --no-jitter

# 3. Same for the feed track.
python linkedin_scout/run.py --track feed --no-jitter
```

If either real run reports "circuit breaker tripped" in the log, LinkedIn threw
back a login/checkpoint page or an anti-bot interstitial. Do NOT retry
immediately — re-run `tools/linkedin_login.py` to refresh the session, then:
```
python linkedin_scout/run.py --track search --reset
python linkedin_scout/run.py --track feed --reset
# or reset both at once:
python linkedin_scout/run.py --reset
```

Only register the Task Scheduler entries below once a manual `--no-jitter` run
of both tracks has gone cleanly.

---

## Windows Task Scheduler registration

The two tracks have deliberately DIFFERENT cadences and window behavior
(owner decisions 2026-07-07/08) — they are not symmetric, don't copy one
schedule/setup to the other:

- **`search`**: hourly, all day, WITH the built-in ~30% skip chance + 0-45min
  jitter (see Safety rails). A single run is a few seconds (3 quick scrolls),
  so even with jitter there's negligible risk of two runs overlapping. To
  avoid an hourly Chrome window stealing focus while you're working,
  `scout_keyword()` launches it off-screen (`--window-position=-3000,0`) —
  still a real, fully-rendered headed window (not headless, not minimized),
  just positioned outside the visible desktop area.
- **`feed`**: NOT hourly all day — three separate tasks: hourly overnight
  (03:00-08:00, six runs) plus two extra runs at 13:00 and 18:00. Each run
  scrolls the plain feed for up to ~10 minutes at a slow, randomized pace
  (`_FEED_SCROLL_MAX_DURATION_SEC`), stopping early once the feed plateaus
  (`_FEED_SCROLL_PLATEAU_LIMIT`). Always `--no-jitter` (an already-fixed
  hourly/daily cadence gains nothing from jitter and risks overlap) and
  intentionally left ON-SCREEN, not off-screen like search — a long session
  moved off-screen risks Chrome treating it as occluded/backgrounded and
  throttling the lazy-loaded content (see Safety rails).

Replace `D:\LearningProject\Claude` and the `python.exe` path with your own;
find your Python path with `(Get-Command python).Source` first.

```powershell
# search — hourly, all day, with jitter + off-screen window
schtasks /create /tn "LinkedInScout-Search" ^
  /tr "\"C:\Path\To\python.exe\" \"D:\LearningProject\Claude\linkedin_scout\run.py\" --track search" ^
  /sc hourly /mo 1 /st 00:00 ^
  /ru "%USERNAME%" /rl LIMITED

# feed — hourly ONLY overnight (03:00..08:00 inclusive, 6 runs), no jitter
schtasks /create /tn "LinkedInScout-Feed-Night" ^
  /tr "\"C:\Path\To\python.exe\" \"D:\LearningProject\Claude\linkedin_scout\run.py\" --track feed --no-jitter" ^
  /sc daily /st 03:00 /ri 60 /du 0006:00 ^
  /ru "%USERNAME%" /rl LIMITED

# feed — two extra daytime runs
schtasks /create /tn "LinkedInScout-Feed-Day1" ^
  /tr "\"C:\Path\To\python.exe\" \"D:\LearningProject\Claude\linkedin_scout\run.py\" --track feed --no-jitter" ^
  /sc daily /st 13:00 ^
  /ru "%USERNAME%" /rl LIMITED

schtasks /create /tn "LinkedInScout-Feed-Day2" ^
  /tr "\"C:\Path\To\python.exe\" \"D:\LearningProject\Claude\linkedin_scout\run.py\" --track feed --no-jitter" ^
  /sc daily /st 18:00 ^
  /ru "%USERNAME%" /rl LIMITED
```

Flag-by-flag:
- `/tn "<name>"` — task name shown in Task Scheduler's UI.
- `/tr "<command>"` — the command line to run. Quote the executable and the
  script path separately since both may contain spaces.
- `/sc hourly /mo 1 /st 00:00` — (search track) fires every 1 hour, all day,
  starting from midnight (`/mo` is the hour-interval modifier for `/sc
  hourly`, not a day count).
- `/sc daily /st 03:00 /ri 60 /du 0006:00` — (feed night task) a single daily
  trigger at 03:00 that then REPEATS every 60 minutes (`/ri`) for a 6-hour
  DURATION (`/du HHHH:MM`) — i.e. 03:00, 04:00, 05:00, 06:00, 07:00, 08:00,
  then stops until the next day's 03:00 trigger. `schtasks` has no plain
  "/sc hourly between these two clock times" option, so this `/ri`+`/du`
  combination on a daily trigger is the standard way to get it.
- `/sc daily /st 13:00` / `/st 18:00` — (feed day tasks) one fixed trigger a
  day each — plain daily schedule, no repeat needed.
- `/ru "%USERNAME%"` — run as your own logged-in account (needed so the script
  can see your Chrome/session; a different service account would need its own
  login).
- `/rl LIMITED` — run with standard (not elevated/admin) privileges.

Every trigger above uses `--track` (never bare `run.py`), and the feed tasks
all pass `--no-jitter` — the search task does NOT pass `--no-jitter`, since it
keeps the skip-chance/jitter layer on purpose.

**An hourly search run plus 8 feed sessions a day (6 overnight + 2 daytime,
~10 minutes each) is a much larger standing time-on-page commitment than the
original few-times-a-week design.** This is a deliberate owner trade-off
(favoring volume/coverage over the original "look infrequent" posture) — the
circuit breaker, plateau early-stop, off-screen window (search), and
randomized scroll pace are what's carrying the safety burden now, not
schedule irregularity. Watch the first several runs' logs before leaving it
fully unattended for days.

To verify registration:
```
schtasks /query /tn "LinkedInScout-Search" /v /fo LIST
schtasks /query /tn "LinkedInScout-Feed-Night" /v /fo LIST
schtasks /query /tn "LinkedInScout-Feed-Day1" /v /fo LIST
schtasks /query /tn "LinkedInScout-Feed-Day2" /v /fo LIST
```

To remove a task:
```
schtasks /delete /tn "LinkedInScout-Search" /f
schtasks /delete /tn "LinkedInScout-Feed-Night" /f
schtasks /delete /tn "LinkedInScout-Feed-Day1" /f
schtasks /delete /tn "LinkedInScout-Feed-Day2" /f
```

**Task Scheduler must run under a session where the desktop is normally
available** (default Windows behavior when `/ru` is your own logged-in user) —
the scout launches a headed, visible Chrome window on purpose (headless got
flagged within 2-3 loads during the original live probe). If your machine is
locked/logged-out when a trigger fires, that run will fail to open a window;
Windows will just skip it, which is a safe failure mode here (no partial state).

---

## Safety rails — why a given run might silently do nothing

This is deliberate, not a bug. In order:

1. **~30% skip chance.** Every invocation first rolls dice; on a "skip" it just
   logs and exits — no browser, no network. Real humans don't check LinkedIn on
   a fixed cadence either. Tune via `LINKEDIN_SCOUT_SKIP_CHANCE` (0.0-1.0).
2. **0-45 minute jitter sleep (search track only).** If not skipped, it sleeps
   a random amount before opening the browser, so the Task Scheduler trigger
   time and the actual browser-open time never line up exactly. Tune via
   `LINKEDIN_SCOUT_JITTER_MAX_MIN`. The `feed` track's scheduled tasks always
   pass `--no-jitter` — its cadence is already fixed (hourly overnight / two
   fixed daytime triggers) and jitter there would risk two runs overlapping.
3. **Off-screen window (search track only).** `scout_keyword()` launches its
   (still real, headed) Chrome window at `--window-position=-3000,0` — off
   the visible desktop — so an hourly search run doesn't steal focus from
   whatever you're doing. The `feed` track is intentionally left on-screen
   instead (see next point).
4. **Feed-track scroll budget + plateau stop.** The `feed` track scrolls for
   up to `_FEED_SCROLL_MAX_DURATION_SEC` (~10 minutes) at a randomized pace
   and distance per scroll — but stops as soon as `_FEED_SCROLL_PLATEAU_LIMIT`
   consecutive scrolls surface no NEW posts (the feed ran dry; no point
   continuing). A run finishing in well under 10 minutes with a short log is
   this working as intended, not a bug. It stays on-screen (not moved
   off-screen like search) because a long session sitting off-screen risks
   Chrome treating the window as occluded/backgrounded and throttling the
   lazy-loaded content — you'd get fewer posts for no safety benefit. Just
   don't minimize the window while it's running (see the section above on
   switching windows).
5. **Circuit breaker (non-negotiable).** The instant a run sees a
   login/checkpoint/authwall redirect or a known anti-bot interstitial marker
   (captcha, `protechts.net`, "verify you're a human", …), it aborts
   immediately — no retries, ever — and writes a "tripped" flag to that
   track's state file (`search_state.json` / `feed_state.json`). It sends
   you exactly ONE Telegram alert on the run that actually trips it. Every
   subsequent run of that track silently no-ops (logs, exits 0) until you run
   `--reset`. The other track is unaffected (separate state file).
6. **Every keyword, every run, in random order (search track only, owner
   decision 2026-07-08).** `run.py --track search` searches the ENTIRE
   `LINKEDIN_SCOUT_KEYWORDS` list in one invocation, in a freshly shuffled
   order each time (not the same sequence every run) — a 10-30s jittered
   pause between each keyword's search, the circuit breaker still stops the
   whole run immediately (no further keywords attempted) if any single
   keyword's search trips it. This replaced the original "one rotation-
   keyword per run, full coverage over several days" design — a deliberate
   trade-off toward speed/coverage over looking
   infrequent, on top of the hourly cadence from point 1 above.
7. **Headed real Chrome only.** Never run with `--headless` except for your own
   local debugging with no live LinkedIn navigation — the spec's live probe
   showed headless Chromium gets flagged within 2-3 page loads; a visible,
   real-Chrome window with stealth flags survived multiple consecutive runs.

If Telegram goes quiet for a while, check the log for "circuit breaker is
tripped" before assuming there's just nothing to report — the difference matters
because a tripped session also affects the main bot's shared
`LINKEDIN_STORAGE_STATE` LinkedIn detail-page fetches, so a Telegram alert here
is worth acting on promptly (re-run `tools/linkedin_login.py`, then `--reset`).

---

## Files this script creates (git-ignored, never committed)

- `linkedin_scout/.profile_search/`, `linkedin_scout/.profile_feed/` — persistent
  Chrome profile directories, one per track.
- `linkedin_scout/search_state.json`, `linkedin_scout/feed_state.json` — circuit
  breaker + keyword rotation state, one per track.
- `linkedin_scout/seen_posts.json` — dedup store on THIS machine (prevents relaying
  the same post twice across scout runs).
- `.secrets/telegram_user_session.session` — your Telegram user login (Telethon).

Note: `pending_candidates.json` (the actual relay queue) now lives on the BOT's
own machine, not here — see `hunter/sources/linkedin_scout_relay.py` in the main
repo. There is nothing to clean up for it on this side.

Deleting any of these is safe (loses history/rotation-position/dedup memory, not
your LinkedIn login — that always comes from `LINKEDIN_STORAGE_STATE`).

---

## Superseded design

An earlier plan ran this scraper INSIDE the bot's Docker container on the
production server (`docs/LINKEDIN_POSTS_SOURCE_PLAN.md`, branch
`feat/linkedin-posts-source`, PR #114). That approach is superseded by this
standalone desktop script — see "Why standalone" above and the "LinkedIn Posts
Scout" section of the root `CLAUDE.md` for the full reasoning. This note lives
here (not as an edit to that other branch's plan document, which this change
does not touch) per the task spec's fallback option. PR #114 was NOT closed or
merged as part of this change — that decision is left for the owner.
