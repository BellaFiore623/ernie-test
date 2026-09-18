# Discord, and what Ernie keeps of it

The mirror, the channels Ernie writes to, and how fast any of it
moves. Discord is the source of truth; everything here is about
reading it faithfully and writing back as little as possible.

Split out of CLAUDE.md on 2026-09-16, word for word. CLAUDE.md keeps the
rules and points here for the why.

## The message type, and why it is stored

- **`messages.type` is Discord's, and it is stored because it cannot be
  derived.** 0 is an ordinary message; the rest are things Discord itself
  posted. **4 is CHANNEL_NAME_CHANGE -- a thread rename, carrying the new
  name as its content and whoever did it as its author** -- which is the only
  exact record of a retag there is. Confirmed against the sandbox rather than
  read off the documentation: three renamed threads, each carrying a type 4
  whose content was the new title. 6 is CHANNEL_PINNED_MESSAGE, which is what
  pinning the status message posts.
  **Without it a rename cannot be told from a paste.** 573 of production's
  messages have content that parses as a title and **550 of those were
  written by people**, so the two are the same row. Nothing reads the column
  yet -- the retag figure comes off `thread_titles` and does not need it --
  and it is stored anyway because it can only be captured as it goes past.
  It also makes the record exact where `thread_titles` is merely current: a
  thread renamed twice while the sync was down leaves one title revision and
  two type 4 messages.
  **A re-read fills it in on a row that has none**, which is what makes a
  backfill possible at all. `load_messages` keeps its `INSERT OR IGNORE` and
  follows it with an `UPDATE ... WHERE type IS NULL` -- **only** the type,
  because the mirror is append-only and a name or a timestamp reading
  differently on a second fetch is Discord being mutable rather than us being
  wrong. It is a separate statement rather than an upsert clause because an
  upsert that *updates* still reports `rowcount` 1, and that counter is what
  says a message is new.
  So `rescan_edits` backfills on its own: it re-reads the last
  `RESCAN_TAIL` messages of every thread active within `RESCAN_DAYS`, on
  rotation. **Threads older than that stay NULL until something fetches them
  again** -- which is most of production's 889, and is a deliberate re-read
  pass to decide on rather than something that happens quietly.
  `messages_new` was counted off `con.total_changes` and is counted off the
  cursor's `rowcount` now: the connection's total is cumulative from the
  moment it was opened, so it is truthy after the first write of the process
  and stays that way, and every message seen was counted as one that was new.

## Closures, and who made them

- **A thread archived in Discord closes its card, and Ernie has to go looking
  for it.** The sync loop runs on `/guilds/{id}/threads/active`, and an
  archived thread is simply *not in that list* -- so the row kept whatever
  `archived` it had, the card was never completed, and a ticket somebody
  finished in Discord sat on the board for ever. Measured before the fix: a
  thread archived in Discord, then a full cycle, and `threads.archived` was
  still 0 with no event and no completion.
  **Absence is the question, never the answer.** `reconcile_closures()` takes
  the cards that are open and not in the listing and asks Discord about each
  one -- `GET /channels/{id}` -- and closes only what comes back
  `archived: true`. That ordering is the whole safety of it: a listing short
  for any other reason (a hiccup, a permission change, a channel dropping out
  of `watched`) would otherwise close the entire board in one pass, with an
  event each and every one of them propagated to the other machine.
  It costs **nothing** when nothing has closed -- no card is missing, so no
  request is made -- which is why it rides the fast beat and a closure shows
  up in about five seconds.
- **The time is Discord's, not ours.** `thread_metadata.archive_timestamp` is
  when the work actually finished, and that is what `completed_at` and the
  event's `occurred_at` carry. Stamping `now()` would put the feed line at
  the moment Ernie happened to notice, which on a stack that was off all
  weekend is Monday morning.
