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
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import ernie_changelog
import ernie_load as load
import ernie_state
import ernie_status
import ernie_version
from ernie_sync import Discord, GuildMismatch, load_env

POLL_SECONDS = 30
MAX_ATTEMPTS = 5
# Matches ernie_api.UNDO_WINDOW_S. Duplicated rather than imported, because
# importing the API here would pull FastAPI into the outbox for one integer --
# the same trade MAX_ATTEMPTS already makes. Only make_threads needs it, for a
# ticket closed before its thread existed: every other event arrives with a
# dispatch_after the API has already worked out. tests/check_state.py holds
# the two together.
UNDO_WINDOW_S = 60
CLAIM_STALE_S = 300    # a claim older than this belonged to a process that died


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Message text
# --------------------------------------------------------------------------

def describe(e) -> str:
    """
    What an event did, phrased to follow "undid".

    Named rather than pointed at, because "the last message" stops being true
    the moment anything else is posted in the thread -- another person's
    update, or Ernie's own next one.
    """
    verb, old, new = e["verb"], e["old_value"], e["new_value"]
    if verb == "completed":
        return "marking this complete"
    if verb == "priority_changed":
        if new == "critical":
            return "making this critical"
        if old == "critical":
            return f"taking this out of critical (to {new or 'unassigned'})"
        return f"the move to {new}"
    if verb == "work_done":
        return f"finishing “{new}”" if new else "finishing a work item"
    if verb == "edited":
        return f"the edit — {new}" if new else "the edit"
    if verb == "renamed":
        return "the rename"
    return "the previous update"


def render(event, original=None) -> str | None:
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
        what = describe(original) if original else "the previous update"
        return f"Correction: **{who}** undid {what}."
    return None


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

# Verbs that should leave the thread archived once the message is posted, and
# verbs that should leave it open.
ARCHIVES = {"completed"}
UNARCHIVES = {"reopened", "undo_correction"}


def steps_done(row) -> set:
    """Which irreversible writes this row has already had.

    Tolerant of a database that has not had `migrate_outbox_steps.py` run
    against it: `check_schema` asks for the column at startup, but the outbox
    starts without going through the API, and answering "none done yet" is
    the behaviour this had before the column existed.
    """
    try:
        raw = row["sent_steps"]
    except (IndexError, KeyError):
        return set()
    return {x for x in (raw or "").split(",") if x}


