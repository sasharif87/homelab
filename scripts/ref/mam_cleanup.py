#!/usr/bin/env python3
"""
mam_cleanup.py — Triage 122 paused MAM torrents

Group A (partial, >0%): all have correct save paths — resume them.
Group B (0%, nothing on disk yet):
  - Scan disk for existing files with matching names.
  - Found on disk  → delete torrent entry from qBit (file stays).
  - Not on disk    → delete from qBit, trigger LL search so LL re-finds
                     it on MAM and downloads to the correct library path.

Usage:
    python3 mam_cleanup.py               # dry-run report + JSON output
    python3 mam_cleanup.py --execute     # take action (resume A, clean B, trigger LL)
    python3 mam_cleanup.py --execute --skip-ll   # skip LL calls (delete only)
    python3 mam_cleanup.py --execute --limit 10  # process first 10 Group B only
"""
import requests
import json
import os
import re
import sys
import argparse
from urllib.parse import quote_plus

# ── Config ────────────────────────────────────────────────────────────────────
QBIT      = "http://localhost:8082"
QUSER     = "admin"
QPASS     = "ctzlyLkXPSOX3r"
LL_BASE   = "http://localhost:5299/api?apikey=2833a671925b43089e2002226e17fd4e"

MEDIA_DIRS = [
    "/mnt/storage/media/audiobooks",
    "/mnt/storage/media/ebooks",
    "/mnt/storage/downloads",
]
MAM_CATS   = ["mam-audiobooks", "mam-ebooks", "mam-unmatched"]
# qBit v5 state names
STOPPED    = ("stoppedDL", "error", "missingFiles")
SEEDING    = ("uploading", "stalledUP", "forcedUP", "queuedUP", "checkingUP", "stoppedUP")

STOP_WORDS = frozenset([
    "the","a","an","of","and","in","by","to","is","at","on","with",
    "for","its","his","her","from","this","that","are","was","be","or",
])

# ── Text normalisation ────────────────────────────────────────────────────────

