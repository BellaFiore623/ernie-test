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


def read_env(path: str) -> dict:
    """KEY=VALUE out of an env file, by real path, or say so and stop.

    `ernie_sync.load_env` resolves against *its own* directory and returns
    **silently** when the file is not there, which is the wrong shape for
    this: production's env need not live in the checkout, and it must not be
    copied into one -- two copies of a token is how the two sets got mixed up
    in the first place. A missing file here reported itself as "no
    DISCORD_TOKEN", which sends the reader looking inside a file that does
    not exist.
    """
    p = pathlib.Path(path).expanduser()
    for cand in (p, pathlib.Path.cwd() / p,
                 pathlib.Path(__file__).resolve().parent.parent / p):
        if cand.is_file():
            out = {}
            for line in cand.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
            out["__path__"] = str(cand)
            return out
    sys.exit(f"no such env file: {path}")


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

    env = read_env(a.env)
    token = env.get("DISCORD_TOKEN")
    guild = env.get("DISCORD_GUILD_ID")
    if not token or not guild:
        sys.exit(f"{env['__path__']} has no DISCORD_TOKEN / DISCORD_GUILD_ID")

    # This tool has no reason to hold a key that opens the door. An env file
    # granting writes is a sandbox one, and a sandbox token against a
    # production database would read the wrong server's history into it.
    if env.get("ALLOW_DISCORD_WRITES"):
        sys.exit(f"{env['__path__']} carries ALLOW_DISCORD_WRITES. This reads "
                 f"and never writes, so it will not run with an env file that "
                 f"grants writes -- point it at production's, which has no "
                 f"such line.")

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row

    # The failure this exists to catch: the right token against the wrong
    # database. Both halves are read out and compared before a single message
    # is fetched, because the tool writes into rows keyed by message id and a
    # mismatched pair would be filling in one server's mirror from another
    # server's history -- silently, since every id would simply miss.
    theirs = con.execute("""SELECT guild_id, COUNT(*) n FROM threads
                            GROUP BY guild_id ORDER BY n DESC""").fetchall()
    known = {r["guild_id"] for r in theirs if r["guild_id"]}
    if known and guild not in known:
        con.close()
        sys.exit(f"refusing: {a.env} names guild {guild}, and every thread in "
                 f"{a.db} belongs to {', '.join(sorted(known))}. One of the "
                 f"two is the wrong one.")

    # `allow_writes_for` is left unset, so `Discord.write()` refuses whatever
    # any env file says -- and nothing in this file calls it. Asserted rather
    # than assumed, because it is the one property that must hold.
    d = S.Discord(token, guild)
    assert not d.writes_allowed, "writes must be impossible here"

    who = d.whoami()
    print(f"bot {who['bot']!r} on {who['guild']!r} ({who['guild_id']}), "
          f"writes={who['writes']}")
    print(f"env {env['__path__']}")
    print(f"db  {pathlib.Path(a.db).resolve()}  "
          f"({sum(r['n'] for r in theirs)} threads, all in "
          f"{', '.join(sorted(known)) or 'no guild'})\n")
    if who["writes"]:
        sys.exit("refusing: this client reports writes allowed")

    try:
        backfill(con, d, a.limit or None, a.dry_run)
    except KeyboardInterrupt:
        print("\ninterrupted -- run it again to carry on where it stopped")
    finally:
        con.close()


if __name__ == "__main__":
    main()
