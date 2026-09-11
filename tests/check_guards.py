"""
The guards that stand between a test run and the real board.

Four scripts here can do something to a Discord server that cannot be taken
back or can put fiction where facts should be: `wipe_test.py` deletes every
thread in a channel, `seed_test_server.py` creates two dozen, `ernie_state.py
--check` runs a preflight that writes, and `tools/fake_stats_data.py` invents
a year of history in a mirror. Each refuses to run against production, and
each refusal is one comparison against one constant.

**That constant was wrong for the life of the project.** It held a guild id
that is not this company's, so every one of those guards compared production
against a server nobody has ever used and waved it through. `wipe_test.py`
carried the comment "set this to your real guild", and it never was. It was
found the only way a wrong guard ever is -- by one of them firing on the
wrong side, when the fake-data tool wrote 257 invented tickets into
production's mirror. Recovered in full from a backup; nothing reached
Discord, because production has no ALLOW_DISCORD_WRITES to reach it with.

So this file exists, and it holds two things rather than one. That the copies
agree -- because a value declared in four places is four chances to fix one
and believe the job is done. And that each guard still *asks the question*,
because the failure mode above is a comparison that runs, returns False, and
looks exactly like a comparison that was never there.

What it cannot hold is that the value is *correct*: the only thing to check
it against is production's own env file, which is deliberately not in the
repo. That stays a person's job, and is written down in CLAUDE.md.
"""

import ast
import pathlib

from support import Check

import ernie_sync


ROOT = pathlib.Path(__file__).resolve().parent.parent

# Every file that guards, and how it gets the constant. The two that import
# it cannot drift; the one copy that cannot import is checked by value.
GUARDING = ("wipe_test.py", "seed_test_server.py", "ernie_state.py",
            "tools/fake_stats_data.py")


def declarations():
    """Every `PRODUCTION_GUILD = "..."` in the tree, by file."""
    found = {}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith(("tests/", "build/", "dist/", ".venv/")):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and any(getattr(t, "id", "") == "PRODUCTION_GUILD"
                            for t in node.targets)
                    and isinstance(node.value, ast.Constant)):
                found[rel] = node.value.value
    return found


def check_every_copy_of_the_guild_agrees() -> bool:
    """
    One value, and anything holding its own copy holds the same one.

    Four files guarded on it and three kept their own literal. Fixing one and
    believing the job was done is exactly what a value copied four times
    invites, and there was nothing to say the other three were still wrong.
    """
    c = Check("every copy of the production guild agrees")

    found = declarations()
    c.ok(found, f"the constant is declared somewhere: {sorted(found)}")
    c.ok("ernie_sync.py" in found,
         "ernie_sync declares it, which is where the importers read it from")

    for where, value in sorted(found.items()):
        c.equal(value, ernie_sync.PRODUCTION_GUILD,
                f"{where} holds the same guild")

    # A digits-only snowflake. The old value was a well-formed id for a server
    # that does not exist here, so this cannot catch *wrong* -- but it does
    # catch a placeholder, an empty string, or a name typed in by mistake,
    # each of which would disable every guard at once.
    c.ok(ernie_sync.PRODUCTION_GUILD.isdigit()
         and len(ernie_sync.PRODUCTION_GUILD) >= 17,
         "and it is shaped like a guild id rather than a placeholder")

    return c.report()


def check_every_guard_still_asks() -> bool:
    """
    A guard that stops comparing looks exactly like one that never did.

    Deleting the import and the `if` from any of these leaves a script that
    runs perfectly well and does its work anywhere it is pointed. Nothing
    else in the suite would notice: these files have no unit surface, and
    every check here runs against a temp database rather than Discord.
    """
    c = Check("every guard still asks the question")

    for rel in GUARDING:
        src = (ROOT / rel).read_text(encoding="utf-8")
        c.ok("PRODUCTION_GUILD" in src, f"{rel} still mentions it")
        # Compared, not merely imported: an unused import is a guard that was
        # removed and left a tidy-looking line behind.
        tree = ast.parse(src)
        compares = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)
                    and any(getattr(x, "id", "") == "PRODUCTION_GUILD"
                            for x in ast.walk(n))]
        c.ok(bool(compares), f"{rel} compares against it")
        # And leaves when it matches. A comparison whose body does nothing is
        # the same as no comparison, and reads as safer than it is.
        leaves = any(
            isinstance(n, ast.If)
            and any(getattr(x, "id", "") == "PRODUCTION_GUILD"
                    for x in ast.walk(n.test))
            and any(isinstance(s, (ast.Raise, ast.Return))
                    or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                        and getattr(s.value.func, "attr", "") == "exit")
                    for s in ast.walk(n))
            for n in ast.walk(tree))
        c.ok(leaves, f"{rel} stops when it matches")

    return c.report()


def check_the_tool_guards_on_the_guild_not_the_filename() -> bool:
    """
    The exe made a filename worthless as evidence of which board it is.

    `tools/fake_stats_data.py` refused anything named `ernie.db`, which was
    exactly right while the only two databases in the world were `ernie.db`
    and `ernie-test.db`. An installed copy keeps its database at
    `%LOCALAPPDATA%\\Ernie\\ernie.db` whatever server it points at -- so the
    check refused a sandbox install, and would have allowed production under
    any other name. The guild is the fact the question is actually about, and
    the database already carries it on every thread.
    """
    c = Check("the fake-data tool guards on the guild, not the filename")

    src = (ROOT / "tools" / "fake_stats_data.py").read_text(encoding="utf-8")
    c.ok("endswith(\"ernie.db\")" not in src,
         "it no longer decides from the filename")
    c.ok("guild_id" in src, "it reads the guild out of the database")
    c.ok("no threads yet" in src,
         "and refuses a database too empty to tell, rather than guessing")

    return c.report()


CHECKS = (check_every_copy_of_the_guild_agrees,
          check_every_guard_still_asks,
          check_the_tool_guards_on_the_guild_not_the_filename)
