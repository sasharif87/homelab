#!/bin/bash
# Staged Docker startup — run after VM boot
LOG=/var/log/staged-startup.log

# Signatures of containerd image-store corruption from an unclean VM stop.
CORRUPTION_RE='blob not found|unable to prepare extraction snapshot|RWLayer is unexpectedly nil|failed to get reader from content store'

source /etc/container-watchdog.env 2>/dev/null || true

notify() {
    [ -z "${NTFY_URL:-}" ] && return 0
    curl -fsS -u "${NTFY_USER}:${NTFY_PASS}" \
        -H "Priority: ${2:-high}" -d "$1" \
        "${NTFY_URL}/${NTFY_TOPIC}" > /dev/null 2>&1 || true
}

cd /mnt/apps/compose || exit 1

# --- Pre-flight containerd health check ---
# Probe Docker/containerd before attempting any compose up.  When the image
# store is corrupt (unclean VM shutdown, NVMe hiccup) even basic commands like
# `docker images` emit the same blob/snapshot errors we catch later — but
# waiting until individual services fail wastes 10+ minutes of retries.
preflight_ok=true
if ! preflight_out=$(docker info 2>&1); then
    echo "PRE-FLIGHT: docker info failed"
    preflight_ok=false
elif echo "$preflight_out" | grep -qiE "$CORRUPTION_RE"; then
    echo "PRE-FLIGHT: docker info shows corruption signatures"
    preflight_ok=false
fi

if $preflight_ok; then
    if ! preflight_out=$(docker images --no-trunc 2>&1) \
       || echo "$preflight_out" | grep -qiE "$CORRUPTION_RE"; then
        echo "PRE-FLIGHT: docker images shows corruption"
        preflight_ok=false
    fi
fi

if ! $preflight_ok; then
    echo "=== Pre-flight corruption detected $(date) — running docker-recover.sh ===" >> $LOG
    notify "staged-startup: pre-flight detected containerd corruption — wiping before startup" high
    /opt/scripts/docker-recover.sh >> $LOG 2>&1
    echo "=== Pre-flight recovery complete $(date) ===" >> $LOG
fi

up() {
    local file=$1 out rc attempt
    for attempt in 1 2 3; do
        out=$(docker compose -f "$file" up -d 2>&1)
        rc=$?
        echo "$out"
        if [ $rc -eq 0 ]; then echo "  OK: $file"; return 0; fi
        if echo "$out" | grep -qiE "$CORRUPTION_RE"; then
            if echo "$out" | grep -qiE 'RWLayer is unexpectedly nil'; then
                # Level 1: containers exist but their layers are corrupt.
                # Force-remove them so the next attempt recreates cleanly.
                echo "  REMOVING corrupt containers for $file"
                docker compose -f "$file" rm -f 2>/dev/null
            fi
            if grep -q 'build:' "$file"; then
                echo "  REBUILD: $file (broken build cache)"
                docker builder prune -af > /dev/null 2>&1
                docker compose -f "$file" build --no-cache 2>&1 | tail -3
            else
                # Pulled image with missing blobs: needs the recovery wipe,
                # retrying the same pull just fails again.
                echo "  FAIL: $file (corruption signature — needs recovery, not retry)"
                return 1
            fi
        fi
        [ $attempt -lt 3 ] && { echo "  RETRY: $file (attempt $attempt failed)"; sleep 15; }
    done
    echo "  FAIL: $file (after 3 attempts)"
    return 1
}

run_stages() {
    echo "1. Infrastructure"
    up npm.yml; up portainer.yml; up vaultwarden.yml
    up uptime-kuma.yml; up homarr.yml; up duplicati.yml; up adguard.yml
    up dozzle.yml; up watchtower.yml; up ntfy.yml; up forgejo.yml; up crowdsec.yml
    up observability.yml; up postgres.yml
    sleep 10

    echo "2. Media"
    up jellyfin.yml; up audiobookshelf.yml
    up calibre.yml; up calibre-web.yml; up homebox.yml; up navidrome.yml
    # Metadata provider for both bookshelf instances (stage 5, gluetun.yml) --
    # must be up before them. Brings its own Postgres with a healthcheck, so
    # compose handles the internal ordering.
    up rreading-glasses.yml
    sleep 10

    echo "3. Knowledge"
    up knowledge.yml
    sleep 5

    echo "4. Immich"
    up immich.yml
    sleep 10

    echo "5. VPN stack"
    up gluetun.yml
    sleep 15

    echo "6. AI"
    up ollama.yml; up qdrant.yml; up open-webui.yml

    echo "7. Tools"
    up profilarr.yml; up tubearchivist.yml; up samba.yml; up speedtest-tracker.yml
}

echo "=== Staged startup $(date) ===" >> $LOG

TMP=$(mktemp)
run_stages > "$TMP" 2>&1
cat "$TMP" >> $LOG

# Recover only when corruption actually left something broken — a corruption
# signature from a service the rebuild fallback already fixed must not
# trigger a full store wipe.
if grep -q '^  FAIL:' "$TMP" && grep -qiE "$CORRUPTION_RE" "$TMP"; then
    echo "=== Corruption detected $(date) — running docker-recover.sh ===" >> $LOG
    /opt/scripts/docker-recover.sh >> $LOG 2>&1
    echo "=== Rebuilding after recovery $(date) ===" >> $LOG
    run_stages > "$TMP" 2>&1
    cat "$TMP" >> $LOG
fi

FAILED=$(grep '^  FAIL:' "$TMP" | awk '{print $2}' | tr '\n' ' ')
if [ -n "$FAILED" ]; then
    notify "staged-startup: compose files still failing after retries: $FAILED" high
fi
rm -f "$TMP"

echo "=== Done $(date) ===" >> $LOG
