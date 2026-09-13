# Job Hunter — Deployment Plan V2

**Goal:** bot runs on a VPS 24/7, files mirrored to Google Drive,
code updates = push to master → CI/CD → auto-restart. Future: Angular site on top.

---

## Status

- [x] Phase 0 — GitHub repo ready, develop → master structure
- [ ] Phase 1 — Dockerfile hardened for production
- [ ] Phase 2 — VPS on Hetzner
- [ ] Phase 3 — Google Drive integration
- [ ] Phase 4 — CI/CD pipeline (GitHub Actions → GHCR → VPS)
- [ ] Phase 5 — First live deploy
- [ ] Phase 6 — Website (Angular + FastAPI) [future]

---

## Phase 1 — Production Dockerfile

### 1.1 — Fix Dockerfile

The draft in DEPLOY.md is incomplete. Production version:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# LibreOffice needed by generate_docs.py (DOCX → PDF conversion).
# gcc needed for some Python packages.
RUN apt-get update && apt-get install -y \
    gcc \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p Applications backups

CMD ["python", "hunter.py"]
```

### 1.2 — Add Google API packages to requirements.txt

```
google-api-python-client==2.131.0
google-auth==2.29.0
google-auth-oauthlib==1.2.0
google-auth-httplib2==0.2.0
```

### 1.3 — Create .dockerignore

```
.env
.secrets/
.git/
.github/
__pycache__/
*.pyc
*.pyo
tracker.xlsx
to_send.xlsx
Applications/
backups/
*.pdf
*.docx
.claude/
gmail_token.json
drive_token.json
gmail_credentials.json
drive_credentials.json
```

### 1.4 — Create docker-compose.yml

```yaml
version: "3.9"

services:
  job-hunter:
    image: ghcr.io/igrdevelop/job-hunter:latest
    container_name: job-hunter
    restart: always
    env_file:
      - .env
    volumes:
      # Persistent data — lives on the server disk.
      - ./tracker.xlsx:/app/tracker.xlsx
      - ./to_send.xlsx:/app/to_send.xlsx
      - ./Applications:/app/Applications
      - ./backups:/app/backups
      - ./.secrets:/app/.secrets
      # Google tokens — never baked into the image.
      - ./gmail_credentials.json:/app/gmail_credentials.json
      - ./gmail_token.json:/app/gmail_token.json
      - ./drive_credentials.json:/app/drive_credentials.json
      - ./drive_token.json:/app/drive_token.json
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "5"
```

### 1.5 — Server environment variables

Add to `.env` on the server (do not commit!):

```
# Existing variables — same as local
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
LLM_API_KEY=...
LLM_PROVIDER=anthropic
LLM_MODEL=claude-3-5-haiku-20241022

# Disable on server
INHIRE_ENABLED=false
APPLY_USE_CLI=false

# Google Drive (new)
GDRIVE_ENABLED=true
GDRIVE_FOLDER_ID=...        # Root JobHunter folder ID on Drive
GDRIVE_CREDENTIALS=drive_credentials.json
GDRIVE_TOKEN=drive_token.json
GDRIVE_SYNC_TRACKER=true    # upload tracker.xlsx to Drive
```

### 1.6 — Commit

```bash
git add Dockerfile .dockerignore docker-compose.yml
git commit -m "chore: production Dockerfile with LibreOffice + Drive deps"
git push origin develop
```

---

## Phase 2 — VPS on Hetzner

### 2.1 — SSH key (if you don't have one)

```powershell
ssh-keygen -t ed25519 -C "job-hunter-vps"
cat ~/.ssh/id_ed25519.pub   # copy — needed when creating the server
```

### 2.2 — Create server

1. console.hetzner.com → New Project → "job-hunter"
2. Add Server:
   - Location: **Nuremberg**
   - Image: **Ubuntu 22.04**
   - Type: **Shared vCPU → x86 → CX22** (2 CPU, 4 GB RAM, €4.35/month)
   - SSH Keys → Add → paste public key from 2.1
   - Name: `job-hunter`
3. Create & Buy → **write down the IP address**

### 2.3 — Connect and configure

```bash
ssh root@YOUR_IP

