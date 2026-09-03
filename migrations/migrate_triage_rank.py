"""Rank the unreadable unassigned cards to the top for real. Safe to re-run.

Bert used to float them at draw time. Everything else -- the state channel
summary, the `#N` on each card message, the neighbours a drop is measured
against -- still read them in rank order, so the board disagreed with itself
and a drag inside unassigned landed against cards that were not its
neighbours. ensure_card ranks new ones to the top now; this moves the ones
already on the board.

Their relative order is kept, and nothing else in the band moves. No events
are written: this is the board being made to say what it already showed, not
somebody reordering it, and a feed full of migration reorders helps nobody.

    python migrate_triage_rank.py --db ernie.db
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone

RANK_STEP = 1000.0
UNREADABLE = ("none", "loose", "prefix_only")

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="ernie.db")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

con = sqlite3.connect(a.db)
con.row_factory = sqlite3.Row

marks = ",".join("?" * len(UNREADABLE))
triage = con.execute(f"""
    SELECT c.thread_id, c.rank, v.name FROM cards c
    JOIN v_thread_current v ON v.thread_id = c.thread_id
    WHERE c.priority = 'unassigned' AND c.completed_at IS NULL
      AND v.confidence IN ({marks})
      AND COALESCE(TRIM(c.client_override), '') = ''
    ORDER BY c.rank""", UNREADABLE).fetchall()

if not triage:
    print("nothing unreadable in unassigned, nothing to do")
    sys.exit()

ids = [r["thread_id"] for r in triage]
holes = ",".join("?" * len(ids))
floor = con.execute(f"""
    SELECT MIN(rank) AS m FROM cards
    WHERE priority = 'unassigned' AND completed_at IS NULL
      AND thread_id NOT IN ({holes})""", ids).fetchone()["m"]

if floor is None:
    print(f"unassigned holds nothing but the {len(ids)} unreadable card(s), "
          f"already the whole order")
    sys.exit()
if max(r["rank"] for r in triage) < floor:
    print(f"{len(ids)} unreadable card(s) already at the top")
    sys.exit()

now = datetime.now(timezone.utc).isoformat()
for i, r in enumerate(triage):
    new = floor - (len(triage) - i) * RANK_STEP
    print(f"  {r['rank']:>9.0f} -> {new:>9.0f}  {r['name'][:58]}")
    if not a.dry_run:
        con.execute("UPDATE cards SET rank=?, updated_at=? WHERE thread_id=?",
                    (new, now, r["thread_id"]))

if a.dry_run:
    print(f"dry run: {len(triage)} card(s) would move above rank {floor:.0f}")
else:
    con.commit()
    print(f"moved {len(triage)} card(s) to the top of unassigned")
con.close()
