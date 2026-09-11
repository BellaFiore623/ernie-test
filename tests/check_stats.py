"""
The board over time, which the board itself cannot say.

Each figure earns its place by answering something no other view does:
whether closing is keeping up, which tickets have been open since April, and
how long one takes end to end. There was a fourth -- open tickets with no
build or return raised against them -- and it was dropped after Julian read
the panel, which is that standard being applied rather than an exception to
it.

Two rules shape what is in here. Nothing is derived from `events`, because
production's is empty and every completion there reads as "imported" -- a
statistic that lies once is never trusted again. And the middle is quoted
rather than the mean: measured against production, the average time to close
is 13.1 days and the median is 7.9, because a handful of very old tickets drag
the average a long way from anything typical.
"""

import ast
import pathlib

from support import Board, Check, iso

import bert
import ernie_api as api
import ernie_extract as ex


ROOT = pathlib.Path(__file__).resolve().parent.parent


def opened(b, name, days_ago, priority="medium", done_days_ago=None):
    """A card whose thread opened a given number of days ago."""
    tid = b.card(name, priority)
    b.con.execute("UPDATE threads SET created_at=?, first_seen_at=? "
                  "WHERE thread_id=?",
                  (iso(-days_ago * 86400), iso(-days_ago * 86400), tid))
    if done_days_ago is not None:
        b.con.execute("UPDATE cards SET completed_at=?, completed_by=? "
                      "WHERE thread_id=?",
                      (iso(-done_days_ago * 86400), "imported", tid))
    b.con.commit()
    return tid


def check_the_figures_are_what_they_claim() -> bool:
    """Each number, against a board built to have a known answer."""
    c = Check("the figures are what they claim")

    with Board() as b:
        # Three closed, two of them in the same month, one much older.
        opened(b, "PROD: A - 01Jan26 - one", 40, done_days_ago=30)
        opened(b, "PROD: B - 01Jan26 - two", 20, done_days_ago=18)
        opened(b, "PROD: C - 01Jan26 - three", 12, done_days_ago=2)
        # Two still open, one of them old.
        old = opened(b, "PROD: D - 01Jan26 - four", 150)
        opened(b, "PROD: E - 01Jan26 - five", 3)
        api.DB = b.path
        s = api.stats(days=365)

        done = s["completed"]
        c.equal(sum(b["count"] for b in done["periods"]), 3,
                "every closure inside the window is counted")
        c.equal(done["bucket"], "month",
                "a year-long window is bucketed by month")
        c.ok(done["periods"] == sorted(done["periods"],
                                       key=lambda b: b["start"]),
             "oldest first, so it reads as a trend rather than a list")

        ages = s["ageing"]
        c.equal(ages[0]["thread_id"], old, "the longest open comes first")
        c.ok(149 <= ages[0]["days"] <= 150,
             f"with how long it has been ({ages[0]['days']}d) -- the cast "
             f"truncates, so the day it was opened counts as a boundary")
        c.ok(all(a["thread_id"] != ages[0]["thread_id"] or i == 0
                 for i, a in enumerate(ages)), "and each appears once")
        c.equal(len(ages), 2, "closed ones are not in it")

        took = s["time_to_complete"]
        c.equal(took["count"], 3, "the spans are the closed ones")
        c.ok(took["median_days"] is not None, "with a middle")
        c.ok(took["slowest_days"] >= took["median_days"],
             "and the slowest, which is where the story usually is")

    return c.report()


