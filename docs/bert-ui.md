# Bert's interface

How the board is laid out and why, in the order the questions came
up. The rules that keep it from breaking are in CLAUDE.md; this is the
reasoning, the measurements behind it, and the things that went wrong
on the way.

Split out of CLAUDE.md on 2026-09-16, word for word. CLAUDE.md keeps the
rules and points here for the why.

## The running order, the feed, and the board column

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
- **The window has three foldable sections and one fold button between
  them.** The running order, the figures and the activity feed. `fold_button()`
  is that control written once -- it had been written three times and had
  already drifted, two copies differing only in the alpha of their hover tint,
  0.12 against 0.14, which is nobody's decision and nothing anybody would
  notice going wrong. **The glyph is the one thing that differs**, because
  each folds toward its own edge: the rail left, the figures right, the feed
  down. "Just like the running order" is about the control rather than the
  arrow -- a panel that drops to the bottom marked with a leftward chevron is
  a button describing somebody else's panel.
  The feed was the one without it: a bare caret with no border, on a header
  that happened to be clickable, so the reader who had found the other two had
  no reason to think this was one. The header is still clickable underneath --
  a wider target costs nothing -- but a label that merely happens to be
  clickable does not say a section folds.
- **Folding re-places the splitter, and the feed was the last panel to learn
  it.** Narrowing the widget does not narrow the pane it sits in.
  `_fit_feed` set a *fixed* height on the panel and returned, so the panel
  came down to its caption and the splitter went on holding the pane at
  whatever it last allotted: the heading, then three hundred pixels of empty
  floor, then a handle stranded above it, and the board given none of the room
  the fold was for. Reported as clicking the header collapsing "the content in
  it" rather than the section, which is exactly what it was doing. Measured
  after: `[380, 346]` open, `[696, 30]` folded -- **316px back to the board** --
  and `[380, 346]` again on unfolding, because the layout somebody chose has to
  survive a fold.
  `_place_feed()` is the one place that moves the handle, called from the
  button, because folding is the button's business. Not from `_fit_feed`,
  which runs on every poll and would put the handle back under anybody
  dragging it.
- **The spine is measured off its own caption, not written down.**
  `FEED_FOLDED` was 30, chosen when the control on that header was a caret in
  an 11px label -- about 14px, which fitted inside the panel's 6 and 8 of
  margin with two to spare. The 24px fold button that replaced it did not, so
  the panel was pinned eight pixels shorter than its own contents and the
  button was clipped along its top edge. Reported as the collapse button
  being cut off, **and cut off at every window size**, which is the signature
  of a fixed height: wrong by the same amount everywhere. "A hand-counted
  fixed height clips in silence" is the reasoning at the top of `_fit_feed`
  about the rows; this was that sentence one layout up. `_folded_height()`
  asks the caption, keeps `FEED_FOLDED` as a floor, and measures 38.
- **And folded is a minimum, never a fixed height** -- the rule the two side
  panels already follow, arrived at here the same way. Fixed, the pane cannot
  be moved at all, and the handle is still sitting right there against the
  caption. `_unfold_feed_by_drag` is `_unfold_by_drag` for the other axis: a
  drag past `UNFOLD_GRAB` becomes the unfold, and it deliberately does *not*
  re-place the pane, because the drag is already the height somebody is
  choosing.
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
  **And a band a *filter* emptied is drawn too.** It used to hide, on the
  reasoning that somebody narrowing the view did it deliberately and five
  headings over one result fights the narrowing. That is true about the
  headings and it quietly took the rest with them: **a band is a drop target
  and it carries `+ New Ticket`**, so with a chip on there was again no way
  to drag a card into an empty priority or start one there -- which is
  exactly the bug measured above, coming back whenever the board was
  narrowed. It also brought back the glitch the rule was written against,
  since a drag makes empty bands reappear: the list rearranging itself under
  the pointer at the moment somebody is aiming at it.
  Found from the other end. A board with one card in Low and an equipment
  chip on drew no Low at all, and was reported as tickets going missing from
  the running order and hunted as a scrolling fault -- the feed collapsed to
  look for more height, which is precisely the wrong place. The first answer
  was a footer saying `2 bands hidden by the filter`; the better one, and the
  one taken, was to stop hiding them. **Explaining a disappearance is worse
  than not disappearing.**
  `Bert.filtering()` went with it -- it existed for this rule alone, and a
  predicate nothing asks is dead code with a check standing over it.
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

