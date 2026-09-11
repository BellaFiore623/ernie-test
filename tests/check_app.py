"""
One process running sync, the outbox, the API and Bert.

The shape the packaging note settles on: everybody runs everything and shares
only `#ernie-state`, so no machine has to stay powered on. What it costs is
five jobs that fall to a person today and cannot be asked of a non-technical
one -- starting four processes, stopping them in the right order, not
starting them twice, installing Python, and pasting keys into a text file.

Two of those are what this file holds down. **Not starting it twice**, which
`run.sh` already records failing six times over in one day: six syncs writing
to one database and six outboxes publishing to one state channel. And
**stopping in the right order**, which is new here -- under `run.sh` the
outbox is deliberately *not* stopped and the person is asked to leave three
windows open for a minute, and one process cannot ask that.
"""

import ast
import os
import pathlib
import threading

from support import Board, Check, iso

import ernie_app


ROOT = pathlib.Path(__file__).resolve().parent.parent


class Fake:
    """Discord, counting what it was told to do."""

    def __init__(self, writes=True):
        self.calls = []
        self.guild_id = "guild"
        self.writes_allowed = writes

    def write(self, verb, path, **kw):
        self.calls.append((verb, path.rsplit("/", 1)[-1]))
        return {"id": f"m{len(self.calls)}"}


def check_closing_spends_the_undo_window() -> bool:
    """
    Closing is the person saying they are done.

    `UNDO_WINDOW_S` is 60 and the outbox polls every 30, so a change made
    just before closing can be ninety seconds from its customer thread. An
    event still inside its window is therefore **brought forward and sent**,
    not waited out and not dropped -- dropping it would make quitting lose
    the last minute of work, which is the opposite of what Bert's close
    warning promises.
    """
    c = Check("closing spends the undo window rather than losing it")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        b.event(tid, verb="completed", old=None, new="done",
                dispatch_after=iso(+55))       # the outbox would not touch it
        b.con.commit()

        due = b.con.execute(
            """SELECT COUNT(*) FROM events
               WHERE datetime(dispatch_after) <= datetime('now')
                 AND posted_at IS NULL""").fetchone()[0]
        c.equal(due, 0, "nothing is due yet, so a plain drain would send none")

        d = Fake()
        out = ernie_app.shut_down(b.path, d, threading.Event())
        c.equal(out["drained"], 1, "closing sends it anyway")
        c.equal(b.con.execute(
            """SELECT COUNT(*) FROM events WHERE dispatch_after IS NOT NULL
               AND posted_at IS NULL AND undone_at IS NULL""").fetchone()[0],
            0, "and nothing is left owed")
        c.ok(any(v == "POST" for v, _ in d.calls),
             f"the message reached Discord ({d.calls})")

    return c.report()


def check_an_undone_change_is_not_resurrected() -> bool:
    """
    Bringing dispatch forward must not reach past `undone_at`.

    Undo inside the window deletes nothing from Discord because nothing was
    ever sent -- so an event somebody took back and then closed the window on
    has to stay taken back. It is the one row that looks exactly like the one
    above until you read the column that says it was cancelled.
    """
    c = Check("an undone change is not resurrected by closing")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        b.event(tid, verb="completed", old=None, new="done",
                dispatch_after=iso(+55), undone_at=iso(-1))
        b.con.commit()

        d = Fake()
        out = ernie_app.shut_down(b.path, d, threading.Event())
        c.equal(out["drained"], 0, "nothing is sent")
        c.equal(d.calls, [], "and Discord is not spoken to at all")

    return c.report()


def check_the_two_loops_never_share_a_client() -> bool:
    """
    The sync is read-only against Discord, and that is a guard, not a habit.

    `Discord.write()` refuses unless `ALLOW_DISCORD_WRITES` names the exact
    guild, and the sync's client is built without it. Handing both loops one
    client would give the read-only half a handle that can post -- and the
    hard rule is that every write goes through the one place the guard lives.
    """
    c = Check("the two loops never share a Discord client")

    was = {k: os.environ.get(k) for k in
           ("DISCORD_TOKEN", "DISCORD_GUILD_ID", "ALLOW_DISCORD_WRITES")}
    try:
        os.environ["DISCORD_TOKEN"] = "not-a-real-token"
        os.environ["DISCORD_GUILD_ID"] = "123"
        os.environ["ALLOW_DISCORD_WRITES"] = "123"
        reader, writer, guild = ernie_app.build_clients()

        c.ok(reader is not writer, "two clients, not one passed twice")
        c.ok(not reader.writes_allowed,
             "the sync's cannot write, whatever the env says")
        c.ok(writer.writes_allowed, "and the outbox's can")
        c.equal(guild, "123", "both are pointed at the guild named")

        # And with the allow line absent -- which is what production's env
        # looks like -- neither of them can.
        del os.environ["ALLOW_DISCORD_WRITES"]
        reader, writer, _ = ernie_app.build_clients()
        c.ok(not reader.writes_allowed and not writer.writes_allowed,
             "with no ALLOW_DISCORD_WRITES, nothing posts")
    finally:
        for k, v in was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    return c.report()


