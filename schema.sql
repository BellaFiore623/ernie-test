-- Ernie / Bert schema
--
-- Two halves, deliberately separated:
--
--   MIRROR  -- append-only record of what Discord said. Never updated in
--              place. Discord is the source of truth, but Discord is
--              mutable, so we keep every revision we ever observed.
--
--   STATE   -- Bert's own data: cards, rank, statuses. Has no Discord
--              representation and is never derived from a re-sync.
--
-- Plus EVENTS, which is the spine: activity feed, undo, outbox and
-- history are all reads of this one table.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ==========================================================================
-- MIRROR
-- ==========================================================================

CREATE TABLE IF NOT EXISTS threads (
    thread_id       TEXT PRIMARY KEY,          -- snowflake, immutable identity
    parent_id       TEXT NOT NULL,
    guild_id        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL,
    last_seen_message_id TEXT,                 -- cursor for incremental sync
    archived        INTEGER NOT NULL DEFAULT 0,
    locked          INTEGER NOT NULL DEFAULT 0,
    archived_by_ernie INTEGER NOT NULL DEFAULT 0,
                    -- set when Ernie archived it on completion; lets the
                    -- reopen check tell a real revival from Ernie's own work
    deleted_at      TEXT                       -- soft delete; never DELETE a row
);

CREATE INDEX IF NOT EXISTS ix_threads_parent ON threads(parent_id);

-- Titles change (PROD <-> OPS especially), so every observed value is kept.
CREATE TABLE IF NOT EXISTS thread_titles (
    thread_id     TEXT NOT NULL REFERENCES threads(thread_id),
    observed_at   TEXT NOT NULL,
    name          TEXT NOT NULL,
    queue         TEXT,                        -- OPS | PROD | ENG | CS | DATA
    client_raw    TEXT,
    client_key    TEXT,                        -- normalised for matching
    thread_date   TEXT,                        -- ISO date parsed from title
    summary       TEXT,
    confidence    TEXT NOT NULL,               -- strict | loose | prefix_only | none
    PRIMARY KEY (thread_id, observed_at)
);

CREATE INDEX IF NOT EXISTS ix_titles_client ON thread_titles(client_key);

-- Current title, for convenient joins.
CREATE VIEW IF NOT EXISTS v_thread_current AS
SELECT t.*, ti.name, ti.queue, ti.client_raw, ti.client_key,
       ti.thread_date, ti.summary, ti.confidence
FROM threads t
JOIN thread_titles ti ON ti.thread_id = t.thread_id
WHERE ti.observed_at = (
    SELECT MAX(observed_at) FROM thread_titles WHERE thread_id = t.thread_id
);

CREATE TABLE IF NOT EXISTS messages (
    message_id    TEXT PRIMARY KEY,
    thread_id     TEXT NOT NULL REFERENCES threads(thread_id),
    author_id     TEXT NOT NULL,
    author_name   TEXT,
    is_bot        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    deleted_at    TEXT                         -- set when a re-scan finds it gone
);

CREATE INDEX IF NOT EXISTS ix_messages_thread ON messages(thread_id, created_at);

-- Message bodies are revisioned: edits insert, never overwrite.
CREATE TABLE IF NOT EXISTS message_revisions (
    message_id    TEXT NOT NULL REFERENCES messages(message_id),
    observed_at   TEXT NOT NULL,
    edited_at     TEXT,                        -- Discord's edited_timestamp
    content       TEXT,
    embeds_json   TEXT,
    components_json TEXT,
    attachments_json TEXT,
    PRIMARY KEY (message_id, observed_at)
);

-- ==========================================================================
-- DERIVED  (recomputable from the mirror; safe to drop and rebuild)
-- ==========================================================================

