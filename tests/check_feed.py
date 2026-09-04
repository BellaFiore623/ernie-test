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

import json
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
    c.ok(bare.strip().endswith(THREAD[:46].strip()),
         "no detail means no trailing separator left dangling")

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


CHECKS = (check_a_work_item_added, check_a_work_item_removed,
          check_an_edit_that_is_not_work,
          check_it_survives_a_row_it_cannot_read,
          check_renamed_says_the_new_name,
          check_the_lines_that_already_worked,
          check_long_text_is_clipped)
