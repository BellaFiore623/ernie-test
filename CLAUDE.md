# Ernie + Bert

Internal tooling for tracking equipment tickets. Discord threads are the
single source of truth. Ernie mirrors them into SQLite and posts updates
back; Bert is a desktop board on top of Ernie's HTTP API.

## Files

| File | Role |
|---|---|
| `ernie_sync.py` | Discord → SQLite. Read-only against Discord. Runs as a loop. |
| `ernie_extract.py` | Parsing. Pure functions over dicts, no I/O, no network. |
| `ernie_load.py` | Writes extracted records into SQLite. Idempotent. |
| `ernie_api.py` | FastAPI over the database. Everything Bert talks to. |
| `ernie_outbox.py` | The **only** thing that posts to Discord. |
| `bert.py` | PySide6 client. |
| `schema.sql` | Applied on every `connect()`. All `CREATE ... IF NOT EXISTS`. |
| `seed_test_server.py` | Builds realistic test threads. Test guild only. |
| `wipe_test.py` | Deletes all threads in the test channel. Test guild only. |
| `ernie_state.py` | Board state in Discord: one message per card in `#ernie-state`. |
| `ernie_changelog.py` | Every change, appended to `#change-log`. Off unless configured. |
| `ernie_jira.py` | Customer list, Jira → SQLite. Read-only against Jira. Off unless configured. |
| `run.sh` | Starts the whole stack. `./run.sh test bert` |
| `bert.cmd` | Double-clickable launcher for a tester who runs only Bert. |
| `stack.cmd` | Double-clickable launcher for a tester who runs their own stack. |
| `tools/q.py` | Ad-hoc SQL helper. `python tools/q.py "SELECT ..." ernie-test.db` |
| `tools/ernie_backup.py` | Online SQLite backup with rotation. |
| `tools/dump_threads.py` | Raw API JSON to disk. Read-only, for seeing what Discord actually sent. |
| `tools/bashrc-snippet.sh` | Optional shell shortcuts. Nothing depends on it. |
| `assets/bert_logo.png` | Bert's mark. `bert.py` resolves it relative to itself. |
| `requirements.txt` | httpx, fastapi, uvicorn, pydantic; PySide6 for Bert only. |
| `ernie-test.env.example` | The env file's shape, with no values. Copied, not edited. |
| `migrations/` | One-off scripts already applied everywhere. Kept as a record; a fresh database never runs them. |
| `tests/` | `python tests/run.py`. Standard library, no network, no database of yours -- the fixture builds one from `schema.sql` in a temp directory. |
| `README.md` | For somebody arriving at the repository. What it is, how to run it, why it is shaped this way. |
| `TESTING.md` | Hand this to the tester. Both setups, start to finish. |

## Hard rules

- **Never test against production.** Always `--env ernie-test.env --db ernie-test.db`.
- **All Discord writes go through `Discord.write()`.** The guild guard lives
  there so no code path can skip it. Never add a direct `http.post` or
  `http.patch` to Discord anywhere else.
- `ALLOW_DISCORD_WRITES` must exactly equal `DISCORD_GUILD_ID` or nothing
  posts. Production's env file does not contain the line at all.
- **`if __name__ == "__main__":` stays at the very end of every file.**
  `uvicorn.run()` blocks, so anything appended below it never registers.
  This has already caused a "route not found" bug once.
- **Keep sync transactions short.** `ernie_sync` commits per thread. Bert
  writes to the same database, and a long transaction causes
  `database is locked`.
- **Never hard-delete from the mirror.** Discord is mutable, so
  `thread_titles` and `message_revisions` are append-only and deletions set
  `deleted_at`. Bert's own state (`cards`, `events`) is never overwritten by
  a re-sync.
- **Compare timestamps with `datetime()` on both sides in SQL.** Python
  writes ISO8601 with a `T`; SQLite's `datetime('now')` uses a space. Raw
  string comparison is always false. This silently broke the outbox once.
- Secrets live in `ernie.env` / `ernie-test.env`, both gitignored. Never put
  a token in a `.py` file.

## Data model notes

- Key everything on `thread_id`. Titles change (PROD ↔ OPS renames are
  normal), so nothing may be keyed on parsed title fields.
- Priority bands: `unassigned`, `critical`, `high`, `medium`, `low`. New
  threads land in `unassigned`; a human drags them out. Ties within a band
  are broken by a shared fractional `rank`.
- **`rank` is the order, and it is the only one.** A thread nobody can read
  (`ex.UNREADABLE_CONFIDENCE`) is ranked to the *top* of unassigned by
  `ensure_card`, not the bottom, because it needs a person soonest. Bert used
  to arrange that at draw time instead: the card sat first on screen and
  twelfth in the state channel, and a drop between two visible cards was
  measured against neighbours that were not its neighbours. Nothing may sort
  a band by anything but `rank` -- if a card belongs somewhere, give it the
  rank that puts it there. Dragging one down leaves it down; a later rename
  to something unreadable does not haul it back up.
