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
| `seed_test_server.py` | Builds realistic test threads. Test guild only. `--limit N` for a short board while iterating. |
| `wipe_test.py` | Deletes all threads in the test channel. Test guild only. |
| `ernie_state.py` | Board state in Discord: one message per card in `#ernie-state`. |
| `ernie_changelog.py` | Every change, appended to `#change-log`. Off unless configured. |
| `ernie_jira.py` | Customer list, Jira → SQLite. Read-only against Jira. Off unless configured. |
| `ernie_version.py` | The version number, and which build is answering. Imported by everything that says one. |
| `ernie_status.py` | The ticket's status, as a pinned message in its own thread. Rides with the outbox. |
| `run.sh` | Starts the whole stack. `./run.sh test bert` |
| `bert.cmd` | Double-clickable launcher for a tester who runs only Bert. |
| `stack.cmd` | Double-clickable launcher for a tester who runs their own stack. |
| `tools/q.py` | Ad-hoc SQL helper. `python tools/q.py "SELECT ..." ernie-test.db` |
| `tools/fake_stats_data.py` | Invents a past for the sandbox so the figures panel can be looked at. Refuses production; `--clear` undoes it. |
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
- **It names nobody, deliberately.** The thread object does not say who
  archived it, and the audit log that would (`THREAD_UPDATE`) needs a **View
  Audit Log** permission the bot does not have -- checked, and refused. So
  the event carries `actor_name` NULL and `new_value = "discord"`, and Bert's
  feed says "closed in Discord" rather than running the usual fallback, which
  would have read "Ernie closed it" -- the one attribution that is certainly
  wrong.
- **`dispatch_after` is NULL**, the same rule `started` follows: it happened
  in Discord already, and posting "closed" back into the thread is Ernie
  telling the room what it just watched somebody do.
- **Undo refuses it and points at reopen.** Clearing `completed_at` would
  leave the thread archived, so the next pass closes the card again -- back
  on the board for five seconds and gone, for ever. Reopen posts to the
  thread, and posting to an archived thread unarchives it, so the two agree
  afterwards. Verified end to end in the sandbox: reopen, outbox drain,
  Discord reports `archived: false`, and the next sync leaves the card open.
- **Bert's own Complete is not re-detected**, and not because of a flag about
  who did it: a completed card is not in the query at all, which is guarded
  on `completed_at IS NULL`.

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
  same rules apply: `RAIL_MIN_W`/`RAIL_MAX_W` rather than a fixed width, and
  unfolding clears `_rail_sized` so the width comes back rather than whatever
  the fold left.
  **Folding is the button's business; opening is either's.** A folded panel
  used to be pinned with `setFixedWidth`, which meant the pane could not be
  moved at all -- and the handle is still sitting right there against the
  spine, so dragging it did nothing and nothing on screen said why. Reported
  as not being able to drag the sides back open, which is exactly what it was.
  Folded, the panel is held to the spine as a *minimum* and keeps its full
  maximum, so the handle can pull it out; `_unfold_by_drag` turns a drag past
  `UNFOLD_GRAB` into the unfold. It passes `settle=False`, because the drag is
  already the width somebody is choosing and re-placing the panes would snap
  it out from under the pointer. The stretch factors are what keep this safe:
  panes 0 and 2 are 0, so a window resize is absorbed by the board and a
  folded spine cannot be widened into opening itself -- measured across four
  resizes from 1050 to 1600px, both spines stayed at 30. Kept in `settings.rail_width`. There is slack to take: the
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
- **An empty band is still named, in both lists.** It is somewhere to drop a
  card and somewhere to start one, and the shape of the order is easier to
  read when every step of it is on screen. The rail used to draw a band only
  if it held something, unless a drag was in flight -- so a board with
  everything in Needs Attention showed one heading at rest and four more the
  instant a card was picked up: the list rearranging itself under the pointer
  at the moment somebody was aiming at it, with nothing to aim at before that.
  It reads as a glitch and was reported as one.
  On the board it was worse than untidy, because the heading carries
  `+ New Ticket`: a hidden band takes its button with it, so with everything
  in Needs Attention there was no way to start a ticket in Critical at all --
  measured, four of the five buttons did not exist. **The drop zone is still
  drag-only**, though, because a zone is a target rather than a label and four
  of them stacked up at rest pushes the running order off the bottom.
  **A band a *filter* emptied still hides**: somebody who typed a search did
  that deliberately, and five headings over one result fights the narrowing
  rather than helping it. `Bert.filtering()` is the one question both lists
  ask, and the queue checkboxes count as narrowing for the same reason the
  search does.
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

