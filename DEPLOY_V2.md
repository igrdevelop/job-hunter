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
            cd ${{ secrets.VPS_WORK_DIR }}
            echo ${{ secrets.GHCR_TOKEN }} | docker login ghcr.io -u ${{ github.actor }} --password-stdin
            docker compose pull
            docker compose up -d
            docker image prune -f
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

## Server command reference

```bash
# Logs
docker compose logs -f job-hunter

# Restart without pulling a new image
docker compose restart job-hunter

# Manual update (normally done by CI/CD)
docker compose pull && docker compose up -d

# Shell into the container
docker exec -it job-hunter bash

# Resource usage
docker stats job-hunter

# Status
docker compose ps
```

---

## Progress log

| Date | Who | What |
|------|-----|------|
| 2026-05-13 | sonnet-4-6 | Plan created based on answers to 11 questions |
