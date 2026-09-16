"""
Re-derive the parsed columns of title rows the parser now reads differently.

`record_title` is the one place that decides what a title row holds, and it
runs when a *name* changes. So a fix to `parse_title` reaches new rows and
nothing else: every revision already stored keeps the queue, client and date
the old parser gave it, and the board goes on showing them. That is usually
invisible and occasionally wrong -- `PROD: 29Jun26 - Trade show TOF` was
stored as client "2", date the 9th of June, and stayed that way after the
parser learned better.

    python tools/reparse_titles.py --db ernie-test.db --dry-run
    python tools/reparse_titles.py --db ernie-test.db

The name is never touched, only the columns derived from it, and only where
the new parse actually differs. No network, and nothing here reads Discord.

Run it after any change to `parse_title`, against each database, and expect
it to find nothing most of the time: the fix this was written for changed 1
row of 193 in the sandbox and 0 of 1,139 in production.
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ernie_extract as ex           # noqa: E402

FIELDS = ("queue", "client_raw", "client_key", "thread_date", "summary",
          "confidence")


def derived(name: str) -> tuple:
    """Exactly what `record_title` would store for this name."""
    t = ex.parse_title(name or "")
    return (t.queue, t.client_raw,
            ex.normalise_client(t.client_raw or "") or None,
            t.date.isoformat() if t.date else None, t.summary, t.confidence)


def stale(con) -> list:
    out = []
    for r in con.execute("""SELECT thread_id, observed_at, name, queue, client_raw,
                                   client_key, thread_date, summary, confidence
                            FROM thread_titles ORDER BY observed_at"""):
        was = tuple(r[f] for f in FIELDS)
        now = derived(r["name"])
        if was != now:
            out.append((r, was, now))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie-test.db")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    rows = stale(con)

    total = con.execute("SELECT COUNT(*) n FROM thread_titles").fetchone()["n"]
    print(f"{pathlib.Path(a.db).resolve()}")
    print(f"  {total} title rows, {len(rows)} the parser now reads differently\n")

    for r, was, now in rows:
        print(f"  {r['observed_at'][:19]}  {r['name']}")
        for f, x, y in zip(FIELDS, was, now):
            if x != y:
                print(f"     {f:<12} {x!r}  ->  {y!r}")
        if not a.dry_run:
            con.execute(
                """UPDATE thread_titles
                      SET queue=?, client_raw=?, client_key=?, thread_date=?,
                          summary=?, confidence=?
                    WHERE thread_id=? AND observed_at=?""",
                (*now, r["thread_id"], r["observed_at"]))
        print()

    if a.dry_run:
        print("dry run: nothing written")
    else:
        con.commit()
        print(f"{len(rows)} row(s) re-derived")
    con.close()


if __name__ == "__main__":
    main()
