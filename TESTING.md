# Testing with two people

There are two ways to share a board. Pick one before you start — they are
different setups and mixing them will confuse you.

| | **Two stacks** | **One stack, two Berts** |
|---|---|---|
| You each run | everything | one of you runs everything |
| They need a Discord token | **yes** | no |
| Same network required | no | **yes** |
| Their changes appear in | up to a minute | about 5 seconds |
| Set up in | ~20 minutes | ~10 minutes |

**Same office, same afternoon — use one stack** (jump to the bottom). It is
simpler, faster to set up, and the board updates as fast as you can click.

**Different places, or nobody wants to leave a laptop running — use two
stacks.** That is the rest of this page.

---

# Installed build, or from source?

That is a different question from the one above, and you answer it first.

**The installed build is the normal way.** `Ernie-<version>-setup.exe` in the
shared Drive folder is one download and one double-click: no Python, no
clone, no command line. It runs the sync, the outbox, the API and Bert as one
program. Take this unless you are changing the code.

**From source is for working on it** — four processes you can restart
separately, which is what the rest of this page describes. You need Python
and a clone.

Either way you still pick two stacks or one stack above; how you installed
and how you share are not related.

## Two names, and they are not the same thing

You install something called Bert and the next step hands you a file called
`ernie.env`. That is deliberate rather than a leftover:

| | |
|---|---|
| **Bert** | The application. The window, the Start menu shortcut, `Bert.exe`, the Add/Remove Programs entry — the board with the tickets on it. |
| **Ernie** | The machinery. `ernie.env`, `ernie.db`, `%LOCALAPPDATA%\Ernie`, the logs — the half that talks to Discord. |
| `ErnieBert` | The lock that stops two copies running against one database. Named after both, because one program is both. |

**Bert never opens the env file or the database.** Not one line of it: it
talks to Ernie over a local connection and nothing else, holds no Discord
token, and does not know where that folder is. Everything that *does* read
that file — the sync, the outbox, the state channel, the status messages — is
Ernie. So the file is named after the half that reads it, and the program is
named after the half you look at.

## The env file, and which one

**The installer carries no token on purpose.** A secret baked into a setup
file in a shared folder is a secret shared with everyone who can reach that
folder. So it is handed over separately and it goes here, exactly:

```
%LOCALAPPDATA%\Ernie\ernie.env
```

Paste that into the Explorer address bar to get to the folder. If the file
isn't there, the first run says so on screen and names that path — it does
not open an empty board and leave the reason in a log.

**The one in the Drive folder is production's.** It points at the real
server — hundreds of threads of live customer work — and it is what "obtain
ernie.env from the google drive folder" gets you. That is the right file if
you are joining the production board, which is what most people are doing.

**A sandbox env exists too and is not in that folder.** Ask for it. It points
at the test server — invented tickets, nothing anybody is working on — and it
is what you want if you are trying things out rather than joining the real
board.

**Production's copy arrives read-only.** There is no `ALLOW_DISCORD_WRITES`
line in it, so your board mirrors and reads and cannot post a thing. That is
a legitimate way to run and it is how a second machine should start — read
the board, check it agrees with everyone else's, and only then post into
threads people are working in. Turning it into a writer is uncommenting one
line, and worth agreeing with whoever sent you the file first.

The sandbox env is the other way round: writes are already on, because a test
server is for testing and there is nothing there to damage.

**The token in either file is a password for the bot.** The production one
can post into customer threads as Ernie, and no env setting changes that —
`ALLOW_DISCORD_WRITES` restrains this program, not the token.

## Upgrading

Run the new setup file. **Do not uninstall first**, and do not re-place your
env.

The program and the board live in two different directories, which is what
makes that safe:

```
%LOCALAPPDATA%\Programs\Ernie    the program — wiped and rewritten whole
%LOCALAPPDATA%\Ernie             env, database, logs — never touched
```

The program directory is replaced rather than written over, because its
internals change shape between builds and leaving old files beside new ones
is how you get a version that works until it doesn't. Nothing you care about
is in there.

Close Ernie before you upgrade. If it's open, the installer notices and
offers **Retry** rather than failing halfway and leaving you with half of one
build and half of another — close the window and click Retry.

---

# Two stacks

You each run your own copy of everything: your own database, your own sync,
your own Bert. Neither machine talks to the other. You meet in Discord: the
board's priorities, running order, work items and closures live in a channel
called `#ernie-state`, one message per card, and each side reads the other's
changes from there.

This means either of you can shut your laptop without breaking the other's
board.

## Before you start

You need three things from whoever set this up:

1. **The bot token**, the sandbox guild id, and the channel ids. Step 3 has
   a template with every line in it, so they only need to send the values.
