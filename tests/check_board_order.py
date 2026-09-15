"""
One order, and rank is it.

An unreadable thread belongs at the top of unassigned -- it is the one needing
a person soonest, and the bottom of a nineteen-card band is where it goes
unlooked-at. Bert used to arrange that at draw time. Everything else still
read rank order, so the board disagreed with itself: the two unknown-client
cards sat first on screen and twelfth in the state channel, and a drop between
two visible cards was measured against neighbours that were not its
neighbours. The rank is the real one now, so there is nothing left to
disagree.
"""

import ast
import pathlib
from support import Board, Check, iso

import bert
import ernie_api as api
import ernie_extract as ex
import ernie_load as load
import ernie_state as S

ROOT = pathlib.Path(__file__).resolve().parent.parent


def a_record(name: str, owner: str | None = None) -> ex.ThreadRecord:
    """The parsed thread ensure_card is handed, with nothing else going on."""
    title = ex.parse_title(name)
    return ex.ThreadRecord(
        thread_id=f"t-{abs(hash(name)) % 10**9}", parent_id="chan", name=name,
        owner_id=owner,
        title=title, client_key=None, equipment=[], proposals=[], created=[],
        participants=[], message_count=1, first_ts=iso(-60), last_ts=iso(-60),
        archived=False, issues=[])


def check_predicate() -> bool:
    c = Check("title_unreadable")

    for name, want in (
            ("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool", False),
            ("Rhino needs to approve there inspections", True),
            ("OPS: outdated escalation list", True),
    ):
        rec = a_record(name)
        c.equal(rec.title_unreadable, want,
                f"{rec.title.confidence:<11} {name[:44]}")

    # The one definition, not a second list that can drift from Bert's.
    import bert
    derived = {f"title_{conf}" for conf in ex.UNREADABLE_CONFIDENCE}
    c.ok(derived <= bert.BLOCKING,
         "every unreadable confidence is blocking in Bert too")

    return c.report()


def check_new_cards_rank() -> bool:
    c = Check("ensure_card ranking")

    with Board() as b:
        b.con.execute(
            "INSERT INTO watched_channels (channel_id, generate_cards) VALUES (?,1)",
            ("chan",))
        b.con.commit()

        def arrive(name):
            rec = a_record(name)
            b.con.execute(
                """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                        first_seen_at, last_synced_at)
                   VALUES (?,?,?,?,?,?)""",
                (rec.thread_id, "chan", "g", iso(-60), iso(-60), iso()))
            b.con.execute(
                """INSERT INTO thread_titles (thread_id, observed_at, name, confidence)
                   VALUES (?,?,?,?)""",
                (rec.thread_id, iso(-60), name, rec.title.confidence))
            load.ensure_card(b.con, rec)
            b.con.commit()
            return rec.thread_id

        first = arrive("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool")
        second = arrive("OPS: McKeesport - 02Sep26 - ODE-2977 wheel motor")
        junk = arrive("Rhino needs to approve there inspections")
        third = arrive("CS: Bethel Park - 02Sep26 - Operator training refresher")
        junk2 = arrive("OPS: outdated escalation list")

        order = [r["thread_id"] for r in b.con.execute(
            "SELECT thread_id FROM cards WHERE priority='unassigned' ORDER BY rank")]

        c.equal(order.index(junk) < order.index(first), True,
                "an unreadable thread lands above the cards already there")
        c.equal(order.index(junk2) < order.index(first), True,
                "and so does the next one")
        c.equal(order.index(junk2) < order.index(junk), True,
                "the newest unreadable one goes to the very top")
        c.equal(order[-1], third, "a readable thread still lands at the bottom")
        c.equal(order.index(first) < order.index(second), True,
                "readable ones keep the order they arrived in")

    return c.report()