- Work is a list, not a field: `work_items`, one row per bubble on the card.
  **The card shows only what is left; the editor keeps the finished ones.**
  `/cards` sends them all, flagged -- it used to filter on `done_at IS NULL`,
  so a ticked bubble was never sent at all and there was no way to reach it.
  The card filters them off its face, because a card is a list of what still
  needs doing. In the editor a finished bubble is unfilled with a dashed green
  border, and a **double-click puts it back to outstanding**: double rather
  than single, because a stray click must not undo finished work, and the
  second click is the confirmation -- a dialog would tax every tick to guard
  against the rare wrong one. It rides in the batched save as `work_undone`,
  so Cancel takes it back like anything else typed there, and `touched` counts
  it or the row is updated and the function returns before the commit. The ✕
  works on a finished bubble too: removing says it should not be on the card
  at all, which is as true of something ticked off as of something
  outstanding. Both greens are `T.OK_BG`/`T.OK_FG`, which the palette already
  had.
  Rows are never deleted — a tick in view mode sets `done_at`, an ✕ in the
  editor sets `removed_at`, and undo needs both rows still there. The editor
  sends `work_add` / `work_remove`, not the whole list, so two people adding
  different items merge instead of colliding.
- **A retired queue is parsed, never offered.** `QUEUES` is every prefix a
  title may legitimately start with and drives `_PREFIX`, so nothing may be
  taken out of it because it fell out of use: the titles already in the mirror
  would stop matching, fall to `UNREADABLE_CONFIDENCE`, and be ranked to the
  *top* of unassigned as cards nobody can read. `QUEUES_OFFERED` is what Bert's
  editor offers, and `T.QUEUE` must hold exactly those -- the filter checkboxes
  are built by walking the palette, so a tag with no colour also has no filter.
  `tests/check_palette.py` holds the two together. DATA was retired 2026-09
  with one thread in the sandbox and one in production still carrying it; the
  editor keeps a retired tag on a card that already has it rather than
  silently clearing it on the next save.
- `cards.action_item`, `build_state`, `return_state` and `direction` are
  retired — work items replaced all four. Nothing shows or edits them; they
  stay only so undo can reach an old `edited` event.
- Ticket embeds carry no priority signal — `Priority` is always "High" and
  `Labels` always "Operations" in real data. Priority is set by hand in Bert.
- `-- not found --` in an embed and `####` in an equipment ID are *pending*
  states, not errors. Amber, not red. Red is for genuinely unreadable titles.
- Ticket `Existing Return ticket(s) referenced in this thread (N)` has a
  varying count in the field name — match by prefix, not exact name.
- `#customer-threads` generates cards. `#customer-support` is mirrored for
  history only (`generate_cards = 0`).
- Archived means done: a keepalive bot pings live threads every three days,
  so nothing goes quiet by accident.

## Writes and undo

Every change writes one row to `events`. That single table backs the
activity feed, undo, and the outbox.

- `dispatch_after` = when Ernie may post. `NULL` means never post.
- Priority moves are silent except in or out of `critical`, which posts. Every
  other band change, and every reorder within a band, is board housekeeping:
  posting each nudge between high and medium is noise in a customer thread.
- A **`reordered`** event carries the band and the card's position in it,
  before and after, as `band:position` in `old_value` / `new_value` --
  `high:5` -> `high:3`, which reads as "High 5th -> 3rd". Not the rank: that is
  a fraction and "1000 -> 1500" tells a reader only that something moved. Both
  values carry the band, though a reorder never leaves one, so either is
  readable on its own; `ex.reorder_spot()` parses them and returns a bare
  position for the rows written before the band was recorded. The
  positions have to be worked out where the move happens, against the ranks it
  is ordered among, because afterwards those ranks have moved on and it is no
  longer derivable. Rows written before this carry nothing and are shown
  without the places rather than being given invented ones. **A drag that lands
  a card back where it started writes no event at all** -- the rank is still
  saved so both boards agree, but four identical lines for a card that never
  went anywhere is the feed reporting the dragging rather than the outcome.
- Undo inside the window deletes nothing from Discord because nothing was
  ever sent. Undo after it posts a correction message instead.
- Edits are **batched**: saving four fields writes one event and posts one
  message. Do not split this into per-field events. The feed still says which
  it was: `old_value` is the previous value of everything that moved plus a
  `__work__` entry naming the bubbles added and removed, so Bert reads the
  shape off it -- bubbles only, fields only, or both -- and `new_value` is the
  prose it shows. A row whose `old_value` won't parse falls back to "edited"
  rather than being dropped.
- **A feed row opens only when clipping actually hid something.** The line is
  rendered twice, clipped and whole, and the two being different is the test --
  no layout measuring, and no affordance on the rows that already say
  everything. Which rows are open is held on the window by `event_id`, not on
  the widgets: `_render_feed` throws every row away and builds it again on each
  poll, so a row opened to read would shut again within five seconds.