def norm(s):
    s = re.sub(r"\[.*?\]|\(.*?\)|\{.*?\}", "", str(s).lower())
    s = re.sub(r"\bbook\s*\d+\b|\bvol\w*\s*\d+\b|\bpart\s*\d+\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(s.split())

def keywords(s):
    return {w for w in norm(s).split() if w not in STOP_WORDS and len(w) > 2}

# ── qBittorrent ───────────────────────────────────────────────────────────────

def qlogin():
    sess = requests.Session()
    r = sess.post(f"{QBIT}/api/v2/auth/login",
                  data={"username": QUSER, "password": QPASS},
                  headers={"Referer": QBIT})
    # v5 returns 204 with empty body on success
    assert r.status_code in (200, 204), f"Login failed HTTP {r.status_code}: {r.text!r}"
    return sess

def qget_mam(sess):
    r = sess.get(f"{QBIT}/api/v2/torrents/info",
                 headers={"Referer": QBIT},
                 params={"limit": 5000})
    all_t = r.json()
    return [t for t in all_t if t.get("category", "").startswith("mam-")]

def qresume(sess, h):
    r = sess.post(f"{QBIT}/api/v2/torrents/resume",
                  headers={"Referer": QBIT},
                  data={"hashes": h})
    return r.status_code == 200

def qdel(sess, h, delete_files=False):
    r = sess.post(f"{QBIT}/api/v2/torrents/delete",
                  headers={"Referer": QBIT},
                  data={"hashes": h, "deleteFiles": str(delete_files).lower()})
    return r.status_code == 200

# ── Disk index ────────────────────────────────────────────────────────────────

def build_index():
    """
    Walk media directories.
    Returns dict: normalized_stem -> [{"path": ..., "size": ..., "type": "file"|"dir"}]
    Also indexes directory names at depth 1 and 2 (book folder names).
    """
    idx = {}

    def add(key, entry):
        idx.setdefault(key, []).append(entry)

    for base in MEDIA_DIRS:
        if not os.path.exists(base):
            print(f"  WARN: {base} not mounted", file=sys.stderr)
            continue
        n = 0
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            depth = root.count(os.sep) - base.count(os.sep)

            # Index directory names at depth 1 and 2 (book/author folders)
            if depth <= 2:
                for d in dirs:
                    dpath = os.path.join(root, d)
                    try:
                        dsize = sum(
                            os.path.getsize(os.path.join(dp, f))
                            for dp, _, fs in os.walk(dpath)
                            for f in fs
                        )
                    except OSError:
                        dsize = 0
                    add(norm(d), {"path": dpath, "size": dsize, "type": "dir"})

            for fname in files:
                if fname.startswith("."):
                    continue
                stem = norm(os.path.splitext(fname)[0])
                fpath = os.path.join(root, fname)
                try:
                    add(stem, {"path": fpath, "size": os.path.getsize(fpath), "type": "file"})
                    n += 1
                except OSError:
                    pass
        print(f"    {n:>7,} files in {base}")
    return idx


def find_matches(name, idx):
    """
    Returns (list_of_hits, match_type) where each hit is a dict with path/size/type.
    match_type: 'exact' | 'fuzzy' | 'none'
    """
    n = norm(name)
    if n in idx:
        return idx[n], "exact"

    kw = keywords(name)
    if len(kw) < 2:
        return [], "none"

    hits = []
    for key, entries in idx.items():
        kw2 = {w for w in key.split() if w not in STOP_WORDS}
        overlap = kw & kw2
        if not overlap:
            continue
        ratio = len(overlap) / len(kw)
        if ratio >= 0.6 and len(overlap) >= 2:
            hits.extend(
                {"path": e["path"], "size": e["size"], "type": e["type"], "ratio": ratio}
                for e in entries
            )
    hits.sort(key=lambda x: -x["ratio"])
    return hits, ("fuzzy" if hits else "none")


# ── LazyLibrarian ─────────────────────────────────────────────────────────────

def ll_get(cmd, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{LL_BASE}&cmd={cmd}" + (f"&{q}" if q else "")
    try:
        r = requests.get(url, timeout=30)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


# Cache so we call getAllBooks only once
_ll_books_cache = None

def ll_get_all_books():
    global _ll_books_cache
    if _ll_books_cache is None:
        data = ll_get("getAllBooks")
        _ll_books_cache = data if isinstance(data, list) else []
        print(f"  LL: {len(_ll_books_cache)} books in database")
    return _ll_books_cache


def ll_find_in_db(title):
    """
    Check LL's existing DB for a book matching title.
    Returns (BookID, BookName) or (None, None).
    """
    t_norm = norm(title)
    t_kw   = keywords(title)
    best   = None
    best_r = 0.0

    for b in ll_get_all_books():
        b_norm = norm(b.get("BookName", ""))
        b_kw   = {w for w in b_norm.split() if w not in STOP_WORDS}
        if not b_kw:
            continue
        overlap = t_kw & b_kw
        ratio   = len(overlap) / max(len(t_kw), len(b_kw))
        if ratio > best_r and ratio >= 0.6 and len(overlap) >= 2:
            best_r = ratio
            best   = b

    if best:
        return best["BookID"], best["BookName"]
    return None, None


def ll_trigger(title, category):
    """
    Trigger LL to search for this title.
    Returns (action_str, detail_str).
    """
    book_type = "audiobook" if "audio" in category else "book"

    # 1. Already in LL DB?
    bid, bname = ll_find_in_db(title)
    if bid:
        ll_get("searchBook", id=bid)
        return "triggered_existing", f"id={bid} '{bname[:40]}'"

    # 2. Search external (Goodreads) and add
    found = ll_get("findBook", name=quote_plus(title), type=book_type)
    if isinstance(found, list) and found:
        first = found[0]
        ext_id = (first.get("bookid") or first.get("BookID") or
                  first.get("id") or first.get("bookID"))
        if ext_id:
            ll_get("addBook", id=ext_id)
            ll_get("searchBook", id=ext_id)
            bname_ext = first.get("bookname") or first.get("BookName") or ""
            return "added_and_searched", f"id={ext_id} '{bname_ext[:40]}'"

    return "not_found_in_ll", f"findBook returned {len(found) if isinstance(found, list) else type(found).__name__}"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute",  action="store_true",
                    help="Resume Group A + delete Group B + trigger LL")
    ap.add_argument("--skip-ll",  action="store_true",
                    help="Skip LazyLibrarian API calls")
    ap.add_argument("--limit",    type=int, default=0,
                    help="Process only first N Group B not-on-disk torrents")
    args = ap.parse_args()

    HR  = "─" * 72
    HR2 = "═" * 72
    mode = "EXECUTE" if args.execute else "DRY RUN"

    print(f"\n{HR2}\n  MAM Torrent Triage  [{mode}]\n{HR2}\n")

    # ── Connect ──────────────────────────────────────────────────────────────
    print("Connecting to qBittorrent...", end=" ", flush=True)
    sess = qlogin()
    print("OK")

    if not args.skip_ll:
        print("Checking LazyLibrarian...", end=" ", flush=True)
        ver = ll_get("getVersion")
        if isinstance(ver, dict) and ver.get("Success"):
            print(f"OK  (version {ver.get('current_version','?')})")
        else:
            print(f"WARN: {ver}")
    print()

    # ── Fetch torrents ────────────────────────────────────────────────────────
    print("Fetching MAM torrents...")
    mam = qget_mam(sess)
    print(f"  Total MAM: {len(mam)}")

    seeding = [t for t in mam if t["state"] in SEEDING]
    group_a = [t for t in mam if t["state"] in STOPPED and 0 < t["progress"] < 1]
    group_b = [t for t in mam if t["state"] in STOPPED and t["progress"] < 0.01]

    print(f"  Seeding:   {len(seeding):>4}")
    print(f"  Group A:   {len(group_a):>4}  (partial downloads, >0%)")
    print(f"  Group B:   {len(group_b):>4}  (stopped at 0%)")
    print()

    # ── GROUP A ──────────────────────────────────────────────────────────────
    print(f"{HR}\nGROUP A — PARTIAL DOWNLOADS  ({len(group_a)} torrents)\n{HR}")

    def path_ok(t):
        cat = t.get("category", "")
        sp  = t.get("save_path", "")
        if "audio" in cat:
            return "/audiobooks" in sp or "/downloads" in sp
        if "ebook" in cat:
            return "/ebooks" in sp or "/downloads" in sp
        # mam-unmatched: /downloads is expected
        return "/downloads" in sp or "/audiobooks" in sp or "/ebooks" in sp

    a_good, a_bad = [], []
    for t in sorted(group_a, key=lambda x: -x["progress"]):
        ok = path_ok(t)
        (a_good if ok else a_bad).append(t)
        flag  = "OK " if ok else "BAD"
        pct   = t["progress"] * 100
        cat   = t.get("category", "")
        sp    = t.get("save_path", "")
        state = t.get("state", "")
        print(f"  [{flag}] {pct:5.1f}%  {t['name'][:60]}")
        print(f"          cat={cat}  state={state}")
        print(f"          save: {sp}")

    print(f"\n  → {len(a_good)} with correct paths  /  {len(a_bad)} suspicious paths")
    if a_bad:
        print("  BAD PATH torrents (need manual review):")
        for t in a_bad:
            print(f"    {t['name'][:70]}  save={t.get('save_path','')}")

    # ── GROUP B ──────────────────────────────────────────────────────────────
    print(f"\n{HR}\nGROUP B — ZERO PROGRESS  ({len(group_b)} torrents)\n{HR}")

    print("\nBuilding disk index (walks audiobooks + ebooks + downloads)...")
    idx = build_index()
    total_files = sum(1 for v in idx.values() for e in v if e["type"] == "file")
    print(f"  {total_files:,} files indexed  ({len(idx):,} unique stems)\n")

    b_on_disk_exact = []   # (torrent, hits) — high confidence, skip LL
    b_on_disk_fuzzy = []   # (torrent, hits) — low confidence, still trigger LL
    b_not_on_disk   = []   # torrent

    for t in group_b:
        hits, mtype = find_matches(t["name"], idx)
        if hits and mtype == "exact":
            b_on_disk_exact.append((t, hits[:3]))
        elif hits:
            b_on_disk_fuzzy.append((t, hits[:3]))
        else:
            b_not_on_disk.append(t)

    b_on_disk = b_on_disk_exact + b_on_disk_fuzzy

    print(f"  Exact match on disk: {len(b_on_disk_exact)}  (skip LL)")
    print(f"  Fuzzy match on disk: {len(b_on_disk_fuzzy)}  (delete + trigger LL as safeguard)")
    print(f"  Not on disk:         {len(b_not_on_disk)}  (delete + trigger LL)")

    if b_on_disk_exact:
        print(f"\n--- EXACT MATCH ON DISK ({len(b_on_disk_exact)}) — delete qBit entry, skip LL ---")
        for t, hits in b_on_disk_exact:
            cat = t.get("category", "")
            print(f"\n  {t['name'][:72]}  [{cat}]")
            for h in hits[:2]:
                mb = h["size"] // 1_048_576
                print(f"    [{h['type']}] {h['path']}  ({mb} MB)")

    if b_on_disk_fuzzy:
        print(f"\n--- FUZZY MATCH ON DISK ({len(b_on_disk_fuzzy)}) — delete + trigger LL (may be wrong match) ---")
        for t, hits in b_on_disk_fuzzy:
            cat = t.get("category", "")
            print(f"\n  {t['name'][:72]}  [{cat}]")
            for h in hits[:2]:
                mb = h["size"] // 1_048_576
                print(f"    [{h['type']}] {h['path']}  ({mb} MB)")

    if not args.skip_ll:
        print(f"\nLoading LL book database...")
        ll_get_all_books()

    to_proc = b_not_on_disk[:args.limit] if args.limit else b_not_on_disk

    print(f"\n--- NOT ON DISK ({len(b_not_on_disk)}) — will delete + trigger LL ---")
    for t in to_proc:
        cat = t.get("category", "")
        sp  = t.get("save_path", "")
        print(f"  {t['name'][:65]}  [{cat}]")

    if not args.execute:
        print(f"\n{'─'*72}")
        print(f"  [DRY RUN] Would:")
        print(f"    - Resume {len(a_good)} Group A torrents")
        if a_bad:
            print(f"    - Flag {len(a_bad)} Group A torrents with bad paths (no action)")
        print(f"    - Delete {len(b_on_disk_exact)} Group B exact-match entries from qBit (no LL)")
        print(f"    - Delete {len(b_on_disk_fuzzy)} Group B fuzzy-match entries from qBit + trigger LL")
        print(f"    - Delete {len(to_proc)} Group B 'not on disk' from qBit + trigger LL")
        print(f"  Re-run with --execute to proceed.\n")

        report = {
            "mode": "dry_run",
            "seeding": len(seeding),
            "group_a_resume": [
                {"name": t["name"], "pct": round(t["progress"] * 100, 1),
                 "save": t["save_path"], "cat": t["category"]}
                for t in a_good
            ],
            "group_a_bad_path": [
                {"name": t["name"], "pct": round(t["progress"] * 100, 1),
                 "save": t["save_path"], "cat": t["category"]}
                for t in a_bad
            ],
            "group_b_exact_on_disk": [
                {"name": t["name"], "cat": t.get("category", ""),
                 "match": hits[0]["path"]}
                for t, hits in b_on_disk_exact
            ],
            "group_b_fuzzy_on_disk": [
                {"name": t["name"], "cat": t.get("category", ""),
                 "match": hits[0]["path"], "ratio": hits[0].get("ratio", 0)}
                for t, hits in b_on_disk_fuzzy
            ],
            "group_b_not_on_disk": [
                {"name": t["name"], "cat": t.get("category", ""),
                 "hash": t["hash"], "save_path": t.get("save_path", "")}
                for t in b_not_on_disk
            ],
        }
        with open("/tmp/mam_triage.json", "w") as f:
            json.dump(report, f, indent=2)
        print("  Full report saved to /tmp/mam_triage.json")
        return

    # ═══════════════════════════════════════════════════════════════════════════
    # EXECUTE
    # ═══════════════════════════════════════════════════════════════════════════
    print(f"\n{HR2}\n  EXECUTING\n{HR2}\n")

    results = {"resumed_a": [], "deleted_exact": [], "deleted_ll": []}
    ll_counts = {"triggered_existing": 0, "added_and_searched": 0,
                 "not_found_in_ll": 0, "skipped": 0}

    def do_ll(name, cat):
        if args.skip_ll:
            ll_counts["skipped"] += 1
            return "skipped", ""
        action, detail = ll_trigger(name, cat)
        ll_counts[action] = ll_counts.get(action, 0) + 1
        return action, detail

    # ── Resume Group A ────────────────────────────────────────────────────────
    print(f"Resuming {len(a_good)} Group A torrents...")
    for t in a_good:
        ok = qresume(sess, t["hash"])
        sym = "✓" if ok else "✗"
        print(f"  [{sym}] {t['name'][:65]}")
        results["resumed_a"].append({"name": t["name"], "ok": ok})

    # ── Delete Group B exact-match entries (skip LL — file already there) ─────
    if b_on_disk_exact:
        print(f"\nDeleting {len(b_on_disk_exact)} exact-match Group B entries (file confirmed on disk)...")
        for t, hits in b_on_disk_exact:
            ok = qdel(sess, t["hash"], delete_files=False)
            sym = "✓" if ok else "✗"
            print(f"  [{sym}] {t['name'][:65]}")
            print(f"         disk: {hits[0]['path']}")
            results["deleted_exact"].append(
                {"name": t["name"], "ok": ok, "disk_path": hits[0]["path"]}
            )

    # ── Delete Group B fuzzy-match entries + trigger LL as safeguard ──────────
    # Fuzzy matches might be wrong (e.g. series books matching the wrong volume)
    needs_ll = [(t, True) for t in to_proc]  # not on disk
    if b_on_disk_fuzzy:
        print(f"\nDeleting {len(b_on_disk_fuzzy)} fuzzy-match Group B entries + triggering LL...")
        for t, hits in b_on_disk_fuzzy:
            ok = qdel(sess, t["hash"], delete_files=False)
            sym = "✓" if ok else "✗"
            print(f"  [{sym}] {t['name'][:55]}  (fuzzy→{hits[0]['path'][-30:]})")
            ll_action, ll_detail = do_ll(t["name"], t.get("category", ""))
            if not args.skip_ll:
                print(f"         LL: {ll_action} — {ll_detail}")
            results["deleted_ll"].append({
                "name": t["name"], "hash": t["hash"], "deleted": ok,
                "match_type": "fuzzy", "disk_hint": hits[0]["path"],
                "ll_action": ll_action, "ll_detail": ll_detail,
            })

    # ── Delete Group B not-on-disk + trigger LL ───────────────────────────────
    print(f"\nProcessing {len(to_proc)} Group B 'not on disk' torrents...")
    for i, t in enumerate(to_proc, 1):
        name  = t["name"]
        h     = t["hash"]
        cat   = t.get("category", "")

        del_ok = qdel(sess, h, delete_files=False)
        ll_action, ll_detail = do_ll(name, cat)

        sym = "✓" if del_ok else "✗"
        print(f"  [{i:3}/{len(to_proc)}][{sym}] {name[:55]}")
        if not args.skip_ll:
            print(f"             LL: {ll_action} — {ll_detail}")

        results["deleted_ll"].append({
            "name": name, "hash": h, "deleted": del_ok,
            "match_type": "not_on_disk",
            "ll_action": ll_action, "ll_detail": ll_detail,
        })

    # ── Summary ───────────────────────────────────────────────────────────────
    n_resumed  = sum(1 for r in results["resumed_a"]    if r["ok"])
    n_del_exact= sum(1 for r in results["deleted_exact"] if r["ok"])
    n_del_ll   = sum(1 for r in results["deleted_ll"]   if r["deleted"])

    print(f"\n{HR2}")
    print(f"  Done.")
    print(f"  Group A resumed:             {n_resumed}/{len(a_good)}")
    print(f"  Group B exact-on-disk del:   {n_del_exact}/{len(b_on_disk_exact)}")
    print(f"  Group B fuzzy+not-on-disk:   {n_del_ll}/{len(b_on_disk_fuzzy) + len(to_proc)}")
    if not args.skip_ll:
        print(f"  LL: triggered_existing={ll_counts['triggered_existing']}  "
              f"added_new={ll_counts.get('added_and_searched',0)}  "
              f"not_found={ll_counts['not_found_in_ll']}")
    print(HR2)

    results["summary"] = {
        "resumed_a": n_resumed, "deleted_exact": n_del_exact,
        "deleted_ll": n_del_ll, "ll_counts": ll_counts,
    }
    with open("/tmp/mam_cleanup_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("  Full results → /tmp/mam_cleanup_results.json\n")


if __name__ == "__main__":
    main()
