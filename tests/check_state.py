"""
The board says how it is honestly.

`synced_at` is the publish -- this machine pushing its own view -- and it
advances every cycle whether or not anything is coming back. Measuring contact
with it reported "in step" with the sync loop stopped. `agreed_at` is written
by the pull alone, which is the direction their changes arrive in.

The summary message is held to the same standard: it says when it was
published, because one machine's write time is all it can honestly know.
"""

import dataclasses
import json
import pathlib

NL = chr(10)
import sqlite3

from support import Board, Check, FakeDiscord, iso

import bert
import ernie_api as api
import ernie_outbox as outbox
import ernie_state as S


def a_board(b: Board) -> list:
    """A few cards across the bands, one of them closed."""
    b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber response", "unassigned", 2000.0)
    b.card("OPS: Munhall - 26Aug26 - 1k reel", "unassigned", 3000.0)
    b.card("OPS: Clinton MS - 24Aug26 - Order for Wheels", "critical", 1000.0)
    b.card("OPS: Baldwin - 02Sep26 - Gooseneck 10in", "low", 1000.0)
    b.card("CS: Latrobe - 30Aug26 - Camera head fogging", "medium", 1000.0,
           completed=True)
    return S.load_board(b.path)


def as_channel(cards) -> dict:
    """The state channel holding exactly what this machine holds."""
    return {c.thread_id: {"message_id": f"m-{c.thread_id}", "payload": c.payload()}
            for c in cards}


def check_agreed_at() -> bool:
    c = Check("agreed_at")

    with Board() as b:
        cards = a_board(b)
        channel = as_channel(cards)
        S.fetch_state = lambda d, cid: channel      # no Discord in a check

        # Publishing is what creates these rows, and it must not claim contact.
        for card in cards:
            S.save_base(b.con, card.thread_id, "m", card.payload())
        b.con.commit()
        rows = b.state_sync()
        c.ok(all(r["synced_at"] for r in rows.values()),
             "publish records a base for every card")
        c.ok(all(r["agreed_at"] is None for r in rows.values()),
             "and claims no agreement with the other board")

        pushed = {t: r["synced_at"] for t, r in rows.items()}

        r = S.reconcile(None, "chan", b.path)
        c.equal(r["settled"], len(cards), "every card settles against itself")

        rows = b.state_sync()
        c.ok(all(row["agreed_at"] for row in rows.values()),
             "the settled path stamps agreed_at -- it used to write nothing")
        c.ok(all(rows[t]["synced_at"] == v for t, v in pushed.items()),
             "and does not re-stamp synced_at on a card it only compared")

        first = max(row["agreed_at"] for row in rows.values())
        S.reconcile(None, "chan", b.path)
        again = max(row["agreed_at"] for row in b.state_sync().values())
        c.ok(again > first, "a later pass moves it on")

        S.reconcile(None, "chan", b.path, dry_run=True)
        c.equal(max(row["agreed_at"] for row in b.state_sync().values()), again,
                "a dry run records nothing")

    return c.report()


def check_health_guard() -> bool:
    c = Check("/health column guard")

    def health(path):
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(state_sync)")}
            col = "agreed_at" if "agreed_at" in cols else "NULL"
            return dict(con.execute(
                f"SELECT COUNT(*) AS n, MAX({col}) AS last FROM state_sync"
            ).fetchone())
        finally:
            con.close()

    with Board() as b:
        cards = a_board(b)
        for card in cards:
            S.save_base(b.con, card.thread_id, "m", card.payload())
        b.con.execute("UPDATE state_sync SET agreed_at=?", (iso(-30),))
        b.con.commit()

        c.ok(health(b.path)["last"] is not None, "a migrated database reports contact")

        # A database that has not had the migration run against it must say
        # "no contact", not 500 on /health.
        b.con.execute("ALTER TABLE state_sync DROP COLUMN agreed_at")
        b.con.commit()
        r = health(b.path)
        c.ok(r["n"] > 0 and r["last"] is None,
             "an unmigrated one reports no contact rather than failing")

    return c.report()


