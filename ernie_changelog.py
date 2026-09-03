"""
A durable record of every board change, in a Discord channel.

Separate from the customer threads, which only hear about the handful of
changes worth interrupting somebody for. This gets all of them, in one place,
in the order they happened -- for looking back at later rather than reading
as it goes.

    python ernie_changelog.py --once --env ernie-test.env --db ernie-test.db
    python ernie_changelog.py --backfill    # include everything already in the db

Inert until CHANGELOG_CHANNEL_ID is set, and **only one machine should set
it**. Both boards hold the whole history -- their own changes and replays of
the other's -- so two of these logging into one channel writes every line
twice.

Nothing reads this back. Deleting the channel and unsetting the variable
leaves no trace beyond a table nothing looks at.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import ernie_load as load
from ernie_state import connect, discord_time
from ernie_sync import Discord

POLL_SECONDS = 60
BATCH = 20          # lines per pass, so a backlog doesn't hold the loop
# Must match ernie_api.UNDO_WINDOW_S. Copied rather than imported: ernie_api
# pulls in FastAPI, and the outbox process this runs inside has no other
# reason to load it.
UNDO_WINDOW_S = 60


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def settled(e) -> bool:
    """
    Has this change finished happening?

    An event inside its undo window may still be cancelled, and logging it
    then would put something in the record that never happened. Waiting costs
    a minute and means the log only ever says true things.

    A silent change -- every band move that isn't in or out of critical, and
    every reorder -- carries no dispatch_after, because Ernie is never going
    to post it to the thread. That is not the same as being finished, and
    reading it that way logged the bulk of the board's activity the instant it
    happened, ahead of the changes that do wait. They get the same minute,
    measured from when they happened rather than from a dispatch they haven't
    got.
    """
    if e["undone_at"]:
        return True                       # undone is itself an outcome
    if e["posted_at"]:
        return True                       # already out; undo posts a correction
    if e["dispatch_after"]:
        return e["dispatch_after"] <= now_iso()
    return deadline(e["occurred_at"]) <= now_iso()


def deadline(occurred_at: str) -> str:
    """When a silent change stops being cancellable without a trace."""
    try:
        at = datetime.fromisoformat(occurred_at)
    except (TypeError, ValueError):
        # An unparseable timestamp is not a reason to withhold the record
        # forever. Log it and let the line carry whatever it says.
        return "0000"
    return (at + timedelta(seconds=UNDO_WINDOW_S)).isoformat()


def describe(e) -> str:
    """One change, as a line somebody would want to read back."""
    verb, old, new = e["verb"], e["old_value"], e["new_value"]
    thread = e["thread_name"] or e["thread_id"]
    where = f"*{thread}*"

    if verb == "priority_changed":
        return f"moved {where} — {old or 'unassigned'} → {new or 'unassigned'}"
    if verb == "reordered":
        return f"reordered {where}"
    if verb == "completed":
        return f"completed {where}"
    if verb == "reopened":
        return f"reopened {where}"
    if verb == "work_done":
        return f"finished “{new}” on {where}"
    if verb == "edited":
        return f"edited {where}" + (f" — {new}" if new else "")
    if verb == "renamed":
        return f"renamed {where} → *{new}*"
    if verb == "undo_correction":
        return f"retracted an update on {where}"
    if verb.startswith("set_"):
        return f"set {verb[4:].replace('_', ' ')} on {where} to {new or '(empty)'}"
    return f"{verb.replace('_', ' ')} on {where}"


def render(e) -> str:
    """
    The line as it goes into the channel.

    An undone change is struck through rather than left out: that it was made
    and then taken back is part of the record, and leaving it out would make
    the log disagree with Bert's activity feed.
    """
    who = e["actor_name"] or "Ernie"
    line = f"**{who}** {describe(e)}"
    if e["undone_at"]:
        line = f"~~{line}~~ — undone by {e['undone_by'] or 'someone'}"
    return f"{discord_time(e['occurred_at'], 'f')}  {line}"


def pending(con, limit: int = BATCH) -> list:
    """Settled events this channel hasn't been told about, oldest first."""
    rows = con.execute(
        """SELECT e.*, v.name AS thread_name
           FROM events e
           LEFT JOIN v_thread_current v ON v.thread_id = e.thread_id
           WHERE e.event_id NOT IN (SELECT event_id FROM changelog_sent)
           ORDER BY e.occurred_at
           LIMIT ?""", (limit * 4,)).fetchall()
    return [r for r in rows if settled(r)][:limit]


def mark(con, event_id: str, message_id: str | None) -> None:
    con.execute(
        "INSERT OR REPLACE INTO changelog_sent (event_id, message_id, sent_at)"
        " VALUES (?,?,?)", (event_id, message_id, now_iso()))