- **The running order is on a splitter too**, along the other axis, and the
  same rules apply: `RAIL_MIN_W`/`RAIL_MAX_W` rather than a fixed width,
  `set_folded()` fixes it because folding is the button's business, and
  unfolding clears `_rail_sized` so the width comes back rather than whatever
  the fold left. Kept in `settings.rail_width`. There is slack to take: the
  rail plus a full-width board is 1040px, so on anything wider the rail grows
  into empty space rather than out of the board. Both lines in a row are cut
  with `QFontMetrics.elidedText` against the font they draw in, not at a
  character count -- a count does not follow a rail that can be dragged, and
  characters-per-pixel is a guess about a proportional font. The measured width
  is part of `Rail.set_cards`' signature, so a drag counts as a change and the
  rows rebuild; the rebuild is held behind `RAIL_REDRAW_MS` rather than run on
  every pixel of the drag.
- **The board and the feed are the two halves of a `QSplitter`**, so the
  height between them can be traded off -- some days the history is the thing
  being read. Neither half may carry a fixed height, or the handle has nothing
  to move: `_fit_feed()` sets a *minimum* on the panel and leaves the height to
  the splitter, and fixes it only when folded, which the caret owns rather than
  the handle. The handle is placed once per unfold, from `settings.feed_height`
  or the row-count default -- re-applying it on every poll would drag it back
  out from under whoever was moving it. Rows are still held to one height;
  that is a different question and the thing that stops an undo shifting the
  list.
- **The place in a list is a card, not a scrollbar number.** `render()` tears
  every card down and builds it again whenever the data changes, so both
  scrolling lists have to be put back afterwards -- and the number alone is not
  where you were. Every pixel above the view belongs to some other card and any
  of it can change between rebuilds: measured, sixteen cards above the view
  each gaining four bubbles left the scrollbar reading exactly the number it
  had before with a different card under the cursor. `_hold_scroll` notes the
  card covering the top of the view by `thread_id` and puts *it* back at the
  same height; the number is the fallback for when that card has gone. It
  walks the band and rail layouts rather than `findChildren`, which answers in
  the order Qt happens to hold the widgets and not the order they are drawn.
  Hitting Edit does not itself rebuild the board -- a card builds its own
  editor -- so the jump somebody sees there is a poll, or the other editor's
  save, landing on the same click.
- **Nothing touches a scrollbar until the geometry has stopped moving.** A
  rebuild posts its layout requests rather than doing the work there and then,
  so the first reading after one is the *old* geometry: measured, the board
  reported its old maximum on the first pass and was 144px out on the next.
  The correction is worked out from where the anchor landed, so a pass run
  against a layout still settling computes the wrong one -- and correcting on
  every pass until the answer stops changing means each wrong one is applied
  and then taken back, which is a visible glitch rather than a held place.
  **A resize is where that shows**, because `resizeEvent` rebuilds the board
  through the same timer the rail handle uses: the bar went +496px and returned
  75ms later, twice for every drag of the window edge. So `_hold_scroll`
  *waits* instead -- it reads the anchor's offset inside the scrolled widget,
  which does not move when the bar does and so can be read without being
  disturbed by its own correction, and only once that reading repeats does it
  move each bar, exactly once. Bounded, so a layout that never settles cannot
  loop. Measured after: 30 rebuilds held to the pixel with at most one bar
  move each, and five resizes across both axes moved no bar at all.

- **A feed row stops growing at `FEED_ROW_MAX_W`.** The status chip and Undo
  are right-aligned in fixed columns, which is what makes them a column you can
  run down and click -- but unbounded, a full-screen board put them a thousand
  pixels from the line they belong to and it stopped being clear which button
  went with which entry. The cap goes on the panel the rows fill, **not on the
  rows**: a row capped on its own takes the width of its own text, so the
  buttons land at a different x on every line (measured at 761 and 845 for two
  rows of one feed). It goes on the **scroll area** specifically, so its
  scrollbar comes to the cap with it and closes the feed off, rather than
  sitting out at the window edge with a field of nothing between it and the
  last button. It is added with no alignment flag: aligning a scroll area makes
  it take its own `sizeHint`, which is small -- 432px, cap or no cap.
  `_feed_scale()` measures against the capped width too, or the line would be
  sized for room the row is never given.
- **Every column in a feed row is centred, and the row keeps `FEED_ROW_PAD`
  above and below.** The Undo button only ever looked centred: it is taller
  than a line of text, so it filled a row that a top-aligned 16px label sat
  high in, and the time, the text and the status chip all rode above it.
  Centring them puts the five on one line. The padding is what stops the
  button touching the hairline under the row -- without it the button is
  exactly as tall as the row, so the rule that ties a line to its buttons was
  resting on one. It costs density: rows went from 29px to 38px.
