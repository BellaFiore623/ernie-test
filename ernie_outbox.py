"""
Ernie's outbox -- the only thing that posts to Discord.

Reads events whose undo window has expired and posts them in-thread. Every
call goes through Discord.write(), so the guild guard applies automatically:
nothing posts unless ALLOW_DISCORD_WRITES names the configured guild.

    python ernie_outbox.py --once --env ernie-test.env --db ernie-test.db
    python ernie_outbox.py --env ernie-test.env --db ernie-test.db

Needs two permissions the read-only bot doesn't have:
  Send Messages in Threads  -- to post
  Manage Threads            -- to unarchive first, since you can't post into
                               an archived thread
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

import ernie_load as load
from ernie_sync import Discord, GuildMismatch, load_env

POLL_SECONDS = 30
MAX_ATTEMPTS = 5
CLAIM_STALE_S = 300    # a claim older than this belonged to a process that died


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Message text
# --------------------------------------------------------------------------

def render(event) -> str | None:
    """
    Turn an event row into thread text. Returning None means 'nothing to say'
    -- the event still gets marked posted so it doesn't retry forever.
    """
    who = event["actor_name"] or "Someone"
    verb = event["verb"]

    if verb == "completed":
        return f"**{who}** marked this complete in Bert."
    if verb in ("reopened", "thread_reopened"):
        if verb == "thread_reopened":
            return "This thread was reopened, so it's back on the Bert board."
        return f"**{who}** reopened this in Bert."
    if verb == "edited":
        # new_value already holds a rendered summary of every field that moved,
        # so a four-field edit is still one message.
        return f"**{who}** updated this in Bert \u2014 {event['new_value']}"
    if verb == "priority_changed":
        # Only queued for critical in either direction; see move_card. Say
        # which way it went, because "priority changed" on its own tells the
        # thread nothing it can act on.
        old, new = event["old_value"], event["new_value"]
        if new == "critical":
            return f"**{who}** made this **critical** in Bert."
        if old == "critical":
            return (f"**{who}** took this out of critical in Bert "
                    f"— it's {new or 'unassigned'} now.")
        return None
    if verb == "work_done":
        # new_value is the item's text, so the thread reads as a statement
        # about the work rather than about the board.
        return f"**{who}** finished: {event['new_value']}"
    if verb == "undo_correction":
        return (f"Correction: **{who}** undid the previous update. "
                f"Disregard the last message.")
    return None


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

# Verbs that should leave the thread archived once the message is posted, and
# verbs that should leave it open.
ARCHIVES = {"completed"}
UNARCHIVES = {"reopened", "undo_correction"}


def post_one(con, d: Discord, event) -> str:
    """
    Post a single event, then set the thread's archive state to match.

    Posting and archiving happen together, after the undo window. That way an
    undo inside the window means neither ever happened -- no message to
    retract, no thread to un-hide.
    """
    tid = event["thread_id"]
    verb = event["verb"]

    # Claim the row before saying anything to Discord. Posting first and
    # marking afterwards left a window where an undo saw posted_at still NULL,
    # decided nothing had gone out, and skipped the correction -- while the
    # message was already on its way. The claim also keeps two outbox processes
    # from both posting the same event.
    claimed = con.execute(
        """UPDATE events SET claimed_at=? WHERE event_id=?
           AND claimed_at IS NULL AND posted_at IS NULL AND undone_at IS NULL""",
        (now(), event["event_id"]))
    con.commit()
    if claimed.rowcount != 1:
        return "skipped"          # undone, or another worker got there first

    text = render(event)
    # A rename is an action, not an announcement: Discord posts its own system
    # message when a thread name changes, so render() stays quiet for it.
    rename_to = event["new_value"] if verb == "renamed" else None

    if not text and not rename_to:
        con.execute("UPDATE events SET posted_at=? WHERE event_id=?",
                    (now(), event["event_id"]))
        return "skipped"

    try:
        # You can't post into an archived thread, so open it first regardless
        # of where it should end up.
        row = con.execute("SELECT archived FROM threads WHERE thread_id=?",
                          (tid,)).fetchone()
        if row and row["archived"]:
            d.write("PATCH", f"/channels/{tid}", archived=False)
            con.execute("UPDATE threads SET archived=0 WHERE thread_id=?", (tid,))

        if rename_to:
            d.write("PATCH", f"/channels/{tid}", name=rename_to)

        msg = {}
        if text:
            msg = d.write("POST", f"/channels/{tid}/messages", content=text)

        # Now put it where it belongs.
        if verb in ARCHIVES:
            d.write("PATCH", f"/channels/{tid}", archived=True)
            con.execute(
                "UPDATE threads SET archived=1, archived_by_ernie=1 WHERE thread_id=?",
                (tid,))
        elif verb in UNARCHIVES:
            con.execute(
                "UPDATE threads SET archived=0, archived_by_ernie=0 WHERE thread_id=?",
                (tid,))

        con.execute(
            "UPDATE events SET posted_at=?, discord_message_id=? WHERE event_id=?",
            (now(), msg.get("id"), event["event_id"]))
        return "sent"

    except GuildMismatch:
        raise                                  # config problem, not a bad row
    except Exception as e:
        # Release the claim so the row is eligible again on the next pass.
        con.execute(
            """UPDATE events SET claimed_at=NULL, attempts=attempts+1,
                                  last_error=? WHERE event_id=?""",
            (str(e)[:300], event["event_id"]))
        return "failed"


def release_stale_claims(con) -> int:
    """Free rows held by a worker that died mid-post."""
    cur = con.execute(
        """UPDATE events SET claimed_at=NULL
           WHERE claimed_at IS NOT NULL AND posted_at IS NULL
             AND datetime(claimed_at) < datetime('now', ?)""",
        (f"-{CLAIM_STALE_S} seconds",))
    con.commit()
    return cur.rowcount


def drain(con, d: Discord) -> dict:
    """Post everything that's due. Rows past MAX_ATTEMPTS are left alone."""
    release_stale_claims(con)
    due = con.execute("SELECT * FROM v_outbox_due").fetchall()
    counts = {"sent": 0, "skipped": 0, "failed": 0}

    for event in due:
        counts[post_one(con, d, event)] += 1
        con.commit()

    return counts