- **The board column is centred in its pane, and its scrollbar comes with
  it.** It was pinned left, which is why folding the running order gave the
  board nothing to look at: the column slid across into the space the rail had
  been in and left the same width of floor on the other side. Centred, folding
  either side opens the room around it evenly and the tickets stay where the
  eye already is. Asked for once the window had settled into three big
  sections, and the column being attached to the rail stopped being worth
  anything.
  **Two ways to centre it and only one takes the bar along.** Centring the
  *column* inside a full-width scroll area leaves the area's scrollbar at the
  pane's own edge: measured with the rail folded, the column sat 116px in from
  the left and its bar 160px out to the right, hard against the figures panel
  and reading as though it belonged to them. So the cap goes on the **scroll
  area**, which brings its bar to the cap with it -- the rule `feed_scroll`
  already follows -- and two spacers centre the area inside `board_holder`,
  which is what the splitter now holds. The cap allows for the bar's own
  width, or the column loses that much the moment the board is long enough to
  scroll.
  The area is added **by stretch factor, never by an alignment flag**: a
  scroll area added with one takes its own sizeHint, cap or no cap. And the
  two spacers carry **no factor** -- given one each they split the pane three
  ways with the area and it never reached the cap at all, measured as the
  column stuck at its 463px minimum on a 1500px window.
  **Centred on the window, not on the pane.** `_centre_board()` works the
  spacers out against the *splitter*, which spans the whole row. Splitting
  the pane evenly is only centring while the two side panels happen to
  match: drag the running order out to 460 and leave the figures at 180 and
  the pane's own middle is 139px right of the window's, which is what
  somebody looking at the screen sees and reported.
  **Staying centred costs width, and that is the trade.** A column filling
  its pane cannot be centred, because the pane is not -- so it comes in to
  the widest that can be, whichever of its two edges runs out first.
  Measured at 1500px with the rail at 460 and the figures at 180: 568 against
  the 846 it would otherwise take. At 1920 the same arrangement centres at
  the full 846 and costs nothing, folded or not. The floor is the column's
  own `minimumSizeHint` -- under that it gives up no more room and sits as
  near the middle as it can, because a board too narrow to read is the worse
  of the two. Every path that can move either width re-centres:
  `_place_sides`, `resizeEvent`, and the handle.

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
- **Nothing rebuilds a band while an editor is open, whatever asked.** The
  poll parks its payload for this reason, and always has -- but `render()` is
  reached from places no poll goes: a window resize goes through the same
  timer the rail handle uses, and the end of a drag calls it outright. Those
  rebuilt the bands regardless, so **maximising the window while writing a new
  ticket destroyed it**. For a card that exists it lost whatever had been
  typed, the widget being replaced from the data behind it; for a ticket being
  started it was worse, because the placeholder is not in `self.cards` -- so
  nothing rebuilt it at all and `editing_card` was left naming a widget that
  no longer existed, which holds every later poll and freezes the board with
  nothing on screen to say why. Only the **bands** are spared, because that is
  where an editor lives: the rail holds none and goes on re-clipping to the
  new width, which is the whole point of the resize. The band signatures are
  deliberately left un-updated, and `_bands_stale` carries the missed rebuild
  so `apply_pending()` draws it the moment the editor closes -- a parked poll
  redraws by its own route, but a resize parks nothing, and without the flag
  the board kept a layout for a window that was gone until whichever poll came
  next.
- **Bert's close warning owes three debts, and the third is a different
  kind.** The two below are about Discord, and neither is lost by closing --
  the outbox posts them whether Bert is open or not, which is why that warning
  is about shutting the *stack* down. An **editor nobody has saved** is the
  opposite: gone the moment the window shuts, the only one of the three that
  is lost with the stack already down, and for a long time the only one not
  asked about. `_editor_may_close()` asks it **before `connected` is read**,
  for that reason, and offers the same three-way the second click on Edit
  does, with *keep editing* as the default.
  `save_edits()` and `create_ticket()` answer `True`/`False` for themselves,
  because the editor being shut says nothing about whether a write landed.