- **Two pixels decide whether a row looks centred, and both are the rule's.**
  `feed_lay` has **no spacing**: a layout gap falls above a row's contents but
  below the rule above them, so it counts entirely against the top -- measured
  14px above the text and 10 below in a band meant to be even. The rule is the
  separator now, so the gap was a second one throwing off the first. And the
  bottom padding is `FEED_ROW_PAD + 1`, because the hairline is drawn in the
  row's own last pixel: pad both sides equally and everything centres against
  a box a pixel shorter than it looks. With both: text 11px above and 11
  below, rule to rule.
- **Each feed row carries a hairline under it**, in `T.LINE`, so an entry and
  its buttons read as one row. `FEED_ROW_MAX_W` caps how far apart the two
  ends can get; the rule closes the rest, and it was still not obvious which
  Undo went with which line without it. Every row has one, not only the ones
  that open -- a rule on some entries and not others groups them wrongly.
  `FeedRow.paintEvent` draws it rather than the layout holding a separator
  widget: `_fit_feed` walks every widget in `feed_lay`, measures it, and holds
  it to a row height, so a separator would have to be excluded from all of
  that and from every count the panel height is worked out from. A line costs
  nothing in that accounting.
- **In a feed row the text is what gives way, never the controls.** An
  unwrapped `QLabel` cannot be made narrower than its text, so the row's
  minimum width was the whole line plus every fixed column -- 1644px inside a
  1000px window, measured -- and `feed_scroll` has its horizontal scrollbar
  off, so everything past the edge was cut away in silence. Undo is the last
  column, so Undo is what disappeared. The label is `QSizePolicy.Ignored`
  across, which lets it be squeezed to nothing before any control is touched,
  and the chevron sits in its own column rather than on the end of the line it
  would be clipped with. The window's minimum width is its opening width: below
  that the fixed columns crowd the line out and the bands are too tight to drop
  into.
- **A closed feed row never word-wraps.** `_fit_feed` takes the height every
  row is held to from the closed rows' `sizeHint()`, and a wrapped `QLabel`
  reports its hint at a heuristic width of its own rather than the width the
  layout will give it -- 112px against 14 for the same line. Wrapping them all
  put that into `_feed_row_h`, the panel grew to fit eight-line rows, and since
  that height was a running maximum it could only ever get worse: clicking
  anything made the feed swallow the window. An open row wraps and takes its
  height from the label's `heightForWidth`, because `sizeHint()` under-reports
  the other way there and clips the text the row was opened to show.
- **Nothing in the feed moves vertically unless the text needs the room.** The
  height rows are held to is the tallest of them -- a row carrying an Undo
  button, 20px against 12 for the text alone -- and it is held steady rather
  than re-measured, or it would drop the moment the last undoable row aged out
  and slide the list up while somebody was reading it. That same height is the
  *floor* for an open row: released to its natural size, a row with no Undo
  button collapsed to the smaller one, so opening a line to read four more
  characters pulled everything below it upward. Opening a row now either
  changes nothing or adds exactly the lines the text needs. Holding the height
  steady is only safe because closed rows never wrap, which is the invariant
  above.
- **An open row keeps the room a closed one leaves.** A closed row is
  `_feed_row_h` tall around a single line -- the height comes from the taller
  Undo column beside it, and the label is top-aligned, so there is space under
  the words. An open row set to exactly what its label needs has none of it,
  and its last line sat 13px nearer the row below than every other line did:
  measured on a real feed, 16px under a closed row and 3px under the open one.
  It read as the row squeezing into the gap rather than the list making space
  for it. The slack is measured off the closed rows rather than a font metric,
  because the height being matched is whatever the tallest of them wanted.
- **The clip scales with the window, capped at half of it.** A closed row is
  cut to 46 characters of thread and 44 of detail at the narrowest, and those
  widths are scaled up together by `_feed_scale()` so a full-screen board is
  not clipping lines with half the row still empty. Half the window, not the
  space available: a line run the full width of a wide screen is further than
  the eye tracks, and the whole of it is one click away. It measures
  `FEED_FONT_PX`, the size the row actually draws at -- `self.fontMetrics()` is
  the window's font, reports a wider character, and cancels the calculation
  out so a wide board clips at the narrow width anyway. The scale never falls
  below 1, so a narrow board reads exactly as it did.
- Writes take an idempotency `key`; retries return the original result.
- **One editor at a time, and the second click offers to finish the first.**
  `editor_is_busy()` used to say no and stop, leaving somebody to find the
  other card themselves. It now offers Save / Discard / Keep editing, with
  *keep editing* as the default because it is the one that loses nothing. An
  editor with nothing typed in it is closed without asking: `Card.is_dirty()`
  compares exactly what `save()` would send against `_edit_base`, so clicking
  Edit on the wrong ticket and moving on costs no dialog. Every button names
  both tickets -- "this ticket" is the one phrase that cannot be used here,
  because the ticket being closed is not the one just clicked.
