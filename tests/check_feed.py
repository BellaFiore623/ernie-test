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
import pathlib
import json
import pathlib
import re
from datetime import datetime, timedelta, timezone

from support import Board, Check, iso

import bert
import ernie_api as api

ROOT = pathlib.Path(__file__).resolve().parent.parent


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


def check_a_closed_row_stays_one_line() -> bool:
    """
    A closed row must not word-wrap.

    _fit_feed takes the height every row is held to from the closed rows'
    sizeHint, and a wrapped QLabel reports its hint at a heuristic width of its
    own rather than the width the layout will give it -- 112px against 14 for
    the same line, measured. Wrapping the closed rows put that number into
    _feed_row_h, the panel grew to fit eight-line rows, and because the height
    was a running maximum that never came down, every redraw ratcheted it
    further: clicking anything made the feed swallow the window.

    Holding that height steady is safe only because of this: every value going
    into it is one line plus, at most, an Undo button.
    """
    c = Check("a closed row is one line")

    render = _method("_render_feed")
    wraps = [n for n in ast.walk(render)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "setWordWrap"]
    c.equal(len(wraps), 1, "the feed sets word wrap in one place")
    arg = wraps[0].args[0]
    c.ok(not (isinstance(arg, ast.Constant) and arg.value is True),
         "and not unconditionally on -- a closed row has to stay one line")
    c.ok(isinstance(arg, ast.Name) and arg.id == "opened",
         "it wraps exactly when the row is open")

    fit = _method("_fit_feed")
    assigns = [n for n in ast.walk(fit)
               if isinstance(n, ast.Assign)
               and any(getattr(t, "attr", None) == "_feed_row_h" for t in n.targets)]
    c.ok(assigns, "the fit still sets a row height")

    # The wrap guard above is what keeps the value going in here bounded, and
    # is why it is safe for it to be held steady rather than re-measured.
    c.ok(any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "max"
             for a in assigns for n in ast.walk(a)),
         "and holds it steady rather than letting it drop between renders")

    return c.report()


def check_opening_a_row_never_shortens_it() -> bool:
    """
    Opening a row must not move the ones below it upward.

    Rows are all held to one height, and that height is the tallest of them --
    a row carrying an Undo button, which is 20px against 12 for the text alone.
    An opened row is let out of that so it can grow, and a row with no Undo
    button then collapsed to its own smaller size: opening a line to read four
    more characters pulled the whole list up under the pointer.

    So the height a row is held to is the floor for an open one too. Opening
    either changes nothing, or adds exactly the lines the text needs.
    """
    c = Check("opening a row never makes it shorter")

    fit = _method("_fit_feed")
    setmins = [n for n in ast.walk(fit)
               if isinstance(n, ast.Call)
               and getattr(n.func, "attr", None) == "setMinimumHeight"
               and getattr(getattr(n.func, "value", None), "id", None) == "r"]
    c.equal(len(setmins), 1, "the open row gets one minimum height")

    arg = setmins[0].args[0]
    c.ok(isinstance(arg, ast.Call) and getattr(arg.func, "id", None) == "max",
         "and it is the larger of two things, not the natural size")
    c.ok(any(getattr(n, "attr", None) == "_feed_row_h" for n in ast.walk(arg)),
         "one of which is the height the closed rows are held to")
    c.ok(not (isinstance(arg, ast.Constant) and arg.value == 0),
         "never a bare zero, which is what let it collapse")

    return c.report()


def check_a_wide_window_shows_more() -> bool:
    """
    The clip was a fixed number of characters whatever the window was doing.

    On a full-screen board that threw the room away: lines were cut with half
    the row still empty, and rows offered themselves to be opened when there
    was nothing stopping them being read where they sat. The widths scale with
    the space instead -- capped near halfway across the window, because a line
    run the full width of a wide screen is further than the eye tracks, and the
    whole of it is one click away regardless.
    """
    c = Check("a wider window shows more of the line")

    long_thread = "PROD: Steel City Water - 30Aug26 - SSD0311 firmware rollback"
    e = ev("edited", work(added=1),
           'added "Return the equipment to the depot before Friday"',
           name=long_thread)

    narrow = bert.Bert._feed_text(e, scale=1.0)
    wide = bert.Bert._feed_text(e, scale=2.0)
    whole = bert.Bert._feed_text(e, full=True)

    c.ok(len(wide) > len(narrow), "a wider window shows more")
    c.ok(len(whole) >= len(wide), "but never more than there is")
    c.ok("Steel City Water" in wide, "the thread name gets its share")
    c.ok("depot" in wide, "and so does the detail")

    # The point of it: at some width the row stops needing to be opened.
    c.equal(bert.Bert._feed_text(e, scale=4.0), whole,
            "given room enough, the closed row is already the whole line")

    return c.report()


def check_the_scale_never_shrinks_a_narrow_board() -> bool:
    c = Check("a narrow board reads as it always did")

    e = ev("edited", work(added=1), 'added "Return bot"')
    base = bert.Bert._feed_text(e, scale=1.0)

    # Anything at or below 1 has to give exactly what it gave before, or a
    # small window would start clipping more than it used to.
    for sc in (1.0, 0.9, 0.5, 0.0):
        c.equal(bert.Bert._feed_text(e, scale=sc), base,
                f"scale {sc} is not allowed to clip further")

    # full ignores it entirely -- an opened row is the whole line at any width.
    whole = bert.Bert._feed_text(e, full=True)
    for sc in (0.5, 1.0, 3.0):
        c.equal(bert.Bert._feed_text(e, full=True, scale=sc), whole,
                f"an open row is unaffected at scale {sc}")

    return c.report()


def check_the_scale_is_capped_at_half_the_window() -> bool:
    c = Check("the cap on how far a line may run")

    scale = _method("_feed_scale")
    src = ast.dump(scale)
    c.ok("width" in src, "it reads the window width")
    c.ok(any(isinstance(n, ast.Constant) and n.value == 2 for n in ast.walk(scale)),
         "and stops at half of it rather than the whole row")
    c.ok(any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "min"
             for n in ast.walk(scale)),
         "taking the smaller of the room and that cap")
    c.ok(any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "max"
             for n in ast.walk(scale)),
         "and never returning less than 1")

    # It must measure the feed's own font, not the window's -- the window's is
    # wider, which cancelled the calculation out and left a full-screen board
    # clipping at the narrow width anyway.
    c.ok("FEED_FONT_PX" in src, "measured at the size the feed actually draws")

    return c.report()