## The editor, and the dialogs it opens

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

## The figures panel

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
- **A gutter down the right, because every figure here is right-aligned.**
  The counts in the tally, the number on a bar, the total, the age on an
  ageing row -- they all end at one edge, and with no margin on the body that
  edge is exactly where the scrollbar starts. Measured: one pixel between the
  two, reported as the numbers running into it. `STATS_GUTTER` goes on the
  **body**, not the panel: the scroll area is what the bar belongs to, so
  padding outside it moves the bar along with the content and leaves the gap
  where it was. `FEED_GUTTER` is the same rule on the feed, which has had one
  since it started scrolling. It comes out of the room an elided client name
  is given, or the longest are cut to a width that no longer exists and sit
  under the bar anyway.
- **The window selector is built once and lives outside the body.**
  `set_stats` throws the body away and builds it again whenever the numbers
  change; a combo rebuilt under somebody's pointer loses its popup mid-choice
  and would have to have its value restored from settings on every redraw.
  Changing it clears `stats_at` and asks again straight away -- the figures
  ride the slow lane, and a dropdown that takes a minute to change the numbers
  under it reads as broken.
- **Nothing is derived from `events`**, and the reason has now half expired
  rather than gone away. It was written when production's `events` was
  **empty** and all 313 of its completions read `completed_by = "imported"` --
  inferred from archived threads, recorded by nobody -- so a per-person figure
  would have shown one fake name for the whole board.
  Production ran this build on **2026-09-15** and that is no longer entirely
  true: `events` holds real rows, and 25 of the 350 completions carry a real
  person, recovered from the audit log. But **325 still read `imported`**, so
  a per-person figure today would say one name did 93% of the work and would
  be wrong about all of it -- which is worse than the empty table was, because
  it looks like an answer.
  The rule stands for the ordinary reason as well: everything here comes off
  `cards` and `threads`, which are mirrored from Discord and true on **any**
  board, while `events` is what this machine happened to witness. A second
  stack replays the other's changes, but a board installed next year inherits
  no history at all. Worth revisiting when the imported share is small enough
  that a per-person figure would say something -- not before.
- **The middle, not the mean.** Measured against production: average time to
  close 14.3 days, median **8.1** (2026-09-15, 350 completions; it was 13.1
  and 7.9 at 313, so the shape holds as the board grows). The mean is the one
  that reads as "this is
  how long a job takes" and it is the one a handful of very old tickets drag
  away. The panel quotes the median; the mean is in the tooltip for anyone who
  wants it, and the slowest is shown outright because the spread is the story.
