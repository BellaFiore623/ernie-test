# Versions, builds, and handing it out

What version a machine is running, how it finds out there is a newer
one, and what happens between a commit and somebody else's Start
menu.

Split out of CLAUDE.md on 2026-09-16, word for word. CLAUDE.md keeps the
rules and points here for the why.

## The version number


- **It is not `FORMAT_VERSION`.** That describes the shape of a payload in
  `#ernie-state` and moves when that shape changes; this moves when a build
  ships. Conflating them would tie a wire-format bump to a release.
- **The thing it replaces is worse than nothing.** `FastAPI(version="0.1")`
  was a literal that sat at 0.1 for the life of the project while `/docs` and
  `/openapi.json` quoted it at every reader.
- **The commit is part of the answer**, because the number alone cannot
  separate two machines while everybody runs from source: both say `0.9.0` and
  one of them is a week behind. It is read straight out of `.git` -- HEAD, then
  a loose ref, then `packed-refs`, and a detached HEAD is the sha itself --
  rather than shelled out to `git`, which need not be installed. Read **once,
  at import**: a process is the build it started as, so rebasing under a
  running Ernie must not change what it claims to be. No working copy is the
  ordinary answer for a zip download or a frozen build, and gives the bare
  number rather than an error.
- Said in four places, all off the same constant: `--version` on every entry
  point, a line at startup so a log answers it after the fact, `/health`'s
  `build` block, and Bert's settings window. Bert shows **both** ends there,
  its own and whatever `/health` reported, because in the one-backend-two-Berts
  setup those are two checkouts and either can be the stale one.
- **The build number is compared now, and there are two of them.** The wire
  format was always compared; this is the other half, and it arrived with the
  exe. While everybody runs from source a `git pull` is the update and drift
  is visible; a handed-out build is frozen at the day it was made and there is
  no pull.
  **`VERSION` is the newest; `MIN_BERT` is the floor** -- the oldest Bert this
  Ernie will work with, raised **by hand** on a release that genuinely breaks
  something. Refusing anything that is not exactly `VERSION` was the obvious
  rule and is the wrong one here: `describe()` carries the commit, people run
  from source, and every push would lock the other laptop out of a board it
  reads perfectly well. So "you cannot use Bert unless it is up to date" is
  true *when somebody decides it needs to be*.
  `/health`'s `build` block carries both, `bert.build_standing()` is the pure
  decision, and it **fails open at every step**: an Ernie that names no
  version says nothing about ours, one that names no floor can never block. A
  build check has no business taking a working board away over a field an
  older Ernie does not send.
  **Below the floor is read-only**, which `writable()` gates alongside
  `connected` -- what a distrusted build would write cannot be trusted to
  land. **Behind but above it is a note, not a wall**, and it has to *read*
  like one: "Bert found a new update" over a board that is working perfectly
  is an alarm, and an alarm that turns out to be nothing is how somebody
  learns to click through the next one without reading it -- and the next one
  is the blocked one. So it says the build is fine and nothing is paused,
  before it says there is a newer one.
  **And it can be silenced, for that build only.** The checkbox exists on the
  harmless state and not on the blocked one -- the dialog is not what is
  stopping anybody there, the floor is, so offering to hide it would promise
  something it cannot do. `settings.update_muted` holds **Ernie's** version
  rather than Bert's: "stop mentioning 0.9.9" should stop mentioning 0.9.9
  and should speak up again at 0.9.10, where muting on Bert's own version
  would silence every future release at once and amount to not having the
  check.
  The checkbox states its own `::indicator`, because Fusion draws that from
  the palette's roles and against this dialog's ground it came out as a label
  with no box beside it.
