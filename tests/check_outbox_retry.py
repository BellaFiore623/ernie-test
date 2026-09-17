"""
A retry must not do again what already reached Discord.

Reported as changes showing up in Discord while the card went on saying
"Pushing to Discord...", with no details to hand, which is what made it hard
to place. It reproduces cleanly and is worse than the symptom.

Posting one event takes up to four writes -- unarchive, rename, message,
archive -- and creating a ticket takes three: the thread, a note naming who
started it, their opening message. Only the *last* was recorded, so a failure
anywhere threw away the record of everything before it and the retry started
from the top.

Two of those writes are harmless twice and two are not: archiving an archived
thread is the same as archiving it once, while renaming is 2 per 10 minutes on
a shared budget and posts a system message each time, posting a message is a
message, and opening a thread is a whole second ticket.

Measured before the fix: an archive that failed twice put **three identical
"marked this complete" messages** into one customer thread, and an opening
message that failed twice left **three real threads** for one ticket -- the
board keeping the third, the sync free to pick the other two up later as
fresh unassigned cards.

`sent_steps` is the record, written and committed the moment each irreversible
write lands.
"""

import ast
import pathlib
import threading
import time
import sqlite3
import uuid

from support import PARENT, Board, Check, iso

import ernie_outbox as O


ROOT = pathlib.Path(__file__).resolve().parent.parent


class Flaky:
    """Discord, with one kind of write failing on demand."""

    def __init__(self, fail=None):
        self.calls = []
        self.guild_id = "guild"
        self.fail = fail            # a predicate over (verb, path, kw)
        self.threads = 0

    def write(self, verb, path, **kw):
        if self.fail and self.fail(verb, path, kw):
            self.calls.append((verb, path, kw, "failed"))
            raise RuntimeError("boom -- Discord said no")
        self.calls.append((verb, path, kw, "ok"))
        if path.endswith("/threads"):
            self.threads += 1
            return {"id": f"thread-new-{self.threads}"}
        return {"id": f"msg-{len(self.calls)}"}

    def ok(self, verb, ending=None):
        """The successful calls of a shape, for counting."""
        return [(p, kw) for v, p, kw, how in self.calls
                if how == "ok" and v == verb
                and (ending is None or p.endswith(ending))]


def pushing(con, tid):
    """What the card's unsent mark reads off -- the API's own query."""
    return con.execute(
        """SELECT COUNT(*) FROM events
           WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
             AND undone_at IS NULL AND attempts < 5 AND thread_id=?""",
        (tid,)).fetchone()[0]


def check_a_message_is_posted_once_however_often_the_rest_fails() -> bool:
    """
    Completing posts a message and then archives the thread.

    The archive failing is an ordinary thing -- a 500, a permission that has
    just changed, a thread somebody deleted -- and it left the message posted
    with nothing recording that. Three attempts, three messages.
    """
    c = Check("a message is posted once however often the rest fails")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        eid = b.event(tid, verb="completed", old=None, new="done",
                      dispatch_after=iso(-60))
        d = Flaky(fail=lambda v, p, kw: kw.get("archived") is True)

        for _ in range(2):
            ev = b.con.execute("SELECT * FROM events WHERE event_id=?",
                               (eid,)).fetchone()
            c.equal(O.post_one(b.con, d, ev), "failed", "the archive fails")
            b.con.commit()

        c.equal(len(d.ok("POST", "/messages")), 1,
                "and the message went out once, not once per attempt")
        c.ok(pushing(b.con, tid),
             "the card still says it is pushing, which is true -- the thread "
             "is not archived yet")

        d.fail = None
        ev = b.con.execute("SELECT * FROM events WHERE event_id=?",
                           (eid,)).fetchone()
        c.equal(O.post_one(b.con, d, ev), "sent", "and it lands on the third")
        b.con.commit()
        c.equal(len(d.ok("POST", "/messages")), 1,
                "still one message in the thread")
        c.ok(not pushing(b.con, tid), "and the mark clears")

        row = b.con.execute("SELECT discord_message_id FROM events "
                            "WHERE event_id=?", (eid,)).fetchone()
        c.ok(row["discord_message_id"],
             "the message id survives the retries, so undo can reply to it")

    return c.report()


