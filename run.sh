#!/usr/bin/env bash
# Start the whole stack in one command.
#
#   ./run.sh test        sandbox guild, writes enabled, port 8788
#   ./run.sh prod        real guild, read-only sync, port 8787
#   ./run.sh test bert   same, and open Bert too
#
# Ctrl+C stops everything. Logs land in logs/.

set -uo pipefail
cd "$(dirname "$0")"

ENVNAME="${1:-test}"
WITH_BERT="${2:-}"

case "$ENVNAME" in
  test) ENVFILE=ernie-test.env; DB=ernie-test.db; PORT=8788; OUTBOX=yes ;;
  prod) ENVFILE=ernie.env;      DB=ernie.db;      PORT=8787; OUTBOX=no  ;;
  *) echo "usage: ./run.sh [test|prod] [bert]"; exit 1 ;;
esac

[ -f "$ENVFILE" ] || { echo "missing $ENVFILE"; exit 1; }

mkdir -p logs
PIDS=()

stop() {
  echo ""
  echo "stopping..."
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
  echo "stopped"
  exit 0
}
trap stop INT TERM

start() {                       # start <name> <command...>
  local name="$1"; shift
  echo "  $name"
  "$@" >> "logs/$name.log" 2>&1 &
  PIDS+=($!)
}

echo "starting [$ENVNAME]  db=$DB  port=$PORT"

start sync   python ernie_sync.py --env "$ENVFILE" --db "$DB"
[ "$OUTBOX" = yes ] && start outbox python ernie_outbox.py --env "$ENVFILE" --db "$DB"
start api    python ernie_api.py --db "$DB" --port "$PORT"

sleep 2
if curl -s "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
  echo "  api ready on http://127.0.0.1:$PORT/docs"
else
  echo "  api not responding yet -- check logs/api.log"
fi

if [ "$WITH_BERT" = bert ]; then
  start bert python bert.py --api "http://127.0.0.1:$PORT"
fi

echo ""
echo "tailing logs, Ctrl+C to stop everything"
echo "---"
tail -f logs/*.log