def note_step(con, table: str, key_col: str, key: str, done: set, step: str,
              **extra) -> None:
    """Write down an irreversible thing the moment it is done, and commit.

    **The commit is the point.** Everything here used to be recorded once, at
    the end, so a failure on the third write discarded the fact that the first
    two had happened -- and the retry did them again. A rename is 2 per 10
    minutes on a shared budget and posts a system message every time; a
    message is a message. Neither is a thing to do twice because a later step
    timed out.
    """
    done.add(step)
    cols = ", ".join(["sent_steps=?"] + [f"{k}=?" for k in extra])
    con.execute(f"UPDATE {table} SET {cols} WHERE {key_col}=?",
                (",".join(sorted(done)), *extra.values(), key))
    con.commit()


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

    # A correction names the event it retracts, so it can say what was undone
    # and reply straight to the message that said it -- which stays right
    # however many messages land in between.
    original = reply_to = None
    if verb == "undo_correction" and event["new_value"]:
        original = con.execute("SELECT * FROM events WHERE event_id=?",
                               (event["new_value"],)).fetchone()
        if original:
            reply_to = original["discord_message_id"]

    text = render(event, original)
    # A rename is an action, not an announcement: Discord posts its own system
    # message when a thread name changes, so render() stays quiet for it.
    rename_to = event["new_value"] if verb == "renamed" else None

    # What a previous attempt already got done. Both of the writes below are
    # irreversible in a way the others are not -- unarchiving and archiving a
    # thread twice is the same as doing it once, and renaming or posting twice
    # is not.
    done = steps_done(event)

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

        if rename_to and "renamed" not in done:
            d.write("PATCH", f"/channels/{tid}", name=rename_to)
            note_step(con, "events", "event_id", event["event_id"],
                      done, "renamed")

        msg = {}
        if text and "message" not in done:
            body = {"content": text}
            if reply_to:
                # fail_if_not_exists lets it post as a plain message if the
                # one it points at has been deleted, rather than 400ing and
                # retrying until it gives up.
                body["message_reference"] = {"message_id": reply_to,
                                             "fail_if_not_exists": False}
            msg = d.write("POST", f"/channels/{tid}/messages", **body)
            # Recorded before the archive below, which is the write that was
            # failing when this was found.
            note_step(con, "events", "event_id", event["event_id"],
                      done, "message", discord_message_id=msg.get("id"))

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

        # COALESCE, because a retry that skipped the post has no `msg` and
        # must not wipe the id the earlier attempt already stored -- undo
        # replies to that message.
        con.execute(
            """UPDATE events SET posted_at=?,
                   discord_message_id=COALESCE(?, discord_message_id)
               WHERE event_id=?""",
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


def make_threads(con, d: Discord) -> dict:
    """Create the Discord threads for tickets started in Bert.

    Bert cannot do this and neither can the API -- every write to Discord goes
    through Discord.write, which is here. Until this runs the ticket is a row
    in new_threads and a card on the board wearing the unsent mark.

    Three writes, in an order chosen so a failure leaves something legible:
    the thread, a note saying who started it, then their opening message if
    they wrote one. The note comes from Bert's own settings and is plain text,
    the same way every other name this posts is -- Ernie has no idea which
    Discord account belongs to which person, so the thread is opened by the
    bot and says whose it is.
    """
    counts = {"made": 0, "failed": 0}
    due = con.execute(
        "SELECT * FROM new_threads WHERE posted_at IS NULL AND attempts < ? "
        "ORDER BY created_at", (MAX_ATTEMPTS,)).fetchall()

    for row in due:
        try:
            # **The thread is remembered the instant it exists.** Its id used
            # to be written only after both messages had gone out, so a
            # message that failed threw it away and the retry opened a
            # *second thread for the same ticket*: measured, an opening
            # message failing twice left three real threads in the customer
            # channel, the board keeping the third and the sync picking the
            # other two up later as fresh unassigned cards.
            tid = row["thread_id"]
            done = steps_done(row)
            if not tid:
                made = d.write("POST",
                               f"/channels/{row['channel_id']}/threads",
                               name=row["title"][:100], type=11,
                               auto_archive_duration=10080)
                tid = made["id"]
                con.execute("UPDATE new_threads SET thread_id=? "
                            "WHERE draft_id=?", (tid, row["draft_id"]))
                con.commit()

            # The card is written before the two messages rather than after
            # them, so a message that fails still leaves the ticket on the
            # board where somebody put it -- and recorded here at all rather
            # than left to the sync, which would bring the card back a cycle
            # later and in unassigned, losing the band the + was pressed in.
            # The sync reconciles these rows on its next pass anyway; they
            # are written the way it writes them.
            #
            # Guarded on the card not existing yet, because a retry reaches
            # this a second time: the work items are a plain INSERT with a
            # fresh uuid each, so running it twice would give the ticket every
            # bubble twice.
            ts = now()
            if not con.execute("SELECT 1 FROM cards WHERE thread_id=?",
                               (tid,)).fetchone():
                con.execute(
                    """INSERT OR IGNORE INTO threads (thread_id, parent_id,
                                                      guild_id, created_at,
                                                      first_seen_at,
                                                      last_synced_at)
                       VALUES (?,?,?,?,?,?)""",
                    (tid, row["channel_id"], d.guild_id, ts, ts, ts))
                # Parsed, through the one writer the sync uses. Writing just
                # the name left queue and client NULL until a sync cycle
                # filled them in, so a ticket whose title reads perfectly well
                # came up grey with "unknown client" the moment its thread
                # existed.
                load.record_title(con, tid, row["title"], ts)
                # Where the board has been showing it. rank is the order and
                # the only one, so it has to say what the board says -- MAX
                # plus a step put the card at the bottom of the band, and a
                # ticket somebody had just written slid away from them as soon
                # as it became real.
                #
                # The draft carries its own rank now, because it can be
                # dragged while it waits: recomputing the band's edge here
                # would take a ticket somebody had moved down into Medium and
                # put it back at the top. A row written before that column
                # existed has none, and falls back to the edge it would have
                # been given.
                rank = row["rank"]
                if rank is None:
                    edge = con.execute(
                        "SELECT MIN(rank) AS m FROM cards WHERE priority=?",
                        (row["priority"],)).fetchone()
                    rank = (load.RANK_STEP if edge["m"] is None
                            else edge["m"]) - load.RANK_STEP
                con.execute(
                    """INSERT OR IGNORE INTO cards (thread_id, priority, rank,
                                                    updated_at)
                       VALUES (?,?,?,?)""",
                    (tid, row["priority"], rank, ts))
                for n, body in enumerate(json.loads(row["work_json"] or "[]")):
                    con.execute(
                        """INSERT INTO work_items (item_id, thread_id, body,
                                                   position, created_at,
                                                   created_by)
                           VALUES (?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), tid, body, float(n + 1), ts,
                         row["actor"]))

                if row["complete_on_arrival"]:
                    # Closed while it was still a draft. Done here rather than
                    # left to Bert, which has nothing to press by then -- the
                    # card left the board when the button was pressed.
                    closer = row["completed_by"] or row["actor"]
                    con.execute(
                        "UPDATE cards SET completed_at=?, completed_by=?, "
                        "updated_at=? WHERE thread_id=?",
                        (ts, closer, ts, tid))
                    # With a dispatch, so the thread says it closed the same
                    # way every other closure does. It is undoable from the
                    # feed from here on, which is the first moment there is
                    # anything to undo.
                    con.execute(
                        """INSERT INTO events (event_id, occurred_at,
                                               actor_name, thread_id, verb,
                                               dispatch_after)
                           VALUES (?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), ts, closer, tid, "completed",
                         (datetime.now(timezone.utc)
                          + timedelta(seconds=UNDO_WINDOW_S)).isoformat()))
                con.commit()

            # The two messages, each remembered as it lands. Ernie opens the
            # thread and then says whose it is: there is no map from a Bert
            # install to a Discord account, so the bot is the author and the
            # name from settings goes in as plain text.
            who = (row["actor"] or "").strip()
            if who and "note" not in done:
                d.write("POST", f"/channels/{tid}/messages",
                        content=f"Thread started by {who} from the board.")
                note_step(con, "new_threads", "draft_id", row["draft_id"],
                          done, "note")
            if row["first_message"] and "first" not in done:
                d.write("POST", f"/channels/{tid}/messages",
                        content=row["first_message"])
                note_step(con, "new_threads", "draft_id", row["draft_id"],
                          done, "first")

            con.execute("UPDATE new_threads SET thread_id=?, posted_at=? "
                        "WHERE draft_id=?", (tid, now(), row["draft_id"]))
            counts["made"] += 1
        except Exception as e:
            con.execute(
                "UPDATE new_threads SET attempts=attempts+1, last_error=? "
                "WHERE draft_id=?", (str(e)[:500], row["draft_id"]))
            counts["failed"] += 1
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


