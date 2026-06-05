#!/bin/bash
# Pre-flight check — runs Sunday 3:30 AM and 3:50 AM (backup).
# Detects pending NVIDIA driver or kernel updates, alerts Discord, and schedules
# maintenance-reboot.sh via `at 04:30` so Watchtower finishes first.
#
# Writes /run/preflight-packages (package list) and /run/preflight-reboot-scheduled
# (lock) so the backup run skips if a reboot is already queued. Both files live in
# tmpfs and clear on reboot — no stale state across weeks.
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

FLAG=/run/preflight-packages
LOCK=/run/preflight-reboot-scheduled

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

# --- Skip if a reboot is already scheduled this cycle ---
if [ -f "$LOCK" ]; then
    echo "Reboot already scheduled this cycle — skipping"
    exit 0
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
    MSG=$(printf ':rotating_light: **Kernel update pending.** Will apply packages then reboot at 4:30 AM CDT.\n\n```\n%s\n```' "$PKG_BLOCK")
    COLOR=16711680  # red
else
    PATH_TYPE="nvidia"
    MSG=$(printf ':warning: **NVIDIA driver update pending.** Rebooting at 4:30 AM CDT to reload kernel module.\n\n```\n%s\n```' "$PKG_BLOCK")
    COLOR=16763904  # orange
fi

notify_discord "$MSG" "$COLOR"
notify_ntfy "$MSG"
echo "Discord + ntfy notified — $PATH_TYPE path, packages: $(echo "$PACKAGES" | tr '\n' ' ')"

# --- Schedule maintenance reboot at 4:30 AM CDT (09:30 UTC — VM runs UTC) ---
echo "/opt/scripts/maintenance-reboot.sh" | at 09:30
echo "Maintenance reboot scheduled for 4:30 AM CDT (09:30 UTC)"
