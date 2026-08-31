"""Add cards.client_override. Safe to re-run."""
import argparse, sqlite3
ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(cards)")}
if "client_override" in cols:
    print("already migrated")
else:
    con.execute("ALTER TABLE cards ADD COLUMN client_override TEXT")
    con.commit(); print("added cards.client_override")
con.close()
