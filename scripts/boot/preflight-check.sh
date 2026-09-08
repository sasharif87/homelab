#!/bin/bash
# Pre-flight check — runs Sunday 3:30 AM and 3:50 AM (backup).
# Detects pending NVIDIA driver or kernel updates and alerts Discord/ntfy.
#
# NOTIFY-ONLY as of 2026-07-25: auto-install + auto-reboot are disabled
# (unattended-upgrades installed an NVIDIA driver without rebooting on
# 2026-07-24 and desynced the kernel module from userspace). Updates are
# applied by hand via maintenance-reboot.sh once someone reviews the alert.
#
# Writes /run/preflight-packages (package list) and /run/preflight-reboot-scheduled
# (lock). The lock's only job is to stop the 3:50 AM backup run from re-notifying
# the same morning — it is NOT a cross-week gate. It lives in tmpfs and would
# normally clear on reboot, but with auto-reboot disabled the VM can go weeks
# without one, so a lock older than LOCK_TTL is treated as stale and cleared
# instead of silently suppressing every future run (this is exactly what
# happened Aug 9-23 2026: one stale lock silenced three consecutive weeks).
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

FLAG=/run/preflight-packages
LOCK=/run/preflight-reboot-scheduled
LOCK_TTL=21600  # 6 hours — same-morning dedupe only, not a cross-week gate

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

# --- Skip only if this cycle already notified (fresh lock); clear stale ones ---
if [ -f "$LOCK" ]; then
    LOCK_AGE=$(( $(date +%s) - $(stat -c %Y "$LOCK") ))
    if [ "$LOCK_AGE" -lt "$LOCK_TTL" ]; then
        echo "Reboot already scheduled this cycle — skipping"
        exit 0
    fi
    echo "Stale lock from a previous cycle ($((LOCK_AGE / 3600))h old) — clearing and re-checking"
    rm -f "$LOCK"
fi

# --- Refresh package index (needs internet) ---
if ! apt-get update -qq 2>/dev/null; then
    echo "apt-get update failed — network may be down, backup run will retry"
    exit 1
fi

# --- Find pending driver/kernel packages ---
PACKAGES=$(apt list --upgradable 2>/dev/null \
    | grep -E "^(nvidia|libnvidia|linux-image|linux-modules|nvidia-container-toolkit)" \
    | awk -F/ '{print $1}' || true)

if [ -z "$PACKAGES" ]; then
    echo "No driver or kernel updates pending"
    exit 0
fi

# --- Write package list and lock ---
echo "$PACKAGES" > "$FLAG"
touch "$LOCK"

# --- Determine path and build Discord message ---
PKG_BLOCK=$(echo "$PACKAGES" | sed 's/^/  /')
if echo "$PACKAGES" | grep -qE "^linux-(image|modules)"; then
    PATH_TYPE="kernel"
    MSG=$(printf ':rotating_light: **Kernel update pending.** MANUAL ACTION REQUIRED - nothing is installed automatically.\n\n```\n%s\n```' "$PKG_BLOCK")
    COLOR=16711680  # red
else
    PATH_TYPE="nvidia"
    MSG=$(printf ':warning: **NVIDIA driver update pending.** MANUAL ACTION REQUIRED - install then reboot to reload the kernel module.\n\n```\n%s\n```' "$PKG_BLOCK")
    COLOR=16763904  # orange
fi

notify_discord "$MSG" "$COLOR"
notify_ntfy "$MSG"
echo "Discord + ntfy notified — $PATH_TYPE path, packages: $(echo "$PACKAGES" | tr '\n' ' ')"

# --- Schedule maintenance reboot at 4:30 AM CDT (09:30 UTC — VM runs UTC) ---
# NOTIFY-ONLY as of 2026-07-25. Auto-install + auto-reboot disabled -- updates
# are manual now (unattended-upgrades is off too; see 20auto-upgrades).
# To apply pending driver/kernel packages by hand, run:
#     /opt/scripts/maintenance-reboot.sh
# which still does the surgical install + pre_shutdown_cleanup + reboot.
# echo "/opt/scripts/maintenance-reboot.sh" | at 09:30
echo "Notify-only mode - no reboot scheduled. Apply manually: /opt/scripts/maintenance-reboot.sh"
