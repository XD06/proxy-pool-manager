#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path("config/app.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
print(data.get("port") or 9000)
PY
)"

echo "Web service:"
if [ -f tmp/server.pid ] && kill -0 "$(cat tmp/server.pid)" 2>/dev/null; then
  echo "  pid $(cat tmp/server.pid)"
else
  echo "  not running by pid file"
fi

curl -fsS "http://127.0.0.1:${PORT}/api/status" || true
echo

echo "Project sing-box:"
CONFIG_PATH="$ROOT/config/sing-box.json"
if command -v pgrep >/dev/null 2>&1; then
  pgrep -af "sing-box.*$CONFIG_PATH" || echo "  not running"
else
  echo "  pgrep not available"
fi
