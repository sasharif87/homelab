#!/usr/bin/env bash
# Ollama model refresh — run on <server-ip>
# Phase 1: clean up deprecated models + pull qwen3-coder (works on current 0.24.0)
# Phase 2: upgrade Ollama container, then pull gemma4 + gpt-oss (require 0.30.6+)
set -euo pipefail

OLLAMA="docker exec ollama ollama"

# ── helpers ────────────────────────────────────────────────────────────────────

log() { echo "[$(date '+%H:%M:%S')] $*"; }

pull() {
  log "Pulling $1..."
  $OLLAMA pull "$1"
  log "Done: $1"
}

remove() {
  if $OLLAMA list | grep -q "^$1"; then
    log "Removing $1..."
    $OLLAMA rm "$1"
  else
    log "Skip (not installed): $1"
  fi
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Safe on current Ollama 0.24.0
# Run: bash ollama-refresh.sh phase1
# ══════════════════════════════════════════════════════════════════════════════

phase1() {
  log "=== PHASE 1: Remove deprecated, pull qwen3-coder ==="

  # Remove models the reference doc marks as deprecated
  remove "llama3.2:3b"       # replaced by gemma4:e4b
  remove "qwen2.5:14b"       # superseded by qwen3:14b
  remove "qwen3:32b"         # replaced by gpt-oss:20b

  # Pull primary code model — qwen3-coder:30b
  # MoE: 30B total / 3.3B active. 19GB, 256K context. Fits in 16GB VRAM + ~3GB RAM.
  pull "qwen3-coder:30b"

  log "=== Phase 1 complete ==="
  $OLLAMA list
}

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Requires Ollama 0.30.6+ (gpt-oss:20b needs MXFP4 kernel)
# Run AFTER upgrading the container: bash ollama-refresh.sh phase2
# ══════════════════════════════════════════════════════════════════════════════

phase2() {
  log "=== PHASE 2: Verify Ollama version, pull new models ==="

  OLLAMA_VER=$(docker exec ollama ollama --version | grep -oP '\d+\.\d+\.\d+')
  REQUIRED="0.30.6"
  if [[ "$(printf '%s\n' "$REQUIRED" "$OLLAMA_VER" | sort -V | head -1)" != "$REQUIRED" ]]; then
    log "ERROR: Ollama $OLLAMA_VER is below required $REQUIRED — upgrade first (see upgrade_ollama)"
    exit 1
  fi
  log "Ollama $OLLAMA_VER OK"

  pull "gemma4:e4b"                      # 6GB, replaces llama3.2:3b — vision + audio
  pull "gpt-oss:20b"                     # 14GB MXFP4 MoE, ~o3-mini quality, 140 t/s
  pull "gemma4:26b"                      # 18GB MoE, 256K context, vision + thinking

  log "=== Phase 2 complete ==="
  $OLLAMA list
}

# ══════════════════════════════════════════════════════════════════════════════
# UPGRADE — Recreate Ollama container from latest image
# Run: bash ollama-refresh.sh upgrade_ollama
# ══════════════════════════════════════════════════════════════════════════════

upgrade_ollama() {
  log "=== Upgrading Ollama container ==="
  log "Pulling latest ollama image..."
  docker pull ollama/ollama:latest

  log "Stopping and removing old container..."
  docker stop ollama
  docker rm ollama

  log "Starting new container..."
  docker run -d \
    --name ollama \
    --runtime=nvidia \
    -e NVIDIA_VISIBLE_DEVICES=all \
    -e NVIDIA_DRIVER_CAPABILITIES=all \
    -e OLLAMA_HOST=0.0.0.0:11434 \
    -v /mnt/docker-local/ollama:/root/.ollama \
    -p 11434:11434 \
    --restart unless-stopped \
    ollama/ollama:latest

  log "Waiting for Ollama to start..."
  sleep 5
  docker exec ollama ollama --version
  log "=== Upgrade complete ==="
}

# ══════════════════════════════════════════════════════════════════════════════
# OPTIONAL CLEANUP — Uncomment and run manually after deciding
# ══════════════════════════════════════════════════════════════════════════════

# optional_cleanup() {
#   # deepseek-r1:70b — 42GB. Reasoning-focused, overlaps with llama3.3:70b.
#   # Remove if you want to reclaim 42GB; keep if you want a reasoning-first 70B.
#   # remove "deepseek-r1:70b"
#
#   # deepseek-r1:32b — 19GB. Fills gap between qwen3:14b and 70B tier.
#   # Now partially covered by qwen3-coder:30b for code tasks.
#   # remove "deepseek-r1:32b"
#
#   # llama3.2-vision:11b — 7.8GB. Vision superseded by gemma4:e4b / gemma4:26b.
#   # remove "llama3.2-vision:11b"
# }

# ══════════════════════════════════════════════════════════════════════════════

case "${1:-help}" in
  phase1)         phase1 ;;
  phase2)         phase2 ;;
  upgrade_ollama) upgrade_ollama ;;
  all)            upgrade_ollama && phase1 && phase2 ;;
  *)
    echo "Usage: $0 {phase1|phase2|upgrade_ollama|all}"
    echo ""
    echo "  phase1          — remove deprecated models, pull qwen3-coder:30b (safe on 0.24.0)"
    echo "  upgrade_ollama  — pull latest Ollama image, recreate container"
    echo "  phase2          — pull gemma4:e4b, gpt-oss:20b, gemma4:26b (requires 0.30.6+)"
    echo "  all             — upgrade_ollama → phase1 → phase2"
    ;;
esac
