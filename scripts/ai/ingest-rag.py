#!/usr/bin/env python3
"""
ingest-rag.py — ZIM → Qdrant ingestion for Open WebUI

Reads ZIM files directly via libzim (no Kiwix HTTP server required),
chunks articles, embeds via Ollama nomic-embed-text, and writes vectors
directly into open-webui_knowledge with the payload format OW expects:
  - top-level tenant_id: "knowledge-bases"
  - metadata.knowledge_base_id: <OW collection UUID>
No per-file ChromaDB churn. Fully resumable.

Usage:
  python3 ingest-rag.py --api-key KEY [--zim-dir DIR] [options]
  python3 ingest-rag.py --check                    # verify deps + connectivity
  python3 ingest-rag.py --status                   # show ingestion progress

Install deps on Proxmox host:
  pip3 install libzim requests beautifulsoup4

Log file: ingest-rag.log (same directory as this script)
"""

import argparse
import ctypes
import gc
import json
import logging
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Force glibc to return freed malloc arenas to the OS.
# Without this, Python's RSS grows indefinitely even after gc.collect()
# because glibc holds empty arenas in process virtual memory.
try:
    _libc = ctypes.CDLL("libc.so.6")
    def _trim_malloc():
        _libc.malloc_trim(0)
except Exception:
    def _trim_malloc():
        pass

import requests
from bs4 import BeautifulSoup

# ── defaults ──────────────────────────────────────────────────────────────────
OLLAMA_BASE  = "http://<server-ip>:11434"
QDRANT_BASE  = "http://<server-ip>:6333"
WEBUI_BASE   = "http://<server-ip>:3000"
KIWIX_BASE   = "http://<server-ip>:8086"
ZIM_DIR      = "/mnt/storage/knowledge/kiwix"
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE   = os.path.join(SCRIPT_DIR, "ingest-rag-state.json")
LOG_FILE     = os.path.join(SCRIPT_DIR, "ingest-rag.log")

WATCHDOG_ENV = "/etc/container-watchdog.env"

# Discord embed colors
_DISCORD_GREEN  = 2244095   # success
_DISCORD_ORANGE = 16763904  # warnings / partial errors
_DISCORD_RED    = 16711680  # failure

EMBED_MODEL   = "nomic-embed-text"
EMBED_DIM     = 768
TARGET_COL    = "open-webui_knowledge"   # OW's single managed collection — all chunks go here
CHUNK_CHARS   = 1600   # ~400 tokens at ~4 chars/token
CHUNK_OVERLAP = 200
MIN_TEXT_LEN  = 150
MAX_TEXT_LEN  = 16000
EMBED_BATCH   = 256    # texts per Ollama /api/embed call
QDRANT_BATCH  = 512    # points per Qdrant upsert
TMP_BASE      = "/mnt/docker-local/zim-tmp"  # scratch space for phase-A HTML (local NVMe — faster than NFS for many small files)

# Phase B is a 3-stage pipeline (producer → embed consumer → upsert) so the GPU
# embeds back-to-back instead of stalling on file-read/lxml-parse/upsert.
# MEASURED on this box (Quadro RTX 5000, nomic-embed-text): embedding is
# GPU-compute-bound at ~70 texts/s and ~88% util — bigger batches and concurrent
# /api/embed requests give ZERO speedup (Ollama serializes server-side). The only
# real Phase-B win is keeping that single GPU stream fed, which pipelining does
# (the old single-threaded loop ran at ~35 chunks/s — half the GPU ceiling).
EMBED_QUEUE_DEPTH  = 4   # ready text-batches buffered ahead of the GPU
UPSERT_QUEUE_DEPTH = 8   # point-batches buffered ahead of Qdrant

# Phase A vote-sort: for capped Stack Exchange ZIMs, do a two-pass scan so we
# ingest the highest-voted articles rather than whatever happens to be first in
# the ZIM index (which is question-ID order, i.e. oldest-first).
# Skipped for ZIMs over this threshold — a full scan of stackoverflow (30M art)
# would take ~4h just for Phase A, not worth it.
_VOTE_SORT_MAX    = 2_000_000
_SE_SCORE_RE      = re.compile(rb'data-value="(-?\d+)"')

WEBUI_HEALTH_RETRIES = 5
WEBUI_HEALTH_WAIT    = 10  # seconds between retries

# Deterministic UUID namespace for point IDs (stable across re-runs)
_UUID_NS = uuid.UUID("7a3f9c12-4e8b-5d2a-9f1c-3b6e8a4d7f20")

# ── logging ───────────────────────────────────────────────────────────────────

log = logging.getLogger("ingest-rag")


def setup_logging(verbose: bool = False):
    """
    Two handlers:
      - Rotating file (10MB × 3): DEBUG level — captures everything
      - Stdout: INFO level (DEBUG if --verbose)
    """
    log.setLevel(logging.DEBUG)

    fmt_file   = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s",
                                   datefmt="%Y-%m-%d %H:%M:%S")
    fmt_stdout = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s",
                                   datefmt="%H:%M:%S")

    fh = RotatingFileHandler(LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt_file)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG if verbose else logging.INFO)
    sh.setFormatter(fmt_stdout)

    log.addHandler(fh)
    log.addHandler(sh)

    log.info("=" * 60)
    log.info("ingest-rag starting  |  log: %s", LOG_FILE)


# ── discord notifications ─────────────────────────────────────────────────────

def _load_discord_env():
    """Source DISCORD_WEBHOOK_URL from /etc/container-watchdog.env if not already set."""
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        return
    if not os.path.exists(WATCHDOG_ENV):
        return
    try:
        with open(WATCHDOG_ENV) as f:
            for line in f:
                line = line.strip()
                if line.startswith("DISCORD_WEBHOOK_URL=") and "=" in line:
                    _, val = line.split("=", 1)
                    os.environ["DISCORD_WEBHOOK_URL"] = val.strip().strip('"').strip("'")
                    break
    except Exception as e:
        log.debug("Could not load %s: %s", WATCHDOG_ENV, e)


def send_discord(title: str, description: str, color: int = _DISCORD_GREEN):
    """
    Send a Discord embed notification via webhook.
    Reads DISCORD_WEBHOOK_URL from environment (auto-loaded from watchdog env).
    Silently skips if webhook URL is not configured.
    """
    _load_discord_env()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        log.debug("Discord webhook not configured — skipping notification")
        return

    payload = json.dumps({
        "embeds": [{
            "title":       title,
            "description": description,
            "color":       color,
            "footer":      {"text": f"ingest-rag  •  {time.strftime('%Y-%m-%d %H:%M:%S')}"},
        }]
    })

    try:
        result = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "POST", webhook_url,
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        code = result.stdout.strip()
        if code == "204":
            log.info("Discord notification sent: %s", title)
        else:
            log.warning("Discord webhook returned HTTP %s for: %s", code, title)
    except Exception as e:
        log.warning("Discord notification failed: %s", e)


