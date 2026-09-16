"""
Two beats, because the halves of a cycle cost wildly different things.

Measured against the sandbox: a whole cycle is 13 GETs and ~4s, and 11 of
those GETs and 3.5s of that time are the **edit rescan** -- which found
nothing in either sampled cycle. What a new ticket actually arrives through
is the thread listing, and that is 1 GET and 0.30s.

Discord agrees the split is free. The listing route answered
`x-ratelimit-remaining: 999/1000` on eight back-to-back calls; the route the
rescan and the state pull use, `/channels/{id}/messages`, is 5 per 5s and
429s on the sixth. The cheap route is the one being asked more often.

The rule these hold: a fast pass does the listing, whatever is new in the
threads it named, and the local recompute -- and nothing else.
"""

import ast
import time
import threading
import pathlib

from support import Board, Check, iso

import ernie_sync as S


ROOT = pathlib.Path(__file__).resolve().parent.parent


def source_of(name, kind=ast.FunctionDef):
    src = (ROOT / "ernie_sync.py").read_text(encoding="utf-8")
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, kind) and n.name == name)
    return ast.get_source_segment(src, node) or ""


def check_a_fast_pass_skips_what_a_fast_pass_is_for_skipping() -> bool:
    """The rescan is 11 of the 13 GETs, so it is the thing that must not run."""
    c = Check("a fast pass leaves the expensive half alone")

    body = source_of("cycle")
    c.ok("full" in (S.cycle.__code__.co_varnames[:S.cycle.__code__.co_argcount]),
         "cycle() takes the beat it is on")
    c.ok("if full:" in body, "and asks before the expensive half")
    # The three that a new ticket has to go through are unconditional.
    for fn in ("sync_threads", "sync_messages", "rebuild_derived"):
        line = next((l for l in body.splitlines() if fn + "(" in l), "")
        # Its own statement rather than nested under anything -- `threads =`
        # in front of one of them is an assignment, not a condition, so the
        # indent is what says whether it is guarded.
        indent = len(line) - len(line.lstrip())
        c.ok(line and indent == 8,
             f"{fn} runs on every pass, at the top level of the try "
             f"(indent {indent})")
    # The one that must not be.
    guarded = body.split("if full:")[1] if "if full:" in body else ""
    c.ok("rescan_edits" in guarded,
         "rescan_edits runs only on a full pass")
    c.ok("rescan_edits" not in body.split("if full:")[0],
         "and nowhere else in the cycle")

    return c.report()


def check_the_loop_holds_its_beat() -> bool:
    """
    The sleep is measured from the top of the pass, not the end of it.

    A pass that takes four seconds followed by a five-second sleep is a
    nine-second beat, and the full pass takes exactly that long -- so
    sleeping from the end would make the beat lurch once a minute.
    """
    c = Check("the loop sleeps against the top of the pass")

    # `run()` rather than `main()`: the loop was lifted out so a supervisor
    # thread can drive the same one the CLI does, and its sleeps became a
    # wait a stop event can cut short -- which is still a sleep for this
    # question, and still has to be measured from the top of the pass.
    body = source_of("run")
    sleeps = [l.strip() for l in body.splitlines() if "_pause(stop," in l]
    c.ok(sleeps, "the loop waits between passes")
    c.ok(all("t0" in l for l in sleeps),
         f"and every sleep is measured from the top of the pass ({sleeps})")
    c.ok("max(0.0, seconds)" in source_of("_pause"),
         "and never goes negative when a pass outruns the beat")
    c.ok("stop.wait" in source_of("_pause"),
         "and a stop asked for mid-beat does not wait the beat out")

    # The full pass is scheduled by wall clock rather than by counting fast
    # passes, so a slow one does not drift the minute.
    c.ok("next_full" in body, "the full beat is its own clock")

    return c.report()


def check_a_quiet_fast_pass_says_nothing() -> bool:
    """
    Twelve times the passes must not be twelve times the log.

    Eleven of every twelve lines would read "nothing happened", which buries
    the ones somebody opens the file to find.
    """
    c = Check("a quiet fast pass writes no log line")

    body = source_of("run")
    c.ok("noisy" in body, "the loop asks whether anything happened")
    c.ok("if full or noisy:" in body,
         "and prints only then, or on the beat that always reports")
    for field in ("messages_new", "edits_found", "titles_changed",
                  "deletions_found"):
        c.ok(field in body.split("noisy")[1].split("if full or noisy")[0],
             f"{field} counts as something happening")

    return c.report()