- **Who closed it comes from the audit log, and that needs a permission.**
  The thread object does not carry it. `who_archived()` reads
  `GET /guilds/{id}/audit-logs?action_type=111` -- **111, not 112**: 112 is
  THREAD_DELETE, and asking for it returns entries whose changes all read
  `new_value: None`, which looks enough like an archive to be believed. The
  entry carrying `{"key": "archived", "new_value": true}` names the person,
  `global_name` preferred over `username` the way the `started` line does.
  **View Audit Log** is granted on the sandbox bot and has to be granted
  separately on production's -- it is a per-role toggle on each server, and
  the symptom of it missing is not an error but a feed that quietly says
  "closed in Discord" and names nobody, for ever. Same trap as **Pin
  Messages**, which is why that one is written down in four places. Both
  sides are live code: without it the client turns the 403 into `None` and the closure
  is recorded unattributed. Naming somebody is a nicety; closing the ticket
  is the feature, and a revoked permission or a guild busy enough to push the
  archive past `AUDIT_LOOKBACK` must never hold it up.
  **Unattributed is not permanent, which the wording above once implied.**
  Discord keeps audit log entries for **45 days**, so a closure recorded
  before the permission existed can still be given its name afterwards --
  `tools/backfill_closers.py` pages the log and fills `events.actor_name` and
  `cards.completed_by`, only ever onto a NULL, leaving `new_value` alone
  because `CLOSED_IN_DISCORD` says *where* it happened and that was never in
  doubt. Production's first sync is what it was written for: the pass closed
  **20 cards** in one go, every one of them before the toggle was found, and
  all 20 came back two pages in -- 19 JulianD, 1 adubeau. The window is the
  catch. Grant the permission and the backfill on the same day and nothing is
  lost; leave it six weeks and the entries have aged out, with nothing
  anywhere to say what was missed.
- **One audit call per pass, and only when something closed.** It is a
  guild-wide read, so it rides on there being something to attribute -- the
  closures are collected first and the log is asked once for all of them.
  A quiet board still makes no request at all.
- **Our own bot is skipped.** Ernie archives threads when Complete is pressed
  in Bert. That path never reaches here -- the card is already closed, so it
  is not in the query -- but "ernie-test closed it" is the one attribution
  worth making impossible rather than merely unlikely.
- **Unattributed, the feed still reads.** `new_value = "discord"` is what
  Bert phrases the line off, so an unnamed closure says "closed in Discord"
  rather than running the usual fallback, which would have read "Ernie closed
  it" -- certainly wrong, Ernie being the one party that definitely did not.
- **Complete is the word for a work item; close is for a ticket.** Pressing
  the card's button posts `X closed this thread in Bert`, which is this
  sentence with the place swapped -- and the ticket cannot be closed at all
  while work items are outstanding, which `ernie_api.guard_work_done` holds.
  That guard is Bert's alone: archiving in Discord closes the card whatever
  is left on it, because Discord is the source of truth.
- **The thread is told, and only one machine may tell it.** It was silent for
  a long time on the reasoning `started` follows -- it happened in Discord
  already, so saying so there is Ernie telling the room what it just watched
  somebody do. That is true of the *closer*, who was standing there, and not
  of the other people on the thread, for whom the ticket simply stopped
  appearing anywhere. So `dispatch_after` is set and the thread reads
  **"JulianD closed this thread in Discord"**, phrased off `new_value` by
  `ernie_outbox.render` and named off the audit log -- or **"This thread was
  closed in Discord"** where there is no name, because `render` falls back to
  "Someone" and a someone is exactly what is not known here. The same
  distinction Bert's feed line draws.
  **It is immediate rather than held for the undo window**, because undo
  refuses this verb outright and points at reopen: there is nothing to wait
  for.
  **`ANNOUNCE_THREAD_CHANGES` is the switch, and it is off by default**,
  for the
  reason `CHANGELOG_CHANNEL_ID` is: every stack runs its own sync, every
  stack notices the same archived thread, and every stack writes its own
  closure row. Those rows cost nothing while they never post, and tell the
  thread twice the moment two of them do. The state channel does not settle
  it -- `reconcile_closures` skips a card only once `completed_at` is set,
  the pull is on the 60s beat and this runs on the 5s one, so the local close
  wins the race nearly every time. Liftable the way the change log's rule is,
  by claiming the closure through the channel; not done, because one line in
  an env file buys the same thing today.
  **A burst says nothing.** `ANNOUNCE_MAX` is 3, and a pass finding more than
  that closed at once records them all silently and prints why. Telling the
  thread costs an unarchive, a message and a re-archive, so every announcement
  lands in the sidebar of everybody on that thread: the point for one closure,
  noise for twelve. At a 5s beat more than a handful at once is a machine
  catching up rather than one watching -- a stack started after a weekend, a
  channel coming back into `watched` -- and a backlog announcing itself as
  news is the failure `witnessed_start` exists to prevent one table along.
  What is held back is the announcement and never the closure: those cards
  still leave the board.
  **Nothing old is ever sprayed.** A row written with NULL keeps it -- nothing
  rewrites one -- so switching this on affects closures from that moment and
  not the history. A fresh install cannot either: `ernie_load.ensure_card`
  marks an already-archived thread's card completed as `imported` when the
  card is made, so an inherited thread never reaches `reconcile_closures` at
  all.