# Update system
apt update && apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com | sh
apt install docker-compose-plugin -y

# Create deploy user
useradd -m -s /bin/bash deploy
usermod -aG docker deploy
mkdir -p /home/deploy/job-hunter
chown deploy:deploy /home/deploy/job-hunter

# Copy SSH key for deploy user
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

### 2.4 — Attach Hetzner Volume (backups)

In the Hetzner panel → Volumes → Create Volume:
- Size: **10 GB** (~€0.50/month)
- Location: Nuremberg (same as the server)
- Name: `job-hunter-backups`
- Attach to server `job-hunter`

On the server:

```bash
# Format and mount
mkfs.ext4 /dev/disk/by-id/scsi-0HC_Volume_XXXXX   # ID from Hetzner panel
mkdir -p /mnt/backups
mount /dev/disk/by-id/scsi-0HC_Volume_XXXXX /mnt/backups
chown deploy:deploy /mnt/backups

# Auto-mount on reboot
echo "/dev/disk/by-id/scsi-0HC_Volume_XXXXX /mnt/backups ext4 defaults 0 0" >> /etc/fstab
```

Update `docker-compose.yml` — replace local backups folder with Volume:

```yaml
volumes:
  - /mnt/backups:/app/backups   # Hetzner Volume instead of local folder
```

### 2.5 — Upload files to the server

From your computer (PowerShell):

```powershell
$VPS = "deploy@YOUR_IP"
$SRC = "D:\LearningProject\Claude"
$DST = "/home/deploy/job-hunter"

# Secrets and data
scp "$SRC\.env"                   "${VPS}:${DST}/"
scp "$SRC\tracker.xlsx"           "${VPS}:${DST}/"
scp "$SRC\gmail_credentials.json" "${VPS}:${DST}/"
scp "$SRC\gmail_token.json"       "${VPS}:${DST}/"
scp "$SRC\drive_credentials.json" "${VPS}:${DST}/"   # after Phase 3
scp "$SRC\drive_token.json"       "${VPS}:${DST}/"   # after Phase 3
scp -r "$SRC\.secrets"            "${VPS}:${DST}/"

# Create folders
ssh $VPS "mkdir -p ${DST}/Applications ${DST}/backups"
```

---

## Phase 3 — Google Drive integration

### 3.1 — Create a separate OAuth client for Drive

Drive uses **separate credentials** (do not touch the Gmail OAuth):

1. Go to console.cloud.google.com → open the existing project (same as Gmail)
2. APIs & Services → Enable APIs → find **Google Drive API** → Enable
3. APIs & Services → Credentials → **Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: `job-hunter-drive`
4. Download JSON → save as `drive_credentials.json` in the project root
5. **Never commit** (already in .gitignore)

### 3.2 — Authorize Drive (locally, once)

Create `tools/drive_auth.py`:

```python
"""Authorize Google Drive OAuth and save drive_token.json. Run once locally."""
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
import json

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CREDS_FILE = Path("drive_credentials.json")
TOKEN_FILE = Path("drive_token.json")

flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
creds = flow.run_local_server(port=0)
TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
print(f"Saved: {TOKEN_FILE}")
```

Run locally:

```bash
python tools/drive_auth.py
# Browser opens → sign in to Google account → allow access
# drive_token.json is saved
```

### 3.3 — Create the JobHunter folder on Drive

1. Open drive.google.com
2. Create folder `JobHunter` → inside it create `Applications` and `Tracker`
3. Open the `JobHunter` folder → copy the ID from the URL:
   `https://drive.google.com/drive/folders/`**`1ABC123xyz`**
4. Write down this ID → add to `.env` as `GDRIVE_FOLDER_ID=1ABC123xyz`

### 3.4 — Create hunter/drive_client.py

