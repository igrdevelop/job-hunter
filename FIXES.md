# Fix Plan — May 2026

Errors found in `logs/hunter_errors.log` from 2026-05-19.

---

## FIX-01 — gsheets_state.json is a directory (CRITICAL)

**Error:**
```
[WARNING] hunter.gsheets_sync: gsheets_sync: could not read state file: [Errno 21] Is a directory: '/app/gsheets_state.json'
[WARNING] hunter.gsheets_sync: gsheets_sync: could not write state file: [Errno 21] Is a directory: '/app/gsheets_state.json'
```

**Cause:**  
Docker Volume created a directory at `/app/gsheets_state.json` instead of a file.  
This happens when `docker-compose.yml` mounts `./gsheets_state.json` but the file doesn't exist on the host yet — Docker auto-creates a directory.

**Fix:**
1. On server: stop container, remove the directory, create an empty file, restart
   ```bash
   docker stop job-hunter
   rm -rf ~/gsheets_state.json
   echo '{}' > ~/gsheets_state.json
   docker start job-hunter
   ```
2. In `docker-compose.yml`: add a comment warning about this + document in CLAUDE.md
3. In `gsheets_sync.py`: add a startup check — if `gsheets_state.json` is a directory, log a clear error with remediation instructions instead of silent WARNING

**Status:** [x] DONE — server fixed (rm dir, create file, docker compose up -d); code hardened in `gsheets_sync.py`; `deploy.yml` now runs `touch gsheets_state.json` before every deploy; `docker-compose.yml` has warning comment + `logs/` volume added.

---

## FIX-02 — Gmail token expired (invalid_grant)

**Error:**
```
[ERROR] hunter.sources.gmail: [gmail] Error: ('invalid_grant: Token has been expired or revoked.', ...)
```

**Cause:**  
`gmail_token.json` on the server is expired/revoked. Google revokes tokens after ~6 months of inactivity or if OAuth consent was revoked.

**Fix:**
1. Locally: re-run OAuth flow
   ```bash
   python tools/gmail_auth.py
   ```
2. Upload fresh `gmail_token.json` to server:
   ```bash
   scp gmail_token.json deploy@<SERVER_IP>:~/gmail_token.json
   ```
3. Restart container (token is mounted as volume, no rebuild needed)

**Status:** [x] DONE — re-authorized locally via `tools/gmail_auth.py`, uploaded fresh `gmail_token.json` to server. Token is bind-mounted, no restart needed.

---

## FIX-03 — theprotocol.it returns 0 jobs

**Error:**
```
[WARNING] hunter.sources.theprotocol: [theprotocol] 0 jobs from https://theprotocol.it/filtry/angular;sp?remote=true (HTML length=223588)
```

**Cause:**  
HTML was fetched (223KB) but 0 jobs parsed. Either:
- `dehydratedState` JSON path changed
- The query URL/params changed
- Cloudflare block returning empty data

**Fix:**
1. Run `/debug-scraper theprotocol` skill to inspect live HTML vs current parser
2. If JSON path changed — update `hunter/sources/theprotocol.py`
3. If Cloudflare block — check cloudscraper headers/cookies

**Status:** [x] DONE — `angular;sp` slug no longer returns results on theprotocol.it (API returns 0 offers). Removed from `LISTING_URLS`. `frontend;sp` URLs work fine (50+ offers).

---

## Deployment Fix — mount logs/ volume

**Issue:**  
`logs/` is not mounted in `docker-compose.yml`, so logs live only inside the container and require `docker cp` to retrieve.

**Fix:** Add to `docker-compose.yml` volumes:
```yaml
- ./logs:/app/logs
```

**Status:** [x] DONE — added to `docker-compose.yml`.

---

## FIX-04 — LinkedIn batch jobs lost after 529 Overloaded

**Error:**
```
[ERROR] LinkedIn batch job failed (529): https://www.linkedin.com/jobs/view/...
```

**Cause:**  
`_run_linkedin_batch` in `telegram_bot.py` would silently discard jobs that failed with 529 (Claude API overloaded). They never got retried.

**Fix:**  
On failure, write a stub `Job` to the FAIL queue via `add_failed()`. The `_retry_failed()` loop in `main.py` will pick them up on the next hunt.  
Also increased `CLI_MAX_RETRIES` 3→5 and `CLI_RETRY_DELAY` 30→60s in `config.py`.

**Status:** [x] DONE — commit `72183eb`.

---

## FIX-05 — /status showed only "idle" even during active apply

**Issue:**  
`_active_apply_urls` was a `set[str]`, so `/status` could only say "processing N jobs" with no detail.

**Fix:**  
Changed to `dict[str, datetime]` to track start time per URL. `/status` now shows elapsed time and a warning when nearing the timeout. Added `/schedule` command.

**Status:** [x] DONE — commit `ca6ed4e`.

---

## FIX-06 — Google Drive upload not happening after auto-apply

**Issue:**  
`main.py` auto-apply path (`_auto_apply_all` + `_retry_failed`) never called Drive upload. Files had to be uploaded manually.

**Fix:**  
Added `_upload_to_drive(url)` helper to `main.py`, called right after `_sync_to_sheets()` for both paths.

**Status:** [x] DONE — commit `5f94288`.

---

## FIX-07 — /force crashes with NameError: _parse_ats_score

**Error:**
```
NameError: name '_parse_ats_score' is not defined
```

**Cause:**  
`apply_agent.py` used `_parse_ats_score` (imported from `hunter.tracker`) only inside `--force` mode but never imported it at the call site.

**Fix:**  
Added inline `from hunter.tracker import _parse_ats_score` inside the force-mode block.

**Status:** [x] DONE — commit `6cfed25`.

---

## FIX-08 — ATS checker crashes with AttributeError: dict has no lower

**Error:**
```
[ERROR] ATS checker: AttributeError: 'dict' object has no attribute 'lower'
```

**Cause:**  
`resume_en` from the LLM response is a structured dict (sections/bullets), but `ats_checker.check()` expects a plain string.

**Fix:**  
Added `isinstance(resume_en, dict)` check; if dict, serialize with `json.dumps()` before passing to ATS checker.

**Status:** [x] DONE — commit `796965c`.

---

## FIX-09 — React-only filter too permissive (60 React jobs got docs)

**Issue:**  
`_angular_in_raw` scanned the raw job text for the word "angular". Sidebar/recommended-jobs content on fetched pages (e.g. JustJoin) often mentions Angular in unrelated listings, causing React-only jobs to bypass the filter.

**Fix:**  
Removed `_angular_in_raw` from the React-only skip condition in both `main_api` and `main_cli` paths. Now trusts the LLM's stack field exclusively.

**Status:** [x] DONE — commit `1905c4c`.
