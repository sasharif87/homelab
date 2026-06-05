#!/bin/bash
# Maintenance reboot — called by `at 09:30` after preflight-check.sh detects
# pending updates. Reads /run/preflight-packages to decide which path to take:
#
#   Kernel path:  installs kernel + any nvidia packages surgically, then reboots.
#   Driver path:  installs nvidia packages surgically, then reboots.
#   No flag file: plain reboot (manual call fallback).
#
# Post-reboot: boot-check.service fires automatically via systemd (runs on every
# boot after docker.service) — waits 90s then posts per-container status to Discord.
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

FLAG=/run/preflight-packages

pre_shutdown_cleanup() {
    # Kill background heavy-I/O scripts to prevent containerd blob corruption
    pkill -f upload-rag-kiwix.py 2>/dev/null || true
    pkill -f kiwix-restart-watcher 2>/dev/null || true

    # Stop all containers gracefully (parallel, 60s per container) before
    # systemd gets to Docker — prevents mid-write SIGKILL of containerd
    local running
    running=$(docker ps -q 2>/dev/null)
    if [ -n "$running" ]; then
        docker stop --time 60 $running 2>/dev/null || true  # unquoted intentionally: word-split is desired here
    fi
    systemctl stop docker 2>/dev/null || true
    systemctl stop containerd 2>/dev/null || true
}

notify_ntfy() {
    local msg="$1"
    local priority="${2:-high}"
    [ -z "${NTFY_URL:-}" ] && return 0
    curl -fsS -u "${NTFY_USER}:${NTFY_PASS}" \
        -H "Priority: ${priority}" \
        -d "$msg" "${NTFY_URL}/${NTFY_TOPIC}" > /dev/null 2>&1 || true
}

notify_discord() {
    local msg="$1"
    local color="${2:-16763904}"
    DISCORD_MSG="$msg" DISCORD_COLOR="$color" python3 -c "
import json, os, subprocess
payload = json.dumps({
    'embeds': [{'description': os.environ['DISCORD_MSG'], 'color': int(os.environ['DISCORD_COLOR'])}]
})
subprocess.run(
    ['curl', '-sS', '-X', 'POST', os.environ.get('DISCORD_WEBHOOK_URL', ''),
     '-H', 'Content-Type: application/json', '-d', payload],
    timeout=10, capture_output=True
)
" || true
}

# --- No flag file: manual/fallback reboot ---
if [ ! -f "$FLAG" ]; then
    notify_discord ":construction: **Services VM rebooting.** Manual reboot." 16763904
    notify_ntfy "Services VM rebooting — manual reboot." default
    pre_shutdown_cleanup
    sleep 5
    /sbin/shutdown -r now "Manual reboot"
    exit 0
fi

PACKAGES=$(cat "$FLAG")
PKG_LIST=$(echo "$PACKAGES" | tr '\n' ' ')

# --- Determine path ---
if echo "$PACKAGES" | grep -qE "^linux-(image|modules)"; then
    PATH_TYPE="kernel"
else
    PATH_TYPE="nvidia"
fi

# --- Announce maintenance window ---
if [ "$PATH_TYPE" = "kernel" ]; then
    notify_discord ":construction: **Kernel maintenance starting.** Installing packages, then rebooting." 16763904
    notify_ntfy "Kernel maintenance starting — installing packages, then rebooting."
else
    notify_discord ":construction: **NVIDIA driver maintenance starting.** Installing packages, then rebooting." 16763904
    notify_ntfy "NVIDIA driver maintenance starting — installing packages, then rebooting."
fi

# --- Surgical package install (both paths) ---
echo "Installing: $PKG_LIST"
DEBIAN_FRONTEND=noninteractive apt-get install -y $PKG_LIST 2>&1 | tee /var/log/maintenance-reboot.log

INSTALL_EXIT=${PIPESTATUS[0]}
if [ "$INSTALL_EXIT" -ne 0 ]; then
    notify_discord ":skull: **Package install failed** (exit $INSTALL_EXIT). Aborting reboot — manual intervention required." 16711680
    notify_ntfy "Package install FAILED (exit $INSTALL_EXIT). Reboot aborted — manual intervention required." urgent
    exit 1
fi

# --- Confirm install, then reboot ---
if [ "$PATH_TYPE" = "kernel" ]; then
    notify_discord ":white_check_mark: Kernel packages installed. Rebooting now — boot-check will report container status." 2244095
    notify_ntfy "Kernel packages installed — rebooting now." default
else
    notify_discord ":white_check_mark: NVIDIA packages installed. Rebooting now — boot-check will report container status." 2244095
    notify_ntfy "NVIDIA packages installed — rebooting now." default
fi

pre_shutdown_cleanup
sleep 5
/sbin/shutdown -r now "Maintenance reboot ($PATH_TYPE)"