# ── collection definitions ────────────────────────────────────────────────────
COLLECTIONS = [
    {
        "name": "Kiwix — Medical",
        "desc": (
            "Offline medical knowledge: WikiMed Medical Encyclopedia, NHS Medicines, "
            "Medical Library, Medicine LibreTexts, LibrePathology, Military Medicine, "
            "Gutenberg medical texts."
        ),
        "match": [
            "wikipedia_en_medicine", "nhs_uk", "zimgit-medicine",
            "libretexts_org_en_med", "ted_mul_medicine", "librepathology",
            "fas-military-medicine", "gutenberg_en_lcc-r",
        ],
        "caps": {
            "wikipedia_en_medicine": 100_000,
        },
    },
    {
        "name": "Kiwix — Technical",
        "desc": (
            "Technical Q&A and references: Stack Overflow, Server Fault, Unix & Linux, "
            "Electronics, Physics, Chemistry, Biology, 3D Printing, Data Science, "
            "Ham Radio, FreeCodeCamp, iFixit, Restarters, Gutenberg tech/science."
        ),
        "match": [
            "stackoverflow_en", "serverfault.com", "unix.stackexchange",
            "electronics.stackexchange", "physics.stackexchange",
            "chemistry.stackexchange", "biology.stackexchange",
            "3dprinting.stackexchange", "datascience_stackexchange",
            "ham.stackexchange", "ifixit", "restarters",
            "gutenberg_en_lcc-t", "gutenberg_en_lcc-q",
        ],
        "caps": {
            "stackoverflow_en":          100_000,
            "serverfault.com":           100_000,
            "unix.stackexchange":        100_000,
            "physics.stackexchange":     100_000,
            "electronics.stackexchange": 100_000,
            # lcc-q/lcc-t are small (≈3.7k/2.6k pages) — caps below never bind,
            # kept only as guardrails for future larger Gutenberg drops.
            "gutenberg_en_lcc-q":        100_000,
            "gutenberg_en_lcc-t":        100_000,
        },
    },
    {
        "name": "Kiwix — Practical Skills",
        "desc": (
            "Hands-on how-to and practical skills: wikiHow, DIY, Woodworking, Mechanics, "
            "Gardening, Cooking, Sustainability Stack Exchange, FOSS Cooking. "
            "Homesteading and low-tech living: Appropedia, CD3WD, Low-tech Magazine, "
            "Urban Prepper, post-disaster/water/food/knots guides, "
            "Gutenberg agriculture and natural history (LCC-S)."
        ),
        "match": [
            "wikihow",
            "diy.stackexchange", "woodworking.stackexchange",
            "mechanics.stackexchange", "gardening.stackexchange",
            "sustainability.stackexchange", "cooking.stackexchange",
            "foss_cooking",
            "cd3wdproject", "solar.lowtechmagazine", "zimgit-post-disaster",
            "urban-prepper", "zimgit-food-preparation", "zimgit-water",
            "zimgit-knots", "appropedia",
            "gutenberg_en_lcc-s",
        ],
        "caps": {
            "wikihow":            100_000,
            "diy.stackexchange":  100_000,
            "gutenberg_en_lcc-s": 100_000,
        },
    },
    {
        "name": "Kiwix — General Reference",
        "desc": (
            "Encyclopedic general knowledge: Wikipedia (full + Simple English), "
            "Wikibooks, Wikiversity, Wikivoyage, Wiktionary."
        ),
        "match": [
            "wikipedia_en_simple", "wikipedia_en_all",
            "wikibooks", "wikiversity", "wikivoyage", "wiktionary",
        ],
        "caps": {
            "wikipedia_en_simple": 100_000,
            "wikipedia_en_all":    100_000,
            "wikibooks":           100_000,
            "wikiversity":         100_000,
        },
    },
]

# HTML elements to strip before text extraction
_STRIP_TAGS    = {"script", "style", "nav", "noscript", "form", "iframe"}
_STRIP_IDS     = {"mw-navigation", "mw-head", "mw-panel", "footer", "siteNotice", "p-tb"}
_STRIP_CLASSES = {"navbox", "sidebar", "noprint", "mw-jump-link", "catlinks",
                  "reflist", "references", "toc", "printfooter"}


# ── text processing ───────────────────────────────────────────────────────────

def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    for id_ in _STRIP_IDS:
        el = soup.find(id=id_)
        if el:
            el.decompose()
    for cls in _STRIP_CLASSES:
        for el in soup.find_all(class_=cls):
            el.decompose()

    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    elif soup.title:
        title = (soup.title.string or "").split(" – ")[0].split(" - ")[0].strip()

    main = (
        soup.find(id="mw-content-text")
        or soup.find(id="mainbar")
        or soup.find(id="question-page")
        or soup.find("main")
        or soup.find("article")
        or soup.body
    )
    if not main:
        soup.decompose()
        return ""

    text = main.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    if title:
        text = f"# {title}\n\n{text}"

    # Break all circular parent↔child refs so CPython's refcount can free
    # the tree immediately rather than waiting for cyclic GC. Without this,
    # thousands of soup trees accumulate and exhaust RAM during Phase B.
    soup.decompose()
    return text.strip()