- **A write that did not land keeps what was typed.** `Card.save()` used to
  put the card back in view mode *before* the write, so a failure was followed
  by `refresh()` redrawing the card from server data: everything typed was
  gone, and the error box explaining the failure sat on top of work already
  discarded. Worse for a ticket being started, where the placeholder is all
  there is -- the request failed and took the whole ticket with it. The write
  goes first now and the editor closes only once there is nothing left to
  keep, which is also why `create_ticket()` drops the placeholder in the
  `try`'s `else`. A failed save therefore leaves an editor open, so
  `editor_is_busy` returns `not w.save()`: opening the other card on top of it
  would put two editors on screen, which is the thing that guard exists to
  stop. `_edit_conflict()` answers what it *settled* rather than what it
  wrote -- "keep theirs" and "discard my changes" close the editor, because
  the person said so; a retry that fails, or a dialog closed without
  answering, leaves the typing where it is.
- **Bert's close warning owes two different debts, and must count both.**
  `queued` is events waiting out their undo window before Ernie posts them to
  the customer thread; `sharing.waiting_to_send` is cards that have moved since
  the shared board was last published. A reorder, and every band move that is
  not in or out of `critical`, is silent -- no `dispatch_after` at all -- so it
  appears only in the second. Counting the first alone meant reordering the
  board and closing straight away asked nothing, and the running order never
  left the machine. `Bert._owed()` reads both.
- **The unsent mark says what it means, in words.** It was `*` and `!`, on
  the reasoning that the glyph is what tells the two states apart and it
  spends no colour. Both halves were true and neither made `*` mean anything:
  an asterisk in the corner of a card is a footnote mark with nothing to point
  at, and the sentence explaining it lived in a tooltip nobody hovers on a card
  they are not already asking about. It is **Pushing to Discord…** and **Not
  sent** now, drawn as chips -- the card already wears chips for the tag, the
  PIP count and "edited", so it is the shape the eye is reading there anyway.
  One sentence covers all three waiting cases, because `#ernie-state` is a
  Discord channel too: a card waiting only on the shared board is still waiting
  on Discord, and which of the three it is stays in the tooltip. The given-up
  one must never say *pushing* -- nothing is being pushed, and telling somebody
  to wait for something that is not coming is the whole reason `/health`
  reports `stuck` apart from `queued`.
- **And it is drawn in the ink, not the accent.** Measured against every card
  fill in both palettes: the accent averages 4.6:1 in light and 5.9:1 in dark,
  the ink 13.7:1 and 11.8:1. It is also the cheaper choice -- a card already
  wears its tag's colour, and a mark spending none leaves colour meaning
  something, which is the same rule the band bars in the running order follow.
  Amber is kept for the given-up one alone, because a caution is the one thing
  on the card that is genuinely a warning.
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
- **The outbox writes the card the way the sync would, or the board shows two
  different tickets.** The rows it writes are read straight back by the API, so
  a shortcut there is visible immediately. It wrote the title row with the name
  alone and `confidence='pending'`, leaving queue and client NULL: a title that
  parses perfectly well -- `PROD: CHA Solutions - 08Sep26 - what it's about` --
  came up grey with "unknown client" the moment its thread existed, and stayed
  that way, because the sync writes a revision only when the *name* changes and
  the name never did. `load.record_title()` is now the one writer both go
  through. And it ranked the card to `MAX + RANK_STEP`, the bottom of the band,
  while Bert had been showing it at the top since the `+` was pressed -- rank is
  the order and the only one, so it has to say what the board says. It goes to
  `MIN - RANK_STEP` of the band whose `+` was pressed, the same idiom
  `ensure_card` uses to rank an unreadable thread to the top.
- **A ticket can be closed before Discord has it.** Completing looks up a
  `cards` row and a ticket still waiting in `new_threads` has none, so it
  answered `no such card` -- true, and useless: the person meant to close it,
  and the wait for the thread is Ernie's problem rather than theirs.
  `complete_on_arrival` records the intent, the card leaves the board the
  moment the button is pressed, and `make_threads` closes it as soon as the
  thread exists -- writing the `completed` event with a `dispatch_after`, so
  the thread says so the same way every other closure does and it becomes
  undoable from the feed at the first moment there is anything to undo. Until
  then the API answers `event_id: None`, because there is nothing in a thread
  to take back.
  **Closing a card never cuts off what it still owes.** `v_outbox_due` filters
  on dispatch, posted, undone, claimed and attempts, and never looks at
  `completed_at` -- measured, an edit queued behind its undo window is still
  due to post after the card is closed. So a change made seconds before
  closing still goes out.
