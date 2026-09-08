#!/bin/bash
# Weekly container image vulnerability scan — runs Sunday 5am CDT, one hour after Watchtower.
# Scans all running container images for CRITICAL CVEs and alerts Discord if found.
# Requires: trivy installed natively (apt install trivy), /etc/container-watchdog.env for webhook.
#
# Exits 1 when findings or scan errors exist, so `systemctl status trivy-weekly` reflects
# reality instead of showing green with 28 vulnerable images.
set -euo pipefail

set -a; source /etc/container-watchdog.env; set +a

SEVERITY="CRITICAL"
FINDINGS=()
ERRORS=()

# Findings are split by whether a fixed version actually exists upstream. Only fixable
# CVEs are actionable — 166 unfixable criticals in a third-party image is a fact about
# that image, not a task, and paging on it weekly is what turned this alert into
# wallpaper. Unfixable counts are still tallied and reported as context, and land in the
# journal every run, so nothing is hidden — they just don't page on their own.
UNFIXABLE_IMAGES=0
UNFIXABLE_CVES=0

NL=$'\n'

# Trivy extracts every image layer to TMPDIR before analysing it. /tmp lives on the 30G
# root filesystem (87% full as of Aug 3 2026), which is nowhere near enough for a
# multi-GB CUDA image — open-webui:cuda failed with "no space left on device" and was
# reported only as a bare scan error because stderr was being discarded. Point scratch
# at the local docker zvol instead (885G, ~500G free). Override with TRIVY_TMPDIR.
TRIVY_TMP="${TRIVY_TMPDIR:-/mnt/docker-local/tmp/trivy}"
if mkdir -p "$TRIVY_TMP" 2>/dev/null; then
    export TMPDIR="$TRIVY_TMP"
    # Clear leftovers from a previous run that was killed before its own cleanup.
    rm -rf "${TRIVY_TMP:?}"/trivy-* 2>/dev/null || true
else
    echo "trivy-weekly: WARNING — cannot create $TRIVY_TMP, falling back to default TMPDIR" >&2
fi

# Separate from TMPDIR: the cache holds the vulnerability DB plus per-image analysis
# results and is *persistent*. It defaults to /root/.cache/trivy, i.e. the same 30G root
# filesystem, where it had grown to 2.6G. Netdata alerted at 96.1% during an
# open-webui:cuda scan on Aug 3 2026. Moving scratch alone does not fix that.
TRIVY_CACHE="${TRIVY_CACHEDIR:-/mnt/docker-local/tmp/trivy-cache}"
CACHE_ARG=()
if mkdir -p "$TRIVY_CACHE" 2>/dev/null; then
    CACHE_ARG=(--cache-dir "$TRIVY_CACHE")
else
    echo "trivy-weekly: WARNING — cannot create $TRIVY_CACHE, using default cache dir" >&2
fi

TMPERR=$(mktemp)
trap 'rm -f "$TMPERR"' EXIT

notify_ntfy() {
    local msg="$1"
    local priority="${2:-high}"
    [ -z "${NTFY_URL:-}" ] && return 0
    curl -fsS -u "${NTFY_USER}:${NTFY_PASS}" \
        -H "Priority: ${priority}" \
        -H "Title: Trivy Weekly" \
        -d "$msg" "${NTFY_URL}/${NTFY_TOPIC}" > /dev/null 2>&1 || true
}

