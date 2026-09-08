#!/usr/bin/env python3
"""
Crawls all Kiwix ZIMs via BFS and uploads articles to an Open WebUI knowledge collection.
Resumable: progress saved to kiwix-rag-state.json after every BATCH_SIZE uploads.

WARNING: Full English Wikipedia and all of Stack Overflow are each millions of articles.
Running without --max-per-zim on those ZIMs would take weeks. Recommended starts:

  # All specialty ZIMs, cap full Wikipedia/SO at 10k each
  python upload-rag-kiwix.py --api-key <key> --max-per-zim 10000

  # Only the medicine and specialty Stack Exchange ZIMs (fast, ~hours)
  python upload-rag-kiwix.py --api-key <key> \\
    --only-zims medicine stackexchange wikipedia_en_simple cd3wd lowtechmag \\
                zimgit appropedia ifixit restarters freecodecamp libretexts

  # Resume after interruption (re-run same command — already-uploaded URLs are skipped)
  python upload-rag-kiwix.py --api-key <key> [same flags]

Dependencies: pip install requests beautifulsoup4
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# ── defaults ──────────────────────────────────────────────────────────────────
KIWIX_BASE = "http://<server-ip>:8086"
WEBUI_BASE = "http://<server-ip>:3000"
STATE_FILE = os.path.join(os.path.dirname(__file__), "kiwix-rag-state.json")
BATCH_SIZE = 25   # save state every N uploads
RATE_DELAY = 0.03 # seconds between Kiwix requests (~33 req/s)
MIN_TEXT_LEN = 150  # skip articles with fewer chars of extracted text
MAX_TEXT_LEN = 8000  # truncate oversized pages to keep embedding fast

# ── collection definitions ────────────────────────────────────────────────────
# Each collection gets its own key in state["collections"][name].
# ZIM matching: any ZIM whose path contains one of the listed substrings goes
# into that collection. Order matters — first match wins.
COLLECTIONS = [
    {
        "name": "Kiwix — Medical",
        "desc": (
            "Offline medical knowledge: WikiMed Medical Encyclopedia, NHS Medicines A-Z, "
            "Medical Library, Medicine LibreTexts, TED Medicine talks, "
            "Libre Pathology, Military Medicine."
        ),
        "match": [
            "wikipedia_en_medicine",
            "nhs_uk",
            "zimgit-medicine",
            "libretexts_org_en_med",
            "ted_mul_medicine",
            "librepathology",
            "fas-military-medicine",
        ],
    },
    {
        "name": "Kiwix — Technical",
        "desc": (
            "Technical Q&A and references: Stack Overflow, Server Fault, Unix & Linux, "
            "Electronics, Physics, Chemistry, Biology, 3D Printing, Data Science, "
            "Amateur Radio, FreeCodeCamp, iFixit repair guides, Restarters."
        ),
        "match": [
            "stackoverflow_en",
            "serverfault.com",
            "unix.stackexchange",
            "electronics.stackexchange",
            "physics.stackexchange",
            "chemistry.stackexchange",
            "biology.stackexchange",
            "3dprinting.stackexchange",
            "datascience_stackexchange",
            "ham.stackexchange",
            "freecodecamp",
            "ifixit",
            "restarters",
        ],
    },
    {
        "name": "Kiwix — Practical & Reference",
        "desc": (
            "Practical skills, survival, and general reference: Home Improvement, Woodworking, "
            "Mechanics, Gardening, Sustainability, Cooking, CD3WD, Low-tech Magazine, "
            "Post-Disaster Library, Urban Prepper, Food Prep, Water Treatment, Knots, "
            "Appropedia, FOSS Cooking, Wikipedia (Simple + Full), Wikibooks, Wikiversity, "
            "Wikivoyage, wikiHow, Project Gutenberg."
        ),
        "match": [
            "diy.stackexchange",
            "woodworking.stackexchange",
            "mechanics.stackexchange",
            "gardening.stackexchange",
            "sustainability.stackexchange",
            "cooking.stackexchange",
            "cd3wdproject",
            "solar.lowtechmagazine",
            "zimgit-post-disaster",
            "urban-prepper",
            "zimgit-food-preparation",
            "zimgit-water",
            "zimgit-knots",
            "appropedia",
            "foss_cooking",
            "wikipedia_en_simple",
            "wikipedia_en_all",
            "wikibooks",
            "wikiversity",
            "wikivoyage",
            "wikihow",
            "gutenberg",
            "wiktionary",
        ],
    },
]

# HTML elements to strip before text extraction
_STRIP_TAGS    = {"script", "style", "nav", "noscript", "form", "iframe"}
_STRIP_IDS     = {"mw-navigation", "mw-head", "mw-panel", "footer", "siteNotice", "p-tb"}
_STRIP_CLASSES = {"navbox", "sidebar", "noprint", "mw-jump-link", "catlinks",
                  "reflist", "references", "toc", "printfooter"}


# ── state ─────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"collections": {}}  # collections: { name: { collection_id, uploaded: {url: file_id} } }


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ── kiwix catalog ─────────────────────────────────────────────────────────────

def get_kiwix_books(session):
    """Return list of {title, path} dicts from the Kiwix OPDS catalog."""
    resp = session.get(f"{KIWIX_BASE}/catalog/v2/entries", params={"count": -1}, timeout=15)
    resp.raise_for_status()

    # OPDS 1.1 Atom XML
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "dc":   "http://purl.org/dc/terms/",
    }
    root = ET.fromstring(resp.content)
    books = []
    seen_paths = set()
    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        title = title_el.text if title_el is not None else "Unknown"
        # The text/html link is the ZIM's HTTP root, e.g. /content/zim-name-here
        for link in entry.findall("atom:link", ns):
            href = link.get("href", "")
            ltype = link.get("type", "")
            if "html" in ltype and href.startswith("/content/"):
                # path = everything after /content/  (the ZIM name)
                path = href[len("/content/"):].split("/")[0]
                if path and path not in seen_paths:
                    seen_paths.add(path)
                    books.append({"title": title, "path": path})
                break
    return books


# ── html extraction ───────────────────────────────────────────────────────────

def extract_text(html):
    """Strip Kiwix article HTML to clean plain text. Returns empty string if too thin."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(_STRIP_TAGS):
        tag.decompose()
    for id_ in _STRIP_IDS:
        el = soup.find(id=id_)
        if el:
            el.decompose()
    for cls in _STRIP_CLASSES:
        for el in soup.find_all(class_=cls):
            el.decompose()

    # Title
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    elif soup.title:
        title = (soup.title.string or "").split(" – ")[0].split(" - ")[0].strip()

    # Main content area (Wikipedia uses #mw-content-text, SE uses #mainbar, etc.)
    main = (
        soup.find(id="mw-content-text")
        or soup.find(id="mainbar")
        or soup.find(id="question-page")
        or soup.find("main")
        or soup.find("article")
        or soup.body
    )
    if not main:
        return ""

    text = main.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)

    if title:
        text = f"# {title}\n\n{text}"

    return text.strip()