- **Bert's close warning owes two different debts, and must count both.**
  `queued` is events waiting out their undo window before Ernie posts them to
  the customer thread; `sharing.waiting_to_send` is cards that have moved since
  the shared board was last published. A reorder, and every band move that is
  not in or out of `critical`, is silent -- no `dispatch_after` at all -- so it
  appears only in the second. Counting the first alone meant reordering the
  board and closing straight away asked nothing, and the running order never
  left the machine. `Bert._owed()` reads both.
- **The unsent mark is drawn in the ink, not the accent.** Measured against
  every card fill in both palettes: the accent averages 4.6:1 in light and
  5.9:1 in dark, the ink 13.7:1 and 11.8:1. It is also the cheaper choice --
  a card already wears its tag's colour, and a mark spending none leaves
  colour meaning something, which is the same rule the band bars in the
  running order follow. The glyph is what says which state it is, so nothing
  is lost: `*` is still waiting, `!` is given up, and `!` keeps amber because
  a caution is the one thing on the card that is genuinely a warning.
- **`/health` counts as owed only what the outbox will still try.**
  `OUTBOX_MAX_ATTEMPTS` matches `ernie_outbox.MAX_ATTEMPTS` and the
  `attempts < 5` in `v_outbox_due`; without it a row nothing would ever pick up
  again was reported as pending for ever, so Bert warned about unsent changes
  on a board nobody had touched for ten minutes -- and the warning's advice,
  leave the stack running another minute, was the one thing that could not
  help. Given up is not hidden, it is reported separately as `stuck`.
- A thread opening in Discord writes a **`started`** event, so the feed can
  say where a card came from -- the one thing on it that nobody did in Bert.
  `dispatch_after` is NULL, because it happened in Discord already, and undo
  refuses it in both Bert and the API: there is nothing here to take back.
  The name is `threads.owner_id` resolved against `messages`, preferring
  Discord's `global_name` ("Tyler") to the username ("tyler_mazza"). A thread
  a bot opened gets no line -- the seeder makes two dozen at a time. Neither
  does one Ernie merely inherited: only a thread created within
  `WITNESSED_WITHIN_S` of being first seen counts, because a first sync makes
  a card for every thread there has ever been, and claiming to have watched
  those start would write a line per thread into the feed and the change log.

## Starting a ticket from the board

A `+ New Ticket` on every band header, because the band is the answer to
"where does this go" and pressing the one you mean has already given it. Needs
Attention keeps one too: every thread opened in Discord lands there anyway, so
a ticket with no home yet is the ordinary case rather than an exception.

- **A card cannot exist without a thread**, and Bert cannot make one -- every
  write to Discord goes through `Discord.write()`, which lives in the outbox.
  So `POST /tickets` records what to make in `new_threads`, and the board
  shows it wearing the **unsent mark** until the outbox has made it. A ticket
  that exists here and not yet in Discord is exactly a change that has not
  left this machine, which is what that mark already says.
- **The outbox writes the mirror rows itself** rather than leaving them to the
  sync. Waiting would put the card on the board a cycle later and in
  `unassigned`, losing the band somebody chose by pressing the `+` in it. The
  sync reconciles both on its next pass; they are written the way it writes
  them.
- **Ernie opens the thread and then says whose it is.** There is no map from a
  Bert install to a Discord account, so the bot is the author and the name
  from `settings` goes in as plain text -- the same way every other name this
  posts does. The optional first message follows it.
- **A folded band opens for the ticket started in it.** Folding hides a band's
  *panel* and keeps its header -- which is what lets the count and the drop
  target go on working, and is also the trap, because `+ New Ticket` sits on
  that header. Pressing it on a folded band put the card and its editor into
  the hidden panel, and `editing_card` holds every poll off while an editor is
  open, so the board sat frozen with nothing on it to say why: five bands
  tried, five editors opened, none of them visible. Three paths put something
  into a band -- starting a ticket, `reveal()`, and a drag arriving -- and all
  three have to open a folded one. `tests/check_board_order.py` holds them
  together.
- **A ticket with no thread has not left the board.** Every poll checks that
  the card under an open editor is still in the payload and warns if it has
  gone -- and `NEW_TICKET` is never in the payload, because the thread does not
  exist yet. So pressing `+ New Ticket` raised "this ticket has left the board
  ... saving will probably fail" within a poll, over a blank form, and again on
  every poll after it. `_flag_edited_underneath` exempts the sentinel and
  nothing else: a real card that has genuinely gone still says so, which is
  the half of it worth keeping.
- **Starting one is not editing one, and the words follow.** `NEW_TICKET` is
  the sentinel a ticket with no thread stands under. It goes through the same
  one-editor rule, but the dialog offers *Create it* rather than *Save*, says
  *Keep writing* rather than *Keep editing*, and says plainly that discarding
  loses the whole thing -- there is no card behind it to go back to. Closing
  the editor throws the placeholder away, and a blank template is not a draft:
  `is_dirty()` compares against the template, so pressing `+` and changing
  your mind costs no dialog.

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

## The change log

A durable record of every change, in its own channel, for looking back at
rather than reading as it goes. Customer threads only hear the handful of
changes worth interrupting somebody for; this gets all of them.