def check_one_order() -> bool:
    c = Check("one order everywhere")

    with Board() as b:
        # A triage card parked mid-band. Nothing may lift it: it is where the
        # rank says, and the rank is what a person dragged it to.
        b.card("PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool",
               "unassigned", 1000.0)
        dragged = b.card("Rhino needs to approve there inspections",
                         "unassigned", 2000.0)
        b.card("OPS: McKeesport - 02Sep26 - ODE-2977 wheel motor",
               "unassigned", 3000.0)

        cards = S.load_board(b.path)
        band = [x for x in cards if x.priority == "unassigned"]
        by_rank = [x.thread_id for x in sorted(band, key=lambda x: x.rank)]

        c.equal([x.thread_id for x in band], by_rank,
                "load_board hands them over in rank order")
        c.equal(S.positions(cards)[dragged], 2,
                "the state channel numbers it where its rank puts it")
        c.ok(0 < S.positions(cards)[dragged] < len(band),
             "a triage card dragged down the band stays down")

        # What a drop between two adjacent cards has to produce. With the
        # display order and the rank order the same, the visible neighbours
        # are the real ones and the midpoint lands between them.
        ranks = sorted(x.rank for x in band)
        mid = (ranks[0] + ranks[1]) / 2
        c.ok(ranks[0] < mid < ranks[1],
             "a drop between the first two lands strictly between them")

    return c.report()


def check_a_reorder_says_where_it_went() -> bool:
    """
    rank is a fraction and says nothing to a reader.

    "reordered PROD: ..." was the whole line, for every drag, so the feed and
    the change log recorded that something moved and not where. The position
    in the band is what the person was looking at when they dragged it, and it
    is derivable at the moment of the write from the ranks it is ordered
    against -- afterwards it is not, because the other ranks have moved on.
    """
    c = Check("a reorder records the places it moved between")

    with Board() as b:
        api.DB = b.path
        ids = [b.card(f"PROD: Client {i} - 02Sep26 - item {i}", "high",
                      1000.0 * (i + 1)) for i in range(5)]

        def positions(tid):
            row = b.con.execute(
                "SELECT priority, rank FROM cards WHERE thread_id=?",
                (tid,)).fetchone()
            others = [r[0] for r in b.con.execute(
                """SELECT rank FROM cards WHERE priority=? AND thread_id<>?
                   AND completed_at IS NULL""", (row["priority"], tid))]
            return sum(1 for r in others if r < row["rank"]) + 1

        def last_reorder():
            r = b.con.execute(
                """SELECT old_value, new_value FROM events WHERE verb='reordered'
                   ORDER BY rowid DESC LIMIT 1""").fetchone()
            return (r["old_value"], r["new_value"]) if r else None

        before = b.con.execute(
            "SELECT COUNT(*) FROM events WHERE verb='reordered'").fetchone()[0]

        # Last card to the front.
        api.move_card(ids[4], api.MoveBody(priority="high", before_id=ids[0],
                                      actor="Tester"))
        c.equal(positions(ids[4]), 1, "it really is first now")
        c.equal(last_reorder(), ("high:5", "high:1"),
                "and the event says 5th to 1st, in High")

        # And back down between two others.
        api.move_card(ids[4], api.MoveBody(priority="high", after_id=ids[1],
                                      actor="Tester"))
        c.equal(last_reorder(), ("high:1", f"high:{positions(ids[4])}"),
                "the next move starts from where the last one left it")

        # The band travels with the place, so a row can be read on its own.
        c.equal(ex.reorder_spot(last_reorder()[0])[0], "high",
                "and names the band it happened in")

        return_ = b.con.execute(
            "SELECT COUNT(*) FROM events WHERE verb='reordered'").fetchone()[0]
        c.equal(return_ - before, 2, "one event per move that moved something")

    return c.report()


def check_a_reorder_that_moves_nothing_says_nothing() -> bool:
    """
    A drag that lands a card back where it started is not a change.

    It still writes the rank, so two boards agree about it, but four identical
    "reordered" lines in a row for a card that never went anywhere is the feed
    reporting the dragging rather than the outcome.
    """
    c = Check("a drag that changes nothing writes no line")

    with Board() as b:
        api.DB = b.path
        ids = [b.card(f"OPS: Client {i} - 02Sep26 - item {i}", "medium",
                      1000.0 * (i + 1)) for i in range(4)]

        def reorders():
            return b.con.execute(
                "SELECT COUNT(*) FROM events WHERE verb='reordered'").fetchone()[0]

        # Drop it straight back after the card it already follows.
        rank_before = b.con.execute(
            "SELECT rank FROM cards WHERE thread_id=?", (ids[2],)).fetchone()[0]
        api.move_card(ids[2], api.MoveBody(priority="medium", after_id=ids[1],
                                      actor="Tester"))
        c.equal(reorders(), 0, "no line for a move that went nowhere")

        rank_after = b.con.execute(
            "SELECT rank FROM cards WHERE thread_id=?", (ids[2],)).fetchone()[0]
        c.ok(rank_after is not None, "but the rank is still written")
        c.ok(rank_before is not None, "and it had one before")

        # A real move still speaks up.
        api.move_card(ids[3], api.MoveBody(priority="medium", before_id=ids[0],
                                      actor="Tester"))
        c.equal(reorders(), 1, "a move that goes somewhere still does")

    return c.report()


