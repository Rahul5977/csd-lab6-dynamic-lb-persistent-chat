#!/usr/bin/env bash
# Run a command on all four lab systems with tagged output.
# Usage: bash scripts/ssh_all.sh 'hostname; date'
set -u
CMD="${1:?usage: ssh_all.sh '<command>'}"
for h in lbsys1 lbsys2 lbsys3 lbsys4; do
  echo "===== $h ====="
  ssh -o BatchMode=yes -o ConnectTimeout=6 "$h" "$CMD" 2>&1 | sed "s/^/[$h] /"
  echo "[$h] exit: $?"
done
