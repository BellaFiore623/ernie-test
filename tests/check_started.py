"""
Who opened the thread, in the activity feed.

Threads are started in Discord, not in Bert, so the one thing the feed could
never say was where a card came from. Discord sends both halves already and
the mirror was dropping them: the thread's owner_id, and the author's
global_name -- "Tyler" rather than "tyler_mazza".

The line is written where the card is, in ensure_card, because that is the
one place that runs exactly once per thread. Two things it must not do:
claim a thread it merely inherited on a first sync, and offer to undo
something that happened in Discord.
"""

from support import GUILD, PARENT, Board, Check, iso

import bert
import ernie_api as api
import ernie_extract as ex
import ernie_load as load


def a_record(tid, name, owner=None) -> ex.ThreadRecord:
    return ex.ThreadRecord(
        thread_id=tid, parent_id=PARENT, name=name, owner_id=owner,
        title=ex.parse_title(name), client_key=None, equipment=[],
        proposals=[], created=[], participants=[], message_count=1,
        first_ts=iso(-60), last_ts=iso(-60), archived=False, issues=[])


def a_board():
    """A board whose channel makes cards, which is what ensure_card asks."""
    b = Board()
    b.con.execute(
        """INSERT INTO watched_channels (channel_id, name, mirror, generate_cards)
           VALUES (?,?,1,1)""", (PARENT, "customer-threads"))
    b.con.commit()
    return b


def a_thread(b, tid, name, *, made_ago=30, seen_ago=25, owner=None):
    b.con.execute(
        """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                first_seen_at, last_synced_at, owner_id)
           VALUES (?,?,?,?,?,?,?)""",
        (tid, PARENT, GUILD, iso(-made_ago), iso(-seen_ago), iso(), owner))
    b.con.execute(
        """INSERT INTO thread_titles (thread_id, observed_at, name, confidence)
           VALUES (?,?,?,?)""", (tid, iso(-made_ago), name, "strict"))
    b.con.commit()


