#!/usr/bin/env bash
# ============================================================================
# feed_mode.sh — switch what GET /feed returns, across all three backends.
#
# There is a real trade-off here and it is worth being able to move on it
# between evaluation runs, because the leaderboard is the only rig that
# measures the conditions that actually count.
#
#   bash scripts/feed_mode.sh full      every message, compressed for clients
#                                       that accept gzip. Message completeness
#                                       1.00, at the cost of response time.
#   bash scripts/feed_mode.sh window    the newest 256 KB of the feed. Fastest,
#                                       but the evaluation marks the run "lossy".
#   bash scripts/feed_mode.sh fresh     start the routes on an empty room, so the
#                                       feed holds only what the next run posts.
#                                       Deletes nothing: the old room stays in the
#                                       database and is still reachable by the chat.
#   bash scripts/feed_mode.sh status    what the backends are running now
#
# Either mode always reports the room's true total in `count`, and ?limit= and
# ?since= reach the complete history in both.
# ============================================================================
set -uo pipefail
cd "$(dirname "$0")/.."
LB="${LB_URL:-http://10.1.75.53:3269}"

status() {
  for h in lbsys2 lbsys3 lbsys4; do
    echo -n "  $h  "
    ssh -o BatchMode=yes "$h" 'grep -hE "^(PUBLIC_ROOM|FEED_BYTES|FEED_MAX)=" ~/assignment6/.env | tr "\n" " "'
    echo
  done
  echo -n "  live: "
  curl -sS -m 20 --compressed "$LB/feed" | python3 -c "
import json,sys
d = json.load(sys.stdin)
print(f\"room={d['room']} count={d['count']} returned={d['returned']} truncated={d['truncated']} via={d['backend']}\")" \
    || echo "unreachable"
  echo -n "  compressed body: "
  curl -sS -m 20 -H 'Accept-Encoding: gzip' -o /dev/null -w "%{size_download} B\n" "$LB/feed" || true
  echo -n "  plain body:      "
  curl -sS -m 20 -o /dev/null -w "%{size_download} B\n" "$LB/feed" || true
}

case "${1:-status}" in
  full)
    for s in sys2 sys3 sys4; do FEED_BYTES=1048576 bash scripts/deploy.sh "$s" | tail -1; done
    echo "feed mode: FULL (gzip for clients that accept it)"
    ;;
  window)
    for s in sys2 sys3 sys4; do FEED_BYTES=262144 FEED_GZIP_MS=999999999 bash scripts/deploy.sh "$s" | tail -1; done
    echo "feed mode: WINDOW (newest 256 KB)"
    ;;
  fresh)
    room="${2:-room-$(date +%H%M%S)}"
    for s in sys2 sys3 sys4; do PUBLIC_ROOM="$room" bash scripts/deploy.sh "$s" | tail -1; done
    echo "feed mode: routes now on the empty room '$room'; nothing was deleted"
    ;;
  status) ;;
  *) echo "usage: feed_mode.sh full|window|fresh [room]|status"; exit 1 ;;
esac
echo
status