def check_the_audit_table_is_bounded() -> bool:
    """
    Every pass writes a `sync_runs` row, and nothing was deleting them.

    The fast pass has to write one: `/health` reads the newest finished row
    and Bert draws it as "synced 20s ago", so a pass that wrote nothing would
    have the board reporting a staleness it does not have. Twelve times the
    passes is twelve times the rows, for ever.
    """
    c = Check("the audit table is bounded")

    with Board() as b:
        for _ in range(S.SYNC_RUNS_KEPT + 40):
            b.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)",
                          (iso(),))
        b.con.commit()
        newest = b.con.execute("SELECT MAX(run_id) FROM sync_runs").fetchone()[0]
        S.prune_runs(b.con)
        left = b.con.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
        still = b.con.execute("SELECT MAX(run_id) FROM sync_runs").fetchone()[0]
        c.equal(left, S.SYNC_RUNS_KEPT, "it is trimmed to the cap")
        c.equal(still, newest, "keeping the newest, which is what /health reads")

        # A table under the cap is left alone rather than emptied.
        b.con.execute("DELETE FROM sync_runs")
        for _ in range(5):
            b.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)",
                          (iso(),))
        b.con.commit()
        S.prune_runs(b.con)
        c.equal(b.con.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0], 5,
                "and a table under the cap keeps every row")

    # On the full beat, so it is one statement a minute rather than twelve.
    body = source_of("run")
    after = body.split("if not full:")[-1]
    c.ok("prune_runs" in after,
         "pruned on the full pass rather than every five seconds")

    return c.report()


def check_the_beats_cannot_be_swapped() -> bool:
    """`--fast 60 --interval 5` is the two arguments the wrong way round."""
    c = Check("the two beats cannot be given the wrong way round")

    body = source_of("main")
    c.ok("a.fast > a.interval" in body,
         "a fast beat longer than the full one is refused")
    c.ok("a.fast < 1" in body, "and a zero beat is a loop with no sleep in it")
    c.ok(S.FAST_SECONDS <= S.CYCLE_SECONDS,
         f"the defaults agree ({S.FAST_SECONDS}s inside {S.CYCLE_SECONDS}s)")

    return c.report()


def check_the_loop_stops_when_asked() -> bool:
    """
    The reason the loop came out of `main()` at all.

    One exe runs sync, outbox, the API and Bert in a single process, so
    closing the window has to stop three loops -- and a loop that only ends
    when the process dies cannot drain the outbox on the way out. `stop` is
    a `threading.Event`, checked at the top of each pass and waited on
    instead of slept through, so a shutdown asked for mid-beat does not hold
    the window open for the rest of it.
    """
    c = Check("the loop stops when asked")

    # Already set: it must return before touching Discord or the database,
    # which is what passing None for both proves.
    stop = threading.Event()
    stop.set()
    t0 = time.time()
    S.run(None, None, "guild", "nowhere.db", stop=stop)
    c.ok(time.time() - t0 < 0.5,
         "a loop told to stop before it starts returns at once, "
         "without a connection or a client")

    # And set *during* a wait, which is the real case: the beat is five
    # seconds and nobody should watch a window hang for five seconds.
    stop = threading.Event()
    threading.Timer(0.05, stop.set).start()
    t0 = time.time()
    stopped = S._pause(stop, 5)
    took = time.time() - t0
    c.ok(stopped, "a stop during the wait is reported as a stop")
    c.ok(took < 1.0, f"and cuts the beat short rather than sitting it out "
                     f"({took:.2f}s of a 5s wait)")

    # Nothing is passed by default, so the CLI keeps the loop it had.
    c.ok(S.run.__kwdefaults__.get("stop") is None,
         "and a caller that does not want one is unchanged")

    return c.report()


