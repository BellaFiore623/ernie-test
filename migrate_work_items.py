"""Create work_items and seed it from the four fields it replaces.

Each card's action_item, build_state, return_state and direction become
bubbles, in that order, so nothing that was on a card before disappears from
it. The columns themselves are left alone -- undo still reads them.

Safe to re-run: a card that already has any work_items row is skipped, so a
second run can't double the bubbles.

    python migrate_work_items.py --db ernie-test.db
"""
import argparse
import sqlite3
import uuid
from datetime import datetime, timezone

STATE_LABEL = {"needs_created": "needs created", "created": "created",
               "not_needed": "not needed"}
DIR_LABEL = {"leaving": "equipment leaving", "coming_back": "equipment coming back"}

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()

con = sqlite3.connect(a.db)
con.row_factory = sqlite3.Row
con.execute("""
    CREATE TABLE IF NOT EXISTS work_items (
        item_id    TEXT PRIMARY KEY,
        thread_id  TEXT NOT NULL REFERENCES threads(thread_id),
        body       TEXT NOT NULL,
        position   REAL NOT NULL,
        created_at TEXT NOT NULL,
        created_by TEXT,
        done_at    TEXT,
        done_by    TEXT,
        removed_at TEXT,
        removed_by TEXT
    )""")
con.execute("CREATE INDEX IF NOT EXISTS ix_work_thread "
            "ON work_items(thread_id, position)")

now = datetime.now(timezone.utc).isoformat()
seeded = skipped = 0

for c in con.execute("SELECT * FROM cards").fetchall():
    tid = c["thread_id"]
    if con.execute("SELECT 1 FROM work_items WHERE thread_id=? LIMIT 1",
                   (tid,)).fetchone():
        skipped += 1
        continue

    bubbles = []
    if (c["action_item"] or "").strip():
        bubbles.append(c["action_item"].strip())
    # "needs created" is the default every card is born with, so carrying it
    # across would put two bubbles on every untouched card that say nothing.
    for kind, col in (("build ticket", "build_state"),
                      ("return ticket", "return_state")):
        v = c[col] or ""
        if v and v != "needs_created":
            bubbles.append(f"{kind} {STATE_LABEL.get(v, v)}")
    if c["direction"] in DIR_LABEL:
        bubbles.append(DIR_LABEL[c["direction"]])

    for i, body in enumerate(bubbles, start=1):
        con.execute(
            """INSERT INTO work_items (item_id, thread_id, body, position,
                                       created_at, created_by)
               VALUES (?,?,?,?,?,?)""",
            (str(uuid.uuid4()), tid, body, float(i), now, None))
    if bubbles:
        seeded += 1

con.commit()
print(f"work_items ready -- seeded {seeded} card(s), "
      f"skipped {skipped} that already had items")
con.close()
