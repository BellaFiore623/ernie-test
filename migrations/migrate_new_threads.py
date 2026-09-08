"""Add the new_threads table. Safe to re-run.

schema.sql creates it, and connect() applies schema.sql on every open, so a
database opened through ernie_load already has it. This is for one that is
only ever opened directly -- and for the record of when it arrived.
"""
import argparse, pathlib, sqlite3

ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
have = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                   "AND name='new_threads'").fetchone()
if have:
    print("already migrated")
else:
    sql = pathlib.Path(__file__).resolve().parent.parent / "schema.sql"
    body = sql.read_text(encoding="utf-8")
    start = body.index("CREATE TABLE IF NOT EXISTS new_threads")
    end = body.index("ix_new_threads_due", start)
    end = body.index(";", end) + 1
    con.executescript(body[start:end])
    con.commit(); print("added new_threads")
con.close()