def check_the_outbox_drains_faster_than_it_publishes() -> bool:
    """
    A card says "Pushing to Discord..." until the drain, and nothing else.

    The outbox did five things on one beat: drain, make the threads tickets
    are waiting on, publish the state channel, publish the status embeds and
    append to the change log. Only the first two are ones anybody is waiting
    on -- the three publishes edit in place and announce nothing. So a change
    queued behind however long those took, and when one of them was slow, or
    a write contended, `drain()` lost its whole turn.

    Found as a Complete sitting unsent for minutes while the state channel
    was being rewritten after 357 messages arrived at once, with the error --
    if there was one -- going to a log Python was buffering.

    Same split, and the same reasoning, as this file's own subject: cheap and
    watched on the fast beat, expensive and unwatched on the slow one.
    """
    c = Check("the outbox drains faster than it publishes")

    import ernie_outbox as ob

    c.ok(ob.FAST_SECONDS < ob.POLL_SECONDS,
         f"the drain beat is the shorter one ({ob.FAST_SECONDS}s "
         f"against {ob.POLL_SECONDS}s)")

    src = (ROOT / "ernie_outbox.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    run = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "run")
    body = ast.get_source_segment(src, run) or ""

    # Which calls sit under an `if full:` and which do not.
    def guarded(name):
        for node in ast.walk(run):
            if not (isinstance(node, ast.If)
                    and getattr(node.test, "id", "") == "full"):
                continue
            if name in (ast.get_source_segment(src, node) or ""):
                return True
        return False

    c.ok(not guarded("drain(con"), "drain runs on every pass")
    c.ok(not guarded("make_threads(con"),
         "and so does making the threads tickets are waiting on")
    for slow in ("ernie_state.publish", "ernie_status.publish",
                 "ernie_changelog.tick"):
        c.ok(guarded(slow), f"{slow} runs only on a full pass")

    # A wall clock, not a count of fast passes -- and the sleep measured from
    # the top of the pass, or a slow one lurches the beat.
    c.ok("next_full" in body, "the full pass is due by a clock")
    c.ok("time.time() - t0" in body,
         "and the beat is measured from the top of the pass, not its end")

    # `--once` is somebody asking for the whole job by hand.
    c.ok("once or" in body, "a single pass by hand still does everything")

    return c.report()


class Publishes:
    """Stands in for the three publishes, recording that it was asked."""

    def __init__(self, order, name):
        self.order, self.name = order, name

    def publish(self, *a, **kw):
        self.order.append(self.name)
        return {"posted": 0, "edited": 0}

    def tick(self, *a, **kw):
        self.order.append(self.name)
        return {"sent": 0, "struck": 0}


class Writer:
    writes_allowed = True


def outbox_pass(interval, stop_after):
    """Run the outbox loop with everything stubbed. Returns the call order.

    `stop_after` is how many drains to allow before asking it to stop, which
    is how a loop with no exit condition is made to have one.
    """
    import os
    import ernie_outbox as ob

    order = []
    stop = threading.Event()
    keep = {k: getattr(ob, k) for k in ("drain", "make_threads", "pending",
                                        "stuck", "ernie_state", "ernie_status",
                                        "ernie_changelog")}
    env = {k: os.environ.get(k) for k in ("STATE_CHANNEL_ID",
                                          "CHANGELOG_CHANNEL_ID")}

    def fake_drain(con, d):
        order.append("drain")
        if order.count("drain") >= stop_after:
            stop.set()
        return {"sent": 0, "skipped": 0, "failed": 0}

    try:
        os.environ["STATE_CHANNEL_ID"] = "chan-1"
        os.environ["CHANGELOG_CHANNEL_ID"] = "chan-2"
        ob.drain = fake_drain
        ob.make_threads = lambda con, d: {"made": 0, "failed": 0}
        ob.pending = lambda con: 0
        ob.stuck = lambda con: []
        ob.ernie_state = Publishes(order, "state")
        ob.ernie_status = Publishes(order, "status")
        ob.ernie_changelog = Publishes(order, "changelog")
        ob.run(None, Writer(), "nowhere.db", interval=interval, fast=0.01,
               stop=stop)
        return order
    finally:
        for k, v in keep.items():
            setattr(ob, k, v)
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def check_a_restart_drains_before_it_publishes() -> bool:
    """The first pass is a fast one, and that is the whole of this.

    It used to be full -- `next_full = 0.0` -- so starting the stack did the
    state channel, the status embeds and the change log before it ever
    called `drain()`. Nobody is waiting on any of those three; they edit in
    place and announce nothing.

    Measured on a 51-card sandbox: the first pass took **4m46s**, and a Close
    pressed 18 seconds into it sat behind the whole of it. Reported as Ernie
    saying nothing when a ticket was closed in Bert, which is exactly what it
    looks like from the board -- and every restart had this window, because
    the first pass was full unconditionally.
    """
    c = Check("a restart drains before it publishes")

    # A long interval, so the first pass cannot be a full one by arriving late.
    order = outbox_pass(interval=3600, stop_after=1)

    c.equal(order, ["drain"],
            "the first pass drains and does nothing else")
    for slow in ("state", "status", "changelog"):
        c.ok(slow not in order,
             f"{slow} is not published before the first drain")

    return c.report()


def check_a_long_publish_is_followed_by_a_drain() -> bool:
    """The other half: the publishes still block, so the pass drains again.

    The beat split made the three publishes less *frequent*, 30s against 5s.
    It never stopped them **blocking**, because all five things run on one
    thread -- so a publish that takes four minutes is four minutes in which
    nothing posts, and without this the change then waits out a whole fast
    beat on top.
    """
    c = Check("a long publish is followed by a drain")

    # interval 0, so every pass is a full one.
    order = outbox_pass(interval=0, stop_after=2)

    c.equal(order, ["drain", "state", "status", "changelog", "drain"],
            "a full pass drains, publishes, and drains again")
    c.ok(order.index("drain") < order.index("state"),
         "the cheap half still goes first")
    c.ok(order[-1] == "drain",
         "and the pass ends on the half somebody is waiting for, so nothing "
         "queued during a publish waits out the beat as well")

    return c.report()


CHECKS = (check_a_restart_drains_before_it_publishes,
          check_a_long_publish_is_followed_by_a_drain,
          check_the_outbox_drains_faster_than_it_publishes,
          check_the_loop_stops_when_asked,
          check_a_fast_pass_skips_what_a_fast_pass_is_for_skipping,
          check_the_loop_holds_its_beat,
          check_a_quiet_fast_pass_says_nothing,
          check_the_audit_table_is_bounded,
          check_the_beats_cannot_be_swapped)
