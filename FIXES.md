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

**Status:** [ ] pending
