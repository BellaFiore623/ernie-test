"""Give a ticket with no thread yet a real place in its band. Safe to re-run.

rank is the order and the only one, and a draft is on the board from the
moment the + is pressed -- but it had no rank, so it was sent at 0.0 and sat
wherever that happened to fall among the band's real ranks. Dragging it
answered "no such card", because a move looks up a cards row and a draft has
none.

Existing drafts are backfilled to the top of their band, which is where the
board has been drawing them.
"""
import argparse, sqlite3

ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(new_threads)")}
if "rank" in cols:
    print("already migrated")
else:
    con.execute("ALTER TABLE new_threads ADD COLUMN rank REAL")
    # One step above the band's lowest rank, the same edge make_threads used
    # to compute for itself. Ordered by created_at so two drafts in one band
    # keep the order they were started in rather than tying.
    step = 1000.0
    done = 0
    for band, in con.execute(
            "SELECT DISTINCT priority FROM new_threads WHERE posted_at IS NULL"):
        edge = con.execute("SELECT MIN(rank) FROM cards WHERE priority=?",
                           (band,)).fetchone()[0]
        top = (step if edge is None else edge)
        for n, (draft,) in enumerate(con.execute(
                "SELECT draft_id FROM new_threads WHERE posted_at IS NULL "
                "AND priority=? ORDER BY created_at DESC", (band,)), 1):
            con.execute("UPDATE new_threads SET rank=? WHERE draft_id=?",
                        (top - n * step, draft))
            done += 1
    con.commit()
    print(f"added rank; placed {done} waiting draft(s)")
con.close()
