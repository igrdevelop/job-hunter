# Deploying Job Hunter to a VPS

Step-by-step guide — do it yourself, line by line.

---

## Current status

- [x] Phase 1 — GitHub repo created, code pushed
- [ ] Phase 2 — Dockerfile
- [ ] Phase 3 — VPS on Hetzner
- [ ] Phase 4 — GitHub Actions CI/CD
- [ ] Phase 5 — First launch

---

## Phase 2 — Dockerfile (done locally)

### What is a Dockerfile?

A Dockerfile is a recipe. You describe how to build a "box" (container) with your bot.
Docker reads the file and creates an image — a snapshot of everything needed to run:
Python, libraries, code. That image can then be started on any server identically.

---

### Step 2.1 — Create Dockerfile

Open VS Code in `D:\LearningProject\Claude`.
Create a file called `Dockerfile` (no extension) in the project root.

Contents:

```dockerfile
# Official Python 3.11 base image.
# slim = lightweight variant without dev tools (smaller image size).
FROM python:3.11-slim

# Set working directory inside the container.
# All subsequent commands run from here.
WORKDIR /app

# Install gcc — required to compile some Python packages.
# Clean apt cache afterwards to keep the image small.
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy ONLY requirements.txt (not the full source yet).
# Why separately? Docker caches layers.
# If source changes but requirements.txt does not, pip install is skipped.
COPY requirements.txt .

# Install dependencies.
# --no-cache-dir = do not store pip cache (saves space).
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the full source.
# Separate layer — only rebuilt when source changes.
COPY . .

# Create the Applications folder (will be overridden by a server volume mount).
RUN mkdir -p Applications

# Bot startup command.
CMD ["python", "hunter.py"]
```

When done — say "ready", we'll continue.

---

### Step 2.2 — Create .dockerignore

Like .gitignore, but for Docker.
Docker will not copy these files/folders into the image.

Create `.dockerignore` in the project root:

```
# Secrets — must never end up in the image
.env
.secrets/

# Git
.git/
.github/

# Python cache
__pycache__/
*.pyc
*.pyo

# Personal data — not needed in the image
tracker.xlsx
Applications/

# Documents
*.pdf
*.docx

# Claude Code
.claude/

# Tests and dev files
*.md
DEPLOY.md
```

When done — say "ready".

---

### Step 2.3 — Create docker-compose.yml

docker-compose describes how to run the container on the server:
which environment variables, which folders to mount, restart policy.

Create `docker-compose.yml` in the project root:

```yaml
version: "3.9"

services:
  job-hunter:
    # Pull image from GitHub Container Registry (CI/CD pushes there).
    image: ghcr.io/igrdevelop/job-hunter:latest
    container_name: job-hunter

    # always = auto-restart if the container crashes or the server reboots.
    restart: always

    # Load environment variables from the .env file on the server.
    env_file:
      - .env

    # Volumes — mount server folders into the container.
    # Format: host_path:container_path
    volumes:
      # tracker.xlsx lives on the server disk; survives image updates.
      - ./tracker.xlsx:/app/tracker.xlsx
      # Generated application documents also persist.
      - ./Applications:/app/Applications
      # LinkedIn session (if used).
      - ./.secrets:/app/.secrets

    # Log settings.
    logging:
      driver: "json-file"
      options:
        max-size: "10m"   # max 10 MB per log file
        max-file: "3"     # keep the last 3 rotated files
```

When done — say "ready".

---

### Step 2.4 — Commit and push

After all three files are created, run in the terminal:

```bash
git add Dockerfile .dockerignore docker-compose.yml
git commit -m "chore: add Docker configuration"
git push
```

---

### Phase 2 verification

After pushing, go to github.com/igrdevelop/job-hunter —
three new files should appear in the repo:
- `Dockerfile`
- `.dockerignore`
- `docker-compose.yml`

---

## Phase 3 — VPS on Hetzner

### What is a VPS?

