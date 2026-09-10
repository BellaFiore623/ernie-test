"""
A ticket closed in Discord has to reach the board, and only then.

Archiving a thread is how work finishes, and Bert could not see it happen:
the loop runs on `/guilds/{id}/threads/active`, an archived thread is simply
not in that list, so the row kept whatever `archived` it had and the card was
never completed. Measured before this was written -- a thread archived in
Discord, then a full sync cycle: `threads.archived` still 0, `completed_at`
still NULL, no event, card still on the board.

The rule these checks defend is the one that keeps that fix from becoming a
worse bug: **absence is the question, never the answer.** A card missing from
the listing earns a `GET /channels/{id}` and nothing else. The card is closed
on what Discord says in the reply. A listing short for any other reason -- a
hiccup, a permission change, a channel dropping out of `watched` -- must not
close half the board in one pass.
"""

import sqlite3

from support import Board, Check, PARENT

import bert
import ernie_api as api
import ernie_sync as S


BOT_ID = "bot-1"


class Answers:
    """Discord, for the two calls this makes: the thread, and the audit log.

    Records what was asked, so "it cost nothing" is a thing a check can
    assert rather than a thing a comment claims. It carries `guild_id`
    because the real client does and the code reaches for it.

    `audit` is None by default, which is the shape of a bot without **View
    Audit Log** -- the client turns a 403 into None. That is the path this
    shipped on before the permission was granted, so it is the default here.
    """

    def __init__(self, replies, audit=None):
        self.replies = replies          # thread_id -> reply, or None
        self.audit = audit              # list of (thread_id, user) or None
        self.guild_id = "guild-1"
        self.asked = []
        self.audit_calls = 0

    def get(self, path, **kw):
        if path.endswith("/audit-logs"):
            self.audit_calls += 1
            if self.audit is None:
                return None
            users, entries = {}, []
            for tid, user in self.audit:
                users[user["id"]] = user
                entries.append({
                    "target_id": tid, "user_id": user["id"],
                    "changes": [{"key": "archived", "new_value": True}]})
            return {"audit_log_entries": entries, "users": list(users.values())}
        if path == "/users/@me":
            return {"id": BOT_ID}
        tid = path.rsplit("/", 1)[-1]
        self.asked.append(tid)
        return self.replies.get(tid)


def archived_at(when):
    return {"thread_metadata": {"archived": True, "archive_timestamp": when}}


LIVE = {"thread_metadata": {"archived": False}}
WHEN = "2026-09-10T14:40:17.265000+00:00"


def board_with_two(b):
    """Two open cards in a watched channel, which is the ordinary board."""
    b.con.execute(
        "INSERT OR IGNORE INTO watched_channels (channel_id, generate_cards) "
        "VALUES (?,1)", (PARENT,))
    one = b.card("PROD: A - 01Jan26 - x", "high")
    two = b.card("OPS: B - 01Jan26 - y", "high")
    b.con.commit()
    return one, two


def check_a_thread_archived_in_discord_closes_its_card() -> bool:
    """The whole point: it reaches the board, at the time it really happened."""
    c = Check("a thread archived in Discord closes its card")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN)})
        stats = {}
        # `two` is in the listing, `one` is not -- which is what an archive
        # looks like from here.
        S.reconcile_closures(b.con, d, {two}, stats)

        c.equal(d.asked, [one], "only the missing card is asked about")
        c.equal(stats.get("closed_in_discord"), 1, "and it is closed")

        row = b.con.execute(
            "SELECT completed_at FROM cards WHERE thread_id=?", (one,)).fetchone()
        c.equal(row["completed_at"], WHEN,
                "at Discord's own archive_timestamp, not at now -- the feed "
                "is supposed to say when the work finished")
        c.equal(b.con.execute("SELECT archived FROM threads WHERE thread_id=?",
                              (one,)).fetchone()["archived"], 1,
                "and the mirror agrees the thread is archived")

        e = b.con.execute(
            "SELECT * FROM events WHERE thread_id=? AND verb='completed'",
            (one,)).fetchone()
        c.ok(e is not None, "an event is written for the activity feed")
        if e:
            c.equal(e["occurred_at"], WHEN, "stamped when it happened")
            c.equal(e["new_value"], S.CLOSED_IN_DISCORD,
                    "carrying where it happened")
            c.equal(e["dispatch_after"], None,
                    "and never posted back -- it happened in Discord already, "
                    "so saying so there is Ernie telling the room what it "
                    "just watched somebody do")
            c.equal(e["actor_name"], None,
                    "naming nobody here, because this fake has no audit log "
                    "to read -- which is the shape of a bot without View "
                    "Audit Log. The permission is granted now; the checks "
                    "below cover both sides of that")

        # The other card is untouched.
        c.equal(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                              (two,)).fetchone()["completed_at"], None,
                "the card that is still listed stays open")

    return c.report()