def check_reordered_says_where_it_went() -> bool:
    """
    The row used to be the verb and nothing else.

    rank is a fraction, so it could never be shown as it stands; the positions
    are worked out where the move happens and stored on the event, because
    afterwards the ranks they were measured against have moved on.
    """
    c = Check("a reorder says where it went")

    line = text(ev("reordered", "high:5", "high:2"))
    c.ok("reordered" in line, "it still says reordered")
    c.ok("5th" in line and "2nd" in line, "and the two places it moved between")
    c.ok("High" in line, "and the band, so the places mean something")
    c.ok("Penn Hills" in line, "and which ticket")

    # Whatever the band is called on the board, which is not always its
    # stored name -- unassigned reads as "Needs Attention" there.
    c.ok(bert.BAND_LABEL["unassigned"]
         in text(ev("reordered", "unassigned:3", "unassigned:1")),
         "every band names itself the way the board names it")

    # The ones a naive ordinal rule gets wrong.
    teens = text(ev("reordered", "high:13", "high:11"))
    c.ok("13th" in teens and "11th" in teens, "11th and 13th, not 13rd and 11st")
    c.ok("21st" in text(ev("reordered", "low:22", "low:21")), "and 21st after them")

    # Written in the hour between recording the place and recording the band.
    no_band = text(ev("reordered", "5", "2"))
    c.ok("5th" in no_band and "2nd" in no_band, "a bare place still reads")
    c.ok("High" not in no_band, "and no band is invented for it")

    # Every reorder written before this existed carries nothing, and there is
    # no way to work it out now -- the ranks it was measured against are gone.
    old_row = text(ev("reordered", None, None))
    c.ok("reordered" in old_row and "Penn Hills" in old_row,
         "an older row still reads, without the places")
    c.ok("th" not in old_row.split("Penn")[0],
         "and invents no position it never recorded")

    # Not a number is not a position.
    c.ok("reordered" in text(ev("reordered", "somewhere", "else")),
         "and anything that isn't a number falls back too")

    return c.report()


def check_a_narrow_window_keeps_the_controls() -> bool:
    """
    Undo disappeared when the window got narrower, with nothing to say why.

    An unwrapped QLabel cannot be made smaller than its text, so the row's
    minimum width was the whole line plus every fixed column -- measured at
    1644px inside a 1000px window. The feed's horizontal scrollbar is off, so
    everything past the edge was cut away silently, and the Undo button is the
    last column. The text is what should give: it is a summary, and the whole
    of it is one click away.
    """
    c = Check("a narrow window keeps the Undo button")

    render = _method("_render_feed")

    # The line is a FeedLine, whose whole reason for existing is that it
    # reports no minimum width and so can be squeezed.
    c.ok(any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "FeedLine"
             for n in ast.walk(render)),
         "the line is the widget that can be squeezed")

    tree = ast.parse(_bert_src())
    cls = next((n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "FeedLine"), None)
    c.ok(cls is not None, "FeedLine exists")
    if cls:
        m = next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "minimumSizeHint"), None)
        c.ok(m is not None, "and overrides minimumSizeHint")
        if m:
            c.ok(any(isinstance(n, ast.Constant) and n.value == 0
                     for n in ast.walk(m)),
                 "reporting no minimum width, so the layout may squeeze it")

    # The chevron cannot ride along in the text: clipping the line would take
    # away the only sign that there is more of it.
    src = ast.dump(render)
    c.ok("FEED_MORE_W" in src, "the chevron has a column of its own")
    # Read off `_render_feed` alone. It used to grep the whole of bert.py,
    # so any local called `body` anywhere in 8,000 lines failed a rule about
    # this one function -- and the rule is about the line being built here,
    # not about a name being unavailable in the rest of the file.
    c.ok("body += " not in ast.unparse(render),
         "and is not appended to the line it would be clipped with")

    return c.report()


def check_the_window_has_a_floor() -> bool:
    """Below some width the board stops being usable at all -- the feed's
    fixed columns crowd the line out and the bands are too tight to drop
    into. It opens at that width, so it may as well not go under it."""
    c = Check("the window will not be squeezed to nothing")

    init = _method("__init__")
    mins = [n for n in ast.walk(init)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "setMinimumWidth"]
    c.ok(mins, "the window has a minimum width")

    resizes = [n for n in ast.walk(init)
               if isinstance(n, ast.Call)
               and getattr(n.func, "attr", None) == "resize"]
    c.ok(resizes, "and an opening size")
    if mins and resizes:
        c.equal(mins[0].args[0].value, resizes[0].args[0].value,
                "which is the same number -- it opens at its narrowest")

    return c.report()


def check_the_chevron_is_a_character() -> bool:
    """
    It was written as an HTML entity and drawn as one.

    The line itself is rich text -- it carries <b> tags, so Qt reads it as
    HTML and "&#9656;" comes out as an arrow. The chevron moved into a label
    of its own with nothing but the entity in it, and a QLabel only reads rich
    text when it can see a tag: with none, "&#9656;" is just seven characters.
    They were 84px of them in a 14px column, so what showed was "&#".
    """
    c = Check("the chevron is a character, not an entity")

    src = _bert_src()
    line = next((l for l in src.splitlines() if "chevron = QLabel(" in l), "")
    c.ok(line, "the chevron label is still built in one place")
    c.ok("&#" not in line, "and not out of an HTML entity")
    c.ok(chr(0x25b8) in line or "u25b8" in line, "but the character itself")

    # Anything drawn in a label of its own has the same problem, so no entity
    # may appear in a QLabel that holds nothing else.
    bare = [l.strip() for l in src.splitlines()
            if "QLabel(" in l and "&#" in l and "<" not in l]
    c.equal(bare, [], "and no other bare label is built from one")

    return c.report()


