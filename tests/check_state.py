"""
The board says how it is honestly.

`synced_at` is the publish -- this machine pushing its own view -- and it
advances every cycle whether or not anything is coming back. Measuring contact
with it reported "in step" with the sync loop stopped. `agreed_at` is written
by the pull alone, which is the direction their changes arrive in.

The summary message is held to the same standard: it says when it was
published, because one machine's write time is all it can honestly know.
"""

import ast
import dataclasses
import time
import json
import pathlib

NL = chr(10)
import sqlite3

from support import Board, Check, FakeDiscord, PARENT, iso

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


def check_a_card_says_it_holds_an_unsent_change() -> bool:
    """
    The mark on a card, and the warning on close, count the same two debts.

    `unsent` is events waiting out their undo window before Ernie posts them
    to the customer thread. `unshared` is the card having moved since the
    shared board was last published -- and a reorder, or any band move that is
    not in or out of critical, is silent by design and appears only there. A
    mark that read the first alone would leave a card somebody had just
    dragged looking as though it had already gone out, while the close warning
    said otherwise. One of the two would be lying.
    """
    c = Check("a card says when it holds a change that has not left here")

    with Board() as b:
        api.DB = b.path
        quiet = b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 respool")
        edited = b.card("OPS: Munhall - 26Aug26 - 1k reel", "high", 1000.0)
        dragged = b.card("OPS: Baldwin - 02Sep26 - Gooseneck 10in", "high", 2000.0)

        # Everything settled: the cards were written, then published.
        b.con.execute("UPDATE cards SET updated_at=?", (iso(-120),))
        for tid in (quiet, edited, dragged):
            b.con.execute(
                """INSERT INTO state_sync (thread_id, message_id, base_json,
                                           synced_at) VALUES (?,?,?,?)""",
                (tid, f"msg-{tid}", "{}", iso(-60)))
        b.con.commit()

        def board():
            return {x["thread_id"]: x for x in
                    api.cards(queue=None, client=None,
                              include_completed=False)["cards"]}

        c.equal(bert.unsent_mark(board()[quiet]), None,
                "a card owing nothing wears nothing")

        # An edit, still inside its undo window.
        b.event(edited, verb="edited", dispatch_after=iso(+40))
        # A drag. Silent by design -- no dispatch_after at all -- so the card
        # row moving is the only trace of it.
        b.event(dragged, verb="reordered", old="high:5", new="high:3")
        b.con.execute("UPDATE cards SET updated_at=? WHERE thread_id=?",
                      (iso(), dragged))
        b.con.commit()

        seen = board()
        c.equal((bert.unsent_mark(seen[edited]) or [None])[0], "*",
                "an edit waiting to post is marked")
        c.equal(seen[dragged]["unsent"], 0,
                "a reorder queues nothing for the thread")
        c.equal((bert.unsent_mark(seen[dragged]) or [None])[0], "*",
                "and is marked anyway, off the shared board it has not reached")

        # The board-wide warning has to agree with what the cards are wearing.
        owed = bert.Bert._owed(api.health())
        c.equal(owed, (1, 1), "the close warning owes the same two")

        # Sent and published: both clear.
        b.con.execute("UPDATE events SET posted_at=? WHERE thread_id=?",
                      (iso(), edited))
        b.con.execute("UPDATE state_sync SET synced_at=? WHERE thread_id=?",
                      (iso(), dragged))
        b.con.commit()
        seen = board()
        c.ok(all(bert.unsent_mark(seen[t]) is None
                 for t in (quiet, edited, dragged)),
             "and every mark clears once it has gone out")

    return c.report()


def check_a_given_up_change_is_not_about_to_send() -> bool:
    """
    A card the outbox has given up on must not wear the waiting mark.

    /health already keeps `stuck` apart from `queued`, because leaving the
    stack running will not send those and a warning saying "wait a moment"
    would be advising the one thing that cannot help. A star that never
    cleared would be making exactly that promise on the card instead.
    """
    c = Check("a given-up change is not drawn as about to send")

    with Board() as b:
        api.DB = b.path
        tid = b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 respool")
        b.con.execute("UPDATE cards SET updated_at=?", (iso(-120),))
        b.con.execute(
            """INSERT INTO state_sync (thread_id, message_id, base_json,
                                       synced_at) VALUES (?,?,?,?)""",
            (tid, "msg-1", "{}", iso(-60)))
        eid = b.event(tid, verb="edited", dispatch_after=iso(-600))
        b.con.commit()

        def mark():
            card = api.cards(queue=None, client=None,
                             include_completed=False)["cards"][0]
            return bert.unsent_mark(card)

        got = mark()
        c.equal((got or [None])[0], "*", "while it is still being tried")
        # The ink, not the accent: three times the contrast against a card,
        # measured, and it spends no colour a tag might want.
        c.equal(got[1] if got else None, bert.T.INK,
                "and drawn in the ink rather than the accent")

        b.con.execute("UPDATE events SET attempts=? WHERE event_id=?",
                      (api.OUTBOX_MAX_ATTEMPTS, eid))
        b.con.commit()
        got = mark()
        c.equal((got or [None])[0], "!", "past the limit it reads differently")
        c.ok("not retry" in (got[2] if got else ""),
             "and says it will not go on its own")
        c.equal(api.health()["queued"]["count"], 0,
                "matching /health, which stops counting it as owed")

    return c.report()