def check_a_thread_is_renamed_once() -> bool:
    """
    A rename is 2 per 10 minutes, shared, and posts a system message.

    So doing it again because a later write failed spends a budget the board
    has very little of, and puts a second "renamed the thread" line into a
    customer thread that has already had one.
    """
    c = Check("a thread is renamed once")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        # Archived, so there is an unarchive before the rename and a real
        # sequence to interrupt.
        b.con.execute("UPDATE threads SET archived=1 WHERE thread_id=?", (tid,))
        eid = b.event(tid, verb="renamed", old="PROD: A - 01Jan26 - x",
                      new="OPS: A - 01Jan26 - x", dispatch_after=iso(-60))
        b.con.commit()

        # The rename lands; the write after it fails. Then everything works.
        state = {"n": 0}

        def fail(v, p, kw):
            if "name" in kw:
                state["n"] += 1
                return False        # the rename itself always succeeds
            return state["n"] == 1 and kw.get("archived") is False

        d = Flaky(fail=fail)
        for _ in range(2):
            ev = b.con.execute("SELECT * FROM events WHERE event_id=?",
                               (eid,)).fetchone()
            O.post_one(b.con, d, ev)
            b.con.commit()

        renames = [kw for _, kw in d.ok("PATCH") if "name" in kw]
        c.equal(len(renames), 1,
                "one rename reached Discord across a failure and a retry")

    return c.report()