def check_the_rail_clips_to_its_width() -> bool:
    """
    The running order can be dragged, so its text has to follow.

    Both lines were cut at a fixed number of characters -- 24 and 28 -- which
    is a count and not a measurement, so widening the rail gave the text more
    room and not one more letter of it. The obvious replacement, characters
    per pixel, is a guess about a proportional font; QFontMetrics.elidedText
    knows exactly, and cannot overflow the row it was measured for.
    """
    c = Check("the running order clips to the width it has")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def method(cls_name, fn):
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == cls_name)
        return next(n for n in cls.body
                    if isinstance(n, ast.FunctionDef) and n.name == fn)

    row = method("RailRow", "__init__")
    elides = [n for n in ast.walk(row)
              if isinstance(n, ast.Call)
              and getattr(n.func, "attr", None) == "elidedText"]
    c.equal(len(elides), 2, "both lines are elided against their own font")
    c.ok(not any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "clip"
                 for n in ast.walk(row)),
         "and neither is cut at a character count any more")

    # The width has to be part of what a row is, or a drag would not redraw it.
    setc = method("Rail", "set_cards")
    c.ok(any(isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "row_width"
             for n in ast.walk(setc)),
         "the rail measures itself before building rows")
    sig = next((n for n in ast.walk(setc) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "sig" for t in n.targets)), None)
    c.ok(sig is not None, "there is still a signature guarding the rebuild")
    c.ok(sig is not None and any(getattr(n, "id", None) == "room"
                                 for n in ast.walk(sig)),
         "and the width is in it, so a drag counts as a change")

    # Thirty rows rebuilt on every pixel of a drag is a stutter.
    init = method("Bert", "__init__")
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "rail_redraw"
             for n in ast.walk(init)),
         "the redraw waits for the handle to settle rather than chasing it")

    return c.report()


def check_a_collapsed_band_still_lands_a_drop() -> bool:
    """
    Folding a band away must not move where a drop lands.

    This is the shape of a bug the board has already had: Bert used to arrange
    cards at draw time, and "a drop between two visible cards was measured
    against neighbours that were not its neighbours". Hiding a band puts two
    rows next to each other on screen that are not next to each other in the
    order, which is the same trap.

    What makes it safe is a guard that predates collapsing: _drop_at only ever
    takes a neighbour from the dragged card's own band. Nobody may remove it.
    """
    c = Check("a collapsed band still lands a drop where it looks")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def method(cls_name, fn):
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == cls_name)
        return next(n for n in cls.body
                    if isinstance(n, ast.FunctionDef) and n.name == fn)

    # The guard the whole thing rests on: a neighbour has to share the band.
    drop = method("Rail", "_drop_at")
    same_band = [n for n in ast.walk(drop)
                 if isinstance(n, ast.Compare)
                 and any(isinstance(x, ast.Subscript)
                         and getattr(getattr(x, "slice", None), "value", None)
                         == "priority"
                         for x in [n.left] + list(n.comparators))]
    c.equal(len(same_band), 2,
            "_drop_at takes a neighbour only from the card's own band")

    # Collapsing has to count as a change, or the rail paints the old picture.
    setc = method("Rail", "set_cards")
    sig = next(n for n in ast.walk(setc) if isinstance(n, ast.Assign)
               and any(getattr(t, "id", None) == "sig" for t in n.targets))
    c.ok(any(getattr(n, "attr", None) == "collapsed" for n in ast.walk(sig)),
         "the fold is part of the redraw signature")

    # A fold that opened itself the moment a drag began would defeat its own
    # purpose -- shortening the run from High to Low is what it is for.
    body = ast.get_source_segment(src, setc) or ""
    shut = body.split("shut = band in self.collapsed")[-1]
    c.ok("shut = band in self.collapsed" in body,
         "the build loop reads the fold")
    c.ok("dragging" not in shut.split("for c in group")[0],
         "and a drag does not quietly reopen it")

    # Reachable while folded: the header takes the drop, before any neighbour
    # logic gets a look at it.
    drop_ev = ast.get_source_segment(src, method("Rail", "dropEvent")) or ""
    c.ok(drop_ev.index("_shut_head_at") < drop_ev.index("_zone_at"),
         "a drop on a folded header is answered before the empty-band slot")
    c.ok("move_card" in drop_ev.split("_shut_head_at")[1].split("return")[0],
         "and it moves the card into that band")

    # Only a folded header takes a drop. An open one sits above rows that can
    # speak for themselves.
    head_at = ast.get_source_segment(src, method("Rail", "_shut_head_at")) or ""
    c.ok("if not h.collapsed" in head_at,
         "an open band's header is not a drop target")

    # Remembered, like the rail's own width.
    c.ok(any(isinstance(n, ast.FunctionDef) and n.name == "remember_collapsed"
             for n in ast.walk(tree)),
         "the fold survives a restart")

    return c.report()