```python
"""
hunter/drive_client.py — Google Drive uploader.

Uploads application folders (PDF/DOCX) and optionally tracker.xlsx to Drive.
Uses a separate OAuth token from Gmail (drive_token.json / drive_credentials.json).
"""

import logging
from pathlib import Path

from hunter.config import PROJECT_DIR, GDRIVE_ENABLED

logger = logging.getLogger(__name__)

_CREDS_FILE = PROJECT_DIR / "drive_credentials.json"
_TOKEN_FILE = PROJECT_DIR / "drive_token.json"
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)


def _get_or_create_folder(service, name: str, parent_id: str) -> str:
    """Return folder ID, creating it if it doesn't exist."""
    q = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
        f" and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(q=q, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def upload_application_folder(local_folder: Path, root_folder_id: str) -> str | None:
    """Upload all PDF/DOCX/TXT files from local_folder to Drive.

    Creates JobHunter/Applications/{date}/{company}/ structure on Drive.
    Returns the Drive folder URL, or None on failure.
    """
    if not GDRIVE_ENABLED:
        return None
    try:
        from googleapiclient.http import MediaFileUpload

        service = _get_service()

        # local_folder is Applications/{date}/{company}/
        # Drive path: root_folder_id / Applications / {date} / {company}
        parts = local_folder.parts
        app_idx = next(i for i, p in enumerate(parts) if p == "Applications")
        date_part = parts[app_idx + 1]
        company_part = parts[app_idx + 2]

        apps_id = _get_or_create_folder(service, "Applications", root_folder_id)
        date_id = _get_or_create_folder(service, date_part, apps_id)
        company_id = _get_or_create_folder(service, company_part, date_id)

        UPLOAD_EXTS = {".pdf", ".docx", ".txt"}
        for f in sorted(local_folder.iterdir()):
            if f.suffix.lower() not in UPLOAD_EXTS:
                continue
            mime = (
                "application/pdf" if f.suffix == ".pdf"
                else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if f.suffix == ".docx"
                else "text/plain"
            )
            media = MediaFileUpload(str(f), mimetype=mime, resumable=False)
            service.files().create(
                body={"name": f.name, "parents": [company_id]},
                media_body=media,
                fields="id",
            ).execute()
            logger.info("[drive] Uploaded: %s", f.name)

        url = f"https://drive.google.com/drive/folders/{company_id}"
        logger.info("[drive] Folder: %s", url)
        return url

    except Exception as e:
        logger.error("[drive] Upload failed: %s", e)
        return None


def upload_tracker(tracker_path: Path, root_folder_id: str) -> None:
    """Upload tracker.xlsx to JobHunter/Tracker/ on Drive."""
    if not GDRIVE_ENABLED:
        return
    try:
        from googleapiclient.http import MediaFileUpload

        service = _get_service()
        tracker_id = _get_or_create_folder(service, "Tracker", root_folder_id)

        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        media = MediaFileUpload(str(tracker_path), mimetype=mime, resumable=False)

        # Overwrite existing file if it exists.
        q = f"name='tracker.xlsx' and '{tracker_id}' in parents and trashed=false"
        existing = service.files().list(q=q, fields="files(id)").execute().get("files", [])
        if existing:
            service.files().update(
                fileId=existing[0]["id"], media_body=media
            ).execute()
        else:
            service.files().create(
                body={"name": "tracker.xlsx", "parents": [tracker_id]},
                media_body=media,
                fields="id",
            ).execute()
        logger.info("[drive] tracker.xlsx synced")
    except Exception as e:
        logger.error("[drive] tracker sync failed: %s", e)
```

### 3.5 — Add GDRIVE_ENABLED and GDRIVE_FOLDER_ID to config.py

```python
# Google Drive
GDRIVE_ENABLED: bool = os.getenv("GDRIVE_ENABLED", "false").lower() in ("true", "1", "yes")
GDRIVE_FOLDER_ID: str = os.getenv("GDRIVE_FOLDER_ID", "")
```

### 3.6 — Wire Drive upload into apply_agent.py

At the end of a successful apply (after generate_docs, before Telegram send):

