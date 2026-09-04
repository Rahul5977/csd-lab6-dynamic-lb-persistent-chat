#!/usr/bin/env bash
# Install a user-local static Node.js on a lab system that has none.
# Usage: bash scripts/install_node.sh lbsys2
# Idempotent: skips if ~/node/bin/node already runs.
set -euo pipefail
HOST="${1:?usage: install_node.sh <ssh-alias>}"
NODE_VER="v20.19.0"
URL="https://nodejs.org/dist/${NODE_VER}/node-${NODE_VER}-linux-x64.tar.xz"

ssh "$HOST" "
  set -e
  if ~/node/bin/node --version 2>/dev/null; then
    echo 'node already installed'; exit 0
  fi
  echo 'downloading ${NODE_VER}…'
  curl -fsSL -o /tmp/node.tar.xz '${URL}'
  rm -rf ~/node ~/node-tmp && mkdir -p ~/node-tmp
  tar xJf /tmp/node.tar.xz -C ~/node-tmp --strip-components=1
  mv ~/node-tmp ~/node
  rm -f /tmp/node.tar.xz
  ~/node/bin/node --version
"
