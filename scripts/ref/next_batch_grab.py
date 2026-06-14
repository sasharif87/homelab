#!/usr/bin/env python3
"""
next_batch_grab.py — Fresh Prowlarr searches for the agreed next-batch items,
then add matches to qBit one at a time with delays.

Covers:
  - Defiance of the Fall (audio, books 1-16)
  - Shadow Slave (ebook+audio)
  - Ultramarines (ebook)
  - Battletech Blood Legacy (audio)
  - Dungeon Crawler Carl 2-7 (audio)
  - Rise of the Ranger / Echoes Saga 3-9 (ebook+audio)
  - Backyard Starship 2-10 (ebook+audio)
  - Cookbook / Home Maintenance / Home Distilling / Brewing wishlist (44 grouped queries)

~56 Prowlarr searches total. MAM quota is 30/hr, so this script paces
searches SEARCH_DELAY seconds apart (default 125s => ~56 searches in ~2hrs).
Run with --fast for testing matching logic without the long waits (dry-run only).

Usage:
    python3 next_batch_grab.py --dry-run            # search + show picks, no downloads
    python3 next_batch_grab.py --dry-run --fast     # quick test, short delays
    python3 next_batch_grab.py --yes                # search + add picks, no prompts
"""
import sys, re, time, argparse
sys.path.insert(0, '/root')
import prowl_add as pa

SEARCH_DELAY = 125   # seconds between Prowlarr searches (30/hr budget)
ADD_DELAY    = 4     # seconds between qBit/MAM download fetches

MAM_INDEXER_ID = 6


def mam_search(query, cats, limit=40, offset=0):
    """Like pa.prowlarr_search but scoped to MAM only (indexerIds=-1 errors
    out when other indexers are in cooldown)."""
    import requests
    params = [
        ("query", query),
        ("indexerIds", MAM_INDEXER_ID),
        ("type", "search"),
        ("limit", limit),
        ("offset", offset),
        ("apikey", pa.PROWLARR_KEY),
    ]
    for c in cats:
        params.append(("categories", c))
    resp = requests.get(f"{pa.PROWLARR_URL}/api/v1/search", params=params, timeout=30)
    resp.raise_for_status()
    results = resp.json()
    return sorted(results, key=lambda r: (r.get("seeders") or 0, r.get("size") or 0), reverse=True)

# ── Custom pickers for series with numbered books ───────────────────────────

DOTF_BOOKS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 14, 15, 16]


def fmt_rank(title, itype):
    tl = title.lower()
    fmap = {'m4b': 3, 'mp3': 2, 'm4a': 1} if itype == 'audiobook' else {'epub': 3, 'azw3': 2, 'mobi': 1, 'pdf': 0}
    for fmt, rank in fmap.items():
        if f'/ {fmt}' in tl or f'[{fmt}' in tl or f' {fmt}]' in tl:
            return rank
    return -1


def pick_dotf(results):
    picks = []
    for n in DOTF_BOOKS:
        cands = []
        for r in results:
            t = r.get('title', '').lower()
            if 'defiance of the fall' not in t:
                continue
            if n == 1:
                if re.search(r'defiance of the fall\s+\d', t):
                    continue
            else:
                if not re.search(rf'defiance of the fall\s*0?{n}\b', t):
                    continue
            cands.append(r)
        if not cands:
            print(f'    [DOTF {n}] NOT FOUND')
            continue
        itype = pa.detect_type(cands[0])
        cands.sort(key=lambda r: (fmt_rank(r['title'], itype), r.get('seeders', 0) or 0), reverse=True)
        best = cands[0]
        picks.append(best)
        print(f"    [DOTF {n}] -> {best['title'][:70]} ({best.get('seeders', 0)}s)")
    return picks


def pick_shadow_slave(results):
    picks, seen = [], set()
    for r in results:
        t = r.get('title', '').lower()
        if 'shadow slave' not in t:
            continue
        m = re.search(r'shadow slave\s*0?([1-3])\b', t)
        book = m.group(1) if m else '1'
        itype = pa.detect_type(r)
        key = (book, itype)
        if key in seen:
            continue
        seen.add(key)
        picks.append(r)
        print(f"    [Shadow Slave {book} / {itype}] -> {r['title'][:65]} ({r.get('seeders', 0)}s)")
    return picks


