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


CHECKS = (check_the_figures_are_what_they_claim,
          check_the_middle_not_the_mean,
          check_it_says_nothing_rather_than_something_wrong,
          check_the_month_label_reads_in_a_narrow_column,
          check_the_figures_ride_the_slow_lane,
          check_it_folds_the_way_the_rail_folds)