def check_the_middle_not_the_mean() -> bool:
    """
    One very old ticket must not move the headline figure.

    Measured against production: mean 13.1 days, median 7.9. The mean is the
    one that reads as "this is how long a job takes", and it is the one that
    is wrong.
    """
    c = Check("the headline is the middle, not the mean")

    with Board() as b:
        for i in range(9):
            opened(b, f"PROD: quick {i} - 01Jan26 - x", 3, done_days_ago=1)
        opened(b, "PROD: the old one - 01Jan26 - x", 400, done_days_ago=1)
        api.DB = b.path
        took = api.stats()["time_to_complete"]

        c.ok(took["median_days"] < 5,
             f"the middle stays with the nine quick ones "
             f"({took['median_days']} days)")
        c.ok(took["average_days"] > took["median_days"] * 3,
             f"while the mean is dragged away by the one outlier "
             f"({took['average_days']} days)")
        c.ok(took["slowest_days"] > 300, "and the outlier is reported, not hidden")

        # And the panel has to *show* the middle. The API offering both is no
        # use if the headline quotes the one that is wrong.
        src = (ROOT / "bert.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        stats = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                     and n.name == "Stats")
        draw = next(n for n in stats.body if isinstance(n, ast.FunctionDef)
                    and n.name == "set_stats")
        body = ast.get_source_segment(src, draw) or ""
        headline = body.split("days, typically")[0].splitlines()[-1]
        c.ok("median_days" in headline,
             "the headline figure in the panel is the median")
        c.ok("average_days" not in headline,
             "and not the mean, which one old ticket drags away")
        c.ok("average" in body,
             "the mean is still there, in the tooltip, for anyone who wants it")

    return c.report()


def check_it_says_nothing_rather_than_something_wrong() -> bool:
    """A board with no history has no trend, and must not invent one."""
    c = Check("an empty board says nothing rather than something wrong")

    with Board() as b:
        api.DB = b.path
        s = api.stats()
        # An empty board still gets its buckets -- they are built forward
        # from the start of the window rather than off the rows, so the
        # answer is "nothing closed" rather than "no data", which are not the
        # same news.
        c.ok(all(b["count"] == 0 for b in s["completed"]["periods"]),
             "every bucket is empty")
        c.ok(s["completed"]["periods"], "but the buckets are still there")
        c.equal(s["ageing"], [], "nothing ageing")
        c.equal(s["time_to_complete"], None,
                "and no time to close, rather than a zero that reads as instant")

    return c.report()


def check_the_month_label_reads_in_a_narrow_column() -> bool:
    """The panel is 244px, so the year is spelled only where it turns over.

    And the bar's label follows the bucket, which follows the window: a day,
    a rolling week, or a month, each written to fit that column.
    """
    c = Check("the month label reads in a narrow column")

    c.equal(bert.period_name("2026-09-04", "day"), "4 Sep", "a day is a date")
    c.equal(bert.period_name("2026-08-21", "week"), "w/c 21 Aug",
            "a week says which week it commences")
    c.equal(bert.period_name("2026-08-01", "month"), "Aug",
            "and a month is just the month")
    c.equal(bert.period_name("2026-01-01", "month"), "Jan 26",
            "carrying the year where it turns over, as it always did")
    for junk in ("", "nonsense", None, "2026-13-01"):
        got = bert.period_name(junk, "day")
        c.ok(isinstance(got, str), f"{junk!r} gives a string rather than raising")

    c.equal(bert.month_name("2026-08"), "Aug", "an ordinary month is three letters")
    c.equal(bert.month_name("2026-01"), "Jan 26",
            "January carries the year, or two of them sit side by side")
    # Never raises, and never invents a month it was not given. An empty
    # string back from an empty string is the right answer, not a gap.
    for junk in ("", "nonsense", None, "2026-13"):
        got = bert.month_name(junk)
        c.ok(isinstance(got, str), f"{junk!r} gives a string rather than raising")
        c.ok(got == str(junk) if junk is not None else got == "None",
             f"{junk!r} is handed back rather than guessed at")

    return c.report()


def check_the_figures_ride_the_slow_lane() -> bool:
    """
    They move when a ticket closes, not every five seconds.

    The board's poll is what the window depends on; hanging four aggregates
    off it would make every one of them a chance to fail.
    """
    c = Check("the figures ride the slow lane, not the board's poll")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    poller = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                   and n.name == "Poller"), None)
    c.ok(poller is not None, "the poller is there")
    if poller:
        run = next((n for n in poller.body if isinstance(n, ast.FunctionDef)
                    and n.name == "run"), None)
        body = ast.get_source_segment(src, run) or "" if run else ""
        c.ok("want_stats" in body, "and asks for the figures only when told to")
        c.ok("except Exception" in body.split("want_stats")[-1],
             "an Ernie too old to serve them does not fail the poll")

    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    refresh = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                   and n.name == "refresh")
    r = ast.get_source_segment(src, refresh) or ""
    c.ok("STATS_MAX_AGE_S" in r, "on its own cadence, like the roster")

    return c.report()