- **A ticket can be dragged before Discord has it**, and it is the same
  sentence as closing one. A move looks up a `cards` row, a draft has none, and
  "no such card" is true and useless: the person moved it, the board had
  already drawn it in the new place, and the wait for the thread is Ernie's
  problem rather than theirs. The band and the rank are written to
  `new_threads` and `make_threads` brings the card in where it was left --
  which is why it takes `new_threads.rank` rather than recomputing the band's
  edge, or a ticket dragged down into Medium would arrive back at the top of
  it. **Nothing is logged.** The ticket does not exist, so there is no history
  to record and nothing in a thread to announce; dragging a draft into
  Critical is the same act as pressing the `+` in Critical, which writes no
  event either.
- **A draft carries a real rank, because rank is the order and the only one.**
  It went to the board at `0.0`, which is not a place -- ranks can be
  negative, and `ensure_card` puts an unreadable thread at `MIN - RANK_STEP`,
  so a ticket the board promised to put at the top of High could sort below
  everything in it. It is ranked when the `+` is pressed, against the band's
  cards **and its other drafts**, or two tickets started in one band in a row
  are given the same number and tie.
- **A draft is a neighbour like any other.** The board draws it among the
  cards, so a drop can land against one -- and `move_card` read the band out of
  `cards` alone, so the neighbour Bert named was not in the list, the midpoint
  fell through to "end of band", and the card went somewhere nobody aimed at.
  `band_order()` is the one place both tables are read as one order, and
  `band_top()` and the respacing go through it too.
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

- **`NEW_TICKET` is a sentinel, not an identity.** `editor_is_busy` lets a card
  through when the editor already open is that same card -- there is no
  decision to put to anybody -- and every ticket being started shares the one
  sentinel, so a second `+` matched it and walked straight past the one-editor
  rule. Two placeholder cards, two open editors, and one `editing_card` naming
  both: `_card_widget` answered with the first while somebody typed into the
  second, so clicking Edit on a real ticket offered to save a draft other than
  the one on the screen. The dialog for the collision already existed and
  simply could not be reached.
- **Two tickets nobody has created yet is its own sentence.** "Opening a new
  ticket" reads as though there were something there to open, when the choice
  is whether to start a second, so the wording turns on `another` and the verb
  becomes *start*. A blank draft is still closed without asking and simply
  moves to the band whose `+` was pressed: `is_dirty()` compares against the
  template, so nothing was lost.
- **The box says each thing once.** The title said "Unsaved changes on another
  ticket", the body said "You have unsaved changes on:" under it, and the line
  below said the editor would close -- three sentences carrying one fact, above
  buttons that then said it a fourth time. It is a title, the ticket's name,
  and what the button will do: *Unsaved changes* / the name / "Opening a new
  ticket will close it." Nothing is lost by dropping "on another ticket",
  because the ticket named is the one being closed and it is visibly not the
  one just clicked.
- **The dialog does not name the band.** It said "a new ticket in Needs
  Attention" on all three buttons, which was the longest thing in the box and
  the one part never in question -- the band was decided by which `+` was
  pressed. What the buttons have to keep apart is the two *tickets*, and the
  one being closed is named in the body above them, so "it" against "a new
  ticket" carries it. `editor_is_busy(tid)` takes no label: `_short_name()`
  answers "a new ticket" for the sentinel already, so there is one place a
  ticket is named rather than two.

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

## The figures beside the board

`Stats` is the panel on the right, and `/stats` is what fills it. The board
says what is on the plate now; none of it says whether that is getting better
or worse, how long a ticket takes, or which ones have been open since April.

- **A few figures, not a survey.** Completed per month, the open ones that
  have been open longest, and how long one takes end to end. Each answers
  something no other view does. A page of statistics nobody acts on is
  furniture, and the first one that turns out to be wrong takes the
  credibility of the others with it.
- **There was a fourth, and it was dropped.** Open tickets with no build or
  return raised against them: true, interesting, and nothing anybody did
  differently for having read it. Julian read the panel and said so, which is
  the standard above being applied rather than an exception to it -- the query
  went with it rather than being left to run every refresh for a field nothing
  reads. The panel is headed **Stats**, which is what people call it.
- **How much there is, how much came in, how much went out -- by tag, over a
  window somebody picks.** `tally` in `/stats?days=N`, drawn as a table
  because the three numbers are read together: "three open, eleven in, twenty
  out" is a sentence about PROD, and the same figures as three separate lists
  is three things to hold at once. `STATS_WINDOWS` offers 7 days through a
  year, kept in `settings.stats_days`.
