"""
Fill in `messages.type` on rows written before the column existed.

Discord's message type is not derivable from anything in the mirror -- it can
only be fetched again. Every message ingested before the column was added has
none, which is 22,810 rows in production, and among them are the only exact
records of a thread ever being retagged: **type 4, CHANNEL_NAME_CHANGE**,
carrying the new name as its content. Without the type a rename cannot be
told from somebody pasting a title into the chat, and 573 of production's
messages have content that parses as a title with 550 written by people.

`rescan_edits` fills it in on its own for threads active in the last
`RESCAN_DAYS`. This is for everything older, which is most of the board.

    python tools/backfill_message_types.py --env ernie.env --db ernie.db
    python tools/backfill_message_types.py --env ernie-test.env \
        --db ernie-test.db --limit 5        # a short run, to watch it work

**It writes one column and nothing else.** Not through `load_messages`, which
also inserts messages, opens revisions and moves `threads.last_seen_message_id`
-- paging *backwards* through history would hand that last one an old id and
send the forward sync back over ground it had already covered. The only
statement here is `UPDATE messages SET type=? WHERE message_id=? AND type IS
NULL`, so a message this finds and the mirror has never seen is left alone:
filling in the mirror is a different job from filling in a column, and doing
both at once on 889 threads is not a thing to start on a production database.

Read-only against Discord -- every request is a GET, and nothing here can
reach `Discord.write()`.

Resumable. Its own bookkeeping lives in `backfill_message_types`, a table it
creates and nothing else reads; drop it when the run is done. Threads are
marked finished rather than re-derived from what is still NULL, because a
message Discord no longer returns -- deleted, or in a thread that has gone --
stays NULL for ever and would otherwise make its thread eligible on every run.

Cost, measured against production: 889 threads and 910 pages of 100, on a
route that allows 5 requests per 5 seconds. About 15 minutes, and it can be
interrupted and picked up again.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sqlite3
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import ernie_sync as S           # noqa: E402  (after the path insert)


PROGRESS = """
CREATE TABLE IF NOT EXISTS backfill_message_types (
    thread_id  TEXT PRIMARY KEY,
    done_at    TEXT NOT NULL,
    pages      INTEGER NOT NULL,
    filled     INTEGER NOT NULL,
    seen       INTEGER NOT NULL
)
"""


def thread_history(d: S.Discord, tid: str):
    """Every message in a thread, newest first, paged with `before`.

    `messages_after` exists and walks forward from a cursor; this walks the
    whole history backwards, which is the shape a full re-read wants and
    needs no starting point.
    """
    before = None
    while True:
        p = {"limit": 100}
        if before:
            p["before"] = before
        page = d.get(f"/channels/{tid}/messages", **p)
        if not page:
            return
        yield page
        if len(page) < 100:
            return
        before = page[-1]["id"]


def backfill(con: sqlite3.Connection, d: S.Discord, limit: int | None,
             dry_run: bool) -> None:
    con.execute(PROGRESS)
    con.commit()

    rows = con.execute(
        """SELECT t.thread_id, COUNT(*) AS blank
             FROM threads t
             JOIN messages m ON m.thread_id = t.thread_id
            WHERE m.type IS NULL
              AND t.thread_id NOT IN (SELECT thread_id
                                        FROM backfill_message_types)
            GROUP BY t.thread_id
            ORDER BY t.thread_id""").fetchall()
    if limit:
        rows = rows[:limit]

    total_blank = sum(r["blank"] for r in rows)
    print(f"{len(rows)} thread(s) to read, {total_blank} message(s) with no "
          f"type yet")
    if dry_run:
        print("--dry-run: stopping before the first request")
        return

    started = time.time()
    pages = filled = seen = 0
    kinds: dict[int, int] = {}

    for i, r in enumerate(rows, 1):
        tid = r["thread_id"]
        t_pages = t_filled = t_seen = 0
        try:
            for page in thread_history(d, tid):
                t_pages += 1
                for m in page:
                    t_seen += 1
                    kind = m.get("type")
                    if kind is None:
                        continue
                    kinds[kind] = kinds.get(kind, 0) + 1
                    cur = con.execute(
                        "UPDATE messages SET type=? "
                        "WHERE message_id=? AND type IS NULL",
                        (kind, m["id"]))
                    t_filled += cur.rowcount
        except Exception as e:                       # noqa: BLE001
            # One thread failing is not the run failing. A thread that has
            # been deleted, or that the bot has lost sight of, answers 404 --
            # which `Discord.get` already turns into None -- but a channel
            # type that refuses the endpoint answers 400, and there is no
            # reason for that to cost the other 888.
            print(f"  [{i}/{len(rows)}] {tid}: {type(e).__name__}: {e}")
            con.rollback()
            continue

        # Per thread, so an interruption keeps what it has. Short
        # transactions, because the sync writes to this database too.
        con.execute(
            """INSERT OR REPLACE INTO backfill_message_types
               (thread_id, done_at, pages, filled, seen)
               VALUES (?, datetime('now'), ?, ?, ?)""",
            (tid, t_pages, t_filled, t_seen))
        con.commit()

        pages += t_pages
        filled += t_filled
        seen += t_seen
        if i % 25 == 0 or i == len(rows):
            per = (time.time() - started) / i
            left = int(per * (len(rows) - i))
            print(f"  [{i}/{len(rows)}] {pages} pages, {filled} filled, "
                  f"~{left // 60}m{left % 60:02d}s left")

    took = int(time.time() - started)
    print(f"\nread {pages} page(s) over {len(rows)} thread(s) in "
          f"{took // 60}m{took % 60:02d}s")
    print(f"{seen} message(s) seen, {filled} type(s) filled in")
    if kinds:
        print("types found: " + ", ".join(
            f"{k}={n}" for k, n in sorted(kinds.items())))
        if 4 in kinds:
            print(f"  -- {kinds[4]} of them are renames (type 4)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--env", default="ernie.env")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many threads")
    ap.add_argument("--dry-run", action="store_true",
                    help="say what would be read, make no request")
    a = ap.parse_args()

    S.load_env(a.env)
    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("DISCORD_GUILD_ID")
    if not token or not guild:
        sys.exit(f"{a.env} has no DISCORD_TOKEN / DISCORD_GUILD_ID")

    # No writes are possible: `allow_writes_for` is left unset, so
    # `Discord.write()` refuses whatever the env file says.
    d = S.Discord(token, guild)

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    try:
        backfill(con, d, a.limit or None, a.dry_run)
    except KeyboardInterrupt:
        print("\ninterrupted -- run it again to carry on where it stopped")
    finally:
        con.close()


if __name__ == "__main__":
    main()