# ── bfs crawler ───────────────────────────────────────────────────────────────

def crawl_zim(session, book_path, already_uploaded):
    """
    BFS-crawl all HTML articles reachable from a ZIM's home page.
    Yields (url, text) for each article not yet in already_uploaded.
    """
    prefix = f"{KIWIX_BASE}/content/{book_path}/"
    start  = prefix
    queue  = deque([start])
    seen   = set(already_uploaded) | {start}

    article_count = 0
    while queue:
        url = queue.popleft()
        try:
            resp = session.get(url, timeout=15, allow_redirects=True)
            if resp.status_code != 200:
                continue
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype:
                continue

            # Enqueue new internal links
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.find_all("a", href=True):
                full = urljoin(url, a["href"]).split("#")[0]
                if full.startswith(prefix) and full not in seen:
                    seen.add(full)
                    queue.append(full)

            # Skip already-uploaded
            if url in already_uploaded:
                continue

            text = extract_text(resp.text)
            if len(text) < MIN_TEXT_LEN:
                continue

            article_count += 1
            yield url, text
            time.sleep(RATE_DELAY)

        except Exception as exc:
            print(f"    [WARN] {url}: {exc}", file=sys.stderr)


# ── open webui upload ─────────────────────────────────────────────────────────

def ensure_collection(session, api_key, col_state, name, desc):
    """Return existing collection_id from state, or create it and store it."""
    if col_state.get("collection_id"):
        return col_state["collection_id"]

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    resp = session.post(
        f"{WEBUI_BASE}/api/v1/knowledge/create",
        headers=headers,
        json={"name": name, "description": desc},
        timeout=30,
    )
    resp.raise_for_status()
    cid = resp.json()["id"]
    col_state["collection_id"] = cid
    print(f"  Created: {name!r} ({cid})")
    return cid