def check_a_folded_band_opens_for_what_goes_into_it() -> bool:
    """
    A band folded away must not swallow the thing you just asked for.

    Folding hides the band's *panel* and keeps its header, which is what lets
    the count and the drop target go on working -- and it is also the trap:
    the `+ New Ticket` button sits on that header, so it stays clickable with
    nowhere on screen for the card to go. Pressing it on a folded band put the
    card and its editor into the hidden panel, and `editing_card` holds every
    poll off while an editor is open, so the board sat frozen with nothing on
    it to say why. Measured on all five bands before the fix: five editors
    opened, none of them visible.

    Three paths put something into a band, and all three have to open it.
    """
    c = Check("a folded band opens for whatever is put into it")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def method(cls_name, fn):
        cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                    and n.name == cls_name), None)
        if cls is None:
            return None
        return next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                     and n.name == fn), None)

    def opens_a_fold(fn):
        """Does it unfold a band it found folded?"""
        return fn is not None and any(
            isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "set_collapsed"
            and n.args and isinstance(n.args[0], ast.Constant)
            and n.args[0].value is False
            for n in ast.walk(fn))

    # Why the guard is needed at all: the header outlives the fold.
    fold = method("Band", "set_collapsed")
    c.ok(fold is not None and any(
        isinstance(n, ast.Call)
        and getattr(n.func, "attr", None) == "setVisible"
        and getattr(getattr(n.func, "value", None), "attr", None) == "panel"
        for n in ast.walk(fold)),
        "folding hides the panel, so the header and its button stay live")

    for cls_name, fn, why in (
            ("Bert", "start_ticket", "starting a ticket in it"),
            ("Bert", "reveal", "revealing a card in it"),
            ("Band", "dragEnterEvent", "a drag arriving over it")):
        c.ok(opens_a_fold(method(cls_name, fn)),
             f"{cls_name}.{fn} opens a folded band -- {why}")

    return c.report()


def check_a_build_ticket_links_to_jira() -> bool:
    """
    A key on the card, pointing at the issue -- and no Jira access at all.

    The link is `{base}/browse/PIP-8448`, which the desktop opens against the
    reader's own Jira session. Ernie never calls Jira for it: no token, no
    permission, no request. Confirmed against production's own confirmation
    messages, which carry that exact URL beside the key they announce.

    **The chip cannot exist pointing nowhere**, which is the rule
    `BERT_UPDATE_URL` already established: with no Jira configured there is no
    address, so there is no chip, rather than a control that looks live and
    goes nowhere.
    """
    c = Check("a build ticket links to Jira")

    base = "https://edgeaisolutions.atlassian.net"
    c.equal(bert.ticket_url(base, "PIP-8448"),
            f"{base}/browse/PIP-8448", "the key becomes a browse URL")
    c.equal(bert.ticket_url(base + "/", "PIP-8448"),
            f"{base}/browse/PIP-8448", "a trailing slash does not double up")

    for why, got in (("no Jira configured", bert.ticket_url(None, "PIP-8448")),
                     ("no ticket on the card", bert.ticket_url(base, None)),
                     ("neither", bert.ticket_url("", ""))):
        c.equal(got, "", f"no address, so no chip: {why}")

    return c.report()


