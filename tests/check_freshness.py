"""
The freshness label reports the mirror, not the poll.

It used to count from the last time Bert asked Ernie. Bert asks every
POLL_MS, so the answer was always "just now" -- a constant dressed up as a
measurement, and a reassuring one: it read exactly the same with the sync
loop dead and the board hours behind Discord.

Two things follow from measuring the mirror instead. Staleness has to come
off the last run that *finished*, because a cycle takes a few seconds and its
row exists with a NULL finished_at throughout -- read off the newest row, the
board announced "never synced" once a minute. And the refresh button has to
wait for a read of Discord rather than for Ernie to answer, because only the
first of those moves the number.

Only the deciding is checked here, not the painting: _say_fresh is the one
call that touches Qt, so the fake records what it was asked to say.
"""

import time

from support import Board, Check, iso

import pathlib
import bert
import ernie_api as api


class FakeButton:
    def __init__(self):
        self.down = False
        self.text = bert.REFRESH_GLYPH

    def setDown(self, v):
        self.down = v

    def setText(self, t):
        self.text = t

    def setIcon(self, i):
        pass

    def setToolTip(self, t):
        pass


class FakeTimer:
    """The spin timer. Whether it runs is the testable part; the painting
    needs a QApplication and is left to the running app."""

    def __init__(self):
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class Running:
    """A poll already on its way back, so refresh() starts no thread."""

    def isRunning(self):
        return True


class FakeBert:
    """Enough of Bert for the freshness label and the refresh button."""

    def __init__(self, *, since=0, health_age=0, reached=True, synced_at="run-1"):
        self.last_sync = time.time() if reached else None
        # since=None models a database with no finished sync run at all.
        self.health = {"seconds_since_sync": since, "synced_at": synced_at}
        self.health_at = time.time() - health_age
        self.awaiting = False
        self.await_run = None
        self.await_since = 0.0
        self.refresh_btn = FakeButton()
        self.spin_timer = FakeTimer()
        self.spin_angle = 0
        self.poller = Running()
        self.dragging = False
        self.said = None

    def _tick_sharing(self):
        pass

    def _tick_roster(self):
        # A third indicator hangs off _tick_freshness now. It has its own
        # check; here it only has to not exist as a hole in the fake.
        pass

    def _say_fresh(self, text, amber, tip):
        self.said = (text, amber, tip)

    _tick_freshness = bert.Bert._tick_freshness
    _check_awaited = bert.Bert._check_awaited
    _stop_awaiting = bert.Bert._stop_awaiting
    _show_busy = bert.Bert._show_busy
    refresh = bert.Bert.refresh


def said(b):
    b._tick_freshness()
    return b.said[0]


def amber(b):
    b._tick_freshness()
    return b.said[1]


def check_mirror_age() -> bool:
    c = Check("the number is the mirror's age")

    c.equal(said(FakeBert(since=2)), "synced just now", "a fresh mirror")
    c.equal(said(FakeBert(since=45)), "synced 45s ago", "seconds")
    c.equal(said(FakeBert(since=600)), "synced 10m ago", "minutes")

    b = FakeBert(since=10, health_age=30)
    c.equal(said(b), "synced 40s ago", "it goes on counting between polls")

    return c.report()


def check_stale_mirror_is_visible() -> bool:
    c = Check("a stopped sync loop is not reported as fresh")

    # The regression, exactly: Bert is talking to Ernie perfectly well and
    # polling every 5s, but Ernie stopped reading Discord four minutes ago.
    # The old label read "updated just now" all the way through this.
    b = FakeBert(since=240)
    c.ok(b.last_sync is not None, "Bert is in contact with Ernie")
    c.equal(said(b), "synced 4m ago", "the board's real age is shown")
    c.ok(amber(b), "and it is amber, not reassuring")
    c.ok("sync loop" in b.said[2], "the tooltip says what to go and check")

    c.ok(not amber(FakeBert(since=bert.MIRROR_STALE_S - 1)), "under the line")
    c.ok(amber(FakeBert(since=bert.MIRROR_STALE_S + 1)), "over it")

    return c.report()