-- Ticket panels posted by Python-Interface-Bot. A proposal is a draft until
-- a matching confirmation appears.
CREATE TABLE IF NOT EXISTS ticket_proposals (
    message_id       TEXT PRIMARY KEY REFERENCES messages(message_id),
    thread_id        TEXT NOT NULL REFERENCES threads(thread_id),
    kind             TEXT NOT NULL,            -- build | return
    proposed_at      TEXT NOT NULL,
    equipment_master TEXT,                     -- PIP-#### or NULL if not found
    equipment_label  TEXT,
    client_cr        TEXT,
    client_label     TEXT,
    client_key       TEXT,
    equipment_type   TEXT,
    template         TEXT,
    assignee         TEXT,
    reporter         TEXT,
    reported_problem TEXT,
    has_buttons      INTEGER NOT NULL DEFAULT 0,
    issues_json      TEXT NOT NULL DEFAULT '[]',
    prompt_version   INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS ix_prop_thread ON ticket_proposals(thread_id);

-- The authoritative "this ticket exists" signal.
CREATE TABLE IF NOT EXISTS tickets (
    pip_key          TEXT PRIMARY KEY,
    thread_id        TEXT NOT NULL REFERENCES threads(thread_id),
    message_id       TEXT NOT NULL REFERENCES messages(message_id),
    kind             TEXT,                     -- build | return
    created_at       TEXT NOT NULL,
    assignee         TEXT,
    equipment_master TEXT,
    client_cr        TEXT
);

CREATE INDEX IF NOT EXISTS ix_tickets_thread ON tickets(thread_id);

CREATE TABLE IF NOT EXISTS thread_equipment (
    thread_id  TEXT NOT NULL REFERENCES threads(thread_id),
    eq_type    TEXT NOT NULL,                  -- EReel | ODE | SSD | LED | OLK
    eq_number  TEXT,                           -- NULL when '####' placeholder
    state      TEXT NOT NULL,                  -- resolved | pending | malformed
    raw        TEXT NOT NULL,
    PRIMARY KEY (thread_id, raw)
);

-- ==========================================================================
-- STATE  (Bert's own; never overwritten by a Discord re-sync)
-- ==========================================================================

CREATE TABLE IF NOT EXISTS cards (
    thread_id    TEXT PRIMARY KEY REFERENCES threads(thread_id),
    priority     TEXT NOT NULL DEFAULT 'unassigned',
                 -- unassigned|critical|high|medium|low
                 -- new threads land in 'unassigned' until a human drags them
    rank         REAL NOT NULL,                   -- fractional, within priority
    -- Superseded by work_items. Bert no longer shows or edits these four;
    -- migrate_work_items.py copied their values across as bubbles. Kept
    -- because old 'edited' events name them and undo still has to restore
    -- one if somebody reaches that far back in the feed.
    build_state  TEXT NOT NULL DEFAULT 'needs_created',
    return_state TEXT NOT NULL DEFAULT 'needs_created',
    direction    TEXT,                            -- leaving | coming_back
    action_item  TEXT,
    client_override TEXT,          -- set in Bert when the parsed client is wrong
    completed_at TEXT,
    completed_by TEXT,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cards_sort ON cards(priority, rank);

-- The bubbles on a card. What used to be one action_item string is a list:
-- one row per item, typed in Bert, with no Discord representation.
--
-- Rows are never deleted. Ticking an item off in view mode sets done_at, and
-- x-ing one out in the editor sets removed_at -- two different statements
-- ("this happened" vs "this shouldn't be here"), and both are undoable from
-- the feed, which needs the row still there to put back.
CREATE TABLE IF NOT EXISTS work_items (
    item_id    TEXT PRIMARY KEY,             -- uuid, minted by the API
    thread_id  TEXT NOT NULL REFERENCES threads(thread_id),
    body       TEXT NOT NULL,                -- what the person typed
    position   REAL NOT NULL,                -- order on the card
    created_at TEXT NOT NULL,
    created_by TEXT,
    done_at    TEXT,
    done_by    TEXT,
    removed_at TEXT,
    removed_by TEXT
);

CREATE INDEX IF NOT EXISTS ix_work_thread ON work_items(thread_id, position);

-- Learned client resolutions. Turns fuzzy matching into exact lookup.
CREATE TABLE IF NOT EXISTS client_aliases (
    raw_key     TEXT PRIMARY KEY,             -- normalised source string
    client_id   TEXT NOT NULL,                -- canonical client
    confidence  REAL,
    resolved_by TEXT,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    client_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    name_key    TEXT NOT NULL,
    synced_at   TEXT
);

-- No roster table. Each Bert install stores the user's first + last name in
-- its own settings and sends it with every write; Ernie drops that string
-- straight into the thread message. Plain text, no Discord mention.

-- ==========================================================================
-- EVENTS  (activity feed + undo + outbox, one table)
-- ==========================================================================

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,          -- client-generated UUID
    occurred_at     TEXT NOT NULL,
    actor_name      TEXT,                      -- free text from Bert settings
    thread_id       TEXT REFERENCES threads(thread_id),
    verb            TEXT NOT NULL,             -- completed | reranked | ...
    old_value       TEXT,
    new_value       TEXT,
    undone_at       TEXT,
    undone_by       TEXT,
    -- outbox columns; NULL dispatch_after means "never post to Discord"
    dispatch_after  TEXT,
    claimed_at      TEXT,                      -- outbox holds the row while it
                                               -- talks to Discord, so an undo
                                               -- can't race a message in flight
    posted_at       TEXT,
    discord_message_id TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);

CREATE INDEX IF NOT EXISTS ix_events_feed ON events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_thread ON events(thread_id, occurred_at DESC);

-- Rows Ernie should post now: past the undo window, not undone, not sent.
-- What this machine last agreed with the state channel about, per card.
-- Conflict resolution compares against this rather than comparing two
-- machines' clocks: a laptop whose clock is wrong would otherwise win or
-- lose every tie, silently, with nothing to show for it.
CREATE TABLE IF NOT EXISTS state_sync (
    thread_id   TEXT PRIMARY KEY REFERENCES threads(thread_id),
    message_id  TEXT,                       -- the card's message in the channel
    base_json   TEXT NOT NULL,              -- payload as of the last agreement
    synced_at   TEXT NOT NULL
);


-- Which events have already been written to the change-log channel.
-- Per event rather than a high-water mark: a change replayed from the other
-- board carries the timestamp it originally happened at, so events do not
-- arrive in occurred_at order and a cursor would step over them.
-- Whether the change log has ever run against this database. A row count in
-- changelog_sent can't answer that: on a fresh database it is empty because
-- there was no history to skip, not because the log has never started, and
-- treating those the same means the first real change gets marked as already
-- logged and never posted.
CREATE TABLE IF NOT EXISTS changelog_state (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    started_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS changelog_sent (
    event_id   TEXT PRIMARY KEY REFERENCES events(event_id),
    message_id TEXT,
    sent_at    TEXT NOT NULL
);


CREATE VIEW IF NOT EXISTS v_outbox_due AS
SELECT * FROM events
WHERE dispatch_after IS NOT NULL
  AND posted_at IS NULL
  AND undone_at IS NULL
  AND claimed_at IS NULL
  -- datetime() on both sides: Python writes ISO8601 with a 'T' and an
  -- offset, SQLite's datetime('now') uses a space and no offset, so a raw
  -- string compare is always false and nothing would ever post.
  AND datetime(dispatch_after) <= datetime('now')
  AND attempts < 5
ORDER BY occurred_at;

-- Idempotency for writes that cross the network.
CREATE TABLE IF NOT EXISTS write_keys (
    idempotency_key TEXT PRIMARY KEY,
    result_json     TEXT,
    created_at      TEXT NOT NULL
);

-- ==========================================================================
-- SYNC BOOKKEEPING
-- ==========================================================================

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    threads_seen  INTEGER DEFAULT 0,
    messages_new  INTEGER DEFAULT 0,
    edits_found   INTEGER DEFAULT 0,
    titles_changed INTEGER DEFAULT 0,
    error         TEXT
);

-- ==========================================================================
-- CHANNEL CONFIG
-- ==========================================================================

-- Which channels Ernie reads, and which produce cards on Bert's board.
-- customer-support is mirrored for history/search but generates no cards.
CREATE TABLE IF NOT EXISTS watched_channels (
    channel_id     TEXT PRIMARY KEY,
    name           TEXT,
    mirror         INTEGER NOT NULL DEFAULT 1,
    generate_cards INTEGER NOT NULL DEFAULT 0,
    backfilled_at  TEXT               -- set once archived history is pulled
);

-- No seed rows here on purpose. Channel ids are per-environment config, and
-- this file is applied on every connect(), so hardcoding them pointed every
-- new database -- test ones included -- at production's channels. They are
-- registered from CARD_CHANNEL_IDS / HISTORY_CHANNEL_IDS at sync startup.