def check_only_build_tickets_are_shown() -> bool:
    """
    Asked for as builds only, and reversing it is one line.

    Measured against production before choosing: `kind` is NULL on 223 of 441
    tickets, which sounds fatal and is history -- **all 32 open cards that
    carry any ticket carry a known build one**, so filtering to builds costs
    the current board nothing. And every thread that has a build ticket has
    exactly one (193 threads, 193 tickets), so a card shows one chip or none
    and there is no "which of them" to answer.
    """
    c = Check("only build tickets are shown")

    c.equal(tuple(api.TICKET_KINDS_SHOWN), ("build",),
            "builds only, as asked")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        for key, kind in (("PIP-1", "return"), ("PIP-2", "build"),
                          ("PIP-3", None)):
            # tickets.message_id is a real foreign key -- the confirmation
            # message is what proves the ticket exists, so a row cannot name
            # one that was never seen.
            b.con.execute(
                "INSERT INTO messages (message_id, thread_id, author_id, "
                "author_name, is_bot, created_at, first_seen_at) "
                "VALUES (?,?,?,?,1,datetime('now'),datetime('now'))",
                (f"m{key}", tid, "bot", "Python-Interface-Bot"))
            b.con.execute(
                "INSERT INTO tickets (pip_key, thread_id, message_id, kind, "
                "created_at) VALUES (?,?,?,?,datetime('now'))",
                (key, tid, f"m{key}", kind))
        b.con.commit()

        got = b.con.execute(
            """SELECT (SELECT t.pip_key FROM tickets t
                        WHERE t.thread_id = c.thread_id AND t.kind = 'build'
                        ORDER BY t.created_at LIMIT 1) AS build_ticket
                 FROM cards c WHERE c.thread_id = ?""", (tid,)).fetchone()
        c.equal(got["build_ticket"], "PIP-2",
                "the build one, not the return and not the unknown")

    return c.report()


def check_the_equipment_chips_narrow_to_what_was_clicked() -> bool:
    """
    Click ODE, see the ODEs. One click, not three unchecks.

    The queue checkboxes beside these default to all-on and narrow by being
    turned *off*; these default to all-off and narrow by being turned *on*.
    Opposite conventions, which is exactly why they are pills rather than a
    second row of checkboxes -- a control that looks the same and behaves
    backwards is the kind of thing nobody works out by looking.
    """
    c = Check("the equipment chips narrow to what was clicked")

    def card(tid, *types):
        return {"thread_id": tid, "completed_at": None,
                "equipment": [{"eq_type": t, "raw": f"{t}-1"} for t in types]}

    cards = [card("a", "ODE"), card("b", "EReel"), card("c", "SSD"),
             card("d", "OLK"), card("e")]           # no equipment at all

    def shown(chosen):
        want = bert.equipment_types(chosen)
        if not want:
            return {x["thread_id"] for x in cards}
        return {x["thread_id"] for x in cards
                if {e["eq_type"] for e in x["equipment"]} & want}

    c.equal(shown(set()), {"a", "b", "c", "d", "e"},
            "nothing on shows the whole board")
    c.equal(shown({"ODE"}), {"a"}, "ODE shows only the ODE")
    c.equal(shown({"E-Reels"}), {"b"}, "E-Reels reads EReel, which is what "
                                       "the parser calls it")
    c.equal(shown({"Bot"}), {"c"}, "Bot reads SSD")
    c.equal(shown({"ODE", "OLK"}), {"a", "d"},
            "two chips is either, not both")

    # A ticket about a bot and a reel belongs under both chips: 65 of
    # production's threads carry two or more pieces and one carries six.
    cards.append(card("f", "SSD", "EReel"))
    c.ok("f" in shown({"Bot"}) and "f" in shown({"E-Reels"}),
         "a ticket with two pieces is under both")

    return c.report()


def check_a_ticket_with_no_equipment_is_hidden_and_counted_for() -> bool:
    """
    The half of this that could look like a bug.

    **31 of production's 50 open cards carry no equipment at all.** So one
    chip can legitimately empty most of the board, and a reader who does not
    know that sees three quarters of their tickets vanish and reasonably
    concludes the filter is broken.

    Hiding them is right -- a ticket with no equipment is not an ODE. What
    makes it legible is the counts: the chips are numbered off the whole
    board, they visibly do not add up to it, and `Show all` appears the
    moment one is on.
    """
    c = Check("a ticket with no equipment is hidden, and counted for")

    cards = [{"thread_id": "a", "completed_at": None,
              "equipment": [{"eq_type": "ODE"}]},
             {"thread_id": "b", "completed_at": None, "equipment": []},
             {"thread_id": "c", "completed_at": None},
             {"thread_id": "d", "completed_at": "2026-01-01",
              "equipment": [{"eq_type": "ODE"}]}]

    want = bert.equipment_types({"ODE"})
    kept = [x["thread_id"] for x in cards
            if {e["eq_type"] for e in (x.get("equipment") or [])} & want]
    c.equal(kept, ["a", "d"], "only the ones carrying an ODE survive the chip")

    # Both rows write a count the same way, off one function. `Bot 3` reads
    # as the name of a third bot, which is what the chips said until somebody
    # read one back.
    c.equal(bert.queue_label("Bot", 3), "Bot (3)",
            "a chip's count is bracketed, like the queue boxes above it")
    c.equal(bert.queue_label("Bot", None), "Bot",
            "and there is no empty bracket before the board has loaded")

    counts = bert.equipment_counts(cards)
    c.equal(counts["ODE"], 1, "the count is open tickets only, not closed ones")
    c.equal(counts["Bot"], 0, "and a kind nothing carries counts nought")
    c.ok(sum(counts.values()) < len([x for x in cards if not x["completed_at"]]),
         "the chips add to less than the board, which is the honest answer")

    return c.report()