def pick_ultramarines(results):
    picks = []
    vol16 = [r for r in results if 'vol. 1-6' in r.get('title', '').lower() or 'vol 1-6' in r.get('title', '').lower()]
    anth = [r for r in results if 'ultramarines' in r.get('title', '').lower() and r not in vol16]
    if vol16:
        vol16.sort(key=lambda r: r.get('seeders', 0) or 0, reverse=True)
        picks.append(vol16[0])
        print(f"    [Ultramarines vol1-6] -> {vol16[0]['title'][:65]} ({vol16[0].get('seeders', 0)}s)")
    if anth:
        anth.sort(key=lambda r: r.get('seeders', 0) or 0, reverse=True)
        picks.append(anth[0])
        print(f"    [Ultramarines anthology] -> {anth[0]['title'][:65]} ({anth[0].get('seeders', 0)}s)")
    return picks


def pick_top1(results, label, must_contain=None):
    cands = results
    if must_contain:
        cands = [r for r in cands if all(w in r.get('title', '').lower() for w in must_contain)]
    if not cands:
        print(f'    [{label}] NOT FOUND')
        return []
    cands = sorted(cands, key=lambda r: r.get('seeders', 0) or 0, reverse=True)
    best = cands[0]
    print(f"    [{label}] -> {best['title'][:65]} ({best.get('seeders', 0)}s)")
    return [best]


def pick_numbered_series(results, series_words, numbers, label):
    """For each n in numbers, find best ebook AND audiobook match containing
    all series_words plus the number n as a standalone token."""
    picks = []
    for n in numbers:
        for itype in ('ebook', 'audiobook'):
            cands = []
            for r in results:
                t = r.get('title', '').lower()
                if pa.detect_type(r) != itype:
                    continue
                if not all(w in t for w in series_words):
                    continue
                if not re.search(rf'\b0?{n}\b', t):
                    continue
                cands.append(r)
            if not cands:
                print(f'    [{label} {n} / {itype}] not found')
                continue
            cands.sort(key=lambda r: (fmt_rank(r['title'], itype), r.get('seeders', 0) or 0), reverse=True)
            best = cands[0]
            picks.append(best)
            print(f"    [{label} {n} / {itype}] -> {best['title'][:60]} ({best.get('seeders', 0)}s)")
    return picks


def pick_generic(results, target_specs, label):
    """target_specs: list of (match_keywords, types, sublabel)"""
    picks = []
    for match_kw, types, sublabel in target_specs:
        for itype in types:
            cands = [r for r in results
                     if pa.detect_type(r) == itype
                     and all(kw.lower() in r.get('title', '').lower() for kw in match_kw)]
            if not cands:
                print(f'    [{label}/{sublabel} / {itype}] not found')
                continue
            cands.sort(key=lambda r: (fmt_rank(r['title'], itype), r.get('seeders', 0) or 0), reverse=True)
            best = cands[0]
            picks.append(best)
            print(f"    [{label}/{sublabel} / {itype}] -> {best['title'][:55]} ({best.get('seeders', 0)}s)")
    return picks


# ── Query list ────────────────────────────────────────────────────────────────

CARL_BOOKS = [
    ('Dungeon Crawler Carl Doomsday Scenario', 'DCC 2'),
    ('Dungeon Crawler Carl Gate of the Feral Gods', 'DCC 3'),
    ("Dungeon Crawler Carl Dungeon Anarchist's Cookbook", 'DCC 4'),
    ('Dungeon Crawler Carl Eye of the Bedlam Bride', 'DCC 5'),
    ('Dungeon Crawler Carl Spite House', 'DCC 6'),
    ('Dungeon Crawler Carl Iblis Oven', 'DCC 7'),
]

