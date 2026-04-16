#!/usr/bin/env bash
# scripts/auto_update.sh
# ─────────────────────
# Hourly cron script for the Raspberry Pi.
# Pulls latest code from GitHub; if anything changed, rebuilds and restarts
# the Docker container. No-ops silently if already up to date.
#
# Install:
#   chmod +x ~/alpaca-bot/scripts/auto_update.sh
#   crontab -e
#   # Add this line (runs every hour at :05 past):
#   5 * * * * ~/alpaca-bot/scripts/auto_update.sh >> ~/alpaca-bot/logs/auto_update.log 2>&1

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_PREFIX="[auto_update $(date '+%Y-%m-%d %H:%M:%S')]"

cd "$REPO_DIR"

# ── Fetch without merging so we can compare SHAs ─────────────────────────────
git fetch origin main --quiet

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "$LOG_PREFIX No new commits ($(git rev-parse --short HEAD)) — skipping rebuild"
    exit 0
fi

COMMITS=$(git log --oneline HEAD..origin/main | head -5)
echo "$LOG_PREFIX New commits detected:"
echo "$COMMITS" | sed "s/^/$LOG_PREFIX   /"

# ── Pull ─────────────────────────────────────────────────────────────────────
echo "$LOG_PREFIX Pulling origin/main..."
git pull origin main --quiet

NEW_SHA=$(git rev-parse --short HEAD)
echo "$LOG_PREFIX Now at $NEW_SHA"

# ── Rebuild and restart ───────────────────────────────────────────────────────
echo "$LOG_PREFIX Rebuilding Docker image and restarting container..."
docker compose down
docker compose up --build --remove-orphans -d

echo "$LOG_PREFIX Done — container restarted with $NEW_SHA"