- **A trend, not a number.** "Completed this month" was the request; a single
  figure throws away 27 → 61 → 63 → 67 → 100 → 32 across six months, which is
  the
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
- **The longest window is a year, and what falls off the end of it is kept.**
  Asked 2026-09-15: should tickets over a year old stay for the record, or
  does holding them slow the panel down? Neither half turned out to be the
  question.
  **Nothing is ever deleted**, so keeping them is not a decision anybody has
  to take -- it is the append-only rule, and there is no path in this
  codebase that drops an old ticket.
  **And there is nothing over a year old on the board to lose.** Measured
  against production: 923 threads, 3 opened in 2023, 4 in 2024, 316 in 2025
  and 600 in 2026 -- but **every one of the 345 completed cards belongs to a
  thread opened in 2026**, and the oldest completion of any kind is
  2026-04-13. The 323 older threads are all `#customer-support`, which is
  `generate_cards = 0`: mirrored for history, never tickets, never on this
  panel. `#customer-threads` is about five months old, so the year window
  currently reaches everything there is.
  **Looking further back costs 9ms.** The whole endpoint is 12.8ms at seven
  days, 14.2 at a year and 21.7 at ten -- on the slow lane, not the poll the
  board depends on. Volume is not the constraint here and will not be for
  years at ~65 completions a month.
  So the gap is the **dropdown**, not the data, and it opens around **April
  2027**: `STATS_WINDOWS` stops at 365, and the first completion older than
  that is history sitting in the database with no way to reach it from the
  panel. That is the worst of the three states, because nothing looks broken.
  **The catch, for whoever adds `All time`:** it collides with the bucket
  rule directly above. Three years of monthly buckets is 36 bars in a 244px
  panel, which is not a trend any more -- so an all-time window needs a
  quarterly or yearly bucket to go with it, or it is the two-bar failure at
  the other end of the same scale.
  Left alone deliberately until there is a year of tickets to read. Whether
  year-plus figures are worth looking at is a question for the people using
  the panel, and it cannot be answered before the data exists; what could
  have gone wrong quietly was the cost of keeping them, and it does not.
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

## The card face and the filter row

- **The equipment row narrows by being turned on, and says so by not being
  a checkbox.** `EQUIPMENT_FILTERS` maps the four names people say -- Bot,
  E-Reels, ODE, OLK -- onto the `eq_type` the parser reads off a title, which
  is not the same word: measured across production's 889 threads, SSD 217,
  EReel 166, ODE 52, LED 14, OLK 3. **Bot covers SSD and LED**, because
  production says so outright -- `SSD0040: Edge AI Services DEMO bot` and
  `OPS: IPI - LED Bot (exception) LED0059`. Splitting LED out is one line if
  it turns out to be its own thing to the people using this.
  **Pills, not checkboxes, and that is the whole of why.** The queue filters
  beside them default to all-on and narrow by being turned *off*; these
  default to none-on and narrow by being turned *on*, so one click shows the
  ODEs instead of three unchecks hiding everything else. Two rows of
  identical-looking controls behaving oppositely is the thing nobody works
  out by looking, so they are visibly a different control.
  **Any of the chosen kinds, never all.** 65 of production's threads carry
  two or more pieces and one carries six, so a ticket about a bot and a reel
  belongs under both chips.
  **A ticket with no equipment is hidden while a chip is on, and that needs
  saying**: 31 of production's 50 open cards carry none at all, so one chip
  can legitimately empty most of the board -- which reads as a broken filter
  to anybody who does not know. The counts on the chips are what makes it
  legible, deliberately not adding up to the board, and `Show all` appears
  the moment a chip is on so getting back is one click.
  **Its own row under the toolbar**, because that bar already asks for about
  45px more than it has at the window's minimum width -- `_fit_toolbar`
  exists entirely to shorten two labels until it fits, and five more controls
  would be five more things for it to squeeze.
  It narrows the **running order too**, which is what the queue checkboxes
  already do: the two lists are the same board said twice, and one showing
  three tickets while the other shows thirty-four reads as a fault in both.