A VPS (Virtual Private Server) is your computer on the internet.
It runs 24/7, has a public IP address, and you control it over SSH.

Hetzner is a German provider with the best price/performance ratio in Europe.
CX22 — 2 CPU, 4 GB RAM, 40 GB SSD — €4.35/month (~18 PLN).
More than enough for the bot and a future website.

---

### Step 3.1 — Create an SSH key (if you don't have one)

An SSH key is like a password, but more secure. It has two parts:
- private key (stays on your computer, never share it)
- public key (give it to the server — it recognises you)

Open PowerShell and run:

```powershell
ssh-keygen -t ed25519 -C "job-hunter-vps"
```

When asked for a path — press Enter (saves to ~/.ssh/id_ed25519).
When asked for a passphrase — leave it empty (just Enter).

Show the public key:

```powershell
cat ~/.ssh/id_ed25519.pub
```

Copy the entire output — you'll need it when creating the server.

---

### Step 3.2 — Create a server on Hetzner

1. Go to console.hetzner.com → register
2. New Project → name it "job-hunter"
3. Add Server:
   - Location: **Nuremberg** (closest to Poland)
   - Image: **Ubuntu 22.04**
   - Type: **Shared vCPU → x86 → CX22**
   - SSH Keys → Add SSH Key → paste the public key from step 3.1
   - Name: `job-hunter`
4. Create & Buy

Write down the server IP — it appears on the page after creation.

---

### Step 3.3 — First login to the server

```powershell
ssh root@YOUR_IP
```

If asked "Are you sure?" — type `yes`.

You are now inside the server. The prompt changes to something like:
```
root@job-hunter:~#
```

---

### Step 3.4 — Install Docker

Run commands one by one:

```bash
# Update package list
apt update && apt upgrade -y

# Install Docker using the official script
curl -fsSL https://get.docker.com | sh

# Install docker-compose
apt install docker-compose -y

# Verify installation
docker --version
docker-compose --version
```

---

### Step 3.5 — Create a deploy user

Running as root is bad practice. Create a dedicated user:

```bash
# Create user
useradd -m -s /bin/bash deploy

# Add to the docker group (so they can run containers)
usermod -aG docker deploy

# Create project folder
mkdir -p /home/deploy/job-hunter
chown deploy:deploy /home/deploy/job-hunter
```

Add SSH key for the deploy user:

```bash
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

Verify you can log in as deploy (from your computer, not the server):

```powershell
ssh deploy@YOUR_IP
```

---

### Step 3.6 — Upload files to the server

From your computer (not the server!), in PowerShell:

```powershell
# Upload .env with secrets
scp D:\LearningProject\Claude\.env deploy@YOUR_IP:/home/deploy/job-hunter/

# Upload docker-compose.yml
scp D:\LearningProject\Claude\docker-compose.yml deploy@YOUR_IP:/home/deploy/job-hunter/

# Upload tracker.xlsx
scp D:\LearningProject\Claude\tracker.xlsx deploy@YOUR_IP:/home/deploy/job-hunter/

# Create Applications folder on the server
ssh deploy@YOUR_IP "mkdir -p /home/deploy/job-hunter/Applications"