def check_the_controls_stay_near_their_line() -> bool:
    """
    On a full-screen board the buttons drifted away from the entry.

    The status chip and Undo are right-aligned in fixed columns, which is what
    keeps them a column you can run down and click. Unbounded, that put them a
    thousand pixels from the line they belong to and it stopped being obvious
    which button went with which entry.

    The row is capped instead. Capping each row on its own does not work --
    a row limited by itself takes the width of its own text, so the buttons
    land at a different x on every line, measured at 761 and 845 for two rows
    of one feed. The cap goes on the panel the rows fill, so every row is one
    width and the column stays a column.
    """
    c = Check("the controls stay near the line they belong to")

    panel = _method("_feed_panel")
    caps = [n for n in ast.walk(panel)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "setMaximumWidth"]
    c.ok(caps, "the feed has a maximum width")
    c.ok(any(getattr(a, "id", None) == "FEED_ROW_MAX_W"
             for n in caps for a in ast.walk(n)),
         "and it is the row cap")

    # On the container, not on each row: that is the part that keeps the
    # buttons in line with each other.
    render = _method("_render_feed")
    c.ok(not any(isinstance(n, ast.Call)
                 and getattr(n.func, "attr", None) == "setMaximumWidth"
                 for n in ast.walk(render)),
         "the rows themselves are not capped one by one")

    # The cap belongs to the scroll area, so its scrollbar comes to the cap
    # with it and closes the feed off instead of sitting at the window edge.
    c.ok(any(isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "setMaximumWidth"
             and getattr(getattr(n.func, "value", None), "attr", None) == "feed_scroll"
             for n in ast.walk(panel)),
         "and it is the scroll area that carries it, so the scrollbar does too")

    # Adding it with an alignment flag makes it take its own sizeHint, which
    # for a scroll area is small -- 432px, cap or no cap.
    adds = [n for n in ast.walk(panel)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", None) == "addWidget"
            and any(getattr(a, "attr", None) == "feed_scroll" for a in n.args)]
    c.ok(adds, "the scroll area is added to the panel")
    c.ok(all(len(n.args) == 1 for n in adds),
         "with no alignment flag, or it would shrink to its own size hint")

    # The line has to be sized for the room the row actually gets.
    scale = _method("_feed_scale")
    c.ok(any(getattr(n, "id", None) == "FEED_ROW_MAX_W" for n in ast.walk(scale)),
         "the line is measured against the capped width, not the window")

    return c.report()


def check_the_feed_can_be_resized() -> bool:
    """
    The feed was a fixed height nobody could argue with.

    Some days the history is the thing you are reading and the board can wait;
    other days the reverse. A splitter lets the two be traded off, which means
    neither half may carry a fixed height -- a fixed widget gives the handle
    nothing to move -- and _fit_feed has to stop dictating one, or the next
    poll would drag the handle back out from under whoever was moving it.
    """
    c = Check("the feed can be dragged bigger and smaller")

    src = _bert_src()
    init = _method("__init__")
    c.ok(any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "QSplitter"
             for n in ast.walk(init)),
         "the board and the feed are the two halves of a splitter")
    c.ok("setChildrenCollapsible(False)" in src,
         "and neither can be collapsed away to nothing by dragging")

    fit = _method("_fit_feed")
    # Rows are still held to one height -- a different question, and what
    # stops an undo shifting the list. It is the panel that must not be.
    #
    # Not even folded, which is the rule the side panels already follow: a
    # fixed widget gives the handle nothing to move, and the handle sits
    # right there against the caption. Folded is a minimum with the maximum
    # left open, so a drag can pull it back out.
    fixed = [n for n in ast.walk(fit)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "setFixedHeight"
             and getattr(getattr(n.func, "value", None), "attr", None) == "feed_panel"]
    c.equal(len(fixed), 0,
            "the panel is never given a fixed height, folded or not")
    c.ok(any(isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "setMinimumHeight"
             and getattr(getattr(n.func, "value", None), "attr", None)
             == "feed_panel"
             for n in ast.walk(fit)),
         "it is held to a minimum instead, which is the spine when folded")

    c.ok(any(isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "setMaximumHeight"
             for n in ast.walk(fit)),
         "an unfolded panel is uncapped, so the handle can pull it open")

    # Placed once. Re-applying it every poll is the bug this shape invites.
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "_feed_sized"
             for n in ast.walk(fit)),
         "the handle is placed once, not on every poll")

    # **The fold has to re-place the splitter**, which is the whole of the
    # bug behind it: narrowing the widget does not narrow the pane it sits
    # in, so folding left the caption, then three hundred pixels of empty
    # floor, then a handle stranded above it -- and the board got none of the
    # room the fold was for. Reported as the header collapsing "the content
    # in it" rather than the section. The figures panel already knew this;
    # the feed was the last one still learning it.
    place = _method("_place_feed")
    c.ok(place is not None, "there is one place that puts the handle")
    body = ast.get_source_segment(src, place) or "" if place else ""
    c.ok("setSizes" in body, "and it moves the splitter, not just the widget")
    c.ok("_folded_height" in body,
         "taking the pane down to the spine when the feed is folded")

    # The spine is measured, not counted. FEED_FOLDED was a hand-counted 30,
    # sized for a caret in an 11px label; the 24px fold button that replaced
    # it did not fit, so the panel was pinned shorter than its own contents
    # and the button was clipped -- at every window size, which is the
    # signature of a fixed height. `_folded_height()` asks the caption and
    # keeps FEED_FOLDED as a floor.
    folded = _method("_folded_height")
    c.ok(folded is not None, "the folded height is worked out, not written down")
    fbody = ast.get_source_segment(src, folded) or "" if folded else ""
    c.ok("feed_head" in fbody and "sizeHint" in fbody,
         "off the caption's own sizeHint, so a taller control moves it")
    c.ok("contentsMargins" in fbody,
         "plus the margins the panel keeps around it")
    c.ok("FEED_FOLDED" in fbody and "max(" in fbody,
         "with the old constant kept as a floor rather than the answer")

    # Nothing may go back to using the bare constant as a height.
    for name in ("_place_feed", "_unfold_feed_by_drag"):
        m = _method(name)
        mb = ast.get_source_segment(src, m) or "" if m else ""
        c.ok("FEED_FOLDED" not in mb,
             f"{name} asks for the measured height rather than the constant")

    toggle = _method("toggle_feed")
    tbody = ast.get_source_segment(src, toggle) or "" if toggle else ""
    c.ok("_place_feed" in tbody,
         "and the button calls it, because folding is the button's business")

    # A folded panel that cannot be dragged open is the complaint the side
    # panels already answered. The feed folds along the other axis and needs
    # the same answer.
    drag = _method("_unfold_feed_by_drag")
    c.ok(drag is not None, "a drag away from the bottom edge opens it again")
    dbody = ast.get_source_segment(src, drag) or "" if drag else ""
    c.ok("UNFOLD_GRAB" in dbody,
         "past the same grab distance the side panels use")
    c.ok("_place_feed" not in dbody,
         "and it does not re-place the pane -- the drag is already the "
         "height somebody is choosing")
    c.ok("splitterMoved" in src and "_unfold_feed_by_drag" in src,
         "wired to the handle that was doing nothing before")

    # And kept, or it has to be found again on every launch.
    remember = _method("_remember_feed_height")
    c.ok(any(isinstance(n, ast.Constant) and n.value == "feed_height"
             for n in ast.walk(remember)),
         "where it was dragged to is remembered")

    toggle = _method("toggle_feed")
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "_feed_sized"
             for n in ast.walk(toggle)),
         "and unfolding restores it rather than keeping what the fold left")

    # The running order is the same trade along the other axis.
    c.ok(src.count("QSplitter(") == 2, "the rail is on a splitter too")
    # One function places both sides: they share a splitter, and
    # setSizes takes every pane at once.
    place = _method("_place_sides")
    c.ok(any(isinstance(n, ast.Constant) and n.value == "rail_width"
             for n in ast.walk(place)),
         "its width is remembered as well")
    c.ok(any(isinstance(n, ast.Attribute) and n.attr == "folded"
             for n in ast.walk(place)),
         "and a folded rail is left to the button, not the handle")

    # Folding has to re-place the splitter, both ways. Narrowing the widget
    # does not narrow the pane it sits in -- the splitter keeps the width it
    # last allotted -- so a fold left a spine, then four hundred pixels of
    # empty floor, then a handle stranded in the middle of it, and the board
    # got none of the room the fold was for.
    whole = ast.parse(src)
    for cls_name in ("Rail", "Stats"):
        cls = next((n for n in ast.walk(whole) if isinstance(n, ast.ClassDef)
                    and n.name == cls_name), None)
        fold = next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                     and n.name == "set_folded"), None) if cls else None
        body = ast.get_source_segment(src, fold) or "" if fold else ""
        c.ok("_place_sides()" in body,
             f"{cls_name}.set_folded re-places the splitter")
        # Outside the if/else, or one direction goes unplaced.
        tail = body.split("setMaximumWidth")[-1] if "setMaximumWidth" in body else ""
        c.ok("_place_sides()" in tail,
             f"{cls_name} does it folding as well as unfolding")

    # Bounded: too narrow loses the client, too wide is a second board.
    c.ok("RAIL_MIN_W" in src and "RAIL_MAX_W" in src,
         "with a floor and a ceiling on how far it goes")

    return c.report()


