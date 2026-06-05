#!/usr/bin/env python3
"""
Queue best ebook + audiobook from all *_validated.json files.
Deduplicates by normalized title+type, picks best format then seeds.
"""
import json, glob, re, requests

QBIT_URL = "http://localhost:8082"
STAGING  = {"audiobook": "/mnt/storage/downloads/prowl/audiobooks",
            "ebook":     "/mnt/storage/downloads/prowl/ebooks"}
CATEGORY = {"audiobook": "prowl-audiobooks", "ebook": "prowl-ebooks"}
SKIP_Q   = frozenset(["defiance of the fall"])  # complete in library

FMT_AUDIO = {"m4b": 10, "mp3": 8, "m4a": 7, "flac": 6, "ogg": 4}
FMT_EBOOK = {"epub": 10, "azw3": 9, "cbz": 9, "cbr": 8, "mobi": 7, "pdf": 5, "lit": 4, "doc": 3}

def fmt_rank(title, itype):
    tl = title.lower()
    fmap = FMT_AUDIO if itype == "audiobook" else FMT_EBOOK
    for fmt, rank in fmap.items():
        if f"/ {fmt}" in tl or f"[{fmt}]" in tl or f" {fmt}]" in tl:
            return rank
    return 0

def norm(title):
    t = re.sub(r'\[ENG[^\]]*\]', '', title, flags=re.IGNORECASE)
    t = re.sub(r'\[VIP\]', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.lower()

def qlogin():
    s = requests.Session()
    s.post(f"{QBIT_URL}/api/v2/auth/login",
           data={"username": "admin", "password": "ctzlyLkXPSOX3r"},
           headers={"Referer": QBIT_URL})
    return s

def qadd(sess, url, itype, title, seeds):
    # Download torrent binary first to bypass qBit's async URL-fetch queue
    try:
        tr = sess.get(url, timeout=15)
        if tr.status_code != 200 or b"d8:" not in tr.content[:10] and not tr.content.startswith(b"d"):
            raise ValueError(f"bad torrent response: {tr.status_code} {tr.content[:40]}")
        torrent_bytes = tr.content
    except Exception as e:
        print(f"  [FAIL] {itype[:5]}: {title[:64]} — download err: {e}")
        return False

    r = sess.post(f"{QBIT_URL}/api/v2/torrents/add",
                  data={"savepath": STAGING[itype], "category": CATEGORY[itype]},
                  files={"torrents": ("torrent.torrent", torrent_bytes, "application/x-bittorrent")},
                  headers={"Referer": QBIT_URL})
    ok4 = r.status_code == 200 and r.text.lower().startswith("ok")
    ok_dup = r.status_code == 409  # already exists — treat as success
    try:
        ok5 = r.status_code == 202 and r.json().get("failure_count", 1) == 0
    except Exception:
        ok5 = False
    ok = ok4 or ok5 or ok_dup
    sym = "OK" if ok else "FAIL"
    print(f"  [{sym}] {itype[:5]}: {title[:64]} ({seeds}s)")
    return ok

def load_json(jf):
    with open(jf) as f:
        data = json.load(f)
    query = data.get("query", "")
    if query.lower().strip() in SKIP_Q:
        return query, None
    all_r = []
    for bucket in ("confident", "mam_only", "review"):
        for item in data.get(bucket, []):
            r = item.get("result", {}) if isinstance(item.get("result"), dict) else item
            if r and r.get("download_url"):
                all_r.append(r)
    # Deduplicate: per (norm_title, type) keep best format then seeds
    best = {}
    for r in all_r:
        title = r.get("title", "")
        itype = r.get("type", "ebook")
        key   = (norm(title), itype)
        seeds = r.get("seeders", 0) or 0
        fr    = fmt_rank(title, itype)
        if key not in best:
            best[key] = r
        else:
            old  = best[key]
            ofr  = fmt_rank(old.get("title", ""), itype)
            osds = old.get("seeders", 0) or 0
            if fr > ofr or (fr == ofr and seeds > osds):
                best[key] = r
    return query, list(best.values())


def main():
    sess = qlogin()
    json_files = sorted(glob.glob("/root/*_validated.json"))
    print(f"Processing {len(json_files)} JSON files\n")

    ok_total = fail_total = skip_total = 0
    log = []

    for jf in json_files:
        query, results = load_json(jf)
        if results is None:
            print(f"[SKIP] {query} — complete in library")
            skip_total += 1
            continue

        ebooks = sorted(
            [r for r in results if r.get("type") == "ebook"],
            key=lambda r: (fmt_rank(r.get("title", ""), "ebook"), r.get("seeders", 0)),
            reverse=True,
        )
        audios = sorted(
            [r for r in results if r.get("type") == "audiobook"],
            key=lambda r: (fmt_rank(r.get("title", ""), "audiobook"), r.get("seeders", 0)),
            reverse=True,
        )

        print(f"=== {query} === ({len(ebooks)} ebook | {len(audios)} audio)")
        for r in ebooks + audios:
            ok = qadd(sess, r["download_url"], r["type"], r["title"], r.get("seeders", 0))
            log.append({
                "query": query, "ok": ok,
                "type": r["type"], "title": r["title"],
                "seeds": r.get("seeders", 0),
            })
            if ok:
                ok_total += 1
            else:
                fail_total += 1

    print(f"\n{'='*60}")
    print(f"Queued: {ok_total} OK  |  {fail_total} FAIL  |  {skip_total} series skipped")
    with open("/root/bulk_queue_log.json", "w") as f:
        json.dump(log, f, indent=2)
    print("Log -> /root/bulk_queue_log.json")


if __name__ == "__main__":
    main()