def check_absence_is_the_question_never_the_answer() -> bool:
    """
    A short listing must not close the board.

    This is the failure that would be far worse than the one being fixed: a
    hiccup, a permission change, or a channel dropping out of `watched` and
    every open ticket closes itself in one pass, each with an event and each
    propagated to the other board.
    """
    c = Check("absence is the question, never the answer")

    with Board() as b:
        one, two = board_with_two(b)
        # Both missing from the listing, and Discord says both are alive.
        d = Answers({one: LIVE, two: LIVE})
        stats = {}
        S.reconcile_closures(b.con, d, set(), stats)

        c.equal(sorted(d.asked), sorted([one, two]), "both are asked about")
        c.equal(stats.get("closed_in_discord", 0), 0, "and neither is closed")
        for tid in (one, two):
            c.equal(b.con.execute("SELECT completed_at FROM cards WHERE "
                                  "thread_id=?", (tid,)).fetchone()["completed_at"],
                    None, "the card stays open")
        c.equal(b.con.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0,
                "and nothing is written to the feed")

    return c.report()


def check_a_thread_it_cannot_read_is_never_declared_finished() -> bool:
    """403 and 404 come back as None, and None is not "closed"."""
    c = Check("a thread it cannot read is never declared finished")

    with Board() as b:
        one, _ = board_with_two(b)
        d = Answers({one: None})            # the client's soft failure
        stats = {}
        S.reconcile_closures(b.con, d, set(), stats)

        c.equal(stats.get("closed_in_discord", 0), 0, "nothing is closed")
        c.equal(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                              (one,)).fetchone()["completed_at"], None,
                "a thread we cannot read is not a thread we may finish")

    return c.report()


def check_it_costs_nothing_when_nothing_has_closed() -> bool:
    """
    It runs on the fast beat, so a quiet board must make no request at all.

    The listing is already in hand; the query is one SELECT against the
    cards. That is what lets a closure show within five seconds instead of
    sixty.
    """
    c = Check("it costs nothing when nothing has closed")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({})
        S.reconcile_closures(b.con, d, {one, two}, {})
        c.equal(d.asked, [], "no card is missing, so Discord is not asked")

    return c.report()


def check_a_card_bert_closed_is_not_found_again() -> bool:
    """
    Bert's own Complete archives the thread, and that must not read as
    somebody else's closure a moment later.

    It does not, and the reason is worth stating: a completed card is not in
    the query at all. The guard is `completed_at IS NULL`, not a flag about
    who did it.
    """
    c = Check("a card Bert closed is not found again")

    with Board() as b:
        one, two = board_with_two(b)
        b.con.execute(
            "UPDATE cards SET completed_at=?, completed_by=? WHERE thread_id=?",
            ("2026-09-10T10:00:00+00:00", "Bella Fiore", one))
        b.con.execute("UPDATE threads SET archived=1, archived_by_ernie=1 "
                      "WHERE thread_id=?", (one,))
        b.con.commit()

        d = Answers({one: archived_at(WHEN)})
        S.reconcile_closures(b.con, d, {two}, {})
        c.equal(d.asked, [], "a closed card is never asked about again")
        c.equal(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                              (one,)).fetchone()["completed_at"],
                "2026-09-10T10:00:00+00:00",
                "and keeps the time and the person Bert recorded")

    return c.report()


