#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OK=0
WARN=0
FAIL=0

pass() { OK=$((OK + 1)); printf '[OK]   %s\n' "$*"; }
warn() { WARN=$((WARN + 1)); printf '[WARN] %s\n' "$*"; }
fail() { FAIL=$((FAIL + 1)); printf '[FAIL] %s\n' "$*"; }

json_value() {
  local key="$1"
  local default_value="$2"
  python3 - "$key" "$default_value" <<'PY'
import json
import sys
from pathlib import Path

key, default = sys.argv[1], sys.argv[2]
path = Path("config/app.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
print(data.get(key) or default)
PY
}

check_command() {
  local name="$1"
  if command -v "$name" >/dev/null 2>&1; then
    pass "$name: $(command -v "$name")"
  else
    fail "$name not found"
  fi
}

echo "=== Proxy Pool Manager doctor ==="
echo "root: $ROOT"

check_command python3

if [ -x ".venv/bin/python" ]; then
  pass "venv python: .venv/bin/python"
else
  warn "venv python not found; run ./scripts/install-linux.sh"
fi

if [ -f requirements.txt ]; then
  pass "requirements.txt exists"
else
  fail "requirements.txt missing"
fi

if [ -f config/app.json ]; then
  pass "config/app.json exists"
else
  warn "config/app.json missing; run ./scripts/install-linux.sh"
fi

PORT="$(json_value port 9000)"
HOST="$(json_value host 127.0.0.1)"
PROXY_LISTEN_HOST="$(json_value proxy_listen_host 127.0.0.1)"
CLASH_API_ADDR="$(json_value clash_api_addr 127.0.0.1:9090)"
echo "config: host=$HOST port=$PORT proxy_listen_host=$PROXY_LISTEN_HOST clash_api_addr=$CLASH_API_ADDR"

if [ -x bin/sing-box ]; then
  pass "sing-box executable: bin/sing-box"
  bin/sing-box version | head -n 1 || warn "sing-box version failed"
else
  warn "bin/sing-box missing or not executable"
fi

if [ -x proxycheck-api/proxycheck ]; then
  pass "proxycheck executable: proxycheck-api/proxycheck"
elif [ -f proxycheck-api/proxycheck ]; then
  fail "proxycheck exists but is not executable; run chmod +x proxycheck-api/proxycheck"
else
  warn "proxycheck binary missing; run cd proxycheck-api && go build -o proxycheck ./cmd/proxycheck"
fi

if [ -f tmp/server.pid ] && kill -0 "$(cat tmp/server.pid)" 2>/dev/null; then
  pass "web pid file running: $(cat tmp/server.pid)"
else
  warn "web service not running by tmp/server.pid"
fi

if command -v curl >/dev/null 2>&1; then
  if curl -fsS "http://127.0.0.1:${PORT}/api/status" >/tmp/proxy-pool-manager-status.json 2>/dev/null; then
    pass "web API reachable: http://127.0.0.1:${PORT}/api/status"
    python3 - <<'PY' || true
import json
from pathlib import Path

data = json.loads(Path("/tmp/proxy-pool-manager-status.json").read_text(encoding="utf-8"))
running = data.get("running")
ready = data.get("ready")
expected = data.get("expected_ports") or []
listening = data.get("listening_ports") or []
missing = sorted(set(expected) - set(listening))
print(f"status: running={running} ready={ready} listening={len(listening)}/{len(expected)}")
if missing:
    print("missing ports:", ",".join(map(str, missing[:20])))
PY
  else
    warn "web API not reachable on 127.0.0.1:$PORT"
  fi
else
  warn "curl not found; skip web API check"
fi

CONFIG_PATH="$ROOT/config/sing-box.json"
if command -v pgrep >/dev/null 2>&1; then
  if pgrep -f "sing-box.*$CONFIG_PATH" >/dev/null 2>&1; then
    pass "project sing-box process found"
  else
    warn "project sing-box process not found"
  fi
else
  warn "pgrep not found; skip sing-box process check"
fi

echo "=== Summary: ok=$OK warn=$WARN fail=$FAIL ==="
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
