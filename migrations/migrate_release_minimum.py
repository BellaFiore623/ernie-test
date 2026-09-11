"""Add release_seen.minimum. Safe to re-run."""
import argparse, sqlite3
ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(release_seen)")}
if not cols:
    print("no release_seen table -- schema.sql creates it on the next open")
elif "minimum" in cols:
    print("already migrated")
else:
    con.execute("ALTER TABLE release_seen ADD COLUMN minimum TEXT NOT NULL DEFAULT ''")
    con.commit(); print("added release_seen.minimum")
con.close()
