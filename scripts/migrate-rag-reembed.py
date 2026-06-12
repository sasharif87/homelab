#!/usr/bin/env python3
"""
migrate-rag-reembed.py

Migrates Kiwix RAG data into Open WebUI's open-webui_knowledge collection by:
  1. Scrolling text payload (no vectors) from each UUID source collection — fast
  2. Re-embedding text via Ollama nomic-embed-text (GPU)
  3. Upserting into open-webui_knowledge with correct OW payload format

This avoids the Qdrant on-disk vector transfer bottleneck entirely.
Vectors are deterministic (same model + text = same vector), so results are identical.

Resumable: progress saved in migrate-rag-reembed-state.json next to this script.

Run:
    python3 /opt/scripts/migrate-rag-reembed.py
    # Or in background:
    nohup python3 /opt/scripts/migrate-rag-reembed.py > /opt/scripts/migrate-rag-reembed.log 2>&1 &
"""

import json
import logging
import os
import sys
import time
import uuid as _uuid
from pathlib import Path

import requests

# ── config ────────────────────────────────────────────────────────────────────
QDRANT_URL   = "http://localhost:6333"
OLLAMA_URL   = "http://localhost:11434"
EMBED_MODEL  = "nomic-embed-text"
EMBED_DIM    = 768
TARGET_COL   = "open-webui_knowledge"

SCROLL_LIMIT = 1000   # text-only scroll — no vector payload, can be large
EMBED_BATCH  = 64     # texts per Ollama embed call
UPSERT_BATCH = 256    # points per Qdrant upsert

SCRIPT_DIR  = Path(__file__).parent
STATE_FILE  = SCRIPT_DIR / "migrate-rag-reembed-state.json"
LOG_FILE    = SCRIPT_DIR / "migrate-rag-reembed.log"

COLLECTION_MAP: dict[str, str] = {}

# ── logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("migrate-reembed")
log.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s", "%Y-%m-%d %H:%M:%S")
fh  = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(fmt)
sh  = logging.StreamHandler(sys.stdout)
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
def qdrant_get(path: str, timeout: int = 30) -> dict:
    for attempt in range(3):
        try:
            r = requests.get(f"{QDRANT_URL}{path}", timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                raise
            log.warning("qdrant GET %s attempt %d failed: %s — retrying", path, attempt + 1, e)
            time.sleep(5)


def list_source_collections() -> list[str]:
    data = qdrant_get("/collections")
    return [c["name"] for c in data["result"]["collections"] if c["name"] != TARGET_COL]


def collection_point_count(col: str) -> int:
    return qdrant_get(f"/collections/{col}")["result"]["points_count"]


def scroll_text_batch(col: str, offset) -> tuple[list[dict], object]:
    """Scroll text payload only (no vectors). Returns (items, next_offset).
    Each item: {"id": ..., "text": ..., "metadata": {...}}
    """
    body = {"limit": SCROLL_LIMIT, "with_payload": True, "with_vector": False}
    if offset is not None:
        body["offset"] = offset

    for attempt in range(5):
        try:
            r = requests.post(
                f"{QDRANT_URL}/collections/{col}/points/scroll",
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            result = r.json()["result"]
            items = [
                {
                    "id":       p["id"],
                    "text":     p["payload"].get("text", ""),
                    "metadata": p["payload"].get("metadata", {}),
                }
                for p in result["points"]
                if p["payload"].get("text")
            ]
            return items, result.get("next_page_offset")
        except Exception as e:
            if attempt == 4:
                raise
            wait = 10 * (attempt + 1)
            log.warning("Scroll error (attempt %d): %s — retrying in %ds", attempt + 1, e, wait)
            time.sleep(wait)


# ── ollama embed ──────────────────────────────────────────────────────────────
def embed_texts(texts: list[str]) -> list[list[float]]:
    for attempt in range(5):
        try:
            r = requests.post(
                f"{OLLAMA_URL}/api/embed",
                json={"model": EMBED_MODEL, "input": texts},
                timeout=300,
            )
            r.raise_for_status()
            vecs = r.json()["embeddings"]
            if len(vecs) != len(texts):
                raise ValueError(f"Expected {len(texts)} embeddings, got {len(vecs)}")
            return vecs
        except Exception as e:
            if attempt == 4:
                raise
            wait = 15 * (attempt + 1)
            log.warning("Embed error (attempt %d): %s — retrying in %ds", attempt + 1, e, wait)
            time.sleep(wait)


# ── qdrant upsert ─────────────────────────────────────────────────────────────
def upsert_points(points: list[dict]):
    for attempt in range(5):
        try:
            r = requests.put(
                f"{QDRANT_URL}/collections/{TARGET_COL}/points",
                json={"points": points},
                timeout=120,
            )
            if r.status_code not in (200, 202, 204):
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            return
        except Exception as e:
            if attempt == 4:
                raise
            wait = 10 * (attempt + 1)
            log.warning("Upsert error (attempt %d): %s — retrying in %ds", attempt + 1, e, wait)
            time.sleep(wait)


def build_point(item: dict, vector: list[float], collection_id: str) -> dict:
    meta = dict(item["metadata"])
    meta["knowledge_base_id"] = collection_id
    return {
        "id":      item["id"],
        "vector":  vector,
        "payload": {
            "text":      item["text"],
            "metadata":  meta,
            "tenant_id": "knowledge-bases",
        },
    }


# ── collection map ─────────────────────────────────────────────────────────────
def load_collection_map():
    path = SCRIPT_DIR / "ingest-rag-state.json"
    if not path.exists():
        return
    with open(path) as f:
        data = json.load(f)
    for name, cs in data.get("collections", {}).items():
        cid = cs.get("collection_id")
        if cid:
            COLLECTION_MAP[cid] = name


# ── migrate one collection ────────────────────────────────────────────────────
def migrate_collection(col_id: str, state: dict) -> int:
    col_state = state.setdefault(col_id, {"offset": None, "migrated": 0, "done": False})
    name = COLLECTION_MAP.get(col_id, col_id[:8])

    if col_state.get("done"):
        log.info("SKIP %s (%s) — already done", col_id[:8], name)
        return 0

    total = collection_point_count(col_id)
    log.info("MIGRATE %s — %s  (%d points)", col_id[:8], name, total)

    offset   = col_state["offset"]
    migrated = col_state["migrated"]
    batch_n  = 0

    text_buf: list[str]  = []
    id_buf:   list       = []
    meta_buf: list[dict] = []

    def flush_buffer():
        nonlocal migrated
        if not text_buf:
            return
        t0 = time.monotonic()
        # embed in sub-batches
        vectors = []
        for i in range(0, len(text_buf), EMBED_BATCH):
            chunk = text_buf[i:i + EMBED_BATCH]
            vectors.extend(embed_texts(chunk))

        # build and upsert in sub-batches
        for i in range(0, len(vectors), UPSERT_BATCH):
            pts = [
                build_point(
                    {"id": id_buf[j], "text": text_buf[j], "metadata": meta_buf[j]},
                    vectors[j],
                    col_id,
                )
                for j in range(i, min(i + UPSERT_BATCH, len(vectors)))
            ]
            upsert_points(pts)
            migrated += len(pts)

        elapsed = time.monotonic() - t0
        rate = len(text_buf) / max(elapsed, 0.01)
        log.info(
            "  %s  batch=%d  migrated=%d/%d  (%.1f%%)  texts=%d  %.0f txt/s  %.1fs",
            name, batch_n, migrated, total,
            100 * migrated / max(total, 1),
            len(text_buf), rate, elapsed,
        )
        col_state["migrated"] = migrated
        save_state(state)
        text_buf.clear()
        id_buf.clear()
        meta_buf.clear()

    while True:
        items, next_offset = scroll_text_batch(col_id, offset)
        if not items:
            break

        batch_n += 1
        for item in items:
            text_buf.append(item["text"])
            id_buf.append(item["id"])
            meta_buf.append(item["metadata"])

        # flush when buffer is big enough for a full embed+upsert cycle
        if len(text_buf) >= SCROLL_LIMIT:
            flush_buffer()

        col_state["offset"] = next_offset
        if next_offset is None:
            break
        offset = next_offset

    flush_buffer()  # drain remainder

    col_state["done"] = True
    save_state(state)
    log.info("DONE %s — %d points migrated", name, migrated)
    return migrated


# ── verify target ─────────────────────────────────────────────────────────────
def verify_target():
    data = qdrant_get(f"/collections/{TARGET_COL}")["result"]
    dim = data["config"]["params"]["vectors"]["size"]
    if dim != EMBED_DIM:
        log.error("%s is %d-dim, expected %d — abort", TARGET_COL, dim, EMBED_DIM)
        sys.exit(1)
    log.info("Target %s: %d-dim OK  (%d existing points)", TARGET_COL, dim, data["points_count"])


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info("migrate-rag-reembed starting")
    log.info("Embed model: %s  batch=%d  scroll=%d  upsert=%d",
             EMBED_MODEL, EMBED_BATCH, SCROLL_LIMIT, UPSERT_BATCH)

    load_collection_map()
    verify_target()

    sources = list_source_collections()
    log.info("Source collections: %s", sources)

    state = load_state()
    total = 0
    t_start = time.monotonic()

    for col_id in sources:
        total += migrate_collection(col_id, state)

    elapsed = time.monotonic() - t_start
    log.info("=" * 60)
    log.info("Migration complete — %d total points  elapsed=%.0fs (%.1fh)",
             total, elapsed, elapsed / 3600)
    log.info("open-webui_knowledge now has %d points",
             qdrant_get(f"/collections/{TARGET_COL}")["result"]["points_count"])


if __name__ == "__main__":
    main()
