#!/bin/bash
# Pull the latest YARA-Forge ruleset and install it as the scanner index.
# Run by yara-update.timer (Friday midnight CDT) so rules are fresh before Saturday sweep.
#
# Replaces the previous "git clone Neo23x0/signature-base and include every file that
# compiles" approach. signature-base is a threat-HUNTING ruleset meant for an analyst
# triaging in an IR context — pointed at a media/download library unattended it produced
# steady false positives (a Snake Malware rule matching inside a compressed MKV stream).
#
# YARA-Forge is by the same author (Florian Roth) and is the maintained answer to exactly
# that problem: it aggregates 70+ public rule repos — signature-base included — normalises
# them, and scores each rule for false positives, publishing three tiers:
#   core     — high-accuracy, low-FP rules only        (~5,000 rules)  ← what we use
#   extended — core + hunting rules, some more FPs     (~10,700 rules)
#   full     — everything functional, highest FP rate  (~12,400 rules)
# Set YARA_FORGE_TIER to change tiers. Default is core: this runs unattended against a
# library of untrusted downloads, so stability beats breadth.
set -euo pipefail

TIER="${YARA_FORGE_TIER:-core}"
INDEX=/opt/yara-index.yar
REPO=YARAHQ/yara-forge

for bin in curl unzip yara python3; do
    command -v "$bin" >/dev/null 2>&1 || { echo "yara-update: missing required binary '$bin'" >&2; exit 1; }
done

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Resolve the latest release tag, then fetch the tier package from it.
TAG=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['tag_name'])")
[[ -n "$TAG" ]] || { echo "yara-update: could not resolve latest release tag" >&2; exit 1; }

URL="https://github.com/${REPO}/releases/download/${TAG}/yara-forge-rules-${TIER}.zip"
echo "yara-update: fetching ${TIER} ruleset from release ${TAG}"

if ! curl -fsSL --max-time 180 -o "$WORK/rules.zip" "$URL"; then
    echo "yara-update: download failed — leaving existing $INDEX in place" >&2
    exit 1
fi

unzip -q -o "$WORK/rules.zip" -d "$WORK/x"

# The package ships a single consolidated .yar; locate it rather than hardcoding the
# path inside the archive, which has changed between releases.
CANDIDATE=$(find "$WORK/x" -name '*.yar' -type f -printf '%s\t%p\n' | sort -rn | head -1 | cut -f2)
[[ -n "$CANDIDATE" ]] || { echo "yara-update: no .yar file in package" >&2; exit 1; }

# Never install a ruleset that does not compile — a broken index silently disables the
# YARA layer on every scan until someone notices.
if ! yara "$CANDIDATE" /dev/null >/dev/null 2>&1; then
    echo "yara-update: downloaded ruleset failed to compile — keeping existing $INDEX" >&2
    exit 1
fi

RULE_COUNT=$(grep -c '^[[:space:]]*rule[[:space:]]' "$CANDIDATE" || echo "?")

[[ -f "$INDEX" ]] && cp -f "$INDEX" "${INDEX}.prev"
install -m 0644 "$CANDIDATE" "$INDEX"

echo "yara-update: installed YARA-Forge ${TIER} (${TAG}) — ${RULE_COUNT} rules → $INDEX"
