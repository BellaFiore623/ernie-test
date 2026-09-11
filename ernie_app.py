"""
One process: sync, the outbox, the API and Bert.

The shape the packaging note settles on -- everybody runs everything and
shares only `#ernie-state`, so no machine has to stay powered on and anybody
can open it whenever they like. What that costs is five jobs that fall to a
person today and cannot be asked of a non-technical one: starting four
processes, stopping them in the right order, not starting them twice,
installing Python, and pasting keys into a text file.

**The API stays an API.** uvicorn runs on a thread bound to 127.0.0.1 and
Bert talks to it exactly as it does now -- no refactor of the client, and
`run.sh test bert lan` still works the day somebody does want one backend
shared across a network.

**Qt owns the main thread**, which is not a preference: a QApplication has to
be created on it and its event loop has to run there. So the two background
loops and uvicorn are the threads, and Bert is what `main()` blocks on.

**Closing the window is now what stops the outbox.** Under `run.sh` that is
somebody closing three console windows; here the application has to do it,
and the order matters -- see `shut_down()`.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

import ernie_load as load
import ernie_outbox
import ernie_sync
import ernie_version
from ernie_sync import CONFIG_DIR, Discord, load_env


# One at a time, per user. A shipped exe gets double-clicked twice, and
# without this that is two syncs writing to one database and two outboxes
# publishing to one state channel -- the failure `run.sh` already records
# having happened six times over in a day.
MUTEX_NAME = "Local\\ErnieBert"

# Where uvicorn tries first. Falling back to an ephemeral port rather than
# failing keeps this working beside a stack somebody is already running from
# source, which is exactly the machine it will be tested on.
DEFAULT_PORT = 8787


class AlreadyRunning(RuntimeError):
    pass


def take_lock():
    """Hold the single-instance lock, or raise. Answers the handle.

    A named mutex rather than a pid file: a pid file outlives a crash and
    then lies, and the truthful version of it is what `run.sh` had to grow
    -- reading Windows pids out of `/proc/<job>/winpid` because its own job
    numbers do not outlive the shell. The kernel already keeps this one
    honest.
    """
    if os.name != "nt":
        # A socket bound to a fixed loopback port is the portable equivalent,
        # and it is here so this file can be exercised off Windows.
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", 49731))
        except OSError as e:
            raise AlreadyRunning("another copy is already running") from e
        return s

    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, True, MUTEX_NAME)
    # 183 is ERROR_ALREADY_EXISTS: the mutex was there, so somebody else has
    # it. The handle is still valid and still has to be closed.
    if ctypes.get_last_error() == 183:
        k32.CloseHandle(handle)
        raise AlreadyRunning("another copy is already running")
    return handle


def free_port(prefer: int = DEFAULT_PORT) -> int:
    """`prefer` if it is free, otherwise whatever the OS hands out."""
    for port in (prefer, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            got = s.getsockname()[1]
            s.close()
            return got
        except OSError:
            s.close()
    raise RuntimeError("no port available on 127.0.0.1")


def serve_api(db: str, port: int, ready: threading.Event):
    """uvicorn on a thread, bound to loopback only.

    `ernie_api` reads its database from a module global rather than from
    configuration, because it was written as a script. Setting it here is the
    same thing `main()` does there.
    """
    import uvicorn
    import ernie_api

    ernie_api.DB = db
    ernie_api.check_schema()
    cfg = uvicorn.Config(ernie_api.app, host="127.0.0.1", port=port,
                         log_level="warning")
    server = uvicorn.Server(cfg)

    def note():
        # Bert must not start polling a port nothing is listening on yet: the
        # first poll would fail, and a failed first poll is the "Can't reach
        # Ernie" banner on a board that is fine.
        while not server.started:
            time.sleep(0.02)
        ready.set()

    threading.Thread(target=note, daemon=True).start()
    server.run()
    return server


def build_clients():
    """A Discord client each for the two loops, and never one shared.

    The sync's is constructed with no `allow_writes_for` and the outbox's
    with it, which is not tidiness: it is the guard that keeps a read-only
    loop read-only. One client between them would hand the sync a handle that
    can post.
    """
    token = os.environ.get("DISCORD_TOKEN")
    guild = os.environ.get("DISCORD_GUILD_ID")
    allow = os.environ.get("ALLOW_DISCORD_WRITES")
    if not token or not guild:
        return None, None, None
    return (Discord(token, guild),
            Discord(token, guild, allow_writes_for=allow),
            guild)


def shut_down(db: str, outbox_client, stop: threading.Event, say=print):
    """What closing the window has to do, in this order.

    `UNDO_WINDOW_S` is 60 and the outbox polls every 30, so a change made
    just before closing can be ninety seconds from its customer thread.
    Under `run.sh` that is handled by *not* stopping the outbox and asking
    the person to leave three windows open for a minute. One process cannot
    ask that, so it does the waiting itself.

    **The undo window is spent, not waited out.** Closing is the person
    saying they are done, so an event still inside its window is dispatched
    rather than held -- otherwise quitting would silently drop the last
    minute of work, which is the opposite of what the warning promises.
    """
    stop.set()                      # the loops finish their current pass

    if outbox_client is None:
        return {"drained": 0, "published": False}

    con = load.connect(db)
    try:
        # 1. Everything still owed, including what is inside its window.
        con.execute(
            """UPDATE events SET dispatch_after = datetime('now')
               WHERE dispatch_after IS NOT NULL AND posted_at IS NULL
                 AND undone_at IS NULL
                 AND datetime(dispatch_after) > datetime('now')""")
        con.commit()

        sent = ernie_outbox.drain(con, outbox_client)
        made = ernie_outbox.make_threads(con, outbox_client)

        # 2. The board, one last time, so the other machine sees where things
        #    were left rather than where they were a cycle ago.
        published = False
        channel = os.environ.get("STATE_CHANNEL_ID")
        if channel and outbox_client.writes_allowed:
            import ernie_state
            ernie_state.publish(outbox_client, channel, db)
            published = True
        return {"drained": sent.get("sent", 0) + made.get("made", 0),
                "published": published}
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Ernie and Bert, one process.")
    ap.add_argument("--version", action="version",
                    version=ernie_version.describe())
    ap.add_argument("--env", default="ernie.env")
    ap.add_argument("--db", default=None,
                    help="defaults to the config directory, which is the "
                         "only place a frozen build can write")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--headless", action="store_true",
                    help="start everything except Bert, and stop on Ctrl-C. "
                         "For checking the stack comes up.")
    a = ap.parse_args()

    try:
        lock = take_lock()
    except AlreadyRunning:
        # Nothing on screen yet, so there is nothing to raise; saying so and
        # leaving is the honest version until the window can be found.
        print("Ernie is already running.", file=sys.stderr)
        raise SystemExit(1)

    read = load_env(a.env)
    db = a.db or str(CONFIG_DIR / "ernie.db")
    if a.db is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    say = lambda *x: print(*x, flush=True)
    say(f"ernie_app {ernie_version.describe()}")
    say(f"  config  {read or '(none found)'}")
    say(f"  db      {db}")

    sync_client, outbox_client, guild = build_clients()
    stop = threading.Event()
    threads = []

    port = free_port(a.port)
    ready = threading.Event()
    threads.append(threading.Thread(
        target=serve_api, args=(db, port, ready), daemon=True, name="api"))

    if sync_client is not None:
        def sync_loop():
            con = load.connect(db)
            try:
                ernie_sync.run(con, sync_client, guild, db, stop=stop)
            finally:
                con.close()

        def outbox_loop():
            con = load.connect(db)
            try:
                ernie_outbox.run(con, outbox_client, db, stop=stop)
            finally:
                con.close()

        threads.append(threading.Thread(target=sync_loop, daemon=True,
                                        name="sync"))
        threads.append(threading.Thread(target=outbox_loop, daemon=True,
                                        name="outbox"))
    else:
        print("  no DISCORD_TOKEN: the board will read what is already in "
              "the database and nothing will reach Discord", file=sys.stderr)

    for t in threads:
        t.start()
    if not ready.wait(20):
        say("  the API did not come up")
    say(f"  api     http://127.0.0.1:{port}")

    if a.headless:
        try:
            while not stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        print("stopping...")
        print(" ", shut_down(db, outbox_client, stop))
        return

    # Bert on the main thread, because Qt requires it. Imported here rather
    # than at the top so `--headless` needs no display.
    import bert
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    bert.apply_theme(bert.load_settings().get("theme", bert.THEME_DEFAULT))
    w = bert.Bert(f"http://127.0.0.1:{port}")
    bert._OPEN.append(w)
    w.show()
    app.exec()

    print(" ", shut_down(db, outbox_client, stop))
    del lock


if __name__ == "__main__":
    main()
