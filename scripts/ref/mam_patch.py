#!/usr/bin/env python3
"""
mam_patch.py — Fix the two issues from mam_cleanup.py run:
  1. Resume Group A torrents (qBit v5 uses /torrents/start, not /torrents/resume)
  2. Retry LL findBook for the 90 "not_found_in_ll" entries with cleaned titles

Usage:
    python3 mam_patch.py              # dry run: show what would be done
    python3 mam_patch.py --execute    # actually resume + search
"""
import requests
import json
import os
import re
import sys
import argparse
from urllib.parse import quote_plus

QBIT      = "http://localhost:8082"
QUSER     = "admin"
QPASS     = "ctzlyLkXPSOX3r"
LL_BASE   = "http://localhost:5299/api?apikey=2833a671925b43089e2002226e17fd4e"

MAM_CATS  = ["mam-audiobooks", "mam-ebooks", "mam-unmatched"]
STOPPED   = ("stoppedDL", "error", "missingFiles")
STOP_WORDS = frozenset([
    "the","a","an","of","and","in","by","to","is","at","on","with",
    "for","its","his","her","from","this","that","are","was","be","or",
])


# ── Title cleaning ─────────────────────────────────────────────────────────────

EXTENSIONS = re.compile(
    r'\.(epub|m4b|m4a|mp3|mp4|pdf|cbr|cbz|mobi|azw3|opf|aac|flac|djvu|lit|doc|docx)$',
    re.IGNORECASE
)

def clean_title(raw):
    """
    Extract a clean searchable book title from a torrent filename/name.
    Removes: extensions, author attributions, ASIN/bracket IDs, encoding artifacts.
    """
    s = raw

    # Strip file extension
    s = EXTENSIONS.sub('', s)

    # Remove ASINs and IDs in brackets: [B0DLJD87C5], {B0F1554Y8N}, etc.
    s = re.sub(r'\s*[\[\{][A-Z0-9]{8,}[\]\}]\s*', ' ', s)

    # Remove short parenthetical suffixes: (retail), (2024), (eng), etc.
    s = re.sub(r'\s*\([^)]{1,25}\)\s*$', ' ', s)
    s = re.sub(r'\s*\[[^\]]{1,25}\]\s*$', ' ', s)

    # Replace encoding artifacts with spaces
    s = re.sub(r'[_꞉]', ' ', s)

    # Replace dots used as word separators (scene-release style: Game.Of.Thrones)
    # Only replace dots surrounded by word chars (not decimals or acronyms)
    s = re.sub(r'(?<=\w)\.(?=\w)', ' ', s)

    # Normalise whitespace
    s = re.sub(r'\s+', ' ', s).strip()

    # NOTE: do NOT split on " - " — Goodreads search handles "Author - Title"
    # and "Title - Author" formats fine, and splitting picks wrong part for
    # short titles (e.g. "Hooked - Asako Yuzuki" → "Asako Yuzuki").

    return s.strip()


# ── qBittorrent ─────────────────────────────────────────────────────────────────

def qlogin():
    sess = requests.Session()
    r = sess.post(f"{QBIT}/api/v2/auth/login",
                  data={"username": QUSER, "password": QPASS},
                  headers={"Referer": QBIT})
    assert r.status_code in (200, 204), f"Login failed {r.status_code}: {r.text!r}"
    return sess


def qget_mam(sess):
    r = sess.get(f"{QBIT}/api/v2/torrents/info",
                 headers={"Referer": QBIT},
                 params={"limit": 5000})
    all_t = r.json()
    return [t for t in all_t if t.get("category", "").startswith("mam-")]


def qstart(sess, h):
    """Resume/start a stopped torrent (qBit v5 renamed 'resume' → 'start')."""
    r = sess.post(f"{QBIT}/api/v2/torrents/start",
                  headers={"Referer": QBIT},
                  data={"hashes": h})
    return r.status_code == 200


# ── LazyLibrarian ──────────────────────────────────────────────────────────────

