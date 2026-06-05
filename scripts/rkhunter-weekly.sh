#!/bin/bash
# Weekly rootkit + system integrity scan via rkhunter.
# Saturday 4am CDT (after malware-weekly at 3am).
# Alerts ntfy on any warnings — exit 0 if clean.
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

notify_ntfy() {
    local title="$1" msg="$2" priority="${3:-default}"
    [[ -z "${NTFY_URL:-}" ]] && return 0
    curl -fsS -u "${NTFY_USER}:${NTFY_PASS}" \
        -H "Priority: $priority" \
        -H "Title: $title" \
        -d "$msg" "${NTFY_URL}/${NTFY_TOPIC}" >/dev/null 2>&1 || true
}

# Pull latest rkhunter data files before check
rkhunter --update --nocolors --skip-keypress 2>&1 | tail -3 || true

# Run check — report-warnings-only suppresses the clean-line noise
exit_code=0
output=$(rkhunter --check --nocolors --skip-keypress --report-warnings-only 2>&1) || exit_code=$?

if [[ $exit_code -ne 0 ]] || [[ -n "$output" ]]; then
    # Trim to first 20 warning lines so ntfy message stays readable
    warnings=$(echo "$output" | grep -i 'warning\|found\|possible' | head -20 || echo "$output" | head -20)
    msg="rkhunter warnings found:"$'\n'"$warnings"
    notify_ntfy "rkhunter — Warnings" "$msg" "high"
    echo "rkhunter-weekly: warnings detected — ntfy sent"
    exit 1
fi

echo "rkhunter-weekly: clean"