def check_the_other_states() -> bool:
    c = Check("the states that are not a number")

    c.equal(said(FakeBert(reached=False)), "never updated",
            "Bert has never reached Ernie")

    b = FakeBert(since=None, synced_at=None)
    c.equal(said(b), "never synced", "Ernie has no finished sync run")
    c.ok(amber(b), "which is amber too -- nothing has been read yet")

    return c.report()


def check_refresh_waits_for_a_read() -> bool:
    c = Check("refresh waits for the next read of Discord")

    b = FakeBert(since=42, synced_at="run-1")
    b.refresh(manual=True)
    c.ok(b.awaiting, "the press is registered")
    c.ok(b.refresh_btn.down, "the button is held down, so it looks darker")
    c.ok(b.spin_timer.running, "and the glyph is turning")
    c.equal(b.refresh_btn.text, "", "the still glyph is out of the way")
    c.equal(said(b), "refreshing…", "and the label says so")
    c.ok("42s old" in b.said[2], "the tooltip keeps the age to hand")

    # A second press must not restart the wait it is already serving.
    was = b.await_since
    b.refresh(manual=True)
    c.equal(b.await_since, was, "pressing again while waiting changes nothing")

    # Ernie answering again is not the thing being waited for -- the same read
    # of Discord, however freshly fetched, puts the same number back.
    b.health = {"seconds_since_sync": 44, "synced_at": "run-1"}
    b.health_at = time.time()
    c.equal(said(b), "refreshing…", "another answer about the same read waits")
    c.ok(b.awaiting, "still waiting")

    # A new cycle lands.
    b.health = {"seconds_since_sync": 0, "synced_at": "run-2"}
    b.health_at = time.time()
    c.equal(said(b), "synced just now", "a new read ends the wait")
    c.ok(not b.awaiting, "and the wait is over")
    c.ok(not b.refresh_btn.down, "the button comes back up")
    c.ok(not b.spin_timer.running, "the glyph stops turning")
    c.equal(b.refresh_btn.text, bert.REFRESH_GLYPH, "and is the glyph again")

    return c.report()


def check_refresh_gives_up() -> bool:
    c = Check("refresh does not wait for ever")

    b = FakeBert(since=42, synced_at="run-1")
    b.refresh(manual=True)
    b.await_since = time.time() - (bert.AWAIT_GIVEUP_S + 1)
    b.health = {"seconds_since_sync": 300, "synced_at": "run-1"}
    b.health_at = time.time()

    c.equal(said(b), "synced 5m ago", "it falls back to the real age")
    c.ok(amber(b), "which by now is amber, and more use than a spinner")
    c.ok(not b.refresh_btn.down, "the button comes back up")
    c.ok(not b.spin_timer.running, "and stops turning")

    # An automatic poll never starts a wait.
    auto = FakeBert(since=10)
    auto.refresh()
    c.ok(not auto.awaiting, "an automatic poll stays silent")
    c.ok(not auto.refresh_btn.down, "and leaves the button alone")
    c.ok(not auto.spin_timer.running, "with nothing turning")

    return c.report()


def check_health_ignores_a_running_cycle() -> bool:
    c = Check("a cycle in flight is not 'never synced'")

    with Board() as b:
        api.DB = b.path

        b.con.execute("DELETE FROM sync_runs")
        b.con.commit()
        h = api.health()
        c.ok(h["seconds_since_sync"] is None, "a database with no runs has no age")
        c.ok(h["synced_at"] is None, "and no read to point at")

        b.con.execute("INSERT INTO sync_runs (started_at, finished_at) VALUES (?,?)",
                      (iso(-70), iso(-65)))
        b.con.commit()
        h = api.health()
        c.equal(h["seconds_since_sync"], 65, "a finished run gives the age")
        c.ok(not h["syncing"], "and nothing is running")
        first = h["synced_at"]

        # ernie_sync opens the next cycle. Its row exists for the several
        # seconds the cycle takes with finished_at still NULL -- and reading
        # the age off the newest row made the board say "never synced" for
        # exactly that long, once a minute, every minute.
        b.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (iso(-2),))
        b.con.commit()
        h = api.health()
        c.equal(h["seconds_since_sync"], 65,
                "the age still comes off the last finished run")
        c.ok(h["syncing"], "and the cycle in flight is reported as such")
        c.equal(h["synced_at"], first, "the read being waited on hasn't changed")

        # It lands.
        b.con.execute("UPDATE sync_runs SET finished_at=? WHERE finished_at IS NULL",
                      (iso(-1),))
        b.con.commit()
        h = api.health()
        c.equal(h["seconds_since_sync"], 1, "now the age is the new run's")
        c.ok(h["synced_at"] != first, "which Bert can tell apart from the old one")

    return c.report()