```python
# Google Drive upload
drive_url = None
if GDRIVE_ENABLED and GDRIVE_FOLDER_ID:
    from hunter.drive_client import upload_application_folder, upload_tracker
    drive_url = upload_application_folder(Path(output_folder), GDRIVE_FOLDER_ID)
    if GDRIVE_SYNC_TRACKER:
        upload_tracker(TRACKER_PATH, GDRIVE_FOLDER_ID)
```

Include the Drive folder URL in the Telegram message:

```python
if drive_url:
    msg += f"\n📁 <a href='{drive_url}'>Drive folder</a>"
```

### 3.7 — Nightly re-sync (cron via JobQueue)

New file `tools/sync_to_drive.py` — scans `Applications/` and uploads anything not yet
on Drive (by checking whether a file with the same name exists in the Drive folder).
Scheduled as a daily task in telegram_bot.py at 03:00.

---

## Phase 4 — CI/CD pipeline

### 4.1 — GitHub Secrets

Go to: github.com/igrdevelop/job-hunter → Settings → Secrets → Actions

| Secret | Value |
|--------|-------|
| `VPS_HOST` | Server IP |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | Contents of `~/.ssh/id_ed25519` (private key) |
| `VPS_WORK_DIR` | `/home/deploy/job-hunter` |
| `GHCR_TOKEN` | GitHub PAT (write:packages, read:packages) |
| `TELEGRAM_BOT_TOKEN` | Bot token (for failure alerts) |
| `TELEGRAM_CHAT_ID` | Your chat ID |

### 4.2 — GitHub Container Registry token

GitHub profile → Settings → Developer settings → Personal access tokens → Tokens (classic):
- Note: `job-hunter-ghcr`
- Expiration: No expiration
- Scopes: `write:packages`, `read:packages`, `delete:packages`
- Save as secret `GHCR_TOKEN`

### 4.3 — Workflow file

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy Job Hunter

on:
  push:
    branches: [ master ]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: pytest tests/ -q

  build-and-deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GHCR_TOKEN }}

      - name: Build and push image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ env.IMAGE_NAME }}:latest
            ghcr.io/${{ env.IMAGE_NAME }}:${{ github.sha }}

      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            # set -eo pipefail is NOT optional here — see the workflow file.
            # Without it the step's exit code comes from the last command and a
            # failed pull reports success (2026-08-29 incident).
            set -eo pipefail
            cd ${{ secrets.VPS_WORK_DIR }}
            curl -fsSL https://raw.githubusercontent.com/igrdevelop/job-hunter/master/docker-compose.yml -o docker-compose.yml
            echo ${{ secrets.GHCR_TOKEN }} | docker login ghcr.io -u ${{ github.actor }} --password-stdin
            export IMAGE_TAG=${{ github.sha }}
            # Bounded retention (scripts/docker_prune.sh, fetched by the SHA
            # being deployed -- raw.githubusercontent.com caches `master` URLs
            # for ~5 min, 404s included): keep the live job-hunter image
            # + 1 previous as a rollback target, remove the rest, then the
            # generic `prune -a --filter until=168h` for everything else. A
            # time window alone could not cap the disk — ten ~8.1 GB images
            # younger than 2 days filled it on 2026-09-12 (see "Disk hygiene").
            curl -fsSL https://raw.githubusercontent.com/igrdevelop/job-hunter/${{ github.sha }}/scripts/docker_prune.sh -o docker_prune.sh
            sh docker_prune.sh
            # Free-space gate: the image is ~8.1 GB UNCOMPRESSED; 12000 MB is
            # that plus headroom for the compressed layers during extraction.
            AVAIL_MB=$(df -Pm / | awk 'NR==2 {print $4}')
            if [ "$AVAIL_MB" -lt 12000 ]; then
              echo "Only ${AVAIL_MB} MB free on / - need at least 12000 MB to pull and extract the ~8.1 GB image."
              df -h /
              docker system df
              exit 1
            fi
            docker compose pull
            docker compose up -d
            # Settle to live + 1 previous now that the new image is live.
            sh docker_prune.sh
            echo "Deploy complete"

      - name: Notify on failure
        if: failure()
        uses: appleboy/telegram-action@master
        with:
          to: ${{ secrets.TELEGRAM_CHAT_ID }}
          token: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          message: |
            Deploy FAILED
            Branch: ${{ github.ref_name }}
            Commit: ${{ github.sha }}
            https://github.com/${{ github.repository }}/actions/runs/${{ github.run_id }}