# Upload .secrets if you use LinkedIn
scp -r D:\LearningProject\Claude\.secrets deploy@YOUR_IP:/home/deploy/job-hunter/
```

---

## Phase 4 — GitHub Actions CI/CD

### What is GitHub Actions?

Automation built into GitHub. You describe in a YAML file:
"when someone pushes to master — do this".

"This" in our case:
1. Build a Docker image from the code
2. Push the image to GitHub Container Registry
3. SSH into the VPS
4. Pull the new image
5. Restart the container

---

### Step 4.1 — Add secrets to GitHub

Go to github.com/igrdevelop/job-hunter →
Settings → Secrets and variables → Actions → New repository secret

Add one by one:

| Name | Value |
|------|-------|
| `VPS_HOST` | Your server IP (e.g. 65.21.123.45) |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | Contents of `~/.ssh/id_ed25519` (private key!) |
| `VPS_WORK_DIR` | `/home/deploy/job-hunter` |

Show the private key contents:
```powershell
cat ~/.ssh/id_ed25519
```

Copy EVERYTHING including `-----BEGIN...` and `-----END...`.

---

### Step 4.2 — GitHub Container Registry token

GitHub → Settings (your profile, not the repo!) →
Developer settings → Personal access tokens → Tokens (classic) →
Generate new token (classic)

- Note: `job-hunter-ghcr`
- Expiration: No expiration
- Check: `write:packages`, `read:packages`, `delete:packages`
- Generate token → copy the token

Add it as a repo secret (step 4.1):

| Name | Value |
|------|-------|
| `GHCR_TOKEN` | the token you just copied |

---

### Step 4.3 — Create the workflow file

Create the folders and file:
```
.github/
  workflows/
    deploy.yml
```

Contents of `deploy.yml`:

```yaml
name: Deploy Job Hunter

# Run on every push to master.
on:
  push:
    branches: [ master ]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest

    steps:
      # 1. Check out the repository.
      - name: Checkout code
        uses: actions/checkout@v4

      # 2. Log in to GitHub Container Registry.
      - name: Log in to Container Registry
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GHCR_TOKEN }}

      # 3. Build and push the Docker image.
      - name: Build and push Docker image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ env.IMAGE_NAME }}:latest
            ghcr.io/${{ env.IMAGE_NAME }}:${{ github.sha }}

      # 4. Deploy to VPS over SSH.
      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd ${{ secrets.VPS_WORK_DIR }}

            # Log in to GHCR.
            echo ${{ secrets.GHCR_TOKEN }} | docker login ghcr.io -u ${{ github.actor }} --password-stdin

            # Pull the new image.
            docker pull ghcr.io/${{ env.IMAGE_NAME }}:latest

            # Restart the container with the new image.
            docker-compose up -d --pull always

            # Remove old images to free disk space.
            docker image prune -f

            echo "Deploy complete!"
```

After creating the file:

```bash
git add .github/
git commit -m "chore: add GitHub Actions CI/CD pipeline"
git push origin develop
```

---

### Step 4.4 — First deploy

CI/CD triggers on push to **master**.
Merge develop into master:

```bash
git checkout master
git merge develop
git push origin master
```

Go to github.com/igrdevelop/job-hunter → **Actions** tab.
You will see the workflow running. It takes 2–3 minutes.
Green checkmark = deploy succeeded.

---

## Phase 5 — First launch

### Step 5.1 — Log in to the server

```powershell
ssh deploy@YOUR_IP
cd /home/deploy/job-hunter
```

### Step 5.2 — Log in to GHCR (once)

```bash
echo YOUR_GHCR_TOKEN | docker login ghcr.io -u igrdevelop --password-stdin
```

### Step 5.3 — Start the bot

```bash
docker-compose up -d
```

### Step 5.4 — Verify it works

```bash
# Container status
docker-compose ps

# Live logs (Ctrl+C to exit)
docker-compose logs -f
```

If a message from the bot arrives in Telegram — everything works.

---

## Useful commands (on the server)

```bash
# View logs
docker-compose logs -f job-hunter

# Restart
docker-compose restart job-hunter

# Stop
docker-compose stop

# Update manually (usually CI/CD does this automatically)
docker-compose pull && docker-compose up -d

# Shell into the container for debugging
docker exec -it job-hunter bash

# Memory/CPU usage
docker stats job-hunter
```

---

## After deploy — how to update the bot

The workflow going forward:

```bash
# 1. Make changes to the code locally.
# 2. Commit.
git add .
git commit -m "fix: description of the fix"

# 3. Merge into master.
git checkout master
git merge develop
git push origin master

# 4. GitHub Actions deploys automatically in 2–3 minutes.
#    Watch progress at github.com/igrdevelop/job-hunter -> Actions.
```

Nothing else needs to be done.