- **Inert unless `CHANGELOG_CHANNEL_ID` is set, and only one machine should
  set it.** Both boards hold the whole history -- their own changes and
  replays of the other's -- so two loggers write every line twice.
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

## The customer list

Client names were typed into thread titles by hand, and the board grew **120
distinct spellings of 43 customers** -- five ways of writing Inspect.AI, two of
RavanAir, one title where the apostrophe in Duke's arrived as a replacement
character. `ernie_jira.py` pulls the real list off the Client CR issues in Jira
so the name is *picked* in Bert instead of typed.

- **Inert unless `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_TOKEN` and
  `JIRA_CLIENT_JQL` are all set.** Read-only against Jira: the only POST is the
  search endpoint, which is a read carrying a body. Nothing here creates or
  edits an issue. It runs inside `ernie_sync`'s loop for the same reason the
  state-channel pull does -- that loop is the one that reads into SQLite -- on
  its own hourly heartbeat, because the roster changes about never.
- **The query is `parent = PIP-2132`** -- the *Client Tracker* issue. Its 105
  children are the customer list; `project = PIP AND issuetype = "Customer
  Requirement"` returns 129, and the extra 24 are product capabilities (*Video
  Capture Capability*, *Tether Reel*), trade shows, and rejected junk (*invalid
  ticket*, *DUPE*) that carry the type without being customers. Verified 105/105
  against the list and 43/43 against the board.
- **`--check` fails if the JQL misses a client, and judges a miss by what it
  is.** A key it did not return is only a finding when that key is a Customer
  Requirement; the sandbox's seeded threads carry CR keys that are real issues
  of the wrong kind -- `PIP-4902` is a Build Request, `PIP-4940` a Bug -- and no
  query could or should reach those. Testing mere existence called all seven a
  failure and advised widening a query that was already right.
- **`clients.short_name` is what a title calls them; `name` is the Jira
  summary.** The summary carries the account note as well as the customer --
  `IPI : El Paso`, `SCI Infrastructure LLC. **PURCHASE** (Should Have 3
  Bots!)`, `GFT - *PURCHASE* (ST Client)` -- and none of that belongs in a
  thread title. `short_name()` drops anything parenthesised (looping, because
  Jira nests them), cuts at the colon, and strips `*STARRED*` notes and a
  trailing `- note`. It deliberately does **not** reuse `normalise_client`'s
  annotation list, which strips `purchase|loaner|rental|demo` wherever they
  appear and turns `Edge AI Demo Team` into `Edge AI Team`. The derived value
  is a seed, and `sync_clients` tells a seed from a correction by re-deriving
  from the *previous* summary: if what is stored is exactly what that summary
  would have produced, nobody has touched it and it may follow a rename.
  `COALESCE(clients.short_name, ...)` could not tell them apart -- short_name
  is filled on the first insert, so it is never NULL again and was therefore
  never updated: renaming a client in Jira left the dropdown on the old name
  for ever.
- **A client the query stops returning is retired, not deleted.** `offered`
  goes to 0, so the cards already carrying it keep their name and it comes
  back if the query finds it again -- the same rule as an `*INACTIVE*` one.
  **A pull that returns nothing retires nobody**: an empty result is Jira
  being unreachable, not an empty roster, and quietly retiring all 65 clients
  is not a thing to do on a failed request.
- **Freshness is reported only when it has stopped.** `/health` carries a
  `clients` block -- count, offered, and how long since the pull -- and it is
  `None` on a machine with no Jira, so nothing shows there. Bert says nothing
  until `ROSTER_STALE_S` (six hours, against an hourly pull), because the list
  changes rarely and an indicator that is always on is furniture. The failure
  it exists for is otherwise silent: `ernie_sync` catches it, writes a line to
  `logs/sync.log` and carries on, and the dropdown goes on offering whatever
  it last knew.
- **`offered = 0` is not deletion.** A summary marked `*INACTIVE*`,
  `*PENDING*` or `*PAUSED*` drops off the dropdown and keeps naming the cards
  that already carry it -- `PIP-7079` and `PIP-8410` are retired and sit under
  five live cards. Same rule as `QUEUES` vs `QUEUES_OFFERED`. Match the
  **starred** form only: `City of Superior WI : LENDING CALIB. BAR - Unpaused`
  is a live customer that contains the letters. And test it on the summary
  **before any cutting**: `Wilson Excavating: ACTIVE FOR 3RD PARTY CODING
  *INACTIVE*` carries its marker after the colon.
- **Two customers may shorten to the same label and both be live.** `IPI : El
  Paso` and `IPI : *REP*` both read as `IPI`. A list with the same word twice
  is worse than the typos it replaces, so a collision is reported rather than
  written and forgotten, and `/clients/roster` flags the rows and sends the
  full summary along to tell them apart.