- **"Get the new build", and only when there is a build to get.** A button
  called *Update now* that opens a browser is the unsent mark saying
  *pushing* over something that was never going to be pushed -- a control has
  to do what it says. Self-updating a running exe is a different project:
  Windows locks the binary, so it wants a helper or an installer, and it runs
  into the code-signing question rather than around it.
  **Ernie publishes the address; Bert holds none.** `BERT_UPDATE_URL` in the
  env file reaches `/health`'s `build` block, so moving from GitHub to
  Bitbucket or anywhere else is **one line and a restart of Ernie** -- nobody
  holding a built Bert has to be sent a new one. Unset means no address,
  which means no button, which is the point: the control cannot exist without
  somewhere to go. It is why the API takes `--env` at all -- it reads that one
  key and nothing else; the API has no business with a token.
  **The scheme is checked at both ends**, `http` and `https` only. Bert is
  what hands the thing to the desktop, and a value nobody validated on the
  way in is a value somebody trusts on the way out.
  **The dialog names where it goes, taken from the address rather than
  written down.** "Get the new build" says what the button does and not where
  it lands, and a control that opens a browser should say so before it does --
  so a muted line under it reads `Opens drive.google.com in your browser`,
  off `urlparse(url).netloc`. Hard-coding "Google Drive" was the thing asked
  for and is the one version that cannot be kept: the address is published by
  Ernie precisely so the download can move without anybody rebuilding Bert,
  so a label naming the host would become a lie the day it moves -- and the
  kind nobody notices, because the button still works.
  **On the buttons' own line, at the far end of it**, rather than under them.
  Under them it has to pick a side, and the two states put the buttons in
  opposite orders -- blocked leads with *Get the new build* because that is
  the person who has to act, the other leads with *Alright* -- so a
  right-aligned caption sat under whichever button it was not describing.
  Measured on the blocked dialog: the line hung beneath *Fine*. On the row it
  reads as a caption for the row, which is what it is, at either order and
  with or without the checkbox above it.
  On the blocked dialog it comes **first**, because that is the person who has
  to act; on the other it sits after *Alright*, where carrying on is a fair
  answer. And the blocked wording says the board is still there to read, so
  "paused" is not mistaken for "gone".
  The failure it replaces was a misleading one: `Api` calls `.json()` without
  checking the status, so an old Bert asking for a route its Ernie lacks got
  `{"detail": "Not Found"}`, then a KeyError, then a failed poll -- surfacing
  as **"Can't reach Ernie."** A stale Bert looked like a dead server.
  **`MIN_BERT` above `VERSION` would lock everybody out at once**, from the
  server, with the only fix a build that does not exist yet. It is one line to
  get wrong in a release and there is no recovering from it in the field, so
  `check_version.py` holds the two in order.
- **Two flags stage a disagreement, because otherwise there is none.** Both
  halves of a from-source stack read the same `ernie_version`, so the one
  screen that appears only when two builds differ is unreachable on the
  machine that built it. `--pretend-version X` claims this Bert is X;
  `--pretend-ernie X` claims Ernie answered as X. They feed the real
  `build_standing()` rather than faking the dialog -- what wants exercising is
  the decision, not the picture.