def retracted(con, limit: int = BATCH) -> list:
    """
    Lines already posted that have since been undone.

    Undo has no deadline, so waiting out the window is not enough on its own:
    a change can be taken back a day later, long after its line went up. The
    line has to be corrected where it stands, or the record quietly keeps
    asserting something that was reversed.

    sent_at against undone_at is the whole bookkeeping. Editing the message
    re-stamps sent_at, which puts it past the undo and takes the row back out
    of this list, so a failed edit retries and a successful one doesn't.
    """
    return con.execute(
        """SELECT e.*, v.name AS thread_name, s.message_id AS log_message_id
           FROM events e
           JOIN changelog_sent s ON s.event_id = e.event_id
           LEFT JOIN v_thread_current v ON v.thread_id = e.thread_id
           WHERE e.undone_at IS NOT NULL
             AND s.message_id IS NOT NULL
             AND datetime(s.sent_at) < datetime(e.undone_at)
           ORDER BY e.undone_at
           LIMIT ?""", (limit,)).fetchall()


def catch_up(con, note: str = "") -> int:
    """
    Mark everything already in the database as logged, without posting it.

    Turning the log on shouldn't replay months of history into a channel
    nobody has read yet. --backfill is how you ask for that instead.
    """
    rows = con.execute(
        """SELECT event_id FROM events
           WHERE event_id NOT IN (SELECT event_id FROM changelog_sent)""").fetchall()
    for r in rows:
        mark(con, r["event_id"], None)
    con.commit()
    if rows and note:
        print(f"{note}: {len(rows)} existing events marked as already logged")
    return len(rows)


def initialised(con) -> bool:
    """Has the log ever run against this database?"""
    return con.execute("SELECT 1 FROM changelog_state WHERE id=1").fetchone() is not None


def mark_initialised(con) -> None:
    con.execute("INSERT OR IGNORE INTO changelog_state (id, started_at) "
                "VALUES (1, ?)", (now_iso(),))
    con.commit()


def drain(d: Discord, cid: str, con) -> dict:
    """Post what's due. One message per change, so each is quotable on its own."""
    sent = failed = struck = 0

    # Corrections first: a line that is now wrong is worse than a line that is
    # late, and if the channel is refusing writes this pass, fixing the record
    # is the half worth having attempted.
    for e in retracted(con):
        try:
            d.write("PATCH", f"/channels/{cid}/messages/{e['log_message_id']}",
                    content=render(e))
            mark(con, e["event_id"], e["log_message_id"])
            con.commit()
            struck += 1
        except Exception as err:
            print(f"  changelog: {e['event_id'][:8]} strike failed -- {err}",
                  file=sys.stderr)
            failed += 1
            break

    for e in pending(con):
        try:
            msg = d.write("POST", f"/channels/{cid}/messages", content=render(e))
            mark(con, e["event_id"], msg.get("id"))
            con.commit()          # per line: a failure halfway repeats nothing
            sent += 1
        except Exception as err:
            print(f"  changelog: {e['event_id'][:8]} failed -- {err}",
                  file=sys.stderr)
            failed += 1
            break                 # channel is unhappy; try again next pass
    return {"sent": sent, "failed": failed, "struck": struck}


def tick(d: Discord, cid: str, con) -> dict:
    """
    One pass, for a caller that already has a connection and a loop.

    Skips the history the first time it ever runs, so switching the log on
    doesn't replay the whole database into a channel nobody has read yet.
    Use --backfill on the standalone script if that is what you want.
    """
    if not initialised(con):
        catch_up(con)
        mark_initialised(con)
        return {"sent": 0, "failed": 0, "struck": 0}
    return drain(d, cid, con)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="ernie-test.env")
    ap.add_argument("--db", default="ernie-test.db")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill", action="store_true",
                    help="post the history already in the database, not just "
                         "what happens from now on")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS)
    a = ap.parse_args()

    d, _ = connect(a.env)
    cid = os.environ.get("CHANGELOG_CHANNEL_ID")
    if not cid:
        sys.exit("CHANGELOG_CHANNEL_ID is not set, so there is nowhere to log. "
                 "Make a #change-log channel, give the bot access, and put its "
                 "id in the env file.")
    if not d.get(f"/channels/{cid}"):
        sys.exit(f"Can't see channel {cid}. The bot needs View Channel and "
                 f"Send Messages on it.")

    con = load.connect(a.db)
    # Only ever on the very first run, when the table is empty. Doing it on
    # every start would silently swallow everything that happened while the
    # logger was down, which is the one thing a record must not do.
    if not initialised(con):
        if not a.backfill:
            catch_up(con, "first run")
        mark_initialised(con)

    while True:
        try:
            c = drain(d, cid, con)
            if c["sent"] or c.get("struck"):
                note = f"{c['sent']} logged"
                if c.get("struck"):
                    note += f", {c['struck']} struck through"
                print(f"[{now_iso()[:19]}] changelog: {note}")
        except Exception as e:
            print(f"[{now_iso()[:19]}] changelog failed: {e}", file=sys.stderr)
        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
