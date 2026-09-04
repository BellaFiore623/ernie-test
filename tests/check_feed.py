"""
What the activity feed actually says happened.

The feed is the one place a person reads the board's history as it goes, and
for most of what it recorded it said only the verb. An edit is batched on
purpose -- four fields and three bubbles are one event and one message to the
customer thread -- so "Bella Fiore edited PROD: ..." covered adding a work
item, removing one, renaming the client, and all three at once. Everything the
line needed was already on the row: old_value carries the shape of the change,
new_value the prose.

The reading is deliberately forgiving. A row whose old_value won't parse still
gets a line, because a feed that drops history it can't classify is worse than
one that says "edited".
"""

import ast
import json
import pathlib
import re

from support import Check

import bert


THREAD = "PROD: Penn Hills - 02Sep26 - EReel-1220 fiber respool"


def ev(verb, old=None, new=None, who="Bella Fiore", name=THREAD):
    return {"verb": verb, "actor_name": who, "thread_name": name,
            "old_value": old, "new_value": new}


def work(added=0, removed=0, **fields):
    """An edited event's old_value: the fields that moved, plus the bubbles."""
    d = dict(fields)
    d["__work__"] = {"added": [f"a{i}" for i in range(added)],
                     "removed": [f"r{i}" for i in range(removed)]}
    return json.dumps(d)


def text(e):
    """The line with its markup taken off, which is what a person reads."""
    return re.sub("<[^>]+>", "", bert.Bert._feed_text(e)).replace("&middot;", "·")


def check_a_work_item_added() -> bool:
    c = Check("adding a work item")

    line = text(ev("edited", work(added=1), 'added "Return bot"'))
    c.ok("Bella Fiore" in line, "it names who did it")
    c.ok("added a work item" in line, "and says what they did, not just 'edited'")
    c.ok("Penn Hills" in line, "and which ticket")
    c.ok("Return bot" in line, "and which bubble")
    c.ok(line.count("added") == 1,
         "without saying 'added' twice -- the prose repeats the verb")

    many = text(ev("edited", work(added=3), 'added "one", "two", "three"'))
    c.ok("added 3 work items" in many, "several are counted")
    for item in ("one", "two", "three"):
        c.ok(item in many, f"and {item} is named")

    return c.report()


def check_a_work_item_removed() -> bool:
    c = Check("removing a work item")

    line = text(ev("edited", work(removed=1), 'removed "Return bot"'))
    c.ok("removed a work item" in line, "it says removed, not edited")
    c.ok("Return bot" in line, "and which one")
    c.ok(line.count("removed") == 1, "once, not twice")

    both = text(ev("edited", work(added=1, removed=1),
                   'added "new"; removed "old"'))
    c.ok("changed the work" in both,
         "adding and removing in one edit is neither on its own")

    return c.report()


def check_an_edit_that_is_not_work() -> bool:
    c = Check("edits that touch fields")

    plain = text(ev("edited", json.dumps({"client_override": None}),
                    "client: (empty) -> IPI"))
    c.ok("edited" in plain, "a field change is still an edit")
    c.ok("IPI" in plain, "but it says what changed")

    # A batched edit is the case the wording must not overclaim: bubbles moved
    # and so did a field, so it is not "added a work item" and never was.
    mixed = text(ev("edited", work(added=1, client_override=None),
                    'client: (empty) -> IPI; added "Return bot"'))
    c.ok("edited" in mixed, "a field and a bubble together is an edit")
    c.ok("added a work item" not in mixed,
         "not dressed up as only the bubble half of it")
    c.ok("IPI" in mixed and "Return bot" in mixed, "and both halves are shown")

    return c.report()


def check_it_survives_a_row_it_cannot_read() -> bool:
    c = Check("a row the shape can't be read off")

    for bad in (None, "", "not json", "[]", '"a string"', json.dumps({"__work__": 7})):
        line = text(ev("edited", bad, "something happened"))
        c.ok("Bella Fiore" in line and "Penn Hills" in line,
             f"still a line for {str(bad)[:14]!r}")
        c.ok("edited" in line, "falling back to the verb rather than guessing")

    # And with no prose either, it is still a sentence.
    bare = text(ev("edited", None, None))
    c.ok("·" not in bare,
         "no detail means no trailing separator left dangling")
    c.ok("Penn Hills" in bare, "and the thread is still named")

    return c.report()


def check_renamed_says_the_new_name() -> bool:
    c = Check("renaming")

    line = text(ev("renamed", "OPS: old", "OPS: Trekk - 04Aug26 - SSD0008"))
    c.ok("renamed" in line, "it says renamed")
    c.ok("Trekk" in line, "and what it is called now, which is the point")

    # Nothing to show falls back rather than printing an empty pair of quotes.
    c.ok("renamed" in text(ev("renamed", "OPS: old", None)),
         "a rename with no new name still reads")

    return c.report()