- **The check was dead in the exe, and `newest` is what revives it.** It
  compares Bert against the Ernie answering it, which is a real comparison
  while those are two checkouts -- `run.sh`, or one backend and two Berts.
  `ernie_app` made them one process importing one module, so the two numbers
  became equal by construction: `build_standing` returned `ok` for every build
  that could ever ship, the dialog could not appear, and the only symptom was
  silence. Shipping the supervisor quietly deleted the feature.
  So the newest build has to come from **outside** the process.
  `**Release** 0.9.1` pinned in `#ernie-state` is where it comes from, read
  once a minute on the slow beat and kept in `release_seen`. Discord because
  it is already load-bearing: the token is on every machine, the channel is
  already being read, and a board that cannot reach Discord has bigger
  problems than being a version behind. **Google Drive was the other
  candidate and is where the installer actually lives** -- but a Drive file
  must be link-shared by somebody else, and its fetch-by-link URLs return an
  HTML interstitial often enough that a check which must fail open would
  simply stop reporting, with nothing to say so. The installer is a person
  clicking; this is not.
  **And read the way a person writes it.** It was
  `startswith("**Release**")`, exactly -- fine while a script posted the note
  and wrong the moment a person did. The first one written by hand said
  `Release 0.9.1`, no asterisks, and the channel went quiet: `read_release`
  answered `{}`, which `note_release` reads as the note having been taken
  down, so every board would have stopped knowing 0.9.1 existed.
  `RELEASE_OPENER` takes any markdown, any case, `Release`/`Released`/
  `Releases`, and any punctuation after. **It can afford to, because it is
  reading a pin** -- pinning is deliberate, so the text does not have to
  carry the whole burden of proving it was meant. What stays strict is that
  the word must *start* the message, so somebody talking about a release in a
  pinned message is still not one.
  **Posted by a person, not the bot.** Discord lets nobody edit somebody
  else's message, so a note the bot wrote can only ever be changed by running
  something -- and the one person who has to change it every release is then
  the one person who cannot. `read_release` reads the message's *content* and
  never its author, precisely so the note can belong to whoever maintains it.
  Found the first time a release was cut by somebody other than the script
  that had posted the note.
  **A pin, not a message in the channel.** The state channel is hundreds of
  messages on a real board and the note would be somewhere in the middle of
  them, so finding it would mean paging the lot; a pin is one request whatever
  the board has grown to. It is also the plainest way for a person to say
  which of two notes is live -- and where two are pinned, the **highest
  version wins**, because answering with the most recently pinned depends on
  an order Discord does not promise and answering with the older tells the
  team to downgrade.
  **`""` and `None` are different answers and the distinction is the point.**
  `""` is a channel read cleanly with no note -- taking the note down is how
  you say there is no newer build, so the stored answer retires. `None` is not
  being able to tell, and leaves what is already known: a 403 on a
  re-permissioned channel must not quietly tell every board it is up to date
  at the moment it stopped being able to find out.
  **And the version is read from where the word left off, not searched for.**
  `Release v0.9.1` read as **`9.1`**: the number was found with a loose
  search, and a word boundary does not fall between `v` and `0`, so the match
  began at the `9`. The `v` is the natural thing to type -- the tags are
  `v0.9.0` and `v0.9.1`, and the recipe above writes the tag two lines before
  the note. It failed in **both** directions and neither said anything:
  `v0.9.1` gave a version far ahead of any real build, so every board was
  told to fetch something that does not exist and went on saying so until
  somebody edited the note; `v1.0.0` gave `0.0`, behind everything, so a
  genuine release announced nothing at all. The `v` is swallowed now, in the
  minimum as well -- a floor that quietly fails to apply leaves somebody
  believing they made a release mandatory when they did not.
  The parser is **strict** and everything downstream is forgiving, which is
  the safe way round: `as_tuple` reads an unparseable version as 0, so junk
  that got past it could only ever make a board look *ahead* of the release --
  a silent no-op, and a silent no-op is the failure nobody notices.
  Whichever of `theirs` and `newest` is further ahead wins, and **the sentence
  names its source**, because the two send you to different places: an Ernie
  further ahead is a colleague's machine on a build you could get, and a
  release note is the build sitting in the shared folder. Nothing here is
  published automatically by a running Ernie -- tempting, since any machine on
  a newer build could bump the note, but a half-finished local build would
  then announce itself as the release and send everybody to fetch something
  that is not there.
- **Two tiers, and the version number decides neither.** `**Release** 0.9.1`
  is a note; `**Release** 0.9.1 minimum 0.9.1` sends anything older
  read-only. A patch release can be mandatory because it stops a build
  writing something wrong, and a minor one can be entirely optional because
  it adds a panel -- how big a change is and how dangerous it is to skip are
  different questions, so the note answers the second out loud. The word is
  spelled out rather than punctuated, because this is the line that takes
  somebody's board away and it should be impossible to type by accident.
  `blocked` was as unreachable in the exe as `behind` had been, and for the
  same reason: `floor` is this build's own `MIN_BERT` against this build's
  own `VERSION`, and `check_version.py` keeps the first at or below the
  second precisely so a release cannot lock everyone out.
  **A floor nobody can reach is the one failure here with no recovery** -- it
  takes every board read-only at once and the fix is to install something
  that does not exist. Unlike `MIN_BERT`, which has a check standing over it,
  this number is a sentence somebody typed into Discord on a Friday. So it is
  refused **twice**: the parser drops a minimum ahead of its own note's
  version, and `build_standing` refuses one again at the point of use,
  because that is the half that matters and it must not depend on the other
  having run. A floor with no known build to reach it is the same case and
  resolves the same way. The floor travels with the version it was written
  beside, so an old note's floor cannot outlive the note it belonged to.
