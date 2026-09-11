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


# One rotation, checked at startup only. The sync writes a line a minute on
# the full beat, so a machine left running writes on the order of 100 KB a
# day and nothing would ever delete it. Checked at startup rather than as it
# grows because a supervisor that rotates its own log mid-write is a race for
# no benefit: the file turns over when somebody restarts, which on a desktop
# application is often enough.
LOG_MAX_BYTES = 5 * 1024 * 1024


class AlreadyRunning(RuntimeError):
    pass


def open_log():
    """Give a windowed build somewhere to print, and answer where.

    `console=False` is what stops a black window opening behind Bert, and it
    takes stdout and stderr with it -- PyInstaller sets both to None, so every
    `print` in this process becomes a no-op. That silently throws away the
    three things the startup banner exists to answer: which config file was
    found, which database is open, and whether this board can post at all.
    Those are asked *after the fact*, about somebody else's machine, when a
    change did not arrive -- which is exactly when there was no console to
    have been watching.

    So the banner goes to a file instead, in the config directory beside the
    database, because that is the one place a frozen build can write.

    It also fixes a quieter problem. uvicorn configures logging with
    `ext://sys.stderr`, and a StreamHandler over None fails on every record
    and is swallowed by `logging`'s own error handling -- so the API's
    failures would go nowhere rather than somewhere unread. Redirecting
    before the server thread starts gives it a real stream.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None                     # there is a console; use it

    path = CONFIG_DIR / "logs" / "ernie.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_name("ernie.log.1"))
    except OSError:
        # A log that cannot be rotated is not a reason not to run. Worst case
        # it grows, which is the state it was already in.
        pass

    # Line buffered, so a crash keeps whatever was written up to it -- the
    # same reason `say` flushes.
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f
    return path


def already_running_dialog():
    """Say it on screen, because a windowed build has nowhere else to say it.

    With a console, a second double-click printed "Ernie is already running."
    and closed. Without one it exits silently, so the mutex -- the thing that
    stops two syncs writing to one database -- looks exactly like nothing
    happening, and the natural response to nothing happening is to
    double-click again.

    ctypes rather than Qt: there is no QApplication at this point, and making
    one costs a second and a taskbar entry to show a line of text.
    """
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            "Ernie is already running.\n\nLook for Bert's window.",
            "Ernie", 0x40)           # MB_ICONINFORMATION
    except Exception:
        # Never the reason the process fails: it is already leaving.
        pass


def no_config_dialog(env_name: str) -> None:
    """Say why the board is empty, on the one run where nobody can tell.

    A fresh install has no env file, so `build_clients()` answers with no
    clients, the two loops never start, and Bert opens on whatever the
    database holds -- which on a new machine is nothing. The board is blank,
    everything looks like it is working, and the reason is a line in a log
    nobody knows exists yet. That is the worst first five minutes this can
    have, and it is the *ordinary* first five minutes: it happens to everybody
    exactly once.

    It names the file and the directory rather than saying "configuration
    error", because the person reading it has been handed an installer and
    told to run it, and the whole of their problem is that one file is not
    somewhere yet.

    Not fatal. The board still opens, read-only over an empty database, and
    nothing is lost by letting somebody look at it -- refusing to start would
    leave them with a program that closes immediately and says nothing.
    """
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None,
            f"Ernie has no settings file yet, so it is not connected to "
            f"Discord.\n\nIt is looking for:\n\n"
            f"    {CONFIG_DIR / env_name}\n\n"
            f"Ask whoever set this up for that file, put it there, and start "
            f"Ernie again. The board will be empty until then.",
            "Ernie", 0x30)           # MB_ICONWARNING
    except Exception:
        pass


def take_lock(name: str = MUTEX_NAME, port: int = 49731):
    """Hold the single-instance lock, or raise. Answers the handle.

    A named mutex rather than a pid file: a pid file outlives a crash and
    then lies, and the truthful version of it is what `run.sh` had to grow
    -- reading Windows pids out of `/proc/<job>/winpid` because its own job
    numbers do not outlive the shell. The kernel already keeps this one
    honest.

    `name` is an argument so the checks can take a lock of their own. Taking
    the real one means the suite cannot run while Ernie is open, which is
    most of the time now that it is installed -- and it fails as an *error*
    rather than a red check, which reads as the suite being broken. The real
    name is still what `main()` uses, and `check_app.py` holds it against the
    installer's copy.
    """
    if os.name != "nt":
        # A socket bound to a fixed loopback port is the portable equivalent,
        # and it is here so this file can be exercised off Windows.
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
        except OSError as e:
            raise AlreadyRunning("another copy is already running") from e
        return s

    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL,
                                 wintypes.LPCWSTR]
    handle = k32.CreateMutexW(None, True, name)
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
    # **And the update address, which is the other half of that sentence.**
    # `ernie_api` is written as a script: its `__main__` block reads one key
    # out of the env file -- where a newer build comes from -- and sets this
    # module global. `ernie_app` calls `serve_api` instead and inherits none
    # of that, so `/health` published an empty address, Bert drew no button,
    # and the dialog telling somebody there is an update had nowhere to send
    # them. Third time this shape of bug has turned up: the supervisor calls
    # the internals directly and silently skips what the CLI does around them.
    ernie_api.UPDATE_URL = ernie_api.clean_url(
        os.environ.get("BERT_UPDATE_URL"))
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

    # Before anything prints, and before the API thread configures logging
    # over sys.stderr.
    log = open_log()

    # Flushed, because on Windows a piped stdout holds these until the
    # process ends -- which for a supervisor is the one moment they stop
    # being useful.
    def say(*x):
        print(*x, flush=True)

    try:
        lock = take_lock()
    except AlreadyRunning:
        print("Ernie is already running.", file=sys.stderr)
        # With no console that print reaches a file nobody has open, and a
        # second double-click would do nothing visible at all.
        if log is not None:
            already_running_dialog()
        raise SystemExit(1)

    read = load_env(a.env)
    db = a.db or str(CONFIG_DIR / "ernie.db")
    if a.db is None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # A separator, because the log is appended to across runs and the
    # question asked of it is always "what did *this* start do".
    if log is not None:
        say("\n=== " + time.strftime("%Y-%m-%d %H:%M:%S") + " ===")
    # **Create the database before anything can race for it.**
    # `ernie_api.db()` opens read-only -- `mode=ro` cannot create a file that
    # is not there -- and the API, the sync and the outbox threads all start
    # in the same instant. On a machine that has never run this there is no
    # file yet, so whoever arrives first loses: the API dies on a database
    # that does not exist, or the sync cannot take the write lock
    # `schema.sql` wants because a reader already has the file open. Both
    # failures land in a thread, so the board opens, empty, and stays empty
    # with the traceback in a log.
    #
    # Every installation meets this exactly once, on its first run -- which
    # is the worst possible place for it, and the one case that testing
    # against a database you already have can never reach. Found on a real
    # first install, five runs in a row, after the build shipped.
    load.connect(db).close()

    say(f"ernie_app {ernie_version.describe()}")
    say(f"  config  {read or '(none found)'}")
    say(f"  db      {db}")
    if log is not None:
        say(f"  log     {log}")

    sync_client, outbox_client, guild = build_clients()
    # Said at startup the way ernie_sync says it, because "is this board
    # read-only" is the question somebody asks after the fact, off a log,
    # when a change did not arrive. Read-only is a legitimate way to run --
    # it is what production does today -- so it is reported, not refused.
    if outbox_client is not None:
        say(f"  writes   {'ENABLED' if outbox_client.writes_allowed else 'blocked'}"
            f"  (guild {guild})")
        if not outbox_client.writes_allowed:
            say("           nothing will post: set ALLOW_DISCORD_WRITES to "
                "this guild id to go live")
    # **Registering the watched channels belongs to `ernie_sync.main()`, and
    # nothing here was calling it.** `ernie_app` runs `ernie_sync.run()`
    # directly, one layer below the CLI that reads CARD_CHANNEL_IDS and
    # HISTORY_CHANNEL_IDS into `watched_channels` -- so on every machine this
    # was developed on it was already right, because the rows were put there
    # by `run.sh` months ago. On a database that has never seen the CLI, the
    # sync watches nothing, finds nothing, and the board stays empty for
    # ever. No error: there is genuinely nothing to report about a list of
    # channels that is legitimately empty.
    #
    # On the main thread, before any loop starts, for the same reason the
    # database is opened there.
    if sync_client is not None:
        con = load.connect(db)
        try:
            ernie_sync.register_channels(
                con, sync_client, guild,
                os.environ.get("CARD_CHANNEL_IDS", ""),
                os.environ.get("HISTORY_CHANNEL_IDS", ""))
            con.commit()
            # Said out loud, the way the CLI says it: which channels, not just
            # which guild. An empty list here is the whole of the failure
            # above, and it should be readable at a glance rather than
            # inferred from a board that never fills in.
            seen = ernie_sync.watched(con)
            for c in seen.values():
                kind = "cards" if c["generate_cards"] else "history only"
                say(f"  watching #{c['name']} ({c['channel_id']}) -- {kind}")
            if not seen:
                say("  !! watching nothing: set CARD_CHANNEL_IDS in the config "
                    "file, or the board stays empty")
        finally:
            con.close()

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
            except ernie_sync.GuildMismatch as e:
                # The one failure that must not be quiet: everything else
                # still works, so without this the board looks healthy while
                # nothing it does reaches Discord.
                say(f"  !! the outbox has stopped: {e}")
                say("     nothing will reach Discord until "
                    "ALLOW_DISCORD_WRITES names this guild.")
            finally:
                con.close()

        threads.append(threading.Thread(target=sync_loop, daemon=True,
                                        name="sync"))
        threads.append(threading.Thread(target=outbox_loop, daemon=True,
                                        name="outbox"))
    else:
        print("  no DISCORD_TOKEN: the board will read what is already in "
              "the database and nothing will reach Discord", file=sys.stderr)
        # On screen too, when there is no screen to have printed it to. The
        # windowed build is the one somebody has just installed, and this is
        # the run where the message matters most and is least visible.
        if log is not None and not a.headless:
            no_config_dialog(os.path.basename(a.env))

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

    # Before the window is built, because the close warning reads it. Closing
    # Bert here closes the sync and the outbox too, which is the opposite of
    # what that warning assumes from source.
    bert.SUPERVISED = True

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
