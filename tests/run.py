"""
Every check, in one go. Nothing here touches Discord or a real database.

    python tests/run.py
    python tests/run.py state          # just the ones in check_state.py

Exits non-zero if anything failed, so it can go in front of a commit.
"""

from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import check_board_order                                     # noqa: E402
import check_changelog                                       # noqa: E402
import check_freshness                                       # noqa: E402
import check_poll_hold                                       # noqa: E402
import check_state                                           # noqa: E402

MODULES = {"changelog": check_changelog, "state": check_state,
           "order": check_board_order, "poll": check_poll_hold,
           "fresh": check_freshness}


def main(argv: list[str]) -> int:
    wanted = argv or list(MODULES)
    unknown = [w for w in wanted if w not in MODULES]
    if unknown:
        print(f"no such check: {', '.join(unknown)}. "
              f"Try: {', '.join(MODULES)}", file=sys.stderr)
        return 2

    failed = 0
    for name in wanted:
        mod = MODULES[name]
        print(f"\n{name} -- {(mod.__doc__ or '').strip().splitlines()[0]}")
        for check in mod.CHECKS:
            if not check():
                failed += 1

    print()
    if failed:
        print(f"{failed} check(s) FAILED")
        return 1
    print("all checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