```

### 4.4 — Commit

```bash
git add .github/ Dockerfile .dockerignore docker-compose.yml requirements.txt hunter/drive_client.py hunter/config.py tools/drive_auth.py
git commit -m "feat: CI/CD pipeline + Drive integration"
git push origin develop
```

---

## Phase 5 — First live deploy

### 5.1 — Log in to GHCR on the server (once)

```bash
ssh deploy@YOUR_IP
cd /home/deploy/job-hunter
echo YOUR_GHCR_TOKEN | docker login ghcr.io -u igrdevelop --password-stdin
```

### 5.2 — Merge into master → trigger CI/CD

```bash
# Locally
git checkout master
git merge develop
git push origin master
```

Open github.com/igrdevelop/job-hunter → **Actions** tab — wait for the green checkmark (~3–5 min).

### 5.3 — Verify

```bash
ssh deploy@YOUR_IP
cd /home/deploy/job-hunter
docker compose ps             # status: Up
docker compose logs -f        # live logs
```

In Telegram: `/start` → should reply. `/hunt` → jobs should arrive.

### 5.4 — Verify Drive

After the first apply via the bot:
- Telegram sent a PDF and a Drive folder link
- On Drive: `JobHunter/Applications/{date}/{company}/` with the generated files
- `JobHunter/Tracker/tracker.xlsx` updated

---

## Workflow after deploy

```
Develop locally (develop branch)
  → git push origin develop
  → PR or direct merge into master
  → git push origin master
  → GitHub Actions: tests → Docker image → deploy to VPS (~4 min)
  → Bot restarts with the new code
```

No manual SSH needed after the first deploy.

---

## Phase 6 — Website (Angular + FastAPI) [future]

### Concept

```
Angular SPA (frontend)
    ↕ HTTP API
FastAPI (backend) ← reads tracker.xlsx, Applications/
    ↕ shared volume
Job Hunter Bot (already running)
```

### What needs to be done

**6.1 — Domain and HTTPS**
- Set DNS: point domain to server IP (A record)
- Install Nginx + Certbot:
  ```bash
  apt install nginx certbot python3-certbot-nginx -y
  certbot --nginx -d your-domain.com
  ```

**6.2 — FastAPI backend (`website/api/`)**
- `GET /api/applications` — list all applications from tracker.xlsx
- `GET /api/applications/{id}/files` — list files in folder
- `GET /api/applications/{id}/files/{filename}` — download file
- `POST /api/hunt` — trigger hunt manually (proxy to bot)
- Auth: simple Bearer token in `.env`

**6.3 — Angular frontend (`website/frontend/`)**
- Applications table with filters (status, date, stack)
- PDF preview in browser
- Apply / Skip / Force buttons
- Dashboard: stats by source, stack, day

**6.4 — Extended docker-compose.yml**
```yaml
services:
  job-hunter:        # already exists
    ...
  api:
    build: ./website/api
    volumes:
      - ./tracker.xlsx:/app/tracker.xlsx:ro
      - ./Applications:/app/Applications:ro
  frontend:
    build: ./website/frontend
    ports:
      - "80:80"
      - "443:443"
