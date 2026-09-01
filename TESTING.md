# Testing Bert on your laptop

Bert is the board you'll be clicking on. Everything behind it — the database,
the Discord sync, the bit that posts back to threads — runs on the other
person's machine. You run Bert only and point it at theirs, so there is one
database and one board between you: they'll see your changes and you'll see
theirs.

Nothing gets installed on your laptop except Python. No Discord token, no
database, no setup beyond the four steps below. Ten minutes, most of it
waiting for downloads.

## Before you start

Ask whoever is running it for **the address**. It looks like
`192.168.1.20:8788` — a number and a port. They get it from their own terminal
when they start the stack.

You both need to be on the **same network**. Office wifi is fine. A VPN, a
phone hotspot, or "guest" wifi will not reach their machine.

## 1. Install Python

Download it from [python.org/downloads](https://www.python.org/downloads/) —
any version from 3.11 up.

On the very first screen of the installer, tick **"Add python.exe to PATH"**
before clicking Install. It's easy to miss and everything else depends on it.

To check it worked, open Command Prompt (Start → type `cmd`) and run:

```
python --version
```

If that prints a version number, you're set. If it says `'python' is not
recognized`, the PATH box wasn't ticked — run the installer again and choose
Modify.

## 2. Get the code

If you have Git:

```
git clone https://github.com/BellaFiore623/ernie-test.git
```

If you don't: open
[the repository](https://github.com/BellaFiore623/ernie-test) in a browser,
click the green **Code** button, **Download ZIP**, and extract it somewhere
you'll find again — Documents is fine. Extract it properly; opening the ZIP
and running from inside it won't work.

If GitHub asks you to sign in or says the page doesn't exist, the repository
is private — ask them to add you.

## 3. Start Bert

Open the folder and **double-click `bert.cmd`**.

The first time, it will:

- ask for the address from the top of this page — paste it in, press Enter;
- install the two things Bert needs (PySide6 and httpx), which takes a minute
  or two the first time and never again;
- open the board.

It remembers the address, so from then on double-clicking is all it takes.

To point it somewhere else later, run it from Command Prompt with the new
address after it — `bert.cmd 192.168.1.20:8788` — or delete the file
`%USERPROFILE%\.bert-host` and it will ask you again next time.

Closing the Bert window is all you need to do to stop it.

## 4. Put your name in

Click the **gear** in the top right, type your name, Save.

Do this before anything else. Every change is recorded against a person, and
that name goes into the Discord thread — so until it's set, Bert won't let you
change anything.

## What you can do

- **Drag a ticket** between priority bands, or drag a row in the running order
  on the left. Empty bands show a dashed "drop here" while you're dragging.
- **Edit** a ticket for its title, tag, client and work items. Work items are
  the bubbles: type one and press Enter to add it, click the ✕ to remove it.
- **✓ on a bubble** ticks that item off as done.
- **Complete** closes the whole ticket.

Everything you do is posted into the Discord thread about a minute later,
under your name. Inside that minute there's an **Undo** button in Recent
activity at the bottom — undo within the minute and nothing is ever posted at
all.

This is the sandbox Discord server, not real customer threads. It's still
worth pretending it's real, since that's what we're testing.

## If something goes wrong

**A red bar saying "Can't reach Ernie".** Their stack isn't running, the
address is wrong, or you're on different networks. Changes are paused while
it's up, and the board shows the last thing it saw. Check with them — Bert
keeps retrying and picks up on its own once Ernie is back, no restart needed.
An amber "Reconnecting to Ernie" bar is the same thing, briefly, and usually
clears itself.

**Windows asks about the firewall.** That prompt appears on *their* machine,
not yours. If they clicked "Cancel" on it, nothing will connect until they
allow Python on private networks.

**`'python' is not recognized`.** Step 1, the PATH tick box.

**It's pointed at the wrong address.** Run it once with the right one:
`bert.cmd 192.168.1.20:8788`.

## What not to do

Don't run `run.sh`. That starts a second copy of the whole system against a
database on your laptop — you'd be looking at your own separate board rather
than the shared one, and it needs a Discord token you don't have.

## Not on Windows?

`bert.cmd` is the Windows launcher. Anywhere else, after installing Python:

```
pip install PySide6 httpx
python bert.py --api http://192.168.1.20:8788
```

with their address in place of that one.