COOKBOOK_QUERIES = [
    ('Salt Fat Acid Heat Samin Nosrat', [([], ['ebook', 'audiobook'], 'Salt Fat Acid Heat')]),
    ('J Kenji Lopez-Alt', [
        (['food lab'], ['ebook', 'audiobook'], 'The Food Lab'),
        (['wok'], ['ebook', 'audiobook'], 'The Wok'),
    ]),
    ('How to Cook Everything Bittman', [([], ['ebook', 'audiobook'], 'How to Cook Everything')]),
    ('Michael Ruhlman', [
        (['ratio'], ['ebook', 'audiobook'], 'Ratio'),
        (['charcuterie'], ['ebook', 'audiobook'], 'Charcuterie'),
    ]),
    ('On Food and Cooking Harold McGee', [([], ['ebook', 'audiobook'], 'On Food and Cooking')]),
    ('Yotam Ottolenghi', [
        (['jerusalem'], ['ebook', 'audiobook'], 'Jerusalem'),
        (['plenty'], ['ebook', 'audiobook'], 'Plenty'),
    ]),
    ('Mastering the Art of French Cooking Julia Child', [([], ['audiobook'], 'audio only - have ebook')]),
    ("The Bread Baker's Apprentice Reinhart", [([], ['ebook', 'audiobook'], "Bread Baker's Apprentice")]),
    ('Tartine Bread Chad Robertson', [([], ['audiobook'], 'audio only - have ebook')]),
    ('The Perfect Loaf Maurizio Leo', [([], ['ebook', 'audiobook'], 'The Perfect Loaf')]),
    ('Sandor Katz Fermentation', [
        (['art of fermentation'], ['ebook', 'audiobook'], 'Art of Fermentation'),
        (['wild fermentation'], ['ebook', 'audiobook'], 'Wild Fermentation'),
    ]),
    ('Ball Complete Book of Home Preserving', [([], ['audiobook'], 'audio only - have ebook')]),
    ('Putting Food By Janet Greene', [([], ['ebook', 'audiobook'], 'Putting Food By')]),
    ('The Complete Book of Butchery Lobaton', [([], ['ebook', 'audiobook'], 'Complete Book of Butchery')]),
    ('The Self-Sufficient Life John Seymour', [([], ['ebook', 'audiobook'], 'Self-Sufficient Life')]),
    ('Encyclopedia of Country Living Carla Emery', [([], ['audiobook'], 'audio only - have ebook')]),
    ('The Urban Homestead Kelly Coyne', [([], ['ebook', 'audiobook'], 'Urban Homestead')]),
    ('Home Cheese Making Ricki Carroll', [([], ['ebook', 'audiobook'], 'Home Cheese Making')]),
    ('Black and Decker Home Repair Plumbing Electrical', [
        (['photo guide to home repair'], ['ebook', 'audiobook'], 'Photo Guide to Home Repair'),
        (['plumbing'], ['ebook', 'audiobook'], 'Guide to Plumbing'),
        (['electrical'], ['ebook', 'audiobook'], 'Guide to Electrical Wiring'),
    ]),
    ('HVAC Fundamentals Sugarman', [([], ['ebook', 'audiobook'], 'HVAC Fundamentals')]),
    ('Home Maintenance For Dummies', [([], ['ebook', 'audiobook'], 'Home Maintenance For Dummies')]),
    ('Plumbing For Dummies Gene Hamilton', [([], ['ebook', 'audiobook'], 'Plumbing For Dummies')]),
    ('Family Handyman Whole House Repair Guide', [([], ['ebook', 'audiobook'], 'Family Handyman Repair Guide')]),
    ('How to Diagnose and Fix Everything Electronic', [([], ['ebook', 'audiobook'], 'Fix Everything Electronic')]),
    ('Appliance Science Hellebuyck', [([], ['ebook', 'audiobook'], 'Appliance Science')]),
    ('Drywall Myron Ferguson', [([], ['ebook', 'audiobook'], 'Drywall')]),
    ('Roofing and Siding Jerry Germer', [([], ['ebook', 'audiobook'], 'Roofing and Siding')]),
    ('Complete Guide to Finishing Basements Philip Schmidt', [([], ['ebook', 'audiobook'], 'Finishing Basements')]),
    ('Chris Marshall Complete Guide', [
        (['bathroom'], ['ebook', 'audiobook'], 'Complete Guide to Bathrooms'),
        (['kitchen'], ['ebook', 'audiobook'], 'Complete Guide to Kitchens'),
    ]),
    ('Your New House Alert Consumers Guide Fields', [([], ['ebook', 'audiobook'], 'Your New House')]),
    ('Complete Manual of Woodworking Albert Jackson', [([], ['ebook', 'audiobook'], 'Manual of Woodworking')]),
    ('Landscape and Yard Care For Dummies', [([], ['ebook', 'audiobook'], 'Landscape and Yard Care')]),
    ('Ian Smiley Distilling', [
        (['rum'], ['ebook', 'audiobook'], "Distiller's Guide to Rum"),
        (['corn whiskey'], ['ebook', 'audiobook'], 'Making Pure Corn Whiskey'),
    ]),
    ('The Compleat Distiller Michael Nixon', [([], ['ebook', 'audiobook'], 'Compleat Distiller')]),
    ('The Craft of Whiskey Distilling Bill Owens', [([], ['ebook', 'audiobook'], 'Craft of Whiskey Distilling')]),
    ('How to Make Whiskey The Easy Way Dowd', [([], ['ebook', 'audiobook'], 'How to Make Whiskey')]),
    ('The Home Distillers Workbook Jeff King', [([], ['ebook', 'audiobook'], "Home Distiller's Workbook")]),
    ('Craft Distilling Victoria Redhed Miller', [([], ['ebook', 'audiobook'], 'Craft Distilling')]),
    ('Complete Guide to Making Wine Beer Vinegar Cider', [([], ['ebook', 'audiobook'], 'Wine Beer Vinegar Cider Guide')]),
    ('Homemade Gin and Vodka Paul Knorr', [([], ['ebook', 'audiobook'], 'Homemade Gin and Vodka')]),
    ('Home Brewers Guide to Vintage Beer Pattinson', [([], ['ebook', 'audiobook'], 'Vintage Beer')]),
    ('True Brews Emma Christensen', [([], ['ebook', 'audiobook'], 'True Brews')]),
    ('The Complete Joy of Homebrewing Papazian', [([], ['ebook', 'audiobook'], 'Joy of Homebrewing')]),
    ('Radical Brewing Randy Mosher', [([], ['ebook', 'audiobook'], 'Radical Brewing')]),
]