- **Open is a level; created and closed are flows, and the labels say so.**
  Open is the backlog *now* and does not move when the window does. Windowing
  it would answer "opened inside the window and still open", which is a
  different and much less useful question -- the work somebody is carrying
  does not begin at the start of whatever window they chose. The columns are
  `open`, `new` and `done` for that reason, and the tooltips spell it out.
- **The rows have to add up, which is the whole reason `Other` exists.** A
  card whose tag is retired -- or that has none -- still counts towards the
  board, and dropping it would leave a table whose rows do not make its own
  total. A figure that does not add up is the first one somebody stops
  believing. `Other` appears only when it has something in it; every
  *offered* tag keeps its row even at nought, so the shape of the list does
  not change under a reader.
- **In the toolbar's order, not the parser's.** The table walks `T.QUEUE`,
  which is what the filter checkboxes walk, so it reads PROD OPS ENG CS --
  the order already read once across the top of the window.
  `ex.QUEUES_OFFERED` is a different order.
- **The window compares dates with `datetime()` on both sides**, and the way
  that fails is not the obvious one. For dates a day or more apart a raw
  string compare gives the right answer anyway, because the digits differ
  before the separator is reached. It goes wrong **only on the boundary day**,
  and always in the same direction: `T` (0x54) sorts after a space (0x20), so
  a thread opened earlier in the day than the cutoff compares as later and is
  counted in a window it falls outside. Every figure reads slightly high and
  nothing looks broken. `check_stats.py` puts a row four hours the wrong side
  of a seven-day cutoff, which is the only place it shows.
- **The blocks stack from the top, and the leftover goes under them.**
  `setWidgetResizable` stretches the holder to the viewport, and a
  `QVBoxLayout` with nothing to absorb the surplus hands it out *between* the
  items -- so on a panel with less in it than the window is tall, the rows
  spread down it like a menu. `set_stats` ends with `addStretch(1)` for that.
  It only shows when the content is shorter than the panel, which is exactly
  why it survived every render that had each block full.
- **The panel scrolls.** It was a plain column with a stretch under it, which
  was fine while three blocks fitted -- add a fourth and Qt does not clip the
  overflow, it **squashes every block proportionally**: measured, the tally
  asked for 134px and was given 11, so a six-row table drew as one line and
  "time to close" was cut off the bottom edge. Nothing reported anything,
  because nothing had failed. More blocks are coming as people say what they
  want here, so it is a scroll area now rather than a height to keep an eye on.
- **The window selector is built once and lives outside the body.**
  `set_stats` throws the body away and builds it again whenever the numbers
  change; a combo rebuilt under somebody's pointer loses its popup mid-choice
  and would have to have its value restored from settings on every redraw.
  Changing it clears `stats_at` and asks again straight away -- the figures
  ride the slow lane, and a dropdown that takes a minute to change the numbers
  under it reads as broken.
- **Nothing is derived from `events`.** Production's is **empty**, and all 313
  of its completions read `completed_by = "imported"` -- they were inferred
  from archived threads, not recorded by anyone using Bert. So per-person and
  per-action figures would show one fake name until production runs this
  build. Everything here comes off `cards` and `threads`, which are mirrored
  from Discord and true on any board.
- **The middle, not the mean.** Measured against production: average time to
  close 13.1 days, median **7.9**. The mean is the one that reads as "this is
  how long a job takes" and it is the one a handful of very old tickets drag
  away. The panel quotes the median; the mean is in the tooltip for anyone who
  wants it, and the slowest is shown outright because the spread is the story.
- **A trend, not a number.** "Completed this month" was the request; a single
  figure throws away 27 → 61 → 63 → 67 → 95 across five months, which is the
  news. The same query gives the shape for free.
- **And it follows the window, because everything on the panel does.** It was
  pinned to six months while the tally beside it moved, and it is the biggest
  block there -- reported as "I change the timeframe and nothing changes",
  which was fair. A control that visibly does nothing to the thing under it
  reads as broken whatever else it is quietly doing.
- **The bucket comes from the window, not from a constant.** Daily to a
  fortnight, weekly to two months, monthly beyond -- which puts every offered
  window between three bars and fourteen. The thresholds are the whole
  design: at ten days a two-week window fell to weekly and drew *two bars*,
  which is not a trend, it is two numbers with a picture round them.
