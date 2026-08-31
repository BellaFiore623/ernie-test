"""Add events.claimed_at and refresh v_outbox_due. Safe to re-run."""
import argparse, sqlite3
ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(events)")}
if "claimed_at" in cols:
    print("already migrated")
else:
    con.execute("ALTER TABLE events ADD COLUMN claimed_at TEXT")
    print("added events.claimed_at")
# CREATE VIEW IF NOT EXISTS never updates an existing view, so drop it and let
# connect() rebuild it from schema.sql with the claimed_at guard.
con.execute("DROP VIEW IF EXISTS v_outbox_due")
con.commit(); con.close()
print("dropped v_outbox_due; connect() will recreate it")
