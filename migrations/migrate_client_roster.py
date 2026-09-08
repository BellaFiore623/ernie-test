"""Add clients.short_name and clients.offered. Safe to re-run.

schema.sql creates the table with these columns, but CREATE TABLE IF NOT
EXISTS leaves an existing one alone -- so a database that already has the
(previously unused) clients table needs them added here.
"""
import argparse, sqlite3

ap = argparse.ArgumentParser(); ap.add_argument("--db", default="ernie.db")
a = ap.parse_args()
con = sqlite3.connect(a.db)
cols = {r[1] for r in con.execute("PRAGMA table_info(clients)")}
added = []
if "short_name" not in cols:
    con.execute("ALTER TABLE clients ADD COLUMN short_name TEXT")
    added.append("short_name")
if "offered" not in cols:
    con.execute("ALTER TABLE clients ADD COLUMN offered INTEGER NOT NULL DEFAULT 1")
    added.append("offered")
con.commit()
print("added clients." + ", clients.".join(added) if added else "already migrated")
con.close()