- **A card links to its build ticket, and nothing here talks to Jira.**
  `Build PIP-8448` in the card's corner, opening
  `{JIRA_BASE_URL}/browse/PIP-8448` -- which the desktop resolves against the
  *reader's own* Jira session. No token, no permission, no request from
  Ernie; the `JIRA_*` config the customer list uses is a different job that
  happens to share a hostname. Confirmed against production's own
  confirmation messages, which carry that exact URL beside the key:
  `Created **PIP-9457**: https://.../browse/PIP-9457`.
  **The address is published, not built in.** `JIRA_URL` reaches `/health`
  the way `UPDATE_URL` does, so the instance can move without re-distributing
  anybody's copy -- and with none configured there is no address, so there is
  no chip. `ticket_url()` is pure and returns `""` for a missing half, for
  the reason `build_standing` is pure: a check can exercise the decision
  without a QApplication, which these checks never make.
  **Builds only, and reversing it is one line** --
  `ernie_api.TICKET_KINDS_SHOWN`. `kind` is NULL on 223 of production's 441
  tickets, which sounds fatal and is history: all 32 open cards carrying any
  ticket carry a *known* build one, so the filter costs the current board
  nothing. And every thread with a build ticket has exactly one -- 193
  threads, 193 tickets -- so a card shows one chip or none and there is no
  "which of them" to answer.
  **The sandbox had to be given some.** `seed_test_server.py` stopped
  imitating Python-Interface-Bot on the reasoning that "Bert shows nothing
  from a Build Request yet", which stopped being true here -- so the sandbox
  had no build tickets at all and the feature was invisible on it.
- **The age moved to the footer, because every column in the head is paid
  for by the client name.** `_client_room` subtracts each one, so the corner
  and the customer are drawing on the same width -- and the corner had just
  grown a `Build PIP-8448` link, which is wide. Measured across production's
  50 open cards, the corner runs **207px on average and 342px at its widest**
  inside a column whose minimum is 463, and the age is **56px of that on
  every card**. At the narrowest the client name was pinned at
  `CARD_CLIENT_MIN_W` -- clamped at its floor, having run out altogether --
  and it gets 68px there now, 56 more at every width above it.
  **The footer's left-hand end was free**, because everything in it is pushed
  right by a stretch, so the age costs nothing there. The **corner stays the
  mark's**: "last in the row, so it sits in the card's top corner" is a
  decision about the one thing on a card that is about the change rather than
  the ticket, and the age leaving does not change who owns that end.
  **It says what it counts, because the number never did.** `3d` in the
  corner of a ticket reads as the *ticket's* age, which is the more obvious
  thing to put on a card and is not what this is: it is `last_human_at`, the
  newest message with `is_bot = 0`. So it reads **`Last reply 3d`**, and
  **never "last updated"** -- `cards.updated_at` is this machine's own clock
  about its own row, a different fact, and the wrong word sends a reader to
  it. The tooltip carries the two rules no label can show, and is the only
  place a person looking at a card can find the bot one. Drawn only when
  there is a number, rather than added blank -- an empty label still takes a
  column and its spacing.
  **And it gives up its words before anything is cut**, which is
  `_fit_toolbar`'s rule one row down: `Last reply 3d` is 143px and `3d` is
  22, and the short form is exactly what the card said before it was
  labelled, so the words cost the number nothing. `_age_forms` is pure and
  returns them longest first, so the first that fits is the most the row can
  say.
  **Blank has two causes and they are not the same news.** No timestamp
  means nobody has *ever* posted; 0 days means somebody posted today, which
  is deliberately silent because most of the board is today most of the time.
  Production shows a number on **all 50** open cards and is blank for neither
  reason. The sandbox is blank on **30 of 34**, because the seeder writes
  every message as the bot -- so the four that do show one are the threads
  opened by hand on 2026-09-11. A board looking broken there is the seeder,
  not the figure. `_age_days` returns `None` against `0` so the two stay
  told apart, and a **negative** reads as nothing too: that is a clock
  disagreeing rather than an age, and `-1d` on a card is a bug report nobody
  can act on.
  **A timestamp with no timezone is read as UTC, and used to kill the card.**
  Subtracting a naive datetime from an aware one raises `TypeError`, which is
  not `ValueError`, so it went past the guard and out of `_build_view` -- not
  a wrong number, a card that did not draw, and every card in that thread.
  Production's 20,755 human timestamps all carry an offset today, so it was
  latent; it is closed anyway because this is the SQL trap already written
  down here one language up -- Python writes ISO8601 with a `T`, SQLite's
  `datetime('now')` writes a space and no offset -- and anything that ever
  puts one of those in `messages.created_at` would take the board out. UTC
  rather than refused, because everything writing one here already is, so
  attaching the offset is reading the value rather than guessing at it.