- **Undo refuses it and points at unarchiving, which is where the
  action is.** Clearing `completed_at` would leave the thread
  archived, so the next pass closes the card again. It used to say
  "reopen it instead" -- and Bert has no reopen for a card that has
  left the board: it never asks for completed cards, so the one
  Reopen it does have, in the edit-conflict dialog, cannot be reached
  for a closed ticket. The advice named something the reader could not
  do from the window they were reading it in. Unarchiving the thread
  is the real route, and it only became one once the reopen guard
  learned to ignore Ernie's own messages. Clearing `completed_at` would
  leave the thread archived, so the next pass closes the card again -- back
  on the board for five seconds and gone, for ever. Reopen posts to the
  thread, and posting to an archived thread unarchives it, so the two agree
  afterwards. Verified end to end in the sandbox: reopen, outbox drain,
  Discord reports `archived: false`, and the next sync leaves the card open.
- **Bert's own Complete is not re-detected**, and not because of a flag about
  who did it: a completed card is not in the query at all, which is guarded
  on `completed_at IS NULL`.

## The status message in the thread

The board knows what a ticket still needs and the thread is where the work is
discussed, and the two only met by somebody opening Bert. `ernie_status.py`
puts what is left where the conversation is: band, what is still to do, what
has been done, when it last moved and who moved it.

- **New threads only, and there is no backfill.** `witnessed_start()` -- the
  same predicate the `started` feed line uses -- asks whether Ernie saw the
  thread appear rather than inheriting it. A first sync makes a card for every
  thread there has ever been, 889 of them in production, and posting into all
  of those is not a thing to do to a channel people are working in.
- **One message, edited in place.** The state channel's design, for the reason
  measured there: an edit recovers from its rate limit in 0.67s and announces
  nothing, so the status can follow every tick without the thread becoming a
  notification feed. `thread_status.body` holds what was last written and a
  pass rewrites **only when the rendering would differ**, or a quiet board
  would edit every message every cycle for nothing.
- **Nothing in it may move on its own.** The time is Discord's `<t:...:R>`
  markup, so the reader's client renders "2 hours ago" and keeps it current
  while the source text stays fixed at the moment the card changed. A written
  out "2h ago" differs on every pass and rewrites the message for ever, which
  is the trap `ernie_state.without_stamp()` exists for.
- **It is an embed, not a message of text**, for the one thing text cannot
  do: the bar down its side carries the band, so a thread says how urgent it is
  before a word of it is read. These threads are already embed-shaped -- the
  ticket bot posts one per build and return -- so it reads as native rather
  than as a bot shouting. To do and Done are `inline` fields, side by side on a
  desktop and stacked on a phone, one item per line: a run of them separated by
  dots stops being a list you can count.
- **The colour is the board's, copied and held to it.** `BAND_COLOUR` is
  bert's `DARK["band_text"]`, because a colour chosen here would put the thread
  and the board out of step -- and this file cannot import bert, which pulls in
  PySide6 where the outbox has no display. `tests/check_status.py` holds the
  two together, the way `check_palette.py` holds the two themes together. A
  closed ticket goes `OK_FG` green, which is what done looks like everywhere
  else. **Needs Attention and Critical share a bar, and that is left alone on
  purpose** -- `band_text` gives both `#F5AAA2`, so five bands produce four
  colours. On the board they are told apart by position and by the header's
  own label; in a thread there is neither, so the two most urgent states do
  look alike. Asked and answered 2026-09-09: fine as it is. Giving Critical a
  colour of its own here would mean a Discord palette that is no longer the
  board's, which is the thing the check above exists to prevent.
- **The time goes in a field, never the footer.** Discord renders `<t:...:R>`
  in a description or a field value and **not** in footer text, so putting it
  there would lose the self-updating clock that keeps the stored body still.
- **`thread_status.body` is the embed serialised with sorted keys**, so "would
  this read differently" survives the dict being built in another order.
- **It carries what the thread is *about*, because most threads say it
  nowhere else.** The Build Request and Return embeds come from
  `Python-Interface-Bot`, and only **230 of 889** threads have one -- 18 of the
  50 open cards have none at all, and of those 18 not one has a parsed
  proposal and exactly one has any equipment recorded. Ernie has never posted
  such an embed and must not start: one that looks like a ticket with no PIP
  key behind it is worse than none. But the facts parsed *out* of the ones
  that exist belong here, because this message is on every thread -- so there
  is one place to look whether or not a ticket was ever raised. Equipment,
  ticket, client CR, assignee, each **only when known**: a thread with none of
  it draws none of those lines, because a format kept at the cost of the truth
  is not worth keeping.
