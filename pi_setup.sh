#!/usr/bin/env bash
#
# pi_setup.sh — Bootstrap a Raspberry Pi as a headless Alpaca Bot server.
#
# Runs the full run_all.py stack (equity scanner + crypto scanner + web dashboard)
# as a systemd service with auto-restart, plus a cron-driven git auto-deploy.
#
# What it does (idempotently — safe to re-run):
#   1. Installs system packages, then reclaims disk space (apt clean,
#      autoremove old kernels, vacuum journal) — often 200-500 MB on a Pi
#      that was just `apt upgrade`d
#   2. Creates a 1 GB swapfile if none exists (Pi 2 has 1 GB RAM — full stack
#      will OOM without it)
#   3. Clones https://github.com/OliverF21/alpaca-bot
#   4. Creates a venv and installs requirements-pi.txt (runtime subset —
#      drops streamlit/hyperopt/mplfinance/plotly/pytest, saves ~200+ MB).
#      If a prior venv was built from a different requirements file, rebuilds
#      it from scratch to drop stale packages. Purges pip's wheel cache after.
#   5. Copies .env.example → .env if no .env exists (chmod 600)
#   6. Installs a hardened systemd service that runs `python -u run_all.py`
#   7. Installs update.sh + cron job for auto-deploy on git push to $BRANCH
#   8. Narrow sudoers rule so cron can restart the service without a password
#
# Usage (on the Pi):
#   scp pi_setup.sh pi@raspberrypi.local:~
#   ssh pi@raspberrypi.local
#   ./pi_setup.sh
#
# Optional overrides:
#   REPO_URL=https://github.com/OliverF21/alpaca-bot.git
#   BRANCH=main                      # recommend a 'deploy' branch for discipline
#   INSTALL_DIR=$HOME/alpaca-bot
#   SERVICE_NAME=alpaca-bot
#   BOT_CMD="run_all.py"             # default: full stack. Pass flags here,
#                                    # e.g. "run_all.py --no-web --no-equity"
#   UPDATE_INTERVAL_MIN=5
#   SWAP_SIZE_MB=1024                # set to 0 to skip swap creation

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_URL="${REPO_URL:-https://github.com/OliverF21/alpaca-bot.git}"
BRANCH="${BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/alpaca-bot}"
SERVICE_NAME="${SERVICE_NAME:-alpaca-bot}"
BOT_CMD="${BOT_CMD:-run_all.py}"
UPDATE_INTERVAL_MIN="${UPDATE_INTERVAL_MIN:-5}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-1024}"
RUN_USER="$(whoami)"

echo "== Alpaca Bot Pi setup =="
echo "  User:        $RUN_USER"
echo "  Install dir: $INSTALL_DIR"
echo "  Repo:        $REPO_URL ($BRANCH)"
echo "  Service:     $SERVICE_NAME"
echo "  Bot command: python -u $BOT_CMD"
echo "  Swap:        ${SWAP_SIZE_MB} MB"
echo "  Auto-update: every ${UPDATE_INTERVAL_MIN} min"
echo

# ── 1. System packages ────────────────────────────────────────────────────────
echo "[1/8] Installing system packages..."
sudo apt-get update -qq
# Trixie ships Python 3.13 as the default, and piwheels builds armv7 wheels
# against each Debian release's default Python — so pandas/numpy/pandas-ta
# should install as prebuilt wheels. build-essential + libopenblas-dev are
# only a safety net in case any sdist falls through (and cheap vs. the alternative).
sudo apt-get install -y -qq \
    python3-venv python3-pip python3-dev \
    git build-essential \
    libopenblas-dev gfortran \
    libffi-dev libssl-dev

# Disk cleanup — critical on small SD cards. Removes:
#   - downloaded .deb archives (apt clean, usually 100-300 MB after an upgrade)
#   - old kernels and orphaned packages from previous upgrades (autoremove)
#   - systemd journal beyond 50 MB
echo "  Cleaning apt caches and old kernels..."
sudo apt-get clean
sudo apt-get autoremove --purge -y -qq
sudo journalctl --vacuum-size=50M >/dev/null 2>&1 || true
df -h / | awk 'NR==2 {print "  Disk: "$4" free ("$5" used)"}'

# ── 2. Swap ───────────────────────────────────────────────────────────────────
echo "[2/8] Checking swap..."
CURRENT_SWAP_MB=$(free -m | awk '/^Swap:/ {print $2}')
if [ "$SWAP_SIZE_MB" -gt 0 ] && [ "$CURRENT_SWAP_MB" -lt "$SWAP_SIZE_MB" ]; then
    echo "  Current swap: ${CURRENT_SWAP_MB} MB, target: ${SWAP_SIZE_MB} MB"
    if [ -f /etc/dphys-swapfile ]; then
        # Pi OS default: dphys-swapfile manages /var/swap
        sudo dphys-swapfile swapoff || true
        sudo sed -i "s/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=${SWAP_SIZE_MB}/" /etc/dphys-swapfile
        sudo dphys-swapfile setup
        sudo dphys-swapfile swapon
    else
        # Fallback: plain swapfile at /swapfile
        if [ ! -f /swapfile ]; then
            sudo fallocate -l "${SWAP_SIZE_MB}M" /swapfile || \
                sudo dd if=/dev/zero of=/swapfile bs=1M count="$SWAP_SIZE_MB"
            sudo chmod 600 /swapfile
            sudo mkswap /swapfile
            sudo swapon /swapfile
            grep -q '^/swapfile' /etc/fstab || \
                echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
        fi
    fi
    echo "  Swap now: $(free -m | awk '/^Swap:/ {print $2}') MB"
