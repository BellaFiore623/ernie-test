"""
Which build everybody should be on, read off a pinned note in Discord.

**The update check was dead in the exe, and nothing said so.** It compares
Bert against the Ernie answering it, which is a real comparison while those
are two checkouts -- `run.sh`, or one backend and two Berts. `ernie_app` made
them one process importing one module, so the two numbers became equal by
construction: `build_standing` returned "ok" for every build ever shipped, the
dialog could not appear, and the only symptom was silence.

So the newest build has to come from outside the process. It comes from a pin
in the state channel, because Discord is already load-bearing here -- the
token is on every machine, and a board that cannot reach Discord has bigger
problems than being a version behind. Google Drive holds the installer, which
is what `BERT_UPDATE_URL` already points a browser at.

The rule underneath all of it: **a check about a build may never take a
working board away.** Every path that cannot answer has to answer "ok".
"""

import ernie_state as S
import ernie_version
from support import Board, Check

import bert


class FakePins:
    """Discord, holding whatever pins a check gives it."""

    def __init__(self, pins, shape="list"):
        self.pins = pins
        self.shape = shape
        self.asked = []

    def get(self, path, **kw):
        self.asked.append(path)
        if self.pins is None:
            return None                  # 403, 404, or it never landed
        msgs = [{"content": c} for c in self.pins]
        if self.shape == "items":
            return {"items": [{"message": m} for m in msgs]}
        return msgs


def check_a_release_note_is_read_and_nothing_else_is() -> bool:
    """
    Strict on the way in, because everything downstream is forgiving.

    `as_tuple` reads an unparseable version as 0, so junk that got past the
    parser could only ever make a board look *ahead* of the release -- which
    is a silent no-op, and a silent no-op is the failure nobody ever notices.
    """
    c = Check("a release note is read, and nothing else is")

    c.equal(S.parse_release("**Release** 0.9.1"), "0.9.1", "the plain form")
    c.equal(S.parse_release("**Release** 0.9.1 -- installer is in Drive"),
            "0.9.1", "prose after it is ignored")
    c.equal(S.parse_release("**Release** 1.10.2"), "1.10.2", "multi-digit parts")

    for other, why in (
            ("**Board** — 3 open, 1 closed", "the summary message"),
            ("```json\n{\"thread\": \"1\"}\n```", "a card payload"),
            ("Release 0.9.1", "the marker has to be the marker"),
            ("**Release** soon", "no version in it"),
            ("**Release**", "nothing after the marker"),
            ("we should release 0.9.1 tomorrow", "somebody talking"),
            ("", "an empty message")):
        c.equal(S.parse_release(other), "", f"not a release note: {why}")

    return c.report()


def check_the_note_is_invisible_to_the_board() -> bool:
    """
    It sits in the state channel, which is a channel with opinions.

    `publish()` deletes summary pages the board has outgrown and `clear()`
    deletes state messages -- so a note either of them mistook for its own
    would be removed by the next cycle, and the update check would go quiet
    again with nothing to say why. It is safe because it is neither: `parse()`
    does not read it and it does not start with `SUMMARY_MARK`.
    """
    c = Check("the note is invisible to the board")

    note = "**Release** 0.9.1 -- installer is in Drive"
    c.ok(S.parse(note) is None, "parse() does not read it as a card")
    c.ok(not note.startswith(S.SUMMARY_MARK),
         "and it is not a summary page, so publish() never prunes it")
    # The same question clear() asks, which is the one that deletes.
    c.ok(not (S.parse(note) or note.startswith(S.SUMMARY_MARK)),
         "clear() leaves it alone")
    return c.report()


def check_a_channel_it_cannot_read_changes_nothing() -> bool:
    """
    "" and None are different answers and the distinction is the whole thing.

    "" is a channel read cleanly with no note in it -- taking the note down is
    how somebody says there is no newer build, so the stored answer retires.
    None is not being able to tell: a 403 on a channel somebody
    re-permissioned, or Discord unreachable. Treating that as "no note" would
    have every board quietly decide it is up to date at exactly the moment it
    stopped being able to find out.
    """
    c = Check("a channel it cannot read changes nothing")

    with Board() as b:
        S.note_release(b.con, "0.9.1")
        b.con.commit()

        def stored():
            r = b.con.execute(
                "SELECT version FROM release_seen WHERE id=1").fetchone()
            return r["version"] if r else None

        c.equal(stored(), "0.9.1", "a note is remembered")

        S.note_release(b.con, None)                 # could not tell
        c.equal(stored(), "0.9.1", "None leaves what we already knew")

        S.note_release(b.con, "")                   # read fine, no note
        c.equal(stored(), None, "and an absent note retires it")

        c.equal(S.read_release(FakePins(None), "c"), None,
                "a pins request that came back empty-handed answers None")
        c.equal(S.read_release(FakePins([]), "c"), "",
                "a channel with no pins answers ''")
    return c.report()