- **The sandbox no longer seeds those embeds, and that is not the same as
  removing them.** `seed_test_server.py` used to imitate Python-Interface-Bot
  so the parser had something real to read; the seeded threads carry only
  Ernie's own status embed now, because Bert shows nothing from a Build
  Request yet and another bot's panel in front of the one message Ernie owns
  is clutter. **`ernie_extract` is untouched** -- production still has them,
  and every rule above about `-- not found --`, `####` and the varying
  "Existing Return ticket(s) (N)" count is still live there. So a sandbox
  status embed draws none of the equipment/ticket/CR/assignee lines, which
  the "only when known" rule already covers, and the parser paths are
  exercised by the checks rather than by the board.
- **The readable form, not the key.** Equipment is `thread_equipment.raw`
  (`EReel-1060`), which is what the job is called out loud, rather than the
  `equipment_master` PIP key the ticket carries; and a client CR is named off
  the roster -- `PIP-7468 (Clinton MS)` -- falling back to the bare key where
  Jira has never run, which is production today. A `####` number is **left
  out**: it is the parser saying it could not read one, which is worth amber
  on a card and is noise repeated in every thread.
- **Not the ticket's name.** That is the thread's own name, shown directly
  above the message in every client. It carries the date and the client, which
  is exactly why repeating it puts the same string on screen twice.
- **It is pinned, and pinning is retried.** "The first message" is what was
  wanted, and a bot cannot be the first message of a thread somebody else
  opened -- a pin is one click from the thread header, which is the same thing
  to a reader. Pinning needs the bot's own **Pin Messages** permission, and
  **not Manage Messages** -- worth writing down, because every symptom points
  at Manage Messages and the bot already had it: bit 13 set in its effective
  permissions, no overwrite on the channel or its category, the thread neither
  archived nor locked, and Discord still answering `50013 Missing Permissions`
  in the thread *and* in the parent channel, on both the old and new pin
  endpoints. It is its own toggle on the role. So pinning is never fatal --
  the status is there either way -- and `thread_status.pinned` records it so
  every later pass tries the ones still outstanding. Attempted only once at
  posting time, granting the permission afterwards would have changed nothing;
  as it went, the grant plus one more pass pinned all five with no reposting
  and no editing, confirmed against Discord's own pin list.
- **A board with no row looks in the thread before it posts.**
  `thread_status` is per database and the thread is not, so every board kept
  its own `message_id` with no way to learn of anybody else's -- and a board
  with no row posted a fresh one. **N boards meant N status embeds in one
  customer thread**, each edited by the board that made it and ignored by the
  rest. `#ernie-state` never had this, because its messages name their own
  `thread_id` and a second board finds and edits the one already there; this
  is that idea one channel along, with the thread as the key.
  **The two-stack setup is the documented one**, so this was waiting for the
  second person to run a stack: two bot embeds in every thread people are
  working in, and in production those are real customer threads. Found on the
  sandbox after a weekend of two databases on one machine -- 10 of 34 threads
  carrying two or three, four belonging to no database that still existed,
  and a burst of notifications every time a board started.
  **The title is the marker and always was.** Every status embed begins
  `STATUS_TITLE`, so `adopt()` works on messages written long before it
  existed -- which is the point, since the ones needing adoption are the ones
  already out there. The **oldest** wins, so two boards adopting
  independently converge rather than taking one duplicate each. It costs one
  GET, only for a thread this board has no row for, and `wanted()` has
  already filtered to threads Ernie *witnessed* -- so a fresh install, which
  inherits everything and witnesses nothing, asks for nothing at all.
- **Every thread gets one, work items or not.** A third of open tickets have
  none, and the trigger being "a card exists" is what puts the message near the
  top of the thread rather than fifty replies down. With nothing to list it
  says *No work items yet*, because a message that stops after the band reads
  as one that failed to load.
- **Priority is shown, which bends a rule on purpose.** Band moves are silent
  in customer threads because posting each nudge is noise -- but an edit
  notifies nobody, so this makes priority *visible* without *announcing* it.
  Checked before doing it: `#customer-threads` is internal, 36 human authors
  with the busiest posting across 645, 655 and 591 of the 889 threads.
- An archived thread is skipped. Discord refuses a post to one, and
  unarchiving to say "closed" would drag a finished ticket back into
  everybody's sidebar.

## Retagging, reconstructed from the titles