- **A card's buttons never leave the card, and they used to.** The footer was
  laid out chips-first and buttons-after, and an unwrapped `QLabel` cannot be
  made narrower than its own text -- so two amber issue chips claimed the row
  and Edit and Complete were pushed past the edge. Rendered and measured at
  the board column's own 463px minimum: one chip laid **Complete out at
  x=470 on a 463px card**, and two needed 850px, over even at the full 846.
  **13 of production's 50 open cards carry an issue chip and 4 carry two**,
  so this was live on a quarter of the board. Nothing reported it because
  nothing had failed -- the layout did exactly what it was asked, and what
  went missing went off-screen. It is the feed row's finding on a card, and
  it resolves the same way: **the text gives way, the controls never do.**
  `_fit_foot` fits the whole row before a widget is placed, which is why the
  buttons are built before they are added. What gives way, in order: the age
  drops its **words**; then a *second* chip is dropped, its text moving into
  the one that stays; then the age goes **altogether**; and only then is the
  survivor cut, with the whole of it on hover. Nothing is ever dropped in
  silence -- whatever is not on the row is in the tooltip of what is.
  **The age goes before the chip does**, because it is context and an amber
  chip is the card asking for somebody. Found by measuring rather than
  reasoned to: at the column's 463px minimum, keeping `12d` left the chip
  **one pixel** under its floor, so the issue and its tooltip went off the
  card to make room for a number. Verified across 240 renders -- both themes,
  five ages, six widths, four issue combinations -- that nothing lands off
  the card, no chip draws empty, and every issue is either readable in full
  or in a tooltip.
  **Two stubs say less than one chip.** `eq...` beside `cl...` is two amber
  shapes and no information, so `CARD_ISSUE_MIN_W` is the width a chip needs
  to carry its **first word** -- measured, `equipment` and `client cr` are
  124px each, and those first words are the whole of what tells the two
  issues apart. Below that the second chip goes rather than both being cut to
  nothing.
  **Room is shared, not split evenly.** A chip that already fits keeps its
  own width and hands the difference on, so `client cr not found` is not
  shortened to pay for a neighbour with room to spare. `_shares` settles it
  by going round until nothing more fits, which for two chips is at most
  twice.
