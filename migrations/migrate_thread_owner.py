"""Add threads.owner_id and messages.author_display. Safe to re-run.

The activity feed can now say who opened a thread, which needs two things
Discord was already sending and the mirror was dropping: the thread's
owner_id, and the author's global_name -- "Tyler" rather than "tyler_mazza".

Neither is backfilled. owner_id arrives on the thread object every cycle, so
live threads fill themselves in on the next sync; archived ones stay NULL and
their cards were created long before any of this, so nothing would read it.
author_display only arrives with a message, and messages are inserted once and
never rewritten, so old rows keep the username the feed falls back to anyway.

    python migrate_thread_owner.py --db ernie-test.db
"""
import argparse
import sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()

con = sqlite3.connect(a.db)
added = []
for table, col in (("threads", "owner_id"), ("messages", "author_display")):
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if not cols:
        print(f"no {table} table -- is this an Ernie database?")
        continue
    if col in cols:
        print(f"{table}.{col} already there")
        continue
    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
    added.append(f"{table}.{col}")

con.commit()
print(f"added {', '.join(added)}" if added else "nothing to do")
con.close()
