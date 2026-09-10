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
        s = api.stats()

        months = s["completed_by_month"]
        c.equal(sum(m["count"] for m in months), 3, "every closure is counted")
        c.ok(all(len(m["month"]) == 7 for m in months),
             "and grouped by month, not by day")
        c.ok(months == sorted(months, key=lambda m: m["month"]),
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
        c.equal(s["completed_by_month"], [], "no months")
        c.equal(s["ageing"], [], "nothing ageing")
        c.equal(s["time_to_complete"], None,
                "and no time to close, rather than a zero that reads as instant")

    return c.report()


def check_the_month_label_reads_in_a_narrow_column() -> bool:
    """The panel is 244px, so the year is spelled only where it turns over."""
    c = Check("the month label reads in a narrow column")

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
    b.con.execute("UPDATE thread_titles SET queue=? WHERE thread_id=?",
                  (queue, tid))
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

    # Every window the panel offers has to be one the API will take.
    c.ok(all(isinstance(d, int) and d > 0 for _, d in bert.STATS_WINDOWS),
         "every window is a positive number of days")
    lens = [d for _, d in bert.STATS_WINDOWS]
    c.equal(lens, sorted(lens), "and they are offered shortest first")
    c.ok(bert.STATS_WINDOW_DEFAULT in lens,
         "the default is one of the options, or the box opens on nothing")

    return c.report()


CHECKS = (check_the_figures_are_what_they_claim,
          check_open_is_a_level_and_the_other_two_are_flows,
          check_the_rows_add_up,
          check_every_offered_tag_keeps_its_row,
          check_the_window_compares_dates_the_only_way_that_works,
          check_the_table_reads_in_the_toolbar_s_order,
          check_the_middle_not_the_mean,
          check_it_says_nothing_rather_than_something_wrong,
          check_the_month_label_reads_in_a_narrow_column,
          check_the_figures_ride_the_slow_lane,
          check_it_folds_the_way_the_rail_folds)
