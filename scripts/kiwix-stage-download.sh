#!/bin/bash
# Downloads ZIMs to staging, moves each to kiwix dir only when complete.
# Kiwix never sees an incomplete file. Resume-safe (-c).
# Log: /mnt/storage/knowledge/kiwix-download.log

STAGING=/mnt/storage/knowledge/kiwix-staging
KIWIX=/mnt/storage/knowledge/kiwix
LOG=/mnt/storage/knowledge/kiwix-download.log

mkdir -p "$STAGING"

dl() {
    local url=$1
    local name
    name=$(basename "$url")
    if [ -f "$KIWIX/$name" ]; then
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] SKIP $name (already in kiwix)" | tee -a "$LOG"
        return
    fi
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] START $name" | tee -a "$LOG"
    wget -c -q --show-progress -P "$STAGING" "$url" 2>&1 | tee -a "$LOG"
    if [ "${PIPESTATUS[0]}" -eq 0 ]; then
        mv "$STAGING/$name" "$KIWIX/$name"
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] DONE  $name" | tee -a "$LOG"
    else
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FAIL  $name" | tee -a "$LOG"
    fi
}

# --- Stack Exchange ---
dl "https://download.kiwix.org/zim/stack_exchange/diy.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/serverfault.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/gardening.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/mechanics.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/physics.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/chemistry.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/biology.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/3dprinting.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/woodworking.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/sustainability.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/ham.stackexchange.com_en_all_2026-02.zim"
dl "https://download.kiwix.org/zim/stack_exchange/cooking.stackexchange.com_en_all_2026-02.zim"

# --- Zimit ---
dl "https://download.kiwix.org/zim/zimit/solar.lowtechmagazine.com_mul_all_2025-01.zim"
dl "https://download.kiwix.org/zim/zimit/cd3wdproject.org_en_all_2025-11.zim"

# --- Wiktionary ---
dl "https://download.kiwix.org/zim/wiktionary/wiktionary_en_all_nopic_2026-05.zim"

# --- Survivor Library (235GB — queued last) ---
dl "https://download.kiwix.org/zim/zimit/survivorlibrary.com_en_all_2025-12.zim"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] ALL DONE" | tee -a "$LOG"
