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


CHECKS = (check_closing_spends_the_undo_window,
          check_an_undone_change_is_not_resurrected,
          check_the_two_loops_never_share_a_client,
          check_it_will_not_start_twice,
          check_it_finds_a_port_beside_a_running_stack,
          check_qt_gets_the_main_thread)
