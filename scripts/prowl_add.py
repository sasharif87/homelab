#!/usr/bin/env python3
"""
prowl_add.py — Manual Prowlarr search → qBittorrent → LazyLibrarian

Replaces LL's auto-search when it mismatches. You pick the exact torrent
from Prowlarr results; the script handles qBit + LL post-processing.
When an ISBN is found or provided it is passed to LL for a near-certain match.

Modes:
    python3 prowl_add.py "Author Title"                        # interactive search
    python3 prowl_add.py "Dune" --type audiobook               # filter type
    python3 prowl_add.py "Dune" --pick 1 --dry-run             # preview match, no download
    python3 prowl_add.py "Dune" --pick 1 --yes --no-ll         # fully non-interactive
    python3 prowl_add.py "battletech" --batch --type both       # show all, verify each, add all
    python3 prowl_add.py "battletech" --batch --dry-run         # preview all matches, no download
    python3 prowl_add.py "battletech" --batch --offset 40       # paginate past first 40
    python3 prowl_add.py --process                             # LL forceProcess on finished downloads
    python3 prowl_add.py --log                                 # show download log

Runs on the VM (uses localhost). Deploy to /mnt/apps/compose/scripts/.
MAM rate limit: one Prowlarr search = one MAM query. Batch adds to qBit
are free — only the Prowlarr search call counts against MAM's quota.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests

# ── Config ────────────────────────────────────────────────────────────────────
PROWLARR_URL = "http://localhost:9696"
PROWLARR_KEY = "0867f30fe515430aa7b17e8450cccd27"

QBIT_URL  = "http://localhost:8082"
QBIT_USER = "admin"
QBIT_PASS = "ctzlyLkXPSOX3r"

LL_BASE = "http://localhost:5299/api?apikey=2833a671925b43089e2002226e17fd4e"

STAGING = {
    "audiobook": "/downloads/prowl/audiobooks",
    "ebook":     "/downloads/prowl/ebooks",
}
LIBRARY = {
    "audiobook": "/audiobooks",
    "ebook":     "/ebooks",
}
CATEGORY = {
    "audiobook": "prowl-audiobooks",
    "ebook":     "prowl-ebooks",
}
NEWZNAB = {
    "audiobook": [3030, 3000],
    "ebook":     [7020, 7000],
}

LOG_PATH = "/mnt/storage/downloads/prowl/prowl_log.json"

HR  = "─" * 78
HR2 = "═" * 78

ISBN_RE = re.compile(r"\b(97[89]\d{10}|\d{9}[\dXx])\b")


# ── Prowlarr ──────────────────────────────────────────────────────────────────

def prowlarr_search(query: str, cats: list, limit: int = 40, offset: int = 0) -> list:
    params = [
        ("query",      query),
        ("indexerIds", -1),
        ("type",       "search"),
        ("limit",      limit),
        ("offset",     offset),
        ("apikey",     PROWLARR_KEY),
    ]
    for c in cats:
        params.append(("categories", c))

    resp = requests.get(f"{PROWLARR_URL}/api/v1/search", params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json()
    return sorted(results, key=lambda r: (r.get("seeders") or 0, r.get("size") or 0), reverse=True)


def detect_type(result: dict) -> str:
    cat_ids = [c.get("id", 0) for c in (result.get("categories") or [])]
    if any(c in [3000, 3030] for c in cat_ids):
        return "audiobook"
    return "ebook"


def extract_isbn(text: str) -> str | None:
    m = ISBN_RE.search(text or "")
    return m.group(0) if m else None


def fmt_size(b) -> str:
    if not b:
        return "?"
    b = int(b)
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f}G"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.0f}M"
    return f"{b / 1024:.0f}K"


def display_results(results: list) -> None:
    print(f"\n{'#':>3}  {'Type':>9}  {'Size':>7}  {'Seed':>4}  {'Indexer':<18}  {'ISBN':<14}  Title")
    print(HR)
    for i, r in enumerate(results):
        rtype   = detect_type(r)
        tag     = "[AB]" if rtype == "audiobook" else "[EB]"
        size    = fmt_size(r.get("size"))
        seeds   = r.get("seeders") or 0
        indexer = (r.get("indexer") or "")[:16]
        title   = (r.get("title")   or "")
        isbn    = extract_isbn(title) or extract_isbn(r.get("description") or "") or ""
        print(f"{i+1:>3}  {tag} {rtype:<7}  {size:>7}  {seeds:>4}  {indexer:<18}  {isbn:<14}  {title[:40]}")


# ── qBittorrent ───────────────────────────────────────────────────────────────

def qlogin() -> requests.Session:
    sess = requests.Session()
    r = sess.post(
        f"{QBIT_URL}/api/v2/auth/login",
        data={"username": QBIT_USER, "password": QBIT_PASS},
        headers={"Referer": QBIT_URL},
    )
    assert r.status_code in (200, 204), f"qBit login failed: {r.status_code} {r.text!r}"
    return sess


def qadd(sess: requests.Session, url: str, save_path: str, category: str) -> None:
    r = sess.post(
        f"{QBIT_URL}/api/v2/torrents/add",
        data={"urls": url, "savepath": save_path, "category": category},
        headers={"Referer": QBIT_URL},
    )
    ok_v4 = r.status_code == 200 and r.text.lower().startswith("ok")
    ok_v5 = r.status_code == 202 and r.json().get("failure_count", 1) == 0
    assert ok_v4 or ok_v5, f"qBit add failed: {r.status_code} {r.text!r}"


def qall(sess: requests.Session) -> list:
    r = sess.get(
        f"{QBIT_URL}/api/v2/torrents/info",
        params={"limit": 5000},
        headers={"Referer": QBIT_URL},
    )
    r.raise_for_status()
    return r.json()


def qfind_hash(sess: requests.Session, title_fragment: str, max_wait: int = 6) -> str | None:
    frag = title_fragment.lower()[:25]
    for _ in range(max_wait):
        time.sleep(1)
        for t in qall(sess):
            if frag in (t.get("name") or "").lower():
                return t["hash"]
    return None


def qget_prowl(sess: requests.Session) -> list:
    return [t for t in qall(sess) if t.get("category", "").startswith("prowl-")]


# ── LazyLibrarian ─────────────────────────────────────────────────────────────

def ll_get(cmd: str, **params) -> object:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{LL_BASE}&cmd={cmd}" + (f"&{qs}" if qs else "")
    try:
        r = requests.get(url, timeout=20)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def ll_find(title: str, author: str, book_type: str, isbn: str = "") -> tuple:
    """
    Look up a book in LL's Goodreads index. Returns (book_id, matched_title, method).
    Does NOT add to LL — call ll_add(bid) separately after confirming the match.
    Uses ISBN when available (near-certain); falls back to title/author search.
    """
    ll_type = "audiobook" if book_type == "audiobook" else "book"

    if isbn:
        results = ll_get("findBook", name=isbn, type=ll_type)
        if isinstance(results, list) and results:
            first = results[0]
            bid   = (first.get("bookid") or first.get("BookID") or
                     first.get("id")     or first.get("bookID"))
            bname = first.get("bookname") or first.get("BookName") or title
            if bid:
                return bid, bname, f"ISBN {isbn}"

    query   = quote_plus(f"{author} {title}".strip() if author else title)
    results = ll_get("findBook", name=query, type=ll_type)

    if not isinstance(results, list) or not results:
        return None, None, "no results"

    first = results[0]
    bid   = (first.get("bookid") or first.get("BookID") or
             first.get("id")     or first.get("bookID"))
    bname = first.get("bookname") or first.get("BookName") or title

    if not bid:
        return None, None, "no book ID"

    return bid, bname, "title search"


def ll_add(book_id: str) -> None:
    ll_get("addBook", id=book_id)


def ll_find_and_add(title: str, author: str, book_type: str, isbn: str = "") -> tuple:
    """Convenience: find + add. Returns (book_id, status_msg)."""
    bid, bname, method = ll_find(title, author, book_type, isbn)
    if not bid:
        return None, f"findBook returned nothing ({method})"
    ll_add(bid)
    return bid, f"[{method}] '{(bname or title)[:50]}' (id={bid})"


def ll_force_process(path: str) -> object:
    url = f"{LL_BASE}&cmd=forceProcess&dir={path}"
    try:
        r = requests.get(url, timeout=20)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# ── Log ───────────────────────────────────────────────────────────────────────

def load_log() -> list:
    try:
        with open(LOG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_log(entries: list) -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w") as f:
        json.dump(entries, f, indent=2)


def _add_one(sess: requests.Session, chosen: dict, item_type: str,
             ll_bid: str | None, log: list, quiet: bool = False) -> dict:
    """Add one result to qBit and append a log entry. Returns the entry."""
    save_path = STAGING[item_type]
    target    = LIBRARY[item_type]
    category  = CATEGORY[item_type]
    dl_url    = chosen.get("downloadUrl") or chosen.get("magnetUrl") or ""

    qadd(sess, dl_url, save_path, category)

    title_frag    = (chosen.get("title") or "")[:25]
    torrent_hash  = qfind_hash(sess, title_frag)
    if not quiet:
        print(f"    hash: {torrent_hash or 'not found'}")

    entry = {
        "timestamp":           datetime.now(timezone.utc).isoformat(),
        "title":               chosen.get("title"),
        "indexer":             chosen.get("indexer"),
        "type":                item_type,
        "size_bytes":          chosen.get("size"),
        "isbn":                extract_isbn(chosen.get("title", "") + " " + chosen.get("description", "") or ""),
        "torrent_save_path":   save_path,
        "target_library_path": target,
        "hash":                torrent_hash,
        "ll_book_id":          ll_bid,
        "guid":                chosen.get("guid"),
        "status":              "downloading",
    }
    log.append(entry)
    return entry


# ── Mode: search (interactive + --pick + --batch) ────────────────────────────

def mode_search(args):
    if args.type == "audiobook":
        cats = NEWZNAB["audiobook"]
    elif args.type == "ebook":
        cats = NEWZNAB["ebook"]
    else:
        cats = NEWZNAB["audiobook"] + NEWZNAB["ebook"]

    print(f"\n{HR2}")
    suffix = f"  offset={args.offset}" if args.offset else ""
    mode   = "[BATCH]" if args.batch else f"[pick={args.pick}]" if args.pick else "[interactive]"
    print(f"  Prowlarr search: {args.query!r}  type={args.type}  limit={args.limit}{suffix}  {mode}")
    print(HR2)

    try:
        results = prowlarr_search(args.query, cats, limit=args.limit, offset=args.offset)
    except requests.RequestException as e:
        print(f"  ERROR: Prowlarr unreachable — {e}")
        sys.exit(1)

    if not results:
        print("  No results found.")
        sys.exit(0)

    display_results(results)
    print(f"\n  {len(results)} result(s) returned.")

    # ── BATCH mode: add everything ────────────────────────────────────────────
    if args.batch:
        # Filter out results without a download URL
        valid = [r for r in results if r.get("downloadUrl") or r.get("magnetUrl")]
        skipped = len(results) - len(valid)
        if skipped:
            print(f"  ({skipped} skipped — no download URL)")
        if not valid:
            print("  Nothing to add.")
            sys.exit(0)

        if not args.yes:
            yn = input(f"\n  Add ALL {len(valid)} results to qBittorrent? [Y/n]: ").strip().lower()
            if yn == "n":
                sys.exit(0)

        sess = qlogin()
        log  = load_log()

        for i, r in enumerate(valid, 1):
            item_type = detect_type(r)
            title     = r.get("title", "?")
            print(f"\n  [{i}/{len(valid)}] {title[:65]}")
            print(f"    type={item_type}  size={fmt_size(r.get('size'))}  seeds={r.get('seeders',0)}")

            ll_bid = None
            if not args.no_ll:
                isbn   = extract_isbn(title + " " + (r.get("description") or ""))
                ll_bid, ll_msg = ll_find_and_add(title, "", item_type, isbn=isbn or "")
                status = "OK" if ll_bid else "WARN"
                print(f"    LL [{status}]: {ll_msg}")

            try:
                _add_one(sess, r, item_type, ll_bid, log)
            except AssertionError as e:
                print(f"    ERROR adding to qBit: {e}")

            # Small pause between adds to be polite to qBit
            if i < len(valid):
                time.sleep(0.5)

        save_log(log)
        print(f"\n  {HR}")
        print(f"  Done. {len(valid)} queued.  Log: {LOG_PATH}")
        print(f"  When downloads finish:  python3 prowl_add.py --process")
        print(HR2)
        return

    # ── PICK mode: auto-select result N ──────────────────────────────────────
    if args.pick is not None:
        try:
            chosen = results[args.pick - 1]
        except IndexError:
            print(f"  ERROR: --pick {args.pick} out of range (only {len(results)} results).")
            sys.exit(1)
        print(f"\n  Auto-picked #{args.pick}: {chosen.get('title', '?')}")
    else:
        print("  Enter # to pick, or q to quit.")
        pick = input("\n  Pick #: ").strip()
        if pick.lower() in ("q", "quit", ""):
            sys.exit(0)
        try:
            chosen = results[int(pick) - 1]
        except (ValueError, IndexError):
            print("  Invalid selection.")
            sys.exit(1)

    item_type = detect_type(chosen)
    dl_url    = chosen.get("downloadUrl") or chosen.get("magnetUrl") or ""

    if not dl_url:
        print("  ERROR: No download URL in this result.")
        sys.exit(1)

    isbn = extract_isbn((chosen.get("title") or "") + " " + (chosen.get("description") or ""))

    print(f"\n  Title   : {chosen.get('title', '?')}")
    print(f"  Type    : {item_type}")
    print(f"  Size    : {fmt_size(chosen.get('size'))}")
    print(f"  Indexer : {chosen.get('indexer', '?')}")
    print(f"  Seeds   : {chosen.get('seeders', '?')}")
    print(f"  ISBN    : {isbn or 'not found in title'}")
    print(f"  Staging : {STAGING[item_type]}")
    print(f"  Library : {LIBRARY[item_type]}")

    # LL registration
    ll_bid = None
    if not args.no_ll:
        print()
        prompt = f"  Add to LL database now? (ISBN={'found' if isbn else 'not found'}) [Y/n]: "
        yn = "" if args.yes else input(prompt).strip().lower()
        if yn != "n":
            author = "" if args.yes else input("  Author (blank if already in title): ").strip()
            ll_bid, ll_msg = ll_find_and_add(
                chosen.get("title", args.query), author, item_type, isbn=isbn or ""
            )
            status = "OK" if ll_bid else "WARN"
            print(f"  LL [{status}]: {ll_msg}")

    # Confirm qBit add
    print()
    yn = "" if args.yes else input("  Send to qBittorrent? [Y/n]: ").strip().lower()
    if yn == "n":
        sys.exit(0)

    sess = qlogin()
    log  = load_log()
    print("  Adding to qBittorrent...", end=" ", flush=True)
    _add_one(sess, chosen, item_type, ll_bid, log)
    save_log(log)

    print(f"  Logged to {LOG_PATH}")
    print(f"  When download completes:  python3 prowl_add.py --process")
    print(HR2)


# ── Mode: process ─────────────────────────────────────────────────────────────

SEEDING_STATES = {"uploading", "stalledUP", "forcedUP", "queuedUP", "checkingUP", "stoppedUP"}


def mode_process(args):
    print(f"\n{HR2}")
    print("  Processing completed prowl downloads via LazyLibrarian forceProcess")
    print(HR2)

    sess     = qlogin()
    torrents = qget_prowl(sess)
    complete = [t for t in torrents
                if t.get("state") in SEEDING_STATES or t.get("progress", 0) >= 0.999]
    pending  = [t for t in torrents if t not in complete]

    print(f"\n  prowl-* torrents — complete: {len(complete)}  downloading: {len(pending)}")

    if pending:
        print("\n  Still downloading:")
        for t in pending:
            pct = t.get("progress", 0) * 100
            print(f"    {pct:5.1f}%  {t['name'][:60]}")

    if not complete:
        print("\n  Nothing ready to process yet.")
        print(HR2)
        return

    print("\n  Ready to process:")
    for t in complete:
        cp = t.get("content_path") or t.get("save_path", "")
        print(f"    {t['name'][:60]}")
        print(f"      path: {cp}")

    print()
    yn = "" if args.yes else input("  Trigger LL forceProcess on all of the above? [Y/n]: ").strip().lower()
    if yn == "n":
        print("  Aborted.")
        print(HR2)
        return

    log     = load_log()
    updated = 0

    for t in complete:
        cp = t.get("content_path") or t.get("save_path", "")
        print(f"\n  forceProcess: {cp}")
        resp = ll_force_process(cp)
        print(f"    LL: {resp}")

        for entry in log:
            if entry.get("hash") == t.get("hash") and entry.get("status") == "downloading":
                entry["status"]       = "processed"
                entry["processed_at"] = datetime.now(timezone.utc).isoformat()
                updated += 1

    save_log(log)
    print(f"\n  Updated {updated} log entr{'y' if updated == 1 else 'ies'} to 'processed'.")
    print(f"  Verify in LL: http://localhost:5299")
    print(HR2)


# ── Mode: log ────────────────────────────────────────────────────────────────

def mode_log():
    entries = load_log()
    if not entries:
        print("  No log entries yet.")
        return

    print(f"\n{'#':>3}  {'Date':>10}  {'Status':<12}  {'Type':>9}  {'Size':>7}  {'ISBN':<14}  Title")
    print(HR)
    for i, e in enumerate(reversed(entries), 1):
        status = e.get("status", "?")
        etype  = e.get("type",   "?")
        size   = fmt_size(e.get("size_bytes"))
        title  = (e.get("title") or "?")[:38]
        ts     = (e.get("timestamp") or "?")[:10]
        isbn   = (e.get("isbn") or "")[:13]
        print(f"{i:>3}  {ts}  {status:<12}  {etype:>9}  {size:>7}  {isbn:<14}  {title}")

    dl   = sum(1 for e in entries if e.get("status") == "downloading")
    done = sum(1 for e in entries if e.get("status") == "processed")
    print(f"\n  Total: {len(entries)}  |  downloading: {dl}  |  processed: {done}")


# ── Entry ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("query",    nargs="?",  help="Search terms (title, author, series, ISBN)")
    ap.add_argument("--type",   choices=["audiobook", "ebook", "both"], default="both",
                    help="Filter by media type (default: both)")
    ap.add_argument("--limit",  type=int, default=40, metavar="N",
                    help="Max results from Prowlarr (default: 40)")
    ap.add_argument("--offset", type=int, default=0,  metavar="N",
                    help="Pagination offset (default: 0)")
    ap.add_argument("--pick",   type=int, metavar="N",
                    help="Auto-select result #N without prompting")
    ap.add_argument("--batch",  action="store_true",
                    help="Add all results to qBit (confirm once, or use --yes to skip)")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Auto-confirm all prompts (combine with --pick or --batch)")
    ap.add_argument("--no-ll",  action="store_true",
                    help="Skip LL database registration step")
    ap.add_argument("--process", action="store_true",
                    help="Trigger LL forceProcess on completed prowl downloads")
    ap.add_argument("--log",    action="store_true",
                    help="Show download log")

    args = ap.parse_args()

    if args.log:
        mode_log()
    elif args.process:
        mode_process(args)
    elif args.query:
        mode_search(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