- **Retagging is already in the mirror, so nothing new is stored for it.**
  Julian asked how many tickets go from PROD to OPS as they change during a
  ticket's life. The two routes offered were a tag history written into
  `#ernie-state` -- which could only ever count from the day it was switched
  on -- and a trawl of the other bot's log channel. Neither is needed:
  **the tag is the title's prefix, `thread_titles` is append-only, and every
  revision carries the queue the parser read off it**, so a tag change is
  already a row with a time on it. `LAG()` over each thread's revisions in
  `observed_at` order gives the pairs, and the window filters on
  `observed_at` because that is the only date a move has -- not when the
  thread opened, and not when it closed.
  **Cards only.** `#customer-support` is mirrored for history with
  `generate_cards = 0`, so a retitle there is not a ticket changing hands.
  **A rename that keeps the tag is not a move**, which is most renames: the
  client gets corrected, the summary gets sharpened.
  **The rename system messages were measured and rejected.** They are in the
  mirror -- Discord posts one into the thread on every rename -- and they were
  the obvious source. But `messages` does not store Discord's message `type`,
  and **573 of production's messages have content that parses as a title, 550
  of them written by people**, so a rename cannot be told from somebody
  pasting a title into the chat. A figure that invents transitions is worse
  than no figure. **`messages.type` is stored now** so that stops being true
  of anything read from here on -- see the data model note. It changes
  nothing about this figure, which still comes off `thread_titles`; it is
  what a later reconstruction of the history before today would be built on.
  **That limit was lifted, and the way it was lifted is the point.** It said
  this counts only what Ernie was watching for, and production had 889 title
  rows for 889 threads. `tools/backfill_message_types.py` fetched the type on
  every message (908 pages, 7m26s, **703 renames recovered**) and
  `tools/rebuild_title_history.py` wrote each rename as the title revision it
  was: 694 of 696 written, and production's history shows **68 PROD → OPS and
  7 back**, out of 75 (2026-09-15; 61 and 7 of 73 when the reconstruction was
  first run, the rest arriving through the sync since). It moves with the
  window -- 13 at four weeks, 44 at three months, 75 at six, and 75 at a year
  too, because the board is younger than that.
  **Ten to one, one way.** That is the answer to the question this was built
  for: tickets start as production work and become operations work, and
  almost never go back.
  **The reconstruction may not change what the board shows today**, which is
  the rule that makes it safe on a live database: a rename is written only if
  it is strictly older than the thread's earliest existing row, so the newest
  revision is never one the tool invented and every card's queue and client
  come from where they always did. Verified by snapshotting the title, tag,
  client and confidence of all 363 cards and comparing after: **0 changed.**
  Renames dated later are the *sync's* business -- 2 of 696 -- and it will
  record them on its next pass.
  **Only an exact row counts as one we already have**, same thread and same
  moment. Matching on the *name* was tried first and is wrong in a way that
  counts: production's single row per thread carries the name as of its first
  sync, which is usually the name the last rename gave it, so skipping that
  rename as familiar left the revision dated the day Ernie first looked
  instead of the day it happened -- a thread that went OPS in April counting
  as August, inside windows it falls outside, on a panel whose whole point is
  the window. Two rows carrying one name is not noise; they share a tag, so
  having both invents no move.
  **And the guards are asked in the order that explains itself.** The
  already-have question comes before the age question: once a pass has run,
  the oldest row a thread holds is one the tool wrote, so every rename is
  at-or-after it, and asked the other way round a second run reported all 694
  as "newer than what we hold -- the sync's job", which is a sentence about
  rows it had put there itself. Same outcome, different account of it.
  `--dry-run` writes, measures and rolls back, so the preview is the real
  figure rather than a count of intentions.
  **Only PROD ↔ OPS is counted.** Four tags make twelve possible pairs and
  the question was about two, so the two that answered it shared a 244px
  panel with `ENG → PROD` and `CS → OPS` -- a handful of rows each about
  something nobody asked. Measured on the sandbox: eight pairs, two carrying
  the question and six noise, and enough noise to push a real row into the
  summed tail. `ernie_api.TAG_MOVES_COUNTED` is the list.
  **Filtered in the query, never in the drawing.** Counting everything and
  showing two would leave a block whose rows do not make its own total, and a
  figure that does not add up is the first one somebody stops believing --
  the rule `Other` exists for in the tally. There is no `Other` here, because
  what nobody asked about is not counted at all. Nothing is lost either way:
  `thread_titles` is append-only and still holds every rename, so widening
  the list again is one line and no backfill.
  On the panel the block draws `PROD → OPS  21` with a bar in the
  *destination's* colour: the arrow already says which way it went, and the
  tag it became is what a reader is counting. Four tags make twelve possible
  pairs and the panel is 244px wide, so past `STATS_MOVES_SHOWN` the tail is
  **summed into one line rather than dropped** -- the rows have to add up to
  the total, and a figure that does not add up is the first one somebody
  stops believing. It sits straight after the tally, which cannot show a
  ticket that arrived as one tag and left as another.

## The state channel

So two people on two machines share one board without either hosting the
other's API. Priority, rank, work items and completion live in
`#ernie-state` (`STATE_CHANNEL_ID`), one message per card, edited in place.