def pending(con) -> int:
    """Events still inside their undo window."""
    return con.execute(
        """SELECT COUNT(*) FROM events
           WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
             AND undone_at IS NULL AND dispatch_after > datetime('now')"""
    ).fetchone()[0]


def stuck(con) -> list:
    return con.execute(
        """SELECT event_id, verb, attempts, last_error FROM events
           WHERE posted_at IS NULL AND undone_at IS NULL AND attempts >= ?""",
        (MAX_ATTEMPTS,)).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ernie.db")
    ap.add_argument("--env", default="ernie.env")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be posted, send nothing")
    a = ap.parse_args()

    load_env(a.env)
    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("DISCORD_GUILD_ID")
    allow = os.environ.get("ALLOW_DISCORD_WRITES")
    if not token or not guild:
        sys.exit("DISCORD_TOKEN and DISCORD_GUILD_ID must be set")

    con = load.connect(a.db)

    if a.dry_run:
        due = con.execute("SELECT * FROM v_outbox_due").fetchall()
        print(f"{len(due)} due, {pending(con)} still in the undo window\n")
        for e in due:
            print(f"  -> thread {e['thread_id']}")
            print(f"     {render(e)}\n")
        return

    d = Discord(token, guild, allow_writes_for=allow)
    who = d.whoami()
    if not who["writes"]:
        sys.exit(f"Writes are blocked for guild {guild}. Set "
                 f"ALLOW_DISCORD_WRITES={guild} in {a.env} to enable posting.")
    print(f"{who['bot']} -> {who['guild']} ({guild})  [POSTING]  db={a.db}")

    while True:
        try:
            c = drain(con, d)
            if any(c.values()):
                print(f"[{now()[:19]}] sent={c['sent']} skipped={c['skipped']} "
                      f"failed={c['failed']} waiting={pending(con)}")
            for s in stuck(con):
                print(f"  STUCK {s['event_id'][:8]} {s['verb']} "
                      f"after {s['attempts']} tries: {s['last_error']}",
                      file=sys.stderr)
        except GuildMismatch as e:
            sys.exit(str(e))
        except Exception as e:
            print(f"[{now()[:19]}] drain failed: {e}", file=sys.stderr)

        if a.once:
            return
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
