#!/usr/bin/env bash
# Start the whole stack in one command.
#
#   ./run.sh test            sandbox guild, writes enabled, port 8788
#   ./run.sh prod            real guild, read-only sync, port 8787
#   ./run.sh test bert       same, and open Bert too
#   ./run.sh test bert lan   same, with the API reachable from other machines
#
# `lan` is for testing with somebody else: they run only Bert, pointed at this
# machine, so there is one database and one board between you. The API has no
# authentication, so anyone who can reach the port can move cards and post to
# the thread -- a trusted network, for as long as the test lasts, and never
# port-forwarded.
#
# Ctrl+C stops everything. Logs land in logs/.

set -uo pipefail
cd "$(dirname "$0")"

ENVNAME=test
WITH_BERT=
HOST=127.0.0.1                  # loopback unless asked otherwise

for arg in "$@"; do
  case "$arg" in
    test|prod)  ENVNAME="$arg" ;;
    bert)       WITH_BERT=bert ;;
    lan|--lan)  HOST=0.0.0.0 ;;
    *) echo "usage: ./run.sh [test|prod] [bert] [lan]"; exit 1 ;;
  esac
done

case "$ENVNAME" in
  test) ENVFILE=ernie-test.env; DB=ernie-test.db; PORT=8788; OUTBOX=yes ;;
  prod) ENVFILE=ernie.env;      DB=ernie.db;      PORT=8787; OUTBOX=no  ;;
esac

# The API has no password on it. Opening the sandbox to the network for an
# afternoon is one thing; doing it to the guild that posts to real customer
# threads is another.
if [ "$ENVNAME" = prod ] && [ "$HOST" = 0.0.0.0 ]; then
  echo "refusing: 'lan' puts the API on the network with nothing guarding it."
  echo "That is for the test environment. Production stays on this machine."
  exit 1
fi

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
start api    python ernie_api.py --db "$DB" --port "$PORT" --host "$HOST"

sleep 2
if curl -s "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
  echo "  api ready on http://127.0.0.1:$PORT/docs"
else
  echo "  api not responding yet -- check logs/api.log"
fi

if [ "$HOST" = 0.0.0.0 ]; then
  # The address of the interface that reaches the outside world. Not
  # `hostname`, which on Windows answers with a name nobody else can resolve.
  LAN_IP=$(python -c "import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(('8.8.8.8', 80))
    print(s.getsockname()[0])
except OSError:
    print('')
finally:
    s.close()" 2>/dev/null)

  echo ""
  if [ -n "$LAN_IP" ]; then
    echo "  reachable from other machines at http://$LAN_IP:$PORT"
    echo "  hand the other tester: $LAN_IP:$PORT"
    echo "  (they put that into bert.cmd, or run:"
    echo "     python bert.py --api http://$LAN_IP:$PORT )"
  else
    echo "  bound to every interface, but this machine's address"
    echo "  couldn't be worked out -- check ipconfig"
  fi
  echo "  Windows will ask once to allow Python through the firewall."
  echo "  Say yes for PRIVATE networks. There is no password on this."
fi

if [ "$WITH_BERT" = bert ]; then
  start bert python bert.py --api "http://127.0.0.1:$PORT"
fi

echo ""
echo "tailing logs, Ctrl+C to stop everything"
echo "---"
tail -f logs/*.log
