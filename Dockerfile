FROM python:3.11-slim

WORKDIR /app

# nodejs/npm: for the Claude CLI (M4 outage fallback, docs/LLM_OUTAGE_RESILIENCE_PLAN.md).
# Debian bookworm ships Node 18 — the CLI's minimum.
RUN apt-get update && apt-get install -y \
    gcc \
    libreoffice \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Claude CLI (subscription path). Only used when the CLI can authenticate —
# CLAUDE_CODE_OAUTH_TOKEN in the environment (deploy host, see docs/DEPLOY.md)
# or a logged-in config dir mounted at the CLI user's ~/.claude (docker-compose:
# ./.claude-cli). That IS the on/off switch (no feature flag); with neither,
# llm_client.cli_credentials_present() keeps the CLI out of the way entirely.
RUN npm install -g @anthropic-ai/claude-code

# Non-root runtime user (docs/improvement-2026-09/05-SECURITY_PLAN.md M1): the
# CLI apply agent (hunter/apply_cli.py) runs an LLM agent with Bash/file access
# over scraped, untrusted job-posting text. Running that as root in a container
# that mounts .env, db/, users/ and .claude-cli turned any command a prompt
# injection got the agent to run into full container compromise, and the old
# `IS_SANDBOX=1` existed only to let `--dangerously-skip-permissions` run as
# root at all — both are gone now that generation runs as this user and the
# CLI's own tool policy (hunter/apply_cli.py::_build_cli_command) replaces the
# skip-permissions flag by default.
RUN useradd --create-home --uid 1000 --shell /bin/bash hunter

# Keep ALL claude state (config + OAuth credentials) inside the mounted volume —
# without this the global config lands outside the mount and the login dies
# with the container. Points at the new user's home (docker-compose mounts
# ./.claude-cli here, was /root/.claude).
ENV CLAUDE_CONFIG_DIR=/home/hunter/.claude
# Shared, world-readable Playwright cache: chromium installs as root below
# (apt packages need root), but the `hunter` user must be able to READ the
# binaries at runtime — /root is mode 700 and invisible to another user, so
# the cache lives outside any home directory instead.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
RUN playwright install chromium --with-deps

COPY . .
RUN pip install --no-cache-dir -e . --no-deps

RUN mkdir -p Applications backups \
    && chown -R hunter:hunter /app "$PLAYWRIGHT_BROWSERS_PATH"

USER hunter

CMD ["python", "-m", "hunter"]