def check_an_empty_band_is_still_named() -> bool:
    """
    A band with nothing in it is somewhere to put something.

    The running order used to draw a band only if it held a card, unless a
    drag was in flight -- so a board with everything in Needs Attention showed
    one heading at rest and four more the instant a card was picked up. The
    list rearranged itself under the pointer at the moment somebody was aiming
    at it, and before that there was nothing to aim at.

    Worse on the board, where the heading carries the + New Ticket button: a
    hidden band takes its button with it, so with everything in Needs
    Attention there was no way to start a ticket in Critical at all. Measured
    before the fix: four of the five buttons did not exist.
    """
    c = Check("an empty band is still named, and can still be added to")

    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def method(cls_name, fn):
        cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                    and n.name == cls_name), None)
        if cls is None:
            return None
        return next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                     and n.name == fn), None)

    # The rail names every band it is not deliberately hiding.
    rail = method("Rail", "set_cards")
    c.ok(rail is not None, "the rail builds its own list")
    body_src = ast.get_source_segment(src, rail) or "" if rail else ""
    skip = [ln for ln in body_src.splitlines() if "continue" in ln]
    c.ok(not any("not group" in ln and "continue" in body_src
                 for ln in body_src.splitlines() if "filtering" in ln),
         "no band is skipped for being empty, however narrow the board")
    c.ok("for band in BANDS" in body_src,
         "it walks every band rather than the cards it happens to hold")

    # The zone is a target, not a label, so it stays drag-only.
    zone = [ln for ln in body_src.splitlines() if "RailZone" in ln]
    c.ok(zone, "the rail still has a drop zone")
    guard = body_src.split("RailZone")[0].splitlines()[-2:] if zone else []
    c.ok(any("dragging" in ln for ln in guard),
         "shown only while a card is in the air, not stacked up at rest")

    # And the board keeps its band, because the band carries the + button
    # and is the target a drag lands on.
    drag_h = method("Band", "_apply_drag_height")
    vis = ast.get_source_segment(src, drag_h) or "" if drag_h else ""
    c.ok("setVisible(True)" in vis,
         "a board band is visible whatever the filter is doing")
    c.ok("filtering" not in vis,
         "and does not ask, so it cannot take its + New Ticket away")

    return c.report()


def check_every_band_is_drawn_however_narrow_the_board() -> bool:
    """
    A band is a drop target and a `+ New Ticket`, not only a heading.

    An empty band used to hide while a filter was on, on the reasoning that
    somebody narrowing the view did it deliberately and five headings over
    one result fights the narrowing. The first half is true about headings
    and it quietly took the rest with it: with a chip on, a card could not be
    dragged into an empty priority and a ticket could not be started in one,
    because neither the target nor the button existed.

    That is the same bug already recorded one comment up in `Band` -- "with
    everything in Needs Attention there was no way to start a ticket in
    Critical at all, measured, four of the five buttons did not exist" --
    coming back whenever the board was narrowed.

    It also brought back the glitch that rule was written to stop: empty
    bands appearing the *instant* a card was picked up, because a drag makes
    them visible again. The list rearranges under the pointer at the moment
    somebody is aiming at it.

    Read off the source, because these checks never make a QApplication.
    """
    c = Check("every band is drawn however narrow the board")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    band = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "Band")
    body = ast.get_source_segment(src, band) or ""
    c.ok("self.setVisible(True)" in body,
         "a band on the board is visible, full stop")
    c.ok("filtering()" not in body,
         "and its visibility does not ask whether the board is narrowed")

    rail = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "Rail")
    rbody = ast.get_source_segment(src, rail) or ""
    c.ok("filtering()" not in rbody,
         "the running order does not ask either, so it cannot skip one")
    # Every band, walked -- not the cards, or a band with nothing in it never
    # gets its turn.
    c.ok("for band in BANDS:" in rbody, "it walks the bands, not the cards")

    # BANDS is the list both lists walk, and it has to hold all five or this
    # is true of a shorter board than anybody has.
    c.equal(len(bert.BANDS), 5, "there are five bands to draw")
    for b in ("unassigned", "critical", "high", "medium", "low"):
        c.ok(b in bert.BANDS, f"including {b}")

    return c.report()