- **Not the thread title.** Measured in the sandbox: a message edit recovers
  from its rate limit in 0.67s and announces nothing; a thread rename allows
  two per ten minutes, comes back `scope: shared` so a second Ernie gets no
  budget of its own, and posts a system message into the customer thread
  every time. The standard rate-limit headers do not warn about the rename
  limit -- they read healthy right up to the 429.
- **One message per card, not one document.** Two people moving different
  cards edit different messages and never collide, and no card can outgrow
  the 2000-character content cap.
- Each message names its own `thread_id`, so the channel is self-describing
  and an interrupted publish resumes without duplicating.
- Everything above the fence is derived and never parsed back, so editing
  the prose in Discord cannot corrupt the board -- which is what lets it be
  chatty enough to read: band and position, the bubbles with ticked ones
  struck through, and who last touched it.
- One or more extra messages carry a **`**Board**` summary** -- the whole
  running order, in the same band order Bert shows, so the channel can be read
  top to bottom when checking two boards agree. It holds no state and is
  skipped by `parse()`. It **spans as many messages as it needs**: it used to
  be one, dropping rows off the bottom until the rest fit the 2000-character
  cap, so a board past about thirty cards silently stopped showing its lowest
  ranked few. Later pages start with `SUMMARY_MARK` too, so everything that
  finds or clears the summary finds them without being taught to, and only the
  first carries the stamp. Two boards crossing a page boundary in the same
  cycle can both post a last page; the next publish sees one too many and
  deletes it. Its only clock is the **last published** line, kept out of the
  comparison by `without_stamp()` and refreshed on its own heartbeat --
  otherwise it would differ on every publish and rewrite the message every
  cycle for nothing.
- A card message is rewritten when its state changes *or* when the prose
  would read differently, so changing `render()` re-renders the channel
  rather than leaving old cards in the old format forever.
- **The two directions live in different processes**, because
  `ernie_sync.py` is read-only against Discord and `ernie_outbox.py` is the
  only thing that writes there. Sync pulls the channel into SQLite; the
  outbox publishes SQLite into the channel.
- **The push resolves three ways too, not just the pull.** `publish()` leaves
  a card whose channel message no longer matches our stored base: that
  difference is theirs, not ours, and the two directions run in different
  processes on different loops, so a change arriving between our last pull and
  this push is exactly that case. It used to overwrite whatever was there
  whenever it differed from the local row, and record its own stale view as
  agreed -- so the board that made the change pulled it back out a cycle later
  and the change vanished. Seen in the two-machine test as somebody moving a
  card and the other stack undoing it a minute later. A deferred card writes no
  base, because nothing was agreed; `reconcile()` applies theirs, or names the
  conflict in the feed, and the next push goes out from a view that knows.
- **Conflicts are resolved three-way against `state_sync`, never by
  comparing the two machines' clocks.** `state_sync` holds what this machine
  last agreed with the channel about, per card; against that base, the
  channel moved, we moved, both moved, or neither. Both moved means the
  channel wins -- it is the shared copy -- and the losing change is named in
  the feed rather than vanishing. A card with no base adopts the channel, so
  a board joining an existing session takes the shared state.
- Timestamps decide nothing. `cards.updated_at` is this machine's own clock
  and means "when this row changed here"; a remote `at` is only used to file
  the replayed event, clamped to now so a fast laptop can't park its changes
  at the top of the feed. `publish()` checks this machine against Discord's
  clock -- the one clock both share -- and says so past `SKEW_WARN_S`.
- **`state_sync` has two timestamps and only `agreed_at` is about the other
  board.** `synced_at` is the publish, which is this machine pushing its own
  view; it advances every cycle whether or not anything is coming back, so
  measuring contact with it reported "in step" with the sync loop stopped.
  `agreed_at` is written by `reconcile()` alone, on every card it compared
  including the two quiet outcomes, and is what `/health` and Bert's
  indicator report. NULL means published but never yet reconciled, which Bert
  shows as `no contact yet` rather than folding into "in step". The publish
  must never write it. **`no contact` is about this machine, not the other
  one.** `reconcile()` stamps every card it compares against the messages in
  the channel, and those sit in Discord whether or not the other laptop is on,
  so the clock advances on our own cycle regardless of anyone else's activity.
  The only thing that stalls it is our own sync stopping or losing Discord.
  Documentation that read it as "their stack is down" had it backwards, and
  sent the reader to ask the other person about a window on their own machine.
  The summary message says **last published**, not "last checked", for the same
  reason: it is one machine's write time, not an agreement between two.
- A card message's `by` is a person or it is nothing. `render()` falls back to
  the publishing machine's own name for a card nobody here has touched, and
  that name is `ernie`, so the other board filed the replay as "ernie moved
  ..." -- reading as though the software had decided something. The payload
  carries `None` instead, which `apply_card()` already renders as "the other
  board". `by` is outside `state_only()`, so this changes nothing that is
  compared.
