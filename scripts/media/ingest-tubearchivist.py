#!/usr/bin/env python3
"""
ingest-tubearchivist.py — TubeArchivist → Qdrant ingestion for Open WebUI

Sibling of ingest-rag.py (Kiwix→Qdrant). Same destination pipeline:
chunk → embed via Ollama nomic-embed-text (768-dim) → write vectors straight
into Qdrant under an Open WebUI knowledge collection UUID. Fully resumable.

Source differs: instead of ZIM files, this reads the TubeArchivist archive.
  • Metadata (title, channel, description, tags, published) via the TA REST
    API on :8000  (Token auth — same header TubeArchivist uses everywhere).
  • Transcripts from the .vtt subtitle files on the /youtube media mount
    (<media-root>/<channel_id>/<youtube_id>.<lang>.vtt). Read locally → no
    per-segment HTTP, mirrors ingest-rag.py reading ZIMs straight off the mount.

Each video contributes:
  • one or more *metadata* chunks  (title + channel + tags + description)
  • timestamp-aware *transcript* chunks, each deep-linking to the moment in the
    video:  https://www.youtube.com/watch?v=<id>&t=<seconds>s
Videos with no downloaded subtitles are still indexed via their metadata chunk.

Collections are grouped by CATEGORY (see CATEGORIES below): each archived
channel is mapped into a category → one Open WebUI knowledge collection per
category. Channels that match no category fall into a default bucket so nothing
is silently dropped.

Usage:
  python3 ingest-tubearchivist.py --api-key OW_KEY --ta-token TA_TOKEN [options]
  python3 ingest-tubearchivist.py --email E --password P --ta-token TA_TOKEN
  python3 ingest-tubearchivist.py --check        # deps + connectivity
  python3 ingest-tubearchivist.py --status       # ingestion progress
  python3 ingest-tubearchivist.py --dry-run --ta-token T   # plan only, no embed

Deps (host):  pip3 install requests
Log file:  ingest-tubearchivist.log (same dir as this script)
"""

import argparse
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

import requests

# ── defaults ──────────────────────────────────────────────────────────────────
OLLAMA_BASE  = "http://<server-ip>:11434"
QDRANT_BASE  = "http://<server-ip>:6333"
WEBUI_BASE   = "http://<server-ip>:3000"
TA_BASE      = "http://<server-ip>:8000"
YT_MEDIA_DIR = "/mnt/storage/media/youtube"   # host mount of the TA /youtube volume

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE   = os.path.join(SCRIPT_DIR, "ingest-tubearchivist-state.json")
LOG_FILE     = os.path.join(SCRIPT_DIR, "ingest-tubearchivist.log")

WATCHDOG_ENV = "/etc/container-watchdog.env"

# Discord embed colors
_DISCORD_GREEN  = 2244095
_DISCORD_ORANGE = 16763904
_DISCORD_RED    = 16711680

EMBED_MODEL   = "nomic-embed-text"
EMBED_DIM     = 768
CHUNK_CHARS   = 1600   # ~400 tokens at ~4 chars/token  (matches ingest-rag.py)
CHUNK_OVERLAP = 200
MIN_TEXT_LEN  = 80     # transcripts are denser than wiki HTML; keep short cues
MAX_TEXT_LEN  = 200_000
EMBED_BATCH   = 256    # texts per Ollama /api/embed call
QDRANT_BATCH  = 512    # points per Qdrant upsert

DEFAULT_LANGS = ["en", "en-US", "en-GB"]   # subtitle language preference order

WEBUI_HEALTH_RETRIES = 5
WEBUI_HEALTH_WAIT    = 10

STATE_SAVE_EVERY = 25   # save state every N videos within a collection

# Deterministic UUID namespace for point IDs (distinct from ingest-rag.py's)
_UUID_NS = uuid.UUID("b51c8e44-2d77-4a19-9c3e-7e2a6f0d4c81")