def check_summary_stamp() -> bool:
    c = Check("summary stamp")

    with Board() as b:
        cards = a_board(b)
        parts = S.render_summary(cards)
        c.equal(len(parts), 1, "a small board is one message")
        content = parts[0]

        c.ok("last published " in content, "the summary says when it was published")
        c.ok("last checked" not in content, "and no longer claims to have checked")
        c.ok(len(content) <= S.CONTENT_MAX, "it fits Discord's content cap")

        old_style = content.replace("last published ", "last checked ")
        c.equal(S.without_stamp(content), S.without_stamp(old_style),
                "both spellings strip, so a pre-rename channel does not churn")
        c.ok(S.CHECKED.search(content) and S.CHECKED.search(old_style),
             "and both read back")

        def existing(stamp):
            body = [l for l in content.splitlines()
                    if not l.startswith("last published ")]
            body.insert(2, stamp)
            return [{"id": "msg1", "content": NL.join(body)}]

        fresh = S.discord_time(S.now_iso())
        stale = int(S.datetime.now(S.timezone.utc).timestamp()) - S.SUMMARY_HEARTBEAT_S - 60

        c.equal(S.publish_summary(FakeDiscord(), "c", cards,
                                  existing(f"last published {fresh}")),
                "unchanged", "an unchanged board is left alone")
        c.equal(S.publish_summary(FakeDiscord(), "c", cards,
                                  existing(f"last checked {fresh}")),
                "unchanged", "including one still carrying the old spelling")
        c.equal(S.publish_summary(FakeDiscord(), "c", cards,
                                  existing(f"last published <t:{stale}:R>")),
                "edited", "a stale stamp is refreshed on the heartbeat")
        c.equal(S.publish_summary(FakeDiscord(), "c", cards, []),
                "posted", "an empty channel gets the summary posted")

        moved = list(cards)
        i = next(i for i, x in enumerate(moved)
                 if not x.completed and x.priority != "critical")
        moved[i] = dataclasses.replace(moved[i], priority="critical")
        c.equal(S.publish_summary(FakeDiscord(), "c", moved,
                                  existing(f"last published {fresh}")),
                "edited", "a board that actually moved is rewritten at once")

    return c.report()


def check_a_long_board_keeps_every_row() -> bool:
    """
    A board bigger than one message used to lose its tail.

    render_summary packed everything into one message and then dropped lines
    off the bottom until what was left fit Discord's 2000 character cap. The
    cards it dropped were the lowest ranked ones, so nothing looked wrong --
    the summary just quietly stopped being the whole running order at about
    thirty cards, which is a size a real board reaches in a fortnight.

    The channel already refuses to put every card in one message. The summary
    has no better claim to it.
    """
    c = Check("a board too big for one message")

    with Board() as b:
        bands = ("critical", "high", "medium", "low", "unassigned")
        for i in range(60):
            b.card(f"PROD: Client {i:02d} - 02Sep26 - Equipment item number {i}",
                   bands[i % len(bands)], 1000.0 + i)
        cards = S.load_board(b.path)
        live = [x for x in cards if not x.completed]

        parts = S.render_summary(cards)
        c.ok(len(parts) > 1, "it takes more than one message")
        c.ok(all(len(x) <= S.CONTENT_MAX for x in parts),
             "and every one of them fits the cap")

        rows = sum(1 for x in parts for l in x.splitlines() if l.startswith("`"))
        c.equal(rows, len(live), "every open card is listed, none dropped")
        c.ok(not any("truncated" in x for x in parts),
             "so nothing has to apologise for a missing tail")

        # Whatever finds or clears the summary has to find the later pages
        # too, and none of them may look like a card to the pull.
        c.ok(all(x.startswith(S.SUMMARY_MARK) for x in parts),
             "every page is found by the same marker")
        c.ok(all(S.parse(x) is None for x in parts),
             "and none of them parses as state")

        # A heading is no use as the last thing on a page.
        for i, x in enumerate(parts):
            last = x.splitlines()[-1]
            c.ok(not (last.startswith("**") and last.endswith("**")),
                 f"page {i + 1} does not end on a stranded band heading")

    return c.report()


def check_the_pages_follow_the_board() -> bool:
    """Pages are posted, edited and removed as the board changes size."""
    c = Check("summary pages follow the board")

    with Board() as b:
        for i in range(60):
            b.card(f"PROD: Client {i:02d} - 02Sep26 - Equipment item {i}",
                   "medium", 1000.0 + i)
        big = S.load_board(b.path)
        pages = S.render_summary(big)

        c.equal(S.publish_summary(FakeDiscord(), "c", big, []),
                "posted", "an empty channel gets every page posted")

        # The same board again, already published: nothing to say.
        settled = [{"id": str(i), "content": x} for i, x in enumerate(pages)]
        settled[0]["content"] = pages[0]      # page 1 keeps its fresh stamp
        c.equal(S.publish_summary(FakeDiscord(), "c", big, settled),
                "unchanged", "and is left alone on the next pass")

        # A board that shrinks below a page boundary must not leave the old
        # tail sitting there claiming cards that have gone.
        small = big[:6]
        out = S.publish_summary(FakeDiscord(), "c", small, settled)
        c.ok("removed" in out, f"a shrunken board removes its spare pages ({out})")

    return c.report()