def ll_get(cmd, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{LL_BASE}&cmd={cmd}" + (f"&{q}" if q else "")
    try:
        r = requests.get(url, timeout=30)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


_ll_books_cache = None

def ll_get_all_books():
    global _ll_books_cache
    if _ll_books_cache is None:
        data = ll_get("getAllBooks")
        _ll_books_cache = data if isinstance(data, list) else []
        print(f"  LL database: {len(_ll_books_cache)} books")
    return _ll_books_cache


def norm(s):
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", str(s).lower())
    s = re.sub(r"\bbook\s*\d+\b|\bvol\w*\s*\d+\b|\bpart\s*\d+\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())


def keywords(s):
    return {w for w in norm(s).split() if w not in STOP_WORDS and len(w) > 2}


def ll_find_in_db(title):
    t_norm = norm(title)
    t_kw   = keywords(title)
    best, best_r = None, 0.0
    for b in ll_get_all_books():
        b_norm = norm(b.get("BookName", ""))
        b_kw   = {w for w in b_norm.split() if w not in STOP_WORDS}
        if not b_kw:
            continue
        overlap = t_kw & b_kw
        ratio   = len(overlap) / max(len(t_kw), len(b_kw))
        if ratio > best_r and ratio >= 0.6 and len(overlap) >= 2:
            best_r, best = ratio, b
    if best:
        return best["BookID"], best["BookName"]
    return None, None


def ll_search(title, category):
    """
    Try to find title in LL DB or Goodreads and trigger a search.
    Returns (action, detail).
    """
    book_type = "audiobook" if "audio" in category else "book"

    # 1. Already in LL?
    bid, bname = ll_find_in_db(title)
    if bid:
        ll_get("searchBook", id=bid)
        return "triggered_existing", f"id={bid} '{bname[:40]}'"

    # 2. Search Goodreads
    found = ll_get("findBook", name=quote_plus(title), type=book_type)
    if isinstance(found, list) and found:
        first  = found[0]
        ext_id = (first.get("bookid") or first.get("BookID") or
                  first.get("id") or first.get("bookID"))
        if ext_id:
            ll_get("addBook", id=ext_id)
            ll_get("searchBook", id=ext_id)
            bname_ext = first.get("bookname") or first.get("BookName") or ""
            return "added_and_searched", f"id={ext_id} '{bname_ext[:40]}'"

    return "not_found", (
        f"findBook({title[:40]!r}) returned "
        f"{len(found) if isinstance(found, list) else type(found).__name__}"
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute",  action="store_true", help="Actually take action")
    ap.add_argument("--skip-ll",  action="store_true", help="Skip LL calls (resume A only)")
    ap.add_argument("--results",  default="/tmp/mam_cleanup_results.json",
                    help="Path to previous run's results JSON")
    args = ap.parse_args()

    HR  = "─" * 72
    HR2 = "═" * 72
    mode = "EXECUTE" if args.execute else "DRY RUN"

    print(f"\n{HR2}\n  MAM Patch  [{mode}]\n{HR2}\n")

    # ── Connect ────────────────────────────────────────────────────────────────
    print("Connecting to qBittorrent...", end=" ", flush=True)
    sess = qlogin()
    print("OK")
    print()

    # ── Group A deleted: search LL for them ───────────────────────────────────
    # These 13 were partially downloaded but were accidentally deleted.
    # Re-trigger LL search so they download fresh into the right library path.
    GROUP_A_DELETED = [
        ("Shirtaloon - He Who Fights with Monsters (Book 1)", "mam-ebooks"),
        ("Dune House Corrino", "mam-ebooks"),
        ("The Courageous Life of Weary Dunlop - Peter FitzSimons", "mam-audiobooks"),
        ("Drowning in Paper Flowers", "mam-audiobooks"),
        ("The Night We Met - Abby Jimenez", "mam-unmatched"),
        ("Mystic Harmony", "mam-audiobooks"),
        ("Game of Thrones A Song of Fire and Ice Full Series George R R Martin", "mam-ebooks"),
        ("Accidental Mystic", "mam-audiobooks"),
        ("Contention", "mam-audiobooks"),
        ("COINage April-May 2026", "mam-unmatched"),
        ("Shinshu Roberts - Meeting the Myriad Things", "mam-unmatched"),
        ("Eric H Cline 1177 BC A Graphic History of the Year Civilisation Collapsed", "mam-unmatched"),
        ("Ross Thomas - The Mordida Man", "mam-ebooks"),
    ]

    print(f"{HR}\nGROUP A (deleted) — Re-trigger LL searches ({len(GROUP_A_DELETED)} titles)\n{HR}\n")

    if not args.skip_ll:
        print("Loading LL database for Group A check...", end=" ", flush=True)
        ll_get_all_books()
        print("OK\n")

    for name, cat in GROUP_A_DELETED:
        cleaned = clean_title(name)
        print(f"  {name[:65]}")
        print(f"    → {cleaned!r}")

    if args.execute and not args.skip_ll:
        print(f"\nSearching LL for {len(GROUP_A_DELETED)} Group A titles...\n")
        a_counts = {"triggered_existing": 0, "added_and_searched": 0, "not_found": 0}
        for name, cat in GROUP_A_DELETED:
            cleaned = clean_title(name)
            action, detail = ll_search(cleaned, cat)
            a_counts[action] = a_counts.get(action, 0) + 1
            sym = "✓" if action != "not_found" else "?"
            print(f"  [{sym}] {name[:55]}")
            print(f"         LL: {action} — {detail}")
        print(f"\n  Results: triggered={a_counts['triggered_existing']}  "
              f"added={a_counts.get('added_and_searched',0)}  "
              f"not_found={a_counts['not_found']}")
    elif not args.skip_ll:
        print(f"\n  [DRY RUN] Would trigger LL search for {len(GROUP_A_DELETED)} titles")

    # ── Any currently stopped partial downloads ────────────────────────────────
    mam = qget_mam(sess)
    group_a_live = [t for t in mam if t["state"] in STOPPED and 0 < t["progress"] < 1]
    if group_a_live:
        print(f"\n{HR}\nGROUP A (live) — {len(group_a_live)} paused partial downloads to resume\n{HR}")
        for t in sorted(group_a_live, key=lambda x: -x["progress"]):
            pct = t["progress"] * 100
            print(f"  {pct:5.1f}%  {t['name'][:60]}")
        if args.execute:
            print(f"\nStarting {len(group_a_live)} torrents...")
            n_ok = sum(1 for t in group_a_live if qstart(sess, t["hash"]))
            print(f"  Started: {n_ok}/{len(group_a_live)}")

    # ── Group B: retry LL for failed entries ───────────────────────────────────
    if args.skip_ll:
        print(f"\n{HR}\nSkipping LL retry (--skip-ll)\n{HR}")
        return

    print(f"\n{HR}\nGROUP B — Retry LL searches with cleaned titles\n{HR}")

    if not os.path.exists(args.results):
        print(f"  ERROR: {args.results} not found — run mam_cleanup.py first")
        sys.exit(1)

    with open(args.results) as f:
        prev = json.load(f)

    all_ll = prev.get("deleted_ll", [])
    failed = [e for e in all_ll if e.get("ll_action") in ("not_found_in_ll", "not_found", "")]
    print(f"  Previous run: {len(all_ll)} LL attempts, {len(failed)} failed\n")

    print("  Loading LL book database...")
    ll_get_all_books()
    print()

    for e in failed:
        raw_name = e["name"]
        cat      = e.get("cat", "mam-unmatched")
        cleaned  = clean_title(raw_name)
        print(f"  {raw_name[:65]}")
        print(f"    → cleaned: {cleaned!r}")

    if not args.execute:
        print(f"\n  [DRY RUN] Would retry LL search for {len(failed)} titles")
        print("  Re-run with --execute to proceed.")
        return

    print(f"\nRetrying {len(failed)} LL searches with cleaned titles...\n")
    ll_counts = {"triggered_existing": 0, "added_and_searched": 0, "not_found": 0}
    retry_results = []

    for i, e in enumerate(failed, 1):
        raw_name = e["name"]
        cat      = e.get("cat", "mam-unmatched")
        cleaned  = clean_title(raw_name)

        action, detail = ll_search(cleaned, cat)
        ll_counts[action] = ll_counts.get(action, 0) + 1

        sym = "✓" if action != "not_found" else "?"
        print(f"  [{i:3}/{len(failed)}][{sym}] {raw_name[:50]}")
        print(f"             → {cleaned[:40]}  LL:{action}")

        e["ll_action_retry"] = action
        e["ll_detail_retry"] = detail
        e["cleaned_title"] = cleaned
        retry_results.append(e)

    print(f"\n{HR2}")
    print(f"  Retry summary:")
    print(f"    triggered_existing: {ll_counts['triggered_existing']}")
    print(f"    added_and_searched: {ll_counts.get('added_and_searched',0)}")
    print(f"    still not found:    {ll_counts['not_found']}")
    print(HR2)

    # Update results file with retry data
    prev["ll_retry"] = retry_results
    prev["ll_retry_summary"] = ll_counts
    with open(args.results, "w") as f:
        json.dump(prev, f, indent=2)
    print(f"  Updated results → {args.results}\n")

    # Show titles still not found (might be non-book content)
    still_missing = [e for e in retry_results if e["ll_action_retry"] == "not_found"]
    if still_missing:
        print(f"  Still not found in Goodreads ({len(still_missing)} — likely non-book content):")
        for e in still_missing:
            print(f"    {e['name'][:72]}")


if __name__ == "__main__":
    main()
