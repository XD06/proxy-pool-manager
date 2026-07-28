#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -f tmp/server.pid ]; then
  PID="$(cat tmp/server.pid)"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" || true
    echo "Stopped Web service, pid $PID"
  fi
  rm -f tmp/server.pid
fi

CONFIG_DIR="$ROOT/config"
if command -v pgrep >/dev/null 2>&1; then
  # Match every project-owned config (sing-box.json, sing-box-test.json, ...)
  # so temporary speed-test engines are not left running.
  pgrep -f "sing-box.*$CONFIG_DIR/" | while read -r pid; do
    kill "$pid" || true
    echo "Stopped project sing-box, pid $pid"
  done
  # Pool router is a separate helper process; leaving it running keeps its
  # listener ports occupied and breaks the next start.
  pgrep -f "pool-router.*$CONFIG_DIR/pool-router.json" | while read -r pid; do
    kill "$pid" || true
    echo "Stopped project pool-router, pid $pid"
  done
fi
