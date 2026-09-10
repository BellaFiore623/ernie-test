#!/usr/bin/env bash
# Start the whole stack in one command.
#
#   ./run.sh test            sandbox guild, writes enabled, port 8788
#   ./run.sh prod            real guild, read-only sync, port 8787
#   ./run.sh test bert       same, and open Bert too
#   ./run.sh test bert lan   same, with the API reachable from other machines
#   ./run.sh stop            stop a stack this script started
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

JUST_STOP=

for arg in "$@"; do
  case "$arg" in
    test|prod)   ENVNAME="$arg" ;;
    bert)        WITH_BERT=bert ;;
    lan|--lan)   HOST=0.0.0.0 ;;
    stop|--stop) JUST_STOP=yes ;;
    *) echo "usage: ./run.sh [test|prod] [bert] [lan] | ./run.sh stop"; exit 1 ;;
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
PIDFILE=logs/stack.pids

# Windows pids, not bash job numbers. Ctrl+C reaches the children through the
# process group and they die tidily; closing the terminal window runs no trap
# at all, and then sync and outbox go on running with nothing on screen to say
# so. taskkill ends the process and its children however it was started.
kill_pidfile() {
  [ -f "$PIDFILE" ] || return 0
  while read -r wpid; do
    [ -n "$wpid" ] || continue
    taskkill //PID "$wpid" //T //F >/dev/null 2>&1
  done < "$PIDFILE"
  rm -f "$PIDFILE"
}

alive_from_pidfile() {          # the pids in it that are still running
  [ -f "$PIDFILE" ] || return 0
  while read -r wpid; do
    [ -n "$wpid" ] || continue
    if tasklist //FI "PID eq $wpid" //NH 2>/dev/null | grep -qi python; then
      echo "$wpid"
    fi
  done < "$PIDFILE"
}

# Every Ernie process alive, whoever started it and whether or not this file
# knows about it. The pidfile alone was not enough and could not be: it is
# truncated on every start, so it only ever remembers the *previous*
# generation. Leave a stack running, start again, let that second one die --
# and the first becomes invisible to the guard for good, because the pids
# that knew about it have been overwritten.
#
# Found in the sandbox at ten syncs and ten outboxes, none of them in the
# pidfile, and one API from the first start of the day still holding the port
# so every later API exited on bind and the board was served yesterday's code
# for hours. The database survived it -- no duplicate cards, no duplicate
# state messages -- but the outbox log carries 18 real rate limits from the
# copies competing for one channel.
#
# The command line is the only thing that identifies these, so this finds
# Ernie processes from *any* clone on the machine. That is the right side to
# err on: two clones running two syncs against two databases and one Discord
# channel is the same problem wearing a different hat.
running_ernie() {
  powershell -NoProfile -Command "
    Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%'\" |
      Where-Object { \$_.CommandLine -match 'ernie_(sync|outbox|api)\.py' } |
      ForEach-Object { \$_.ProcessId }" 2>/dev/null | tr -d '\r'
}

stop() {
  echo ""
  echo "stopping..."
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null; done
  kill_pidfile
  kill_strays
  wait 2>/dev/null
  echo "stopped"
  exit 0
}

kill_strays() {                 # anything the pidfile never knew about
  local n=0 wpid
  for wpid in $(running_ernie); do
    taskkill //PID "$wpid" //T //F >/dev/null 2>&1 && n=$((n + 1))
  done
  [ "$n" -gt 0 ] && echo "  and $n process(es) no pidfile knew about"
  return 0
}
trap stop INT TERM

if [ -n "$JUST_STOP" ]; then
  n=$(running_ernie | grep -c . || true)
  kill_pidfile
  kill_strays >/dev/null
  echo "stopped $n process(es)"
  exit 0
fi

# Starting on top of a stack that is already up gives a second sync writing to
# the same database and a second outbox posting to the same Discord channel.
# Six of each were found running at once after a day of closing the window
# rather than pressing Ctrl+C -- which is where the outbox's 429s came from,
# and every start flashes a console window per process on the way past.
# Asked of the machine, not of this file. See running_ernie above for why the
# pidfile cannot answer this on its own.
LEFTOVER=$(running_ernie)
if [ -n "$LEFTOVER" ]; then
  echo "Ernie is already running:"
  for wpid in $LEFTOVER; do echo "  pid $wpid"; done
  echo ""
  echo "stop it first:  ./run.sh stop"
  exit 1
fi
: > "$PIDFILE"

start() {                       # start <name> <command...>
  local name="$1"; shift
  echo "  $name"
  "$@" >> "logs/$name.log" 2>&1 &
  PIDS+=($!)
  # /proc/<job>/winpid is Git Bash's map from its own pid to the Windows one,
  # and the Windows one is what outlives this script.
  local wpid
  wpid=$(cat "/proc/$!/winpid" 2>/dev/null)
  [ -n "$wpid" ] && echo "$wpid" >> "$PIDFILE"
}

echo "starting [$ENVNAME]  db=$DB  port=$PORT"

start sync   python ernie_sync.py --env "$ENVFILE" --db "$DB"
[ "$OUTBOX" = yes ] && start outbox python ernie_outbox.py --env "$ENVFILE" --db "$DB"
start api    python ernie_api.py --db "$DB" --port "$PORT" --host "$HOST" --env "$ENVFILE"

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