def check_it_does_not_close_the_same_card_twice() -> bool:
    """A thread stays archived, so every later pass sees it missing again."""
    c = Check("it does not close the same card twice")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN)})
        S.reconcile_closures(b.con, d, {two}, {})
        S.reconcile_closures(b.con, d, {two}, {})
        S.reconcile_closures(b.con, d, {two}, {})

        n = b.con.execute("SELECT COUNT(*) FROM events WHERE thread_id=? "
                          "AND verb='completed'", (one,)).fetchone()[0]
        c.equal(n, 1, "one event, however many passes run")
        c.equal(d.asked, [one],
                "and it is asked about once -- completing it takes it out of "
                "the query, which is what stops the request repeating")

    return c.report()


def check_undo_refuses_a_discord_closure() -> bool:
    """
    Undo would loop, so it is refused and points at the verb that works.

    Clearing `completed_at` leaves the thread archived, so the very next pass
    closes the card again: it would come back on the board for five seconds
    and leave, for ever. Reopen posts to the thread, and posting to an
    archived thread unarchives it, so Discord and the board agree afterwards.
    """
    c = Check("undo refuses a Discord closure and says what does work")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN)})
        S.reconcile_closures(b.con, d, {two}, {})
        eid = b.con.execute(
            "SELECT event_id FROM events WHERE thread_id=? AND verb='completed'",
            (one,)).fetchone()["event_id"]

        api.DB = b.path
        try:
            api.undo(eid, api.ActorBody(actor="Bella Fiore"))
        except api.HTTPException as e:
            detail = e.detail if isinstance(e.detail, dict) else {}
            c.equal(e.status_code, 409, "it is refused")
            c.equal(detail.get("code"), "not_undoable", "as not undoable")
            c.ok("reopen" in (detail.get("message") or "").lower(),
                 "and names reopen, which is the one that settles it")
        else:
            c.ok(False, "undo is refused rather than looping")

    return c.report()


def check_the_three_copies_of_the_marker_agree() -> bool:
    """
    `ernie_sync` writes it, the API reads it to refuse undo, and Bert reads
    it to phrase the feed line. Matched rather than imported, the way
    `OUTBOX_MAX_ATTEMPTS` is matched to `ernie_outbox.MAX_ATTEMPTS` -- so
    something has to hold them together.
    """
    c = Check("the three copies of the marker agree")

    c.equal(api.CLOSED_IN_DISCORD, S.CLOSED_IN_DISCORD,
            "the API matches the sync that writes it")
    c.equal(bert.CLOSED_IN_DISCORD, S.CLOSED_IN_DISCORD,
            "and so does Bert, which phrases the feed line off it")

    return c.report()



def check_it_names_whoever_archived_the_thread() -> bool:
    """
    The audit log is the only place that says who, and it needs a permission.

    Granted after the first version shipped, so the code has to work both
    ways: named when the log can be read, and recorded anyway when it cannot.
    global_name over username -- "Tyler" rather than "tyler_mazza" -- which is
    the preference the `started` line already uses.
    """
    c = Check("it names whoever archived the thread")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN)},
                    audit=[(one, {"id": "u-9", "username": "tyler_mazza",
                                  "global_name": "Tyler"})])
        S.reconcile_closures(b.con, d, {two}, {})

        e = b.con.execute("SELECT actor_name FROM events WHERE thread_id=? "
                          "AND verb='completed'", (one,)).fetchone()
        c.equal(e["actor_name"], "Tyler",
                "the feed names them, by the name Discord shows")
        c.equal(b.con.execute("SELECT completed_by FROM cards WHERE thread_id=?",
                              (one,)).fetchone()["completed_by"], "Tyler",
                "and so does the card, which the state channel publishes")
        c.equal(d.audit_calls, 1,
                "one audit call for the pass, however many closed")

    # Only a username, which is what an account with no display name has.
    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN)},
                    audit=[(one, {"id": "u-9", "username": "tyler_mazza"})])
        S.reconcile_closures(b.con, d, {two}, {})
        e = b.con.execute("SELECT actor_name FROM events WHERE thread_id=?",
                          (one,)).fetchone()
        c.equal(e["actor_name"], "tyler_mazza",
                "falling back to the username when there is no display name")

    return c.report()


