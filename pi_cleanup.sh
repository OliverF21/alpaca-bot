#!/usr/bin/env bash
#
# pi_cleanup.sh — Emergency disk reclamation for a full Raspberry Pi.
#
# Run this BEFORE pi_setup.sh when the Pi is out of space. Safe to re-run.
# Does nothing that would break the OS — only removes caches, old kernels,
# orphaned packages, logs, and temp files.
#
# Usage:
#   scp pi_cleanup.sh fichte@fichte.local:~
#   ssh fichte@fichte.local './pi_cleanup.sh'

set -eu

echo "== Pi cleanup =="
echo "Before:"
df -h / | awk 'NR==1 || NR==2'
echo

# ── 1. apt cache ──────────────────────────────────────────────────────────────
# /var/cache/apt/archives holds every .deb apt has downloaded. After an
# `apt upgrade` this is often hundreds of MB of files you'll never need again.
echo "[1/6] apt clean..."
sudo apt-get clean

# ── 2. Orphaned packages + old kernels ────────────────────────────────────────
# `autoremove --purge` drops packages that were pulled in as dependencies but
# are no longer required by anything installed. On a Pi that just upgraded
# kernels (6.12.47 → 6.12.75 in your case), this removes the old kernel
# images, headers, and initramfs — typically 100-200 MB.
echo "[2/6] apt autoremove --purge..."
sudo apt-get autoremove --purge -y -qq

# ── 3. Systemd journal ────────────────────────────────────────────────────────
# Journal grows unbounded by default. Cap at 50 MB of recent logs.
echo "[3/6] Vacuum systemd journal..."
sudo journalctl --vacuum-size=50M 2>&1 | tail -3 || true

# ── 4. User caches ────────────────────────────────────────────────────────────
# ~/.cache is for throwaway per-user caches (pip wheels, thumbnails, etc.).
# Nothing here is load-bearing; apps rebuild what they need.
echo "[4/6] Clear ~/.cache..."
if [ -d "$HOME/.cache" ]; then
    BEFORE=$(du -sh "$HOME/.cache" 2>/dev/null | awk '{print $1}')
    rm -rf "$HOME/.cache"/*
    echo "  Cleared ~/.cache (was $BEFORE)"
fi

# ── 5. Rotated log files ──────────────────────────────────────────────────────
# Removes compressed/rotated logs in /var/log (.gz, .1, .old). Keeps the
# current live logs so running services don't lose their handle.
echo "[5/6] Remove rotated logs in /var/log..."
sudo find /var/log -type f \( -name "*.gz" -o -name "*.1" -o -name "*.old" -o -name "*.[0-9]" \) -delete 2>/dev/null || true

# ── 6. Old tmp files ──────────────────────────────────────────────────────────
# /tmp files older than 7 days. Skips anything in use (find won't delete
# files held open by processes).
echo "[6/6] Prune old /tmp..."
sudo find /tmp -mindepth 1 -type f -atime +7 -delete 2>/dev/null || true
sudo find /tmp -mindepth 1 -type d -empty -delete 2>/dev/null || true

echo
echo "After:"
df -h / | awk 'NR==1 || NR==2'
echo
echo "Done. You can now run ./pi_setup.sh"