- **The client filter is a dropdown, and it groups by the customer rather
  than the spelling.** Four kinds of equipment fit across a row as chips; the
  open board carries **35 distinct client names over 53 tickets**, which is a
  list you scan rather than a set of buttons you read. It sits at the far end
  of the same row, which already reads left to right as "narrow by ...".
  **Keyed on the resolved client.** Four of those 35 names are one customer --
  `bravon`, `bravo`, `Bravo` and `Bravo Environmental`, seven tickets between
  them -- so a filter keyed on the string as typed would offer four Bravos
  and never show those seven together. That is precisely the mess the roster
  and `client_aliases` exist to undo, and a strange place to start ignoring
  them. `client_key()` resolves through the alias table, which
  `reconcile_aliases` wrote by way of each ticket's Client CR key: an exact
  answer, never a resemblance. A name that resolves to nobody is its own
  entry, because a customer exists before Jira hears about them and their
  tickets still have to be findable, and a ticket naming nobody gets one too.
  **A misspelling keeps its own entry, in red.** Grouping is what makes
  `bravon` and `Bravo Environmental` one answer, and it is also what makes
  the mistake *disappear* -- a filter list that hides the thing you would
  want to repair quietly protects it. So a provably wrong spelling is listed
  separately, drawn in `T.RED_FG`, and still pickable: choosing it shows the
  four tickets that still carry it, which is how you find them to fix them.
  Interleaved alphabetically rather than gathered at the end, so a slip sorts
  beside the name it is a slip at.
  **Provably wrong, not merely unfamiliar.** `client_typos()` flags a
  spelling only when the roster knows who it means *and* it is not a
  shortening -- `client_stands_for` is the same line `client_resolve` draws,
  so `Bravo` never goes red and `bravon` does. A name Jira has never heard of
  is left alone: it may be a customer who exists before Jira hears about
  them, and marking that as an error would be the board being wrong about the
  world rather than the other way round.
  The counts deliberately do not add up -- `bravon (4)` is a subset of
  `Bravo Environmental (7)` -- which is the equipment chips' rule, not the
  figures panel's: a filter says how much is behind each way of narrowing,
  and these two narrow differently on purpose.
  **Client first, then equipment**, because that is the broader question:
  *whose* tickets before *which kind*. Read the other way round the row asks
  somebody to pick a piece of equipment before they have said who they are
  looking at, which is not the order anyone arrives with. `_filter_row` is
  named for what it holds rather than for the half that was built first.
  **It sits beside the chips, and `Show all` sits after both.** The two
  narrow the same board in the same way, so the control doing the same job
  belongs where the eye already is rather than across the row. `Show all`
  went last and now clears **both**: between the chips and the dropdown it
  pushed that dropdown **124px to the right** the moment a chip went on,
  under the pointer of somebody about to use it. Last in the row it moves
  nothing, and "stop narrowing" reading over the whole row is truer than
  having it speak for the chips alone. It appears when either half is
  narrowing.
  **One at a time**, unlike the chips: a ticket has several pieces of
  equipment and exactly one customer, so "any of these" is a question nobody
  asks here. Alphabetical, because 35 entries is a list somebody scans for a
  name they already have in mind and one that reorders itself as the board
  moves is one you cannot learn. Counted off the **whole** board for the two
  reasons `queue_counts` gives. And the list is rebuilt only when it would
  read differently -- `render()` runs on every poll and every drag, and a
  combo rebuilt under somebody's pointer loses the popup they had open, which
  is why the figures panel builds its window selector once and leaves it
  outside the body it throws away. A client whose last open ticket closes
  falls back to the whole board rather than leaving it pinned to nobody with
  no way back.
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

## Themes and the palette

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

**A container's stylesheet must name the container** -- and a container that
only holds things is better off with no stylesheet at all. The editor wraps
two of its fields in a QWidget so a note can sit under them, and both carried
`background:transparent` with no selector: `Combo` has no sheet of its own, so
the rule reached the Client box and took its fill away. The field beside it
survived the identical line only because a QLineEdit is handed `field()`
directly and its own rule wins, which is a latent version of the same bug. The
line was doing no work either -- a plain QWidget paints nothing without
`WA_StyledBackground`, so transparent is already what it does.
`tests/check_palette.py` holds the wrappers to carrying none.

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

**A theme previews as it is picked.** It is the one setting nobody can
judge from its name, and the dialog used to ask for it and show the answer
only after OK -- so choosing was a guess, and changing your mind meant
opening the window again. Picking one now closes the dialog with
`SettingsDialog.PREVIEW`, rebuilds the board in that palette and opens the
dialog straight back on top of it, which from the outside reads as the
control simply working.
It has to go through the whole rebuild, because that is the only restyle
there is -- every stylesheet is written where its widget is made, so there is
no sheet to swap. The dialog is closed and reopened rather than kept, because
the window it was parented to is the one being replaced.
**Nothing is stored until OK**: a preview only calls `apply_theme`, and the
fresh window loads what is on disk, so Cancel has something true to go back
to and a board previewed into a theme nobody chose puts itself right. What
had been typed rides across in `pending`, or looking at a colour would cost
you a half-entered name.
**And a window on its way out has to tell its deferred work.** `render()` and
`_render_feed()` post `QTimer.singleShot`s to put their scrollbars back, and
a `singleShot` cannot be stopped the way the poll timers can: they fired on
the next turn of the loop, by which time the layouts and scrollbars were
deleted C++ objects, and printed two tracebacks per rebuild. Survivable while
a rebuild was something nobody did twice in a day; a preview does it on every
pick. `_gone` is set beside `_swapping_theme` and both callbacks check it.

