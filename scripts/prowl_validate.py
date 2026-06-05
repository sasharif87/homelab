#!/usr/bin/env python3
"""
prowl_validate.py — Cross-validate MAM search results against a book wishlist CSV
                    before touching qBittorrent or LazyLibrarian.

Workflow:
  1. Search Prowlarr for a query (one API call = one MAM quota used)
  2. Load your CSV wishlist (Title, Author, ISBN13, Series, Series Order, ...)
  3. Check LL's existing library to skip already-owned books
  4. Score each MAM result against your wishlist:
       ISBN match → 0.99  (near-certain)
       Title + author match → 0.72–0.85
       Title only → 0.55
  5. Output four buckets:
       CONFIDENT  (≥0.85) — safe to auto-queue
       REVIEW     (0.50–0.84) — check the match before queuing
       MAM_ONLY   (<0.50) — MAM has it, not in your wishlist
       CSV_MISSING — in wishlist, not found in this MAM search
  6. Save full results to JSON
  7. Optionally queue CONFIDENT bucket directly to qBittorrent + LL

Usage:
    python prowl_validate.py "battletech" --csv battletech_books.csv
    python prowl_validate.py "battletech" --csv battletech_books.csv --type audiobook
    python prowl_validate.py "battletech" --csv battletech_books.csv --limit 100
    python prowl_validate.py "battletech" --csv battletech_books.csv --queue
    python prowl_validate.py "battletech" --csv battletech_books.csv --queue --yes

Runs on the VM (uses localhost). Deploy to /mnt/apps/compose/scripts/.
SCP your CSV there first:
    scp battletech_books.csv root@<server-ip>:/tmp/battletech_books.csv
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus

import requests

# ── VM endpoints (localhost — runs inside Gluetun network namespace) ──────────
PROWLARR_URL = "http://localhost:9696"
PROWLARR_KEY = "0867f30fe515430aa7b17e8450cccd27"

# MAM indexer ID — search only MAM to avoid wasting quota on other indexers
MAM_INDEXER_ID = 6

QBIT_URL  = "http://localhost:8082"
QBIT_USER = "admin"
QBIT_PASS = "ctzlyLkXPSOX3r"

LL_URL = "http://localhost:5299"
LL_KEY = "2833a671925b43089e2002226e17fd4e"

# qBit staging paths (VM-side paths for the container)
STAGING = {
    "audiobook": "/mnt/storage/downloads/prowl/audiobooks",
    "ebook":     "/mnt/storage/downloads/prowl/ebooks",
}
LIBRARY = {
    "audiobook": "/mnt/storage/media/audiobooks",
    "ebook":     "/mnt/storage/media/ebooks",
}
CATEGORY = {
    "audiobook": "prowl-audiobooks",
    "ebook":     "prowl-ebooks",
}
NEWZNAB = {
    "audiobook": [3030, 3000],
    "ebook":     [7020, 7000],
}

CONFIDENT_THRESHOLD = 0.85
REVIEW_THRESHOLD    = 0.50

HR  = "-" * 78
HR2 = "=" * 78

ISBN_RE  = re.compile(r"\b(97[89]\d{10}|\d{9}[\dXx])\b")
MAM_ID_RE = re.compile(r"/t/(\d+)")

STOP_WORDS = frozenset([
    "the","a","an","of","and","in","by","to","is","at","on","with",
    "for","its","his","her","from","this","that","are","was","be","or",
    "battletech","bt",  # strip series prefix so titles match
])


# ── Text utilities ─────────────────────────────────────────────────────────────

def norm(s: str) -> str:
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", str(s).lower())
    s = re.sub(r"\bbook\s*\d+\b|\bvol\w*\s*\d+\b|\bpart\s*\d+\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def keywords(s: str) -> set:
    return {w for w in norm(s).split() if w not in STOP_WORDS and len(w) > 2}


def extract_isbn(text: str) -> str:
    m = ISBN_RE.search(text or "")
    return m.group(0).replace("-", "") if m else ""


def extract_mam_id(guid: str) -> str:
    m = MAM_ID_RE.search(guid or "")
    return m.group(1) if m else ""


def fmt_size(b) -> str:
    if not b:
        return "?"
    b = int(b)
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f}G"
    if b >= 1_048_576:
        return f"{b / 1_048_576:.0f}M"
    return f"{b / 1024:.0f}K"


# ── CSV loading ────────────────────────────────────────────────────────────────

def load_csv(path: str) -> list:
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            isbn = row.get("ISBN13", "").strip().replace("-", "").replace(" ", "")
            rows.append({
                "title":        row.get("Title", "").strip(),
                "author":       row.get("Author", "").strip(),
                "isbn13":       isbn,
                "series":       row.get("Series", "").strip(),
                "series_order": row.get("Series Order", "").strip(),
                "notes":        row.get("Notes", "").strip(),
            })
    return rows


# ── Prowlarr ──────────────────────────────────────────────────────────────────

def prowlarr_search(query: str, cats: list, limit: int) -> list:
    params = [
        ("query",      query),
        ("indexerIds", MAM_INDEXER_ID),
        ("type",       "search"),
        ("limit",      limit),
        ("apikey",     PROWLARR_KEY),
    ]
    for c in cats:
        params.append(("categories", c))

    resp = requests.get(f"{PROWLARR_URL}/api/v1/search", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def detect_type(result: dict) -> str:
    cat_ids = [c.get("id", 0) for c in (result.get("categories") or [])]
    if any(c in [3000, 3030] for c in cat_ids):
        return "audiobook"
    return "ebook"


# ── LazyLibrarian ─────────────────────────────────────────────────────────────

def ll_get(cmd: str, **params) -> object:
    qs  = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{LL_URL}/api?apikey={LL_KEY}&cmd={cmd}" + (f"&{qs}" if qs else "")
    try:
        r = requests.get(url, timeout=20)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def ll_existing_isbns() -> set:
    """Return set of ISBN13s already in LL's library."""
    data = ll_get("getAllBooks")
    if not isinstance(data, list):
        return set()
    out = set()
    for b in data:
        isbn = (b.get("BookIsbn") or b.get("bookIsbn") or
                b.get("isbn")     or b.get("ISBN") or "")
        if isbn:
            out.add(isbn.replace("-", "").strip())
    return out


