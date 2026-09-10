"""Add sent_steps to events and new_threads. Safe to re-run.

Which irreversible Discord writes a row has already had. Posting an event
takes up to four writes and creating a ticket takes three, and only the last
one was ever recorded -- so a failure anywhere threw away the record of
everything before it and the retry did it all again. Measured: an archive
that failed twice put three identical "marked this complete" messages into
one customer thread, and an opening message that failed twice left three real
threads in the customer channel for one ticket.

Existing rows are left NULL, which reads as "nothing done yet". That is the
right answer for anything still waiting: those rows have not been through a
partial failure this build could have recorded, and a row already posted is
never looked at again.
"""
import argparse, sqlite3

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()

con = sqlite3.connect(a.db)
for table in ("events", "new_threads"):
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    if "sent_steps" in cols:
        print(f"{table}: already migrated")
    else:
        con.execute(f"ALTER TABLE {table} ADD COLUMN sent_steps TEXT")
        print(f"{table}: added sent_steps")
con.commit()
con.close()
