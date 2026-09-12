#!/usr/bin/env bash
# ============================================================================
# Keep the load balancer running.
#
# It is a single process on a 512 MB container, and the kernel's OOM killer has
# taken it out in the middle of an evaluation run. Nothing restarted it, so the
# public URL — the only address clients have — stayed dead until a human noticed.
# One failed stage is recoverable. An endpoint that is down for hours is not.
#
# The loop restarts it immediately and records why it went, so the cause is still
# in the log afterwards instead of having to be inferred from silence.
# ============================================================================
cd "$(dirname "$0")/.."
ulimit -n 65536 2>/dev/null || true
while true; do
  python3 lb/loadbalancer.py lb/lb.conf.json >> lb.log 2>&1
  rc=$?
  oom=$(grep -o 'oom_kill [0-9]*' /sys/fs/cgroup/memory.events 2>/dev/null || echo 'oom_kill ?')
  printf '[lb] EXITED rc=%s at %s — %s — restarting\n' \
      "$rc" "$(date -u +%FT%TZ)" "$oom" >> lb.log
  sleep 1
done
