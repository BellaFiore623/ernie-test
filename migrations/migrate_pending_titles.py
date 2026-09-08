"""Re-read the title rows the outbox wrote without parsing them. Safe to re-run.

make_threads used to write a created thread's title row with the name and
nothing else, stamping confidence='pending'. So queue and client_raw stayed
NULL and the card came up grey with "unknown client" for a title that reads
perfectly well -- and it stayed that way, because the sync only writes a new
revision when the *name* changes and the name never did.

'pending' is not one of the confidences the schema allows, so it is an exact
marker: only that bug wrote it, and this cannot touch anything else. The rows
are corrected in place rather than appended to -- a revision recording what we
failed to read is not an observation worth keeping.
"""
import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ernie_extract as ex     # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="ernie.db")
ap.add_argument("--dry-run", action="store_true")
a = ap.parse_args()

con = sqlite3.connect(a.db)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT thread_id, observed_at, name FROM thread_titles "
    "WHERE confidence='pending' ORDER BY observed_at").fetchall()

if not rows:
    print("nothing to do")
else:
    for r in rows:
        t = ex.parse_title(r["name"] or "")
        print(f"  {r['thread_id']}  {r['name']!r}\n"
              f"    -> queue={t.queue!r} client={t.client_raw!r} "
              f"confidence={t.confidence!r}")
        if not a.dry_run:
            con.execute(
                """UPDATE thread_titles
                      SET queue=?, client_raw=?, client_key=?, thread_date=?,
                          summary=?, confidence=?
                    WHERE thread_id=? AND observed_at=?""",
                (t.queue, t.client_raw,
                 ex.normalise_client(t.client_raw or "") or None,
                 t.date.isoformat() if t.date else None,
                 t.summary, t.confidence, r["thread_id"], r["observed_at"]))
    if a.dry_run:
        print(f"dry run: {len(rows)} row(s) would be re-read")
    else:
        con.commit()
        print(f"re-read {len(rows)} title row(s)")
con.close()