def check_the_highest_pinned_version_wins() -> bool:
    """
    Two notes left up is somebody forgetting to unpin the old one.

    Answering with the most recently *pinned* would depend on an order Discord
    does not promise, and answering with the older would tell the whole team
    to downgrade to a build that is no longer in the folder.
    """
    c = Check("the highest pinned version wins")

    d = FakePins(["**Release** 0.9.1", "**Release** 0.10.0", "not a note"])
    c.equal(S.read_release(d, "c"), "0.10.0", "0.10.0 over 0.9.1, not string order")
    c.equal(d.asked, ["/channels/c/pins"], "one request, whatever the board size")

    # Discord has moved this route once already; both shapes have to read.
    c.equal(S.read_release(FakePins(["**Release** 0.9.2"], shape="items"), "c"),
            "0.9.2", "the newer {items: [{message}]} shape too")
    return c.report()


def check_the_exe_can_finally_notice() -> bool:
    """
    The regression this whole feature exists for.

    In one process Bert's version and Ernie's are the same module attribute,
    so `theirs` equals `mine` always. Before the note, that made "behind"
    unreachable in every build we will ever ship.
    """
    c = Check("the exe can finally notice")

    same = "0.9.0"
    state, said = bert.build_standing(same, same, same, "")
    c.equal(state, "ok", "one process and no note: nothing to say")

    state, said = bert.build_standing(same, same, same, "0.9.1")
    c.equal(state, "behind", "one process and a note: behind")
    c.ok("0.9.1" in said and "0.9.0" in said, "and it names both numbers")
    c.ok("the current build is" in said,
         "sourced to the release, not to Ernie -- they send you to "
         "different places")

    # From source the other end is still a real signal, and keeps its wording.
    state, said = bert.build_standing("0.9.0", "0.9.2", None, "0.9.1")
    c.equal(state, "behind", "an Ernie further ahead than the note still counts")
    c.ok("0.9.2" in said and "Ernie is on" in said,
         "and that one is sourced to Ernie")

    c.equal(bert.build_standing("0.9.1", "0.9.1", None, "0.9.1")[0], "ok",
            "already on the release")
    return c.report()


def check_it_fails_open_on_every_path() -> bool:
    """
    A build check has no business taking a working board away.

    Each of these is a field that can be missing or wrong in the field, and
    none of them may produce a worse answer than "ok".
    """
    c = Check("it fails open on every path")

    for theirs, floor, newest, why in (
            (None, None, "", "an Ernie that publishes nothing"),
            (None, None, None, "and a newest of None rather than ''"),
            ("0.9.0", None, "junk", "a note somebody typed prose into"),
            ("0.9.0", None, "0.9", "a short version that pads equal"),
            ("0.9.0", None, "0.9.0", "a note naming the build we are on")):
        state, _ = bert.build_standing("0.9.0", theirs, floor, newest or "")
        c.equal(state, "ok", f"ok: {why}")

    # Junk can only ever read as older, never newer -- the property the strict
    # parser is defending, stated where it is relied on.
    c.ok(not ernie_version.is_older("0.9.0", "nonsense"),
         "unreadable never reads as ahead of a real build")

    # The floor still outranks the note: blocked is a decision somebody made.
    state, _ = bert.build_standing("0.9.0", "0.9.0", "0.9.5", "0.9.1")
    c.equal(state, "blocked", "blocked outranks behind")
    return c.report()


CHECKS = (check_a_release_note_is_read_and_nothing_else_is,
          check_the_note_is_invisible_to_the_board,
          check_a_channel_it_cannot_read_changes_nothing,
          check_the_highest_pinned_version_wins,
          check_the_exe_can_finally_notice,
          check_it_fails_open_on_every_path)