def chunk_text(text: str) -> list[str]:
    text = text[:MAX_TEXT_LEN]
    n = len(text)
    if n <= CHUNK_CHARS:
        return [text]

    chunks = []
    start = 0
    while start < n:
        end = min(start + CHUNK_CHARS, n)
        if end < n:
            boundary = text.rfind(". ", start + CHUNK_CHARS // 2, end)
            if boundary != -1:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        # Stop once the whole text is consumed. `end` always advances to at
        # least start+CHUNK_CHARS//2+1, so the next `start` moves strictly
        # forward — but `end` also never exceeds n, so the old
        # `if start >= n` test could never fire and the loop spun forever on
        # the final segment (appending the same chunk until OOM). Break on end.
        if end >= n:
            break
        start = end - CHUNK_OVERLAP
    return chunks


def make_point_id(zim_name: str, article_path: str, chunk_idx: int) -> str:
    return str(uuid.uuid5(_UUID_NS, f"{zim_name}|{article_path}|{chunk_idx}"))


# ── state ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"collections": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    log.debug("State saved to %s", STATE_FILE)


# ── webui health check ────────────────────────────────────────────────────────

def wait_for_webui(webui_url: str, api_key: str = "") -> bool:
    """
    Poll Open WebUI until it's healthy and its knowledge API responds.
    Returns True if ready, False after all retries exhausted.
    """
    log.info("Checking Open WebUI stability at %s ...", webui_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    for attempt in range(1, WEBUI_HEALTH_RETRIES + 1):
        try:
            # 1. Basic health ping
            t0 = time.monotonic()
            r = requests.get(f"{webui_url}/health", timeout=10)
            latency = (time.monotonic() - t0) * 1000
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}")
            log.debug("  /health  → %s  (%.0f ms)", r.status_code, latency)

            # 2. Knowledge API availability (requires auth)
            if api_key:
                t0 = time.monotonic()
                r2 = requests.get(
                    f"{webui_url}/api/v1/knowledge/",
                    headers=headers,
                    timeout=15,
                )
                latency2 = (time.monotonic() - t0) * 1000
                if not r2.ok:
                    raise RuntimeError(f"Knowledge API HTTP {r2.status_code}: {r2.text[:120]}")
                log.debug("  /api/v1/knowledge/  → %s  (%.0f ms)", r2.status_code, latency2)

            log.info("Open WebUI is healthy (attempt %d/%d)", attempt, WEBUI_HEALTH_RETRIES)
            return True

        except Exception as e:
            log.warning("WebUI not ready (attempt %d/%d): %s", attempt, WEBUI_HEALTH_RETRIES, e)
            if attempt < WEBUI_HEALTH_RETRIES:
                log.info("Waiting %ds before retry...", WEBUI_HEALTH_WAIT)
                time.sleep(WEBUI_HEALTH_WAIT)

    log.error("Open WebUI did not become healthy after %d attempts.", WEBUI_HEALTH_RETRIES)
    return False


# ── ollama embedding ──────────────────────────────────────────────────────────

def embed_texts(session: requests.Session, texts: list[str], ollama_url: str) -> list[list[float]]:
    t0 = time.monotonic()
    resp = session.post(
        f"{ollama_url}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=120,
    )
    elapsed = time.monotonic() - t0
    if not resp.ok:
        log.error("Ollama embed failed: HTTP %s — %s", resp.status_code, resp.text[:200])
        resp.raise_for_status()
    embeddings = resp.json()["embeddings"]
    log.debug("Ollama embed: %d texts → %d vectors  (%.2fs, %.1f texts/s)",
              len(texts), len(embeddings), elapsed, len(texts) / max(elapsed, 0.001))
    return embeddings


# ── qdrant ────────────────────────────────────────────────────────────────────

def ensure_qdrant_collection(qdrant_url: str):
    """Verify TARGET_COL exists at the right dimension. OW owns it — don't recreate."""
    resp = requests.get(f"{qdrant_url}/collections/{TARGET_COL}", timeout=10)
    if resp.status_code != 200:
        log.error(
            "%s not found. Ensure Open WebUI is running with %s as the embedding model "
            "(Admin → Settings → Documents), then re-run.",
            TARGET_COL, EMBED_MODEL,
        )
        sys.exit(1)
    info = resp.json().get("result", {})
    dim  = info.get("config", {}).get("params", {}).get("vectors", {}).get("size")
    pts  = info.get("points_count", "?")
    if dim != EMBED_DIM:
        log.error(
            "%s is %d-dim, expected %d. Change OW embedding model to %s "
            "in Admin → Settings → Documents, save, then re-run.",
            TARGET_COL, dim, EMBED_DIM, EMBED_MODEL,
        )
        sys.exit(1)
    log.info("Target %s: %d-dim OK  (%s existing points)", TARGET_COL, dim, pts)


def upsert_points(qdrant_url: str, points: list[dict]):
    total = len(points)
    upserted = 0
    for i in range(0, total, QDRANT_BATCH):
        batch = points[i:i + QDRANT_BATCH]
        t0 = time.monotonic()
        resp = requests.put(
            f"{qdrant_url}/collections/{TARGET_COL}/points",
            json={"points": batch},
            params={"wait": "false"},
            timeout=60,
        )
        elapsed = time.monotonic() - t0
        if not resp.ok:
            log.error("Qdrant upsert failed: HTTP %s — %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()
        upserted += len(batch)
        log.debug("Qdrant upsert: %d/%d points  (%.2fs)", upserted, total, elapsed)


# ── open webui collection ─────────────────────────────────────────────────────

def ensure_webui_collection(
    session: requests.Session,
    api_key: str,
    col_state: dict,
    name: str,
    desc: str,
    webui_url: str,
) -> str:
    if col_state.get("collection_id"):
        cid = col_state["collection_id"]
        log.info("Collection already registered: %r  (%s)", name, cid)
        return cid

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    # Check if it already exists in Open WebUI
    log.debug("Checking for existing collection %r in Open WebUI...", name)
    t0 = time.monotonic()
    resp = session.get(f"{webui_url}/api/v1/knowledge/", headers=headers, timeout=15)
    log.debug("GET /api/v1/knowledge/  → %s  (%.2fs)", resp.status_code, time.monotonic() - t0)

    if resp.ok:
        body  = resp.json()
        items = body.get("items", body) if isinstance(body, dict) else body
        for col in items:
            if col.get("name") == name:
                cid = col["id"]
                col_state["collection_id"] = cid
                log.info("Found existing OW collection: %r  (%s)", name, cid)
                return cid

    # Create it
    log.info("Creating OW knowledge collection: %r", name)
    t0 = time.monotonic()
    resp = session.post(
        f"{webui_url}/api/v1/knowledge/create",
        headers=headers,
        json={"name": name, "description": desc},
        timeout=30,
    )
    elapsed = time.monotonic() - t0
    if not resp.ok:
        log.error("Failed to create collection %r: HTTP %s — %s",
                  name, resp.status_code, resp.text[:300])
        resp.raise_for_status()

    cid = resp.json()["id"]
    col_state["collection_id"] = cid
    log.info("Created OW collection: %r  (%s)  (%.2fs)", name, cid, elapsed)
    return cid


def get_api_key(args, session: requests.Session) -> str:
    if args.api_key:
        return args.api_key
    log.info("Logging in to Open WebUI as %s ...", args.email)
    for attempt in range(1, 4):
        try:
            resp = session.post(
                f"{args.webui_url}/api/v1/auths/signin",
                json={"email": args.email, "password": args.password},
                timeout=30,
            )
            break
        except requests.Timeout:
            log.warning("Login timeout (attempt %d/3)", attempt)
            if attempt == 3:
                sys.exit("Login timed out after 3 attempts.")
            time.sleep(10)

    if not resp.ok:
        sys.exit(f"Login failed ({resp.status_code}): {resp.text[:200]}")
    token = resp.json().get("token")
    if not token:
        sys.exit(f"No token in login response: {resp.text[:200]}")
    log.info("Login successful, token acquired.")
    return token


# ── two-phase zim ingestion ───────────────────────────────────────────────────
#
# Phase A  crawl_to_disk()  — BFS via Kiwix HTTP, write each HTML article to
#   a scratch file under TMP_BASE/<zim_stem>/, then immediately free all Python
#   objects (response + BeautifulSoup tree). Peak RSS ≈ size of one HTML page.
#
# Phase B  embed_from_disk()  — read saved files in large batches (EMBED_BATCH),
#   chunk, embed via Ollama, upsert to Qdrant, delete each file as it's done.
#   No network calls during embedding = maximum GPU utilisation.
#
# Why two phases solve the OOM: the single-pass BFS+embed loop accumulated
# ~42 000 Response + BeautifulSoup objects faster than Python's GC could
# collect them, hitting 85 GB RSS.  Separating fetch from embed keeps each
# phase's peak memory small and predictable.
def _kiwix_book_path(kiwix_base: str, zim_stem: str) -> str | None:
    """Return the Kiwix content path segment for a ZIM stem via OPDS catalog."""
    from xml.etree import ElementTree as ET
    try:
        resp = requests.get(f"{kiwix_base}/catalog/v2/entries",
                            params={"count": -1}, timeout=15)
        resp.raise_for_status()
        ns   = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(resp.content)
        for entry in root.findall("atom:entry", ns):
            for link in entry.findall("atom:link", ns):
                href  = link.get("href", "")
                ltype = link.get("type", "")
                if "html" in ltype and href.startswith("/content/"):
                    path = href[len("/content/"):].split("/")[0]
                    if path and (zim_stem.startswith(path) or path in zim_stem):
                        return path
    except Exception as exc:
        log.warning("Kiwix catalog lookup failed for %s: %s", zim_stem, exc)
    return None


# ── phase A (libzim): enumerate ZIM index directly ─────────────────────────────

def crawl_to_disk_libzim(zim_path: Path, out_dir: Path,
                         already_seen: set, cap: int = 0) -> tuple[int, int]:
    """
    Enumerate every entry in a ZIM via libzim and write each HTML article to
    out_dir/<n>.json as {url, title, html}.  Reads the ZIM file read-only —
    no Kiwix HTTP server, no link discovery, no per-request latency.

    This is the correct way to reach EVERY article, including JS-SPA ZIMs whose
    static HTML exposes no <a> links (e.g. libretexts_org_en_med: 23 171 html
    articles that the old HTTP-BFS crawler could never find — it got 1).

    Entry order in a ZIM is NOT html-first (libretexts' first thousands of
    entries are images/fonts), so we scan all entries and filter on mimetype.
    Non-HTML items (PDF, images, video) are skipped — adding a PDF branch here
    later is trivial (mimetype == "application/pdf" → PyMuPDF).

    Returns (n_written, n_html_total) — n_html_total drives the "0 articles but
    many entries → don't mark done" retry heuristic upstream.
    """
    from libzim.reader import Archive

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        arc = Archive(str(zim_path))
    except Exception as exc:
        log.error("libzim could not open %s: %s", zim_path, exc)
        log.debug(traceback.format_exc())
        return 0, 0

    total       = arc.all_entry_count
    art_count   = arc.article_count
    is_se       = any(x in zim_path.stem.lower()
                      for x in ("stackexchange", "stackoverflow", "serverfault"))
    use_vote_sort = (cap > 0
                     and art_count > cap * 2
                     and art_count <= _VOTE_SORT_MAX
                     and is_se)

    log.info("Phase A — libzim enumerate: %s  entries=%d  article_count=%d  cap=%s  vote_sort=%s",
             zim_path.stem, total, art_count, cap or "none", use_vote_sort)

    n_written = n_html = n_redir = n_err = 0
    LOG_EVERY = 5000

    if use_vote_sort:
        # ── pass 1: collect (entry_id, score) for all HTML articles ──────────
        scored = []   # [(entry_id, score)]
        for i in range(total):
            try:
                entry = arc._get_entry_by_id(i)
                if entry.is_redirect:
                    n_redir += 1
                    del entry
                    continue
                item = entry.get_item()
                if "html" not in (item.mimetype or ""):
                    del item, entry
                    continue
                n_html += 1
                if entry.path not in already_seen:
                    raw = bytes(item.content)
                    m   = _SE_SCORE_RE.search(raw[:4096])
                    scored.append((i, int(m.group(1)) if m else 0))
                del raw, item, entry
            except Exception as exc:
                n_err += 1
                log.warning("  Phase A vote-scan error (id=%d): %s", i, exc)

            if i and i % LOG_EVERY == 0:
                log.info("  Phase A vote-scan: %s  scanned=%d/%d  html=%d",
                         zim_path.stem, i, total, n_html)
                gc.collect()
                _trim_malloc()

        scored.sort(key=lambda x: -x[1])
        top_score = scored[0][1]    if scored else 0
        bot_score = scored[cap-1][1] if len(scored) >= cap else 0
        log.info("Phase A vote-scan complete: %s  candidates=%d  keeping top %d  score_range=%d–%d",
                 zim_path.stem, len(scored), min(cap, len(scored)), bot_score, top_score)

        top_entries = sorted(scored[:cap], key=lambda x: x[0])  # sort by entry_id for sequential reads
        del scored

        # ── pass 2: write top-cap entries in index order (sequential reads) ──
        for eid, score in top_entries:
            try:
                entry = arc._get_entry_by_id(eid)
                item  = entry.get_item()
                html  = bytes(item.content).decode("utf-8", errors="replace")
                title = entry.title or entry.path
                fname = out_dir / f"{n_written:07d}.json"
                fname.write_text(json.dumps({"url": entry.path, "title": title,
                                             "html": html, "vote_score": score}),
                                 encoding="utf-8")
                n_written += 1
                del html, item, entry
            except Exception as exc:
                n_err += 1
                log.warning("  Phase A vote-write error (id=%d): %s", eid, exc)

    else:
        # ── single-pass: sequential enumeration (uncapped or too large to sort) ──
        if cap and art_count > cap * 2 and not is_se:
            log.info("  Phase A: %s sequential (vote-sort only for SE ZIMs)", zim_path.stem)
        elif cap and art_count > _VOTE_SORT_MAX:
            log.warning("  Phase A: %s too large for vote-sort (%d articles) — ingesting sequentially",
                        zim_path.stem, art_count)

        for i in range(total):
            if cap and n_written >= cap:
                log.info("  Phase A: %s reached cap (%d articles) — stopping enumeration",
                         zim_path.stem, cap)
                break
            try:
                entry = arc._get_entry_by_id(i)
                if entry.is_redirect:
                    n_redir += 1
                    continue
                item = entry.get_item()
                if "html" not in (item.mimetype or ""):
                    continue
                n_html += 1

                path = entry.path
                if path in already_seen:
                    continue

                html  = bytes(item.content).decode("utf-8", errors="replace")
                title = entry.title or path

                fname = out_dir / f"{n_written:07d}.json"
                fname.write_text(json.dumps({"url": path, "title": title, "html": html}),
                                 encoding="utf-8")
                n_written += 1
                del html, item, entry

                if i and i % LOG_EVERY == 0:
                    log.info("  Phase A: %s  scanned=%d/%d  html=%d  written=%d  redirects=%d",
                             zim_path.stem, i, total, n_html, n_written, n_redir)
                    gc.collect()
                    _trim_malloc()

            except Exception as exc:
                n_err += 1
                log.warning("  Phase A libzim error (id=%d): %s", i, exc)
                log.debug(traceback.format_exc())

    log.info("Phase A complete: %s  scanned=%d  html=%d  written=%d  redirects=%d  errors=%d",
             zim_path.stem, total, n_html, n_written, n_redir, n_err)
    return n_written, n_html


# ── phase A (http): crawl Kiwix via BFS — kept as a fallback ────────────────────

def crawl_to_disk(zim_stem: str, kiwix_base: str, out_dir: Path,
                  already_seen: set, cap: int = 0) -> int:
    """
    BFS-crawl a ZIM via Kiwix HTTP.  Each HTML article is written to
    out_dir/<n>.json as {url, title, html} and the Python objects are
    immediately freed.  Returns count of new files written.
    Peak RSS ≈ size of one HTML page + BFS bookkeeping.

    If cap > 0, the crawl stops once `cap` articles have been written. This
    bounds both crawl time and scratch-disk usage for huge ZIMs (e.g.
    stackoverflow, large Gutenberg sets) rather than crawling the whole ZIM
    to disk and discarding the surplus in Phase B.
    """
    from collections import deque
    from urllib.parse import urljoin

    out_dir.mkdir(parents=True, exist_ok=True)

    book_path = _kiwix_book_path(kiwix_base, zim_stem)
    if not book_path:
        log.error("ZIM not found in Kiwix catalog: %s — is Kiwix serving it?", zim_stem)
        return 0

    prefix  = f"{kiwix_base}/content/{book_path}/"
    session = requests.Session()
    session.headers["User-Agent"] = "ingest-rag/2.0"
    queue   = deque([prefix])
    seen    = set(already_seen) | {prefix}

    n_written = 0
    n_fetched = 0
    n_errors  = 0
    LOG_EVERY = 500

    log.info("Phase A — crawl to disk: %s  prefix=%s", zim_stem, prefix)

    while queue:
        url = queue.popleft()
        try:
            # stream=True: only read body for HTML responses
            with session.get(url, timeout=20, allow_redirects=True,
                             stream=True) as resp:
                if resp.status_code != 200:
                    n_errors += 1
                    log.debug("HTTP %s: %s", resp.status_code, url)
                    continue

                ctype = resp.headers.get("content-type", "")
                if "html" not in ctype:
                    continue  # body never buffered

                html = resp.text   # read now that we know it's HTML
                final_url = resp.url  # final URL after any redirects (may differ from queue URL)
            # resp closed here — connection returned to pool, object freed

            n_fetched += 1

            # Discover new internal links (parse once, discard tree)
            # Use final_url (post-redirect) as base so relative links resolve correctly.
            # ZIMs like nhs_uk redirect /content/zim/ → /content/zim/www.nhs.uk/medicines/
            # and links on the redirected page are relative to that deeper path.
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                full = urljoin(final_url, a["href"]).split("#")[0]
                if full.startswith(prefix) and full not in seen:
                    seen.add(full)
                    queue.append(full)

            # Title extraction
            title = ""
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)
            elif soup.title:
                title = (soup.title.string or "").split(" – ")[0].split(" - ")[0].strip()

            soup.decompose()   # break circular refs for immediate refcount GC
            del soup

            # Return freed malloc arenas to OS every 100 pages
            if n_fetched % 100 == 0:
                gc.collect()
                _trim_malloc()

            # Skip if already processed in a previous run
            if url in already_seen:
                continue

            # Write to disk — free html string after write
            fname = out_dir / f"{n_written:07d}.json"
            fname.write_text(json.dumps({"url": url, "title": title, "html": html}),
                             encoding="utf-8")
            del html

            n_written += 1
            if n_fetched % LOG_EVERY == 0:
                log.info("  Phase A: %s  fetched=%d  written=%d  queued=%d  errors=%d",
                         zim_stem, n_fetched, n_written, len(queue), n_errors)

            if cap and n_written >= cap:
                log.info("  Phase A: %s reached cap (%d articles) — stopping crawl",
                         zim_stem, cap)
                break

            time.sleep(0.015)  # ~65 req/s

        except Exception as exc:
            n_errors += 1
            log.warning("  Phase A error (%s): %s", url, exc)
            log.debug(traceback.format_exc())

    log.info("Phase A complete: %s  fetched=%d  written=%d  errors=%d",
             zim_stem, n_fetched, n_written, n_errors)
    return n_written


# ── phase B: embed from disk ──────────────────────────────────────────────────

def embed_from_disk(out_dir: Path, zim_name: str, knowledge_base_id: str,
                    cap: int, args) -> tuple[int, int]:
    """
    Pipelined Phase B.  Three stages run concurrently so the GPU embeds
    back-to-back instead of stalling on disk reads + lxml parsing + Qdrant:

        producer thread : read file → extract_text → chunk        → embed_q
        embed consumer  : embed_q → Ollama /api/embed (GPU)        → upsert_q   [main]
        upsert thread   : upsert_q → Qdrant

    Bounded queues give backpressure, so peak memory is ~a few batches regardless
    of ZIM size (and this whole function runs in a subprocess, so the OS reclaims
    everything on exit).  Files are deleted as the producer consumes them.

    Why pipeline rather than parallel embed: embedding is GPU-compute-bound at
    ~70 texts/s on this box and Ollama serializes requests, so concurrent
    /api/embed calls give no speedup.  The win is never letting the single GPU
    stream go idle — the old serial loop ran at ~35 chunks/s (half the ceiling)
    because file-read/parse/upsert were interleaved with the embed call.

    Returns (articles_ingested, chunks_ingested).
    """
    import queue
    import resource
    import threading

    files = sorted(out_dir.glob("*.json"))
    if not files:
        log.warning("Phase B: no files found in %s", out_dir)
        return 0, 0

    log.info("Phase B — embed from disk (pipelined): %s  files=%d  cap=%s",
             zim_name, len(files), cap or "none")

    run_start = time.monotonic()
    embed_q  = queue.Queue(maxsize=EMBED_QUEUE_DEPTH)    # batches of (pid, text, payload)
    upsert_q = queue.Queue(maxsize=UPSERT_QUEUE_DEPTH)   # batches of Qdrant points
    DONE = object()  # sentinel

    # Each counter has a single writer thread, so no lock is needed:
    #   arts/skipped → producer, embed_errs → consumer, chunks/upsert_errs → upserter
    stats = {"arts": 0, "skipped": 0, "chunks": 0, "embed_errs": 0, "upsert_errs": 0}

    # ── stage 1: producer (file → chunks) ──────────────────────────────────────
    def producer():
        batch = []
        for fpath in files:
            if cap and stats["arts"] >= cap:
                log.info("Cap of %d reached for %s", cap, zim_name)
                break
            try:
                data       = json.loads(fpath.read_text(encoding="utf-8"))
                art_url    = data["url"]
                art_title  = data.get("title", "")
                vote_score = data.get("vote_score", 0)
                text       = extract_text(data["html"])
                del data

                if len(text) < MIN_TEXT_LEN:
                    stats["skipped"] += 1
                    fpath.unlink(missing_ok=True)
                    continue

                for ci, chunk in enumerate(chunk_text(text)):
                    pid = make_point_id(zim_name, art_url, ci)
                    batch.append((
                        pid, chunk,
                        {"text": chunk,
                         "metadata": {"source": art_url, "name": art_title or art_url,
                                      "zim": zim_name, "chunk_id": ci,
                                      "vote_score": vote_score,
                                      "knowledge_base_id": knowledge_base_id},
                         "tenant_id": "knowledge-bases"},
                    ))
                    if len(batch) >= EMBED_BATCH:
                        embed_q.put(batch)        # blocks when GPU is behind (backpressure)
                        batch = []

                stats["arts"] += 1
                fpath.unlink(missing_ok=True)

                # lxml frees its C structures promptly, but glibc arenas grow
                # with large stackexchange pages (50-90 KB); trim frequently.
                if stats["arts"] % 10 == 0:
                    gc.collect()
                    _trim_malloc()

                if stats["arts"] % 500 == 0:
                    rate   = stats["arts"] / max(time.monotonic() - run_start, 0.001)
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
                    log.info("  Phase B: %s  arts=%d  chunks=%d  skipped=%d  "
                             "embed_errs=%d  upsert_errs=%d  rate=%.1f art/s  rss=%dMB",
                             zim_name, stats["arts"], stats["chunks"], stats["skipped"],
                             stats["embed_errs"], stats["upsert_errs"], rate, rss_mb)
            except Exception as exc:
                log.error("Phase B file error (%s): %s", fpath.name, exc)
                log.debug(traceback.format_exc())
        if batch:
            embed_q.put(batch)
        embed_q.put(DONE)

    # ── stage 3: upserter (points → Qdrant) ────────────────────────────────────
    def upserter():
        while True:
            points = upsert_q.get()
            if points is DONE:
                break
            try:
                upsert_points(args.qdrant_url, points)
                stats["chunks"] += len(points)
            except Exception as exc:
                stats["upsert_errs"] += 1
                log.error("Qdrant upsert failed (%d points): %s", len(points), exc)
                log.debug(traceback.format_exc())

    prod_t = threading.Thread(target=producer, name="phaseB-producer", daemon=True)
    ups_t  = threading.Thread(target=upserter, name="phaseB-upserter", daemon=True)
    prod_t.start()
    ups_t.start()

    # ── stage 2: embed consumer (GPU) — main thread, back-to-back embed calls ───
    session = requests.Session()
    while True:
        batch = embed_q.get()
        if batch is DONE:
            break
        texts = [t for _, t, _ in batch]
        try:
            embeddings = embed_texts(session, texts, args.ollama_url)
        except Exception as exc:
            stats["embed_errs"] += 1
            log.error("Embed batch failed (%d texts): %s", len(texts), exc)
            log.debug(traceback.format_exc())
            continue
        points = [{"id": pid, "vector": vec, "payload": payload}
                  for (pid, _, payload), vec in zip(batch, embeddings)]
        upsert_q.put(points)

    upsert_q.put(DONE)
    prod_t.join()
    ups_t.join()

    elapsed = time.monotonic() - run_start
    log.info("Phase B complete: %s  arts=%d  chunks=%d  skipped=%d  "
             "embed_errs=%d  upsert_errs=%d  elapsed=%.1fs  avg=%.1f art/s",
             zim_name, stats["arts"], stats["chunks"], stats["skipped"],
             stats["embed_errs"], stats["upsert_errs"], elapsed,
             stats["arts"] / max(elapsed, 0.001))

    return stats["arts"], stats["chunks"]


# ── zim ingestion ─────────────────────────────────────────────────────────────

def process_zim(
    zim_path: Path,
    collection_id: str,
    cap: int,
    zim_state: dict,
    args,
) -> tuple[int, int]:
    """
    Two-phase ZIM ingestion:
      Phase A — crawl Kiwix HTTP → disk (bounded memory, explicit object free)
      Phase B — disk → chunk → embed (large batches) → Qdrant → delete files
    """
    zim_name = zim_path.stem
    out_dir  = Path(args.tmp_dir) / zim_name

    already_seen: set = set(zim_state.get("seen_urls", []))
    log.info("Processing ZIM: %s  (seen=%d, cap=%s)",
             zim_name, len(already_seen), cap or "none")

    # ── Phase A ───────────────────────────────────────────────────────────────
    # If out_dir already has files from a previous interrupted run, skip crawl.
    existing = list(out_dir.glob("*.json")) if out_dir.exists() else []
    if existing:
        log.info("Phase A: resuming from %d existing disk files in %s",
                 len(existing), out_dir)
    else:
        try:
            if args.crawl_mode == "http":
                n_written = crawl_to_disk(zim_name, args.kiwix_url,
                                          out_dir, already_seen, cap)
            else:
                # libzim-direct: reads the .zim index, reaches every article
                # (incl. JS-SPA ZIMs), no Kiwix HTTP. total_html drives the
                # "0 articles but many entries → retry" heuristic upstream.
                n_written, total_html = crawl_to_disk_libzim(
                    zim_path, out_dir, already_seen, cap)
                zim_state["total_entries"] = total_html
        except KeyboardInterrupt:
            log.warning("Interrupted during Phase A — disk files preserved for resume")
            raise
        if n_written == 0:
            log.warning("Phase A wrote 0 files for %s — nothing to embed", zim_name)
            # Still mark seen_urls so we don't re-crawl on next run
            zim_state["seen_urls"] = list(already_seen)
            return 0, 0

    # ── Phase B — run in a subprocess so OS reclaims all memory on exit ─────────
    # Python's glibc malloc holds arenas even after gc+free, causing RSS to grow
    # unboundedly across 4000+ files. A subprocess solves this definitively:
    # every byte allocated during Phase B is returned to the OS when it exits.
    result_file = Path(args.tmp_dir) / f"{zim_name}.phase_b_result.json"
    cmd = [
        sys.executable, __file__,
        "--_phase-b-subprocess",
        "--_pb-out-dir",       str(out_dir),
        "--_pb-zim-name",      zim_name,
        "--_pb-collection-id", collection_id,
        "--_pb-cap",           str(cap),
        "--_pb-result-file",   str(result_file),
        "--ollama-url",        args.ollama_url,
        "--qdrant-url",        args.qdrant_url,
    ]
    log.info("Phase B subprocess: %s", zim_name)
    try:
        proc = subprocess.run(cmd, timeout=None,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.stdout:
            for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
                log.info("  [B-sub] %s", line)
        if proc.returncode != 0:
            log.error("Phase B subprocess exited %d for %s", proc.returncode, zim_name)
            raise RuntimeError(
                f"Phase B subprocess killed (exit {proc.returncode}) for {zim_name}"
            )
    except subprocess.TimeoutExpired:
        log.error("Phase B subprocess timed out for %s", zim_name)
        raise RuntimeError(f"Phase B subprocess timed out for {zim_name}")
    except KeyboardInterrupt:
        log.warning("Interrupted during Phase B subprocess — disk files preserved")
        raise

    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
        result_file.unlink(missing_ok=True)
        arts, chunks = result["arts"], result["chunks"]
    except Exception as exc:
        log.error("Could not read Phase B result for %s: %s", zim_name, exc)
        return 0, 0

    # Update state with URLs seen during this crawl
    crawled = {json.loads(f.read_text(encoding="utf-8"))["url"]
               for f in out_dir.glob("*.json")} if out_dir.exists() else set()
    zim_state["seen_urls"]   = list(already_seen | crawled)
    zim_state["chunks_done"] = zim_state.get("chunks_done", 0) + chunks

    # Clean up temp dir for this ZIM
    try:
        if out_dir.exists():
            import shutil
            shutil.rmtree(out_dir)
            log.info("Temp dir cleaned: %s", out_dir)
    except Exception as exc:
        log.warning("Could not remove temp dir %s: %s", out_dir, exc)

    return arts, chunks


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_check(args) -> bool:
    print("=== Dependency + connectivity check ===\n")
    ok = True

    # libzim
    try:
        import libzim
        ver = getattr(libzim, "__version__", "unknown")
        print(f"  [OK]   libzim  {ver}")
        log.info("libzim %s", ver)
    except ImportError as e:
        print(f"  [FAIL] libzim: {e}")
        print("         Install: pip3 install libzim")
        log.error("libzim missing: %s", e)
        ok = False

    # beautifulsoup4
    try:
        import bs4
        print(f"  [OK]   beautifulsoup4  {bs4.__version__}")
    except ImportError:
        print("  [FAIL] beautifulsoup4 — install: pip3 install beautifulsoup4")
        ok = False

    print()

    # Ollama
    try:
        t0 = time.monotonic()
        resp = requests.post(
            f"{args.ollama_url}/api/embed",
            json={"model": EMBED_MODEL, "input": ["connectivity test"]},
            timeout=30,
        )
        resp.raise_for_status()
        emb = resp.json()["embeddings"][0]
        elapsed = (time.monotonic() - t0) * 1000
        dim_ok = len(emb) == EMBED_DIM
        status = "OK" if dim_ok else "WARN"
        print(f"  [{status}]   Ollama  {EMBED_MODEL} → {len(emb)}-dim  ({elapsed:.0f}ms)")
        if not dim_ok:
            print(f"         Expected {EMBED_DIM}-dim — update EMBED_DIM in script if model changed.")
        log.info("Ollama embed test: %d-dim in %.0fms", len(emb), elapsed)
    except Exception as e:
        print(f"  [FAIL] Ollama: {e}")
        log.error("Ollama check failed: %s", e)
        ok = False

    # Qdrant
    try:
        t0 = time.monotonic()
        resp = requests.get(f"{args.qdrant_url}/healthz", timeout=10)
        resp.raise_for_status()
        elapsed = (time.monotonic() - t0) * 1000
        print(f"  [OK]   Qdrant  ({args.qdrant_url})  ({elapsed:.0f}ms)")
        log.info("Qdrant healthy in %.0fms", elapsed)
    except Exception as e:
        print(f"  [FAIL] Qdrant: {e}")
        print("         Run:  docker compose -f compose/qdrant.yml up -d")
        log.error("Qdrant check failed: %s", e)
        ok = False

    # Open WebUI
    try:
        t0 = time.monotonic()
        resp = requests.get(f"{args.webui_url}/health", timeout=10)
        resp.raise_for_status()
        elapsed = (time.monotonic() - t0) * 1000
        print(f"  [OK]   Open WebUI  ({args.webui_url})  ({elapsed:.0f}ms)")
        log.info("Open WebUI healthy in %.0fms", elapsed)
    except Exception as e:
        print(f"  [WARN] Open WebUI: {e}  (needed for collection registration)")
        log.warning("Open WebUI check: %s", e)

    # Kiwix (article source)
    try:
        t0 = time.monotonic()
        resp = requests.get(f"{args.kiwix_url}/catalog/v2/entries",
                            params={"count": 1}, timeout=10)
        resp.raise_for_status()
        elapsed = (time.monotonic() - t0) * 1000
        print(f"  [OK]   Kiwix  ({args.kiwix_url})  ({elapsed:.0f}ms)")
        log.info("Kiwix healthy in %.0fms", elapsed)
    except Exception as e:
        print(f"  [FAIL] Kiwix: {e}")
        print("         Kiwix must be running — articles are fetched via HTTP")
        log.error("Kiwix check failed: %s", e)
        ok = False

    print()

    # ZIM directory
    zim_dir = Path(args.zim_dir)
    zims = sorted(zim_dir.glob("*.zim"))
    if zims:
        total_gb = sum(z.stat().st_size for z in zims) / 1e9
        print(f"  [OK]   ZIM dir  {len(zims)} files, {total_gb:.1f} GB  ({zim_dir})")
        log.info("ZIM dir: %d files, %.1f GB", len(zims), total_gb)
    else:
        print(f"  [FAIL] ZIM dir: no .zim files at {zim_dir}")
        log.error("No ZIM files found at %s", zim_dir)
        ok = False

    # Quick libzim open test
    if ok and zims:
        try:
            from libzim.reader import Archive
            arc = Archive(str(zims[0]))
            print(f"  [OK]   libzim open  {zims[0].name}  ({arc.entry_count:,} entries)")
            log.info("libzim open test passed: %s (%d entries)", zims[0].name, arc.entry_count)
        except Exception as e:
            print(f"  [FAIL] libzim cannot open ZIM: {e}")
            log.error("libzim open test failed: %s\n%s", e, traceback.format_exc())
            ok = False

    print()
    print(f"Log file: {LOG_FILE}")
    print()
    if ok:
        print("All checks passed — ready to run.")
    else:
        print("Fix the failures above before running ingestion.")
    return ok


def cmd_status(args):
    state = load_state()
    if not state.get("collections"):
        print("No ingestion state found. Run --check first, then start ingestion.")
        return

    for col_name, cs in state["collections"].items():
        cid        = cs.get("collection_id", "not created")
        done_zims  = cs.get("zims_done", [])
        in_prog    = cs.get("zims_in_progress", {})
        total_ch   = cs.get("total_chunks", 0)
        print(f"\n{'─'*60}")
        print(f"  {col_name}")
        print(f"  Collection ID : {cid}")
        print(f"  ZIMs done     : {len(done_zims)}")
        for z in done_zims:
            print(f"    ✓ {z}")
        if in_prog:
            print(f"  In progress:")
            for zim_name, info in in_prog.items():
                arts   = info.get("articles_done", 0)
                chunks = info.get("chunks_done", 0)
                print(f"    … {zim_name}  ({arts:,} articles, {chunks:,} chunks)")
        print(f"  Total chunks  : {total_ch:,}")
    print()


def cmd_ingest(args):
    session = requests.Session()
    session.headers["User-Agent"] = "ingest-rag/2.0"

    api_key = get_api_key(args, session)

    # WebUI stability check before doing anything
    if not wait_for_webui(args.webui_url, api_key):
        sys.exit("Aborting: Open WebUI is not stable. Check container health.")

    # Discover ZIMs
    zim_dir = Path(args.zim_dir)
    all_zims = sorted(zim_dir.glob("*.zim"))
    if not all_zims:
        sys.exit(f"No .zim files found in {zim_dir}")

    log.info("Found %d ZIMs in %s", len(all_zims), zim_dir)

    # Apply --only-zims filter
    if args.only_zims:
        all_zims = [z for z in all_zims if any(f in z.stem for f in args.only_zims)]
        if not all_zims:
            sys.exit("No ZIMs matched --only-zims filter.")
        log.info("Filtered to %d ZIMs via --only-zims", len(all_zims))

    # Assign to collections
    assigned  = {col["name"]: [] for col in COLLECTIONS}
    unmatched = []
    for zim in all_zims:
        placed = False
        for col in COLLECTIONS:
            if any(m in zim.stem for m in col["match"]):
                assigned[col["name"]].append(zim)
                placed = True
                break
        if not placed:
            unmatched.append(zim)

    if unmatched:
        log.warning("%d ZIM(s) matched no collection — skipped:", len(unmatched))
        for z in unmatched:
            log.warning("  unmatched: %s", z.name)

    state = load_state()
    if "collections" not in state:
        state["collections"] = {}

    grand_articles = 0
    grand_chunks   = 0
    run_start      = time.monotonic()

    ensure_qdrant_collection(args.qdrant_url)

    for col_def in COLLECTIONS:
        col_name  = col_def["name"]
        col_zims  = assigned[col_name]
        if not col_zims:
            continue

        log.info("=" * 60)
        log.info("COLLECTION: %s  (%d ZIM(s))", col_name, len(col_zims))

        col_state = state["collections"].setdefault(
            col_name,
            {"collection_id": None, "zims_done": [], "zims_in_progress": {}, "total_chunks": 0},
        )

        collection_id = ensure_webui_collection(
            session, api_key, col_state, col_name, col_def["desc"], args.webui_url,
        )
        save_state(state)

        col_start    = time.monotonic()
        col_articles = 0
        col_chunks   = 0
        col_errors   = 0

        for zim_path in col_zims:
            zim_name = zim_path.stem

            if zim_name in col_state.get("zims_done", []):
                log.info("SKIP (already complete): %s", zim_name)
                continue

            # Resolve effective cap
            cap = 0
            for pattern, c in col_def["caps"].items():
                if pattern in zim_name:
                    cap = c
                    break
            if args.max_per_zim and (cap == 0 or args.max_per_zim < cap):
                cap = args.max_per_zim

            log.info("─" * 50)
            log.info("ZIM: %s  cap=%s", zim_name, cap or "none")

            zim_state = col_state["zims_in_progress"].setdefault(
                zim_name, {"articles_done": 0, "chunks_done": 0}
            )

            try:
                arts, chunks = process_zim(zim_path, collection_id, cap, zim_state, args)
            except KeyboardInterrupt:
                save_state(state)
                log.info("Interrupted. State saved — re-run to resume.")
                sys.exit(0)
            except Exception as exc:
                log.error("ZIM processing failed: %s\n%s", exc, traceback.format_exc())
                col_errors += 1
                save_state(state)
                continue

            col_articles += arts
            col_chunks   += chunks
            grand_articles += arts
            grand_chunks   += chunks
            col_state["total_chunks"] = col_state.get("total_chunks", 0) + chunks

            zim_entry_count = zim_state.get("total_entries", 1)
            if arts == 0 and zim_entry_count > 10:
                log.warning("ZIM produced 0 articles from %d entries — NOT marking done, will retry: %s",
                            zim_entry_count, zim_name)
            else:
                # Mark complete once crawled + embedded. The cap is now enforced
                # in Phase A (crawl stops at `cap`), so reaching it is a clean
                # stop, not a partial run — embedding fewer than `cap` articles
                # (e.g. after short-article skips) must NOT block marking done.
                col_state.setdefault("zims_done", []).append(zim_name)
                col_state["zims_in_progress"].pop(zim_name, None)
                log.info("ZIM marked complete: %s", zim_name)

            save_state(state)

        # ── per-collection Discord notification ───────────────────────────────
        col_elapsed  = time.monotonic() - col_start
        done_zims    = col_state.get("zims_done", [])
        notify_color = _DISCORD_GREEN if col_errors == 0 else _DISCORD_ORANGE
        zim_lines    = "\n".join(f"✓ `{z}`" for z in done_zims) or "_none completed_"
        send_discord(
            title=f"📚 RAG phase complete — {col_name}",
            description=(
                f"**Articles ingested:** {col_articles:,}\n"
                f"**Chunks in Qdrant:** {col_chunks:,}\n"
                f"**ZIMs completed:** {len(done_zims)}\n"
                f"{zim_lines}\n\n"
                f"**Elapsed:** {col_elapsed / 60:.1f} min\n"
                f"**ZIM errors:** {col_errors}\n\n"
                f"Collection ID: `{col_state.get('collection_id', '?')}`\n"
                f"Verify: Open WebUI → Workspace → Knowledge → query the collection"
            ),
            color=notify_color,
        )

    total_elapsed = time.monotonic() - run_start
    log.info("=" * 60)
    log.info(
        "Run complete: %d articles, %d chunks  (total elapsed: %.1fs)",
        grand_articles, grand_chunks, total_elapsed,
    )
    log.info("State file: %s", STATE_FILE)
    log.info("Log file:   %s", LOG_FILE)
    save_state(state)

    # ── final run-complete notification ───────────────────────────────────────
    cols_done = [
        col["name"] for col in COLLECTIONS
        if state["collections"].get(col["name"], {}).get("zims_done")
    ]
    send_discord(
        title="✅ RAG ingest run finished",
        description=(
            f"**Chunks ingested this run:** {grand_chunks:,}\n"
            f"**Collections updated:** {len(cols_done)}\n"
            + "\n".join(f"  • {c}" for c in cols_done) +
            f"\n\n**Total elapsed:** {total_elapsed / 60:.1f} min\n"
            f"Log: `{LOG_FILE}`"
        ),
        color=_DISCORD_GREEN if grand_chunks > 0 else _DISCORD_ORANGE,
    )


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ingest Kiwix ZIMs directly into Qdrant for Open WebUI RAG",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check",  action="store_true", help="Verify deps and connectivity")
    mode.add_argument("--status", action="store_true", help="Show ingestion progress")

    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--api-key",  default="", help="Open WebUI API key")
    auth.add_argument("--password", default="", help="Open WebUI password (use with --email)")
    parser.add_argument("--email", default="", help="Open WebUI email")

    parser.add_argument("--ollama-url",  default=OLLAMA_BASE)
    parser.add_argument("--qdrant-url",  default=QDRANT_BASE)
    parser.add_argument("--webui-url",   default=WEBUI_BASE)
    parser.add_argument("--kiwix-url",   default=KIWIX_BASE)
    parser.add_argument("--zim-dir",     default=ZIM_DIR)
    parser.add_argument("--tmp-dir",     default=TMP_BASE,
                        help="Scratch dir for Phase A HTML files (deleted per ZIM after embed). "
                             "Use a LOCAL disk (e.g. /mnt/docker-local/zim-tmp), not the NFS mount.")
    parser.add_argument("--crawl-mode",  choices=["libzim", "http"], default="libzim",
                        help="Phase A source: 'libzim' reads the .zim index directly (reaches "
                             "every article, no Kiwix HTTP); 'http' BFS-crawls Kiwix (fallback).")
    parser.add_argument("--only-zims",   nargs="*", default=[], metavar="PATTERN",
                        help="Only process ZIMs whose filename contains one of these strings")
    parser.add_argument("--max-per-zim", type=int, default=0,
                        help="Global article cap per ZIM (0 = use per-collection defaults)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show DEBUG-level output on stdout (always written to log file)")

    # Hidden args used only by Phase B subprocess
    parser.add_argument("--_phase-b-subprocess", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--_pb-out-dir",       default="", help=argparse.SUPPRESS)
    parser.add_argument("--_pb-zim-name",      default="", help=argparse.SUPPRESS)
    parser.add_argument("--_pb-collection-id", default="", help=argparse.SUPPRESS)
    parser.add_argument("--_pb-cap",           type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--_pb-result-file",   default="", help=argparse.SUPPRESS)

    args = parser.parse_args()
    setup_logging(args.verbose)

    # ── Phase B subprocess entry point ────────────────────────────────────────
    if getattr(args, "_phase_b_subprocess", False):
        arts, chunks = embed_from_disk(
            out_dir=Path(args._pb_out_dir),
            zim_name=args._pb_zim_name,
            knowledge_base_id=args._pb_collection_id,
            cap=args._pb_cap,
            args=args,
        )
        Path(args._pb_result_file).write_text(
            json.dumps({"arts": arts, "chunks": chunks}), encoding="utf-8"
        )
        sys.exit(0)

    if args.check:
        sys.exit(0 if cmd_check(args) else 1)

    if args.status:
        cmd_status(args)
        sys.exit(0)

    if not args.api_key and not args.password:
        parser.error("--api-key or --password is required for ingestion")
    if args.password and not args.email:
        parser.error("--email is required when using --password")

    cmd_ingest(args)


if __name__ == "__main__":
    main()
