#!/usr/bin/env bash
# macos_setup.sh — Install Alpaca Bot as a macOS LaunchAgent (auto-restart).
#
# Usage:
#   ./macos_setup.sh              # install + start
#   ./macos_setup.sh uninstall    # stop + remove
#   ./macos_setup.sh status       # check if running
#
# The agent runs `run_all.py` under the project venv and restarts
# automatically on crash or login.  Logs go to logs/launchd*.log.
# See issue #8.

set -euo pipefail

LABEL="com.oliver.alpaca-bot"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="${REPO_DIR}/.venv/bin/python"
ENV_FILE="${REPO_DIR}/.env"

# ── Helpers ────────────────────────────────────────────────────────────────

red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

check_prereqs() {
    if [[ ! -x "$VENV_PYTHON" ]]; then
        red "ERROR: venv not found at $VENV_PYTHON"
        echo "  Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        exit 1
    fi
    if [[ ! -f "$ENV_FILE" ]]; then
        red "ERROR: .env not found at $ENV_FILE"
        exit 1
    fi
}

# ── Read .env into plist-compatible XML ────────────────────────────────────
# launchd has no EnvironmentFile — we inline key=value pairs from .env.

env_dict_xml() {
    echo "    <key>EnvironmentVariables</key>"
    echo "    <dict>"
    while IFS='=' read -r key value; do
        # Skip comments and blank lines
        [[ -z "$key" || "$key" =~ ^# ]] && continue
        # Strip surrounding quotes from value
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        echo "      <key>${key}</key>"
        echo "      <string>${value}</string>"
    done < "$ENV_FILE"
    echo "    </dict>"
}

# ── Install ────────────────────────────────────────────────────────────────

install() {
    check_prereqs
    mkdir -p "$(dirname "$PLIST")"
    mkdir -p "${REPO_DIR}/logs"

    bold "Writing $PLIST"
    cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${VENV_PYTHON}</string>
        <string>-u</string>
        <string>run_all.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>

$(env_dict_xml)

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>15</integer>

    <key>StandardOutPath</key>
    <string>${REPO_DIR}/logs/launchd_stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${REPO_DIR}/logs/launchd_stderr.log</string>
</dict>
</plist>
PLIST_EOF

    # Load (or reload) the agent
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"

    green "Installed and started ${LABEL}"
    echo "  Logs:   ${REPO_DIR}/logs/launchd_*.log"
    echo "  Status: ./macos_setup.sh status"
    echo "  Stop:   ./macos_setup.sh uninstall"
}

# ── Uninstall ──────────────────────────────────────────────────────────────

uninstall() {
    bold "Stopping ${LABEL}"
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    rm -f "$PLIST"
    green "Uninstalled ${LABEL}"
}

# ── Status ─────────────────────────────────────────────────────────────────

status() {
    if launchctl print "gui/$(id -u)/${LABEL}" &>/dev/null; then
        green "${LABEL} is loaded"
        launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | grep -E "state|pid|last exit"
    else
        red "${LABEL} is not loaded"
    fi
}

# ── Main ───────────────────────────────────────────────────────────────────

case "${1:-install}" in
    install)   install   ;;
    uninstall) uninstall ;;
    status)    status    ;;
    *)
        echo "Usage: $0 [install|uninstall|status]"
        exit 1
        ;;
esac