def check_it_will_not_start_twice() -> bool:
    """
    A shipped exe gets double-clicked twice.

    Without a lock that is two syncs on one database and two outboxes on one
    state channel, which `run.sh` already recorded happening six times over
    in a single day -- and which is where `outbox.log`'s rate limits came
    from. A named mutex rather than a pid file: a pid file outlives a crash
    and then lies, and the honest version of it is what `run.sh` had to grow.
    """
    c = Check("it will not start twice")

    first = ernie_app.take_lock()
    try:
        raised = False
        try:
            ernie_app.take_lock()
        except ernie_app.AlreadyRunning:
            raised = True
        c.ok(raised, "a second copy is refused while the first holds the lock")
    finally:
        # Freeing it is the platform's business either way: a handle on
        # Windows, a socket elsewhere.
        if hasattr(first, "close"):
            first.close()
        else:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(first)

    after = ernie_app.take_lock()
    c.ok(after is not None, "and the lock is free again once it is let go")
    if hasattr(after, "close"):
        after.close()
    else:
        import ctypes
        ctypes.WinDLL("kernel32").CloseHandle(after)

    return c.report()


def check_it_finds_a_port_beside_a_running_stack() -> bool:
    """
    The machine this is first tested on is the one already running `run.sh`.

    Failing to start because 8787 is taken would make the first thing anybody
    tries look broken, so a taken port falls through to whatever the OS
    hands out and Bert is told where to look rather than assuming.
    """
    c = Check("it finds a port beside a running stack")

    import socket
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    taken = held.getsockname()[1]
    held.listen(1)
    try:
        got = ernie_app.free_port(taken)
        c.ok(got != taken, f"a taken port is not handed out again ({got})")
        c.ok(got > 0, "and something usable comes back instead")
    finally:
        held.close()

    c.equal(ernie_app.free_port(0) > 0, True, "0 asks the OS outright")

    # Bert is given the port that was actually bound, not the default.
    src = (ROOT / "ernie_app.py").read_text(encoding="utf-8")
    body = ast.get_source_segment(src, next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "main")) or ""
    c.ok("127.0.0.1:{port}" in body,
         "and Bert is pointed at the port that was bound")

    return c.report()


def check_qt_gets_the_main_thread() -> bool:
    """
    Not a preference: a QApplication has to be created on the main thread and
    its event loop has to run there. So the two loops and uvicorn are the
    threads, and Bert is what `main()` blocks on -- which also makes the
    shutdown sequence reachable, because it is simply the code after
    `app.exec()` returns.
    """
    c = Check("Qt gets the main thread")

    src = (ROOT / "ernie_app.py").read_text(encoding="utf-8")
    body = ast.get_source_segment(src, next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "main")) or ""

    c.ok("app.exec()" in body, "the Qt loop runs in main()")
    for name in ("sync", "outbox", "api"):
        c.ok(f'name="{name}"' in body, f"the {name} loop is a named thread")
    # After `app.exec()` specifically, and asked that way round: `--headless`
    # has its own shut_down call earlier in the function, so "is there one
    # after the first mention" answers about the wrong branch.
    c.ok("shut_down" in body.split("app.exec()")[-1],
         "and the shutdown runs after the window closes, not before")
    c.ok("import bert" in body,
         "Bert is imported inside main(), so --headless needs no display")

    return c.report()