- **`client_aliases` resolves the spellings already on the board, and never
  guesses.** Tier 1 goes through the ticket's Client CR key -- the thread says
  `PIP-8605`, so its title spelling means `PIP-8605`, and no strings are
  compared, which is how `duke s root control` and `dukes root control` collapse
  onto one client. Tier 2 is an exact name match for threads with no ticket.
  Tier 3 leaves the rest alone and reports them. That last tier is the point:
  `falmouth ma` and `falmouth me` are 0.91 similar and are different places, as
  are Fulton County North and South, and a matcher confident enough to merge
  the Duke's spellings would merge those too. `dukes` itself is ambiguous --
  `Duke's Omaha` and `Duke's Root Control` are two customers -- and waits for a
  person.
- **Picking a client writes the thread title, never `cards.client_override`.**
  The Client box already drives the title through `_suggest_title`, so the
  dropdown feeds that and nothing else. `needs_triage()` reads a
  `client_override` as somebody vouching for an unreadable card and clears the
  red edge; writing one as a side effect of naming a client would clear the red
  off every ticket anyone had merely opened. `Card._override()` sends one only
  when the title does *not* already say what the box says, and `is_dirty()`
  reads the same function -- those two are the same statement twice and have to
  stay that way. Nothing new is stored on `cards`, so `ernie_state.py` is
  untouched and no derived name enters the three-way comparison.
- **The board types a shortening, and the roster should match it.** Measured
  against production: 24 of the 65 offered clients are typed shorter in titles
  than their Jira name -- `Trekk` for *Trekk Design Group* on 18 threads, `SCI`
  for *SCI Infrastructure LLC.* on 15, `Precision` for *Precision Trenchless* on
  8. Offering the long form would have every new title disagree with the
  existing ones, so the fix for typos would create a fresh inconsistency. The 17
  where the typed form is a **word subsequence** of the Jira name were adopted
  as `short_name`. The rest were not, and the reason matters: `Dukes Root
  Control` appears on 9 threads and is a *misspelling* of `Duke's Root Control`,
  not a shortening. Adopting by popularity would have made the typo canonical.
- **The box searches, the person picks.** `client_matches()` fills the popup
  rather than the completer filtering it, because a completer can only match
  the strings in the list and half of what people type is not in it: `dukes`
  is not a substring of `Duke's Root Control`, `inspect ai` is not one of
  `Inspect.AI`, and `monaloh` appears only in MBE's Jira summary. It searches
  the name, the summary, and every spelling the board has used -- the alias
  table already knows the misspellings, so a name typed wrong last year finds
  the customer today. Every miss measured on the real board was punctuation
  rather than letters, so `client_squash()` takes it out; a letter genuinely
  wrong falls to a `difflib` tier at `CLIENT_FUZZY_MIN`.
- **The tiers exist because a summary mentions other customers.** `Abay
  Construction *Working under Trekk*` contains Trekk and is not Trekk, and one
  flat score ordered the two by whatever the roster happened to be in. A hit on
  the customer's own name outranks a hit on an alias, which outranks a hit on a
  summary.
- **Searching may be fuzzy; resolving may not.** `reconcile_aliases` refuses to
  merge on resemblance because `falmouth ma` and `falmouth me` are 0.91 similar
  and are different places. That rule is about a matcher writing an alias with
  nobody watching. The search only puts candidates in front of a person, so it
  may offer anything -- and must offer *both* when a query is ambiguous rather
  than choosing: `dukes` returns Duke's Omaha and Duke's Root Control, and
  settles nothing.
- The dropdown is **pick-or-type**. A customer exists before Jira hears about
  them, and a card already carrying an unoffered client keeps it.

## Running

```bash
./run.sh test bert          # sandbox: sync + outbox + api + bert
./run.sh test bert lan      # same, API reachable from other machines
./run.sh prod               # production: sync + api only, no outbox
./run.sh stop               # stop a stack this script started
python ernie_sync.py --once --env ernie-test.env --db ernie-test.db
```

**Closing the terminal window leaves the stack running.** Ctrl+C reaches the
children through the process group and they stop tidily; closing the window
runs no trap at all, and sync and outbox go on with nothing on screen to say
so. Found in the sandbox after a day of it: **six syncs writing to one
database and six outboxes publishing to one state channel**, which is where
`outbox.log`'s 429s came from -- and a console window flashing past for every
process every start, which is what a report of "dozens of white windows" turned
out to be. `run.sh` now records the **Windows** pids (`/proc/<job>/winpid`, not
its own job numbers, which do not outlive it), refuses to start on top of a
stack that is still up, and `./run.sh stop` ends them with `taskkill`.

## Testing with somebody else

Two ways, and they are not the same setup. `TESTING.md` is the version to
hand over.

**One backend, two Berts.** You run `./run.sh test bert lan`, which binds the
API to every interface and prints the address; they run `bert.cmd`, which
asks for it once and remembers it. They need Python and a clone -- no token,
no database, no migration -- and the board updates at Bert's 5s poll. Same
network only. In *this* setup they must not run `run.sh`: a second sync and
outbox with no `STATE_CHANNEL_ID` is a second board, not a shared one.