def check_a_cards_corner_survives_a_long_client() -> bool:
    """
    On a card the text gives way, never the marks in the corner.

    The same rule as a feed row, and it was broken the same way. An ordinary
    QLabel cannot be made narrower than its text, so the client name claimed
    its whole width as a floor and pushed the fixed columns after it -- the
    PIP count, the age, and the mark saying a change has not gone out -- past
    the edge of the card, where they were cut away in silence.

    It is not hypothetical: 'Municipal Authority of Westmoreland County' is a
    real customer, and it wants 408px of head inside a 300px card. The mark
    was measured ending at x=402. The fix is FeedLine's: keep the width the
    label wants, report no minimum, and let the layout squeeze the text.
    """
    c = Check("a card's corner survives a long client name")

    tree = ast.parse(_bert_src())
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "ClickableLabel")
    hint = next((n for n in cls.body if isinstance(n, ast.FunctionDef)
                 and n.name == "minimumSizeHint"), None)
    c.ok(hint is not None,
         "the client label reports a minimum size of its own")

    # Zero width, and the real height -- Ignored would throw away the width
    # the label wants as well, leaving it nothing at all.
    zero = hint is not None and any(
        isinstance(n, ast.Call) and getattr(n.func, "id", None) == "QSize"
        and n.args and getattr(n.args[0], "value", None) == 0
        for n in ast.walk(hint))
    c.ok(zero, "and the width in it is zero, so the text can be squeezed")
    c.ok(hint is not None and any(
            getattr(n, "attr", None) == "height" for n in ast.walk(hint)),
         "while the height is still the label's own")

    # The mark has to be the last thing in the head, or it is not in the
    # corner -- and it is the column most easily pushed out.
    card = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "Card")
    view = next(n for n in card.body if isinstance(n, ast.FunctionDef)
                and n.name == "_build_view")
    body = ast.get_source_segment(_bert_src(), view) or ""
    c.ok("unsent_mark" in body, "the head asks whether anything is unsent")
    # Anchored on the PIP count rather than the age: the age used to be the
    # last column before the mark and now sits in the footer, so pinning the
    # order to it would be asking about a widget that is no longer in the row.
    c.ok(body.index("unsent_mark") > body.index("PIPs"),
         "and the mark is added after every other column, so it sits in the "
         "corner")

    return c.report()