def check_it_folds_the_way_the_rail_folds() -> bool:
    """
    The same idiom, or the two panels behave differently for no reason.

    A range rather than a fixed width, so the handle has something to move;
    a fixed width only while folded, because folding is the button's business;
    and the remembered width comes back on unfold rather than whatever the
    fold left.
    """
    c = Check("the figures fold the way the running order folds")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    stats = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                  and n.name == "Stats"), None)
    c.ok(stats is not None, "there is a Stats panel")
    if stats is None:
        return c.report()

    fold = next((n for n in stats.body if isinstance(n, ast.FunctionDef)
                 and n.name == "set_folded"), None)
    body = ast.get_source_segment(src, fold) or "" if fold else ""
    c.ok("RAIL_FOLDED_W" in body, "folded, it comes down to the spine")
    # A range even when folded, and not a fixed width. Pinned, the pane could
    # not be moved at all -- and the handle still sits against the spine, so
    # dragging it did nothing and there was no way to see why. The button
    # folds; the handle opens.
    c.ok("setFixedWidth" not in body,
         "but is never pinned, or the handle beside it does nothing")
    c.ok("setMinimumWidth" in body and "setMaximumWidth" in body,
         "unfolded, it is handed back to the splitter as a range")
    c.ok("_stats_sized" in body,
         "and the remembered width is re-applied rather than the fold's")
    c.ok("settle" in body,
         "unless the handle did it, which is already the width somebody "
         "chose and must not be snapped away mid-drag")

    # The teardown rule the whole application follows.
    clear = next((n for n in stats.body if isinstance(n, ast.FunctionDef)
                  and n.name == "clear"), None)
    cbody = ast.get_source_segment(src, clear) or "" if clear else ""
    c.ok(cbody.index("hide()") < cbody.index("setParent(None)"),
         "and a row is hidden before it is unparented, so it is never a window")

    return c.report()



def tallied(b, name, queue, created_days_ago, closed_days_ago=None):
    """A card with a tag, an age, and possibly an end."""
    tid = b.card(name, "medium")
    b.con.execute("UPDATE threads SET created_at=?, first_seen_at=? "
                  "WHERE thread_id=?",
                  (iso(-created_days_ago * 86400),
                   iso(-created_days_ago * 86400), tid))
    # The title row is dated with the thread, not left at the fixture's
    # "an hour ago". A title observed after every later revision is not a
    # history, and the retag figure reads the order of these rows.
    b.con.execute("UPDATE thread_titles SET queue=?, observed_at=? "
                  "WHERE thread_id=?",
                  (queue, iso(-created_days_ago * 86400), tid))
    if closed_days_ago is not None:
        b.con.execute("UPDATE cards SET completed_at=?, completed_by=? "
                      "WHERE thread_id=?",
                      (iso(-closed_days_ago * 86400), "imported", tid))
    b.con.commit()
    return tid


def check_open_is_a_level_and_the_other_two_are_flows() -> bool:
    """
    The window moves created and closed. It must not move open.

    Open is the backlog *now* -- what is on the plate. Windowing it would
    answer "opened inside the window and still open", which is a different
    and much less useful question: the work somebody is carrying does not
    start at the beginning of whatever window they picked. The panel labels
    the column `open` against `new` and `done` for that reason, and this is
    the assertion that keeps the two apart.
    """
    c = Check("open is a level; created and closed are flows")

    with Board() as b:
        tallied(b, "PROD: A - 01Jan26 - x", "PROD", 400)          # old, open
        tallied(b, "PROD: B - 01Jan26 - x", "PROD", 3)            # new, open
        tallied(b, "OPS: C - 01Jan26 - x", "OPS", 300, 200)       # long closed
        tallied(b, "OPS: D - 01Jan26 - x", "OPS", 10, 2)          # just closed
        api.DB = b.path

        week = api.stats(days=7)["tally"]
        year = api.stats(days=365)["tally"]

        c.equal(week["totals"]["open"], 2, "two are open")
        c.equal(year["totals"]["open"], week["totals"]["open"],
                "and the same two are open at every window, because open is "
                "not a thing that happened inside one")

        c.equal(week["totals"]["created"], 1, "one thread opened this week")
        # Three, not four: the 400-day-old one is outside a year, which is
        # the window doing its job at the far end as well as the near one.
        c.equal(year["totals"]["created"], 3, "and three inside the year")
        c.equal(week["totals"]["closed"], 1, "one ticket closed this week")
        c.equal(year["totals"]["closed"], 2, "and two inside the year")

        c.ok(year["totals"]["created"] > week["totals"]["created"],
             "a wider window can only find more of a flow")

    return c.report()


