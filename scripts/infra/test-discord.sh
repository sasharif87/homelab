#!/bin/bash
# Smoke-tests Discord notifications for all watchdog/maintenance scripts.
# Run on the server to verify the webhook is wired correctly before relying on it.
# Usage: ./test-discord.sh [--all | preflight | maintenance | boot | watchdog]
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

SCOPE="${1:---all}"

notify() {
    local msg="$1"
    local color="${2:-5793266}"
    DISCORD_MSG="$msg" DISCORD_COLOR="$color" python3 -c "
import json, os, subprocess
payload = json.dumps({
    'embeds': [{'description': os.environ['DISCORD_MSG'], 'color': int(os.environ['DISCORD_COLOR'])}]
})
r = subprocess.run(
    ['curl', '-sS', '-o', '/dev/null', '-w', '%{http_code}',
     '-X', 'POST', os.environ.get('DISCORD_WEBHOOK_URL', ''),
     '-H', 'Content-Type: application/json', '-d', payload],
    capture_output=True, text=True, timeout=10
)
code = r.stdout.strip()
print(f'  HTTP {code}', '✓' if code == '204' else '✗ UNEXPECTED')
"
}

echo "=== Discord webhook smoke test ==="
echo "Webhook: ${DISCORD_WEBHOOK_URL:0:50}..."
echo ""

run_all=false
[ "$SCOPE" = "--all" ] && run_all=true

# --- Preflight alerts ---
if $run_all || [ "$SCOPE" = "preflight" ]; then
    echo "[preflight] NVIDIA driver update detected (driver path)..."
    notify $':warning: **[TEST] NVIDIA driver update pending.** Rebooting at 4:30 AM to reload kernel module.\n\n```\n  libnvidia-gl-535\n  nvidia-utils-535\n  nvidia-container-toolkit\n```' 16763904

    echo "[preflight] Kernel update detected (kernel path)..."
    notify $':rotating_light: **[TEST] Kernel update pending.** Will apply packages then reboot at 4:30 AM.\n\n```\n  linux-image-6.8.0-60-generic\n  linux-modules-6.8.0-60-generic\n```' 16711680
fi

# --- Maintenance reboot alerts ---
if $run_all || [ "$SCOPE" = "maintenance" ]; then
    echo "[maintenance] Kernel maintenance starting..."
    notify ':construction: **[TEST] Kernel maintenance starting.** Installing packages, then rebooting.' 16763904

    echo "[maintenance] Packages installed, rebooting..."
    notify ':white_check_mark: **[TEST]** Kernel packages installed. Rebooting now - boot-check will report container status.' 2244095

    echo "[maintenance] Package install failed..."
    notify ':skull: **[TEST] Package install failed** (exit 1). Aborting reboot - manual intervention required.' 16711680

    echo "[vm-reboot] Rebooting..."
    notify ':construction: **[TEST] Services VM rebooting.** NVML driver fix reboot.' 16763904
fi

# --- Boot-check alerts ---
if $run_all || [ "$SCOPE" = "boot" ]; then
    echo "[boot-check] All containers up..."
    notify ':white_check_mark: **[TEST] VM back online.** All 24 containers running.' 2244095

    echo "[boot-check] Some containers failed..."
    notify $':warning: **[TEST] VM back online.** 22/24 containers running.\n\n**Not running:**\n`jellyfin` - exited\n`ollama` - created\n\nWatchdog will retry automatically.' 16763904
fi

# --- Watchdog alerts ---
if $run_all || [ "$SCOPE" = "watchdog" ]; then
    echo "[watchdog] Container went down..."
    notify $':warning: **[TEST] jellyfin** went down (exit 1).\nRestarting in 30s... (attempt 1/3)' 16763904

    echo "[watchdog] Container restarted successfully..."
    notify ':white_check_mark: **[TEST] jellyfin** restarted successfully (attempt 1/3).' 2244095

    echo "[watchdog] All retries exhausted..."
    notify $':skull: **[TEST] jellyfin** failed to restart after 3 attempts.\nManual intervention required.' 16711680

    echo "[watchdog] OOM kill..."
    notify $':skull: **[TEST] immich-machine-learning** was OOM killed.\nConsider adding `mem_limit` to its compose file.' 16711680
fi

echo ""
echo "=== Done. Check Discord for the test messages. ==="