def upload_article(session, api_key, collection_id, url, text):
    """Upload article text as a file and add it to the collection. Returns file_id."""
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    path = urlparse(url).path
    safe = re.sub(r"[^a-zA-Z0-9._-]", "_", path.strip("/"))[-80:]
    filename = (safe or "article") + ".txt"

    file_bytes = text[:MAX_TEXT_LEN].encode("utf-8")
    upload_resp = session.post(
        f"{WEBUI_BASE}/api/v1/files/",
        headers=headers,
        files={"file": (filename, file_bytes, "text/plain")},
        timeout=30,
    )
    upload_resp.raise_for_status()
    file_id = upload_resp.json()["id"]

    # Wait for the file to finish embedding before adding it to the collection.
    # Status starts as "pending"; file/add returns 400 if still pending.
    for _ in range(30):
        time.sleep(1)
        status_resp = session.get(f"{WEBUI_BASE}/api/v1/files/{file_id}", headers=headers, timeout=30)
        if status_resp.ok and status_resp.json().get("data", {}).get("status") != "pending":
            break

    session.post(
        f"{WEBUI_BASE}/api/v1/knowledge/{collection_id}/file/add",
        headers=headers,
        json={"file_id": file_id},
        timeout=30,
    ).raise_for_status()

    # Delete the per-file record and its ChromaDB collection to prevent
    # accumulation of orphaned collections that degrade ChromaDB performance.
    session.delete(f"{WEBUI_BASE}/api/v1/files/{file_id}", headers=headers, timeout=15)

    return file_id


def _worker_upload(api_key, collection_id, url, text):
    """Thread-safe upload — each worker creates its own HTTP session."""
    sess = requests.Session()
    return upload_article(sess, api_key, collection_id, url, text)


