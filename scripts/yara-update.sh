#!/bin/bash
# Pull latest Neo23x0/signature-base rules and rebuild the YARA index.
# Run by yara-update.timer (Friday midnight CDT) so rules are fresh before Saturday sweep.
set -euo pipefail

RULES_DIR=/opt/yara-rules
INDEX=/opt/yara-index.yar

if [[ ! -d "$RULES_DIR" ]]; then
    git clone --depth=1 https://github.com/Neo23x0/signature-base.git "$RULES_DIR"
else
    git -C "$RULES_DIR" pull --ff-only
fi

# Build index from the yara/ subdirectory, excluding rules that fail to compile.
# Some rules use pe module functions not available in the distro yara package.
VALID=0
FAILED=0
TMPIDX=$(mktemp)

while IFS= read -r f; do
    if yara "$f" /dev/null >/dev/null 2>&1; then
        echo "include \"$f\""
        ((VALID++)) || true
    else
        ((FAILED++)) || true
    fi
done < <(find "$RULES_DIR/yara" -name '*.yar' | sort) > "$TMPIDX"

mv "$TMPIDX" "$INDEX"
echo "yara-update: $VALID valid rule files indexed ($FAILED skipped — compile errors) → $INDEX"