def check_the_rows_add_up() -> bool:
    """
    Per tag, and the tags have to sum to the total.

    Which is the whole reason `Other` exists. A card whose tag is retired --
    or that has none at all -- still counts towards the board, and dropping
    it would leave a table whose rows do not make its own total. A figure
    that does not add up is the first one somebody stops believing.
    """
    c = Check("the per-tag rows add up to the total")

    with Board() as b:
        tallied(b, "PROD: A - 01Jan26 - x", "PROD", 5)
        tallied(b, "OPS: B - 01Jan26 - x", "OPS", 5, 1)
        # A queue that parses and is never offered, and one with no tag at
        # all -- the two ways a card arrives without a column of its own.
        tallied(b, "DATA: C - 01Jan26 - x", "DATA", 5)
        tallied(b, "D - 01Jan26 - x", None, 5)
        api.DB = b.path
        t = api.stats(days=30)["tally"]

        for key in ("open", "created", "closed"):
            rows = sum(t[key][q] for q in t["queues"])
            c.equal(rows, t["totals"][key],
                    f"the {key} column sums to its total ({rows})")

        c.ok("Other" in t["queues"], "a retired or missing tag has a home")
        c.equal(t["open"]["Other"], 2,
                "and both of them are in it rather than dropped")
        c.ok("DATA" not in t["queues"],
             "a retired queue gets no column of its own, or one retired years "
             "ago carries a row of noughts for ever")

    return c.report()


def check_every_offered_tag_keeps_its_row() -> bool:
    """A table whose rows appear and vanish is one nobody can scan."""
    c = Check("every offered tag keeps its row, even at nought")

    with Board() as b:
        tallied(b, "PROD: only one - 01Jan26 - x", "PROD", 2)
        api.DB = b.path
        t = api.stats(days=30)["tally"]

        for q in ex.QUEUES_OFFERED:
            c.ok(q in t["queues"], f"{q} has a row with nothing in it")
            for key in ("open", "created", "closed"):
                c.ok(t[key].get(q) is not None,
                     f"{q} has a {key} number rather than a gap")
        c.ok("Other" not in t["queues"],
             "but Other stays away until it has something in it")

    return c.report()


def check_the_window_compares_dates_the_only_way_that_works() -> bool:
    """
    `datetime()` on both sides, which is a hard rule here.

    Python writes ISO8601 with a `T` and SQLite's `datetime('now')` uses a
    space, so the two cannot be compared as strings. The failure is not the
    obvious one and it took an attempt to find: for dates a day or more apart the
    raw compare happens to give the right answer, because the digits differ
    before the separator is reached. It only goes wrong **on the boundary
    day itself** -- and there it always goes the same way, because `T` (0x54)
    sorts after a space (0x20). So a thread opened earlier in the day than
    the cutoff compares as *later* and is counted in a window it falls
    outside.

    Quietly, and always in the same direction: every figure reads slightly
    high, by up to a day's worth of work at the far edge. Nothing looks
    broken. This is the case that catches it.
    """
    c = Check("the window compares dates the only way that works")

    with Board() as b:
        stamp = b.con.execute(
            "SELECT created_at FROM threads LIMIT 1").fetchone()
        tid = tallied(b, "PROD: inside - 01Jan26 - x", "PROD", 1, 1)
        held = b.con.execute("SELECT created_at FROM threads WHERE thread_id=?",
                             (tid,)).fetchone()[0]
        c.ok("T" in held,
             f"the fixture stores a T, as the mirror does ({held[:19]})")

        # Four hours the wrong side of a seven-day cutoff -- so on the same
        # calendar date as the boundary, which is the only place this breaks.
        tallied(b, "PROD: just outside - 01Jan26 - x", "PROD", 7 + 4 / 24,
                7 + 4 / 24)

        api.DB = b.path
        t = api.stats(days=7)["tally"]
        c.equal(t["totals"]["created"], 1,
                "a thread opened four hours before a seven-day cutoff is "
                "outside it -- compared as strings it reads as inside, "
                "because T sorts after a space")
        c.equal(t["totals"]["closed"], 1,
                "and the same for a ticket closed four hours before it")

        # And the ordinary case still works, so the fix has not gone the
        # other way and started excluding things that are inside.
        year = api.stats(days=365)["tally"]
        c.equal(year["totals"]["created"], 2, "both are inside a year")

    return c.report()


