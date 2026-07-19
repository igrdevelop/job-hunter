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

# Claude CLI (Pro subscription path). Only used when a logged-in config dir is
# mounted (docker-compose: ./.claude-cli:/root/.claude) — the login IS the
# on/off switch (no feature flag); without credentials on disk,
# llm_client.cli_credentials_present() keeps the CLI out of the way entirely.
RUN npm install -g @anthropic-ai/claude-code
# Keep ALL claude state (config + OAuth credentials) inside the mounted volume —
# without this the global config lands in /root/.claude.json OUTSIDE the mount
# and the login dies with the container.
ENV CLAUDE_CONFIG_DIR=/root/.claude
# The CLI refuses --dangerously-skip-permissions (which apply_cli.py passes) when
# running as root unless it is told it's in a sandbox. This container IS the
# sandbox: single-purpose, no interactive user.
ENV IS_SANDBOX=1

COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
RUN playwright install chromium --with-deps

COPY . .
RUN pip install --no-cache-dir -e . --no-deps

RUN mkdir -p Applications backups

CMD ["python", "-m", "hunter"]