def check_a_timestamp_with_no_timezone_does_not_kill_the_card() -> bool:
    """
    Subtracting a naive datetime from an aware one raises TypeError.

    Which is not ValueError, so it went straight past the guard, out of
    `_age_forms`, out of `_fit_foot` and out of `_build_view` -- the card did
    not draw at all. Not a wrong number: a dead card, and every card in that
    thread.

    Production's 20,755 human timestamps all carry an offset today, so this
    was latent rather than live. It is worth closing anyway, because the same
    mismatch is already written down in CLAUDE.md as a SQL trap that broke the
    outbox once: Python writes ISO8601 with a `T`, SQLite's `datetime('now')`
    writes a space and no offset. Anything that ever puts one of those in
    `messages.created_at` takes the board out.

    Read as UTC rather than refused, because everything writing one here
    already is -- Discord's own, and SQLite's -- so attaching the offset is
    reading the value rather than guessing at it.
    """
    c = Check("a timestamp with no timezone does not kill the card")

    day = 86400
    aware = bert.Card._age_days(iso(-3 * day))
    naive = bert.Card._age_days(iso(-3 * day).split("+")[0])
    c.equal(naive, aware, "a naive timestamp reads the same as an aware one")

    # SQLite's own shape, space and all.
    sqlite_ish = iso(-3 * day).split("+")[0].replace("T", " ")
    c.equal(bert.Card._age_days(sqlite_ish), aware,
            "and so does datetime('now'), separator and all")

    # Anything that is not a timestamp is no age, never an exception.
    for junk in (None, "", "not a date", 12345, 3.5):
        try:
            got = bert.Card._age_days(junk)
        except Exception as e:                      # noqa: BLE001 - the point
            got = f"{type(e).__name__}"
        c.equal(got, None, f"{junk!r} is no age rather than a crash")

    return c.report()


def check_a_cards_buttons_never_leave_the_card() -> bool:
    """
    The footer was laid out chips-first, and a QLabel does not shrink.

    An unwrapped QLabel reports its whole text as a minimum width, so two
    amber issue chips claimed the row and Edit and Complete were pushed off
    the end of the card. Rendered and measured at the board column's own
    463px minimum: one chip laid **Complete out at x=470 on a 463px card**,
    and two needed 850px -- over even at the full 846. Not rare: 13 of
    production's 50 open cards carry an issue chip and 4 carry two.

    Nothing reported it because nothing failed. The layout did what it was
    asked and what went missing went off-screen, which is the same way Undo
    disappeared out of a feed row -- and it resolves the same way. **The text
    gives way; the controls never do.**

    Read off the source rather than by building a Card: a widget made with no
    QApplication does not raise, it aborts the process, and these checks
    deliberately never make one.
    """
    c = Check("a card's buttons never leave the card")

    tree = ast.parse(_bert_src())
    card = next(n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "Card")
    view = next(n for n in card.body if isinstance(n, ast.FunctionDef)
                and n.name == "_build_view")
    body = ast.get_source_segment(_bert_src(), view) or ""

    # The buttons exist before anything is fitted, or there is nothing to
    # measure the chips against and the fitter is guessing.
    c.ok("self.edit_btn" in body and "self.done_btn" in body,
         "the footer builds its buttons")
    c.ok("_fit_foot" in body, "and asks what the row can hold")
    c.ok(body.index("self.done_btn = ") < body.index("_fit_foot"),
         "with both buttons made before the row is fitted")

    # The chips are never added straight from chip(): that is the version
    # that cannot be made narrower than its own words.
    fit = next((n for n in card.body if isinstance(n, ast.FunctionDef)
                and n.name == "_fit_foot"), None)
    c.ok(fit is not None, "the fitter is a function of its own")
    fsrc = ast.get_source_segment(_bert_src(), fit) or ""
    c.ok("elided_chip" in fsrc, "and issue chips go through elided_chip")

    # A chip cut to nothing is worse than no chip: the floor has to be big
    # enough to carry the word that tells two issues apart.
    c.ok(getattr(bert, "CARD_ISSUE_MIN_W", 0) >= 124,
         "an issue chip keeps room for its first word "
         f"(CARD_ISSUE_MIN_W = {getattr(bert, 'CARD_ISSUE_MIN_W', None)})")

    return c.report()


def check_the_age_gives_up_words_before_it_is_cut() -> bool:
    """
    The toolbar's rule, one row down.

    `status_forms()` drops a noun rather than letting Qt squeeze the label,
    because a word given up whole still reads and half a word does not. The
    card's age has the same two forms for the same reason -- and its short
    one is exactly what the card said before it was labelled at all, so
    giving the words up costs the number nothing.

    Pure, so it can be exercised without a QApplication: `_age_forms` does
    date arithmetic and returns strings, and builds no widget.
    """
    c = Check("the age gives up words before it is cut")

    day = 86400
    forms = bert.Card._age_forms(iso(-3 * day))
    c.equal(len(forms), 2, "there are two forms to choose between")
    c.ok(forms[0].startswith("Last reply"),
         f"the long one names what it counts: {forms[0]!r}")
    c.equal(forms[-1], "3d", "and the short one is the bare number")
    c.ok(len(forms[0]) > len(forms[-1]), "longest first, so the first that fits wins")

    # "Last updated" is the one wording that must never appear: this is the
    # newest message from a person, and cards.updated_at is a different fact.
    c.ok("updated" not in forms[0].lower(),
         "and it does not claim the ticket was updated")

    c.equal(bert.Card._age_forms(iso(-2 * day))[0], "Last reply 2d",
            "the number is the days, not a bucket")
    c.equal(bert.Card._age_forms(iso(-day))[0], "Last reply 1d",
            "and one day reads as one")

    # Blank has two causes and neither draws anything.
    c.equal(bert.Card._age_forms(None), [],
            "no timestamp -- nobody has ever posted -- says nothing")
    c.equal(bert.Card._age_forms(iso(-60)), [],
            "and somebody posting today says nothing either")
    # A clock disagreeing is not an age.
    c.equal(bert.Card._age_forms(iso(3 * day)), [],
            "a message stamped in the future is not shown as -3d")

    return c.report()