- **A shipped exe migrates itself, because it has no shell to do it in.**
  `schema.sql` is all CREATE TABLE IF NOT EXISTS -- it creates tables and can
  never alter one -- so a database made before a column existed never grows
  it. From a checkout that is what `migrations/` is for: a script, run by
  hand, at a prompt. The person running an installer has neither, and the
  failure is total: the sync dies with `no such column`, `check_schema`
  refuses to start the API, and the only repair tool on that machine is the
  thing that will not start. Found on the first release that added a column
  after the build went out, and every future one would have done the same.
  `ernie_load.ADDED_COLUMNS` is the list and `connect()` applies it on every
  open, idempotently, after the schema script -- a table has to exist before
  it can be altered.
  **A missing table and a stale one are different problems, and the refusal
  has to say which.** `check_schema` named a `migrate_*.py` for both, and for
  a missing table that is a loop: the migration opens the database, finds no
  table, correctly does nothing and says schema.sql will create it on the
  next open; the API refuses again in the same words. Found against a copy of
  production's mirror, which predates `release_seen` and `client_collisions`
  and is exactly the database the first run there will meet. The API is a
  reader and still never applies schema.sql itself -- what changed is that it
  now names `ernie_load.connect()` for an absent table and a migration only
  for an absent column.
  **And `run.sh` brings the database up to the build before it starts
  anything**, which closes a race rather than a mistake: sync and api start
  within milliseconds of each other, the sync would have created the tables
  on its own first connect, and losing that race left the api exiting with
  nothing on screen but "api not responding yet". `ernie_app` already did
  exactly this before starting its API thread; this is the same line for the
  from-source stack. **Only columns added after the first shipped build
  belong there**: everything before it is already in `schema.sql`, and any
  database new enough to be an installed one was created from that.
  `migrations/` stays as the record and for databases that predate the exe.
- **A frozen build names its commit from a file.** There is no `.git` inside
  an exe, so the packaging step writes the sha into `build_commit.txt` beside
  the module and `_read_commit` reads it **before** walking `.git` -- a bundle
  carrying a stamp is not a clone, and there is nothing underneath it worth
  preferring.

## One process, which is what the exe runs

from source -- four processes, four logs, restart one without the others.

- **The API stays an API.** uvicorn on a thread bound to 127.0.0.1, and Bert
  talks to it exactly as it does now. No refactor of the client, and
  `run.sh test bert lan` still works the day somebody wants one backend
  shared across a network.
- **Qt owns the main thread**, which is not a preference: a QApplication has
  to be created there and its loop has to run there. So the two loops and
  uvicorn are the threads and Bert is what `main()` blocks on -- which is
  also what makes the shutdown reachable, since it is simply the code after
  `app.exec()` returns.
- **A port it cannot have is not a reason to fail.** The machine this gets
  tested on first is the one already running `run.sh`, so 8787 falls through
  to whatever the OS hands out and Bert is told the port that was bound.
- **The two loops never share a Discord client.** The sync's is built with no
  `allow_writes_for` and the outbox's with it; one client between them would
  hand the read-only half a handle that can post.
- **A named mutex, not a pid file.** A shipped exe gets double-clicked twice,
  and without a lock that is two syncs on one database and two outboxes on
  one state channel -- the failure `run.sh` already recorded six times over
  in a day. A pid file outlives a crash and then lies, which is why `run.sh`
  had to grow the honest version of it; the kernel keeps this one true.
- **Closing the window is what stops the outbox, and it spends the undo
  window rather than waiting it out.** `UNDO_WINDOW_S` is 60 and the outbox
  polls every 30, so a change made just before closing can be ninety seconds
  from its customer thread. Under `run.sh` that is handled by *not* stopping
  the outbox and asking somebody to leave three windows open for a minute;
  one process cannot ask that. So closing brings every undispatched event
  forward, drains, makes any threads still waiting, and publishes the board
  once more. **`undone_at` is still honoured** -- a change taken back and
  then closed on stays taken back, and it is the one row that looks exactly
  like the one being flushed.