def check_the_lines_that_already_worked() -> bool:
    """The verbs that read well before must not have been disturbed."""
    c = Check("the rest of the feed")

    done = text(ev("work_done", None, "Return bot"))
    c.ok("finished" in done and "Return bot" in done, "work_done still names the item")

    started = text(ev("started", None, None, who="Tyler"))
    c.ok("Tyler" in started and "started" in started, "started still reads")

    moved = text(ev("priority_changed", "medium", "critical"))
    c.ok("moved" in moved, "priority still says moved")

    c.ok("retracted" in text(ev("undo_correction", "a", "b")),
         "and undo_correction still retracts")

    return c.report()


def check_long_text_is_clipped() -> bool:
    c = Check("a bubble longer than the row")

    long_item = "x" * 300
    line = text(ev("edited", work(added=1), f'added "{long_item}"'))
    c.ok(len(line) < 200, f"the line stays readable ({len(line)} chars)")
    c.ok("…" in line, "and says it was cut rather than just stopping")

    return c.report()


def full(e):
    return re.sub("<[^>]+>", "", bert.Bert._feed_text(e, full=True))


def _bert_src():
    return pathlib.Path(bert.__file__).read_text(encoding="utf-8")


def _method(name):
    tree = ast.parse(_bert_src())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Bert")
    return next(n for n in cls.body
                if isinstance(n, ast.FunctionDef) and n.name == name)


def check_only_rows_with_something_behind_them_open() -> bool:
    """
    A row is worth a click only when clipping actually hid something.

    Comparing the clipped line with the whole one is the test, and it needs no
    layout: clipping is the only thing that removes content. Giving every row
    an affordance would teach people to click rows that never change.
    """
    c = Check("which rows can be opened")

    # Short on both counts: nothing for the clip to take.
    short = ev("work_done", None, "Return bot", name="OPS: Trekk - 04Aug26")
    c.equal(bert.Bert._feed_text(short),
            bert.Bert._feed_text(short, full=True),
            "a line that already fits has nothing behind it")

    long_one = ev("edited", work(added=1),
                  'added "' + "a work item far longer than the row allows" * 3 + '"')
    c.ok(text(long_one) != full(long_one), "a clipped line does")
    c.ok(len(full(long_one)) > len(text(long_one)),
         "and opening it shows strictly more")
    c.ok("…" not in full(long_one), "with nothing left cut off")

    # The thread name is clipped too, so a long title alone is enough.
    titled = ev("completed", name="PROD: " + "Some Very Long Client Name " * 3)
    c.ok(text(titled) != full(titled), "a long thread name is reason enough")

    return c.report()


def check_an_open_row_survives_a_poll() -> bool:
    """
    The feed rebuilds every row every five seconds.

    So which rows are open cannot live on the widgets -- they are all thrown
    away and made again on each poll, and a row opened to read would shut
    under you before you finished. It is kept by event_id on the window.
    """
    c = Check("an open row stays open")

    src = _bert_src()
    c.ok("self.feed_open = set()" in src,
         "the set is made once on the window, not per render")

    toggle = _method("_toggle_feed_row")
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "feed_open"
             for n in ast.walk(toggle)),
         "the toggle writes to it")
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "_render_feed"
             for n in ast.walk(toggle)),
         "and redraws, so the click shows immediately")

    render = _method("_render_feed")
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "feed_open"
             for n in ast.walk(render)),
         "and the render reads it back rather than starting closed")

    # The toggle is a plain set operation, so it can be checked outright.
    class Bare:
        _render_feed = lambda self: None
        _toggle_feed_row = bert.Bert._toggle_feed_row

    b = Bare()
    b.feed_open = set()
    b._toggle_feed_row("e1")
    c.equal(b.feed_open, {"e1"}, "clicking opens it")
    b._toggle_feed_row("e2")
    c.equal(b.feed_open, {"e1", "e2"}, "two can be open at once")
    b._toggle_feed_row("e1")
    c.equal(b.feed_open, {"e2"}, "and clicking again closes that one only")

    return c.report()


def check_an_open_row_does_not_stretch_the_others() -> bool:
    """
    Rows are held to one height so an undo doesn't shift the list under you,
    and that height only ever grows. Measuring an opened row into it would
    leave every row four lines deep for the rest of the session.
    """
    c = Check("opening one row leaves the rest alone")

    fit = _method("_fit_feed")
    src = ast.dump(fit)
    c.ok("expanded" in src, "the fit knows which rows are open")
    c.ok("setMinimumHeight" in src or "setMaximumHeight" in src,
         "an open row is let out of the fixed height")
    c.ok("setFixedHeight" in src, "while the closed ones are still held to it")

    return c.report()


CHECKS = (check_a_work_item_added, check_a_work_item_removed,
          check_an_edit_that_is_not_work,
          check_it_survives_a_row_it_cannot_read,
          check_renamed_says_the_new_name,
          check_the_lines_that_already_worked,
          check_long_text_is_clipped,
          check_only_rows_with_something_behind_them_open,
          check_an_open_row_survives_a_poll,
          check_an_open_row_does_not_stretch_the_others)