**Two full stacks.** Both run everything, sharing only `#ernie-state`.
Works across networks and neither laptop has to stay up for the other, at the
cost of a token on both machines and a board that is up to a sync cycle
behind. `STATE_CHANNEL_ID` is what makes it one board; `ALLOW_DISCORD_WRITES`
must equal `DISCORD_GUILD_ID` or their changes never leave their machine. A
machine joining with no `state_sync` rows adopts the channel, so a fresh
clone comes up already matching the shared board.

`ernie_state.py --check` is the preflight for the second setup and what
`stack.cmd` runs before starting anything: it catches a missing
`STATE_CHANNEL_ID`, a channel the bot can't reach, and a clock out by more
than `SKEW_WARN_S`. Clock skew is read from the `Date` header of a live
response -- an existing message's timestamp says when it was written, not
what time it is, so measuring against one reports its age as skew.

Bert shows which it is: the `shared board · …` indicator by the refresh
button appears only when `state_sync` has rows, and reports up to date,
waiting to send, or out of contact. It says "shared board" rather than just
"shared" because the label has to answer *what* it is talking about on its
own -- read cold, `shared · in step` told nobody what was in step with what.

The API has no authentication, so on `lan` anyone who can reach the port can
move cards and post to the thread. Trusted network, for as long as the test
lasts, never port-forwarded. `lan` is refused outright for `prod`.

Environment: Windows, Git Bash (MINGW64), Python 3.13, SQLite in WAL mode.

## Style

Match what's there: standard library first, `httpx` for HTTP, dataclasses for
records, plain functions over classes unless state demands it. Comments
explain *why*, not *what*. No new dependencies without a reason.

**A ticket wears its tag, not its priority.** `card_skin()` fills a card from
`T.QUEUE[queue]` -- PROD, OPS, ENG, CS. The priority is where a card is
sitting, which the band around it already says, and saying it twice spent the
board's whole colour budget on the half nobody was reading. The one exception
is **needs attention** (the `unassigned` band, `BAND_LABEL` renames it and its
header carries `CAUTION`), which outranks the tag and is red: it is the only
state asking for a person rather than describing the work. That is why red is
free to mean one thing again -- it used to be shared with critical, and two
red bands at the top of a board is an emergency that isn't one. Triage stays a
2px outline over whatever fill the card has.

**The band's colour is on its header only.** `BAND_TINT` fills `#bandHeader`
and nothing else; the panel the cards sit in is transparent. Tinting both put
a second colour behind every ticket, which stopped working the moment tickets
took their tag's colour -- a PROD card read as amber on blue in Medium and
amber on amber in High. `tests/check_palette.py` reads this off the source
rather than building a `Band`: a widget built with no QApplication does not
raise, it aborts the process, and the checks deliberately never make one.

**A container's stylesheet must name the container.** `setStyleSheet("background: ...")`
with no selector applies to the widget *and everything under it*, including
the tooltip a child owns. `Rail` did that, so hovering a row in the running
order produced a box the right size for its three lines, painted the canvas
colour, with the text the same colour as the box -- measured against a plain
`QWidget` in the same process: readable there, solid black here. Scope it:
`Rail { background: ... }`. And `apply_theme` states `QToolTip` on the
application, which settles it whatever else cascades and is needed anyway --
Qt draws tooltips itself and ignores the `ToolTipBase`/`ToolTipText` already
in the palette, so they came out the system's pale yellow on a dark board.

**Hide a widget before unparenting it.** The teardowns unparent before
`deleteLater()` on purpose -- one still parented to the panel keeps painting at
the geometry it had, and a rebuild mid-drag left the old rows on screen under
the new ones. But `setParent(None)` on a *visible* widget makes it a visible
**top-level window**, and `deleteLater()` only queues the deletion, so it stays
one until the event loop catches up. Rebuilding the feed threw away 151 rows
and put 151 blank windows on the desktop for ~1.2s each, titled `python3`
because that is the name Qt takes for the application -- counted with
`EnumWindows` during a real `./run.sh test bert`: 152 new windows, 151 of them
that, all from Bert's own pid. `w.hide()` first, at every one of those sites;
`tests/check_palette.py` holds the line above each of them.

**No colour literals in Bert.** Every colour comes off `T`, the active
palette -- `T.INK`, `T.BAND_CARD[band]` -- and a new one has to be added to
both `LIGHT` and `DARK`. A hex typed into a stylesheet works in one theme and
is wrong in the other, silently, in whichever theme nobody happened to be
looking at. Settings offers light, dark, or following the desktop; changing it
rebuilds the window, because each widget styles itself where it is made and
there is no single sheet to swap -- and a stylesheet missed on a restyle is a
white panel in a dark board. `tests/check_palette.py` holds the two palettes
to the same keys, which is the invariant that keeps a theme from crashing only
for the person using the other one.
