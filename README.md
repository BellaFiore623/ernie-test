# Ernie + Bert

Internal tooling for tracking equipment tickets.

Work happens in Discord threads, and it stays there — Discord is the source of
truth, not a notification channel bolted onto a database. **Ernie** mirrors
those threads into SQLite and posts updates back. **Bert** is a desktop board
on top of Ernie's HTTP API: drag a ticket into a priority, tick work off, close
it, and the thread hears about it.

```
   Discord  ──sync──▶  SQLite  ──API──▶  Bert
      ▲                                    │
      └──────────── outbox ◀───────────────┘
```

Four processes, one database:

| | |
|---|---|
| **sync** | Discord → SQLite, every 60s. Read-only against Discord. |
| **api** | FastAPI over the database. The only thing Bert talks to. |
| **outbox** | SQLite → Discord. The **only** thing that writes to Discord. |
| **bert** | PySide6 board. Polls the API every 5s. |

## Running it

```bash
./run.sh test bert          # sandbox: sync + outbox + api + bert
./run.sh prod               # production: sync + api only, never the outbox
```

On Windows without Git Bash, `stack.cmd` does the same thing by double-click.

Configuration lives in `ernie.env` / `ernie-test.env`, both gitignored and
neither in this repository. `ernie-test.env.example` is the template:
copy it, drop the `.example`, and fill in the token, the guild id and the
channel ids from whoever set it up.

```bash
pip install -r requirements.txt
```

Python 3.11+, SQLite in WAL mode. `schema.sql` is applied on every connect and
is all `CREATE ... IF NOT EXISTS`, so there is no migration step for a new
database.

```bash
python tests/run.py
```

Standard library, no network, and no database of yours — the fixture builds
one from `schema.sql` in a temp directory.

## Testing with two people

Two ways, and they are genuinely different setups — see **[TESTING.md](TESTING.md)**,
which is written to be handed to whoever is joining.

- **One stack, two Berts** — one machine runs everything, the other runs only
  Bert against it. No token on the second machine, ~5s to see each other's
  changes, same network required.
- **Two stacks** — both machines run everything and share the board through a
  `#ernie-state` channel in Discord. Works from anywhere, survives either
  laptop closing, costs a token on both machines and up to a sync cycle of lag.

## Why it's built the way it is

A few decisions that look odd until you know what's behind them.

**Board state lives in an edited message, not in the thread title.** Measured
against Discord: editing a message recovers from its rate limit in 0.67s and
announces nothing, while renaming a thread allows two per ten minutes, reports
`scope: shared` so a second instance gets no budget of its own, and posts a
system message into the customer thread every time. Encoding the running order
in titles would be both slower and noisier than the silence it was meant to buy.

**Changes wait a minute before they post.** Every change writes an event with a
`dispatch_after`; the outbox only sends once that passes. Undo inside the window
deletes nothing from Discord because nothing was ever sent. Undo after it posts
a correction that names what it retracts and replies to it.

**Conflicts are resolved against a stored base, never by comparing clocks.**
Two boards means two laptops, and a clock a few minutes out would win or lose
every tie in the same direction with nothing to show for it. `state_sync`
records what each machine last agreed with the channel about, so the question is
who moved, not whose number is bigger.

**Priority reorders are silent.** In or out of `critical` posts to the thread.
Everything else is board housekeeping, and posting each nudge between high and
medium is noise in a conversation with a customer.

## Safety rails

This writes to Discord, so most of the design is about not writing to the
wrong place.

- Every Discord write goes through one `Discord.write()`, and the guild guard
  lives there — no code path can skip it.
- `ALLOW_DISCORD_WRITES` must **equal** `DISCORD_GUILD_ID` or nothing posts.
  Two values that have to agree, rather than a boolean a stray env var could
  flip. Production's env file does not contain the line at all.
- `run.sh prod` never starts the outbox, and refuses `lan` outright.
- `seed_test_server.py` and `wipe_test.py` both refuse to run against the
  production guild id.
- The mirror is append-only: titles and message revisions accumulate, deletions
  set `deleted_at`, and a re-sync never overwrites Bert's own state.

## What's where

| File | Role |
|---|---|
| `ernie_sync.py` | Discord → SQLite. Read-only against Discord. Runs as a loop. |
| `ernie_extract.py` | Parsing. Pure functions over dicts, no I/O. |
| `ernie_load.py` | Writes extracted records into SQLite. Idempotent. |
| `ernie_api.py` | FastAPI over the database. Everything Bert talks to. |
| `ernie_outbox.py` | The only thing that posts to Discord. |
| `ernie_state.py` | Board state shared between machines, in `#ernie-state`. |
| `ernie_changelog.py` | Every change appended to `#change-log`. Off unless configured. |
| `bert.py` | The PySide6 client. |
| `schema.sql` | Applied on every connect. |
| `run.sh` · `stack.cmd` · `bert.cmd` | Launchers. |
| `seed_test_server.py` · `wipe_test.py` | Build and tear down the sandbox. Test guild only. |
| `tools/` | Things you reach for occasionally: `q.py` for ad-hoc SQL, `ernie_backup.py`, `dump_threads.py` for raw API JSON, shell shortcuts. |
| `tests/` | `python tests/run.py`. No network, no database of yours. |
| `migrations/` | One-off scripts already applied. Kept as a record. |
| `assets/` | Bert's logo. |

The seven `ernie_*.py` modules and `bert.py` stay at the root on purpose:
every launcher and every documented command runs them by name, and a
package would buy tidiness at the cost of breaking all of it.

`CLAUDE.md` carries the working notes — the hard rules, the data model, and the
reasons behind the parts that have bitten before. Read it before changing
anything here.