def ll_find(title: str, author: str, book_type: str, isbn: str = "") -> tuple:
    """Look up in LL's Goodreads index. Returns (book_id, matched_title, method)."""
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


def qadd(sess: requests.Session, dl_url: str, item_type: str) -> None:
    r = sess.post(
        f"{QBIT_URL}/api/v2/torrents/add",
        data={
            "urls":     dl_url,
            "savepath": STAGING[item_type],
            "category": CATEGORY[item_type],
        },
        headers={"Referer": QBIT_URL},
    )
    # qBit v4 returns 200 "Ok.", v5 returns 202 JSON with pending/success counts
    ok_v4 = r.status_code == 200 and r.text.lower().startswith("ok")
    ok_v5 = r.status_code == 202 and r.json().get("failure_count", 1) == 0
    assert ok_v4 or ok_v5, f"qBit add failed: {r.status_code} {r.text!r}"


# ── Matching ──────────────────────────────────────────────────────────────────

def score_match(csv_row: dict, result: dict) -> tuple:
    """Returns (score 0.0-0.99, reason string)."""
    csv_isbn  = csv_row["isbn13"].replace("-", "")
    res_text  = (result.get("title") or "") + " " + (result.get("description") or "")
    res_isbn  = extract_isbn(res_text)

    # ISBN exact match — near-certain
    if csv_isbn and res_isbn and csv_isbn == res_isbn:
        return 0.99, f"ISBN {csv_isbn}"

    csv_title_kw  = keywords(csv_row["title"])
    csv_author_kw = keywords(csv_row["author"])
    res_title_kw  = keywords(result.get("title") or "")
    res_all_kw    = keywords(res_text)

    if not csv_title_kw:
        return 0.0, "empty title"

    title_overlap = csv_title_kw & res_title_kw
    title_ratio   = len(title_overlap) / len(csv_title_kw)

    author_overlap = csv_author_kw & res_all_kw
    author_ratio   = len(author_overlap) / max(len(csv_author_kw), 1)

    reason = f"title {title_ratio:.0%}"
    if author_ratio > 0:
        reason += f" + author {author_ratio:.0%}"

    if title_ratio >= 0.9 and author_ratio >= 0.4:
        return 0.88, reason
    if title_ratio >= 0.8 and author_ratio >= 0.3:
        return 0.80, reason
    if title_ratio >= 0.7 and author_ratio >= 0.2:
        return 0.72, reason
    if title_ratio >= 0.6 and author_ratio >= 0.2:
        return 0.60, reason
    if title_ratio >= 0.6:
        return 0.55, reason

    return 0.0, "no match"