```

**6.5 — Nginx config**
```nginx
server {
    server_name your-domain.com;
    location /api/ { proxy_pass http://api:8000/; }
    location /     { root /usr/share/nginx/html; try_files $uri /index.html; }
}
```

---

## Backups

Two layers (docs/improvement-2026-09/06-OPS_PLAN.md M1). Neither existed before
2026-09: the only thing backed up daily was `tracker.xlsx`, which prod only
writes on `/export` — `tracker.db` (the real, live data store), `app.sqlite`
(job-hunter-api's users/auth/profiles) and `users/` (CVs, profiles,
Applications documents) had no backup at all.

### Layer 1 — local snapshots (already running, in-container)

`hunter/tracker_backup.py` runs daily via PTB JobQueue
(`TRACKER_BACKUP_TIME`, default 06:05) — see the `hunter/tracker_backup.py`
entry in CLAUDE.md for the full mechanism. It copies `tracker.db` via
`sqlite3.Connection.backup()` (WAL-safe — never a raw file copy of a live
WAL database), optionally `app.sqlite` when `APP_SQLITE_PATH` points at an
existing file, and the legacy `tracker.xlsx` when present, into
`TRACKER_BACKUP_DIR` (default `./backups`, `TRACKER_BACKUP_KEEP_FILES`
files kept per family, default 90). Every produced `.db` copy is verified
with `PRAGMA integrity_check` right after the backup; a failure surfaces as
a Telegram alert (`hunter/schedules/tracker_backup.py`).

**These are local snapshots on the same disk as the live data.** A dead
disk or a wiped VPS takes both the original and this "backup" with it —
that's what Layer 2 is for.

### Layer 2 — off-host restic (host cron, one-time setup)

`scripts/offhost_backup.sh` (tracked, not run by any agent) pushes
`{db,users,backups}` under the bot's working dir — plus job-hunter-api's
own data dir, when reachable — to an S3-compatible bucket (Backblaze B2,
Hetzner Object Storage, or anything else restic supports) via `restic
backup`, then prunes old snapshots with `restic forget --keep-daily 30
--keep-weekly 12 --prune`.

One-time setup on the VPS, as the `deploy` user:

```bash
# Install restic (Ubuntu 24.04 ships it in the default repos).
sudo apt-get install -y restic

# Pick a bucket + credentials with your S3-compatible provider, then:
mkdir -p /home/deploy/.restic
echo '<a long random passphrase>' > /home/deploy/.restic/password
chmod 600 /home/deploy/.restic/password

export RESTIC_REPOSITORY='s3:https://s3.<region>.backblazeb2.com/<bucket>'
export RESTIC_PASSWORD_FILE=/home/deploy/.restic/password
export AWS_ACCESS_KEY_ID='...'        # or restic's own -o s3.* flags
export AWS_SECRET_ACCESS_KEY='...'

# One-time: create the repo (idempotent — safe to skip if it already exists).
restic init
```

Install the daily cron job (`crontab -e`, same asymmetry-of-ownership
reasoning as the Docker-prune timer below — this line is NOT installed by
any deploy workflow, it's a one-time host step):

```cron
# Off-host backup, daily at 03:10 (before the 06:05 in-container snapshot
# so restic always has a fresh local copy to pick up; either order is safe
# since restic backs up whatever's on disk at run time). Env vars above
# belong in a sourced file, not inline in the crontab line, so the
# passphrase/keys never show up in `crontab -l` or process listings.
10 3 * * * . /home/deploy/.restic/env && /home/deploy/job-hunter/scripts/offhost_backup.sh >> /home/deploy/offhost-backup.log 2>&1
```

(`/home/deploy/.restic/env` is a small `export RESTIC_REPOSITORY=... ...`
file, `chmod 600`, sourced by the cron line above — keeps every backend
credential out of the crontab itself.)

Optional dead-man's-switch: set `BACKUP_PING_URL` in that same env file
(e.g. a healthchecks.io check URL) — the script pings it on success and on
`/fail` on failure, best-effort, never itself fails the backup.

### Restore drill

A backup nobody has restored is a hope, not a backup. `scripts/
restore_drill.sh` restores the latest snapshot into a scratch temp dir and
runs `PRAGMA integrity_check` against every `.db` file it finds — read-only
against the repo, deletes its own scratch dir on exit. Run it by hand after
first setting up Layer 2, and periodically afterward (e.g. monthly, same
cron pattern as above with `SNAPSHOT` left at its `latest` default):

```bash
. /home/deploy/.restic/env && /home/deploy/job-hunter/scripts/restore_drill.sh
```

Non-zero exit = either the restore itself failed, or at least one restored
`.db` file failed `integrity_check` — treat either as "the backup layer is
not actually protecting anything" until fixed, not as a routine warning.

### Retention and erasure

Layer 1 keeps `TRACKER_BACKUP_KEEP_FILES` (default 90) most-recent copies
per family, pruned on every run. Layer 2's `restic forget --keep-daily 30
--keep-weekly 12` is the effective backup retention ceiling — a user's data
erased from the live db per `docs/improvement-2026-09/07-COMPLIANCE_PLAN.md`
still exists in restic snapshots until they age out under this policy;
that ceiling IS the retention mechanism referenced there, not a gap to
close separately.

---

## Disk hygiene (one-time host setup)

The deploy workflow prunes images on every run, but that only fires **when this
repo deploys**. This host's Docker daemon is shared with `job-hunter-api`,
`arifma` and `psybook`, whose deploys leave images here and do **not** prune —
so if job-hunter goes quiet while the siblings keep shipping, nothing reclaims
anything. That asymmetry is what a host-level timer covers.

Measured 2026-08-29, when the disk hit 100% and broke the deploy: 17 images,
72.65 GB total, **63.92 GB reclaimable**. Everything else on the box was
noise by comparison — `users/` 127 MB (100 application folders over 3.5
months), `logs/` 52 MB and already bounded by rotation, `backups/` + `db/`
14 MB. Images are the only thing worth automating.

**Why a bounded count and not a time window (2026-09-12).** The first fix
was `docker image prune -a -f --filter until=168h`, in both the deploy and
this cron. Two weeks later the disk was at 100% again with 17 images /
73 GB, 63 GB reclaimable — **ten `ghcr.io/igrdevelop/job-hunter:<sha>`
images of ~8.1 GB each, all younger than two days**. The image is 8.1 GB
uncompressed (Playwright chromium + LibreOffice + Claude CLI), so a busy
merge day adds tens of GB in hours, and an age filter of a week cannot
reclaim any of it — both the deploy-time prune and this cron ran and freed
nothing. What caps the disk is a bound on the NUMBER of images, whatever
the merge rate: `scripts/docker_prune.sh` keeps the image behind the live
`job-hunter` container plus one previous (`KEEP_PREVIOUS=1`, the rollback
target), removes every other job-hunter image, and only then runs the
generic `until=168h` prune for the sibling projects' leftovers. Steady
state is therefore two job-hunter images (~16 GB), peaking at three during
a deploy. The deploy workflow fetches the same script (pinned to the commit
being deployed, next to `docker-compose.yml`) and runs it before the pull and again after
`up -d`; the cron below runs the local copy that fetch leaves behind, so
the two never drift.

Install once, as the `deploy` user (`crontab -e`). The script lands in the
work dir on the first deploy after this change; to install the cron before
that, fetch it by hand once with the same `curl` the workflow uses:

```cron
# Bounded job-hunter image retention + generic week-old prune, daily at 04:17.
# scripts/docker_prune.sh (fetched by the deploy next to docker-compose.yml):
# keeps the live job-hunter image + 1 previous, removes the rest, then
# `docker image prune -a -f --filter until=168h` for everything else on this
# shared daemon. Images in use by any running container are always kept, and
# anything removed is re-pullable from GHCR. `>` (not `>>`) keeps the log to
# the last run so it cannot itself become a disk problem.
# DOCKER=/usr/bin/docker on purpose: cron runs with a minimal PATH, and a bare
# `docker` that resolves in your login shell is the classic way for a
# scheduled job to fail silently at 04:17 forever.
17 4 * * * DOCKER=/usr/bin/docker /bin/sh /home/deploy/job-hunter/docker_prune.sh > /home/deploy/docker-prune.log 2>&1
```

Verify it took effect, and check what the last run reclaimed (the script
ends with `docker system df`, so the log shows the post-prune state):

```bash
crontab -l | grep docker_prune
cat /home/deploy/docker-prune.log
```

## Claude CLI token (LLM-outage fallback)

The subscription fallback (docs/LLM_OUTAGE_RESILIENCE_PLAN.md M4/M4b) needs the
`claude` CLI inside the container to be authenticated. There is no feature flag:
`llm_client.cli_credentials_present()` returning True IS the switch.

**On this host authentication is a long-lived token, not an interactive login.**
The OAuth login writes a rotating refresh token into
`./.claude-cli/.credentials.json`; when a refresh fails, the CLI does not delete
that file — it rewrites it with **blank** tokens. That is what happened on
2026-09-08: the API balance was drained *and* the login was silently dead, so
both LLM paths were down and auto-apply sat paused for ~18 h. A
`claude setup-token` token is valid ~1 year, does not rotate, and lives in
`.env`, so it cannot be blanked by a background refresh.

### Issue / rotate the token

```bash
cd ~/job-hunter
docker compose exec -it job-hunter claude setup-token
```

Follow the printed OAuth URL in a browser, paste the code back, copy the token
it prints (`sk-ant-oat…`), then:

```bash
# .env — personal subscription credential, same care as gsheets_token.json
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
```

```bash
docker compose up -d          # env_file: .env — the container must be recreated
```

### Verify

```bash
docker compose exec -T job-hunter sh -lc 'claude -p "reply with exactly: OK" --model claude-haiku-4-5-20251001'
```

`Failed to authenticate: OAuth session expired and could not be refreshed` means
the token did not reach the process (check `docker compose exec -T job-hunter
sh -lc 'echo ${CLAUDE_CODE_OAUTH_TOKEN:+set}'`) or has been revoked.

Then lift the outage pause the failed runs armed — `/llm outage clear` in
Telegram — and `/retry_reset` if any rows exhausted `MAX_FAIL_RETRIES` while
both paths were down.

### Disabling the fallback

Unset `CLAUDE_CODE_OAUTH_TOKEN` **and** empty `./.claude-cli/` (or `claude
/logout` in the container). Either one alone leaves the fallback live.

---

## Server command reference

```bash
# Logs
docker compose logs -f job-hunter

# Restart without pulling a new image
docker compose restart job-hunter

# Manual update (normally done by CI/CD). IMAGE_TAG must be an exact commit
# SHA from master — compose falls back to :latest when it is unset.
export IMAGE_TAG=<full-commit-sha>
docker compose pull && docker compose up -d

# Disk check — the image is ~8.1 GB UNCOMPRESSED (measured 2026-09-12; the deploy
# gate wants 12000 MB free before it pulls). A full disk broke the 2026-08-29
# deploy (silently, before set -eo pipefail) and again on 2026-09-12 (ten
# fresh ~8.1 GB images that an age-based prune could not touch).
df -h /
docker system df                       # RECLAIMABLE column is the one to watch
docker images ghcr.io/igrdevelop/job-hunter   # SIZE column = what the gate must fit

# Reclaim space: live job-hunter image + 1 previous kept, everything else of
# ours removed, then the generic week-old prune for the sibling projects.
# Same file the deploy and the 04:17 cron run (see "Disk hygiene").
sh docker_prune.sh
# Rollback target gone too (e.g. after KEEP_PREVIOUS=0)? Any SHA is re-pullable:
#   IMAGE_TAG=<full-commit-sha> docker compose pull

# Shell into the container
docker exec -it job-hunter bash

# Resource usage
docker stats job-hunter

# Status
docker compose ps

# Claude CLI (LLM-outage fallback) — is it authenticated?
docker compose exec -T job-hunter sh -lc 'echo ${CLAUDE_CODE_OAUTH_TOKEN:+token set}'
docker compose exec -T job-hunter sh -lc 'claude -p "reply with exactly: OK" --model claude-haiku-4-5-20251001'
```

---

## Progress log

| Date | Who | What |
|------|-----|------|
| 2026-05-13 | sonnet-4-6 | Plan created based on answers to 11 questions |