- **A month bucket is a whole month, and the query widens to match.** Left
  rolling, the earliest bar was a part month standing beside whole ones --
  June the 14th to the 30th against all of July -- and read as a quiet month
  rather than as half of one. Weeks stay rolling, because a seven-day slice
  is not a named thing anybody compares against a calendar. The exact figure
  for the window is the tally's job; this is a shape to compare along, and
  the things being compared have to be the same size.
- **Buckets are built forward from the start of the window**, not off the
  rows that came back, so a quiet week is a nought in the trend rather than a
  bar that is simply absent. Missing reads as "no data"; nought reads as
  "nothing closed", and they are not the same news.
- **Sized and folded exactly like the running order**, which is the point of
  putting it there: a range rather than a fixed width so the handle has
  something to move, `setFixedWidth` only while folded because folding is the
  button's business, `stats_width` remembered, and `_stats_sized` cleared on
  unfold so the width comes back rather than whatever the fold left. It takes
  the width out of the *board's* share, which is the one with slack in it. Its
  fold button sits on the left of its own header -- the mirror of the rail,
  whose spine is the far edge of the window.
- **Folding has to re-place the splitter, both ways.** Narrowing the widget
  does not narrow the pane it sits in: the splitter keeps the width it last
  allotted, so a fold left a 30px spine, then four hundred pixels of empty
  floor, then a handle stranded in the middle of it -- and the board got none
  of the room the fold was for. `set_folded` clears both sized flags and calls
  `_place_sides()` on the way in as well as the way out, which also puts the
  handle back against the spine where it belongs. Measured on a 1500px window:
  folded, `[30, 1078, 380]` with the handle at x=30; unfolded again,
  `[460, 648, 380]`, exactly where it started.
- **One function places both sides.** They share a splitter and `setSizes()`
  takes every pane at once, so `_place_sides()` owns the three widths
  together. Placing one of them with a two-element list -- which is what
  `_place_rail` did before the figures were added -- leaves the third pane to
  whatever Qt makes of a short list, and in practice *neither* width applied:
  both panes sat at their content width and the rail quietly ignored the one
  somebody had dragged.
- **They ride the slow lane.** `STATS_MAX_AGE_S`, like the roster, because
  they move when a ticket closes rather than every five seconds -- hanging four
  aggregates off the poll the board depends on would make each of them a fresh
  way for that poll to fail. An Ernie too old to serve `/stats` answers `None`
  and the board carries on.

- **A filter says how many it holds.** `PROD (3)`, `OPS (4)`. The checkboxes
  said which tags exist and nothing about how much was behind each, so the
  answer to "how much OPS work is there" was to click three boxes off and
  count. `queue_counts()` is a pure function over the board's cards for the
  two ways this goes wrong, both of which read as a bug rather than a
  different reading: counted after the filtering, unchecking PROD changes the
  number beside OPS; counted against the search, it answers "how many did you
  find", which is what the board itself is already showing. It is the whole
  board, always. `set_count` is guarded because `render()` runs on every poll
  and every drag, and `updateGeometry` on four boxes relays the toolbar each
  time.

- **The toolbar gives up words before it starts cutting them.** At the
  window's own minimum width the bar asks for about 45px more than it has,
  and Qt spends the difference on whatever can be squeezed: `Search client,
  equip` in the box and `shared board · up to da` beside it -- a status cut
  off exactly where it begins saying something, keeping only the words that
  are the same every time. Eliding takes the same half, so nothing here is
  elided. `status_forms()` drops the **noun** instead: `shared board` is
  established the first time anybody reads it, and `up to date`, `no contact`,
  `3 to send` is the part being looked at. Both labels carry the whole
  sentence in a tooltip regardless, and the alarm states stay visible at every
  width -- hiding the indicator would have hidden a warning.
  `_fit_toolbar()` steps both labels down together and stops at the first
  level the layout's own `totalMinimumSize` says it can hold, then picks the
  longest of `SEARCH_HINTS` that fits the box. **In that order**: measured the
  other way round, the box is sized before the shortening has given it its
  room back, and a *narrower* window showed a *longer* placeholder (1002px
  against 940px). It runs straight off `resizeEvent` rather than through the
  board's redraw timer -- the board is thirty widgets and waits for a drag to
  settle; leaving the bar cut for the length of that drag is the thing being
  fixed. The search box carries a stretch factor for the other end of the
  range: without one the surplus went entirely to the spacer and the box sat
  near its minimum at 1200px with 165px going spare.

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