def check_the_table_reads_in_the_toolbar_s_order() -> bool:
    """
    PROD OPS ENG CS, which is the order already read once across the window.

    The filter checkboxes are built by walking `T.QUEUE`, so that is the
    order somebody has in their head by the time they look at this panel.
    `QUEUES_OFFERED` is the parser's order and is not the same one.
    """
    c = Check("the table reads in the toolbar's order")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    stats = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.ClassDef) and n.name == "Stats")
    fn = next((n for n in stats.body if isinstance(n, ast.FunctionDef)
               and n.name == "_tally"), None)
    c.ok(fn is not None, "the panel draws the table in one place")
    body = ast.get_source_segment(src, fn) or "" if fn else ""
    c.ok("for q in T.QUEUE" in body,
         "ordered by the palette, which is what the filters walk")

    # The selector is built once and never rebuilt: set_stats tears the body
    # down on every change, and a combo rebuilt under somebody's pointer
    # loses its popup mid-choice.
    init = ast.get_source_segment(src, next(
        n for n in stats.body if isinstance(n, ast.FunctionDef)
        and n.name == "__init__")) or ""
    c.ok("self.window_box" in init, "the window selector is built once")
    draw = ast.get_source_segment(src, next(
        n for n in stats.body if isinstance(n, ast.FunctionDef)
        and n.name == "set_stats")) or ""
    c.ok("window_box" not in draw,
         "and never touched by the redraw, which throws the body away")

    # The chain that makes the dropdown do anything: it has to clear the
    # stamp, ask again, and the ask has to carry the panel's own window. Miss
    # any one and the box changes and the numbers do not -- which is how this
    # was reported.
    changed = ast.get_source_segment(src, next(
        n for n in stats.body if isinstance(n, ast.FunctionDef)
        and n.name == "_window_changed")) or ""
    c.ok("stats_at = 0" in changed,
         "changing the window drops the freshness stamp")
    c.ok("refresh()" in changed, "and asks again straight away")
    c.ok("save_settings" in changed, "and remembers the choice")

    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    ref = ast.get_source_segment(src, next(
        n for n in cls.body if isinstance(n, ast.FunctionDef)
        and n.name == "refresh")) or ""
    c.ok("stats_days=self.stats_panel.days()" in ref,
         "and the poll carries the panel's window rather than a default")

    # Every window the panel offers has to be one the API will take.
    c.ok(all(isinstance(d, int) and d > 0 for _, d in bert.STATS_WINDOWS),
         "every window is a positive number of days")
    lens = [d for _, d in bert.STATS_WINDOWS]
    c.equal(lens, sorted(lens), "and they are offered shortest first")
    c.ok(bert.STATS_WINDOW_DEFAULT in lens,
         "the default is one of the options, or the box opens on nothing")

    return c.report()



def check_the_window_moves_every_block_it_should() -> bool:
    """
    The selector has to change what is on the panel, or nobody believes it.

    Reported as "I change the timeframe and nothing changes" -- and that was
    fair: the tally followed the window from the start, but Completed was
    hard-wired to six months and it is the biggest block on the panel. A
    control that visibly does nothing to the thing under it reads as broken
    whatever else it is quietly doing.
    """
    c = Check("the window moves every block it should")

    with Board() as b:
        tallied(b, "PROD: old - 01Jan26 - x", "PROD", 300, 250)
        tallied(b, "PROD: recent - 01Jan26 - x", "PROD", 5, 2)
        api.DB = b.path

        week = api.stats(days=7)
        year = api.stats(days=365)

        c.ok(week["completed"] != year["completed"],
             "Completed is a different block at a different window")
        c.equal(sum(x["count"] for x in week["completed"]["periods"]), 1,
                "one closure inside the week")
        c.equal(sum(x["count"] for x in year["completed"]["periods"]), 2,
                "and both inside the year")
        c.ok(week["tally"]["totals"] != year["tally"]["totals"],
             "and the tally moves with it")

        # The bucket has to keep the bar count somewhere the eye can read.
        # Seven days as one bar is not a trend, and a year as 365 is not a
        # panel 244px wide.
        for days in [d for _, d in bert.STATS_WINDOWS]:
            bars = api.stats(days=days)["completed"]["periods"]
            c.ok(3 <= len(bars) <= 14,
                 f"{days}d draws {len(bars)} bars, which is a shape rather "
                 f"than a wall -- three is the floor because two bars is not "
                 f"a trend, and fourteen the ceiling because the panel is "
                 f"244px wide and every bar is a row")

        # A month bucket is a whole month, or the earliest bar is a part
        # month standing beside whole ones and reads as a quiet month.
        for days in (91, 182, 365):
            first = api.stats(days=days)["completed"]["periods"][0]["start"]
            c.ok(first.endswith("-01"),
                 f"{days}d starts on the first of a month ({first})")

    return c.report()