def a_draft(b):
    did = str(uuid.uuid4())
    b.con.execute(
        """INSERT INTO new_threads (draft_id, channel_id, title, priority,
                                    actor, first_message, work_json,
                                    created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (did, PARENT, "PROD: A - 01Jan26 - a new one", "medium", "Tester",
         "here is what it is about", '["check the reel", "pack it"]',
         iso(-10)))
    b.con.commit()
    return did


def check_one_ticket_makes_one_thread() -> bool:
    """
    The worst of them, because the extra threads are real tickets.

    The thread's id was written only after both messages had gone out, so a
    message that failed threw it away -- and the retry opened another thread
    for the same ticket. The board keeps whichever one finished; the sync sees
    the others on its next pass and makes cards for them, so a hiccup while
    starting a ticket quietly puts two more on the board.
    """
    c = Check("one ticket makes one thread")

    with Board() as b:
        a_draft(b)
        d = Flaky(fail=lambda v, p, kw: p.endswith("/messages"))

        for _ in range(2):
            c.equal(O.make_threads(b.con, d)["failed"], 1,
                    "the opening message fails")
        c.equal(d.threads, 1,
                "and exactly one thread was created, not one per attempt")

        d.fail = None
        c.equal(O.make_threads(b.con, d)["made"], 1, "the third pass lands it")
        c.equal(d.threads, 1, "still one thread")
        c.equal(len(d.ok("POST", "/messages")), 2,
                "one note and one opening message, each posted once")

    return c.report()


def check_a_ticket_reaches_the_board_before_its_messages_do() -> bool:
    """
    A card that exists is worth more than a tidy order of writes.

    The rows were written after both messages, so a message that failed left
    the ticket off the board entirely until a later attempt succeeded --
    somebody watching would see the ticket they had just started simply not be
    there. The thread exists by then, which is the only thing a card needs.
    """
    c = Check("a ticket reaches the board before its messages do")

    with Board() as b:
        a_draft(b)
        d = Flaky(fail=lambda v, p, kw: p.endswith("/messages"))
        O.make_threads(b.con, d)

        row = b.con.execute("SELECT thread_id, priority FROM cards").fetchone()
        c.ok(row is not None, "the card is on the board after the failure")
        if row is None:
            return c.report()
        c.equal(row["priority"], "medium", "in the band the + was pressed in")
        c.equal(b.con.execute("SELECT COUNT(*) n FROM work_items")
                .fetchone()["n"], 2, "with its two work items")

        # And the retry does not give it every bubble twice.
        O.make_threads(b.con, d)
        c.equal(b.con.execute("SELECT COUNT(*) n FROM work_items")
                .fetchone()["n"], 2, "still two after a retry, not four")
        c.equal(b.con.execute("SELECT COUNT(*) n FROM thread_titles "
                              "WHERE thread_id=?",
                              (row["thread_id"],)).fetchone()["n"], 1,
                "and one title row, not one per attempt")

    return c.report()


def check_the_steps_are_recorded_as_they_land() -> bool:
    """
    The record has to be committed *between* the writes, not after them.

    Everything else here is a consequence of that. A pass that noted its steps
    in memory and wrote them at the end would lose them in exactly the case
    they exist for.
    """
    c = Check("the steps are recorded as they land")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        eid = b.event(tid, verb="completed", old=None, new="done",
                      dispatch_after=iso(-60))
        d = Flaky(fail=lambda v, p, kw: kw.get("archived") is True)
        ev = b.con.execute("SELECT * FROM events WHERE event_id=?",
                           (eid,)).fetchone()
        O.post_one(b.con, d, ev)

        # Read on a second connection, so only committed rows are visible.
        other = sqlite3.connect(b.path)
        other.row_factory = sqlite3.Row
        steps = other.execute("SELECT sent_steps FROM events WHERE event_id=?",
                              (eid,)).fetchone()["sent_steps"]
        other.close()
        c.ok(steps and "message" in steps,
             f"the message step is committed, not held in memory ({steps!r})")

        c.equal(O.steps_done({"sent_steps": "message,renamed"}),
                {"message", "renamed"}, "and steps_done reads it back")
        c.equal(O.steps_done({"sent_steps": None}), set(),
                "an untouched row has done nothing")

    return c.report()


def check_the_loop_stops_when_asked() -> bool:
    """
    The outbox has to be stoppable, because closing the window is what
    drains it.

    One exe runs sync, the outbox, the API and Bert in a single process. The
    shutdown sequence is: stop taking changes, drain -- *including* events
    still inside their undo window, because closing is the person saying they
    are done -- publish the board once more, exit. None of that is reachable
    from a loop that only ends when the process dies.

    The pass came out of `main()` whole rather than being reimplemented: it
    is not just `drain()`. It makes the threads tickets are waiting on,
    publishes to `#ernie-state`, writes each ticket's status into its own
    thread and appends to the change log -- four things that belong on this
    loop because this is the only process allowed to write to Discord.
    """
    c = Check("the outbox loop stops when asked")

    stop = threading.Event()
    stop.set()
    t0 = time.time()
    O.run(None, None, "nowhere.db", stop=stop)
    c.ok(time.time() - t0 < 0.5,
         "a loop told to stop before it starts returns at once, without a "
         "connection or a client")

    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    t0 = time.time()
    stopped = O._pause(stop, 5)
    c.ok(stopped, "a stop during the wait is reported as one")
    c.ok(time.time() - t0 < 1.0, "and cuts the beat short")

    c.ok(O.run.__kwdefaults__.get("stop") is None,
         "and the CLI keeps the loop it had")

    # All four jobs are still on it, which is what "lifted whole" means.
    body = ast.get_source_segment(
        (ROOT / "ernie_outbox.py").read_text(encoding="utf-8"),
        next(n for n in ast.walk(ast.parse(
            (ROOT / "ernie_outbox.py").read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef) and n.name == "run")) or ""
    for job in ("drain", "make_threads", "ernie_state.publish",
                "ernie_status.publish", "ernie_changelog.tick"):
        c.ok(job in body, f"{job} is still on the loop")

    return c.report()


def check_only_harmless_writes_ride_out_a_blip() -> bool:
    """
    The read path rode out Discord's 5xx and the write path did not.

    `get()` has always retried a 500 with a backoff, a repeated read costing
    nothing. `write()` only handled 429 and then raised, so a `503` -- which
    Discord serves often enough to meet twice in an afternoon -- came straight
    out. Found on a run of six thousand consecutive writes, more in a row than
    this had ever done; it killed the run twice.

    **It is opt-in, and that is the design.** A 5xx does not say whether the
    request was processed, so an automatic retry on a POST can post twice --
    the failure every other check here exists to prevent, and the one that put
    three identical "marked this complete" messages in a customer thread. So
    the caller decides, and only where doing it again is a genuine no-op:
    archiving an archived thread, editing a message to the text it holds,
    pinning a pinned message.

    Never a message, a new thread, or a rename -- two per ten minutes on a
    budget shared between both machines, with a system message each time, so a
    silent retry spends somebody else's allowance.
    """
    c = Check("only harmless writes ride out a blip")

    import inspect
    import ernie_sync

    sig = inspect.signature(ernie_sync.Discord.write)
    c.ok("retry_5xx" in sig.parameters, "write() can be told to ride one out")
    c.equal(sig.parameters["retry_5xx"].default, False,
            "and does not, unless it is asked")

    src = inspect.getsource(ernie_sync.Discord.write)
    c.ok("retry_5xx and" in src and "500" in src,
         "the retry is gated on the flag, not on the status alone")

    # The rule, read off every call site in the tree. A POST that creates
    # something must never carry it; the three no-ops should.
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    bad = []
    opted = []
    for rel in ("ernie_outbox.py", "ernie_state.py", "ernie_status.py",
                "tools/clone_prod_threads.py"):
        text = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "write"):
                continue
            wants = any(k.arg == "retry_5xx" for k in node.keywords)
            if not wants:
                continue
            piece = ast.get_source_segment(text, node) or ""
            opted.append(f"{rel}:{node.lineno}")
            method = node.args[0].value if node.args and isinstance(
                node.args[0], ast.Constant) else "?"
            # POST creates; PATCH/PUT here only ever re-state something.
            if method == "POST":
                bad.append(f"{rel}:{node.lineno} {piece[:60]}")
            if "name=" in piece and method == "PATCH":
                bad.append(f"{rel}:{node.lineno} rename must not retry")

    c.ok(opted, f"some writes opt in: {len(opted)} of them")
    c.ok(not bad, "and nothing that creates or renames does: " + "; ".join(bad))

    return c.report()


def check_a_half_written_thread_resumes_where_it_stopped() -> bool:
    """
    The clone's ledger counted threads, so a resume repeated a thread's posts.

    It recorded "thread made" and "thread finished" and nothing between, so a
    failure part way through a hundred-message thread meant the next run began
    that thread again at its first line. Both real deaths happened to land on
    thread creation, before any message went out -- luck, not design, and the
    ledger is the only thing that could have made it design.

    Same shape as `events.sent_steps`: written between the writes rather than
    after the last, because a count held in memory is lost in exactly the case
    it exists for.
    """
    c = Check("a half-written thread resumes where it stopped")

    import importlib.util
    import pathlib
    import tempfile
    root = pathlib.Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "clone", root / "tools" / "clone_prod_threads.py")
    clone = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(clone)

    with tempfile.TemporaryDirectory() as d:
        path = str(pathlib.Path(d) / "ledger.json")
        led = clone.Ledger(path)
        c.equal(led.posted("t1"), 0, "a thread nobody has started is at nought")

        led.note_thread("t1", "new-1")
        led.note_posted("t1", 7)
        # Reopened from disk, because the point is surviving the process.
        again = clone.Ledger(path)
        c.equal(again.posted("t1"), 7, "seven posted survives a restart")
        c.ok(not again.finished("t1"), "and the thread is not done")
        c.equal(again.thread_for("t1"), "new-1",
                "while the thread it made is still known, so no second one")

        again.finish("t1")
        done = clone.Ledger(path)
        c.ok(done.finished("t1"), "finishing it is recorded")
        c.equal(done.posted("t1"), 0,
                "and the count is dropped, since the thread is skipped whole")

        # A ledger written before counts existed must still load.
        import json as _json
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump({"threads": {"t9": "x"}, "done": []}, fh)
        old = clone.Ledger(path)
        c.equal(old.posted("t9"), 0, "an older ledger reads as nought, not a crash")

    return c.report()


def check_an_outage_is_not_the_cards_fault() -> bool:
    """
    Giving up is a statement about the change, not about the network.

    `attempts` is what takes a row out of `v_outbox_due` for good, and
    `MAX_ATTEMPTS` is 5. A connection failure used to count, so five passes
    against an unreachable Discord abandoned the change permanently and
    `/health` reported it as `stuck` -- which reads as something wrong with
    that card rather than with Discord.

    **The drain beat made it sharp.** Five passes at 30 seconds was two and a
    half minutes; at `FAST_SECONDS` it is **twenty-five seconds** of Discord
    being unreachable to strand every queued change on the board. Measured
    against a copy with a client pointed at a dead port, which is how a guard
    is tested here -- never by waiting for the real thing.

    A `TransportError` is exactly "no HTTP response happened". Anything that
    got an answer still counts, including a 4xx: Discord replied about this
    request, so the attempt was real and a row that keeps being refused
    should still be given up on.
    """
    c = Check("an outage is not the card's fault")

    import httpx
    import ernie_sync
    import ernie_outbox as ob

    guild = "999"

    # 1. Discord unreachable: nothing is ever given up on.
    with Board() as b:
        tid = b.card("PROD: Outage - 01Jan26 - x", "medium")
        eid = b.event(tid, verb="completed", old=None, new=None,
                      dispatch_after=iso(-120))
        b.con.commit()
        d = ernie_sync.Discord("t", guild, allow_writes_for=guild)
        d.http = httpx.Client(base_url="http://127.0.0.1:9", timeout=1.0)
        for _ in range(ob.MAX_ATTEMPTS + 3):
            ob.drain(b.con, d)
        row = b.con.execute("SELECT attempts, last_error FROM events "
                            "WHERE event_id=?", (eid,)).fetchone()
        c.equal(row["attempts"], 0,
                f"{ob.MAX_ATTEMPTS + 3} passes against a dead host cost no attempts")
        c.ok(row["last_error"], "but the error is recorded while it is true")
        c.equal(b.con.execute("SELECT COUNT(*) FROM v_outbox_due").fetchone()[0], 1,
                "and the change is still due to go")
        c.equal(len(ob.stuck(b.con)), 0, "nothing is reported as given up on")

        # And it goes the moment Discord answers.
        d.http = httpx.Client(base_url="http://x", transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"id": "m1"})))
        got = ob.drain(b.con, d)
        c.equal(got["sent"], 1, "and it posts as soon as Discord is back")

    # 2. Discord answering, and refusing: this row really is the problem.
    with Board() as b:
        tid = b.card("PROD: Refused - 01Jan26 - x", "medium")
        eid = b.event(tid, verb="completed", old=None, new=None,
                      dispatch_after=iso(-120))
        b.con.commit()
        d = ernie_sync.Discord("t", guild, allow_writes_for=guild)
        d.http = httpx.Client(base_url="http://x", transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"message": "no"})))
        for _ in range(ob.MAX_ATTEMPTS + 2):
            ob.drain(b.con, d)
        row = b.con.execute("SELECT attempts FROM events WHERE event_id=?",
                            (eid,)).fetchone()
        c.equal(row["attempts"], ob.MAX_ATTEMPTS,
                "a refusal counts, and stops at the ceiling")
        c.equal(b.con.execute("SELECT COUNT(*) FROM v_outbox_due").fetchone()[0], 0,
                "the row is no longer picked up")
        c.equal(len(ob.stuck(b.con)), 1, "and it is reported as given up on")

    return c.report()


CHECKS = (check_an_outage_is_not_the_cards_fault,
          check_only_harmless_writes_ride_out_a_blip,
          check_a_half_written_thread_resumes_where_it_stopped,
          check_the_loop_stops_when_asked,
          check_a_message_is_posted_once_however_often_the_rest_fails,
          check_a_thread_is_renamed_once,
          check_one_ticket_makes_one_thread,
          check_a_ticket_reaches_the_board_before_its_messages_do,
          check_the_steps_are_recorded_as_they_land)
