"""
Rebuilding the renames Ernie was never there to watch.

Discord posts a system message for every thread rename -- type 4, carrying
the new name -- and those have always been in the mirror. What was missing
was the type that tells one from somebody pasting a title into the chat, and
once that was fetched the renames became a title history that could be
written down.

Two rules keep it honest, and both are here because breaking either would be
silent. **It may not change what the board shows today**: a rename is written
only when it is strictly older than the earliest row we already hold, so the
newest revision is never one this invented, and every card's queue and client
come from where they always did. And **it writes through `record_title`**,
the one place that decides what a title row holds -- a row parsed some other
way is the bug that left cards grey with "unknown client" for titles that
read perfectly well.
"""

import pathlib
import sys

from support import PARENT, Board, Check, iso

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "tools"))

import ernie_api as api             # noqa: E402
import ernie_extract as ex          # noqa: E402
import rebuild_title_history as R   # noqa: E402


def a_thread(b, tid, name, titled_days_ago, card=True):
    """A thread whose title was first recorded some days ago."""
    b.con.execute(
        """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                first_seen_at, last_synced_at)
           VALUES (?,?,?,?,?,?)""",
        (tid, PARENT, "guild", iso(-86400 * 400), iso(-86400 * 400), iso()))
    b.con.execute(
        """INSERT INTO thread_titles (thread_id, observed_at, name, queue,
                                      confidence)
           VALUES (?,?,?,?,?)""",
        (tid, iso(-86400 * titled_days_ago), name, name.split(":")[0],
         "strict"))
    if card:
        b.con.execute(
            """INSERT INTO cards (thread_id, priority, rank, updated_at)
               VALUES (?,?,?,?)""", (tid, "medium", 1000.0, iso()))
    b.con.commit()
    return tid