- **The outbox has two beats too, split by cost the way the sync's are.**
  It did five things on one: drain, make the threads tickets are waiting on,
  publish `#ernie-state`, publish the status embeds, append to the change
  log. Only the first two are ones anybody is waiting on -- the three
  publishes edit in place and announce nothing -- so a change queued behind
  however long they took, and a slow pass or a contended write cost `drain()`
  its whole turn. `FAST_SECONDS` is 5 and `POLL_SECONDS` stays 30.
  Found as a Complete sitting unsent for minutes while the state channel was
  being rewritten after 357 messages arrived at once. The card said *Pushing
  to Discord…* the whole time and the log said nothing, because `run.sh`
  redirects to a file and **Python block-buffers stdout the moment it is not
  a tty** -- so the log ended at the previous evening for a process eighteen
  minutes old. `run.sh` starts all three with `python -u` now. The obvious
  move made it worse, and is worth writing down: `./run.sh stop` uses
  `taskkill`, which terminates without flushing, so stopping the process to
  read its buffer **discards** the buffer rather than revealing it.
  `--once` still does everything: a pass asked for by hand is asking for the
  whole job rather than the cheap half of it. `next_full` is a wall clock and
  the sleep is measured from the top of the pass, both for the reasons the
  sync gives.
- **The split made the publishes less frequent; it did not stop them
  blocking.** All five things still run on one thread, so a publish that
  takes four minutes is four minutes in which nothing posts. Two things
  follow from that, and both were found the same way -- a Close pressed in
  Bert with Ernie saying nothing in the thread.
  **The first pass is a fast one.** It was full unconditionally
  (`next_full = 0.0`), so starting the stack did all three publishes before
  ever calling `drain()`. Measured on a 51-card sandbox: **4m46s**, with a
  Close pressed 18 seconds in sitting behind the whole of it, `attempts` at
  0, nothing in the log, and the card saying *Pushing to Discord…*. Every
  restart had that window, and a person restarting the stack and then using
  it is the ordinary morning rather than an edge case. `next_full` starts at
  `now + interval`, so the publishes are 30 seconds late and nobody is
  waiting on them.
  **And a full pass drains again at the end of it**, rather than going
  straight to sleep, so a change queued while the publishes ran does not
  then wait out a whole fast beat as well. It costs one `SELECT` against
  `v_outbox_due`, and only after a pass that published. `fast_half()` is
  that half written once and called twice; `tests/check_sync_beats.py`
  holds the order by running the loop with every publish stubbed and reading
  back what it asked for, in sequence.
  **Still open:** 4m46s for one publish pass on a 51-card board is slow on
  its own account, with no rate limits in the log to explain it. Worth its
  own look -- the two fixes above stop it holding anybody up, which is not
  the same as it being right.
- **A blocked outbox says so; it does not vanish.** `run()` used to answer a
  refused write with `sys.exit`, which is right in a CLI and wrong on a
  thread: `SystemExit` there is swallowed by `threading` without a word, so
  the outbox would stop while the sync and the board carried on working and
  nothing anybody did reached Discord. It raises now, the supervisor catches
  it and names the line that would fix it, and the startup banner says
  whether this board can post at all. Read-only is a legitimate way to run --
  it is what production did for its whole first day, and what every handed-out
  copy of its env still does -- so this is reported, never refused.
  **That was the first configuration this met**, and it went the other way on
  2026-09-15. The switch was thrown last, after the mirror had been read back
  into step, the audit log was readable and **Manage Threads** and **Pin
  Messages** were granted -- the env line is the end of going live rather than
  the start of it, because the three before it are the ones that fail
  quietly.
- **Config and the database live in `%LOCALAPPDATA%\Ernie`**, because
  beside the executable is either PyInstaller's temp extraction directory --
  wiped on exit -- or a Program Files path nobody can write.

### Handing it to somebody