def retagged(b, tid, queue, days_ago, name=None):
    """Another title revision on a thread that already has one.

    `thread_titles` is append-only, which is the whole reason this figure
    needs no new storage: the rows already sitting there *are* the history.
    """
    row = b.con.execute(
        "SELECT name FROM thread_titles WHERE thread_id=? "
        "ORDER BY observed_at DESC LIMIT 1", (tid,)).fetchone()
    name = name or (queue + ": " + str(row["name"]).split(": ", 1)[-1])
    b.con.execute(
        """INSERT INTO thread_titles (thread_id, observed_at, name, queue,
                                      confidence)
           VALUES (?,?,?,?,?)""",
        (tid, iso(-days_ago * 86400), name, queue, "strict"))
    b.con.commit()


def check_a_retag_is_already_in_the_mirror() -> bool:
    """
    How many tickets went from PROD to OPS, off `thread_titles` alone.

    Asked for by Julian. The two routes offered were a tag history written
    into `#ernie-state` -- which could only start counting from the day it
    was switched on -- and a trawl of another bot's log channel. Neither is
    needed: the tag *is* the title's prefix, the table is append-only, and
    every revision already carries the queue the parser read off it, so a tag
    change is a row with a time on it.

    What this defends is that the reading is a *transition* rather than a
    count of revisions: a rename that leaves the tag alone is not a move, and
    a ticket retagged twice moved twice.
    """
    c = Check("a retag is already in the mirror")

    with Board() as b:
        one = tallied(b, "PROD: A - 01Jan26 - x", "PROD", 30)
        retagged(b, one, "OPS", 20)
        # Twice: PROD -> OPS -> PROD is two moves, not one and not three.
        # Both legs are counted pairs, which is the point -- a ticket that
        # went out and came back is two facts about it, not one.
        two = tallied(b, "PROD: B - 01Jan26 - x", "PROD", 30)
        retagged(b, two, "OPS", 25)
        retagged(b, two, "PROD", 10)
        # A rename that keeps the tag. People retitle threads constantly --
        # the client is corrected, the summary is sharpened -- and counting
        # those would make the figure meaningless.
        three = tallied(b, "PROD: C - 01Jan26 - x", "PROD", 30)
        retagged(b, three, "PROD", 15,
                 name="PROD: C - 01Jan26 - a better summary")
        # And one that never moved at all.
        tallied(b, "OPS: D - 01Jan26 - x", "OPS", 30)

        api.DB = b.path
        moves = api.stats(days=365)["tag_moves"]
        counts = {(m["from"], m["to"]): m["count"] for m in moves["moves"]}

        c.equal(counts.get(("PROD", "OPS")), 2,
                "two tickets went PROD -> OPS")
        c.equal(counts.get(("OPS", "PROD")), 1,
                "and one came back, counted as its own move")
        c.ok(("PROD", "PROD") not in counts,
             "a rename that keeps the tag is not a move")
        c.equal(moves["total"], 3, "three moves in all")
        c.equal(sum(m["count"] for m in moves["moves"]), moves["total"],
                "and the total is the rows added up, so the block adds up")

    return c.report()


