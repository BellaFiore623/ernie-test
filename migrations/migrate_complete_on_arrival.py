"""Let a ticket be closed before Discord has it. Safe to re-run.

Completing looks up a cards row, and a ticket still waiting in new_threads
has none -- so it answered "no such card". The flag records the intent and
make_threads acts on it once the thread exists.
"""
import argparse, sqlite3

ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(new_threads)")}
added = []
if "complete_on_arrival" not in cols:
    con.execute("ALTER TABLE new_threads ADD COLUMN complete_on_arrival "
                "INTEGER NOT NULL DEFAULT 0")
    added.append("complete_on_arrival")
if "completed_by" not in cols:
    con.execute("ALTER TABLE new_threads ADD COLUMN completed_by TEXT")
    added.append("completed_by")
con.commit()
print("added " + ", ".join(added) if added else "already migrated")
con.close()