`python build.py --installer` produces `dist/Bert-<version>-setup.exe` --
96 MB of program compressed to about 31, which is what goes in the shared
Drive folder. The folder is also what `BERT_UPDATE_URL` points a browser at,
so *Get the new build* lands somebody on the thing they need to run.

**One installer in the folder, and the rest a level down.** The folder is
what a browser is pointed at, so every build sitting in it side by side is a
list to choose from -- and the largest number is only obviously the newest to
somebody who already knows how these are numbered. The current build is the
only `.exe` in the folder; the ones before it move into `old-installers/`
beside it, which keeps them without asking anybody to read past them.

**Two branches.** Work happens on `development`; `main` is what has been
built and handed out. The split protects nothing on its own -- production and
the sandbox run whatever was *installed*, not whatever a branch says -- so it
is a convention for working comfortably rather than a safety net. The safety
is the commit stamped into the exe.

**Cutting one.** Five steps, and the order is the whole of it:

```bash
git checkout main && git merge development     # build from main, never dev
python build.py --version 0.9.1                # bumps VERSION; no build needed yet
git commit -am "0.9.1"                         # the tag has to point at the bump
git tag v0.9.1
python build.py --installer                    # ~80s, from a clean tree
# upload dist/Bert-0.9.1-setup.exe to the Drive folder
# edit the pinned note in #ernie-state:  **Release** 0.9.1
```

**The bump is committed before the tag, and that order is the whole of it.**
`--version` edits `ernie_version.py`, so tagging first points the tag at a
commit that still says 0.9.0 -- and the exe then reports `0.9.1 (<sha>)`
naming a commit where VERSION is 0.9.0. The build also warns that the tree is
dirty, which is true and is easy to read past. Found on the first release
actually cut this way.

**Build from `main`, not `development`.** Otherwise the sha in the exe is a
commit `main` does not contain, and `git log main` stops explaining what
people are running -- which is the one question `build_commit.txt` exists to
answer.

**And tag it.** A tag answers "what is in the Drive folder" better than a
branch ever will: an exe is deleted, a folder is tidied, and a sha on its own
means looking through history. `v0.9.0` is `c04d2e9`, the first build handed
to anybody.

`--version` bumps `ernie_version.VERSION` on the way through, so the number,
the installer's name and what `/health` reports cannot drift apart.
`--clean` throws `build/` and `dist/` away first, which is only ever needed
when something looks stale.

**Commit before building.** `build_commit.txt` is written from HEAD, and a
dirty tree means the sha in the exe does not describe what is in it. It says
so and carries on, which is easy to read past -- and the field exists exactly
so two machines on one version number can be told apart.

**The upload and the note go together.** A note naming a build that is not in
the folder sends everybody to an empty page; a build in the folder with no
note is one nobody hears about. Neither is broken, and both are the kind of
thing nobody notices for a week.

- **The installed application is called Bert, and only the label moved.**
  The window has always said Bert -- it is the board, the thing with tickets
  in it, the half a person actually uses. Ernie is the half that talks to
  Discord and has no window at all, so the Start menu, the Add/Remove entry
  and the setup file were naming the product after its plumbing.
  **`AppName` was doing four jobs**, which is why this needed care rather
  than a find-and-replace: the display name, the program directory, the
  registry key, and `RMDir /r "$LOCALAPPDATA\${AppName}"` -- **the path to
  somebody's board**. Renamed naively, the uninstall's "also delete my data"
  would have gone looking in `%LOCALAPPDATA%\Bert`, found nothing, deleted
  nothing, and left the database and the env file with the Discord token in
  it sitting on disk. No error, at any point.
  So `AppName` is the label and **`AppDir` is the identity**: the program
  directory, the registry key and the board all key off `AppDir`, which stays
  `Ernie` for ever unless somebody deliberately migrates it. Moving the
  program directory would mean a new installer no longer *replaces* the old
  one -- two copies, two Add/Remove entries, and two programs holding
  different mutexes, so neither can tell the other is running, which is the
  one failure the mutex exists to prevent. Moving the board directory orphans
  every database in the field, production's included.
  `${OldShortcut}` clears the Start menu entry the previous name left, which
  otherwise points at an exe the upgrade has just deleted.
  **The check that should have caught it was assuming its own answer.**
  `check_app.py` resolved `${AppName}` by substituting the string `"Ernie"`
  into the script before looking, so every assertion about the board
  directory would have gone on passing through exactly the rename that broke
  it -- while the mutex beside it had a paragraph explaining why *it* was
  held together. It compares `AppDir` against `ernie_sync.CONFIG_DIR.name`
  now, and the failure was confirmed by making it: `got 'Bert', want
  'Ernie'`.
