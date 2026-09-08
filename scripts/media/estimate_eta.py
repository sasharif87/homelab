#!/usr/bin/env python3
"""Estimate remaining ingest time from real libzim article_count + caps + state.
Read-only: opens ZIMs read-only, reads state. Does not touch the running job."""
import importlib.util, sys, glob
from pathlib import Path
spec = importlib.util.spec_from_file_location("ir", "/opt/scripts/ingest-rag.py")
m = importlib.util.module_from_spec(spec); sys.modules["ir"] = m; spec.loader.exec_module(m)
from libzim.reader import Archive

state = m.load_state()
done = set()
for n, c in state["collections"].items():
    done |= set(c.get("zims_done", []))

patterns = ("fas-military-medicine librepathology libretexts_org_en_med nhs_uk ted_mul_medicine "
            "wikipedia_en_medicine zimgit-medicine gutenberg_en_lcc-r biology.stackexchange "
            "chemistry.stackexchange datascience electronics.stackexchange freecodecamp "
            "gutenberg_en_lcc-q gutenberg_en_lcc-t ham.stackexchange ifixit physics.stackexchange "
            "restarters serverfault stackoverflow unix.stackexchange").split()

def cap_for(stem):
    for col in m.COLLECTIONS:
        for pat, c in col.get("caps", {}).items():
            if pat in stem:
                return c
    return 0

zdir = Path("/mnt/storage/knowledge/kiwix")
sel = [z for z in sorted(zdir.glob("*.zim")) if any(p in z.stem for p in patterns)]

RATE = 96.0
print(f"{'ZIM':46s} {'art_cnt':>9s} {'cap':>7s} {'eff':>8s} done")
rem = 0
for z in sel:
    stem = z.stem
    ac = Archive(str(z)).article_count
    cap = cap_for(stem)
    eff = min(ac, cap) if cap else ac
    isdone = stem in done
    print(f"{stem[:46]:46s} {ac:>9d} {str(cap or '-'):>7s} {eff:>8d} {'YES' if isdone else ''}")
    if not isdone:
        rem += eff

print(f"\nRemaining effective articles (not yet done): {rem:,}")
for cpa in (3.0, 3.5, 4.5):
    ch = rem * cpa
    print(f"  @ {cpa} chunks/art -> {ch:,.0f} chunks -> {ch/RATE/3600:.1f} h embed @ {RATE}/s")
print("(wikipedia counts full 100k here but is ~60% embedded already, so real is a bit less)")