## The version number

`ernie_version.VERSION` is the one, and everything that names a version
imports it. Bump it there and nowhere else.

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
- **The build number is still not compared anywhere.** The *wire format* is,
  and says so loudly -- see the state channel below -- but two machines on
  0.9.0 and 0.8.1 with the same `FORMAT_VERSION` read each other perfectly and
  nothing remarks on it. That is the right order to build these in: the format
  is what actually breaks a board, and the build number is what an update
  check will compare.

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
- **`--fast` is the listing beat (5s); `--interval` is everything else (60s).**
  The full pass does what a fast one does *plus* the rescan, the state channel
  pull and the Jira roster, so nothing that was on a minute has moved. A fast
  pass is `cycle(..., full=False)`.
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

**Four levels in light, and the workspace is the board column alone.**
Darkest first: `well` is the outer chrome -- the toolbar, the floor either
side of the centred board column, the space a folded panel leaves; `panel` is
the sections *around* the work, which is the running order, the figures and
the activity feed; `canvas` is the workspace, which is `#boardColumn` and
nothing else; `surface` is the cards and the things being read and typed in.
Light was reported as glaring three times, and the first two answers went at
the card fills. The fills were never it. What was bright was **how much of the
window** was: the sections and the workspace sat on one value with the cards a
long way above it, so almost everything on screen was near the top of the
range. A grey workspace with bright work surfaces, rather than an application
printed on a sheet of paper -- and the cards are now the only bright thing in
the window, which is also what makes them read as the work.

**Dark keeps three, and that is measured rather than excepted.** Its whole
bottom end from the floor to the workspace is a contrast ratio of **1.06**, so
a fourth step inside that is a difference nobody can see -- `DARK["panel"]` is
its canvas value on purpose. Dark gets its depth from the border-to-fill
relationship instead, which is 5-7x. `tests/check_palette.py` holds both: the
light ramp must be four real steps in order, and dark's must be the three its
range can carry.

**A control is drawn a step *under* the card it sits on, in both themes.**
`control` is what a button, a field and a work-item bubble are filled with. It
used to be `surface`, which was right while a card was a tint and the surface
was the near-white above it -- and stopped being right the moment the cards
came up to the surface level themselves. Measured then, `surface` against
every card fill in light was **1.00-1.01**: a bubble with nothing but its
border and a search box that was a rectangle of hairline, and no check said
anything, because every *text* pairing was still fine. It is the fill against
its ground that had gone, which is why there is a check for that now.
`beside` was the obvious token and is the wrong one -- in dark it is lighter
than some card fills and darker than others (1.02-1.07), so a control would
appear and disappear depending on the ticket's tag.
**Fill or border, and at least one doing real work.** Light has both, 1.14 of
fill and 1.73 of border; dark leans on the border, 1.03 and 1.23, and its fill
alone is not enough on a triage card. The direction is the strict half and is
the same in both: a control is never brighter than the card it is on, so
nothing on a card competes with the card for being the top surface.

**A plain `QWidget` ignores a stylesheet background unless it opts in.**
`Rail`, `Stats` and `#boardColumn` all set one and none of them ever painted
it -- proved by setting a rule to magenta and still seeing straight through
it. What looked like the board column's colour was the scroll viewport behind
it, which is why the strip beside the column could not be told apart from the
column itself. `setAttribute(Qt.WA_StyledBackground, True)` is the opt-in, and
any new widget of this shape needs it or its background rule is decoration.

**And the scroll viewport takes its colour from a stylesheet set on itself.**
Setting the viewport's *palette* does not survive: it reads back as the
application's canvas whether it is set before `setWidget()` or after --
measured, `#14181D` where `#0E1115` was asked for, which is exactly what made
the strip beside the board read as an awkward gap rather than as floor. Give
the viewport an object name and set the rule **on the viewport**, not on the
scroll area: `vp.setStyleSheet("#boardBack { background: ... }")`. The name is
what keeps it off the cards -- a bare `background:` on a scroll area cascades
into every band and card inside it.

**A container with its own stylesheet owns its tooltips too.** `apply_theme`
states `QToolTip` on the application, and that settles it for any widget
carrying no sheet of its own. It does not settle it for one that has one: Qt
resolves a tooltip against the nearest stylesheet in the widget's chain, so a
container that names only itself leaves its rows' tooltips to whatever the
platform draws -- which on a dark desktop is dark, against the ink a light
board asks for. `tip_css()` is that rule written once, and `Rail`, `RailRow`
and `Stats` state it alongside their own. Reported from the running order,
whose rows are the most hovered thing on the board.