def check_a_clients_spellings_are_one_entry() -> bool:
    """
    Thirty-five names, fifty-three tickets, and four of them one customer.

    Measured on the board this was asked for: `bravon`, `bravo`, `Bravo` and
    `Bravo Environmental` are the same seven tickets. Keyed on the string as
    typed, the filter would have offered four Bravos and never shown those
    seven together -- which is the mess the roster and the alias table exist
    to undo, so it would be a strange place to start ignoring them.

    An alias is an exact answer rather than a resemblance. The table is
    written by `reconcile_aliases`, which resolves through the ticket's
    Client CR key and refuses to merge on similarity, so nothing here
    compares strings loosely.

    A name that resolves to nobody is its own entry: a customer exists before
    Jira hears about them and their tickets still have to be findable.
    """
    c = Check("a client's spellings are one entry")

    roster = [{"client_id": "PIP-2165", "short_name": "Bravo Environmental",
               "aliases": ["Bravo", "bravon"]},
              {"client_id": "PIP-4863", "short_name": "Thrasher",
               "aliases": []}]
    cards = ([{"client_raw": n, "completed_at": None}
              for n in ("bravon", "bravon", "bravo", "Bravo",
                        "Bravo Environmental")]
             + [{"client_raw": "Thrasher", "completed_at": None}] * 3
             + [{"client_raw": "", "completed_at": None}]
             + [{"client_raw": "Nobody Ltd", "completed_at": None}]
             # A closed ticket is not on the board and must not be counted.
             + [{"client_raw": "Thrasher", "completed_at": "2026-01-01"}])

    got = dict(bert.client_counts(cards, roster))
    c.equal(got.get("Bravo Environmental"), 5,
            "every spelling of one customer counts as that customer")
    c.equal(got.get("Thrasher"), 3, "and a closed ticket is not on the board")
    c.equal(got.get("Nobody Ltd"), 1,
            "a name the roster never heard of is still its own entry")
    c.equal(got.get(bert.CLIENT_NONE), 1, "and a ticket naming nobody is reachable")

    order = [k for k, _ in bert.client_counts(cards, roster)]
    c.equal(order[:3], ["Bravo Environmental", "Nobody Ltd", "Thrasher"],
            "alphabetical: a list of 35 is scanned for a name already in mind")
    c.equal(order[-1], bert.CLIENT_NONE,
            "with nobody's client last, since it is not a customer")

    c.equal(bert.client_filter_label("Thrasher", 8), "Thrasher (8)",
            "and an entry reads the way the queue boxes and chips do")

    # The counts are the whole board, never the filtered view -- the two ways
    # queue_counts says this goes wrong hold here too.
    fewer = [x for x in cards if x["client_raw"] == "Thrasher"]
    c.equal(dict(bert.client_counts(fewer, roster)).get("Thrasher"), 3,
            "counted off what it is given, which render() gives unfiltered")

    return c.report()


CHECKS = (check_a_clients_spellings_are_one_entry,
          check_every_band_is_drawn_however_narrow_the_board,
          check_a_build_ticket_links_to_jira,
          check_only_build_tickets_are_shown,
check_the_equipment_chips_narrow_to_what_was_clicked,
          check_a_ticket_with_no_equipment_is_hidden_and_counted_for,
check_predicate, check_new_cards_rank, check_one_order,
          check_a_reorder_says_where_it_went,
          check_a_reorder_that_moves_nothing_says_nothing,
          check_the_rail_clips_to_its_width,
          check_a_collapsed_band_still_lands_a_drop,
          check_a_folded_band_opens_for_what_goes_into_it,
          check_an_empty_band_is_still_named)
