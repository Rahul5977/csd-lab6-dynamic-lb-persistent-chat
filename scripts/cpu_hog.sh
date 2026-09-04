#!/usr/bin/env bash
# cpu_hog.sh — burn CPU on one lab system to make its backend "loaded"
# (each container is capped at 1 core, so a busy loop halves what node gets).
#   bash scripts/cpu_hog.sh sys3 start|stop|status
set -u
SYS="${1:?usage: cpu_hog.sh sysN start|stop|status}"; ACTION="${2:?}"
host() { case "$1" in sys2) echo lbsys2;; sys3) echo lbsys3;; sys4) echo lbsys4;; esac; }
H=$(host "$SYS")
case "$ACTION" in
  start)  ssh "$H" 'cd ~/assignment6 && ([ -f hog.pid ] && kill -0 $(cat hog.pid) 2>/dev/null && echo "hog already running" || { nohup python3 -c "while True: pass" > /dev/null 2>&1 & echo $! > hog.pid; echo "hog started pid $!"; })' ;;
  stop)   ssh "$H" 'cd ~/assignment6 && [ -f hog.pid ] && kill $(cat hog.pid) 2>/dev/null && rm -f hog.pid && echo "hog stopped" || echo "no hog"' ;;
  status) ssh "$H" 'cd ~/assignment6 && [ -f hog.pid ] && kill -0 $(cat hog.pid) 2>/dev/null && echo "hog running" || echo "no hog"; uptime' ;;
esac