def _publish_into(chan, b, cards=None):
    """Publish, and reflect what went out into the fake channel."""
    d = FakeDiscord()
    r = S.publish(d, "chan", b.path, cards=cards)
    for verb, path, content in d.calls:
        p = S.parse(content)
        if not p:
            continue
        chan[p["thread"]] = {"message_id": path.rsplit("/", 1)[-1],
                             "payload": p, "content": content}
    return r, d


def check_publish_leaves_a_card_the_channel_moved() -> bool:
    """
    The push has to resolve three ways as well, or it undoes the pull.

    Seen in a two-machine test: somebody moves a card, and a minute later the
    other stack moves it back. The push and the pull are separate processes on
    separate loops, so a change arriving between our last pull and this push
    is a difference that belongs to them -- and publish() overwrote whatever
    was in the channel whenever it differed from the local row, which is only
    right when the difference is ours.

    Worse, it then recorded its own stale view as the agreed base. So the
    board that made the change pulled it back out again a cycle later, and the
    change simply vanished.
    """
    c = Check("publish leaves a card the channel has moved")
    saved = S.fetch_channel, S.fetch_state
    try:
        with Board() as b:
            tid = b.card("PROD: Trekk - 04aug26 - SSD0008", "unassigned", 1000.0)
            channel = {}
            S.fetch_channel = lambda d, cid: (channel, [])

            # First publish: an empty channel, so it is posted and agreed.
            _publish_into(channel, b)
            c.ok(tid in channel, "the card reaches the channel")
            base = json.loads(b.state_sync()[tid]["base_json"])
            c.equal(base["priority"], "unassigned", "and a base is recorded")

            # The other board moves it. We have not pulled yet, so our own row
            # is still the older answer.
            # Rendered the way their machine would render it, prose and
            # payload together -- a message with one but not the other is a
            # state the channel never actually holds.
            theirs = dataclasses.replace(S.load_board(b.path)[0],
                                         priority="critical",
                                         actor="Julian Dubeau")
            content = S.render(theirs, 1, "Julian Dubeau")
            channel[tid] = {"message_id": channel[tid]["message_id"],
                            "payload": S.parse(content), "content": content}
            c.equal(b.con.execute("SELECT priority FROM cards WHERE thread_id=?",
                                  (tid,)).fetchone()[0],
                    "unassigned", "our copy still says unassigned")

            r, d = _publish_into(channel, b)
            c.equal(r.get("deferred"), 1, "the card is left alone")
            c.equal([v for v in d.verbs() if v == "PATCH"], [],
                    "nothing is written over it")
            c.equal(channel[tid]["payload"]["priority"], "critical",
                    "so their change is still there to be read")

            # And nothing was agreed: recording their state as our base without
            # applying it would lose the change just as thoroughly.
            base = json.loads(b.state_sync()[tid]["base_json"])
            c.equal(base["priority"], "unassigned",
                    "the base is untouched, because nothing was agreed")

            # The pull is what settles it.
            S.fetch_state = lambda d, cid: channel
            S.reconcile(None, "chan", b.path)
            c.equal(b.con.execute("SELECT priority FROM cards WHERE thread_id=?",
                                  (tid,)).fetchone()[0],
                    "critical", "the pull applies their change")

            e = b.con.execute(
                """SELECT actor_name, old_value, new_value FROM events
                   WHERE verb='priority_changed'
                   ORDER BY rowid DESC LIMIT 1""").fetchone()
            c.equal((e["old_value"], e["new_value"]), ("unassigned", "critical"),
                    "and the feed says which way it went")
            c.equal(e["actor_name"], "Julian Dubeau", "naming who did it")

            # The two now agree, so the next push has nothing to say.
            r, d = _publish_into(channel, b)
            c.equal(r.get("deferred", 0), 0, "the next push defers nothing")
            c.equal([v for v in d.verbs() if v == "PATCH"], [],
                    "and writes nothing, because there is nothing to write")
    finally:
        S.fetch_channel, S.fetch_state = saved

    return c.report()


def check_publish_still_sends_our_own_changes() -> bool:
    """The guard must only catch differences that are theirs."""
    c = Check("our own changes still go out")
    saved = S.fetch_channel
    try:
        with Board() as b:
            tid = b.card("OPS: Munhall - 26Aug26 - 1k reel", "unassigned", 1000.0)
            channel = {}
            S.fetch_channel = lambda d, cid: (channel, [])
            _publish_into(channel, b)

            # We move it. The channel is exactly where we left it.
            b.con.execute("UPDATE cards SET priority='high' WHERE thread_id=?",
                          (tid,))
            b.con.commit()

            r, d = _publish_into(channel, b)
            c.equal(r.get("deferred", 0), 0, "nothing is deferred")
            c.equal(r["edited"], 1, "the change is published")
            c.ok("PATCH" in d.verbs(), "as an edit to its own message")
            c.equal(channel[tid]["payload"]["priority"], "high",
                    "and the channel now carries it")

            base = json.loads(b.state_sync()[tid]["base_json"])
            c.equal(base["priority"], "high", "and it becomes what we agreed")
    finally:
        S.fetch_channel = saved

    return c.report()