2. **The `#ernie-state` channel id**, which is one of them and the one that
   makes your board and theirs the same board.
3. **An invite to the sandbox Discord server**, if you're not in it.

The token is a password for the bot. Don't put it in a chat that isn't the
two of you, and don't commit it — `.gitignore` already covers `*.env`, so it
stays out of git as long as you leave it named `ernie-test.env`.

## 1. Install Python

[python.org/downloads](https://www.python.org/downloads/), any version from
3.11 up.

On the first screen of the installer tick **"Add python.exe to PATH"**. It's
easy to miss and everything depends on it. Check it worked — open Command
Prompt (Start → type `cmd`) and run:

```
python --version
```

If that prints a version you're set. If it says `'python' is not recognized`,
run the installer again and choose Modify.

## 2. Get the code

```
git clone https://github.com/BellaFiore623/ernie-test.git
cd ernie-test
```

No Git? Open [the repository](https://github.com/BellaFiore623/ernie-test),
green **Code** button, **Download ZIP**, and extract it somewhere you'll find
again. Extract it properly — running from inside the ZIP won't work.

If GitHub asks you to sign in, you don't need an account to read it — the
repository is public. You only need one to push changes back.

## 3. Put the env file in place

The repository ships a template with every line already in it and the values
left blank. Copy it, then paste in the numbers they sent you:

```
copy ernie-test.env.example ernie-test.env
```

(`cp ernie-test.env.example ernie-test.env` in Git Bash.) Open the copy in
Notepad and fill in the values after each `=`. It has to sit in the folder
with `bert.py` in it, and it has to be called exactly `ernie-test.env` --
that name is what keeps it out of git.

Two lines matter more than the rest:

- **`ALLOW_DISCORD_WRITES` is not a separate number.** Paste the same value
  you just put in `DISCORD_GUILD_ID`, character for character. If the two
  don't match, nothing you do will ever reach the other person — your board
  will look fine and they'll never see a thing. Step 5 stops you first, with
  `Writes are off. ALLOW_DISCORD_WRITES must equal <the guild id>`; that
  message is telling you the value to paste in.
- **`STATE_CHANNEL_ID`** is what makes the two boards one board. Without it
  you get a private board of your own.

Leave `CHANGELOG_CHANNEL_ID` empty. Only one machine should set it, and that
is theirs — both boards hold the whole history, so two loggers would write
every line twice.

The token is a password for the bot. Don't put it in a chat that isn't the
two of you.

## 4. Check your clock

Seriously. Right-click the Windows clock → **Adjust date and time** → make
sure **Set time automatically** is on, and hit **Sync now**.

Ernie no longer lets a wrong clock decide who wins a disagreement, but the
times in the activity feed come from whichever machine made the change, so a
clock that's an hour out makes the feed read like nonsense. Ernie will tell
you if yours is more than two minutes off.

## 5. First run

**Double-click `stack.cmd`.**

It installs what's missing, checks the things that are silent when they're
wrong, then starts the sync, the outbox, the API and Bert. The three
background parts each get their own minimised window, so if one falls over
its error is still there to read.

If the check fails it tells you what's wrong and stops rather than starting a
board that can't share. To run just the check on its own at any time:

```
python ernie_state.py --check --env ernie-test.env
```

On Mac or Linux, or if you'd rather use Git Bash: `./run.sh test bert`.

The first run takes a couple of minutes: it builds your database from
scratch, reads the threads out of Discord, then picks up the shared board
from `#ernie-state`. **Your board will start out matching theirs** — a new
machine adopts the shared order rather than imposing its own.

When you're done, close Bert, then give it a minute before closing the three
background windows. A change you made in the last minute hasn't been posted
to its thread yet, and the outbox has to still be running to post it.

Don't use `bert.cmd` in this setup — that's the launcher for the other one,
and it will say so if you try.

## 6. Put your name in

Click the **gear** top right, type your name, Save.

Do this before anything else. Every change is recorded against a person, that
name goes into the Discord thread, and it's what the other person sees in
their activity feed. Until it's set, Bert won't let you change anything.

The same dialog has **Theme** — light, dark, or following the desktop, which
tracks it as it changes. Saving a different one reopens the window; nothing is
lost but your place on the board.

At the bottom it shows the **version** twice: `Bert` is the copy you are
running, `Ernie` is the one answering it. Quote both if you report anything
odd — the two of you can be on different builds without either board saying
so, and that on its own explains a whole class of "it works for me".

## What you'll see

Top right, next to the refresh button, is the shared-board indicator:

- **`shared board · up to date`** — your board and theirs agree.
- **`shared board · 2 to send`** — you've changed something they haven't been
  told about. It goes out on the next cycle. Normal for up to a minute.
- **`shared board · can't read the other board`** — in red, and the one here
  that will not fix itself. The two of you are running builds that write the
  shared copy differently, so those cards are being skipped in both directions:
  nothing you do reaches them and nothing they do reaches you. Hover it — it
  says which of the two machines is the older one. Somebody has to update
  before either board is trustworthy again.
- **`shared board · no contact for 5m`** — **your own** stack has stopped
  reading the channel. Check the three minimised windows on your machine. It
  does not mean the other person is offline: the shared board lives in
  Discord, so their laptop being shut makes no difference to yours.

Hover any of them for the longer version. The "shared board" is one message
per card in the `#ernie-state` channel: it is the copy both machines read and
write, and it is what makes two Berts one board rather than two.

If the indicator isn't there at all, `STATE_CHANNEL_ID` isn't set — you have
a private board and nothing you do will reach them.

## What you can do

- **Drag a ticket** between priority bands, or drag a row in the running
  order on the left.
- **Edit** a ticket for its title, tag, client and work items. Work items are
  the bubbles: type one and press Enter to add, click ✕ to remove.
- **✓ on a bubble** ticks that item off as done.
- **Complete** closes the whole ticket.

All of that reaches the other board. Their changes reach you the same way.

## The things that will surprise you

**It takes up to a minute.** The sync runs on a cycle. Drag a card and the
other person will not see it move for anywhere up to a minute — this is
normal and the indicator tells you where you are. Don't drag it twice.

**If you both move the same card, the later one wins.** The one who loses
isn't told at the moment it happens, but the change shows up in their
activity feed with the other person's name on it, so it doesn't happen
silently — check the feed if a card isn't where you left it.

**The thread only hears once.** Whoever makes a change is the one whose Ernie
posts it to the Discord thread. Both boards show it; only one posts. If you
see the same update twice in a customer thread, something is wrong — say so.

**Undo is for your own recent changes.** Inside a minute of making a change
there's an **Undo** in Recent activity, and nothing was ever posted. After
that, undo posts a correction instead. If somebody else has moved the same
thing since, Ernie refuses the undo and says who — redo it by hand rather
than fighting it.

## If something goes wrong

**"Can't reach Ernie" (red bar).** Your own stack has stopped. Look at the
terminal you started it in. Nothing you do is lost — Bert reconnects on its
own.

**`shared board · no contact` that doesn't clear.** Your own sync has died, or
can't reach Discord. Check the "Ernie sync" window on your machine — this one
is not about the other person, and messaging them won't move it.

**Nothing you do reaches them.** Run the check, which looks for exactly
this:

```
python ernie_state.py --check --env ernie-test.env
```

It covers `STATE_CHANNEL_ID` being missing, the bot not being able to reach
the channel, and your clock being out. If it says everything's fine, check
the minimised "Ernie outbox" window is still running.

**`'python' is not recognized`.** Step 1, the PATH tick box.

**A card jumped back to where it was.** They moved it too, and theirs landed
second. Check Recent activity for their name.

## What not to do

**Don't point any of this at the production server.** Always
`--env ernie-test.env --db ernie-test.db`. `run.sh test` does this for you;
typing the commands by hand is where it goes wrong.

**Don't share the token further.** It can post as the bot in that server.

---

# One stack, two Berts

The simpler setup, for when you're on the same network. One of you runs
everything; the other runs only Bert and points it at the first machine.
No token, no database, no env file on the second machine — and the board
updates in about five seconds rather than a minute.

**The host** runs:

```
./run.sh test bert lan
```

which binds the API to every interface and prints an address like
`192.168.1.20:8788`. Windows asks once to allow Python through the firewall
— say yes, for **private** networks. Hand over that address.

**The other person** installs Python (step 1 above), gets the code (step 2),
then double-clicks **`bert.cmd`**. It asks for the address the first time,
installs PySide6 and httpx, and remembers the address for next time. To point
it somewhere else later, run `bert.cmd 192.168.1.20:8788`, or delete
`%USERPROFILE%\.bert-host`.

Both of you set your name in Settings.

Not on Windows:

```
pip install PySide6 httpx
python bert.py --api http://192.168.1.20:8788
```

**Don't run `run.sh` on the second machine** in this setup. That starts a
second sync and outbox against a database on their laptop — a board of their
own, not a shared one, and it needs a token they don't have.

The API has no password on it. Anyone who can reach that port can move cards
and post to threads. A trusted network, for as long as the test lasts, never
port-forwarded. `lan` is refused outright for production.