def build_queries():
    q = []
    q.append(('Defiance of the Fall', 'audiobook', 'DOTF', pick_dotf, None))
    q.append(('Shadow Slave', 'both', 'Shadow Slave', pick_shadow_slave, None))
    q.append(('Ultramarines', 'ebook', 'Ultramarines', pick_ultramarines, None))
    q.append(('Battletech Blood Legacy', 'audiobook', 'BT Blood Legacy',
              lambda res: pick_top1(res, 'BT Blood Legacy', must_contain=['blood legacy']), None))
    for query, label in CARL_BOOKS:
        q.append((query, 'audiobook', label, lambda res, lbl=label: pick_top1(res, lbl), None))
    q.append(('Rise of the Ranger Echoes Saga Quaintrell', 'both', 'Rise of Ranger',
              lambda res: pick_numbered_series(res, ['ranger'], [3, 4, 5, 6, 7, 8, 9], 'RotR'), None))
    q.append(('Backyard Starship Chaney', 'both', 'Backyard Starship',
              lambda res: pick_numbered_series(res, ['starship'], [2, 3, 4, 5, 6, 7, 8, 9, 10], 'BYS'), None))
    q.append(('Undying Mercenaries', 'both', 'Undying Mercenaries',
              lambda res: pick_generic(res, [
                  (['series'], ['audiobook'], 'Audio series collection'),
                  ([], ['ebook'], 'Ebook series collection'),
              ], 'Undying Mercenaries'), None))
    for query, specs in COOKBOOK_QUERIES:
        label = specs[0][2]
        q.append((query, 'both', label, lambda res, s=specs, l=label: pick_generic(res, s, l), None))
    return q


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--fast', action='store_true', help='short delays, for testing matching only')
    ap.add_argument('--start', type=int, default=0, help='skip first N queries (resume point)')
    ap.add_argument('--count', type=int, default=None, help='only run N queries this run')
    args = ap.parse_args()

    search_delay = 2 if args.fast else SEARCH_DELAY
    add_delay = 1 if args.fast else ADD_DELAY

    sess = pa.qlogin() if not args.dry_run else None
    log = pa.load_log() if not args.dry_run else None

    queries = build_queries()
    end = len(queries) if args.count is None else min(len(queries), args.start + args.count)
    print(f'Total queries available: {len(queries)}  running [{args.start}:{end}]')

    all_picks = []
    for idx in range(args.start, end):
        query, qtype, label, picker, _ = queries[idx]
        cats = (pa.NEWZNAB['audiobook'] if qtype == 'audiobook'
                else pa.NEWZNAB['ebook'] if qtype == 'ebook'
                else pa.NEWZNAB['audiobook'] + pa.NEWZNAB['ebook'])
        print(f'\n=== [{idx}] {label}: {query!r} ===')
        try:
            results = mam_search(query, cats, limit=40)
        except Exception as e:
            print(f'    ERROR: {e}')
            time.sleep(search_delay)
            continue
        print(f'    {len(results)} results')
        picks = picker(results)
        all_picks.extend([(label, p) for p in picks])
        if idx < end - 1:
            time.sleep(search_delay)

    print(f'\n{"=" * 60}')
    print(f'  Total picks: {len(all_picks)}')
    print(f'{"=" * 60}')

    if args.dry_run:
        print('  (dry-run, nothing added)')
        return

    for label, r in all_picks:
        item_type = pa.detect_type(r)
        title = r.get('title', '?')
        if not args.yes:
            yn = input(f"  Add [{label}] {title[:60]} ({item_type})? [Y/n]: ").strip().lower()
            if yn == 'n':
                continue
        try:
            isbn = pa.extract_isbn(title + ' ' + (r.get('description') or ''))
            ll_bid, ll_msg = pa.ll_find_and_add(title, '', item_type, isbn=isbn or '')
            print(f'    LL: {ll_msg}')
            pa._add_one(sess, r, item_type, ll_bid, log)
            print(f'    [OK] added')
        except Exception as e:
            print(f'    [FAIL] {e}')
        time.sleep(add_delay)

    pa.save_log(log)
    print('  Done.')


if __name__ == '__main__':
    main()