def check_a_filling_board_says_it_is_filling() -> bool:
    """A new install is empty for a reason nobody can see.

    A fresh database copies the whole channel into itself before the board
    means anything, and while that is happening Bert looked exactly like a
    board with no tickets on it. Somebody handed a new laptop had no way to
    tell it was working, which is every reason to go and do something else and
    come back to a board they then distrust.

    `synced_at` is the finish of the last run that **completed**, so it is
    empty for precisely as long as no pass has ever reached the end -- and on
    a fresh database the first pass is the long one, because every thread is
    new and all its messages have to be fetched.

    Three things are held here, and the third is the one that matters most: it
    has to stop. A panel that says "still downloading" over a finished board,
    or over a failure, is worse than no panel.
    """
    c = Check("a filling board says it is filling")

    with Board() as b:
        was, api.DB = api.DB, b.path
        try:
            # 1. A database nothing has run against yet.
            h = api.health()
            c.equal(h["synced_at"], None, "a brand new board has finished no sync")
            c.ok(bert.still_arriving(h), "so the panel shows")

            # 2. A pass under way, cards arriving. The count is the honest one:
            #    sync_runs gets its totals only at the end, but cards are
            #    written as they come, so board_size is what rises.
            b.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)", (iso(),))
            b.con.commit()
            h = api.health()
            c.ok(h["syncing"], "the run is reported as in flight")
            c.ok(bert.still_arriving(h), "and the panel is still up")
            c.ok("board_size" in h,
                 "with a count that rises while it works, which is what it shows")

            # 3. It stops the moment a pass completes.
            b.con.execute("UPDATE sync_runs SET finished_at=?, threads_seen=34",
                          (iso(),))
            b.con.commit()
            c.ok(not bert.still_arriving(api.health()),
                 "and it is gone once a pass has finished")

            # An errored pass stamps finished_at too, so the panel does not sit
            # over a failure telling somebody to keep waiting -- the ordinary
            # error path owns that.
            b.con.execute("UPDATE sync_runs SET error='no'")
            b.con.commit()
            c.ok(not bert.still_arriving(api.health()),
                 "nor does it outlive a first pass that failed")
        finally:
            api.DB = was

    # Fails closed. No health at all is "cannot reach Ernie", which has its own
    # indicator; claiming a download is running when we cannot ask would be
    # saying the wrong thing confidently.
    c.ok(not bert.still_arriving(None),
         "and it says nothing when Bert cannot reach Ernie at all")
    c.ok(not bert.still_arriving({}), "or when the answer carries nothing")

    # It is drawn where the other strips are, and updated on the same beat --
    # a panel built but never ticked is the shape that would pass everything
    # above and show nothing.
    src = pathlib.Path(bert.__file__).read_text(encoding="utf-8")
    c.ok("self._tick_setup()" in src, "the poll ticks it")
    c.ok("still_arriving(self.health)" in src, "off the pure decision")
    c.ok("UPDATE_FACE" in src.split("self.setup = QFrame()")[1][:800],
         "and it carries the face, which is the point of it being a panel")

    return c.report()


CHECKS = (check_mirror_age, check_stale_mirror_is_visible,
          check_the_other_states, check_refresh_waits_for_a_read,
          check_refresh_gives_up, check_health_ignores_a_running_cycle,
          check_a_filling_board_says_it_is_filling)
