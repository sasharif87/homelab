#!/usr/bin/env python3
"""
migrate-rag-to-owui.py

Copies all points from the per-collection Qdrant collections created by
ingest-rag.py into the single `open-webui_knowledge` collection that Open WebUI
queries, adding the payload fields OW expects:

    tenant_id                   "knowledge-bases"
    metadata.knowledge_base_id  <collection UUID>

Prerequisites:
  1. Change OW embedding model to nomic-embed-text:latest (Admin → Settings →
     Retrieval), then save. OW will delete and recreate open-webui_knowledge at
     768-dim on next startup / first query.
  2. Verify open-webui_knowledge exists at 768-dim before running this script:
         curl -s http://localhost:6333/collections/open-webui_knowledge \
           | python3 -c "import json,sys; print(json.load(sys.stdin)['result']['config']['params']['vectors']['size'])"
     Should print 768.

Run:
    python3 /opt/scripts/migrate-rag-to-owui.py

Resumable: tracks progress in migrate-rag-state.json next to this script.
Each source collection is processed in batches of BATCH_SIZE.  Safe to
interrupt and re-run — already-migrated batches are skipped.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import requests

# ── config ────────────────────────────────────────────────────────────────────
QDRANT_URL   = "http://localhost:6333"
TARGET_COL   = "open-webui_knowledge"
BATCH_SIZE   = 500   # points per upsert; keep low — each point carries a 768-dim vector
SCROLL_LIMIT = 500   # points per scroll page
QDRANT_TIMEOUT = 60  # seconds for collection metadata requests (longer during optimization)

SCRIPT_DIR  = Path(__file__).parent
STATE_FILE  = SCRIPT_DIR / "migrate-rag-state.json"
LOG_FILE    = SCRIPT_DIR / "migrate-rag-to-owui.log"

# UUID → human name mapping (from ingest-rag-state.json).  Populated at runtime.
COLLECTION_MAP: dict[str, str] = {}

# ── logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("migrate-rag")
log.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
sh = logging.StreamHandler(sys.stdout)
sh.setLevel(logging.INFO)
sh.setFormatter(fmt)
log.addHandler(fh)
log.addHandler(sh)


# ── state ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ── qdrant helpers ─────────────────────────────────────────────────────────────
def collection_exists(name: str) -> bool:
    r = requests.get(f"{QDRANT_URL}/collections/{name}", timeout=QDRANT_TIMEOUT)
    return r.status_code == 200


def collection_vector_size(name: str) -> int:
    r = requests.get(f"{QDRANT_URL}/collections/{name}", timeout=QDRANT_TIMEOUT)
    r.raise_for_status()
    return r.json()["result"]["config"]["params"]["vectors"]["size"]


def collection_point_count(name: str) -> int:
    r = requests.get(f"{QDRANT_URL}/collections/{name}", timeout=QDRANT_TIMEOUT)
    r.raise_for_status()
    return r.json()["result"]["points_count"]


def scroll_speed_ok(name: str, probe_size: int = 50, max_seconds: float = 5.0) -> bool:
    """Return True if a small scroll-with-vectors completes fast enough to proceed."""
    try:
        t0 = time.monotonic()
        r = requests.post(
            f"{QDRANT_URL}/collections/{name}/points/scroll",
            json={"limit": probe_size, "with_payload": False, "with_vector": True},
            timeout=max_seconds + 2,
        )
        elapsed = time.monotonic() - t0
        return r.status_code == 200 and elapsed <= max_seconds
    except Exception:
        return False


def wait_for_green(name: str, poll_interval: int = 30):
    """Block until the collection vectors are fast enough to migrate (in RAM)."""
    while True:
        if scroll_speed_ok(name):
            return
        log.info("Waiting for %s vectors to load into RAM (scroll still slow)...", name)
        time.sleep(poll_interval)


def scroll_batch(collection: str, offset) -> tuple[list, object]:
    """Return (points, next_offset).  points include id, payload, vector."""
    body = {"limit": SCROLL_LIMIT, "with_payload": True, "with_vector": True}
    if offset is not None:
        body["offset"] = offset
    r = requests.post(
        f"{QDRANT_URL}/collections/{collection}/points/scroll",
        json=body,
        timeout=120,
    )
    r.raise_for_status()
    result = r.json()["result"]
    return result["points"], result.get("next_page_offset")


def upsert_batch(points: list):
    r = requests.put(
        f"{QDRANT_URL}/collections/{TARGET_COL}/points",
        json={"points": points},
        timeout=120,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Upsert failed: HTTP {r.status_code} — {r.text[:200]}")


def list_source_collections() -> list[str]:
    r = requests.get(f"{QDRANT_URL}/collections", timeout=QDRANT_TIMEOUT)
    r.raise_for_status()
    return [
        c["name"]
        for c in r.json()["result"]["collections"]
        if c["name"] != TARGET_COL
    ]


# ── transform ─────────────────────────────────────────────────────────────────
def transform_point(point: dict, collection_id: str) -> dict:
    """Add OW-required payload fields; preserve existing metadata."""
    payload = dict(point.get("payload", {}))
    meta = dict(payload.get("metadata", {}))
    meta["knowledge_base_id"] = collection_id
    payload["metadata"] = meta
    payload["tenant_id"] = "knowledge-bases"
    return {
        "id": point["id"],
        "vector": point["vector"],
        "payload": payload,
    }


# ── load collection map ────────────────────────────────────────────────────────
def load_collection_map():
    ingest_state = SCRIPT_DIR / "ingest-rag-state.json"
    if not ingest_state.exists():
        log.warning("ingest-rag-state.json not found — UUIDs will show without names")
        return
    with open(ingest_state) as f:
        data = json.load(f)
    for name, cs in data.get("collections", {}).items():
        cid = cs.get("collection_id")
        if cid:
            COLLECTION_MAP[cid] = name


# ── migrate one collection ────────────────────────────────────────────────────
def migrate_collection(col_id: str, state: dict) -> int:
    col_state = state.setdefault(col_id, {"offset": None, "migrated": 0, "done": False})
    if col_state.get("done"):
        log.info("SKIP %s (%s) — already done", col_id, COLLECTION_MAP.get(col_id, "?"))
        return 0

    log.info("Waiting for %s optimizer to settle...", col_id)
    wait_for_green(col_id)
    total = collection_point_count(col_id)
    name  = COLLECTION_MAP.get(col_id, col_id)
    log.info("MIGRATE %s — %s  (%d points)", col_id, name, total)

    offset    = col_state["offset"]
    migrated  = col_state["migrated"]
    batch_num = 0

    while True:
        t0     = time.monotonic()
        points, next_offset = scroll_batch(col_id, offset)
        if not points:
            break

        transformed = [transform_point(p, col_id) for p in points]
        upsert_batch(transformed)

        migrated  += len(points)
        batch_num += 1
        elapsed    = time.monotonic() - t0

        col_state["offset"]   = next_offset
        col_state["migrated"] = migrated
        save_state(state)

        log.info(
            "  %s  batch=%d  migrated=%d/%d  (%.1f%%)"
            "  offset=%s  batch_time=%.1fs",
            name, batch_num, migrated, total,
            100 * migrated / max(total, 1),
            str(next_offset)[:8] if next_offset else "None",
            elapsed,
        )

        if next_offset is None:
            break
        offset = next_offset

    col_state["done"] = True
    save_state(state)
    log.info("DONE %s — %d points migrated", name, migrated)
    return migrated


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("migrate-rag-to-owui starting")

    load_collection_map()

    # Verify target collection exists and is 768-dim
    if not collection_exists(TARGET_COL):
        log.error(
            "%s does not exist. Change OW embedding model to nomic-embed-text "
            "in Admin → Settings → Retrieval, save, then re-run.",
            TARGET_COL,
        )
        sys.exit(1)

    target_dim = collection_vector_size(TARGET_COL)
    if target_dim != 768:
        log.error(
            "%s is %d-dim, expected 768. Change OW embedding model to "
            "nomic-embed-text in Admin → Settings → Retrieval, save, then re-run.",
            TARGET_COL, target_dim,
        )
        sys.exit(1)

    log.info("Target collection %s: 768-dim OK", TARGET_COL)

    sources = list_source_collections()
    log.info("Source collections: %s", sources)

    state = load_state()
    total_migrated = 0

    for col_id in sources:
        if not collection_exists(col_id):
            log.warning("Collection %s not found — skipping", col_id)
            continue
        src_dim = collection_vector_size(col_id)
        if src_dim != 768:
            log.warning(
                "Collection %s is %d-dim (expected 768) — skipping", col_id, src_dim
            )
            continue
        total_migrated += migrate_collection(col_id, state)

    log.info("=" * 60)
    log.info("Migration complete — %d total points migrated", total_migrated)
    log.info("State file: %s", STATE_FILE)
    log.info("Log file:   %s", LOG_FILE)


if __name__ == "__main__":
    main()
