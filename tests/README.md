# Checks

    python tests/run.py                 # all of them
    python tests/run.py state           # one module

Standard library only, no network, no Discord, and no database of yours —
`support.Board` builds one from `schema.sql` in a temp directory and deletes
it on the way out. Safe to run against a live stack; it never opens
`ernie-test.db`, let alone `ernie.db`.

Not a full test suite and not trying to be. These pin the three places the
board was quietly saying things that were not true, all of them found by a
tester noticing the board and the change log disagreed:

| Module | What it holds down |
|---|---|
| `check_changelog.py` | A silent change — every band move that isn't in or out of `critical`, and every reorder — carries no `dispatch_after`, and reading that as "finished" logged the bulk of the board's activity instantly, ahead of the changes that do wait. And undo has no deadline, so a line posted and undone later has to be corrected where it stands. |
| `check_board_order.py` | `rank` is the running order and the only one. An unreadable thread gets a rank at the top of unassigned when its card is made, rather than being lifted there by whichever view happens to be drawing it. |
| `check_state.py` | `synced_at` is the publish and advances whether or not anything is coming back, so contact has to be measured on `agreed_at`, which only the pull writes. The summary says `last published` for the same reason: one machine's write time is all it can honestly know. |

Each check returns True or False rather than raising, so one failure doesn't
hide the rest — a bug usually trips several assertions and which ones is the
useful part. `run.py` exits non-zero if any of them fail.

Adding one: put it in the relevant module, name it `check_*`, take no
arguments, return `c.report()`, and add it to that module's `CHECKS` tuple.
Say what the check is defending against in the docstring, not just what it
does — the code already says what it does.

Worth knowing: a check that cannot fail proves nothing. When you add one, put
the bug back and watch it go red before you trust it.
