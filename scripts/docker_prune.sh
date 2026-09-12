#!/bin/sh
# scripts/docker_prune.sh — bounded retention for the job-hunter deploy images.
#
# Why a COUNT and not a time window. Every deploy tags its image with a
# commit SHA, and the image is ~8.1 GB uncompressed (Playwright chromium +
# LibreOffice + Claude CLI — measure with `docker images ghcr.io/igrdevelop/
# job-hunter`, re-measured 2026-09-12). A busy day of merges therefore adds
# tens of GB in a few hours, and `docker image prune -a --filter until=168h`
# — the retention this replaced — cannot reclaim any of it for a week: on
# 2026-09-12 the 75 GB VPS disk hit 100% with TEN job-hunter images (~63 GB
# reclaimable), all younger than two days, so both the deploy-time prune and
# the nightly cron ran and freed nothing. A bound on the NUMBER of images is
# what actually caps the disk, whatever the merge rate is.
#
# Rule: keep the image behind the live `job-hunter` container (never
# removable anyway while the container runs) plus the newest KEEP_PREVIOUS
# other job-hunter images (default 1 — the previous deploy, as a rollback
# target); remove the rest. Images used by ANY running container on the host
# are also skipped, so a manually started rollback container is safe.
# Everything removed is re-pullable from GHCR by SHA.
#
# The generic `docker image prune -a --filter until=$PRUNE_UNTIL` still runs
# afterwards for everything ELSE on this shared daemon (job-hunter-api, arifma,
# psybook leave images here and never prune — see docs/DEPLOY.md "Disk
# hygiene"). Note it also removes job-hunter's kept rollback image once that
# is older than the window; a week is deliberate — a regression is usually
# noticed within days, and the SHA is still on GHCR after that.
#
# Callers (both documented in docs/DEPLOY.md):
#   - .github/workflows/deploy.yml — curls this file (pinned to the deployed
#     commit SHA) next to docker-compose.yml and runs it BEFORE `docker
#     compose pull` (reclaim first) and AFTER `docker compose up -d` (settle
#     to running + KEEP_PREVIOUS).
#   - the host cron (`17 4 * * *`) — covers a quiet job-hunter while sibling
#     projects keep shipping.
#
# Optional env:
#   IMAGE_REPO      default ghcr.io/igrdevelop/job-hunter
#   CONTAINER       default job-hunter (the compose container_name)
#   KEEP_PREVIOUS   default 1 — job-hunter images to keep besides the live one
#   PRUNE_UNTIL     default 168h — window for the generic prune of everything else
#   DOCKER          default docker — the binary; cron runs with a minimal PATH,
#                   pass an absolute path there (see the crontab line in DEPLOY.md)
#
# Never exits non-zero because an image could not be removed (that is a
# warning, and the deploy's own free-space gate is the real protection);
# exits non-zero only if `docker images`/`docker ps` themselves fail.

set -eu

IMAGE_REPO="${IMAGE_REPO:-ghcr.io/igrdevelop/job-hunter}"
CONTAINER="${CONTAINER:-job-hunter}"
KEEP_PREVIOUS="${KEEP_PREVIOUS:-1}"
PRUNE_UNTIL="${PRUNE_UNTIL:-168h}"
DOCKER="${DOCKER:-docker}"

case "$KEEP_PREVIOUS" in
  ''|*[!0-9]*) echo "docker_prune: KEEP_PREVIOUS must be a non-negative integer, got '$KEEP_PREVIOUS'" >&2; exit 2 ;;
esac

# Full `sha256:...` ids on both sides so they compare as strings in awk.
# Each docker call is captured on its own line on purpose: POSIX sh has no
# `pipefail`, so `docker images ... | awk` would turn a failed listing into a
# silent "nothing to remove" instead of the non-zero exit promised above.
live=$("$DOCKER" inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)
running_ids=$("$DOCKER" ps -q)
in_use=$(printf '%s\n' "$running_ids" | xargs -r "$DOCKER" inspect --format '{{.Image}}' | tr '\n' ' ')
images=$("$DOCKER" images -q --no-trunc "$IMAGE_REPO")

# `docker images <repo>` lists newest first, one line per TAG — the same image
# shows up twice (:latest + :<sha>), hence the seen[] collapse.
to_remove=$(printf '%s\n' "$images" | awk -v keep="$KEEP_PREVIOUS" -v live="$live" -v in_use="$in_use" '
  BEGIN { n = split(in_use, ids, " "); for (i = 1; i <= n; i++) busy[ids[i]] = 1 }
  $0 == ""           { next }
  seen[$0]++         { next }
  $0 == live         { next }
  ($0 in busy)       { next }
  ++kept > keep      { print }
')

if [ -n "$to_remove" ]; then
  echo "docker_prune: removing $(printf '%s\n' "$to_remove" | wc -l) $IMAGE_REPO image(s) beyond live + $KEEP_PREVIOUS previous"
  # shellcheck disable=SC2086 — word-splitting the id list is the point
  "$DOCKER" rmi -f $to_remove || echo "docker_prune: some images could not be removed (see above); continuing"
else
  echo "docker_prune: nothing to remove for $IMAGE_REPO (live + up to $KEEP_PREVIOUS previous kept)"
fi

"$DOCKER" image prune -a -f --filter "until=$PRUNE_UNTIL"
"$DOCKER" system df
