"""
Put names on closures that were recorded without one.

`reconcile_closures` closes a card when Discord says its thread is archived,
and asks the audit log who did it. That question needs **View Audit Log** on
the bot's role, which is a per-server toggle, and the symptom of it missing
is not an error: `who_archived` turns the 403 into `None`, the closure is
recorded unattributed, and the feed says "closed in Discord" and names
nobody, for ever. Naming somebody is a nicety and closing the ticket is the
feature -- so the pass is deliberately built to carry on without it.

"For ever" is the part that turned out not to be true. Discord keeps audit
log entries for 45 days, so a closure recorded before the permission was
granted can still be attributed afterwards, as long as somebody asks inside
that window. This is that ask.

    python tools/backfill_closers.py --env ernie.env --db ernie.db --dry-run
    python tools/backfill_closers.py --env ernie.env --db ernie.db

Found on production's first sync: 18 days of archiving had happened while
Ernie was not watching, the pass closed 20 cards in one go, and every one of
them was written before the permission existed -- 19 by one person and 1 by
another, all recoverable four pages into the log.

**It fills two NULLs and nothing else.** `events.actor_name` and
`cards.completed_by`, and only where they are already NULL: a name somebody
put there by hand is a decision, and this has no business overruling one.
`events.new_value` is untouched, because it is `CLOSED_IN_DISCORD` whether or
not there is a name -- it says *where* the closure happened, and that was
never in doubt. Nothing here reopens a card, moves a rank or writes an event.

**Our own bot is skipped, the way `who_archived` skips it.** Ernie archives a
thread when Complete is pressed in Bert, and that path never reaches a
closure row at all -- but "Ernie closed it" is the one attribution worth
making impossible rather than merely unlikely, so an entry naming us leaves
the row NULL and is reported as ours.

Read-only against Discord: every request is a GET, and nothing here can reach
`Discord.write()`. Re-runnable -- a row it has already filled is no longer
NULL, so a second run finds nothing to do.
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ernie_sync as S                          # noqa: E402
from backfill_message_types import read_env     # noqa: E402

# One copy of read_env, not two: the reasoning for why `ernie_sync.load_env`
# is the wrong shape for a tool pointed at production's env file is written
# down beside it there, and a second copy would be a second place to fix.

PAGE = 100          # audit log entries per request, Discord's maximum
MAX_PAGES = 20      # 2,000 entries back; past that the closure is out of reach


def unattributed(con) -> list:
    """Closures recorded with no name, newest first."""
    return con.execute(
        """SELECT event_id, thread_id, occurred_at FROM events
            WHERE verb = 'completed'
              AND new_value = ?
              AND actor_name IS NULL
            ORDER BY occurred_at DESC""", (S.CLOSED_IN_DISCORD,)).fetchall()


def archivers(d: S.Discord, guild: str, wanted: set, pages: int = MAX_PAGES):
    """Page back through the audit log for who archived each of these.

    `who_archived` asks once, with `AUDIT_LOOKBACK` entries, because it runs
    on every pass and is attributing something that just happened. This is
    catching up on a backlog, so it pages -- and stops the moment every
    thread it was asked about has an answer, which on a quiet guild is the
    first page and on a busy one is however many it takes.

    Returns (found, ours, pages_read): `found` maps thread to name, `ours`
    is the set our own bot archived, and a thread in neither was not in the
    log at all.
    """
    me = (d.get("/users/@me") or {}).get("id")
    found: dict[str, str] = {}
    ours: set[str] = set()
    before, read = None, 0
    while wanted - set(found) - ours and read < pages:
        params = {"action_type": S.THREAD_UPDATE, "limit": PAGE}
        if before:
            params["before"] = before
        r = d.get(f"/guilds/{guild}/audit-logs", **params)
        if not r:
            break
        entries = r.get("audit_log_entries") or []
        if not entries:
            break
        read += 1
        users = {u["id"]: u for u in (r.get("users") or [])}
        for e in entries:
            tid = e.get("target_id")
            if tid not in wanted or tid in found or tid in ours:
                continue        # newest first, so the first answer is the one
            if not any(c.get("key") == "archived" and c.get("new_value")
                       for c in (e.get("changes") or [])):
                continue        # a thread update that was not an archiving
            if e.get("user_id") == me:
                ours.add(tid)
                continue
            u = users.get(e.get("user_id")) or {}
            # global_name over username, the preference the `started` line
            # and `who_archived` both use: "Tyler" rather than "tyler_mazza".
            name = u.get("global_name") or u.get("username")
            if name:
                found[tid] = name
        before = entries[-1]["id"]
    return found, ours, read


def apply(con, rows: list, found: dict) -> int:
    """Write the names on. Only ever onto a NULL."""
    filled = 0
    for r in rows:
        name = found.get(r["thread_id"])
        if not name:
            continue
        con.execute(
            "UPDATE events SET actor_name=? WHERE event_id=? "
            "AND actor_name IS NULL", (name, r["event_id"]))
        con.execute(
            "UPDATE cards SET completed_by=?, updated_at=? WHERE thread_id=? "
            "AND completed_by IS NULL", (name, S.now(), r["thread_id"]))
        filled += 1
    return filled


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--env", default="ernie.env")
    ap.add_argument("--pages", type=int, default=MAX_PAGES,
                    help=f"audit log pages of {PAGE} to read back")
    ap.add_argument("--dry-run", action="store_true",
                    help="write, count, and roll back")
    a = ap.parse_args()

    env = read_env(a.env)
    token = env.get("DISCORD_TOKEN")
    guild = env.get("DISCORD_GUILD_ID")
    if not token or not guild:
        sys.exit(f"{env['__path__']} has no DISCORD_TOKEN / DISCORD_GUILD_ID")

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row

    # The right token against the wrong database: both halves are read out
    # and compared before a request is made, because every row written here
    # is keyed by a thread id and a mismatched pair would simply miss.
    theirs = con.execute("""SELECT guild_id, COUNT(*) n FROM threads
                            GROUP BY guild_id ORDER BY n DESC""").fetchall()
    known = {r["guild_id"] for r in theirs if r["guild_id"]}
    if known and guild not in known:
        con.close()
        sys.exit(f"refusing: {a.env} names guild {guild}, and every thread in "
                 f"{a.db} belongs to {', '.join(sorted(known))}. One of the "
                 f"two is the wrong one.")

    d = S.Discord(token, guild)
    assert not d.writes_allowed, "writes must be impossible here"

    who = d.whoami()
    print(f"bot {who['bot']!r} on {who['guild']!r} ({who['guild_id']}), "
          f"writes={who['writes']}")
    print(f"env {env['__path__']}")
    print(f"db  {pathlib.Path(a.db).resolve()}\n")

    rows = unattributed(con)
    if not rows:
        print("no unattributed closures -- nothing to do")
        con.close()
        return
    print(f"{len(rows)} closures with no name, "
          f"{rows[-1]['occurred_at'][:10]} to {rows[0]['occurred_at'][:10]}")

    found, ours, pages = archivers(d, guild, {r["thread_id"] for r in rows},
                                   a.pages)
    missing = {r["thread_id"] for r in rows} - set(found) - ours
    print(f"read {pages} page(s) of the audit log: {len(found)} attributed, "
          f"{len(ours)} archived by us, {len(missing)} not found")
    if missing:
        # Out of the log's reach, which is a fact about how long ago it
        # happened rather than something to retry.
        print("  not in the log (too old, or the entry has aged out):")
        for tid in sorted(missing):
            print(f"    {tid}")

    by_name: dict[str, int] = {}
    for name in found.values():
        by_name[name] = by_name.get(name, 0) + 1
    for name, n in sorted(by_name.items(), key=lambda kv: -kv[1]):
        print(f"  {name}: {n}")

    filled = apply(con, rows, found)
    if a.dry_run:
        con.rollback()
        print(f"\ndry run: {filled} would be filled in, rolled back")
    else:
        con.commit()
        print(f"\n{filled} closures now name who closed them")
    con.close()


if __name__ == "__main__":
    main()
