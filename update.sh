#!/bin/bash
# Auto-deploy: pulls main and restarts alpaca-bot if anything changed.
# Installed by pi_setup.sh. Runs from cron every 5 min.
set -e
cd "/home/fichte/alpaca-bot"

git fetch --quiet
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "[$(date -Is)] Updating: $LOCAL -> $REMOTE"
git pull --quiet --ff-only

# Reinstall deps if the requirements file this venv was built from changed.
if git diff --name-only "$LOCAL" "$REMOTE" | grep -qx 'requirements-pi.txt'; then
    echo "[$(date -Is)] requirements-pi.txt changed, reinstalling deps"
    "/home/fichte/alpaca-bot/.venv/bin/pip" install -q -r "requirements-pi.txt"
    "/home/fichte/alpaca-bot/.venv/bin/pip" cache purge >/dev/null 2>&1 || true
fi

echo "[$(date -Is)] Restarting alpaca-bot"
sudo systemctl restart alpaca-bot