notify_discord() {
    local msg="$1"
    local color="${2:-16711680}"
    [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return 0
    DISCORD_MSG="$msg" DISCORD_COLOR="$color" python3 -c "
import json, os, subprocess
# Discord caps embed descriptions at 4096 chars; an over-long POST fails silently.
desc = os.environ['DISCORD_MSG']
if len(desc) > 3900:
    desc = desc[:3900] + '\n… (truncated)'
payload = json.dumps({
    'embeds': [{'description': desc, 'color': int(os.environ['DISCORD_COLOR'])}]
})
subprocess.run(
    ['curl', '-sS', '-X', 'POST', os.environ.get('DISCORD_WEBHOOK_URL', ''),
     '-H', 'Content-Type: application/json', '-d', payload],
    timeout=10, capture_output=True
)
" || true
}

if ! command -v trivy > /dev/null 2>&1; then
    MSG=":shield: **Trivy Weekly Scan**${NL}:warning: **trivy binary not found** — scan did not run.${NL}Reinstall: \`apt install trivy\`"
    notify_discord "$MSG" 16711680
    notify_ntfy "$MSG"
    echo "trivy-weekly: trivy not installed — aborting" >&2
    exit 1
fi

IMAGES=$(docker ps --format '{{.Image}}' | sort -u)

if [ -z "$IMAGES" ]; then
    echo "trivy-weekly: no running containers"
    exit 0
fi

IMAGE_COUNT=$(echo "$IMAGES" | wc -l)
echo "trivy-weekly: scanning $IMAGE_COUNT images for $SEVERITY CVEs..."

# Update the vulnerability DB once before scanning. trivy-db is pulled from GHCR and is
# rate-limited for anonymous clients — if this fails, every scan below runs --skip-db-update
# against a stale or absent DB and reports a false clean. Abort loudly instead.
if ! trivy image "${CACHE_ARG[@]}" --download-db-only --quiet 2> "$TMPERR"; then
    DB_ERR=$(grep -v '^$' "$TMPERR" | tail -2 | tr '\n' ' ' | cut -c1-200)
    MSG=":shield: **Trivy Weekly Scan**${NL}:warning: **Aborted — vulnerability DB update failed.**${NL}${NL}Scanning against a stale or missing DB would report a false clean, so no images were scanned.${NL}\`\`\`${NL}${DB_ERR}${NL}\`\`\`"
    notify_discord "$MSG" 16711680
    notify_ntfy "$MSG"
    echo "trivy-weekly: DB update failed — aborting ($DB_ERR)" >&2
    exit 1
fi

while IFS= read -r image; do
    # stdout and exit status are captured separately. Piping trivy straight into python
    # under `pipefail` silently loses images: if trivy emits complete JSON *and then*
    # exits non-zero, `|| echo error` appends to the already-printed count, the result
    # parses as neither a number nor "error", and the image drops out of both lists.
    rc=0
    json_out=$(trivy image \
        "${CACHE_ARG[@]}" \
        --severity "$SEVERITY" \
        --skip-db-update \
        --quiet \
        --format json \
        "$image" 2> "$TMPERR" < /dev/null) || rc=$?

    # Docker on this host uses the containerd snapshotter image store, whose image
    # export can omit blobs that the manifest references ("not found in tar"). Trivy's
    # default daemon path then fails for that image no matter how often it is re-pulled
    # — grafana/loki:latest failed this way every week. Pulling the image straight from
    # the registry sidesteps the local export entirely, so retry once that way before
    # calling it an error.
    if [ "$rc" -ne 0 ] && grep -q 'not found in tar' "$TMPERR"; then
        echo "trivy-weekly: $image — daemon export incomplete, retrying via registry"
        rc=0
        json_out=$(trivy image \
            "${CACHE_ARG[@]}" \
            --image-src remote \
            --severity "$SEVERITY" \
            --skip-db-update \
            --quiet \
            --format json \
            "$image" 2> "$TMPERR" < /dev/null) || rc=$?
    fi

    if [ "$rc" -ne 0 ]; then
        reason=$(grep -v '^$' "$TMPERR" | tail -1 | cut -c1-160)
        ERRORS+=("$image — ${reason:-trivy exited $rc}")
        continue
    fi

    parsed=$(printf '%s' "$json_out" | python3 -c "
import json, sys
data = json.load(sys.stdin)
fixable = total = 0
for r in data.get('Results', []):
    for v in (r.get('Vulnerabilities') or []):
        total += 1
        if v.get('FixedVersion'):
            fixable += 1
print(fixable, total)
" 2>/dev/null) || parsed=""

    if [ -z "$parsed" ]; then
        ERRORS+=("$image — trivy returned unparseable output")
        continue
    fi

    read -r fixable total <<< "$parsed"

    if [ "$fixable" -gt 0 ]; then
        FINDINGS+=("$image: **${fixable}** fixable / ${total} total CRITICAL")
    elif [ "$total" -gt 0 ]; then
        UNFIXABLE_IMAGES=$((UNFIXABLE_IMAGES + 1))
        UNFIXABLE_CVES=$((UNFIXABLE_CVES + total))
        echo "trivy-weekly: $image — $total CRITICAL, none fixable (not paging)"
    fi
done <<< "$IMAGES"

UNFIXABLE_NOTE="${UNFIXABLE_CVES} unfixable CRITICAL(s) across ${UNFIXABLE_IMAGES} image(s) — no upstream fix exists yet, nothing to action."

if [ ${#FINDINGS[@]} -eq 0 ] && [ ${#ERRORS[@]} -eq 0 ]; then
    echo "trivy-weekly: no fixable CRITICALs across $IMAGE_COUNT images ($UNFIXABLE_NOTE)"
    exit 0
fi

MSG=":shield: **Trivy Weekly Scan**${NL}"

if [ ${#FINDINGS[@]} -gt 0 ]; then
    MSG+=":rotating_light: **Fixable CRITICAL CVEs — upstream fix available:**${NL}"
    for f in "${FINDINGS[@]}"; do
        MSG+="• $f${NL}"
    done
fi

if [ ${#ERRORS[@]} -gt 0 ]; then
    MSG+="${NL}:warning: **Scan errors:**${NL}"
    for e in "${ERRORS[@]}"; do
        MSG+="• $e${NL}"
    done
fi

if [ "$UNFIXABLE_CVES" -gt 0 ]; then
    MSG+="${NL}_Also ${UNFIXABLE_NOTE}_${NL}"
fi

notify_discord "$MSG" 16711680
notify_ntfy "$MSG"
echo "trivy-weekly: ${#FINDINGS[@]} images with fixable CRITICALs, ${#ERRORS[@]} errors — Discord + ntfy notified ($UNFIXABLE_NOTE)"
# 10 = scan ran correctly and reported findings. Was `exit 1` (Aug 3 2026, so the
# unit stopped showing green with 28 vulnerable images), but with OnFailure= wired
# up that made a routine findings run page twice, and a permanently-red unit made
# `systemctl --failed` useless as a dashboard. Failed is now reserved for "the scan
# did not run" — the aborts above still exit 1. Surfacing what is *new* week over
# week is the open TODO item that actually addresses the wall of known CVEs.
exit 10