# ── collection definitions (CATEGORY → channels) ───────────────────────────────
# Map each archived channel into a category. `match` substrings are tested
# case-insensitively against BOTH the channel name and the channel_id, so you
# can use a readable channel name OR its stable UC… id.
#
# Fill these in for YOUR archive (run `--dry-run` first to see channel names +
# how they'd be assigned). Any channel matching no category lands in
# DEFAULT_COLLECTION below — so it is safe to leave categories sparse.
CATEGORIES = [
    {
        "name": "TubeArchivist — Tech & Homelab",
        "desc": ("Self-hosting, homelab, networking, programming, sysadmin and "
                 "hardware channels archived in TubeArchivist."),
        "match": [
            "Louis Rossmann",
            "Jeff Geerling",
            "Techno Tim",
            "NetworkChuck",
            "Lawrence Systems",
            "Craft Computing",
            "Wolfgang",
            "Hobotech",
            "Will Prowse",
        ],
    },
    {
        "name": "TubeArchivist — Making & DIY",
        "desc": ("Woodworking, 3D printing, electronics, repair, home improvement "
                 "and other hands-on maker channels."),
        "match": [
            "SolderingGeek",
            "Stefan Gotteswinter",
            "oxtoolco",
            "Clickspring",
            "hambini",
            "This Old Tony",
            "NYC CNC",
            "Inheritance Machining",
            "CNC Kitchen",
            "Print That Thing",
            "Electrician U",
            "Adam Savage",
            "Tested",
            "AvE",
            "DIY Perks",
            "bigclivedotcom",
        ],
    },
    {
        "name": "TubeArchivist — Cooking & Food",
        "desc": "Cooking, baking, food science and recipe channels.",
        "match": [
            "Adam Ragusea",
            "Ethan Chlebowski",
            "J. Kenji",
            "Kenji Lopez-Alt",
        ],
    },
    {
        "name": "TubeArchivist — Education & Science",
        "desc": ("Science, math, history, engineering explainers and lectures."),
        "match": [
            "3Blue1Brown",
            "Practical Engineering",
            "Technology Connections",
            "Veritasium",
        ],
    },
]

# Catch-all for channels that match no category above. Set to None to instead
# SKIP unmatched channels (they'll be reported but not ingested).
DEFAULT_COLLECTION = {
    "name": "TubeArchivist — Uncategorized",
    "desc": "Archived YouTube videos whose channel is not mapped to a category.",
}

# ── logging ───────────────────────────────────────────────────────────────────

log = logging.getLogger("ingest-tubearchivist")


def setup_logging(verbose: bool = False):
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
    log.info("ingest-tubearchivist starting  |  log: %s", LOG_FILE)


# ── discord notifications ─────────────────────────────────────────────────────