def a_message(b, tid, *, author_id, username, display=None, bot=False, ago=29):
    b.con.execute(
        """INSERT INTO messages (message_id, thread_id, author_id, author_name,
                                 author_display, is_bot, created_at, first_seen_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (f"m-{tid}-{author_id}-{ago}", tid, author_id, username, display,
         int(bot), iso(-ago), iso()))
    b.con.commit()


def started_rows(b, tid=None):
    sql = "SELECT * FROM events WHERE verb='started'"
    args = ()
    if tid:
        sql += " AND thread_id=?"
        args = (tid,)
    return [dict(r) for r in b.con.execute(sql, args)]


def check_a_person_opening_a_thread() -> bool:
    c = Check("a thread somebody opened gets a line")

    with a_board() as b:
        name = "PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool"
        a_thread(b, "t1", name, owner="u-tyler")
        a_message(b, "t1", author_id="u-tyler", username="tyler_mazza",
                  display="Tyler")
        load.ensure_card(b.con, a_record("t1", name, owner="u-tyler"))
        b.con.commit()

        rows = started_rows(b, "t1")
        c.equal(len(rows), 1, "one event")
        e = rows[0]
        c.equal(e["actor_name"], "Tyler", "named the way Discord shows them")
        c.equal(e["new_value"], name, "carrying the title it opened with")
        c.ok(e["dispatch_after"] is None,
             "and never posted back -- it happened in Discord already")

        made = b.con.execute(
            "SELECT created_at FROM threads WHERE thread_id='t1'").fetchone()[0]
        c.equal(e["occurred_at"], made, "filed when the thread was opened")

        # ensure_card is the once-per-thread path, but it runs every cycle.
        load.ensure_card(b.con, a_record("t1", name, owner="u-tyler"))
        b.con.commit()
        c.equal(len(started_rows(b, "t1")), 1, "and not written twice")

    return c.report()


def check_which_name() -> bool:
    c = Check("the name it uses")

    with a_board() as b:
        # No global_name set on the account: the username is all there is.
        a_thread(b, "t2", "OPS: Trekk - 04Aug26 - SSD0008", owner="u-sam")
        a_message(b, "t2", author_id="u-sam", username="sam_r", display=None)
        load.ensure_card(b.con, a_record("t2", "OPS: Trekk", owner="u-sam"))
        b.con.commit()
        c.equal(started_rows(b, "t2")[0]["actor_name"], "sam_r",
                "falls back to the username")

        # owner_id decides, not whoever happens to be first in the thread.
        a_thread(b, "t3", "OPS: SCI - 25Aug26 - Bot swap", owner="u-tyler")
        a_message(b, "t3", author_id="u-bot", username="ernie-test",
                  display=None, bot=True, ago=29)
        a_message(b, "t3", author_id="u-tyler", username="tyler_mazza",
                  display="Tyler", ago=28)
        load.ensure_card(b.con, a_record("t3", "OPS: SCI", owner="u-tyler"))
        b.con.commit()
        c.equal(started_rows(b, "t3")[0]["actor_name"], "Tyler",
                "the owner, not the first message")

        # No owner_id at all -- Discord omits it on some archived threads.
        a_thread(b, "t4", "CS: Kenosha - 22Aug26 - Fogging", owner=None)
        a_message(b, "t4", author_id="u-jo", username="jo_h", display="Jo")
        load.ensure_card(b.con, a_record("t4", "CS: Kenosha", owner=None))
        b.con.commit()
        c.equal(started_rows(b, "t4")[0]["actor_name"], "Jo",
                "falls back to whoever opened the conversation")

    return c.report()


def check_the_lines_not_written() -> bool:
    c = Check("the threads that get no line")

    with a_board() as b:
        # seed_test_server.py opens two dozen at a time. None of them are
        # somebody starting work.
        a_thread(b, "t5", "PROD: seeded", owner="u-bot")
        a_message(b, "t5", author_id="u-bot", username="ernie-test", bot=True)
        load.ensure_card(b.con, a_record("t5", "PROD: seeded", owner="u-bot"))
        b.con.commit()
        c.equal(started_rows(b, "t5"), [], "a thread a bot opened")

        # The first sync of an existing channel creates a card for every
        # thread there has ever been. "Somebody started this" about a thread
        # from four months ago is a migration talking, not news.
        a_thread(b, "t6", "PROD: old news", owner="u-tyler",
                 made_ago=120 * 24 * 3600, seen_ago=20)
        a_message(b, "t6", author_id="u-tyler", username="tyler_mazza",
                  display="Tyler", ago=120 * 24 * 3600)
        load.ensure_card(b.con, a_record("t6", "PROD: old news", owner="u-tyler"))
        b.con.commit()
        c.equal(started_rows(b, "t6"), [], "a thread that predates the mirror")
        c.ok(b.con.execute("SELECT 1 FROM cards WHERE thread_id='t6'").fetchone(),
             "though the card is still made, which is the point of it")

        # Just inside the window: Ernie was down a couple of minutes.
        a_thread(b, "t7", "PROD: just missed it", owner="u-tyler",
                 made_ago=load.WITNESSED_WITHIN_S - 60, seen_ago=0)
        a_message(b, "t7", author_id="u-tyler", username="tyler_mazza",
                  display="Tyler", ago=load.WITNESSED_WITHIN_S - 60)
        load.ensure_card(b.con, a_record("t7", "PROD: just missed it",
                                         owner="u-tyler"))
        b.con.commit()
        c.equal(len(started_rows(b, "t7")), 1,
                "a short outage still counts as watching it appear")

    return c.report()


def check_the_feed_reads_right() -> bool:
    c = Check("how it reads in the feed")

    line = bert.Bert._feed_text({
        "verb": "started",
        "actor_name": "Tyler",
        "thread_name": "PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool",
        "old_value": None, "new_value": None,
    })
    c.ok("Tyler" in line, "it names the person")
    c.ok("started" in line, "and says what they did")
    c.ok("Penn Hills" in line, "and which thread")

    # No undo button: the generic row already refuses anything not in the
    # undoable list, and there is nothing here to take back.
    c.ok("started" not in ("completed", "priority_changed", "edited",
                           "work_done"),
         "and the verb is not one the feed offers to undo")

    return c.report()


def check_undo_refuses_it() -> bool:
    c = Check("undo cannot unmake a thread")

    with a_board() as b:
        api.DB = b.path
        tid = b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool")
        eid = b.event(tid, verb="started", actor="Tyler", old=None, new=None)

        try:
            api.undo(eid, api.ActorBody(actor="Tester"))
            c.ok(False, "it should not have been undoable")
        except Exception as e:
            detail = getattr(e, "detail", {})
            code = detail.get("code") if isinstance(detail, dict) else None
            c.equal(code, "not_undoable", "it is refused, with a reason")

        still = b.con.execute(
            "SELECT undone_at FROM events WHERE event_id=?", (eid,)).fetchone()
        c.ok(still["undone_at"] is None, "and the row is left alone")

    return c.report()


CHECKS = (check_a_person_opening_a_thread, check_which_name,
          check_the_lines_not_written, check_the_feed_reads_right,
          check_undo_refuses_it)