def _pause(stop, seconds) -> bool:
    """Sleep between passes, unless asked to stop. True means stop now.

    The same shape `ernie_sync._pause` has, and here for the same reason:
    one exe runs both loops on threads, and a window that takes a full beat
    to close is a window somebody clicks twice.
    """
    seconds = max(0.0, seconds)
    if stop is None:
        time.sleep(seconds)
        return False
    return stop.wait(seconds)


def run(con, d: Discord, db: str, *, interval: int = POLL_SECONDS,
        once: bool = False, stop=None) -> None:
    """The outbox loop, so something other than a CLI can run it.

    Lifted out whole rather than reimplemented: the pass is not just
    `drain()`. It makes the threads tickets are waiting on, publishes the
    board to `#ernie-state`, writes each ticket's status into its own
    thread, and appends to the change log -- four things with four different
    reasons for being on *this* loop rather than the sync's, all of them
    about this being the only process allowed to write to Discord.

    `stop` is a `threading.Event`; nothing else about the loop moved.
    """
    while True:
        if stop is not None and stop.is_set():
            return
        try:
            c = drain(con, d)
            m = make_threads(con, d)
            if m["made"] or m["failed"]:
                print(f"[{now()[:19]}] new threads: {m['made']} made, "
                      f"{m['failed']} failed")
            if any(c.values()):
                print(f"[{now()[:19]}] sent={c['sent']} skipped={c['skipped']} "
                      f"failed={c['failed']} waiting={pending(con)}")
            for s in stuck(con):
                print(f"  STUCK {s['event_id'][:8]} {s['verb']} "
                      f"after {s['attempts']} tries: {s['last_error']}",
                      file=sys.stderr)
        except GuildMismatch:
            # Raised, not exited. `sys.exit` in a thread raises SystemExit,
            # which threading swallows without a word -- so on a config with
            # no ALLOW_DISCORD_WRITES the outbox thread would simply stop and
            # the application would go on looking fine while nothing posted.
            # That is production's env exactly, so it is the first
            # configuration this would ever have met.
            raise
        except Exception as e:
            print(f"[{now()[:19]}] drain failed: {e}", file=sys.stderr)

        # SQLite -> Discord, so it goes through the one process allowed to
        # write there. The pull in the other direction rides with the sync.
        state_channel = os.environ.get("STATE_CHANNEL_ID")
        if state_channel and d.writes_allowed:
            try:
                s = ernie_state.publish(d, state_channel, db)
                if s["posted"] or s["edited"]:
                    print(f"[{now()[:19]}] state: posted {s['posted']}, "
                          f"edited {s['edited']}")
            except Exception as e:
                print(f"[{now()[:19]}] state publish failed: {e}",
                      file=sys.stderr)

        # The ticket's own status, in its own thread. No channel to
        # configure: it goes to the threads the board already knows about,
        # and only the ones Ernie watched open -- so a machine that has just
        # inherited a server posts nothing.
        if d.writes_allowed:
            try:
                st = ernie_status.publish(d, con, db)
                if st["posted"] or st["edited"]:
                    print(f"[{now()[:19]}] status: posted {st['posted']}, "
                          f"edited {st['edited']}")
            except Exception as e:
                print(f"[{now()[:19]}] status failed: {e}", file=sys.stderr)

        # The durable record, if there is somewhere to keep it. Both boards
        # hold the whole history, so only one machine should set this -- two
        # would write every line twice.
        log_channel = os.environ.get("CHANGELOG_CHANNEL_ID")
        if log_channel and d.writes_allowed:
            try:
                c = ernie_changelog.tick(d, log_channel, con)
                if c["sent"] or c.get("struck"):
                    note = f"{c['sent']} logged"
                    if c.get("struck"):
                        note += f", {c['struck']} struck through"
                    print(f"[{now()[:19]}] changelog: {note}")
            except Exception as e:
                print(f"[{now()[:19]}] changelog failed: {e}", file=sys.stderr)

        if once:
            return
        if _pause(stop, interval):
            return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", action="version",
                    version=ernie_version.describe())
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
    print(f"ernie_outbox {ernie_version.describe()}")
    print(f"{who['bot']} -> {who['guild']} ({guild})  [POSTING]  db={a.db}")

    try:
        run(con, d, a.db, interval=a.interval, once=a.once)
    except GuildMismatch as e:
        # A CLI can still leave on it: it is a configuration problem, not a
        # bad row, and there is a console to say so in.
        sys.exit(str(e))


if __name__ == "__main__":
    main()
