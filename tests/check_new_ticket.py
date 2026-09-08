"""
A ticket started in Bert arrives as the card it was written as.

Bert cannot open a Discord thread -- every write to Discord goes through
Discord.write, which lives in the outbox -- so a new ticket is a row in
new_threads and a placeholder card wearing the unsent mark until the outbox
has made it. The board therefore draws the same ticket twice: once from what
somebody typed, and once from the rows the outbox writes. The two have to
agree, and they did not.

The outbox wrote the title row with only the name, so queue and client stayed
NULL until a sync cycle filled them in -- a title that reads perfectly well
came up grey with "unknown client". And it ranked the card to MAX + a step,
the bottom of its band, while Bert had been showing it at the top since the
+ was pressed: a ticket somebody had just written slid away from them the
moment it became real.
"""

import json

from support import Board, Check, FakeDiscord, GUILD, PARENT, iso

import ernie_extract as ex
import ernie_load as load
import ernie_outbox as outbox


TITLE = "PROD: CHA Solutions - 08Sep26 - what it's about"


def draft(b, title=TITLE, priority="high", work=(), actor="Bella Fiore"):
    b.con.execute(
        """INSERT INTO new_threads (draft_id, channel_id, title, priority,
                                    work_json, actor, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        ("draft-1", PARENT, title, priority, json.dumps(list(work)),
         actor, iso()))
    b.con.commit()


def made_card(b):
    return b.con.execute(
        """SELECT c.thread_id, c.priority, c.rank, t.name, t.queue,
                  t.client_raw, t.client_key, t.summary, t.confidence
           FROM cards c
           JOIN thread_titles t ON t.thread_id = c.thread_id
           WHERE c.thread_id = (SELECT thread_id FROM new_threads
                                WHERE draft_id='draft-1')""").fetchone()


def check_the_title_is_read_when_the_thread_is_made() -> bool:
    """
    The card knows its own tag and client the moment its thread exists.

    Nothing here is guesswork on the outbox's part: the title is the one it
    just gave Discord, and ex.parse_title is what the sync would read it with
    an hour later. Writing only the name meant the card spent a cycle with no
    queue -- so no tag colour -- and no client, which is what the board shows
    as grey and "unknown client".
    """
    c = Check("a new ticket's title is read when its thread is made")

    with Board() as b:
        draft(b)
        counts = outbox.make_threads(b.con, FakeDiscord())
        c.equal(counts["made"], 1, "the thread is made")

        row = made_card(b)
        c.ok(row is not None, "and the card is written with a title row")
        if row is None:
            return c.report()

        want = ex.parse_title(TITLE)
        c.equal(row["name"], TITLE, "the title is the one that was typed")
        c.equal(row["queue"], "PROD", "the tag is read off it, so the card has a colour")
        c.equal(row["client_raw"], "CHA Solutions", "and the client, so it has a name")
        c.equal(row["client_key"], ex.normalise_client("CHA Solutions"),
                "normalised the one way, for matching")
        c.equal(row["summary"], want.summary, "and what it is about")
        c.equal(row["confidence"], want.confidence,
                "at the confidence the parser gives it, not a word of our own")
        c.ok(row["confidence"] not in ex.UNREADABLE_CONFIDENCE,
             "which for a title like this is readable, so no red edge")

    return c.report()


def check_a_new_ticket_lands_where_the_board_showed_it() -> bool:
    """
    Top of the band it was started in, because that is where it has been.

    rank is the order and the only one, so it has to say what the board says.
    Bert inserts the placeholder at position 0 of the band whose + was
    pressed; MAX + a step put the real card at the bottom, so the ticket
    moved on the person who wrote it.
    """
    c = Check("a new ticket lands where the board was showing it")

    with Board() as b:
        b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 respool", "high", 1000.0)
        b.card("OPS: Trekk - 03Sep26 - EReel-1301 eval", "high", 2000.0)
        b.card("PROD: Puris - 04Sep26 - EReel-1400 swap", "low", 500.0)
        draft(b, priority="high")
        outbox.make_threads(b.con, FakeDiscord())

        row = made_card(b)
        c.ok(row is not None, "the card is written")
        if row is None:
            return c.report()

        c.equal(row["priority"], "high", "in the band whose + was pressed")

        order = [r["thread_id"] for r in b.con.execute(
            "SELECT thread_id FROM cards WHERE priority='high' ORDER BY rank")]
        c.equal(order[0], row["thread_id"], "at the top of it, first by rank")
        c.equal(len(order), 3, "and the band's other cards are still there")

        # Ranked against its own band, not the whole board.
        low = b.con.execute(
            "SELECT rank FROM cards WHERE priority='low'").fetchone()["rank"]
        c.equal(low, 500.0, "a card in another band is not moved")

    return c.report()


def check_the_first_band_ticket_still_gets_a_rank() -> bool:
    """An empty band has no MIN to step down from."""
    c = Check("the first ticket in an empty band still gets a rank")

    with Board() as b:
        draft(b, priority="critical")
        outbox.make_threads(b.con, FakeDiscord())
        row = made_card(b)
        c.ok(row is not None, "the card is written")
        if row is not None:
            c.ok(row["rank"] is not None, "with a rank")
            c.ok(isinstance(row["rank"], float), "a real number, not NULL")

    return c.report()


def check_one_writer_decides_what_a_title_row_holds() -> bool:
    """
    Two places write a title revision, and they must write the same shape.

    They did not: the sync wrote nine columns and the outbox wrote two. Both
    go through load.record_title now, so there is one answer.
    """
    c = Check("one writer decides what a title row holds")

    c.ok(hasattr(load, "record_title"), "ernie_load.record_title exists")

    with Board() as b:
        b.con.execute(
            """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                    first_seen_at, last_synced_at)
               VALUES (?,?,?,?,?,?)""",
            ("t-1", PARENT, GUILD, iso(-60), iso(-60), iso()))
        b.con.commit()
        load.record_title(b.con, "t-1", TITLE)
        b.con.commit()
        row = b.con.execute(
            "SELECT * FROM thread_titles WHERE thread_id='t-1'").fetchone()
        c.ok(row is not None, "and writes a row")
        if row is not None:
            for col in ("queue", "client_raw", "client_key", "thread_date",
                        "summary", "confidence"):
                c.ok(row[col] is not None, f"filling {col}")

    return c.report()


CHECKS = (check_the_title_is_read_when_the_thread_is_made,
          check_a_new_ticket_lands_where_the_board_showed_it,
          check_the_first_band_ticket_still_gets_a_rank,
          check_one_writer_decides_what_a_title_row_holds)