- Applying a remote change writes an event with **`dispatch_after` NULL**.
  The machine that made the change already queued its own message; giving
  the replay a dispatch would post the same update twice, once per board.
- A card the channel knows and this machine has no row for is skipped, not
  invented -- its thread simply hasn't synced here yet.
- Completed cards stay in the channel carrying `completed: true`, so closing
  one propagates. Cards closed before the channel ever saw them are not
  backfilled.
- **A payload this build cannot read is an alarm, and a card waiting on its
  thread is not.** Both used to land in one list called `skipped`, and they are
  opposites: a card the channel knows and this machine has no thread for clears
  itself on a later cycle, while a payload whose `v` is not ours means that
  card has stopped being compared *in both directions* and no amount of waiting
  will settle it. Reported together, the one that matters was buried under the
  one that never does. `report["format_skew"]` is now its own list, and
  `note_format_skew()` writes it to `state_format_skew` -- one row, like
  `changelog_state`, because it is one fact about this machine rather than a
  history.
- **The warning takes itself down.** The old messages sit in the channel until
  that machine republishes them, so the row is rewritten every cycle for as
  long as it is true, and a pull that skips nothing **deletes** it. An absent
  row is "no skew seen", which is also the right answer for a database that has
  never pulled, so there is no third state to explain.
- **`/health` reports it even when `sharing` is `None`.** A machine that could
  read none of the channel applied none of it, so nothing wrote a base and it
  has no `state_sync` rows to be "sharing" by -- and that is precisely the
  machine that needs telling. Bert asks about it **before** the guard that
  hides the indicator when nothing is shared, or the one board that most needs
  the message is the one that never sees it. It is red rather than amber
  because it is asking for a person, which is what red means here, and the
  tooltip names *which* machine is behind: `who_is_behind()` is a pure function
  so the wording is testable, and it still produces a sentence for a `v` that
  is not a number.
- **`skew` in `ernie_state.py` means the clock**, and has since the preflight
  was written -- `skew_seconds()`, `clock_skew_s`, `SKEW_WARN_S`. The wire
  format one is `format_skew` throughout for that reason.

## The change log

A durable record of every change, in its own channel, for looking back at
rather than reading as it goes. Customer threads only hear the handful of
changes worth interrupting somebody for; this gets all of them.

- **Inert unless `CHANGELOG_CHANNEL_ID` is set, and only one machine should
  set it.** Both boards hold the whole history -- their own changes and
  replays of the other's -- so two loggers write every line twice.
  **That is an accident of identity rather than a limit**, and
  `plans/changelog-per-machine.md` is how to lift it: a replay is written
  with a fresh uuid, so one real change is two unrelated rows and neither
  machine can tell it is looking at a copy. Mark the replay and every machine
  can log its own changes, once each, with nobody's uptime mattering to
  anybody else. Not started -- the undo case wants settling first, because a
  line struck through on one machine is a message another machine posted.
  **The channel is not called the same thing on both servers**: `#change-log`
  in the sandbox, **`#ernie-logs`** in production. The name is nowhere in the
  code -- the env carries an id -- so the only cost of the difference is a
  reader going to look for a channel that is not there.
  **It is uncommented in the installed env and left commented in the copy
  that gets handed out**, which is the one-machine rule expressed as a file:
  the master at `C:/Users/Edge/ernie/ernie.env` is what a second person would
  be given, and a line they never thought about is exactly how two loggers
  happen.
  **Switch it on before the writes, not after.** `catch_up()` marks
  everything not yet sent as logged on the logger's *first* run, so whatever
  has piled up by then is swallowed rather than posted -- which is right for
  a back catalogue and wrong for the week somebody meant to record. Turned on
  while production was still read-only, the swallow was 21 events: the 20
  closures the first backfill found and one `started`, all of which happened
  in Discord rather than on the board.
- SQLite -> Discord, so it rides with the outbox like the state publish does.
- Nothing is logged until it has **settled**: an event inside its undo window
  may still be cancelled, and a record that says things that never happened
  is worse than no record. A silent change -- every band move that isn't in
  or out of `critical`, and every reorder -- has no `dispatch_after`, which
  is not the same as being finished; it gets the same `UNDO_WINDOW_S`,
  measured from `occurred_at`. Reading NULL as settled logged the bulk of the
  board's activity instantly, ahead of the changes that do wait.
