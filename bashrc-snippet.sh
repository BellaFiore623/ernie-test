# Ernie / Bert shortcuts.
#
# Install once:
#     cat bashrc-snippet.sh >> ~/.bashrc && source ~/.bashrc
#
# Then `e` gets you to the project from anywhere.

export ERNIE_DIR="$HOME/ernie"

e()      { cd "$ERNIE_DIR" || return; }

# start the stack
run()    { (cd "$ERNIE_DIR" && ./run.sh "${1:-test}" "${2:-}"); }

# one-shot sync
sync()   { (cd "$ERNIE_DIR" && python ernie_sync.py --once \
             --env "ernie-${1:-test}.env" --db "ernie-${1:-test}.db"); }

# ad-hoc SQL:  q "SELECT COUNT(*) FROM cards"
q()      { (cd "$ERNIE_DIR" && python q.py "$1" "${2:-ernie-test.db}"); }

# what's on the board right now
board()  { q "SELECT ti.queue, c.priority, ti.name FROM cards c
              JOIN thread_titles ti ON ti.thread_id=c.thread_id
              WHERE c.completed_at IS NULL
              GROUP BY c.thread_id ORDER BY c.priority, c.rank"; }

# outbox state
out()    { q "SELECT verb, actor_name, dispatch_after, posted_at, attempts
              FROM events WHERE dispatch_after IS NOT NULL
              ORDER BY occurred_at DESC LIMIT 10"; }

# last few sync runs
runs()   { q "SELECT started_at, threads_seen, messages_new, error
              FROM sync_runs ORDER BY run_id DESC LIMIT 5"; }

# wipe and rebuild the sandbox from scratch
reseed() {
  (cd "$ERNIE_DIR" \
    && python wipe_test.py \
    && rm -f ernie-test.db \
    && python seed_test_server.py \
    && python ernie_sync.py --once --env ernie-test.env --db ernie-test.db)
}

# stop anything left running
kills()  { pkill -f "ernie_sync.py|ernie_outbox.py|ernie_api.py|bert.py" \
             && echo "stopped" || echo "nothing running"; }