def check_it_still_closes_when_the_audit_log_is_shut() -> bool:
    """
    Naming somebody is a nicety. Closing the ticket is the feature.

    A revoked permission, or a busy guild that has pushed the archive past
    the lookback, must not hold up the closure -- it just goes unattributed,
    which is exactly how this behaved before the permission existed.
    """
    c = Check("it still closes when the audit log is shut")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN)})       # audit=None -> a 403
        S.reconcile_closures(b.con, d, {two}, {})

        row = b.con.execute("SELECT completed_at, completed_by FROM cards "
                            "WHERE thread_id=?", (one,)).fetchone()
        c.equal(row["completed_at"], WHEN, "the card still closes, on time")
        c.equal(row["completed_by"], None, "with nobody named")
        e = b.con.execute("SELECT actor_name, new_value FROM events "
                          "WHERE thread_id=?", (one,)).fetchone()
        c.equal(e["actor_name"], None, "and the event names nobody")
        c.equal(e["new_value"], S.CLOSED_IN_DISCORD,
                "but still says where it happened, so the feed reads")

    # An archive the log knows nothing about: same outcome.
    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN)}, audit=[])
        S.reconcile_closures(b.con, d, {two}, {})
        c.equal(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                              (one,)).fetchone()["completed_at"], WHEN,
                "an archive past the lookback still closes the card")

    return c.report()


def check_it_never_attributes_a_closure_to_ernie() -> bool:
    """
    Our own bot archives threads when Complete is pressed in Bert.

    That path does not reach here -- the card is already closed, so it is not
    in the query -- but "ernie-test closed it" is the one attribution worth
    making impossible rather than merely unlikely.
    """
    c = Check("it never attributes a closure to Ernie itself")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN)},
                    audit=[(one, {"id": BOT_ID, "username": "ernie-test",
                                  "bot": True})])
        S.reconcile_closures(b.con, d, {two}, {})

        e = b.con.execute("SELECT actor_name FROM events WHERE thread_id=?",
                          (one,)).fetchone()
        c.equal(e["actor_name"], None,
                "the closure is recorded with no name rather than Ernie's")
        c.equal(b.con.execute("SELECT completed_at FROM cards WHERE thread_id=?",
                              (one,)).fetchone()["completed_at"], WHEN,
                "and it is still closed")

    return c.report()


def check_the_audit_log_is_asked_once_and_only_when_needed() -> bool:
    """It is a guild-wide read, so it rides on there being something to name."""
    c = Check("the audit log is asked once, and only when something closed")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: LIVE, two: LIVE},
                    audit=[(one, {"id": "u-9", "username": "x"})])
        S.reconcile_closures(b.con, d, set(), {})
        c.equal(d.audit_calls, 0,
                "nothing closed, so the log is not read at all")

    with Board() as b:
        one, two = board_with_two(b)
        d = Answers({one: archived_at(WHEN), two: archived_at(WHEN)},
                    audit=[(one, {"id": "u-9", "username": "x"}),
                           (two, {"id": "u-8", "username": "y"})])
        S.reconcile_closures(b.con, d, set(), {})
        c.equal(d.audit_calls, 1, "two closures, still one call")
        got = {r["thread_id"]: r["actor_name"] for r in b.con.execute(
            "SELECT thread_id, actor_name FROM events WHERE verb='completed'")}
        c.equal(got.get(one), "x", "and each is attributed to its own person")
        c.equal(got.get(two), "y", "not to whoever came first")

    return c.report()


CHECKS = (check_a_thread_archived_in_discord_closes_its_card,
          check_absence_is_the_question_never_the_answer,
          check_a_thread_it_cannot_read_is_never_declared_finished,
          check_it_costs_nothing_when_nothing_has_closed,
          check_a_card_bert_closed_is_not_found_again,
          check_it_does_not_close_the_same_card_twice,
          check_undo_refuses_a_discord_closure,
          check_the_three_copies_of_the_marker_agree,
          check_it_names_whoever_archived_the_thread,
          check_it_still_closes_when_the_audit_log_is_shut,
          check_it_never_attributes_a_closure_to_ernie,
          check_the_audit_log_is_asked_once_and_only_when_needed)
