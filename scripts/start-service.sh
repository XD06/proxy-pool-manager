#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p tmp

read_config() {
  local key="$1"
  local default_value="$2"
  python3 - "$key" "$default_value" <<'PY'
import json
import sys
from pathlib import Path

key, default = sys.argv[1], sys.argv[2]
path = Path("config/app.json")
if not path.exists():
    print(default)
    raise SystemExit
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
print(data.get(key) or default)
PY
}

HOST="${PPM_HOST:-$(read_config host 127.0.0.1)}"
PORT="${PPM_PORT:-$(read_config port 9000)}"
PROXY_LISTEN_HOST="${PPM_PROXY_LISTEN_HOST:-$(read_config proxy_listen_host 127.0.0.1)}"
PROXY_PUBLIC_HOST="${PPM_PROXY_PUBLIC_HOST:-$(read_config proxy_public_host "")}"

if [ -f tmp/server.pid ] && kill -0 "$(cat tmp/server.pid)" 2>/dev/null; then
  echo "Proxy Pool Manager is already running, pid $(cat tmp/server.pid)"
  exit 0
fi

export PPM_HOST="$HOST"
export PPM_PORT="$PORT"
export PPM_PROXY_LISTEN_HOST="$PROXY_LISTEN_HOST"
export PPM_PROXY_PUBLIC_HOST="$PROXY_PUBLIC_HOST"

PYTHON_BIN=".venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

nohup "$PYTHON_BIN" main.py > tmp/server.out.log 2> tmp/server.err.log &
echo $! > tmp/server.pid
sleep 2

echo "Started Proxy Pool Manager: http://${HOST}:${PORT}"
curl -fsS "http://127.0.0.1:${PORT}/api/status" || true
echo