**The application palette starts from the style's, not from a blank one.** A
default-constructed `QPalette` leaves every role it does not name at Qt's
fallback, and `setPalette()` then installs that over the whole application --
so roles nothing here thinks about, including the ones a tooltip paints from,
came out black.

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

**Light mode is not white.** It was: `surface` was `#FFFFFF` at luminance
1.000, the canvas 0.920 behind it, and every card fill between 0.76 and 0.86 --
a field of near-white, and tiring to read for an afternoon. The whole light end
came down together, which is what keeps the order that carries the meaning:
surface above canvas above beside, each card a shade deeper than the wash it
sits on. The inks did not move, so every pairing kept its contrast; measured
after, the worst text pairing anywhere is 4.7:1 and most are above 12:1.
**And two large areas were painted in the brightest token for no reason.** The
toolbar and the feed panel used `T.SURFACE`, so the biggest slab in the window
was also the brightest thing in it -- brighter than any card. They are chrome,
like the rail and the figures either side of the board, so they take `T.CANVAS`
now and recede; the rule under the toolbar is what separates it. That is a
change to both themes on purpose: dark had the same two slabs sitting proud of
everything around them.

**Light mode's job is the same one dark already does, arrived at from the
other end.** It was reported as overwhelming twice, and the first answer was
wrong in a way worth keeping written down. The reasoning was that light's cards
had no brightness separation from what they sat on, so colour was carrying the
whole structural job -- and the measurement behind it was taken against `well`,
which is the floor *beside* the board column and not what a card is drawn on.
`#boardColumn` takes `T.CANVAS` and `#bandPanel` inside it is transparent, so
the ground under every card is the canvas; read off a screenshot to settle it,
every pixel between two cards is `T.CANVAS`. Measured there, light's fills stood
at 1.19x their ground where dark's stand at 1.18-1.26. They were never the
problem. Deepening the neutrals to widen a gap that was already the right size
is how light came to look like it was drawn on slate.

**The gap was the border, not the fill.** The stripe that edges a card is one
hex shared by both themes, and a colour chosen to blaze on a near-black card is
a pastel on a near-white one: dark's borders stand at **4.96-7.00x** their own
fill, light's stood at **1.64-2.34x**. That is the whole of it. The card had an
outline in dark and a suggestion of one in light, and a board of shapes with no
edges is what "overwhelming" describes -- nothing tells you where anything
stops, so the eye reads the colour instead.

So the separation is spent the other way now. The ground is a clean light grey
and the cards are near-white -- `well` the floor, `canvas` the sections,
`surface` above that -- and the **border** carries the tag: same hue, same
saturation, taken down in lightness until it holds an edge, which puts light at
4.44-4.54x. Turning the saturation up instead would have been a different
colour meaning the same thing, which is the one move not available here. With
the border doing that work the fills came down to a whisper: **chroma 6-12,
against 15-29 before and 17-25 in dark.** Worst text pairing anywhere is 5.7:1.

Three rules come out of it, all held by `tests/check_palette.py`:

- **A card stands off the canvas** -- the canvas, because that is the ground it
  is actually drawn on. The floor flatters both themes by a step no card ever
  sits next to.
- **A card is edged in its own tag**, at a ratio with a floor, and it is the
  same hue in both themes. That pairing is the invariant: a fill may be as
  quiet as it likes as long as the border is loud, and the check says so.
- **The band header is accented, not filled.** The colour is a 4px bar down
  its left and the heading's ink; the strip behind them is barely tinted, and
  must never carry more colour than the cards under it. That last is measured
  against the theme's own fills rather than a flat number -- 17 levels of
  chroma on a near-black strip is not the amount of colour 17 is on a
  near-white one, so a shared cap would let light shout or fail dark for a
  tint nobody can see.

**A control drawn by the style is drawn for somebody else's application.**
`BTN_HIT` set the hit area and the type size and left the rest to Fusion, and
what Fusion draws is a vertical grey gradient with a hard bevel -- measured down
a card, the Edit button ran #F9FAFB to #C0C2C7 over 32 pixels, more range than
anything else on the board, on the one control that appears twice per card.
`btn_css()` is the flat version: the surface tone, the hairline every other edge
uses, and the accent arriving only on hover. Both themes, because Fusion was
doing it to both.

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