def a_rename(b, tid, mid, name, days_ago, mtype=4):
    """The system message Discord posts when a thread is renamed."""
    b.con.execute(
        """INSERT INTO messages (message_id, thread_id, author_id,
                                 author_name, is_bot, type, created_at,
                                 first_seen_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (mid, tid, "u1", "tyler_mazza", 0, mtype, iso(-86400 * days_ago),
         iso(-86400 * days_ago)))
    b.con.execute(
        """INSERT INTO message_revisions (message_id, observed_at, content)
           VALUES (?,?,?)""", (mid, iso(-86400 * days_ago), name))
    b.con.commit()


def check_a_rename_becomes_the_revision_it_was() -> bool:
    """
    The history goes in, parsed, and the figure can see it.

    A thread renamed from PROD to OPS a year before Ernie ever looked at it
    has one title row saying OPS, so nothing anywhere knows it was ever PROD.
    The rename message says so exactly, with the day it happened on it.
    """
    c = Check("a rename becomes the revision it was")

    with Board() as b:
        tid = a_thread(b, "t1", "OPS: Trafford Borough - 02Sep26 - x", 30)
        a_rename(b, tid, "m1", "OPS: Trafford Borough - 02Sep26 - x", 200)
        # ^ the rename that produced the name we hold, and one before it
        a_rename(b, tid, "m0", "PROD: Trafford Borough - 02Sep26 - x", 300)

        out = R.rebuild(b.con)
        c.equal(out["written"], 2, "both renames written as revisions")

        rows = b.con.execute(
            "SELECT observed_at, name, queue, confidence FROM thread_titles "
            "WHERE thread_id=? ORDER BY observed_at", (tid,)).fetchall()
        c.equal(len(rows), 3, "three revisions now, oldest first")
        c.equal([r["queue"] for r in rows], ["PROD", "OPS", "OPS"],
                "and the tag on each is parsed, not left NULL")
        c.equal(rows[0]["confidence"], "strict",
                "through record_title, so a readable title reads as readable")

        api.DB = b.path
        moves = api.stats(days=365)["tag_moves"]["moves"]
        c.equal([(m["from"], m["to"], m["count"]) for m in moves],
                [("PROD", "OPS", 1)],
                "and /stats can see the change that was always there")

    return c.report()


def check_it_cannot_change_what_the_board_shows() -> bool:
    """
    The rule that makes this safe to run on a live database.

    Only renames strictly older than the earliest row we hold are written, so
    the newest revision is never one this invented -- and the newest revision
    is what `v_thread_current` answers with, which is where every card gets
    its queue and its client. A rename dated later is the sync's job: it will
    see the current name on its next pass. Measured against production, that
    is 2 of 696.
    """
    c = Check("it cannot change what the board shows")

    with Board() as b:
        tid = a_thread(b, "t1", "OPS: Trafford Borough - 02Sep26 - x", 30)
        # Newer than the row we hold, and a different tag: exactly the row
        # that would move the card if it went in.
        a_rename(b, tid, "m1", "ENG: Trafford Borough - 02Sep26 - x", 5)

        before = b.con.execute(
            "SELECT queue, name FROM v_thread_current WHERE thread_id=?",
            (tid,)).fetchone()
        out = R.rebuild(b.con)
        after = b.con.execute(
            "SELECT queue, name FROM v_thread_current WHERE thread_id=?",
            (tid,)).fetchone()

        c.equal(out["written"], 0, "nothing written")
        c.equal(out["not_history"], 1, "and it is counted as the sync's job")
        c.equal(after["queue"], before["queue"],
                "the card's tag is exactly where it was")
        c.equal(after["name"], before["name"], "and so is its title")

    return c.report()


def check_a_familiar_name_still_gets_its_own_date() -> bool:
    """
    The rule that was wrong first time round, and wrong in a way that counts.

    Production holds one row per thread, carrying the name as of its first
    sync -- which is usually the name the *last* rename gave it. Skipping a
    rename because its name was already familiar left that revision dated the
    day Ernie first looked instead of the day it happened, so a thread that
    went OPS last April counted as having gone OPS in August: inside windows
    it falls outside, on a panel whose whole point is the window.

    So only an exact row -- same thread, same moment -- counts as one we have.
    Two rows carrying one name is not noise; it is the difference between
    when it was renamed and when we first saw it, and they share a tag, so
    having both invents no move.
    """
    c = Check("a familiar name still gets its own date")

    with Board() as b:
        tid = a_thread(b, "t1", "OPS: A - 02Sep26 - x", 30)
        a_rename(b, tid, "m0", "PROD: A - 02Sep26 - x", 300)
        # The rename that gave it the name we hold, 200 days before we looked.
        a_rename(b, tid, "m1", "OPS: A - 02Sep26 - x", 200)

        c.equal(R.rebuild(b.con)["written"], 2, "both written")
        rows = b.con.execute(
            "SELECT observed_at, queue FROM thread_titles WHERE thread_id=? "
            "ORDER BY observed_at", (tid,)).fetchall()
        c.equal([r["queue"] for r in rows], ["PROD", "OPS", "OPS"],
                "three revisions, the last two sharing a tag")

        api.DB = b.path
        # The move is dated where it happened, so a window that ends before
        # the sync still contains it.
        c.equal(api.stats(days=250)["tag_moves"]["total"], 1,
                "the change is inside a 250-day window")
        c.equal(api.stats(days=150)["tag_moves"]["total"], 0,
                "and outside a 150-day one, because it happened at 200")

    return c.report()


def check_only_renames_count() -> bool:
    """
    Type 4 and nothing else, which is the whole reason the type was stored.

    A person pasting a title into the chat writes a message whose content is
    a title and whose author is a person. 550 of production's 573
    title-shaped messages are exactly that. Reading those as renames would
    invent a history rather than recover one.
    """
    c = Check("only renames count")

    with Board() as b:
        tid = a_thread(b, "t1", "OPS: Trafford Borough - 02Sep26 - x", 30)
        # Same content, same age, same author -- a paste, not a rename.
        a_rename(b, tid, "m1", "PROD: Trafford Borough - 02Sep26 - x", 200,
                 mtype=0)

        out = R.rebuild(b.con)
        c.equal(out["renames"], 0, "a type 0 message is not a rename")
        c.equal(out["written"], 0, "so nothing is written from it")

    return c.report()


def check_running_it_twice_changes_nothing() -> bool:
    """
    It will be run again -- after the next backfill, or by somebody unsure
    whether the first one finished. `thread_titles` is keyed on
    (thread_id, observed_at), so the second pass rewrites the same rows.
    """
    c = Check("running it twice changes nothing")

    with Board() as b:
        tid = a_thread(b, "t1", "OPS: A - 02Sep26 - x", 30)
        a_rename(b, tid, "m0", "PROD: A - 02Sep26 - x", 300)

        first = R.rebuild(b.con)
        rows = b.con.execute("SELECT COUNT(*) n FROM thread_titles").fetchone()["n"]
        second = R.rebuild(b.con)
        again = b.con.execute("SELECT COUNT(*) n FROM thread_titles").fetchone()["n"]

        c.equal(first["written"], 1, "written the first time")
        c.equal(second["written"], 0, "and recognised the second")
        c.equal(second["already"], 1,
                "as one we already hold, which is the reason it must give")
        c.equal(second["not_history"], 0,
                "and never as the sync's job -- it is a row this wrote, and "
                "asking the age question first said so about all 694 of them")
        c.equal(rows, again, "with no extra rows either way")

    return c.report()


def check_a_thread_with_no_title_row_is_left_alone() -> bool:
    """
    Inventing its history would be inventing its present.

    A thread the sync has never made a title row for has nothing to be older
    than, so any row written would be the newest one -- which is the case the
    rule above exists to prevent, arrived at from the other side.
    """
    c = Check("a thread with no title row is left alone")

    with Board() as b:
        b.con.execute(
            """INSERT INTO threads (thread_id, parent_id, guild_id,
                                    created_at, first_seen_at, last_synced_at)
               VALUES (?,?,?,?,?,?)""",
            ("bare", PARENT, "guild", iso(-86400), iso(-86400), iso()))
        b.con.commit()
        a_rename(b, "bare", "m1", "OPS: A - 02Sep26 - x", 200)

        out = R.rebuild(b.con)
        c.equal(out["written"], 0, "nothing written")
        c.equal(out["no_thread"], 1, "and it is counted, not silently dropped")
        c.equal(b.con.execute("SELECT COUNT(*) n FROM thread_titles")
                .fetchone()["n"], 0, "the thread still has no title row")

    return c.report()


def check_a_title_with_no_client_keeps_its_whole_date() -> bool:
    """A missing client must not be paid for out of the date.

    LOOSE's client group was `.+?`, which has to match at least one character
    -- so a title with no client at all satisfied it with the first digit of
    the date. `PROD: 29Jun26 - Trade show TOF` parsed as client "2" and date
    the 9th of June: not a missing client, a *wrong date*, twenty days out,
    with nothing on the card to say so. Found when somebody edited a title in
    the sandbox to take the client out.

    It is `.*?` now. Non-greedy means the engine tries the empty client first,
    so the date claims the digits that belong to it, and a real client still
    wins by backtracking -- including one that starts with a digit, which is
    the case that would break a cruder fix.

    STRICT never had it: the separator between its client and its date is
    mandatory, so a bare date cannot be split across the two groups.
    """
    c = Check("a missing client does not eat the date")

    for name, date_want in (("PROD: 29Jun26 - Trade show TOF", "2026-06-29"),
                            ("PROD: 08Sep26 - something", "2026-09-08"),
                            ("OPS: 1Jan26 - x", "2026-01-01")):
        t = ex.parse_title(name)
        c.equal(str(t.date), date_want, f"{name!r} keeps its date")
        c.equal(t.client_raw or "", "", "and reports no client rather than a digit")

    # The half that would break if the fix were a blunter one.
    for name, client, date_want in (
            ("PROD: Edge AI Solutions - 29Jun26 - Trade show TOF",
             "Edge AI Solutions", "2026-06-29"),
            ("PROD: 3M Company - 08Sep26 - x", "3M Company", "2026-09-08"),
            ("OPS: 4 Rivers - 1Jan26 - x", "4 Rivers", "2026-01-01")):
        t = ex.parse_title(name)
        c.equal(t.client_raw, client, f"{client!r} still parses as the client")
        c.equal(str(t.date), date_want, "with the right date")

    return c.report()


def check_changing_one_field_changes_one_field() -> bool:
    """A title is spliced, never rebuilt.

    The editor used to compose the whole string from the parsed parts, so
    picking a client normalised everything it passed on the way: `04aug26`
    came back `04Aug26`, `Thrasher-07Aug26-` gained its spaces, an ISO date
    became `13Jul26`. Measured across the mirrors, **513 of production's
    1,164 titles** differ from their own recomposition -- and each rewrite is
    a real thread rename, two per ten minutes on a budget shared between both
    machines, posting a system message into a customer thread, for a field
    nobody edited.

    So `replace_field` swaps the bytes of one named group and copies the rest
    through untouched. The strongest form of that is the check below: putting
    a field back to the value it already has may not alter the title at all.
    """
    c = Check("changing one field changes one field")

    # The awkward shapes, each with something the parser would have tidied.
    for name in ("OPS: Trekk - 04aug26 - SSD0129 motor short",
                 "OPS: Thrasher-07Aug26- EReel-1023 Unknown Issue",
                 "PROD: MBE - 2026-7-13 - System Sub",
                 "PROD: Rockton - 8Jun26 -  3-Month Sub"):
        t = ex.parse_title(name)
        c.equal(ex.replace_field(name, "client", t.client_raw), name,
                f"the same client leaves it alone: {name!r}")
        out = ex.replace_field(name, "client", "Bill Patterson Construction")
        c.ok("Bill Patterson Construction" in out, "a new client goes in")
        # Everything that was not the client is still exactly as it was.
        c.equal(out.replace("Bill Patterson Construction", t.client_raw), name,
                "and nothing else moved")

    # A client segment that matched nothing still needs its separator, or the
    # name butts against the date.
    c.equal(ex.replace_field("PROD: 29Jun26 - Trade show TOF", "client", "Acme"),
            "PROD: Acme - 29Jun26 - Trade show TOF",
            "an empty client segment gains a separator with the name")

    # Emptying a field takes its separator with it. Found by clearing the
    # Client box in a driven editor: the title kept the punctuation for
    # something no longer there, `OPS:  - 04aug26 - x`, and that is what
    # would have been saved as the thread's name.
    n = "OPS: Trekk - 04aug26 - SSD0129 motor short"
    c.equal(ex.replace_field(n, "client", ""),
            "OPS: 04aug26 - SSD0129 motor short", "a cleared client leaves no gap")
    c.equal(ex.replace_field(n, "summary", ""),
            "OPS: Trekk - 04aug26", "and a cleared last field leaves no trailing dash")
    c.equal(ex.replace_field(ex.replace_field(n, "client", ""), "client", "Trekk"),
            n, "and putting it back gives exactly what was there")
    for f in ("client", "summary"):
        out = ex.replace_field(n, f, "")
        c.ok(ex.parse_title(out).confidence in ("strict", "loose"),
             f"what is left still parses after clearing {f}")

    # Taking the tag off, which the dropdown's dash offers and so must do.
    c.equal(ex.replace_field(n, "queue", ""),
            "Trekk - 04aug26 - SSD0129 motor short", "the tag can be removed")
    # Putting it back is not this function's job and cannot be: once the tag
    # is gone the title has no queue group for a swap to find, which is
    # exactly why the editor prepends rather than splices in that case.
    c.equal(ex.replace_field("Trekk - 04aug26 - x", "queue", "OPS"),
            "Trekk - 04aug26 - x", "a tagless title has no tag to swap")

    # A title with no segments to swap is returned as it stands, which is
    # what lets the editor say so instead of guessing.
    c.equal(ex.replace_field("ENG: Retired bots", "client", "Acme"),
            "ENG: Retired bots", "a title with no client slot is untouched")
    c.equal(ex.replace_field("", "client", "Acme"), "", "and so is an empty one")

    # The date guard parse_title uses, so an impossible date is not a segment.
    c.equal(ex.replace_field("PROD: 32Jun26 - bad day", "client", "Acme"),
            "PROD: 32Jun26 - bad day", "a date that will not parse is not a match")
    return c.report()


def check_a_rebuilt_title_converges() -> bool:
    """Guided rebuilds an unreadable title instead of handing back a box.

    Composing is only dangerous for a title that already parses -- that is
    where the 513 gratuitous renames live -- so the rule is splice when it
    parses, compose when it does not. A title the fields cannot express has
    no bytes worth preserving, which is what makes building a replacement
    free.

    What has to hold is that the replacement converges: fill the four fields
    and the result parses, so the tick can actually be earned. Otherwise
    guided would be a mode you cannot finish.
    """
    import bert
    from datetime import date as _date
    c = Check("a rebuilt title converges")

    for was in ("ENG: Retired bots",
                "OPS: SCI, Bravo, Inspect.AI inventory outreach",
                "Thrasher - Trade show TOF",
                ""):
        t0 = ex.parse_title(was)
        c.ok(t0.confidence not in ("strict", "loose"),
             f"the fixture really is unreadable: {was!r}")
        # What the editor seeds the description with.
        seed = t0.summary or ("" if t0.confidence in ("strict", "loose") else was)
        built = bert.compose_title("PROD", "Acme Digging",
                                   _date(2026, 9, 16), seed)
        c.ok(ex.parse_title(built).confidence in ("strict", "loose"),
             f"and the rebuild parses: {built!r}")
        if seed:
            c.ok(seed in built, "keeping what somebody had already written")

    # Half-filled on the way there is allowed to be unreadable -- it just
    # must not throw anything away.
    part = bert.compose_title("PROD", "", None, "Retired bots")
    c.equal(part, "PROD: Retired bots", "a half-built title keeps the words")
    return c.report()


CHECKS = (check_a_rename_becomes_the_revision_it_was,
          check_it_cannot_change_what_the_board_shows,
          check_a_familiar_name_still_gets_its_own_date,
          check_only_renames_count,
          check_running_it_twice_changes_nothing,
          check_a_thread_with_no_title_row_is_left_alone,
          check_a_title_with_no_client_keeps_its_whole_date,
          check_changing_one_field_changes_one_field,
          check_a_rebuilt_title_converges)