**Dark is what a board opens as.** `THEME_DEFAULT` is `dark`, not `system`.
Light was reported as tiring across four rounds by the person who uses this
all day, and dark is the one measured as restful -- mean luminance 0.012 with
95% of the screen inside a single band, against light's spread over three.
Following the desktop would hand a fresh install whichever the machine
happened to be set to, which is a coin toss on the question that has taken
the most work to answer. Both other choices are one dropdown away in
Settings, and it is only the fallback when the key is absent, so anybody who
has already chosen keeps their choice.

**Light is pitched where real light modes are pitched, and that is up.**
Measured against five of them -- GitHub, Linear, Notion, Atlassian, Stripe --
every one puts its canvas at luminance **1.00**, its panels at 0.92-0.95, its
borders at 0.69-0.80 and its text at **12-18:1**. This palette sat at 0.59,
0.55, 0.41 and 9.5:1: roughly **half the luminance of any of them**.
That was not a light mode, it was a dimmed one, and the muddy middle is the
worst place to be -- too dark to read as crisp, too light to read as restful.
It arrived one reasonable step at a time: light was reported as glaring, the
answer each round was to bring the field down, and three rounds of that
walked away from the thing being asked for. **Real light modes answer glare by
going up** -- a white ground, near-black text at 15:1, and comfort out of
crispness rather than dimness. Colour appears on small things, structure
comes from hairlines, and none of them tint the field.
**The structure was never wrong**, which is why this was a re-pitch rather
than a redesign: card over ground was 1.12 against their 1.05-1.08, and
border off fill 1.56 against their 1.23-1.43. It only needed moving up an
octave. After: canvas 0.85, cards white, text 15.1:1, **92.8% of the screen
reading as grey** and a mean luminance of 0.766.
**Every value is solved against the floors `check_palette.py` already held**,
rather than chosen by eye -- a fill of 0.96 puts the canvas at or under 0.886
to clear `CARD_MIN`, surface over canvas lands inside 1.10-1.30, a control
sits `CONTROL_MIN` under the fill it is on, and a tag stripe stands
`EDGE_MIN` off its own fill. That last one pays for itself twice: **the
figures panel draws a tag's stripe as its label**, so the same constraint that
keeps a border visible on a white card is what keeps that text readable. A
first pass with pale borders looked right and had unreadable tag labels; the
checks would have caught it either way.
**And the tag moved from the fill to the edge.** On a white card the fill is a
whisper by design, so `check_lights_chrome_is_grey_and_nothing_hides_in_it`
measures whichever of fill or stripe is carrying the tag rather than the fill
alone -- the invariant is that a tag is legible against the room in colour and
not only in lightness, not that any particular token carries it.

**Light's chrome is grey, and it is the only grey in the window.** A census
of the rendered board counted **98.5% of the screen at chroma 10 or more, and
1.5% reading as grey** -- the chrome alone was 68% of it, every token at
chroma 13-16 and every one of them blue. That is a coloured application with
more colour on top, rather than a grey one with colour where colour means
something.
Reported as everything looking too cool, which was the right instinct with
the wrong cause: **dark sits at the same hue**, 213-216 against light's
212-217. What differs is the cost. Light's chrome emits **38x** the blue
dark's does, so a tint invisible on a near-black surface is a wash on a
bright one -- the same shape as the two findings before it, where the value
was right and the quantity was not.
**Every luminance is unchanged**, which is what made it safe: each token is
the grey of exactly its old luminance, so every ratio in this section is
still the one it was measured at. Card over workspace 1.12, ink on a card
10.45, muted on the workspace 5.67, hairline 1.57. After: 70.8% of the screen
reads as grey.
**Grey rather than warm, and that is the half that decided it.** Measured as
chromatic distance from each card fill to the workspace it sits on, ignoring
lightness: a cool ground leaves **ENG and Medium at 1.1** -- the blue tags
were the same colour as the room -- and a warm one at hue 34 leaves **PROD
and High at 1.7**. A ground with a hue of its own hides whichever tags share
it, so warm would have moved the failure rather than removed it. Grey's worst
is 4.0, and its only near-zero is `low`, which is the colourless band and is
meant to stand off by lightness alone. `check_palette.py` holds all of it.
**Dark keeps its cast on purpose** -- the same tint costs it almost nothing,
and its neutrals are what its whole bottom end is built from.

