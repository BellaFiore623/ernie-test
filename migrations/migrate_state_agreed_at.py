"""Add state_sync.agreed_at. Safe to re-run.

synced_at was doing two jobs and only answering one of them. The publish
stamps it for every card every cycle, so "seconds since agreed" measured
whether the outbox was alive, not whether the other board was reachable --
Bert reported "in step" with the sync loop stopped. agreed_at is written
only by the pull, which is the direction their changes arrive in.

Existing rows are left NULL rather than backfilled from synced_at: nothing
here knows when this machine last actually reconciled, and inventing a
recent one would re-tell the same lie once. The next reconcile fills it.
"""
import argparse, sqlite3
ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(state_sync)")}
if not cols:
    print("no state_sync table -- this database has never shared a board")
elif "agreed_at" in cols:
    print("already migrated")
else:
    con.execute("ALTER TABLE state_sync ADD COLUMN agreed_at TEXT")
    con.commit(); print("added state_sync.agreed_at")
con.close()
