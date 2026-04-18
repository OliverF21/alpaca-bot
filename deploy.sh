#!/usr/bin/env bash
#
# deploy.sh — Push local changes and deploy to the Oracle Cloud VM.
#
# Usage:
#   ./deploy.sh ubuntu@<vm-ip>        # push + deploy
#   ./deploy.sh ubuntu@<vm-ip> --logs # push + deploy + tail logs
#
# First time: run oracle_setup.sh on the VM first.

set -euo pipefail

VM="${1:-}"
if [[ -z "$VM" ]]; then
    echo "Usage: ./deploy.sh ubuntu@<vm-ip> [--logs]"
    exit 1
fi

LOGS="${2:-}"

echo "Pushing to origin..."
git push

echo "Deploying to $VM..."
ssh "$VM" "cd ~/alpaca-bot && git pull --ff-only && sudo systemctl restart alpaca-bot"

echo "Done. Bot restarted on $VM."
echo "  Dashboard: http://${VM##*@}:8000"

if [[ "$LOGS" == "--logs" ]]; then
    ssh "$VM" "journalctl -u alpaca-bot -f"
fi