- **Per-user, and no admin.** `%LOCALAPPDATA%\Programs\Ernie`, HKCU for the
  Add/Remove Programs entry. Asking for admin would cost a UAC prompt on an
  unsigned binary -- the prompt people are right to refuse -- and put the
  files somewhere the application cannot write.
- **The program directory and the board are not the same place.** The program
  is disposable: wiped and rewritten whole on every upgrade, because
  PyInstaller's `_internal` changes shape between builds and copying over the
  top leaves orphaned DLLs beside the new ones -- and an orphan that still
  loads is the worst kind, since it works until it doesn't. The board lives in
  `%LOCALAPPDATA%\Ernie` where `CONFIG_DIR` already points, so the upgrade can
  be brutal and the uninstall can be safe.
- **The mutex is what makes "delete the old one" work.** Windows will not let
  a running exe be replaced, and the person replacing it is usually the person
  who has it open. The installer opens `ernie_app.MUTEX_NAME` and offers Retry
  rather than failing halfway and leaving a program directory that is half one
  build and half another. `tests/check_app.py` holds the two names together,
  because renaming the constant would break this in silence.
- **The uninstall offers to keep the board and defaults to keeping it.** An
  uninstall is usually somebody reinstalling. A silent one never asks, because
  a question nobody can see is a hang.
- **The sandbox env sits in the Drive folder, and that was decided rather
  than overlooked.** It carries the sandbox Discord token and the Jira API
  token, the folder is readable by everyone at the company, and that was
  raised and accepted 2026-09-11: an internal domain, a bot whose blast
  radius is a test server of invented tickets, and a service account nobody
  is doing anything with. Worth writing down so nobody reading the folder
  later mistakes it for a slip and "fixes" it.
  **Two things it does not extend to.** Our code is read-only against Jira --
  `ernie_jira.py` only ever calls the search endpoint -- but an Atlassian
  token carries the account's permissions, so that one can write to real Jira
  from anywhere; rotating it is `id.atlassian.com` and re-issuing the file.
  And **production's env was a different question, asked and answered
  2026-09-15: it is in the Drive folder too.** Raised as a hazard here and
  decided the other way by the people whose call it is -- the folder is
  shared `type: domain, role: reader` with `edgeaisolutions.com`, so this is
  the production Discord token readable by anyone with a company account,
  and that was accepted knowingly rather than overlooked. Written down for
  the same reason the sandbox one is: so nobody reading the folder later
  mistakes it for a slip and "fixes" it.
  **What that trades away, stated plainly so a later reader can re-decide
  with the facts.** `ALLOW_DISCORD_WRITES` is commented out in the copy up
  there, so anybody who installs it comes up read-only -- but that guard
  lives in *this code* and constrains Ernie, not the token. A person holding
  the token has whatever the bot has, which now includes Manage Threads and
  Pin Messages on a server of real customer threads, and no env file limits
  that. Rotating it is the Discord developer portal plus re-issuing every
  copy.
  **The operational catch is the one that bites a stranger**: the upload
  replaced the sandbox env in place rather than sitting beside it, and
  `install-instructions.txt` in that folder says "obtain ernie.env from the
  google drive folder". So the default download stopped being a sandbox and
  became production's 923 real threads, one uncommented line from posting.
- **No secret is in the installer.** The env file is not installed, generated
  or prompted for: a token baked into a setup.exe in a shared folder is a
  token shared with everyone who can reach that folder, and it would be the
  second copy of it. It is handed over separately, and a first run that cannot
  find one says so on screen -- naming the exact path -- rather than opening a
  blank board with the reason in a log nobody knows exists yet.
