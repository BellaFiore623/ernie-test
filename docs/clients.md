# The customer list

Client names were typed into thread titles by hand and drifted.
This is the Jira roster that fixed it, how a spelling resolves to a
customer, and why nothing here ever guesses.

Split out of CLAUDE.md on 2026-09-16, word for word. CLAUDE.md keeps the
rules and points here for the why.

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
  Paso` and `IPI : *REP*` both read as `IPI`. **Both are kept and both are
  offered** -- asked and answered 2026-09-10: keep them, a rule for telling
  them apart comes later. Nothing refuses to write the second row and nothing
  drops one from the dropdown, because silently hiding one of two live
  customers is the wrong-customer failure this whole feature exists to stop.
  `/clients/roster` flags the rows and sends the full summary along, and
  `client_label()` shows it beside the short name, which is what tells them
  apart: `IPI · IPI : El Paso`.
- **A known collision stops shouting.** It was printed on every hourly pull,
  which for something already known and deliberately accepted is scenery --
  and an alarm that fires for ever is the one nobody reads when a new one
  turns up. `note_collisions()` remembers the set in `client_collisions`
  (one row, like `state_format_skew`) and only a **change** speaks. Clearing
  speaks too, so the log says when it went away as well as when it came, and
  the set is keyed order-independently: the order a query happens to return
  two client ids in is not news.
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
- **A single unambiguous candidate is taken; two are never chosen between.**
  `bravon` used to save as `bravon`, with *Bravo Environmental* sitting in
  the box the whole time. Somebody who knows they have mistyped picks from
  the list -- the typos that survive are the ones nobody noticed, which is
  precisely the case the list could not help with. `client_resolve()` takes
  the candidate when there is **exactly one**, and does nothing at all when
  there are two: `dukes` returns Duke's Omaha and Duke's Root Control, and
  choosing between two customers with nobody watching is the wrong-customer
  failure this whole feature exists to stop. The same line
  `reconcile_aliases` holds when it refuses to merge on resemblance.
  **Three things are left exactly as typed**: a name the roster already
  knows, aliases included, because it already resolves; a name with no
  candidates at all, because a customer exists before Jira hears about them;
  and a name the editor *opened* with, because a card that arrived carrying
  an unknown client is not a mistake anybody is making now -- the same guard
  `client_note` uses, and what keeps `is_dirty` honest, since opening a card
  must never make it dirty.
  **A typo that became an alias is still a typo**, and that took finding.
  Saving `bravon` once put it on a thread carrying Client CR `PIP-2165`, so
  `reconcile_aliases` resolved the spelling **through the key** -- no strings
  compared, confidence 1.0 -- and `bravon` became a recorded alias of Bravo
  Environmental. `client_known` counts aliases, which is right for the
  caution and exactly wrong here, so every later `bravon` was left alone for
  being known: **the slip had taught the board that the slip was a
  spelling.** So this asks `client_stands_for()` instead, which is about the
  customer's own name rather than every string that resolves to them.
  Correcting *through* an alias is in fact the safest case there is -- an
  alias names one client outright, with nothing to guess.
  **But a shortening people type on purpose is not a slip.** 24 of the 65
  offered clients are typed shorter in titles than their Jira name -- `Trekk`
  for *Trekk Design Group* on 18 threads, `SCI` for *SCI Infrastructure LLC.*
  on 15 -- and expanding those would make every new title disagree with the
  ones already there, which is the reasoning that chose the short names in
  the first place. `client_stands_for` takes the name itself punctuation
  aside, a prefix, or a run of whole words in order: `Bravo` stands for Bravo
  Environmental and `bravon` stands for nothing, which is what makes one a
  shortening and the other a typo.
  **It is announced before it happens.** The note under the box reads *will
  be saved as Bravo Environmental* while somebody is still typing. A
  correction you can see coming is a help; the same correction found
  afterwards is the software having quietly changed what you wrote.
  **And it lands in the title**, because that is what `save` sends and what
  the thread is renamed to -- rewriting the box alone would change nothing,
  since `_suggest_title` stops rebuilding once somebody types in the title
  themselves. Only the client segment moves, only on a title that parses,
  and only while the title still carries the name that was corrected:
  somebody who typed a different client straight into the title meant that
  one.
- **A name the roster has never heard of says so, and is still accepted.**
  `client_note()` puts a caution under the box -- amber, because this is a
  pending state and not an error, and 6.4:1 against the palest card it can
  sit on. It is about what somebody **just typed**: a card that arrived
  carrying a retired customer is not a mistake anybody is making now, and
  warning every time it is opened is nagging. An alias counts as known --
  `Dukes Root Control` is a misspelling the board has used nine times and the
  alias table already points it at PIP-8605, so it is a name that resolves.
- **The alternative was measured and cost more than it saved.** Making the
  title read-only and composing it from the fields eliminates the typo, and
  against production: **ten cards carry titles the fields cannot hold** -- no
  prefix, no parseable date, no structure -- and the title box is the only
  way to repair one, which is what the red edge asks for. Worse, **twenty-nine
  more would be silently renamed on the next save**, `04aug26` becoming
  `04Aug26`, each one a real thread rename at two per ten minutes posting a
  system message into a customer thread. Bert is also not the only writer:
  36 people open threads in Discord by hand, so unreadable titles keep
  arriving whatever the editor does.
- The dropdown is **pick-or-type**. A customer exists before Jira hears about
  them, and a card already carrying an unoffered client keeps it -- but it is
  **shown, not offered**: put in the box, never added to the list. It used to
  be an item, and an item is something you can pick, so a card whose title
  read `Trafford NJKNKNKNLN` put that in the dropdown beside the customers
  Jira knows about. Bert cannot tell a retired client from a fat-fingered one
  -- both are just a string the roster has never heard of -- and what it can
  do is stop dressing the second one up as a choice. `text()` reads the line
  edit, so `save()` and `is_dirty()` see it either way, which is what makes
  showing it enough.
- **The fuzzy tier compares words as well as whole strings.** Comparing only
  whole against whole punishes a name for the half that has not been typed
  yet: `trafforf` against `traffordborough` is 0.61, under the 0.72 floor,
  and the miss is the missing half rather than the wrong letters -- against
  the *word* `trafford` it is 0.93. Typing on past the name hid the same way,
  and that is how it was reported: `Trafford NJKNKNKNLN`, aiming for Trafford
  Borough, offered nothing at all. Words under `FUZZY_WORD_MIN` are never
  compared loosely, because `SCI`, `RJN` and `GFT` are whole customer names
  and at three letters almost anything resembles almost anything.
  A wider net is still only a net: `dukes` returns **both** Duke's, and
  `falmouth` both Falmouths. Offering is not deciding, and
  `reconcile_aliases` is untouched.