def check_a_card_cuts_its_client_to_the_room_it_has() -> bool:
    """
    A clipped name is a rendering fault; an elided one is a name.

    Squeezing the label is what keeps the card's corner on the card, but a
    hard clip mid-letter reads as broken. So it is cut the way a rail row cuts
    its two lines: told its room by the parent, measured with QFontMetrics
    against the font it actually draws in, with the whole string one hover
    away.

    Measured and not given a constant, because a card's head is not the fixed
    shape a rail row is -- an "edited" chip, a PIP count and the unsent mark
    are each there or not. On a 300px card 'Trekk Design Group' fits whole,
    and with a mark, three PIPs and an edited chip beside it, it does not.
    """
    c = Check("a card cuts its client name to the room it has")

    src = _bert_src()
    tree = ast.parse(src)

    def method(cls_name, fn):
        # None rather than an exception: a check that raises takes the whole
        # run down with it, and "the method is gone" is a finding, not a
        # crash.
        cls = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.ClassDef) and n.name == cls_name), None)
        if cls is None:
            return None
        return next((n for n in cls.body
                     if isinstance(n, ast.FunctionDef) and n.name == fn), None)

    def walk(node):
        return ast.walk(node) if node is not None else ()

    # Told its room, the way RailRow is.
    init = method("Card", "__init__")
    c.ok(init is not None and any(a.arg == "room" for a in init.args.args),
         "a card is told the width it will be given")

    view = method("Card", "_build_view")
    c.ok(any(isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "elidedText"
             for n in walk(view)),
         "and cuts the client name with elidedText, not a character count")

    # Measured chrome, not a constant: the head's columns come and go.
    roomfn = method("Card", "_client_room")
    c.ok(any(getattr(n, "attr", None) == "sizeHint" for n in walk(roomfn)),
         "the room is measured off the chrome that is actually there")
    c.ok(any(getattr(n, "attr", None) == "ensurePolished"
             for n in walk(roomfn)),
         "after the stylesheet padding has been applied to it")

    # The parent hands the width down, and a change of width is a redraw.
    setc = method("Band", "set_cards")
    sig = next((n for n in walk(setc) if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "sig" for t in n.targets)), None)
    c.ok(sig is not None and any(getattr(n, "id", None) == "room"
                                 for n in ast.walk(sig)),
         "the band counts its width as part of the picture")
    call = next((n for n in walk(setc) if isinstance(n, ast.Call)
                 and getattr(n.func, "id", None) == "Card"), None)
    c.ok(call is not None and any(getattr(a, "id", None) == "room"
                                  for a in call.args),
         "and passes it to every card it builds")

    # ...which is worth nothing unless resizing the window rebuilds them.
    node = method("Bert", "resizeEvent")
    resize = (ast.get_source_segment(src, node) or "") if node else ""
    c.ok("rail_redraw" in resize,
         "a resized window redraws the board, once it has settled")
    c.ok(".start()" in resize,
         "through the timer, so a drag is not thirty rebuilds a second")

    # The name is not lost, only shortened.
    body = (ast.get_source_segment(src, view) or "").split("elidedText")
    c.ok(len(body) > 1 and "setToolTip" in body[1],
         "and the whole name is still on the card, one hover away")

    return c.report()


def check_an_open_row_keeps_the_spacing() -> bool:
    """Opening a row must not close the gap under its last line.

    A closed row is _feed_row_h tall around a single line: the height comes
    from the taller Undo column beside it, and the label is top-aligned, so
    there is room under the words. An open row set to exactly what its label
    needs has none of that, and its last line sits that much nearer the row
    below than every other line in the feed -- which reads as the row being
    squeezed into the gap rather than the list making space for it.

    Measured on a real feed: closed rows left 16px under their text, the open
    one 3px. With the slack added, every row leaves 16.
    """
    c = Check("an open row keeps the spacing")

    src = _bert_src()
    fit = _method("_fit_feed")
    body = ast.get_source_segment(src, fit) or ""

    c.ok("slack" in body, "_fit_feed works out what a closed row leaves spare")
    c.ok("need + slack" in body,
         "and an open row is given its text plus that same room")

    # Off the rows, not off a font metric: the height being matched is
    # whatever the tallest closed row wanted, which no font knows.
    slack = body.split("slack =")[1].split(chr(10))[0] if "slack =" in body else ""
    c.ok("_feed_row_h" in slack,
         f"measured against the height rows are held to  ({slack.strip()!r})")

    # And it is still floored at the closed height, which is the older rule:
    # a row with no Undo button must not shrink when opened.
    c.ok("max(self._feed_row_h" in body,
         "while never coming out shorter than it was closed")
    return c.report()


def check_a_feed_row_reads_as_a_row() -> bool:
    """A rule under each entry ties its line to its buttons.

    The status chip and Undo are right-aligned in fixed columns, and the line
    they belong to ends a long way to their left -- FEED_ROW_MAX_W caps how
    far that can get, and it was still not clear which button went with which
    entry. The rule closes the rest of the distance.

    Drawn rather than added to the layout, because a separator widget would be
    another item for _fit_feed to walk, measure and hold to a row height, and
    would have to be kept out of every count the panel height comes from.
    """
    c = Check("a feed row reads as a row")

    src = _bert_src()
    tree = ast.parse(src)
    cls = next((n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == "FeedRow"), None)
    c.ok(cls is not None, "there is a row class of its own")
    body = (ast.get_source_segment(src, cls) or "") if cls else ""
    c.ok("paintEvent" in body, "which paints the rule itself")
    c.ok("T.LINE" in body, "in the palette's hairline, not a hex")
    c.ok("drawLine" in body, "as a line rather than a border on a stylesheet")

    # Every row, or the grouping is wrong for the ones without it.
    render = ast.get_source_segment(src, _method("_render_feed")) or ""
    c.ok("row = FeedRow()" in render, "and every entry gets one")
    c.ok("ClickableWidget() if more else QWidget()" not in render,
         "rather than only the ones you can open")

    # It must not have become a layout item -- _fit_feed walks every widget in
    # feed_lay and holds it to a row height.
    c.ok("feed_lay.addWidget(row)" in render,
         "rows are still what goes into the layout")
    c.ok("addWidget(QFrame" not in render and "separator" not in render.lower(),
         "and no separator widget was added beside them")
    return c.report()


