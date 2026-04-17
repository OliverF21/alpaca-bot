#!/usr/bin/env bash
#
# oracle_setup.sh — Bootstrap an Oracle Linux VM (OCI Free Tier) as a headless Alpaca Bot server.
#
# Runs the full run_all.py stack (equity scanner + crypto scanner + web dashboard)
# as a systemd service with auto-restart, plus a cron-driven git auto-deploy.
#
# What it does (idempotently — safe to re-run):
#   1. Installs system packages + Python 3.11 via dnf AppStream
#   2. Opens port 8000 in firewalld (Oracle Linux firewall — separate from Security List)
#   3. Clones https://github.com/OliverF21/alpaca-bot
#   4. Creates a venv and installs requirements.txt
#   5. Copies .env.example → .env if no .env exists (chmod 600)
#   6. Installs a systemd service that runs `python -u run_all.py`
#   7. Installs update.sh + cron job for auto-deploy on git push
#   8. Narrow sudoers rule so cron can restart the service without a password
#
# Usage (on the Oracle VM — default user is opc):
#   scp oracle_setup.sh opc@<vm-ip>:~
#   ssh opc@<vm-ip>
#   chmod +x oracle_setup.sh && ./oracle_setup.sh
#
# Before running: also open port 8000 in the Oracle Console → VCN → Security List
# (Ingress rule: TCP, source 0.0.0.0/0, destination port 8000)
# Both the Security List AND firewalld must allow the port.
#
# Optional overrides:
#   REPO_URL=https://github.com/OliverF21/alpaca-bot.git
#   BRANCH=main
#   INSTALL_DIR=$HOME/alpaca-bot
#   SERVICE_NAME=alpaca-bot
#   BOT_CMD="run_all.py"             # e.g. "run_all.py --no-crypto"
#   UPDATE_INTERVAL_MIN=5

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/OliverF21/alpaca-bot.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/alpaca-bot}"
SERVICE_NAME="${SERVICE_NAME:-alpaca-bot}"
BOT_CMD="${BOT_CMD:-run_all.py}"
UPDATE_INTERVAL_MIN="${UPDATE_INTERVAL_MIN:-5}"
RUN_USER="$(whoami)"

echo "== Alpaca Bot Oracle Linux setup =="
echo "  User:        $RUN_USER"
echo "  Install dir: $INSTALL_DIR"
echo "  Repo:        $REPO_URL ($BRANCH)"
echo "  Service:     $SERVICE_NAME"
echo "  Bot command: python -u $BOT_CMD"
echo "  Auto-update: every ${UPDATE_INTERVAL_MIN} min"
echo

# ── 1. System packages + Python 3.11 ─────────────────────────────────────────
echo "[1/8] Installing system packages and Python 3.11..."
sudo dnf install -y -q \
    python3.11 python3.11-devel \
    python3-pip \
    git gcc gcc-c++ make \
    libffi-devel openssl-devel

# ── 2. firewalld — open port 8000 ─────────────────────────────────────────────
echo "[2/8] Opening port 8000 in firewalld..."
sudo firewall-cmd --zone=public --add-port=8000/tcp --permanent --quiet
sudo firewall-cmd --reload --quiet

# ── 3. Clone or update the repo ───────────────────────────────────────────────
echo "[3/8] Cloning/updating repo..."
if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" fetch --quiet
    git -C "$INSTALL_DIR" checkout --quiet "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only --quiet
else
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# ── 4. Virtualenv + deps ──────────────────────────────────────────────────────
echo "[4/8] Setting up venv..."
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    python3.11 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel -q
echo "  Installing requirements.txt..."
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

# ── 5. .env ───────────────────────────────────────────────────────────────────
echo "[5/8] Checking .env..."
ENV_NEEDS_FILL=0
if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    ENV_NEEDS_FILL=1
    echo "  ! Copied .env.example -> .env — fill in real Alpaca keys before starting."
fi
chmod 600 "$INSTALL_DIR/.env"

# ── 6. systemd unit ───────────────────────────────────────────────────────────
echo "[6/8] Installing systemd service..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=Alpaca Bot ($SERVICE_NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/python -u $BOT_CMD
Restart=always
RestartSec=15
TimeoutStopSec=30
KillMode=mixed

ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$INSTALL_DIR /tmp
PrivateTmp=false
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --quiet "$SERVICE_NAME"

# ── 7. update.sh + sudoers + cron ─────────────────────────────────────────────
echo "[7/8] Installing auto-update..."

cat > "$INSTALL_DIR/update.sh" <<'UPDATEEOF'
#!/bin/bash
# Auto-deploy: pulls __BRANCH__ and restarts __SERVICE_NAME__ if anything changed.
set -e
cd "__INSTALL_DIR__"

git fetch --quiet
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "[$(date -Is)] Updating: $LOCAL -> $REMOTE"
git pull --quiet --ff-only

if git diff --name-only "$LOCAL" "$REMOTE" | grep -qx 'requirements.txt'; then
    echo "[$(date -Is)] requirements.txt changed, reinstalling deps"
    "__INSTALL_DIR__/.venv/bin/pip" install -q -r "__INSTALL_DIR__/requirements.txt"
fi

echo "[$(date -Is)] Restarting __SERVICE_NAME__"
sudo systemctl restart __SERVICE_NAME__
UPDATEEOF

sed -i \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__SERVICE_NAME__|$SERVICE_NAME|g" \
    -e "s|__BRANCH__|$BRANCH|g" \
    "$INSTALL_DIR/update.sh"
chmod +x "$INSTALL_DIR/update.sh"

sudo tee "/etc/sudoers.d/${SERVICE_NAME}-restart" > /dev/null <<EOF
$RUN_USER ALL=(root) NOPASSWD: /bin/systemctl restart $SERVICE_NAME
EOF
sudo chmod 440 "/etc/sudoers.d/${SERVICE_NAME}-restart"

CRON_LINE="*/${UPDATE_INTERVAL_MIN} * * * * $INSTALL_DIR/update.sh >> $INSTALL_DIR/update.log 2>&1"
( crontab -l 2>/dev/null | grep -vF "$INSTALL_DIR/update.sh" ; echo "$CRON_LINE" ) | crontab -

# ── 8. Done ───────────────────────────────────────────────────────────────────
echo "[8/8] Done."
echo
if [ "$ENV_NEEDS_FILL" = "1" ]; then
    echo "  >> NEXT STEPS:"
    echo "     1. Fill in secrets:  nano $INSTALL_DIR/.env"
    echo "     2. Start the bot:    sudo systemctl start $SERVICE_NAME"
else
    sudo systemctl restart "$SERVICE_NAME"
    echo "  Service restarted."
fi
echo
echo "  Status:    systemctl status $SERVICE_NAME"
echo "  Logs:      journalctl -u $SERVICE_NAME -f"
echo "  Bot logs:  tail -f $INSTALL_DIR/logs/*.log"
echo "  Updates:   tail -f $INSTALL_DIR/update.log"
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8000"