def best_match(result: dict, csv_rows: list) -> tuple:
    """Find best-scoring CSV row for a given Prowlarr result."""
    best_score  = 0.0
    best_row    = None
    best_reason = ""
    for row in csv_rows:
        score, reason = score_match(row, result)
        if score > best_score:
            best_score  = score
            best_row    = row
            best_reason = reason
    return best_score, best_row, best_reason


# ── Log / output ──────────────────────────────────────────────────────────────

def save_plan(path: str, plan: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)


def print_bucket(label: str, items: list, show_csv: bool = True) -> None:
    if not items:
        return
    print(f"\n{HR}")
    print(f"  {label}  ({len(items)})")
    print(HR)
    for item in items:
        if "score" in item:
            print(f"\n  [{item['score']:.2f}] {item['method']}")
        if show_csv and "csv" in item:
            c = item["csv"]
            series = f" [{c['series']} #{c['series_order']}]" if c.get("series") else ""
            print(f"    CSV : {c['title']} — {c['author']}{series}")
        r = item.get("result") or item
        if "title" in r:
            rtype = r.get("type", "?")
            print(f"    MAM : {r['title'][:65]}")
            print(f"          {rtype}  {fmt_size(r.get('size_bytes'))}  "
                  f"{r.get('seeders', 0)} seeds  [{r.get('indexer', '?')}]")


# ── Queue (push CONFIDENT to qBit + LL) ───────────────────────────────────────