def check_a_feed_row_sits_on_one_line() -> bool:
    """Every column in a row is centred on the same line, with room to spare.

    The Undo button only looked centred: it is taller than a line of text, so
    it filled a row that a top-aligned 16px label sat high in, and the time,
    the text and the status chip all rode above it. Everything is centred now,
    so they share a centre line.

    And the row carries FEED_ROW_PAD above and below. Without it the button is
    exactly as tall as the row, so it sat on the hairline under it -- the rule
    that ties a line to its buttons was touching the button it ties.
    """
    c = Check("a feed row sits on one line")

    src = _bert_src()
    render = ast.get_source_segment(src, _method("_render_feed")) or ""
    start = render.find("row = FeedRow()")
    end = render.find("self.feed_lay.addWidget(row)")
    block = render[start:end] if start >= 0 < end else ""

    c.ok(block, "the row-building block is still readable")
    c.ok("Qt.AlignTop" not in block,
         "nothing in the row is pinned to the top any more")
    c.equal(block.count("Qt.AlignVCenter"), 6,
            "every column is centred: time, text open, text shut, chevron, "
            "status, undo")
    c.ok("FEED_ROW_PAD" in block,
         "and the row keeps room above and below its contents")
    # The row's own layout, not its columns'. The status and undo columns
    # are flush on purpose -- the row is what carries the padding for them.
    c.ok("h.setContentsMargins(0, 0, 0, 0)" not in block,
         "rather than the row sitting flush against its own edges")

    # One pixel more at the bottom: the hairline is drawn in the row's own
    # last pixel, so equal padding centres against a box a pixel shorter than
    # it looks, and everything reads as sitting low.
    c.ok("FEED_ROW_PAD + 1" in block,
         "with the hairline's own pixel allowed for at the bottom")

    # And no layout gap. It falls above a row's contents but below the rule
    # above them, so it counted entirely against the top: measured 14px above
    # the text and 10 below, in a band that is meant to be even.
    panel = ast.get_source_segment(src, _method("_feed_panel")) or ""
    c.ok("feed_lay.setSpacing(0)" in panel,
         "and no gap between rows -- the rule is the separator")
    return c.report()


def check_a_rename_can_be_taken_back() -> bool:
    """
    The one change on the feed that could not be undone, for no reason.

    `undo` has handled a rename all along: inside the window it cancels the
    event, which is the whole job because nothing left the machine; after it,
    it queues a rename back. Bert simply never put the button on the row --
    `renamed` was missing from the tuple that decides which verbs get one.

    Reported as there being no Undo beside a change still sending, which is
    exactly the moment it is free.

    The verbs are read off both ends here, because the bug was the two
    disagreeing: the server could do something the client never offered.
    """
    c = Check("a rename can be taken back")

    src = _bert_src()
    tree = ast.parse(src)
    feed = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                 and n.name == "_render_feed"), None)
    c.ok(feed is not None, "the feed builds its rows")
    body = ast.get_source_segment(src, feed) or ""
    for verb in ("completed", "priority_changed", "edited", "work_done",
                 "renamed"):
        c.ok(f'"{verb}"' in body, f"{verb} gets an Undo button")

    # And the server agrees, which is the half that was already true.
    api = (ROOT / "ernie_api.py").read_text(encoding="utf-8")
    undo = next(n for n in ast.walk(ast.parse(api))
                if isinstance(n, ast.FunctionDef) and n.name == "undo")
    usrc = ast.get_source_segment(api, undo) or ""
    c.ok('e["verb"] == "renamed"' in usrc,
         "and undo knows how to put a title back")
    c.ok('post=True' in usrc,
         "queueing the rename back when the first one has already gone")

    # started is refused at the server, so it must not carry a button either.
    c.ok('"started"' not in body,
         "a thread opening in Discord is not something Bert can take back")

    return c.report()


def check_an_old_row_stops_offering_undo() -> bool:
    """Bert stops offering it; the API still takes one.

    The row this protects is a `completed` that Bert made. Undoing that past
    the undo window unarchives the thread, posts a correction into the
    customer thread and re-archives it -- so undoing a three-week-old closure
    pings everybody on a job that finished weeks ago. It is the most common
    row in the feed and Undo sits beside it.

    Measured on production the day this was written: 48 events over 21 days,
    of which undo already refused 43 -- every closure there came from Discord,
    and `started` is refused outright -- leaving 4 rows, none older than 20
    hours. And every undo anybody has actually made was inside a minute.

    So the division is deliberate and is the same one the close-with-work
    rule makes: **Bert will not offer it, rather than it cannot happen.** The
    API takes an old undo at any age, and `force` is how somebody says they
    mean it. A check that made the server refuse would be a different feature
    and a worse one -- there is no way back from a closure made in error on a
    Friday if Monday is too late.
    """
    c = Check("an old row stops offering undo")

    now = datetime.now(timezone.utc)

    def row(hours):
        return {"occurred_at": (now - timedelta(hours=hours)).isoformat()}

    c.ok(bert.undo_offered(row(0)), "a change just made is offered")
    c.ok(bert.undo_offered(row(23.5)), "and one from yesterday evening still is")
    c.ok(not bert.undo_offered(row(24.5)), "past a day it is not")
    c.ok(not bert.undo_offered(row(24 * 21)),
         "nor is a three-week-old closure, which is the row this is for")
    c.equal(bert.UNDO_OFFERED_S, 24 * 3600, "the cutoff is a day")

    # Fails open. Undo is how somebody repairs a mistake, and withholding it
    # over a field that would not parse is the wrong way round.
    c.ok(bert.undo_offered({}), "a row with no timestamp keeps its button")
    c.ok(bert.undo_offered({"occurred_at": "the other day"}),
         "and so does one whose timestamp will not parse")

    # The button is greyed, not removed: a row that simply loses it answers
    # nothing, and "why can I not undo this one" is the question being asked.
    body = (ROOT / "bert.py").read_text(encoding="utf-8")
    start = body.index("def _render_feed")
    # To the next method, whatever it is -- naming one is how the last slice
    # here silently measured an empty string.
    nxt = body.index(chr(10) + "    def ", start + 10)
    feed = body[start:nxt]
    c.ok("undo_offered(e)" in feed, "the feed asks the pure decision")
    c.ok("and offered" in feed,
         "and spends it on setEnabled, so the button is greyed rather than gone")

    # And the server has no such rule, on purpose.
    api = (ROOT / "ernie_api.py").read_text(encoding="utf-8")
    undo_src = api[api.index("def undo("):]
    undo_src = undo_src[:undo_src.index("\n@app.") if "\n@app." in undo_src
                        else len(undo_src)]
    c.ok("UNDO_OFFERED" not in undo_src and "too old" not in undo_src.lower(),
         "the API refuses nothing for age -- the cutoff is Bert's alone")

    return c.report()


