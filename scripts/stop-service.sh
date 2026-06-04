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

CONFIG_PATH="$ROOT/config/sing-box.json"
if command -v pgrep >/dev/null 2>&1; then
  pgrep -f "sing-box.*$CONFIG_PATH" | while read -r pid; do
    kill "$pid" || true
    echo "Stopped project sing-box, pid $pid"
  done
fi