def check_a_given_up_change_is_not_pending_for_ever() -> bool:
    """
    Bert warned about unsent changes on a board nobody had touched.

    The outbox stops trying after MAX_ATTEMPTS and says so; v_outbox_due
    carries the same limit. /health did not, so a row nothing would ever pick
    up again was counted as owed for ever -- and the warning's advice, leave
    the stack running another minute, was the one thing that could not help.

    It is not hidden either. Given up is a different state from waiting, and
    worth knowing, so it is reported on its own.
    """
    c = Check("a change the outbox gave up on stops being 'about to send'")

    with Board() as b:
        api.DB = b.path
        tid = b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool")

        eid = b.event(tid, verb="edited", actor="Bella Fiore")
        b.con.execute("UPDATE events SET dispatch_after=?, attempts=0 "
                      "WHERE event_id=?", (iso(-30), eid))
        b.con.commit()
        h = api.health()
        c.equal(h["queued"]["count"], 1, "a change still being tried is owed")
        c.equal(h["stuck"]["count"], 0, "and nothing has been given up on")

        # It fails its way to the limit.
        b.con.execute("UPDATE events SET attempts=? WHERE event_id=?",
                      (api.OUTBOX_MAX_ATTEMPTS, eid))
        b.con.commit()
        h = api.health()
        c.equal(h["queued"]["count"], 0,
                "past the limit it is no longer about to go out")
        c.equal(h["stuck"]["count"], 1, "but it is reported as stuck")

        # One under the limit is still going to be tried.
        b.con.execute("UPDATE events SET attempts=? WHERE event_id=?",
                      (api.OUTBOX_MAX_ATTEMPTS - 1, eid))
        b.con.commit()
        c.equal(api.health()["queued"]["count"], 1, "one try left still counts")

    return c.report()


def check_the_attempt_limit_is_one_number() -> bool:
    """Three places know when the outbox gives up, and they have to agree."""
    c = Check("the attempt limit agrees everywhere")

    c.equal(api.OUTBOX_MAX_ATTEMPTS, outbox.MAX_ATTEMPTS,
            "the API and the outbox use the same limit")

    schema = pathlib.Path(
        pathlib.Path(api.__file__).with_name("schema.sql")).read_text(
            encoding="utf-8")
    view = schema[schema.index("v_outbox_due"):]
    view = view[:view.index(";")]
    c.ok(f"attempts < {api.OUTBOX_MAX_ATTEMPTS}" in view,
         f"and so does v_outbox_due ({api.OUTBOX_MAX_ATTEMPTS})")

    return c.report()


def check_closing_knows_about_the_shared_board() -> bool:
    """
    Reordering and closing straight away asked nothing.

    The warning counted only what was queued for the customer thread, and a
    reorder is silent by design -- no dispatch_after, so nothing to queue. But
    it still has to reach the other board, and closing the stack on top of it
    strands it exactly the same way.
    """
    c = Check("closing counts what the shared board is still owed")

    owed = bert.Bert._owed
    c.equal(owed({}), (0, 0), "a board with no health owes nothing")
    c.equal(owed({"queued": {"count": 2}}), (2, 0), "queued changes are owed")
    c.equal(owed({"sharing": {"waiting_to_send": 3}}), (0, 3),
            "and so are cards the shared board has not seen")
    c.equal(owed({"queued": {"count": 1}, "sharing": {"waiting_to_send": 2}}),
            (1, 2), "both at once, counted apart")

    # A solo board has no sharing at all and must not start warning.
    c.equal(owed({"queued": {"count": 0}, "sharing": None}), (0, 0),
            "a board nobody is sharing owes nothing to nobody")

    return c.report()


CHECKS = (check_agreed_at, check_health_guard, check_summary_stamp,
          check_a_given_up_change_is_not_pending_for_ever,
          check_the_attempt_limit_is_one_number,
          check_closing_knows_about_the_shared_board,
          check_publish_leaves_a_card_the_channel_moved,
          check_publish_still_sends_our_own_changes,
          check_a_long_board_keeps_every_row,
          check_the_pages_follow_the_board)
