"""
A board in a throwaway database, and the scaffolding the checks share.

Built from schema.sql rather than copied from ernie-test.db: the databases are
gitignored, so a check that needs one only runs on the machine that happens to
have it. This runs anywhere the repository does.

Nothing here talks to Discord. The state channel is a dict.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ernie_load as load          # noqa: E402  (after the path insert)

GUILD = "test-guild"
PARENT = "test-channel"


def iso(offset_s: float = 0) -> str:
    """An ISO timestamp `offset_s` from now. Negative is in the past."""
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat()


class Board:
    """
    A database with cards in it, deleted when the block exits.

    The rows are the minimum load_board() reads: a thread, a title for the
    view to join against, and a card. Everything else a check needs it adds
    itself, so what a check depends on is visible in the check.
    """

    def __init__(self):
        self._dir = tempfile.TemporaryDirectory(prefix="ernie-check-")
        self.path = str(pathlib.Path(self._dir.name) / "board.db")
        self.con = load.connect(self.path)     # applies schema.sql
        self._n = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        try:
            self.con.close()
        finally:
            self._dir.cleanup()

    # -- building it -------------------------------------------------------

    def card(self, name: str, priority: str = "unassigned", rank: float = 1000.0,
             completed: bool = False) -> str:
        """One card with a readable title. Returns its thread_id."""
        self._n += 1
        tid = f"thread-{self._n:03d}"
        self.con.execute(
            """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                    first_seen_at, last_synced_at)
               VALUES (?,?,?,?,?,?)""",
            (tid, PARENT, GUILD, iso(-3600), iso(-3600), iso()))
        self.con.execute(
            """INSERT INTO thread_titles (thread_id, observed_at, name, confidence)
               VALUES (?,?,?,?)""", (tid, iso(-3600), name, "ok"))
        self.con.execute(
            """INSERT INTO cards (thread_id, priority, rank, updated_at, completed_at)
               VALUES (?,?,?,?,?)""",
            (tid, priority, rank, iso(), iso(-60) if completed else None))
        self.con.commit()
        return tid

    def event(self, thread_id: str, *, verb: str = "priority_changed",
              actor: str = "Tester", old: str | None = "unassigned",
              new: str | None = "low", occurred_at: str | None = None,
              dispatch_after: str | None = None, posted_at: str | None = None,
              undone_at: str | None = None) -> str:
        """One row in the feed. Returns its event_id."""
        eid = str(uuid.uuid4())
        self.con.execute(
            """INSERT INTO events (event_id, occurred_at, actor_name, thread_id,
                                   verb, old_value, new_value, dispatch_after,
                                   posted_at, undone_at, undone_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, occurred_at or iso(), actor, thread_id, verb, old, new,
             dispatch_after, posted_at, undone_at,
             "Tester" if undone_at else None))
        self.con.commit()
        return eid

    def logged(self, event_id: str, *, sent_at: str, message_id: str = "log-msg"):
        """Pretend a line for this event is already up in the change log."""
        self.con.execute(
            """INSERT OR REPLACE INTO changelog_sent (event_id, message_id, sent_at)
               VALUES (?,?,?)""", (event_id, f"{message_id}-{event_id[:6]}", sent_at))
        self.con.commit()

    # -- reading it back ---------------------------------------------------

    def state_sync(self) -> dict[str, sqlite3.Row]:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return {r["thread_id"]: r
                    for r in con.execute("SELECT * FROM state_sync")}
        finally:
            con.close()


class FakeDiscord:
    """
    Stands in for Discord.write(). Records the calls; sends nothing.

    Deliberately not a mock of the real client: these checks are about what
    the modules decide, not how they talk, and a fake that answers POST and
    PATCH is the whole surface they touch.
    """

    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []

    def write(self, verb: str, path: str, **kw):
        self.calls.append((verb, path, kw.get("content", "")))
        return {"id": f"msg-{len(self.calls)}"}

    def verbs(self) -> list[str]:
        return [v for v, _, _ in self.calls]


# -- the tiny bit of test runner this needs --------------------------------

class Check:
    """Counts assertions so a passing run says what it actually proved."""

    def __init__(self, title: str):
        self.title = title
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, condition, label: str):
        if condition:
            self.passed += 1
            print(f"    ok   {label}")
        else:
            self.failed.append(label)
            print(f"    FAIL {label}")

    def equal(self, got, want, label: str):
        self.ok(got == want, f"{label}  (got {got!r}, want {want!r})"
                if got != want else label)

    def report(self) -> bool:
        if self.failed:
            print(f"  {self.title}: {len(self.failed)} FAILED, {self.passed} passed")
            return False
        print(f"  {self.title}: {self.passed} passed")
        return True