**Dark's quality is that the eye rests on one value, and that is what light
was missing.** Measured over a rendered board, 95% of dark's pixels sit in a
single luminance band and only text and accents rise out of it -- mean 0.026.
Light was three bright bands, 45% at 0.6, 31% at 0.8 and 19% at 0.7, mean
0.671: twenty-six times the light, spread so there was nowhere to rest. The
31% was the cards, which is what got noticed.

So the whole ramp moved down together, and **together** is the load-bearing
word: bringing the cards down alone would have closed the step that says a
card is a thing sitting on a surface. Every ratio that carries meaning is
unchanged -- card over workspace still 1.12, border still 4.2x off its own
fill -- and the board simply emits less. After: **mean 0.552, 56% in one
band.** Two things had to move with it. The fills' *saturation* came down,
because the same HLS saturation yields more chroma at a lower lightness and
left alone would have taken them to 27-44 against the 15-26 they were. And
the inks came down, because at the old values five of them fell under 4.5:1
on the new grounds -- accent worst at 3.3. **Moving a ground without moving
the ink on it is how a palette goes quietly unreadable.**

**The cards came down to the room, not the room up to the cards.** Reported
a third time, and the diagnosis was sharper than mine: the chrome was fine by
then and the glare was coming off the *card surfaces* -- 0.91 luminance on a
0.79 ground, a near-white slab taking up most of the window, with the Undo
button a white object sitting on a darker bar doing the same thing in
miniature. So `surface` is `#E3E6EA` now and the fills are mixed **onto that
base rather than onto white**, which is what keeps them a tint of the room
instead of a pastel block dropped into it: chroma 15-26 against 6-12 before,
because on a near-white card that much colour glared and on this one it reads
as the tag. The card stands **1.12x** over the workspace -- a step, where it
was a jump -- and the border still carries the tag at 3.8x its own fill. The
brightest thing in the light palette is now the card, at 0.79.

**Five levels in light, and the workspace is the board column alone.**
(The values below were the mid-grey pitch and have moved up an octave; the
order and the roles are unchanged, which is the part that matters.)
Darkest first: `well` is the outer chrome -- the toolbar, the floor either
side of the centred board column, the space a folded panel leaves; `feed` is
the activity bar, which recedes furthest of the sections so the history is
quiet when nobody is reading it; `panel` is the running order and the figures;
`canvas` is the workspace, which is `#boardColumn` and nothing else; `surface`
is the cards. `feed` is 1.04 against `panel` -- near the edge of what an eye
picks up, kept because the direction is right even where the size is
marginal.
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

**The literal rule is checked now, and was not before.** "No colour literals
in Bert" has been written here a long time and was kept by hand, which is to
say not kept: `#EAF1FA` was the hover on the Undo button, on the running
order's fold button and on the figures' -- a pale blue that works in light and
is a flare on a dark toolbar, in whichever theme nobody happened to be looking
at. Exactly the failure the rule exists to prevent, sitting there through
three passes over this palette. `check_no_colour_is_written_by_hand` walks
every string constant outside the two palette definitions, skipping
**docstrings** (the reasoning for a colour often quotes the measurement that
chose it) and requiring a word boundary so `&#8594;` stays an arrow rather
than becoming `#859`.