def check_only_the_pairs_worth_counting_are_counted() -> bool:
    """
    Four tags make twelve possible pairs and the question was about two.

    All twelve were reported, so `PROD -> OPS` and `OPS -> PROD` -- the
    answer to what was actually asked -- shared a 244px panel with
    `ENG -> PROD` and `CS -> OPS`, a handful of rows each about something
    nobody wanted to know. Measured on the sandbox: eight pairs, of which two
    carried the question and six were noise, and the noise was enough to push
    a real row past `STATS_MOVES_SHOWN` and into the summed tail.

    **Filtered in the query, not in the drawing**, which is the half worth a
    check. Counting everything and showing two would leave a block whose rows
    do not make its own total, and a figure that does not add up is the first
    one somebody stops believing -- the rule `Other` exists for in the tally.
    There is no `Other` to write here, because what nobody asked about is not
    counted at all.

    Nothing is deleted by this. `thread_titles` is append-only and still
    holds every rename, so widening `TAG_MOVES_COUNTED` again is one line and
    no backfill.
    """
    c = Check("only the pairs worth counting are counted")

    c.equal(tuple(api.TAG_MOVES_COUNTED), (("PROD", "OPS"), ("OPS", "PROD")),
            "the counted pairs are the two that were asked about")

    with Board() as b:
        # One of each, so every row that comes back is a decision rather than
        # an absence of data.
        for i, (was, became) in enumerate(
                (("PROD", "OPS"), ("OPS", "PROD"),
                 ("ENG", "PROD"), ("PROD", "ENG"), ("CS", "OPS"))):
            t = tallied(b, f"{was}: C{i} - 01Jan26 - x", was, 30)
            retagged(b, t, became, 20)

        api.DB = b.path
        moves = api.stats(days=365)["tag_moves"]
        pairs = {(m["from"], m["to"]) for m in moves["moves"]}

        c.equal(pairs, {("PROD", "OPS"), ("OPS", "PROD")},
                "only those two come back, though all five happened")
        for gone in (("ENG", "PROD"), ("PROD", "ENG"), ("CS", "OPS")):
            c.ok(gone not in pairs, f"{gone[0]} -> {gone[1]} is not counted")
        c.equal(moves["total"], 2,
                "and the total counts what is shown, not what was dropped")
        c.equal(sum(m["count"] for m in moves["moves"]), moves["total"],
                "so the rows still make their own total")

    return c.report()


def check_the_window_moves_the_retags_too() -> bool:
    """
    The selector moves this block like every other one.

    The same complaint that produced `check_the_window_moves_every_block_it_
    should`: a timeframe control that leaves a block sitting still reads as
    broken. A move is dated by *when the retag was observed*, which is the
    only date it has -- not when the thread opened, and not when it closed.
    """
    c = Check("the window moves the retags too")

    with Board() as b:
        old = tallied(b, "PROD: A - 01Jan26 - x", "PROD", 300)
        retagged(b, old, "OPS", 250)
        new = tallied(b, "PROD: B - 01Jan26 - x", "PROD", 300)
        retagged(b, new, "OPS", 2)        # an old ticket, retagged this week

        api.DB = b.path
        c.equal(api.stats(days=7)["tag_moves"]["total"], 1,
                "one retag inside the week, though both threads are ancient")
        c.equal(api.stats(days=365)["tag_moves"]["total"], 2,
                "and both inside the year")
        c.equal(api.stats(days=7)["tag_moves"]["days"], 7,
                "the block says which window it is answering")

        # The trap the outbox already fell into once: Python writes ISO8601
        # with a T and SQLite's datetime('now') uses a space, so a raw string
        # compare is wrong on the boundary day -- T sorts after space, so a
        # retag earlier in the day than the cutoff reads as later.
        src = (ROOT / "ernie_api.py").read_text(encoding="utf-8")
        block = src.split("tag_moves", 1)[0].rsplit("moves = [", 1)[-1]
        c.ok("datetime(observed_at)" in block
             and "datetime('now', :since)" in block,
             "and both sides of the comparison go through datetime()")

    return c.report()


def check_only_the_board_s_own_threads_count() -> bool:
    """
    `#customer-support` is mirrored for history, not for tickets.

    Its threads carry `generate_cards = 0` and never become cards, so a
    retitle there is not a ticket changing hands. The panel is about the
    board, so the figure joins `cards`.
    """
    c = Check("only the board's own threads count")

    with Board() as b:
        tid = "support-001"
        b.con.execute(
            """INSERT INTO threads (thread_id, parent_id, guild_id, created_at,
                                    first_seen_at, last_synced_at)
               VALUES (?,?,?,?,?,?)""",
            (tid, "support-channel", "guild", iso(-86400 * 30),
             iso(-86400 * 30), iso()))
        for when, queue in ((-86400 * 30, "PROD"), (-86400 * 10, "OPS")):
            b.con.execute(
                """INSERT INTO thread_titles (thread_id, observed_at, name,
                                              queue, confidence)
                   VALUES (?,?,?,?,?)""",
                (tid, iso(when), queue + ": S - 01Jan26 - x", queue,
                 "strict"))
        b.con.commit()

        api.DB = b.path
        c.equal(api.stats(days=365)["tag_moves"]["total"], 0,
                "a thread with no card contributes no move")

        # Put a card under it and the same rows do count, which is what says
        # the join is the reason rather than something else about the rows.
        b.con.execute(
            """INSERT INTO cards (thread_id, priority, rank, updated_at)
               VALUES (?,?,?,?)""", (tid, "medium", 1000.0, iso()))
        b.con.commit()
        c.equal(api.stats(days=365)["tag_moves"]["total"], 1,
                "and the same two rows do count once it is a ticket")

    return c.report()


