"""Add the thread_status table. Safe to re-run.

schema.sql creates it and connect() applies schema.sql on every open, so a
database opened through ernie_load already has it. This is for one that is
only ever opened directly -- and for the record of when it arrived.

Nothing is backfilled. A row appears the first time Ernie posts a status
message into a thread it watched open; its absence means there is none, which
is the right answer for every thread that existed before this.
"""
import argparse, pathlib, sqlite3

ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
have = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                   "AND name='thread_status'").fetchone()
if have:
    print("already migrated")
else:
    sql = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"
    body = sql.read_text(encoding="utf-8")
    start = body.index("CREATE TABLE IF NOT EXISTS thread_status")
    end = body.index(";", start) + 1
    con.executescript(body[start:end])
    con.commit(); print("added thread_status")
con.close()
