"""Add threads.archived_by_ernie to an existing database. Safe to re-run."""
import argparse, sqlite3
ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(threads)")}
if "archived_by_ernie" in cols:
    print("already migrated")
else:
    con.execute("ALTER TABLE threads ADD COLUMN archived_by_ernie "
                "INTEGER NOT NULL DEFAULT 0")
    con.commit()
    print("added threads.archived_by_ernie")
con.close()