else
    echo "  Swap OK (${CURRENT_SWAP_MB} MB)"
fi

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

# Pick the requirements file we'll install from — prefer the Pi subset, fall
# back to the full file for older checkouts that don't have it yet.
if [ -f "$INSTALL_DIR/requirements-pi.txt" ]; then
    REQ_FILE="requirements-pi.txt"
else
    REQ_FILE="requirements.txt"
    echo "  ! requirements-pi.txt not found; falling back to full requirements.txt"
fi

# If the venv exists but was built from a different requirements file, nuke
# and rebuild. This is how we drop stale packages (streamlit, hyperopt, etc.)
# from a previous install that used the full requirements.txt — pip install
# on its own never removes anything, only adds.
VENV_MARKER="$INSTALL_DIR/.venv/.pi-setup-reqfile"
if [ -d "$INSTALL_DIR/.venv" ]; then
    PREV_REQ=""
    [ -f "$VENV_MARKER" ] && PREV_REQ="$(cat "$VENV_MARKER")"
    if [ "$PREV_REQ" != "$REQ_FILE" ]; then
        echo "  venv was built from '${PREV_REQ:-unknown}', rebuilding for '$REQ_FILE'"
        rm -rf "$INSTALL_DIR/.venv"
    fi
fi

if [ ! -d "$INSTALL_DIR/.venv" ]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip wheel

echo "  Installing $REQ_FILE..."
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/$REQ_FILE"

# pandas-ta 0.4+ hard-depends on numba/llvmlite which has no armv7 wheel and
# won't build from source on Pi. Install it WITHOUT its deps — the strategies
# only use pure-pandas indicators (bbands, rsi, sma, ema, adx, supertrend,
# donchian) which work fine without numba. sitecustomize.py sets
# NUMBA_DISABLE_JIT=1 as an extra safety net.
echo "  Installing pandas-ta (--no-deps, skipping numba/llvmlite)..."
"$INSTALL_DIR/.venv/bin/pip" install --no-deps pandas-ta

# Record which requirements file this venv matches, and drop the wheel cache
# that pip accumulates during install (can be 100-300 MB — pure reclaimable).
echo "$REQ_FILE" > "$VENV_MARKER"
"$INSTALL_DIR/.venv/bin/pip" cache purge >/dev/null 2>&1 || true

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
# run_all.py spawns 3 child processes; give them time to come down cleanly
TimeoutStopSec=30
KillMode=mixed

# Hardening — note: NO MemoryMax. run_all.py runs equity scanner + crypto
# scanner + FastAPI dashboard; on a Pi 2 the working set is already close to
# the RAM limit and a cgroup cap would OOM-kill it constantly.
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

# Quoted heredoc avoids $(...) escaping; placeholders are substituted afterwards.
cat > "$INSTALL_DIR/update.sh" <<'UPDATEEOF'
#!/bin/bash
# Auto-deploy: pulls __BRANCH__ and restarts __SERVICE_NAME__ if anything changed.
# Installed by pi_setup.sh. Runs from cron every __INTERVAL__ min.
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

# Reinstall deps if the requirements file this venv was built from changed.
if git diff --name-only "$LOCAL" "$REMOTE" | grep -qx '__REQ_FILE__'; then
    echo "[$(date -Is)] __REQ_FILE__ changed, reinstalling deps"
    "__INSTALL_DIR__/.venv/bin/pip" install -q -r "__REQ_FILE__"
    "__INSTALL_DIR__/.venv/bin/pip" cache purge >/dev/null 2>&1 || true
fi

echo "[$(date -Is)] Restarting __SERVICE_NAME__"
sudo systemctl restart __SERVICE_NAME__
UPDATEEOF

sed -i \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    -e "s|__SERVICE_NAME__|$SERVICE_NAME|g" \
    -e "s|__BRANCH__|$BRANCH|g" \
    -e "s|__INTERVAL__|$UPDATE_INTERVAL_MIN|g" \
    -e "s|__REQ_FILE__|$REQ_FILE|g" \
    "$INSTALL_DIR/update.sh"
chmod +x "$INSTALL_DIR/update.sh"

# Narrow sudoers rule: user can restart ONLY this one service, no password.
sudo tee "/etc/sudoers.d/${SERVICE_NAME}-restart" > /dev/null <<EOF
$RUN_USER ALL=(root) NOPASSWD: /bin/systemctl restart $SERVICE_NAME
EOF
sudo chmod 440 "/etc/sudoers.d/${SERVICE_NAME}-restart"

# Install cron entry (idempotent: strip any prior line for this update.sh first)
CRON_LINE="*/${UPDATE_INTERVAL_MIN} * * * * $INSTALL_DIR/update.sh >> $INSTALL_DIR/update.log 2>&1"
( crontab -l 2>/dev/null | grep -vF "$INSTALL_DIR/update.sh" ; echo "$CRON_LINE" ) | crontab -

# ── 8. Done ───────────────────────────────────────────────────────────────────
echo "[8/8] Done."
echo
if [ "$ENV_NEEDS_FILL" = "1" ]; then
    echo "  >> NEXT STEPS:"
    echo "     1. Edit secrets:  nano $INSTALL_DIR/.env"
    echo "     2. Start the bot: sudo systemctl start $SERVICE_NAME"
else
    sudo systemctl restart "$SERVICE_NAME"
    echo "  Service restarted with latest code."
fi
echo
echo "  Status:    systemctl status $SERVICE_NAME"
echo "  Logs:      journalctl -u $SERVICE_NAME -f"
echo "  Bot logs:  tail -f $INSTALL_DIR/logs/*.log"
echo "  Updates:   tail -f $INSTALL_DIR/update.log"
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8000"