def check_a_ticked_item_still_reaches_the_board() -> bool:
    """Ticking a work item must not delete it from the card.

    /cards filtered on done_at IS NULL, so a ticked bubble disappeared -- and
    the bubble was the only thing on the card saying that work had been done.
    Removed ones do stay gone: an x in the editor says "this should not be
    here", which is a different statement from "this is finished".
    """
    c = Check("a ticked work item still reaches the board")

    with Board() as b:
        api.DB = b.path
        tid = b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 respool")
        for n, (item, done, removed) in enumerate((
                ("Chase the courier", None, None),
                ("Return Equipment", iso(-60), None),
                ("Book the van", None, iso(-30)))):
            b.con.execute(
                """INSERT INTO work_items (item_id, thread_id, body, position,
                                           created_at, done_at, removed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (f"w{n}", tid, item, float(n), iso(-600), done, removed))
        b.con.commit()

        got = api.cards(queue=None, client=None,
                        include_completed=False)["cards"][0]["work_items"]
        bodies = {i["body"]: i["done"] for i in got}
        c.equal(bodies, {"Chase the courier": False, "Return Equipment": True},
                "the ticked one comes too, flagged; the removed one does not")
    return c.report()


def check_reopening_a_work_item_round_trips() -> bool:
    """A finished bubble can be put back, and that can itself be undone.

    Reopening rides in the batched save with everything else the editor
    changes, so it also has to count as a change: without it in `touched`,
    edit_card updated the row and then returned before the commit -- the
    bubble came back ticked and no event was written, silently.
    """
    c = Check("reopening a work item round-trips")

    with Board() as b:
        api.DB = b.path
        tid = b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 respool")
        for n, (body, done) in enumerate((("Chase the courier", None),
                                          ("Return Equipment", iso(-60)))):
            b.con.execute(
                """INSERT INTO work_items (item_id, thread_id, body, position,
                                           created_at, done_at)
                   VALUES (?,?,?,?,?,?)""",
                (f"w{n}", tid, body, float(n), iso(-600), done))
        b.con.commit()

        def state():
            card = api.cards(queue=None, client=None,
                             include_completed=False)["cards"][0]
            return {i["body"]: i["done"] for i in card["work_items"]}

        c.equal(state()["Return Equipment"], True, "it starts finished")

        r = api.edit_card(tid, api.EditBody(actor="Tester", work_undone=["w1"]))
        c.equal(state()["Return Equipment"], False, "reopening puts it back")
        c.ok("reopened" in (r.get("summary") or ""),
             f"and the feed says so  ({r.get('summary')!r})")

        # Asserted before it is used: without the event there is nothing to
        # undo, and a check that raises takes the whole run down rather than
        # reporting one failure.
        c.ok(r.get("event_id"), "the reopen is an event, so it can be undone")
        if r.get("event_id"):
            api.undo(r["event_id"], api.ActorBody(actor="Tester"))
            c.equal(state()["Return Equipment"], True,
                    "and undoing the reopen finishes it again")

        # An x still works on a finished one: removing says it should not be
        # on the card at all, which the tick does not say.
        api.edit_card(tid, api.EditBody(actor="Tester", work_remove=["w1"]))
        c.ok("Return Equipment" not in state(),
             "a finished bubble can still be removed outright")
    return c.report()


def check_the_client_list_says_when_it_has_gone_stale() -> bool:
    """A pull that has stopped shows up nowhere else.

    ernie_sync catches the failure, writes a line to the log and carries on,
    and the Client dropdown goes on offering whatever it last knew. The list
    changes rarely, so its age is not news -- what is news is hours of it.
    """
    c = Check("the client list says when it has gone stale")

    tick = bert.Bert._tick_roster
    seen = {}

    class W:
        health_at = time.time()

        class roster_age:
            @staticmethod
            def hide(): seen["shown"] = False
            @staticmethod
            def show(): seen["shown"] = True
            @staticmethod
            def setText(t): seen["text"] = t
            @staticmethod
            def setStyleSheet(t): pass
            @staticmethod
            def setToolTip(t): seen["tip"] = t

    for label, health, shown in (
            ("no Jira configured", {"clients": None}, False),
            ("never pulled", {"clients": {"seconds_since_sync": None}}, False),
            ("an hour ago", {"clients": {"seconds_since_sync": 3600}}, False),
            ("six hours exactly", {"clients": {"seconds_since_sync": 6 * 3600}}, False),
            ("nine hours ago", {"clients": {"seconds_since_sync": 9 * 3600}}, True)):
        seen.clear()
        W.health = health
        tick(W)
        c.equal(seen.get("shown"), shown, label)

    c.ok("client list" in (seen.get("text") or ""),
         f"and it names what is stale  ({seen.get('text')!r})")
    c.ok("Jira" in (seen.get("tip") or "") and "logs" in (seen.get("tip") or ""),
         "with somewhere to go about it")
    return c.report()


def check_a_ticket_started_in_bert_becomes_a_thread() -> bool:
    """Bert cannot make a Discord thread, so it asks for one and waits.

    Every write to Discord goes through Discord.write, which lives in the
    outbox. So a ticket started on the board is a row in new_threads until
    the outbox picks it up -- and it shows on the board during that gap
    wearing the unsent mark, because that is what it is.

    The outbox writes the mirror rows itself rather than leaving them to the
    sync. Waiting would put the card on the board a cycle later and in
    unassigned, losing the band somebody chose by pressing the + in it.
    """
    c = Check("a ticket started in Bert becomes a thread")

    with Board() as b:
        api.DB = b.path
        b.con.execute("INSERT INTO watched_channels (channel_id, name, mirror,"
                      " generate_cards) VALUES (?,?,1,1)",
                      (PARENT, "customer-threads"))
        b.con.commit()

        api.new_ticket(api.NewTicketBody(
            actor="Bella Fiore", priority="high",
            title="PROD: Trekk - 08Sep26 - EReel-1220 fiber respool",
            work_add=["Chase the courier"],
            first_message="Reel came back with the fiber snapped."))

        def board():
            return api.cards(queue=None, client=None,
                             include_completed=False)["cards"]

        card = board()[0]
        c.ok(card.get("pending"), "it is on the board straight away")
        c.equal(card["priority"], "high", "in the band it was started in")
        c.equal(card["unsent"], 1, "wearing the unsent mark")
        c.equal([i["body"] for i in card["work_items"]], ["Chase the courier"],
                "with the work items typed into it")

        d = FakeDiscord()
        got = outbox.make_threads(b.con, d)
        c.equal(got, {"made": 1, "failed": 0}, "the outbox makes the thread")

        paths = [p for _, p, _ in d.calls]
        c.ok(paths[0].endswith("/threads"), "the thread first")
        said = [t for _, _, t in d.calls if t]
        c.ok(any("Bella Fiore" in t for t in said),
             "then a note naming who started it, since the bot opened it")
        c.ok(any("fiber snapped" in t for t in said),
             "then their own opening message")

        card = board()[0]
        c.ok(not card.get("pending"), "and it is a real card afterwards")
        c.equal(card["priority"], "high", "still in the band it was started in")
        c.equal([i["body"] for i in card["work_items"]], ["Chase the courier"],
                "still carrying its work")
    return c.report()


def a_channel_one_version_ahead(cards):
    """The channel as written by a machine on a newer wire format."""
    out = {}
    for c in cards:
        p = dict(c.payload())
        p["v"] = S.FORMAT_VERSION + 1
        out[c.thread_id] = {"message_id": f"m-{c.thread_id}", "payload": p}
    return out


def skew_row(b):
    return b.con.execute("SELECT * FROM state_format_skew WHERE id=1").fetchone()


def check_a_payload_we_cannot_read_is_written_down() -> bool:
    """
    The skip has to reach the board, not a printout nobody runs.

    reconcile() drops a payload whose v is not ours and carries on. That is
    the right thing to do with it -- we cannot read it -- but it means the
    card stops being compared in either direction, so the two boards drift
    apart and neither says a word. It went into a --pull listing and nowhere
    else.
    """
    c = Check("a payload this build cannot read is written down")

    with Board() as b:
        cards = a_board(b)
        S.fetch_state = lambda d, cid: a_channel_one_version_ahead(cards)
        r = S.reconcile(None, "chan", b.path)

        c.equal(len(r["format_skew"]), len(cards), "every card is skipped")
        c.equal(r["applied"], [], "and none of them applied")
        c.equal(r["unknown"], [],
                "kept apart from the cards merely waiting on a thread")

        row = skew_row(b)
        c.ok(row is not None, "the pull writes it down")
        if row:
            c.equal(row["their_v"], str(S.FORMAT_VERSION + 1),
                    "naming the format it could not read")
            c.equal(row["our_v"], S.FORMAT_VERSION, "and the one it speaks")
            c.equal(row["cards"], len(cards), "and how many it cost")

    return c.report()


def check_the_warning_clears_itself() -> bool:
    """
    It has to come down on its own the cycle after somebody updates.

    The old messages sit in the channel until that machine republishes, so the
    row is rewritten every cycle for as long as it is true -- and a pull that
    skips nothing has to delete it, or the board goes on warning about a
    problem that is over and the warning stops meaning anything.
    """
    c = Check("the warning clears itself once everybody has updated")

    with Board() as b:
        cards = a_board(b)
        S.fetch_state = lambda d, cid: a_channel_one_version_ahead(cards)
        S.reconcile(None, "chan", b.path)
        c.ok(skew_row(b) is not None, "seen once, recorded")

        S.fetch_state = lambda d, cid: as_channel(cards)
        r = S.reconcile(None, "chan", b.path)
        c.equal(r["format_skew"], [], "a clean pull skips nothing")
        c.ok(skew_row(b) is None, "and takes the warning down")

    return c.report()


def check_the_ordinary_skip_is_not_an_alarm() -> bool:
    """
    A card whose thread has not synced here yet is normal and clears itself.

    Both used to land in one list called "skipped". They are opposites: this
    one is a later cycle away from fixing itself, and the other will not fix
    itself at all. Reported together, the one that mattered was buried under
    the one that never does.
    """
    c = Check("a card waiting on its thread raises no alarm")

    with Board() as b:
        cards = a_board(b)
        channel = as_channel(cards)
        stranger = dict(cards[0].payload())
        channel["999999"] = {"message_id": "m-999999", "payload": stranger}
        S.fetch_state = lambda d, cid: channel

        r = S.reconcile(None, "chan", b.path)
        c.equal(len(r["unknown"]), 1, "the stranger is noted as waiting")
        c.equal(r["format_skew"], [], "and is not called a version problem")
        c.ok(skew_row(b) is None, "so no warning is written")

    return c.report()


def check_a_dry_run_writes_no_warning() -> bool:
    """--dry-run reports; it does not change what the board says."""
    c = Check("a dry run writes no warning")

    with Board() as b:
        cards = a_board(b)
        S.fetch_state = lambda d, cid: a_channel_one_version_ahead(cards)
        r = S.reconcile(None, "chan", b.path, dry_run=True)
        c.ok(r["format_skew"], "it still reports what it found")
        c.ok(skew_row(b) is None, "but writes nothing down")

    return c.report()


def check_health_reports_it_with_nothing_shared() -> bool:
    """
    The machine that can read none of the channel has no state_sync rows.

    It applied nothing, so nothing recorded a base -- which means `sharing` is
    None and Bert's indicator hides. That machine is exactly the one that
    needs telling, so the block is reported on its own.
    """
    c = Check("/health reports it even with nothing shared")

    with Board() as b:
        cards = a_board(b)
        S.fetch_state = lambda d, cid: a_channel_one_version_ahead(cards)
        S.reconcile(None, "chan", b.path)

        api.DB = b.path
        h = api.health()
        c.ok(h.get("sharing") is None,
             "nothing was applied, so there is no shared board to report")
        skew = h.get("format_skew")
        c.ok(skew is not None, "and the skew is reported anyway")
        if skew:
            c.equal(skew["their_v"], str(S.FORMAT_VERSION + 1), "their format")
            c.equal(skew["our_v"], S.FORMAT_VERSION, "ours")
            c.equal(skew["cards"], len(cards), "and how many cards it holds")
            c.ok(skew["seconds_since_seen"] >= 0, "with an age")

    return c.report()


def check_bert_says_who_has_to_update() -> bool:
    """
    "Update" is only useful if it says which machine.

    The version arrives as whatever was in the payload rather than as a
    number, so a malformed one still has to produce a sentence.
    """
    c = Check("Bert says which of the two machines has to update")

    c.ok("this machine" in bert.who_is_behind(2, 1),
         "a newer channel means this one is behind")
    c.ok("other machine" in bert.who_is_behind(1, 2),
         "an older channel means they are")
    for bad in (None, "x", ""):
        c.ok(bert.who_is_behind(bad, 1),
             f"{bad!r} still gets a sentence rather than a crash")

    # And it is asked before the guard that hides the indicator, or the
    # machine with no state_sync rows never sees it.
    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tick = src.split("def _tick_sharing")[1].split(NL + "    def ")[0]
    c.ok("format_skew" in tick, "the indicator asks about it")
    c.ok(tick.index("format_skew") < tick.index("self.shared.hide()"),
         "before it decides there is nothing shared to report")

    return c.report()


def check_closing_asks_about_an_unsaved_editor() -> bool:
    """
    Closing Bert over an open editor threw the work away without a word.

    The close warning counted two debts, and both are about Discord: changes
    queued behind the undo window, and cards the shared board has not been
    told about. Neither is lost by closing -- the outbox posts them whether
    Bert is open or not, which is why that warning is about shutting the
    *stack* down.

    An editor nobody has saved is the opposite. It is gone the moment the
    window shuts, it is the only one of the three that is lost with the stack
    already down, and it was the only one not asked about. Measured: an edit
    in progress and a part-written new ticket both closed silently.
    """
    c = Check("closing asks about an editor nobody has saved")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")

    def method(name):
        return next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                     and n.name == name), None)

    ask = method("_editor_may_close")
    c.ok(ask is not None, "there is something that asks")

    close = method("closeEvent")
    body_src = ast.get_source_segment(src, close) or "" if close else ""
    c.ok("_editor_may_close" in body_src, "and closeEvent asks it")

    # Before `connected` is read. The other two debts are only worth a warning
    # while Ernie is reachable; this one is lost either way, so a stack that
    # is already down must not skip the question.
    if "_editor_may_close" in body_src and "self.connected" in body_src:
        c.ok(body_src.index("_editor_may_close") < body_src.index("self.connected"),
             "before connectivity, because this loss is local")

    # Keeping the editor open is the default, being the one that loses nothing.
    ask_src = ast.get_source_segment(src, ask) or "" if ask else ""
    c.ok("setDefaultButton(stay)" in ask_src,
         "and staying is the default, as it is on the other editor dialog")

    return c.report()


def check_a_write_says_whether_it_landed() -> bool:
    """
    "Save and close" must not close on top of a save that failed.

    Card.save() puts the card back in view mode *before* the write, so the
    editor being shut says nothing about whether the write landed -- the first
    guard here read editing_card and was therefore always satisfied. Measured
    with the API down: the save failed, "Couldn't save" appeared, and Bert
    closed anyway, taking the error box with it.

    So the two write paths answer for themselves.
    """
    c = Check("a write says whether it landed")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def fn(cls_name, name):
        cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                    and n.name == cls_name), None)
        if cls is None:
            return None
        return next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                     and n.name == name), None)

    for cls_name, name in (("Bert", "save_edits"), ("Bert", "create_ticket"),
                           ("Card", "save")):
        f = fn(cls_name, name)
        c.ok(f is not None, f"{cls_name}.{name} is there")
        if f is None:
            continue
        returns = [n for n in ast.walk(f) if isinstance(n, ast.Return)]
        c.ok(returns and all(r.value is not None for r in returns),
             f"{cls_name}.{name} answers on every path, never a bare return")
        if cls_name == "Bert":
            c.ok(any(isinstance(r.value, ast.Constant) and r.value.value is False
                     for r in returns),
                 f"{cls_name}.{name} says False when it did not land")

    return c.report()


CHECKS = (check_agreed_at, check_health_guard, check_summary_stamp,
          check_a_given_up_change_is_not_pending_for_ever,
          check_the_attempt_limit_is_one_number,
          check_closing_knows_about_the_shared_board,
          check_publish_leaves_a_card_the_channel_moved,
          check_publish_still_sends_our_own_changes,
          check_a_long_board_keeps_every_row,
          check_the_pages_follow_the_board,
          check_a_card_says_it_holds_an_unsent_change,
          check_a_given_up_change_is_not_about_to_send,
          check_a_ticked_item_still_reaches_the_board,
          check_reopening_a_work_item_round_trips,
          check_the_client_list_says_when_it_has_gone_stale,
          check_a_payload_we_cannot_read_is_written_down,
          check_the_warning_clears_itself,
          check_the_ordinary_skip_is_not_an_alarm,
          check_a_dry_run_writes_no_warning,
          check_health_reports_it_with_nothing_shared,
          check_bert_says_who_has_to_update,
          check_closing_asks_about_an_unsaved_editor,
          check_a_write_says_whether_it_landed,
          check_a_ticket_started_in_bert_becomes_a_thread)