def queue_confident(items: list, log_path: str, yes: bool) -> None:
    if not items:
        print("  No CONFIDENT items to queue.")
        return

    dl_items = [i for i in items if i["result"].get("download_url")]
    if not dl_items:
        print("  No CONFIDENT items have a download URL.")
        return

    if not yes:
        yn = input(f"\n  Queue {len(dl_items)} CONFIDENT results to qBittorrent + LL? [Y/n]: ").strip().lower()
        if yn == "n":
            print("  Aborted.")
            return

    print(f"\n  Connecting to qBit at {QBIT_URL}...")
    sess = qlogin()

    log = []
    for item in dl_items:
        r       = item["result"]
        csv_row = item.get("csv", {})
        title   = r.get("title", "?")
        itype   = r.get("type", "ebook")
        isbn    = item.get("result", {}).get("isbn", "") or csv_row.get("isbn13", "")
        dl_url  = r["download_url"]

        print(f"\n  [{itype}] {title[:65]}")

        # Register in LL (ISBN-based → near-certain match for forceProcess later)
        ll_bid, ll_bname, ll_method = ll_find(
            csv_row.get("title", title),
            csv_row.get("author", ""),
            itype,
            isbn=isbn,
        )
        if ll_bid:
            ll_add(ll_bid)
            print(f"    LL  : [{ll_method}] '{(ll_bname or title)[:50]}' (id={ll_bid})")
        else:
            print(f"    LL  : WARN — could not find in LL database")

        # Add to qBit
        try:
            qadd(sess, dl_url, itype)
            print(f"    qBit: queued → {STAGING[itype]}")
        except AssertionError as e:
            print(f"    qBit: ERROR — {e}")
            continue

        log.append({
            "timestamp":           datetime.now(timezone.utc).isoformat(),
            "title":               title,
            "type":                itype,
            "isbn":                isbn,
            "size_bytes":          r.get("size_bytes"),
            "indexer":             r.get("indexer"),
            "torrent_save_path":   STAGING[itype],
            "target_library_path": LIBRARY[itype],
            "ll_book_id":          ll_bid,
            "score":               item.get("score"),
            "method":              item.get("method"),
            "status":              "downloading",
        })

        time.sleep(0.5)

    if log:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        existing = []
        if os.path.exists(log_path):
            with open(log_path) as f:
                existing = json.load(f)
        existing.extend(log)
        with open(log_path, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"\n  {len(log)} entries added to {log_path}")
        print(f"  When done: python prowl_add.py --process  (on VM)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("query",    help="Prowlarr search query (e.g. 'battletech')")
    ap.add_argument("--csv",    required=True, metavar="FILE",
                    help="Path to wishlist CSV (Title, Author, ISBN13, ...)")
    ap.add_argument("--type",   choices=["audiobook", "ebook", "both"], default="both")
    ap.add_argument("--limit",  type=int, default=100,
                    help="Max results per Prowlarr search (default: 100)")
    ap.add_argument("--offset", type=int, default=0,
                    help="Pagination offset (default: 0)")
    ap.add_argument("--out",    metavar="FILE",
                    help="JSON output path (default: <query>_validated.json)")
    ap.add_argument("--queue",  action="store_true",
                    help="After validation, push CONFIDENT bucket to qBit + LL")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="Auto-confirm queue without prompting")
    ap.add_argument("--skip-ll-check", action="store_true",
                    help="Skip LL getAllBooks check (faster, no dedup against library)")

    args = ap.parse_args()

    out_path = args.out or f"{re.sub(r'[^a-z0-9]+', '_', args.query.lower())}_validated.json"
    log_path = os.path.join(os.path.dirname(os.path.abspath(args.csv)),
                            "prowl_download_log.json")

    print(f"\n{HR2}")
    print(f"  prowl_validate  |  query={args.query!r}  type={args.type}")
    print(f"  CSV: {args.csv}")
    print(HR2)

    # ── Load CSV ──────────────────────────────────────────────────────────────
    print(f"\nLoading CSV...", end=" ", flush=True)
    csv_rows = load_csv(args.csv)
    print(f"{len(csv_rows)} books")

    # Build ISBN → row index for fast lookup
    csv_by_isbn = {r["isbn13"]: r for r in csv_rows if r["isbn13"]}

    # ── Check LL existing library ─────────────────────────────────────────────
    owned_isbns = set()
    if not args.skip_ll_check:
        print("Checking LL existing library...", end=" ", flush=True)
        owned_isbns = ll_existing_isbns()
        print(f"{len(owned_isbns)} books in LL")

    # ── Search Prowlarr ───────────────────────────────────────────────────────
    if args.type == "audiobook":
        cats = NEWZNAB["audiobook"]
    elif args.type == "ebook":
        cats = NEWZNAB["ebook"]
    else:
        cats = NEWZNAB["audiobook"] + NEWZNAB["ebook"]

    print(f"Searching Prowlarr (MAM) for {args.query!r} (limit={args.limit})...", end=" ", flush=True)
    try:
        raw_results = prowlarr_search(args.query, cats, args.limit)
    except requests.RequestException as e:
        print(f"\nERROR: {e}")
        sys.exit(1)
    print(f"{len(raw_results)} results")

    # ── Cross-validate ────────────────────────────────────────────────────────
    print("Cross-validating against CSV...")

    confident    = []
    review       = []
    mam_only     = []
    already_owned = []
    matched_csv_isbns = set()

    for r in raw_results:
        itype   = detect_type(r)
        res_text = (r.get("title") or "") + " " + (r.get("description") or "")
        res_isbn = extract_isbn(res_text)
        mam_id   = extract_mam_id(r.get("guid") or "")

        result_info = {
            "title":        r.get("title"),
            "indexer":      r.get("indexer"),
            "type":         itype,
            "size_bytes":   r.get("size"),
            "seeders":      r.get("seeders") or 0,
            "isbn":         res_isbn,
            "mam_id":       mam_id,
            "download_url": r.get("downloadUrl") or r.get("magnetUrl") or "",
            "info_url":     r.get("infoUrl") or "",
        }

        # Already in LL library?
        if res_isbn and res_isbn in owned_isbns:
            already_owned.append({"result": result_info, "reason": "ISBN in LL library"})
            continue

        # Score against all CSV rows
        score, csv_row, reason = best_match(r, csv_rows)

        entry = {
            "score":  round(score, 3),
            "method": reason,
            "result": result_info,
            "csv":    csv_row,
        }

        if score >= CONFIDENT_THRESHOLD:
            confident.append(entry)
            if csv_row and csv_row["isbn13"]:
                matched_csv_isbns.add(csv_row["isbn13"])
        elif score >= REVIEW_THRESHOLD:
            review.append(entry)
            if csv_row and csv_row["isbn13"]:
                matched_csv_isbns.add(csv_row["isbn13"])
        else:
            mam_only.append({"result": result_info})

    # CSV rows not found in MAM search results
    csv_missing = [r for r in csv_rows
                   if r["isbn13"] not in matched_csv_isbns
                   and r["isbn13"] not in owned_isbns]

    # Sort confident by series order for readability
    confident.sort(key=lambda x: (
        (x["csv"] or {}).get("series", ""),
        (x["csv"] or {}).get("series_order", "0"),
    ))
    review.sort(key=lambda x: -x["score"])

    # ── Display results ───────────────────────────────────────────────────────
    print(f"\n{HR2}")
    print(f"  Results: {len(raw_results)} MAM  |  CSV: {len(csv_rows)} books")
    print(f"  CONFIDENT : {len(confident):>4}  (safe to queue)")
    print(f"  REVIEW    : {len(review):>4}  (check before queuing)")
    print(f"  MAM_ONLY  : {len(mam_only):>4}  (MAM has it, not in your wishlist)")
    print(f"  MISSING   : {len(csv_missing):>4}  (wishlist, not found in this search)")
    if already_owned:
        print(f"  OWNED     : {len(already_owned):>4}  (already in LL library — skipped)")
    print(HR2)

    print_bucket("CONFIDENT — safe to queue", confident)
    print_bucket("REVIEW — verify match before queuing", review)

    if mam_only:
        print(f"\n{HR}")
        print(f"  MAM_ONLY — on MAM but not in your wishlist  ({len(mam_only)})")
        print(HR)
        for item in mam_only:
            r = item["result"]
            print(f"  {r['type']:<9}  {fmt_size(r.get('size_bytes')):>7}  "
                  f"{r.get('seeders', 0):>4} seeds  {r.get('title', '?')[:55]}")

    if csv_missing:
        print(f"\n{HR}")
        print(f"  CSV MISSING — in wishlist, not found in this MAM search  ({len(csv_missing)})")
        print(HR)
        for row in csv_missing[:30]:
            series = f" [{row['series']} #{row['series_order']}]" if row.get("series") else ""
            print(f"  {row['isbn13'] or 'no-isbn':<14}  {row['title'][:50]}{series}")
        if len(csv_missing) > 30:
            print(f"  ... and {len(csv_missing) - 30} more (see JSON output)")

    # ── Save plan JSON ────────────────────────────────────────────────────────
    plan = {
        "query":         args.query,
        "type":          args.type,
        "searched_at":   datetime.now(timezone.utc).isoformat(),
        "csv_file":      args.csv,
        "csv_total":     len(csv_rows),
        "results_total": len(raw_results),
        "confident":     confident,
        "review":        review,
        "mam_only":      mam_only,
        "csv_missing":   csv_missing,
        "already_owned": already_owned,
    }
    save_plan(out_path, plan)

    print(f"\n{HR2}")
    print(f"  Full plan saved → {out_path}")
    print(f"  CONFIDENT  : {len(confident)} results ready")
    print(f"  REVIEW     : {len(review)} results need manual check")

    if not args.queue and confident:
        print(f"\n  To queue CONFIDENT results, re-run with --queue")
    print(HR2)

    # ── Queue if requested ────────────────────────────────────────────────────
    if args.queue:
        queue_confident(confident, log_path, args.yes)


if __name__ == "__main__":
    main()
