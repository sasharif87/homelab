#!/usr/bin/env python3
"""Fix LL book statuses: reset wrong-matched Open books, run forceProcess on Snatched."""
import sqlite3
import requests

# ── Fix wrong-matched Open books ─────────────────────────────────────────────
conn = sqlite3.connect("/config/lazylibrarian.db")
cur = conn.cursor()

cur.execute("SELECT BookID, BookName, BookFile, Status FROM books WHERE BookID IN (11160086, 247984207)")
print("BEFORE (wrong-matched Open books):")
for row in cur.fetchall():
    print(f"  {row[3]:8s} | {row[1][:45]} | {row[2]}")

cur.execute("UPDATE books SET Status='Wanted', BookFile=NULL, BookLibrary=NULL WHERE BookID IN (11160086, 247984207)")
conn.commit()

cur.execute("SELECT BookID, BookName, BookFile, Status FROM books WHERE BookID IN (11160086, 247984207)")
print("AFTER:")
for row in cur.fetchall():
    print(f"  {row[3]:8s} | {row[1][:45]} | {row[2]}")
conn.close()
print()

# ── Run forceProcess on download dirs ────────────────────────────────────────
LL_BASE = "http://localhost:5299/api?apikey=2833a671925b43089e2002226e17fd4e"

for path in ["/downloads", "/downloads/books"]:
    url = f"{LL_BASE}&cmd=forceProcess&dir={path}"
    try:
        r = requests.get(url, timeout=30)
        print(f"forceProcess {path}: {r.status_code} {r.text[:120]}")
    except Exception as e:
        print(f"forceProcess {path}: ERROR {e}")