- An undone change is logged struck through, because that it was made and
  taken back is itself part of the record. Undo has no deadline, so waiting
  out the window is not enough on its own: a line already posted and undone
  later is **edited in place**, off `changelog_sent.sent_at` against
  `events.undone_at`. The edit re-stamps `sent_at`, which is what stops it
  being struck twice.
- `changelog_sent` tracks it per event rather than by a high-water mark: a
  change replayed from the other board carries the timestamp it originally
  happened at, so events do not arrive in `occurred_at` order and a cursor
  would step straight over them.
- The history already in the database is marked as logged **only on the very
  first run**, so switching the log on doesn't replay months into a channel
  nobody has read. Doing that on every start would silently swallow whatever
  happened while the logger was down. `--backfill` asks for the history.
- Nothing reads it back. Deleting the channel and unsetting the variable
  leaves nothing behind but a table nothing looks at.

## How fast Discord reaches the board

Two hops: `ernie_sync` pulls Discord into SQLite, and Bert polls the API every
`POLL_MS` (5s). The sync used to be one 60-second cycle, so a new ticket was
up to **65 seconds** old before it appeared. It is **two beats** now, and a
new ticket is on the board in **0.3-10s, about 5 on average**.

- **The split is by cost, and the two halves are nothing alike.** Measured
  against the sandbox, a whole cycle is **13 GETs and ~4s** -- and **11 of
  those GETs and 3.5s of that time are `rescan_edits`**, which found nothing
  in either sampled cycle, because an edit to an old message is rare. What a
  new ticket actually arrives through is the thread listing: **1 GET, 0.30s**,
  and `sync_messages` costs **nothing at all** on a quiet board, because the
  listing already carries Discord's `last_message_id` and a thread that has
  not moved needs no request.
- **Discord agrees the cheap half is free to ask more often.** Read off the
  headers: the listing route answered `x-ratelimit-remaining: 999/1000` on
  eight back-to-back calls, so it is effectively unmetered. The route the
  rescan and the state pull use, `/channels/{id}/messages`, is **5 per ~5s**
  and 429s on the sixth -- which is exactly why those stay on the slow beat.
  Measured over a real minute of the loop: **37 GETs, 0.60/s**, against a
  global ceiling of 50/s.
- **`--fast` is the listing beat (5s); `--interval` is the rescan and the
  roster (60s); `--state-every` is the state-channel pull (20s).** A fast pass
  is `cycle(..., full=False)`.
  The pull began on `--interval`, with the rescan, because they share the
  route. That grouped them by the wrong property: the budget is spent in
  requests, and **the rescan is a dozen a pass while the pull is one** -- the
  channel's 59 messages fit in a single page of 100. Two machines at 20s spend
  6 pulls a minute on that route against the two rescans' ~24, so it sits
  about where it already sat.
  What bought the move is that this is the **receiving** half of a shared
  board. Measured from the constants, a card moved on one laptop reached the
  other in ~50s average and 105s worst: 15s average waiting for the publish
  beat, a second or two writing, **30s average waiting for this pull**, and
  2.5s for Bert's poll. It was the biggest single term and the cheapest one to
  shorten.
  The send side is deliberately untouched. A publish pass is already
  `PUBLISH_MAX` 10 x `WRITE_PACE` 1.1 = 11s of writing; at a 15s beat two
  machines would be writing into one channel more than half the time, which is
  exactly the collapse `WRITE_PACE` was added to fix.
  The release note stayed on the slow beat: it is a second request, and a
  pinned note naming the current build changes about never.
- **The local recompute is not the constraint.** `rebuild_derived` re-reads
  every message of every active thread and re-extracts on *every* pass, which
  sounds like the thing that would break -- measured at production's size, 50
  threads and 1348 messages, it is **0.02s**.
- **The sleep is measured from the top of the pass, not the end.** A full pass
  takes about four seconds; sleeping five after it is a nine-second beat that
  lurches once a minute. Measured after: a pass starts every 5.0s exactly.
  `next_full` is a wall clock for the same reason, rather than a count of fast
  passes.
- **A quiet fast pass writes no log line.** Twelve times the passes would
  otherwise be twelve times the log, and eleven of every twelve lines would
  say nothing happened -- burying the ones somebody opened the file to find.
- **`sync_runs` is trimmed, because the fast pass has to write to it.**
  `/health` reads the newest *finished* row and Bert draws it as "synced 20s
  ago", so a pass that recorded nothing would have the board reporting a
  staleness it does not have. Twelve times the rows and nothing ever deleting
  them; `prune_runs()` keeps `SYNC_RUNS_KEPT` and runs on the full beat, so it
  is one statement a minute.
- **Bert's own poll is the other 5 seconds** and was left alone. Halving it
  would halve the average again, at twice the API load for a board that is
  usually idle -- worth doing only if 5s stops feeling immediate.