def check_the_block_shows_a_tail_rather_than_dropping_it() -> bool:
    """
    Four tags make twelve possible pairs, and the panel is 244px wide.

    So the block draws the common ones and sums the rest into a line. Summed
    rather than dropped: the rows have to add up to the total under them, and
    a figure that does not add up is the first one somebody stops believing.
    """
    c = Check("the block shows a tail rather than dropping it")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_moves"), None)
    c.ok(fn is not None, "the panel has a block for it")
    body = (ast.get_source_segment(src, fn) or "") if fn else ""

    c.ok("STATS_MOVES_SHOWN" in body, "it draws a bounded number of rows")
    c.ok("rest" in body and "sum(" in body,
         "and the ones past that are summed rather than dropped")
    c.ok("tip=" in body,
         "with the tail named in a tooltip, so nothing is actually hidden")
    c.ok(bert.STATS_MOVES_SHOWN < 12,
         "and the bound is under the twelve pairs four tags can make")

    # Drawn where the tally is drawn: both are flows between the same tags
    # over the same window, and the tally cannot show a ticket that arrived
    # as one tag and left as another.
    draw = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "set_stats"),
                None)
    shown = (ast.get_source_segment(src, draw) or "") if draw else ""
    c.ok("tag_moves" in shown, "and set_stats reads the block")
    c.ok(shown.index("tag_moves") > shown.index('data.get("tally")'),
         "straight after the tally, which is the block beside it")

    return c.report()


def check_the_figures_keep_clear_of_their_scrollbar() -> bool:
    """
    Every number on this panel is right-aligned, so they all end at one edge.

    The counts in the tally, the number on a bar, the total, the age on an
    ageing row. With no margin on the body that edge is exactly where the
    scrollbar starts -- measured, one pixel between the two -- and it was
    reported as the numbers running into it.

    **The gutter goes on the body, not on the panel.** The scroll area is
    what the bar belongs to, so padding outside it moves the bar along with
    the content and leaves the gap exactly where it was. `FEED_GUTTER` is the
    same rule on the activity feed, which has had one since it started
    scrolling; this panel scrolls now too and did not.
    """
    c = Check("the figures keep clear of their scrollbar")

    src = (ROOT / "bert.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    c.ok(getattr(bert, "STATS_GUTTER", 0) > 0,
         f"there is a gutter at all ({getattr(bert, 'STATS_GUTTER', 0)}px)")

    stats = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.ClassDef) and n.name == "Stats"), None)
    init = next((n for n in ast.walk(stats)
                 if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
                None) if stats else None
    body = (ast.get_source_segment(src, init) or "") if init else ""
    c.ok("self.body.setContentsMargins(0, 0, STATS_GUTTER, 0)" in body,
         "and it is the body's right margin, so the bar stays where it is")

    # The room a client name is cut to has to lose the same width, or the
    # longest ones are elided to a width that no longer exists and sit under
    # the bar regardless.
    draw = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "set_stats"),
                None)
    drawn = (ast.get_source_segment(src, draw) or "") if draw else ""
    c.ok("STATS_ROW_CHROME - STATS_GUTTER" in drawn,
         "and it comes out of the room an elided client name is given")

    return c.report()


CHECKS = (check_the_figures_are_what_they_claim,
          check_the_figures_keep_clear_of_their_scrollbar,
          check_open_is_a_level_and_the_other_two_are_flows,
          check_the_window_moves_every_block_it_should,
          check_a_retag_is_already_in_the_mirror,
          check_only_the_pairs_worth_counting_are_counted,
          check_the_window_moves_the_retags_too,
          check_only_the_board_s_own_threads_count,
          check_the_block_shows_a_tail_rather_than_dropping_it,
          check_the_rows_add_up,
          check_every_offered_tag_keeps_its_row,
          check_the_window_compares_dates_the_only_way_that_works,
          check_the_table_reads_in_the_toolbar_s_order,
          check_the_middle_not_the_mean,
          check_it_says_nothing_rather_than_something_wrong,
          check_the_month_label_reads_in_a_narrow_column,
          check_the_figures_ride_the_slow_lane,
          check_it_folds_the_way_the_rail_folds)