def check_send_now_brings_one_change_forward() -> bool:
    """The opposite of undo, and the confirmation is the button itself.

    A change waits out `UNDO_WINDOW_S` before Ernie posts it, which is 60
    seconds of being able to change your mind for free -- and 60 seconds of a
    card saying *Pushing to Discord...* to somebody who is already certain.
    This is how they say so: `dispatch_after` comes forward and the drain
    takes it on its 5-second beat.

    Pressing it **spends** the window rather than skipping it. Afterwards
    undoing posts a correction into the thread instead of being silent, which
    is exactly why it is a button somebody presses rather than the window
    being shorter for everybody.

    The same statement `ernie_app.shut_down` already runs over every queued
    event when the window closes, for one row and on demand.
    """
    c = Check("send now brings one change forward")

    # -- what the feed offers it on, as a pure decision --------------------
    def row(**kw):
        base = {"dispatch_after": "2026-09-18T15:00:00+00:00",
                "posted_at": None, "undone_at": None, "claimed_at": None}
        return {**base, **kw}

    c.ok(bert.send_offered(row()), "a change still inside its window is offered")
    c.ok(not bert.send_offered(row(dispatch_after=None)),
         "a silent change is not -- a reorder posts nothing, so there is "
         "nothing waiting to go and what it waits on is a whole publish")
    c.ok(not bert.send_offered(row(posted_at="2026-09-18T15:01:00+00:00")),
         "nor is one already in the thread")
    c.ok(not bert.send_offered(row(undone_at="2026-09-18T15:01:00+00:00")),
         "nor one that was undone")
    c.ok(not bert.send_offered(row(claimed_at="2026-09-18T15:01:00+00:00")),
         "nor one the outbox is mid-write on -- /events sends claimed_at, so "
         "the button is absent rather than refused by a dialog")

    # -- and it really moves the row ---------------------------------------
    with Board() as b:
        tid = b.card("PROD: Acme - 01Sep26 - a thing")
        b.con.execute(
            """INSERT INTO events (event_id, thread_id, verb, actor_name,
                                   occurred_at, dispatch_after)
               VALUES ('ev-1', ?, 'completed', 'Bella', datetime('now'),
                       datetime('now', '+60 seconds'))""", (tid,))
        b.con.commit()
        was, api.DB = api.DB, b.path
        try:
            due = b.con.execute(
                "SELECT COUNT(*) FROM v_outbox_due WHERE event_id='ev-1'"
            ).fetchone()[0]
            c.equal(due, 0, "it is not due while it is inside its window")

            api.send_now("ev-1", api.ActorBody(actor="Bella Fiore"))

            due = b.con.execute(
                "SELECT COUNT(*) FROM v_outbox_due WHERE event_id='ev-1'"
            ).fetchone()[0]
            c.equal(due, 1, "and the outbox picks it up straight after")

            # The row is unchanged in every other way: this hurries a change,
            # it does not alter one.
            e = b.con.execute("SELECT * FROM events WHERE event_id='ev-1'").fetchone()
            c.equal(e["posted_at"], None, "it is not marked sent here")
            c.equal(e["undone_at"], None, "nor undone")
            c.equal(e["verb"], "completed", "and it is still the same change")

            # A silent change has nothing to bring forward, and says so rather
            # than quietly doing nothing.
            b.con.execute(
                """INSERT INTO events (event_id, thread_id, verb, actor_name,
                                       occurred_at, dispatch_after)
                   VALUES ('ev-2', ?, 'reordered', 'Bella', datetime('now'),
                           NULL)""", (tid,))
            b.con.commit()
            try:
                api.send_now("ev-2", api.ActorBody(actor="Bella Fiore"))
                c.ok(False, "a silent change is refused")
            except Exception as err:
                detail = getattr(err, "detail", {})
                code = detail.get("code") if isinstance(detail, dict) else None
                c.equal(code, "nothing_to_send",
                        "a silent change is refused by name, not by doing nothing")
        finally:
            api.DB = was

    return c.report()


CHECKS = (check_a_rename_can_be_taken_back,
          check_a_timestamp_with_no_timezone_does_not_kill_the_card,
          check_a_cards_buttons_never_leave_the_card,
          check_the_age_gives_up_words_before_it_is_cut,
          check_a_work_item_added, check_a_work_item_removed,
          check_an_edit_that_is_not_work,
          check_it_survives_a_row_it_cannot_read,
          check_renamed_says_the_new_name,
          check_the_lines_that_already_worked,
          check_long_text_is_clipped,
          check_only_rows_with_something_behind_them_open,
          check_an_open_row_survives_a_poll,
          check_an_open_row_does_not_stretch_the_others,
          check_a_closed_row_stays_one_line,
          check_opening_a_row_never_shortens_it,
          check_a_wide_window_shows_more,
          check_the_scale_never_shrinks_a_narrow_board,
          check_the_scale_is_capped_at_half_the_window,
          check_reordered_says_where_it_went,
          check_a_narrow_window_keeps_the_controls,
          check_the_window_has_a_floor,
          check_the_chevron_is_a_character,
          check_the_controls_stay_near_their_line,
          check_the_feed_can_be_resized,
          check_a_cards_corner_survives_a_long_client,
          check_a_card_cuts_its_client_to_the_room_it_has,
          check_an_open_row_keeps_the_spacing,
          check_a_feed_row_reads_as_a_row,
          check_a_feed_row_sits_on_one_line,
          check_an_old_row_stops_offering_undo,
          check_send_now_brings_one_change_forward)
