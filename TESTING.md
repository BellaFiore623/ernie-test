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

1. **The bot token** and the sandbox guild's ids. They will send you an
   `ernie-test.env` file, or its contents.
2. **The `#ernie-state` channel id.**
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

If GitHub says the page doesn't exist, the repository is private. Ask to be
added.

## 3. Put the env file in place

Save what they sent you as `ernie-test.env`, in the folder with `bert.py` in
it. It looks like this:

```
DISCORD_TOKEN=...
DISCORD_GUILD_ID=...
ALLOW_DISCORD_WRITES=...
TEST_CHANNEL_ID=...
CARD_CHANNEL_IDS=...
STATE_CHANNEL_ID=...
```

Two lines matter more than the rest:

- **`ALLOW_DISCORD_WRITES` must be exactly the same number as
  `DISCORD_GUILD_ID`.** If it isn't, nothing you do will ever reach the other
  person — your board will look fine and they'll never see a thing.
- **`STATE_CHANNEL_ID`** is what makes the two boards one board. Without it
  you get a private board of your own.

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

## What you'll see

Top right, next to the refresh button, is the shared-board indicator:

- **`shared board · up to date`** — your board and theirs agree.
- **`shared board · 2 to send`** — you've changed something they haven't been
  told about. It goes out on the next cycle. Normal for up to a minute.
- **`shared board · no contact for 5m`** — nothing has been exchanged in a
  while. Usually their stack isn't running, or yours has stopped. Check the
  terminal you started it in.

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

**`shared board · no contact` that doesn't clear.** Their stack is down, or your
sync has died. Check with them, then check your own terminal.

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
