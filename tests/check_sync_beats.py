"""
Two beats, because the halves of a cycle cost wildly different things.

Measured against the sandbox: a whole cycle is 13 GETs and ~4s, and 11 of
those GETs and 3.5s of that time are the **edit rescan** -- which found
nothing in either sampled cycle. What a new ticket actually arrives through
is the thread listing, and that is 1 GET and 0.30s.

Discord agrees the split is free. The listing route answered
`x-ratelimit-remaining: 999/1000` on eight back-to-back calls; the route the
rescan and the state pull use, `/channels/{id}/messages`, is 5 per 5s and
429s on the sixth. The cheap route is the one being asked more often.

The rule these hold: a fast pass does the listing, whatever is new in the
threads it named, and the local recompute -- and nothing else.
"""

import ast
import pathlib

from support import Board, Check, iso

import ernie_sync as S


ROOT = pathlib.Path(__file__).resolve().parent.parent


def source_of(name, kind=ast.FunctionDef):
    src = (ROOT / "ernie_sync.py").read_text(encoding="utf-8")
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, kind) and n.name == name)
    return ast.get_source_segment(src, node) or ""


def check_a_fast_pass_skips_what_a_fast_pass_is_for_skipping() -> bool:
    """The rescan is 11 of the 13 GETs, so it is the thing that must not run."""
    c = Check("a fast pass leaves the expensive half alone")

    body = source_of("cycle")
    c.ok("full" in (S.cycle.__code__.co_varnames[:S.cycle.__code__.co_argcount]),
         "cycle() takes the beat it is on")
    c.ok("if full:" in body, "and asks before the expensive half")
    # The three that a new ticket has to go through are unconditional.
    for fn in ("sync_threads", "sync_messages", "rebuild_derived"):
        line = next((l for l in body.splitlines() if fn + "(" in l), "")
        # Its own statement rather than nested under anything -- `threads =`
        # in front of one of them is an assignment, not a condition, so the
        # indent is what says whether it is guarded.
        indent = len(line) - len(line.lstrip())
        c.ok(line and indent == 8,
             f"{fn} runs on every pass, at the top level of the try "
             f"(indent {indent})")
    # The one that must not be.
    guarded = body.split("if full:")[1] if "if full:" in body else ""
    c.ok("rescan_edits" in guarded,
         "rescan_edits runs only on a full pass")
    c.ok("rescan_edits" not in body.split("if full:")[0],
         "and nowhere else in the cycle")

    return c.report()


def check_the_loop_holds_its_beat() -> bool:
    """
    The sleep is measured from the top of the pass, not the end of it.

    A pass that takes four seconds followed by a five-second sleep is a
    nine-second beat, and the full pass takes exactly that long -- so
    sleeping from the end would make the beat lurch once a minute.
    """
    c = Check("the loop sleeps against the top of the pass")

    body = source_of("main")
    sleeps = [l.strip() for l in body.splitlines() if "time.sleep(" in l]
    c.ok(sleeps, "the loop sleeps")
    c.ok(all("t0" in l for l in sleeps),
         f"and every sleep is measured from the top of the pass ({sleeps})")
    c.ok(all("max(0.0" in l for l in sleeps),
         "and never goes negative when a pass outruns the beat")

    # The full pass is scheduled by wall clock rather than by counting fast
    # passes, so a slow one does not drift the minute.
    c.ok("next_full" in body, "the full beat is its own clock")

    return c.report()


def check_a_quiet_fast_pass_says_nothing() -> bool:
    """
    Twelve times the passes must not be twelve times the log.

    Eleven of every twelve lines would read "nothing happened", which buries
    the ones somebody opens the file to find.
    """
    c = Check("a quiet fast pass writes no log line")

    body = source_of("main")
    c.ok("noisy" in body, "the loop asks whether anything happened")
    c.ok("if full or noisy:" in body,
         "and prints only then, or on the beat that always reports")
    for field in ("messages_new", "edits_found", "titles_changed",
                  "deletions_found"):
        c.ok(field in body.split("noisy")[1].split("if full or noisy")[0],
             f"{field} counts as something happening")

    return c.report()


def check_the_audit_table_is_bounded() -> bool:
    """
    Every pass writes a `sync_runs` row, and nothing was deleting them.

    The fast pass has to write one: `/health` reads the newest finished row
    and Bert draws it as "synced 20s ago", so a pass that wrote nothing would
    have the board reporting a staleness it does not have. Twelve times the
    passes is twelve times the rows, for ever.
    """
    c = Check("the audit table is bounded")

    with Board() as b:
        for _ in range(S.SYNC_RUNS_KEPT + 40):
            b.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)",
                          (iso(),))
        b.con.commit()
        newest = b.con.execute("SELECT MAX(run_id) FROM sync_runs").fetchone()[0]
        S.prune_runs(b.con)
        left = b.con.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
        still = b.con.execute("SELECT MAX(run_id) FROM sync_runs").fetchone()[0]
        c.equal(left, S.SYNC_RUNS_KEPT, "it is trimmed to the cap")
        c.equal(still, newest, "keeping the newest, which is what /health reads")

        # A table under the cap is left alone rather than emptied.
        b.con.execute("DELETE FROM sync_runs")
        for _ in range(5):
            b.con.execute("INSERT INTO sync_runs (started_at) VALUES (?)",
                          (iso(),))
        b.con.commit()
        S.prune_runs(b.con)
        c.equal(b.con.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0], 5,
                "and a table under the cap keeps every row")

    # On the full beat, so it is one statement a minute rather than twelve.
    body = source_of("main")
    after = body.split("if not full:")[-1]
    c.ok("prune_runs" in after,
         "pruned on the full pass rather than every five seconds")

    return c.report()


def check_the_beats_cannot_be_swapped() -> bool:
    """`--fast 60 --interval 5` is the two arguments the wrong way round."""
    c = Check("the two beats cannot be given the wrong way round")

    body = source_of("main")
    c.ok("a.fast > a.interval" in body,
         "a fast beat longer than the full one is refused")
    c.ok("a.fast < 1" in body, "and a zero beat is a loop with no sleep in it")
    c.ok(S.FAST_SECONDS <= S.CYCLE_SECONDS,
         f"the defaults agree ({S.FAST_SECONDS}s inside {S.CYCLE_SECONDS}s)")

    return c.report()


CHECKS = (check_a_fast_pass_skips_what_a_fast_pass_is_for_skipping,
          check_the_loop_holds_its_beat,
          check_a_quiet_fast_pass_says_nothing,
          check_the_audit_table_is_bounded,
          check_the_beats_cannot_be_swapped)