def purge_file_collections(file_ids):
    """Delete per-file ChromaDB collections via docker exec (OW's own delete leaves empty shells)."""
    if not file_ids:
        return
    names = ",".join(f'"file-{fid}"' for fid in file_ids)
    script = (
        "import chromadb; c=chromadb.PersistentClient(path='/app/backend/data/vector_db');\n"
        f"[c.delete_collection(n) for n in [{names}] if any(col.name==n for col in c.list_collections())]"
    )
    subprocess.run(["docker", "exec", "open-webui", "python3", "-c", script],
                   capture_output=True, timeout=30)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    global KIWIX_BASE, WEBUI_BASE  # may be overridden by --kiwix-url / --webui-url

    parser = argparse.ArgumentParser(description="Upload Kiwix articles to Open WebUI RAG")
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--api-key",  help="Open WebUI API key")
    auth.add_argument("--password", help="Open WebUI password (used with --email to get a token)")
    parser.add_argument("--email",      default="", help="Open WebUI email (required with --password)")
    parser.add_argument("--kiwix-url",  default=KIWIX_BASE)
    parser.add_argument("--webui-url",  default=WEBUI_BASE)
    parser.add_argument("--skip-zims",  nargs="*", default=[], metavar="PATH",
                        help="ZIM path prefixes to skip")
    parser.add_argument("--only-zims",  nargs="*", default=[], metavar="PATH",
                        help="Only crawl ZIMs whose path contains one of these strings")
    parser.add_argument("--max-per-zim", type=int, default=0,
                        help="Max articles per ZIM (0 = unlimited)")
    parser.add_argument("--zim-cap", action="append", default=[], metavar="PATTERN=N",
                        help="Per-ZIM cap override: --zim-cap wikihow=100000 (repeatable, overrides --max-per-zim)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel upload workers (default: 4)")
    args = parser.parse_args()

    zim_caps = {}
    for cap in args.zim_cap:
        if "=" not in cap:
            sys.exit(f"--zim-cap must be PATTERN=N, got: {cap}")
        pat, n = cap.split("=", 1)
        zim_caps[pat] = int(n)

    KIWIX_BASE = args.kiwix_url.rstrip("/")
    WEBUI_BASE = args.webui_url.rstrip("/")

    if args.password:
        if not args.email:
            sys.exit("--email is required when using --password")
        for attempt in range(5):
            try:
                resp = requests.post(
                    f"{WEBUI_BASE}/api/v1/auths/signin",
                    json={"email": args.email, "password": args.password},
                    timeout=30,
                )
                break
            except requests.exceptions.Timeout:
                if attempt < 4:
                    print(f"Login timeout, retrying ({attempt+1}/5)...", flush=True)
                    time.sleep(15)
                else:
                    sys.exit("Login failed after 5 attempts — is Open WebUI running?")
        if resp.status_code != 200:
            sys.exit(f"Login failed ({resp.status_code}): {resp.text[:200]}")
        api_key = resp.json().get("token")
        if not api_key:
            sys.exit(f"No token in login response: {resp.text[:200]}")
        print("Logged in OK, token acquired.")
    else:
        api_key = args.api_key

    state   = load_state()
    session = requests.Session()
    session.headers.update({"User-Agent": "kiwix-rag-uploader/1.0"})

    # Discover ZIMs
    print("Fetching Kiwix catalog...")
    try:
        books = get_kiwix_books(session)
    except Exception as e:
        sys.exit(f"Cannot reach Kiwix at {KIWIX_BASE}: {e}")

    if not books:
        sys.exit("No ZIMs found in catalog. Is Kiwix running and are ZIMs loaded?")

    print(f"Found {len(books)} ZIM(s):")
    for b in books:
        print(f"  {b['path']}")

    # Apply filters
    if args.only_zims:
        books = [b for b in books if any(f in b["path"] for f in args.only_zims)]
    if args.skip_zims:
        books = [b for b in books if not any(f in b["path"] for f in args.skip_zims)]

    if not books:
        sys.exit("No ZIMs left after filtering.")

    # Warn about known very-large ZIMs
    _HUGE = {"wikipedia_en_all", "stackoverflow_en_all", "gutenberg_en_all",
             "wikibooks_en_all", "wikiversity_en_all", "wikivoyage_en_all",
             "wikihow_en_maxi"}
    huge_queued = [b["path"] for b in books if any(h in b["path"] for h in _HUGE)]
    if huge_queued and not args.max_per_zim:
        print("\n  *** WARNING: massive ZIMs detected with no --max-per-zim cap ***")
        for p in huge_queued:
            print(f"    {p}")
        print("  Full crawl of these could take weeks. Ctrl-C and re-run with --max-per-zim N")
        print("  (state is saved every 25 uploads so you can resume anytime)")
        print()

    print(f"\nWill crawl {len(books)} ZIM(s):")
    for b in books:
        print(f"  {b['path']}")

    # state["collections"] = { collection_name: { "collection_id": …, "uploaded": {url: file_id} } }
    if "collections" not in state:
        state["collections"] = {}

    # Assign each ZIM to its collection
    unmatched = []
    assigned  = {col["name"]: [] for col in COLLECTIONS}
    for book in books:
        placed = False
        for col in COLLECTIONS:
            if any(m in book["path"] for m in col["match"]):
                assigned[col["name"]].append(book)
                placed = True
                break
        if not placed:
            unmatched.append(book)

    if unmatched:
        print("\n[WARN] These ZIMs matched no collection and will be skipped:")
        for b in unmatched:
            print(f"  {b['path']}")

    uploaded_total = 0
    failed_total   = 0

    for col_def in COLLECTIONS:
        col_name  = col_def["name"]
        col_books = assigned[col_name]
        if not col_books:
            continue

        print(f"\n{'='*60}")
        print(f"COLLECTION: {col_name}")
        print(f"  ZIMs: {len(col_books)}")

        # Per-collection state bucket
        col_state = state["collections"].setdefault(col_name, {"collection_id": None, "uploaded": {}})
        collection_id = ensure_collection(session, api_key, col_state, col_name, col_def["desc"])
        save_state(state)

        for book in col_books:
            print(f"\n  [ZIM] {book['title']}  ({book['path']})")
            already   = col_state["uploaded"]
            zim_count = 0

            # Resolve effective cap: per-ZIM override > global --max-per-zim > 0 (unlimited)
            effective_cap = args.max_per_zim
            for pat, cap in zim_caps.items():
                if pat in book["path"]:
                    effective_cap = cap
                    break

            cleanup_queue = []   # file_ids pending ChromaDB collection purge

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                pending  = {}   # future -> (url, label)
                gen      = crawl_zim(session, book["path"], already)
                gen_done = False

                # Pre-fill pipeline to keep all workers busy
                while len(pending) < args.workers * 2 and not gen_done:
                    try:
                        u, t = next(gen)
                        lbl = (u.split("/")[-1] or u.split("/")[-2])[:65]
                        pending[pool.submit(_worker_upload, api_key, collection_id, u, t)] = (u, lbl)
                    except StopIteration:
                        gen_done = True

                while pending:
                    finished, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for f in finished:
                        url, label = pending.pop(f)
                        try:
                            file_id = f.result()
                            col_state["uploaded"][url] = file_id
                            cleanup_queue.append(file_id)
                            uploaded_total += 1
                            zim_count += 1
                            print(f"    [OK #{uploaded_total:>5}] {label}")
                            if uploaded_total % BATCH_SIZE == 0:
                                save_state(state)
                                purge_file_collections(cleanup_queue)
                                cleanup_queue.clear()
                        except Exception as exc:
                            print(f"    [FAIL] {url}: {exc}", file=sys.stderr)
                            failed_total += 1

                        # Refill one slot to maintain pipeline depth
                        if not gen_done:
                            if effective_cap and (zim_count + len(pending)) >= effective_cap:
                                gen_done = True
                            else:
                                try:
                                    u, t = next(gen)
                                    lbl = (u.split("/")[-1] or u.split("/")[-2])[:65]
                                    pending[pool.submit(_worker_upload, api_key, collection_id, u, t)] = (u, lbl)
                                except StopIteration:
                                    gen_done = True

                    if effective_cap and zim_count >= effective_cap:
                        print(f"    [LIMIT] cap {effective_cap} reached for {book['path']}")
                        for f in list(pending):
                            f.cancel()
                        pending.clear()
                        break

            purge_file_collections(cleanup_queue)
            cleanup_queue.clear()
            save_state(state)
            print(f"    ZIM done — {zim_count} new articles")

    save_state(state)
    print(f"\n{'='*60}")
    print(f"All done: {uploaded_total} uploaded, {failed_total} failed")
    print(f"State file : {STATE_FILE}")
    print("Open WebUI -> Workspace -> Knowledge to see the 3 collections")


if __name__ == "__main__":
    main()