def _load_discord_env():
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
    _load_discord_env()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url:
        log.debug("Discord webhook not configured — skipping notification")
        return
    payload = json.dumps({
        "embeds": [{
            "title": title, "description": description, "color": color,
            "footer": {"text": f"ingest-tubearchivist  •  {time.strftime('%Y-%m-%d %H:%M:%S')}"},
        }]
    })
    try:
        result = subprocess.run(
            ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
             "-X", "POST", webhook_url,
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        code = result.stdout.strip()
        if code == "204":
            log.info("Discord notification sent: %s", title)
        else:
            log.warning("Discord webhook returned HTTP %s for: %s", code, title)
    except Exception as e:
        log.warning("Discord notification failed: %s", e)


# ── state ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"collections": {}}


def save_state(state: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)
    log.debug("State saved to %s", STATE_FILE)


# ── text processing ───────────────────────────────────────────────────────────

_WS_RE = re.compile(r"[ \t]{2,}")


def chunk_text_with_pos(text: str) -> list[tuple[str, int]]:
    """Char-window chunker (same boundaries as ingest-rag.py) that also returns
    each chunk's starting character offset, so transcript chunks can be mapped
    back to a timestamp.  Returns [(chunk_text, start_char), ...]."""
    text = text[:MAX_TEXT_LEN]
    n = len(text)
    if n == 0:
        return []
    if n <= CHUNK_CHARS:
        return [(text, 0)]
    chunks: list[tuple[str, int]] = []
    start = 0
    while start < n:
        end = min(start + CHUNK_CHARS, n)
        if end < n:
            boundary = text.rfind(". ", start + CHUNK_CHARS // 2, end)
            if boundary != -1:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append((chunk, start))
        if end >= n:
            break
        start = end - CHUNK_OVERLAP
    return chunks


def chunk_text(text: str) -> list[str]:
    return [c for c, _ in chunk_text_with_pos(text)]


# ── VTT (WebVTT) subtitle parsing ──────────────────────────────────────────────

_VTT_TS = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
)
_VTT_TAG = re.compile(r"<[^>]+>")          # <00:00:01.000>, <c>, </c>, <v ...>
_VTT_CUE_SETTINGS = re.compile(r"\s+(align|position|size|line|vertical):\S+")


def _ts_to_seconds(h, m, s, ms) -> float:
    h = int(h) if h else 0
    return h * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_vtt(path: Path) -> list[tuple[float, str]]:
    """Parse a .vtt file into [(start_seconds, text_line), ...].

    Handles YouTube auto-caption quirks: inline word-timing tags, cue position
    settings, and the rolling-duplicate lines auto-captions emit (each cue
    repeats the previous line then adds one). Consecutive duplicate lines are
    collapsed so the transcript reads once-through.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.warning("Could not read VTT %s: %s", path, exc)
        return []

    cues: list[tuple[float, str]] = []
    cur_start: float | None = None
    cur_lines: list[str] = []

    def flush():
        nonlocal cur_start, cur_lines
        if cur_start is not None and cur_lines:
            text = " ".join(cur_lines).strip()
            text = _WS_RE.sub(" ", text)
            if text:
                cues.append((cur_start, text))
        cur_start, cur_lines = None, []

    for line in raw.splitlines():
        m = _VTT_TS.search(line)
        if m:
            flush()
            cur_start = _ts_to_seconds(m.group(1), m.group(2), m.group(3), m.group(4))
            continue
        if cur_start is None:
            continue  # header / NOTE / STYLE / cue-id lines outside a cue
        if not line.strip():
            flush()
            continue
        txt = _VTT_TAG.sub("", line)
        txt = _VTT_CUE_SETTINGS.sub("", txt).strip()
        if txt:
            cur_lines.append(txt)
    flush()

    # Collapse consecutive duplicate lines (rolling auto-captions).
    deduped: list[tuple[float, str]] = []
    last_text = None
    for start, text in cues:
        if text == last_text:
            continue
        deduped.append((start, text))
        last_text = text
    return deduped


def transcript_chunks(cues: list[tuple[float, str]]) -> list[tuple[str, float]]:
    """Window cue text into ~CHUNK_CHARS chunks, tagging each with the start
    timestamp (seconds) of the cue active at the chunk's first character.
    Returns [(chunk_text, start_seconds), ...]."""
    if not cues:
        return []
    buf_parts: list[str] = []
    offsets: list[tuple[int, float]] = []   # (char_offset, start_seconds)
    pos = 0
    for start, text in cues:
        offsets.append((pos, start))
        piece = text + " "
        buf_parts.append(piece)
        pos += len(piece)
    full = "".join(buf_parts).strip()

    out: list[tuple[str, float]] = []
    for chunk, start_char in chunk_text_with_pos(full):
        # nearest offset whose char position is <= start_char
        ts = offsets[0][1]
        for off, sec in offsets:
            if off <= start_char:
                ts = sec
            else:
                break
        out.append((chunk, ts))
    return out


def make_point_id(youtube_id: str, kind: str, lang: str, idx: int) -> str:
    return str(uuid.uuid5(_UUID_NS, f"{youtube_id}|{kind}|{lang}|{idx}"))


# ── TubeArchivist REST API ─────────────────────────────────────────────────────

def ta_headers(token: str) -> dict:
    return {"Authorization": f"Token {token}", "Accept": "application/json"}


def ta_iter_videos(session: requests.Session, ta_url: str, token: str):
    """Yield video objects from /api/video/ across all pages."""
    page = 1
    seen = 0
    while True:
        try:
            resp = session.get(f"{ta_url}/api/video/",
                               headers=ta_headers(token),
                               params={"page": page}, timeout=60)
        except Exception as exc:
            log.error("TA API request failed (page %d): %s", page, exc)
            raise
        if resp.status_code == 404:
            break  # past the last page
        if not resp.ok:
            log.error("TA API HTTP %s on page %d: %s", resp.status_code, page, resp.text[:200])
            resp.raise_for_status()
        body = resp.json()
        data = body.get("data", []) if isinstance(body, dict) else body
        if not data:
            break
        for vid in data:
            seen += 1
            yield vid
        paginate = body.get("paginate", {}) if isinstance(body, dict) else {}
        nxt = paginate.get("next_pages") or []
        if not nxt and len(data) == 0:
            break
        page += 1
    log.info("TA API: enumerated %d videos", seen)


def extract_video_fields(vid: dict) -> dict:
    """Normalize the bits we need from a TA video object (defensive .get's)."""
    ch = vid.get("channel") or {}
    subs = vid.get("subtitles") or []
    sub_list = []
    for s in subs:
        if not isinstance(s, dict):
            continue
        sub_list.append({
            "lang":   s.get("lang") or "",
            "source": s.get("source") or "",
            "url":    s.get("url") or "",
            "name":   s.get("name") or "",
        })
    return {
        "youtube_id":   vid.get("youtube_id") or "",
        "title":        (vid.get("title") or "").strip(),
        "description":  (vid.get("description") or "").strip(),
        "tags":         vid.get("tags") or [],
        "published":    vid.get("published") or "",
        "channel_name": (ch.get("channel_name") or "").strip(),
        "channel_id":   ch.get("channel_id") or "",
        "duration":     (vid.get("player") or {}).get("duration", 0),
        "subtitles":    sub_list,
    }


# ── subtitle file resolution ───────────────────────────────────────────────────

def find_vtt(v: dict, media_dir: Path, langs: list[str]) -> tuple[Path | None, str]:
    """Locate the best .vtt on disk for a video. Preference: requested langs in
    order, source 'user' over 'auto'. Returns (path_or_None, lang)."""
    yid, cid = v["youtube_id"], v["channel_id"]

    def disk_path_for(url: str) -> Path | None:
        # url like "/media/<channel_id>/<id>.en.vtt" → media_dir/<channel_id>/...
        if not url:
            return None
        rel = url
        if rel.startswith("/media/"):
            rel = rel[len("/media/"):]
        elif rel.startswith("media/"):
            rel = rel[len("media/"):]
        p = media_dir / rel
        return p if p.exists() else None

    # 1) Use the API-declared subtitle list, ranked.
    def rank(s):
        lang_rank = langs.index(s["lang"]) if s["lang"] in langs else len(langs)
        src_rank = 0 if s["source"] == "user" else 1
        return (lang_rank, src_rank)

    for s in sorted(v["subtitles"], key=rank):
        if s["lang"] and langs and s["lang"] not in langs and not any(
                s["lang"].startswith(l.split("-")[0]) for l in langs):
            continue
        p = disk_path_for(s["url"])
        if p:
            return p, s["lang"]

    # 2) Fall back to globbing the channel dir for any preferred-lang vtt.
    if cid and yid:
        chan_dir = media_dir / cid
        if chan_dir.is_dir():
            for lang in langs:
                base = lang.split("-")[0]
                for cand in sorted(chan_dir.glob(f"{yid}*.vtt")):
                    name = cand.name.lower()
                    if f".{lang.lower()}.vtt" in name or f".{base}." in name:
                        return cand, lang
            any_vtt = sorted(chan_dir.glob(f"{yid}*.vtt"))
            if any_vtt:
                return any_vtt[0], "?"
    return None, ""


# ── building chunks for one video ──────────────────────────────────────────────

def build_video_points(v: dict, media_dir: Path, langs: list[str]) -> tuple[list[tuple], dict]:
    """Return ([(pid, text, payload), ...], stats) for one video."""
    yid = v["youtube_id"]
    if not yid:
        return [], {"transcript": 0, "metadata": 0, "had_vtt": False}

    watch = f"https://www.youtube.com/watch?v={yid}"
    base_meta = {
        "name":         v["title"] or yid,
        "youtube_id":   yid,
        "channel":      v["channel_name"],
        "channel_id":   v["channel_id"],
        "published":    v["published"],
    }
    points: list[tuple] = []

    # ── metadata chunk(s) ──────────────────────────────────────────────────────
    tags = ", ".join(t for t in v["tags"] if t) if v["tags"] else ""
    meta_text = (
        f"# {v['title']}\n"
        f"Channel: {v['channel_name']}\n"
        f"Published: {v['published']}\n"
        + (f"Tags: {tags}\n" if tags else "")
        + f"URL: {watch}\n\n"
        + (v["description"] or "")
    ).strip()
    n_meta = 0
    for ci, chunk in enumerate(chunk_text(meta_text)):
        if len(chunk) < 1:  # always keep at least the header
            continue
        pid = make_point_id(yid, "metadata", "-", ci)
        payload = {"text": chunk,
                   "metadata": {**base_meta, "source": watch, "kind": "metadata", "chunk_id": ci}}
        points.append((pid, chunk, payload))
        n_meta += 1

    # ── transcript chunk(s) ────────────────────────────────────────────────────
    n_tr = 0
    vtt_path, lang = find_vtt(v, media_dir, langs)
    had_vtt = vtt_path is not None
    if vtt_path:
        cues = parse_vtt(vtt_path)
        for ci, (chunk, start_sec) in enumerate(transcript_chunks(cues)):
            if len(chunk) < MIN_TEXT_LEN:
                continue
            t = int(start_sec)
            src = f"{watch}&t={t}s"
            pid = make_point_id(yid, "transcript", lang or "-", ci)
            payload = {"text": chunk,
                       "metadata": {**base_meta, "source": src, "kind": "transcript",
                                    "lang": lang, "start": t, "chunk_id": ci}}
            points.append((pid, chunk, payload))
            n_tr += 1

    return points, {"transcript": n_tr, "metadata": n_meta, "had_vtt": had_vtt}


# ── category assignment ────────────────────────────────────────────────────────

def assign_collection(v: dict, collections: list[dict]) -> str | None:
    """Return the collection name for a video's channel, or None to skip."""
    hay = f"{v['channel_name']}\n{v['channel_id']}".lower()
    for col in collections:
        if col is DEFAULT_COLLECTION:
            continue
        for m in col["match"]:
            if m and m.lower() in hay:
                return col["name"]
    if DEFAULT_COLLECTION:
        return DEFAULT_COLLECTION["name"]
    return None


def active_collections() -> list[dict]:
    cols = list(CATEGORIES)
    if DEFAULT_COLLECTION:
        cols.append(DEFAULT_COLLECTION)
    return cols


# ── ollama / qdrant / open webui (mirrors ingest-rag.py) ───────────────────────

def embed_texts(session: requests.Session, texts: list[str], ollama_url: str) -> list[list[float]]:
    resp = session.post(f"{ollama_url}/api/embed",
                        json={"model": EMBED_MODEL, "input": texts}, timeout=120)
    if not resp.ok:
        log.error("Ollama embed failed: HTTP %s — %s", resp.status_code, resp.text[:200])
        resp.raise_for_status()
    return resp.json()["embeddings"]


def ensure_qdrant_collection(qdrant_url: str, collection_id: str):
    resp = requests.get(f"{qdrant_url}/collections/{collection_id}", timeout=10)
    if resp.status_code == 200:
        info = resp.json().get("result", {})
        points = info.get("points_count", "?")
        log.info("Qdrant collection exists: %s  (%s points)", collection_id, points)
        return
    log.info("Creating Qdrant collection: %s  (dim=%d, Cosine)", collection_id, EMBED_DIM)
    payload = {"vectors": {"size": EMBED_DIM, "distance": "Cosine", "on_disk": True},
               "on_disk_payload": True}
    resp = requests.put(f"{qdrant_url}/collections/{collection_id}", json=payload, timeout=30)
    if not resp.ok:
        log.error("Failed to create Qdrant collection: HTTP %s — %s", resp.status_code, resp.text[:300])
        resp.raise_for_status()


def upsert_points(qdrant_url: str, collection_id: str, points: list[dict]):
    total = len(points)
    for i in range(0, total, QDRANT_BATCH):
        batch = points[i:i + QDRANT_BATCH]
        resp = requests.put(f"{qdrant_url}/collections/{collection_id}/points",
                            json={"points": batch}, params={"wait": "false"}, timeout=60)
        if not resp.ok:
            log.error("Qdrant upsert failed: HTTP %s — %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()


def wait_for_webui(webui_url: str, api_key: str = "") -> bool:
    log.info("Checking Open WebUI stability at %s ...", webui_url)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    for attempt in range(1, WEBUI_HEALTH_RETRIES + 1):
        try:
            r = requests.get(f"{webui_url}/health", timeout=10)
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}")
            if api_key:
                r2 = requests.get(f"{webui_url}/api/v1/knowledge/", headers=headers, timeout=15)
                if not r2.ok:
                    raise RuntimeError(f"Knowledge API HTTP {r2.status_code}: {r2.text[:120]}")
            log.info("Open WebUI is healthy (attempt %d/%d)", attempt, WEBUI_HEALTH_RETRIES)
            return True
        except Exception as e:
            log.warning("WebUI not ready (attempt %d/%d): %s", attempt, WEBUI_HEALTH_RETRIES, e)
            if attempt < WEBUI_HEALTH_RETRIES:
                time.sleep(WEBUI_HEALTH_WAIT)
    log.error("Open WebUI did not become healthy after %d attempts.", WEBUI_HEALTH_RETRIES)
    return False


def ensure_webui_collection(session, api_key, col_state, name, desc, webui_url) -> str:
    if col_state.get("collection_id"):
        return col_state["collection_id"]
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    resp = session.get(f"{webui_url}/api/v1/knowledge/", headers=headers, timeout=15)
    if resp.ok:
        body = resp.json()
        items = body.get("items", body) if isinstance(body, dict) else body
        for col in items:
            if col.get("name") == name:
                cid = col["id"]
                col_state["collection_id"] = cid
                log.info("Found existing OW collection: %r  (%s)", name, cid)
                return cid
    log.info("Creating OW knowledge collection: %r", name)
    resp = session.post(f"{webui_url}/api/v1/knowledge/create", headers=headers,
                        json={"name": name, "description": desc}, timeout=30)
    if not resp.ok:
        log.error("Failed to create collection %r: HTTP %s — %s", name, resp.status_code, resp.text[:300])
        resp.raise_for_status()
    cid = resp.json()["id"]
    col_state["collection_id"] = cid
    log.info("Created OW collection: %r  (%s)", name, cid)
    return cid


def get_api_key(args, session: requests.Session) -> str:
    if args.api_key:
        return args.api_key
    log.info("Logging in to Open WebUI as %s ...", args.email)
    for attempt in range(1, 4):
        try:
            resp = session.post(f"{args.webui_url}/api/v1/auths/signin",
                                json={"email": args.email, "password": args.password}, timeout=30)
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


def resolve_ta_token(args) -> str:
    if args.ta_token:
        return args.ta_token
    if args.ta_token_env and os.environ.get(args.ta_token_env):
        return os.environ[args.ta_token_env]
    if os.environ.get("TA_TOKEN"):
        return os.environ["TA_TOKEN"]
    return ""


# ── embed + upsert a batch of (pid, text, payload) ─────────────────────────────

def embed_and_upsert(session, items: list[tuple], collection_id: str, args) -> int:
    """Embed texts in EMBED_BATCH sub-batches and upsert to Qdrant. Returns
    number of points written."""
    written = 0
    pending_points: list[dict] = []
    for i in range(0, len(items), EMBED_BATCH):
        sub = items[i:i + EMBED_BATCH]
        texts = [t for _, t, _ in sub]
        vecs = embed_texts(session, texts, args.ollama_url)
        for (pid, _t, payload), vec in zip(sub, vecs):
            pending_points.append({"id": pid, "vector": vec, "payload": payload})
        if len(pending_points) >= QDRANT_BATCH:
            upsert_points(args.qdrant_url, collection_id, pending_points)
            written += len(pending_points)
            pending_points = []
    if pending_points:
        upsert_points(args.qdrant_url, collection_id, pending_points)
        written += len(pending_points)
    return written


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_check(args) -> bool:
    print("=== Dependency + connectivity check ===\n")
    ok = True

    try:
        import requests as _r  # noqa
        print(f"  [OK]   requests  {_r.__version__}")
    except ImportError:
        print("  [FAIL] requests — install: pip3 install requests")
        ok = False

    # TubeArchivist API
    token = resolve_ta_token(args)
    if not token:
        print("  [WARN] TA token not provided (--ta-token / --ta-token-env / $TA_TOKEN) — "
              "cannot test API auth")
    try:
        t0 = time.monotonic()
        resp = requests.get(f"{args.ta_url}/api/video/", headers=ta_headers(token),
                            params={"page": 1}, timeout=30)
        elapsed = (time.monotonic() - t0) * 1000
        if resp.status_code in (200, 404):
            body = resp.json() if resp.status_code == 200 else {}
            total = (body.get("paginate", {}) or {}).get("total_hits", "?")
            print(f"  [OK]   TubeArchivist  ({args.ta_url})  total_hits={total}  ({elapsed:.0f}ms)")
        elif resp.status_code in (401, 403):
            print(f"  [FAIL] TubeArchivist auth: HTTP {resp.status_code} — check token")
            ok = False
        else:
            print(f"  [WARN] TubeArchivist: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  [FAIL] TubeArchivist: {e}")
        ok = False

    # Media mount
    md = Path(args.media_dir)
    if md.is_dir():
        vtts = sum(1 for _ in md.glob("*/*.vtt"))
        print(f"  [OK]   Media mount  {md}  (~{vtts} .vtt files seen)")
        if vtts == 0:
            print("         No .vtt files found — transcripts unavailable; "
                  "only metadata will be indexed. Enable subtitle download in TA.")
    else:
        print(f"  [WARN] Media mount not found: {md}  (transcripts will be skipped)")

    print()

    # Ollama
    try:
        t0 = time.monotonic()
        resp = requests.post(f"{args.ollama_url}/api/embed",
                            json={"model": EMBED_MODEL, "input": ["connectivity test"]}, timeout=30)
        resp.raise_for_status()
        emb = resp.json()["embeddings"][0]
        elapsed = (time.monotonic() - t0) * 1000
        dim_ok = len(emb) == EMBED_DIM
        print(f"  [{'OK' if dim_ok else 'WARN'}]   Ollama  {EMBED_MODEL} → {len(emb)}-dim  ({elapsed:.0f}ms)")
        if not dim_ok:
            print(f"         Expected {EMBED_DIM}-dim — update EMBED_DIM if model changed.")
    except Exception as e:
        print(f"  [FAIL] Ollama: {e}")
        ok = False

    # Qdrant
    try:
        resp = requests.get(f"{args.qdrant_url}/healthz", timeout=10)
        resp.raise_for_status()
        print(f"  [OK]   Qdrant  ({args.qdrant_url})")
    except Exception as e:
        print(f"  [FAIL] Qdrant: {e}")
        ok = False

    # Open WebUI
    try:
        resp = requests.get(f"{args.webui_url}/health", timeout=10)
        resp.raise_for_status()
        print(f"  [OK]   Open WebUI  ({args.webui_url})")
    except Exception as e:
        print(f"  [WARN] Open WebUI: {e}  (needed for collection registration)")

    print(f"\nLog file: {LOG_FILE}\n")
    print("All checks passed — ready to run." if ok else "Fix the failures above before running.")
    return ok


def cmd_status(args):
    state = load_state()
    if not state.get("collections"):
        print("No ingestion state found. Run --check first, then start ingestion.")
        return
    for col_name, cs in state["collections"].items():
        cid   = cs.get("collection_id", "not created")
        done  = cs.get("videos_done", [])
        total = cs.get("total_chunks", 0)
        print(f"\n{'─'*60}")
        print(f"  {col_name}")
        print(f"  Collection ID : {cid}")
        print(f"  Videos done   : {len(done):,}")
        print(f"  Total chunks  : {total:,}")
    print()


def cmd_dry_run(args):
    session = requests.Session()
    session.headers["User-Agent"] = "ingest-tubearchivist/1.0"
    token = resolve_ta_token(args)
    if not token:
        sys.exit("Dry run needs a TA token (--ta-token / --ta-token-env / $TA_TOKEN).")

    media_dir = Path(args.media_dir)
    langs = args.langs or DEFAULT_LANGS
    cols = active_collections()

    per_col: dict[str, int] = {}
    per_channel: dict[str, dict] = {}
    n = 0
    with_vtt = 0
    for vid in ta_iter_videos(session, args.ta_url, token):
        v = extract_video_fields(vid)
        if args.only_channels and not any(
                c.lower() in f"{v['channel_name']} {v['channel_id']}".lower()
                for c in args.only_channels):
            continue
        n += 1
        col_name = assign_collection(v, cols) or "(skipped)"
        per_col[col_name] = per_col.get(col_name, 0) + 1
        ch = v["channel_name"] or v["channel_id"] or "(unknown)"
        d = per_channel.setdefault(ch, {"videos": 0, "vtt": 0, "collection": col_name})
        d["videos"] += 1
        vtt_path, _ = find_vtt(v, media_dir, langs)
        if vtt_path:
            d["vtt"] += 1
            with_vtt += 1
        if args.limit and n >= args.limit:
            break

    print(f"\n=== DRY RUN — {n} videos ({with_vtt} with a usable transcript) ===\n")
    print("Per collection:")
    for name, cnt in sorted(per_col.items(), key=lambda x: -x[1]):
        print(f"  {cnt:6,d}  {name}")
    print("\nPer channel (channel → collection : videos, transcripts):")
    for ch, d in sorted(per_channel.items(), key=lambda x: -x[1]["videos"]):
        print(f"  {d['videos']:5,d}  {d['vtt']:5,d}tr  {ch}  →  {d['collection']}")
    print("\nEdit CATEGORIES at the top of this script to map channels, then run for real.\n")


def cmd_ingest(args):
    session = requests.Session()
    session.headers["User-Agent"] = "ingest-tubearchivist/1.0"

    token = resolve_ta_token(args)
    if not token:
        sys.exit("A TubeArchivist token is required (--ta-token / --ta-token-env / $TA_TOKEN).")

    api_key = get_api_key(args, session)
    if not wait_for_webui(args.webui_url, api_key):
        sys.exit("Aborting: Open WebUI is not stable. Check container health.")

    media_dir = Path(args.media_dir)
    langs = args.langs or DEFAULT_LANGS
    cols = active_collections()

    # ── enumerate + group videos by collection ─────────────────────────────────
    log.info("Enumerating videos from TubeArchivist API ...")
    grouped: dict[str, list[dict]] = {}
    n_total = 0
    for vid in ta_iter_videos(session, args.ta_url, token):
        v = extract_video_fields(vid)
        if not v["youtube_id"]:
            continue
        if args.only_channels and not any(
                c.lower() in f"{v['channel_name']} {v['channel_id']}".lower()
                for c in args.only_channels):
            continue
        col_name = assign_collection(v, cols)
        if not col_name:
            continue
        grouped.setdefault(col_name, []).append(v)
        n_total += 1
        if args.limit and n_total >= args.limit:
            break
    log.info("Grouped %d videos into %d collection(s)", n_total, len(grouped))

    state = load_state()
    state.setdefault("collections", {})

    grand_videos = grand_chunks = 0
    run_start = time.monotonic()

    for col in active_collections():
        col_name = col["name"]
        vids = grouped.get(col_name, [])
        if not vids:
            continue

        log.info("=" * 60)
        log.info("COLLECTION: %s  (%d video(s))", col_name, len(vids))

        col_state = state["collections"].setdefault(
            col_name, {"name": col_name, "collection_id": None,
                       "videos_done": [], "total_chunks": 0})
        done = set(col_state.get("videos_done", []))

        collection_id = ensure_webui_collection(
            session, api_key, col_state, col_name, col["desc"], args.webui_url)
        ensure_qdrant_collection(args.qdrant_url, collection_id)
        save_state(state)

        col_videos = col_chunks = col_errors = col_skipped = 0
        col_start = time.monotonic()
        processed_since_save = 0

        for v in vids:
            yid = v["youtube_id"]
            if yid in done:
                col_skipped += 1
                continue
            try:
                items, st = build_video_points(v, media_dir, langs)
                if not items:
                    log.debug("No content for %s — skipping", yid)
                    done.add(yid)
                    continue
                written = embed_and_upsert(session, items, collection_id, args)
                col_videos += 1
                col_chunks += written
                grand_videos += 1
                grand_chunks += written
                col_state["total_chunks"] = col_state.get("total_chunks", 0) + written
                done.add(yid)
                log.debug("  %s  '%s'  +%d chunks (meta=%d tr=%d vtt=%s)",
                          yid, (v["title"] or "")[:60], written,
                          st["metadata"], st["transcript"], st["had_vtt"])
            except KeyboardInterrupt:
                col_state["videos_done"] = sorted(done)
                save_state(state)
                log.info("Interrupted. State saved — re-run to resume.")
                sys.exit(0)
            except Exception as exc:
                col_errors += 1
                log.error("Video %s failed: %s", yid, exc)
                log.debug(traceback.format_exc())

            processed_since_save += 1
            if processed_since_save >= STATE_SAVE_EVERY:
                col_state["videos_done"] = sorted(done)
                save_state(state)
                processed_since_save = 0
                rate = col_videos / max(time.monotonic() - col_start, 0.001)
                log.info("  %s: videos=%d  chunks=%d  errors=%d  rate=%.1f vid/s",
                         col_name, col_videos, col_chunks, col_errors, rate)

        col_state["videos_done"] = sorted(done)
        save_state(state)

        col_elapsed = time.monotonic() - col_start
        send_discord(
            title=f"📺 TubeArchivist RAG — {col_name}",
            description=(
                f"**Videos ingested this run:** {col_videos:,}\n"
                f"**Chunks added:** {col_chunks:,}\n"
                f"**Already done (skipped):** {col_skipped:,}\n"
                f"**Errors:** {col_errors}\n"
                f"**Elapsed:** {col_elapsed / 60:.1f} min\n\n"
                f"Collection ID: `{collection_id}`\n"
                f"Verify: Open WebUI → Workspace → Knowledge → query the collection"),
            color=_DISCORD_GREEN if col_errors == 0 else _DISCORD_ORANGE,
        )

    total_elapsed = time.monotonic() - run_start
    log.info("=" * 60)
    log.info("Run complete: %d videos, %d chunks  (%.1f min)",
             grand_videos, grand_chunks, total_elapsed / 60)
    save_state(state)
    send_discord(
        title="✅ TubeArchivist ingest run finished",
        description=(f"**Videos this run:** {grand_videos:,}\n"
                     f"**Chunks this run:** {grand_chunks:,}\n"
                     f"**Total elapsed:** {total_elapsed / 60:.1f} min\n"
                     f"Log: `{LOG_FILE}`"),
        color=_DISCORD_GREEN if grand_chunks > 0 else _DISCORD_ORANGE,
    )


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Ingest TubeArchivist videos (metadata + transcripts) into Qdrant for Open WebUI RAG",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check",   action="store_true", help="Verify deps and connectivity")
    mode.add_argument("--status",  action="store_true", help="Show ingestion progress")
    mode.add_argument("--dry-run", action="store_true",
                      help="Enumerate + show per-channel/collection plan (no embedding)")

    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--api-key",  default="", help="Open WebUI API key")
    auth.add_argument("--password", default="", help="Open WebUI password (use with --email)")
    parser.add_argument("--email", default="", help="Open WebUI email")

    parser.add_argument("--ta-token", default="", help="TubeArchivist API token (Settings → User)")
    parser.add_argument("--ta-token-env", default="", help="Env var name holding the TA token")

    parser.add_argument("--ollama-url", default=OLLAMA_BASE)
    parser.add_argument("--qdrant-url", default=QDRANT_BASE)
    parser.add_argument("--webui-url",  default=WEBUI_BASE)
    parser.add_argument("--ta-url",     default=TA_BASE)
    parser.add_argument("--media-dir",  default=YT_MEDIA_DIR,
                        help="Host mount of the TA /youtube volume (for .vtt files)")
    parser.add_argument("--langs", nargs="*", default=[], metavar="LANG",
                        help=f"Subtitle language preference order (default: {DEFAULT_LANGS})")
    parser.add_argument("--only-channels", nargs="*", default=[], metavar="SUBSTR",
                        help="Only ingest videos whose channel name/id contains one of these")
    parser.add_argument("--limit", type=int, default=0, help="Stop after N videos (testing)")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.check:
        sys.exit(0 if cmd_check(args) else 1)
    if args.status:
        cmd_status(args)
        sys.exit(0)
    if args.dry_run:
        cmd_dry_run(args)
        sys.exit(0)

    if not args.api_key and not args.password:
        parser.error("--api-key or --password is required for ingestion")
    if args.password and not args.email:
        parser.error("--email is required when using --password")

    cmd_ingest(args)


if __name__ == "__main__":
    main()