def check_a_blocked_outbox_says_so() -> bool:
    """
    The one failure that must not be quiet.

    Production's env has no `ALLOW_DISCORD_WRITES` -- deliberately, it is the
    line that decides whether anything posts -- so it is the first
    configuration this application will ever meet. The outbox loop used to
    answer a blocked write with `sys.exit`, which is fine in a CLI and is not
    fine on a thread: `SystemExit` there is swallowed by `threading` without
    a word, so the outbox would stop, the sync and the board would carry on
    working, and nothing anybody did would reach Discord.

    Everything else about a read-only board is legitimate -- it is how
    production runs today -- so this is reported, never refused.
    """
    c = Check("a blocked outbox says so rather than vanishing")

    from ernie_sync import GuildMismatch
    import ernie_outbox

    class Blocked:
        guild_id = "g"
        writes_allowed = False

        def write(self, *a, **k):
            raise GuildMismatch("Write blocked.")

    with Board() as b:
        tid = b.card("PROD: A - 01Jan26 - x", "medium")
        b.event(tid, verb="completed", new="done", dispatch_after=iso(-60))
        b.con.commit()
        raised = False
        try:
            ernie_outbox.run(b.con, Blocked(), b.path, once=True)
        except GuildMismatch:
            raised = True
        c.ok(raised, "run() raises it out rather than exiting the thread")

    # Off the AST, not the text: asked as "sys.exit is not in run()", this
    # failed on the comment explaining why it must not be -- which is the
    # third time a check has read the prose about a rule instead of the code
    # obeying it.
    src = (ROOT / "ernie_outbox.py").read_text(encoding="utf-8")
    loop = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "run")
    exits = [n for n in ast.walk(loop) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "exit"
             and getattr(getattr(n.func, "value", None), "id", "") == "sys"]
    c.equal(len(exits), 0,
            "and the loop does not exit a process it may not be the only "
            "thing in")

    app = (ROOT / "ernie_app.py").read_text(encoding="utf-8")
    c.ok("GuildMismatch" in app, "the supervisor catches it")
    c.ok("ALLOW_DISCORD_WRITES" in app,
         "and names the line that would fix it")
    c.ok("writes_allowed" in app,
         "and says at startup whether this board can post at all")

    return c.report()


def check_the_close_warning_is_true_in_one_process() -> bool:
    """
    The warning inverts under a supervisor, and left alone it would have been
    wrong in both halves.

    From source it says: closing Bert loses nothing, because the outbox is
    another process that goes on posting -- but if you are shutting the whole
    stack down, leave the rest running another minute. Under `ernie_app` there
    **is** no rest: closing this window stops the sync and the outbox with it,
    and `shut_down` brings everything owed forward and sends it. So the advice
    names something the person cannot do, about a loss that cannot happen.

    And it hid the consequence that is real. Bringing an event forward
    **spends its undo window**: a change made ten seconds before closing goes
    to the customer thread as it stands, and the chance to take it back goes
    with it. That is what the supervised wording has to say.

    Read off the string literals rather than the source text, because the
    reasoning above each branch quotes the words the other branch uses -- a
    substring search over the function trips on its own comments, which has
    happened three times in this project already.
    """
    c = Check("the close warning is true in one process")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "closeEvent")

    branch = next((n for n in ast.walk(fn)
                   if isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                   and n.test.id == "SUPERVISED"), None)
    c.ok(branch is not None, "closeEvent asks whether it is supervised")
    if branch is None:
        return c.report()

    def words(node):
        return " ".join(x.value for x in ast.walk(node)
                        if isinstance(x, ast.Constant)
                        and isinstance(x.value, str)).lower()

    supervised = words(branch)
    inside = {id(x) for x in ast.walk(branch)}
    from_source = " ".join(
        x.value for x in ast.walk(fn)
        if isinstance(x, ast.Constant) and isinstance(x.value, str)
        and id(x) not in inside).lower()

    # The supervised half.
    c.ok("undo window" in supervised,
         "supervised: says the undo window is what closing spends")
    c.ok("leave" not in supervised,
         "supervised: does not ask anybody to leave anything running")
    c.ok("whether bert is open or not" not in supervised,
         "supervised: does not claim the outbox outlives the window")

    # And the from-source half is untouched, because that stack is still real
    # -- `run.sh` and `bert.cmd` are how the other machine runs today.
    c.ok("leave" in from_source,
         "from source: still says to leave the rest running")

    # The flag has to default to off, or a Bert started by bert.cmd gets the
    # supervised wording and is told its close posts things it cannot post.
    assign = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", "") == "SUPERVISED" for t in n.targets)),
                  None)
    c.ok(assign is not None and assign.value.value is False,
         "SUPERVISED is False unless something says otherwise")

    # And ernie_app is the something, before the window exists to read it.
    app_src = (ROOT / "ernie_app.py").read_text(encoding="utf-8")
    body = ast.get_source_segment(app_src, next(
        n for n in ast.walk(ast.parse(app_src))
        if isinstance(n, ast.FunctionDef) and n.name == "main")) or ""
    c.ok("bert.SUPERVISED = True" in body, "ernie_app sets it")
    c.ok(body.index("bert.SUPERVISED = True") < body.index("bert.Bert("),
         "and sets it before building the window")

    return c.report()


CHECKS = (check_a_blocked_outbox_says_so,
          check_closing_spends_the_undo_window,
          check_an_undone_change_is_not_resurrected,
          check_the_two_loops_never_share_a_client,
          check_it_will_not_start_twice,
          check_it_finds_a_port_beside_a_running_stack,
          check_qt_gets_the_main_thread,
          check_the_close_warning_is_true_in_one_process)
