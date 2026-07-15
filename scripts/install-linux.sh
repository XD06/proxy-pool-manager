#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-proxy-pool-manager}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION=""
INSTALL_SYSTEMD=0
START_SERVICE=0
HOST_VALUE="${PPM_HOST:-0.0.0.0}"
PORT_VALUE="${PPM_PORT:-9100}"
PROXY_LISTEN_HOST_VALUE="${PPM_PROXY_LISTEN_HOST:-0.0.0.0}"
PROXY_PUBLIC_HOST_VALUE="${PPM_PROXY_PUBLIC_HOST:-}"
CLASH_API_ADDR_VALUE="${PPM_CLASH_API_ADDR:-127.0.0.1:9090}"
DOMAIN_RESOLVE_STRATEGY_VALUE="${PPM_DOMAIN_RESOLVE_STRATEGY:-}"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-}}"
FORCED_CONFIG_KEYS=()

usage() {
  cat <<EOF
Usage: bash scripts/install-linux.sh [options] [sing-box-version]

Options:
  --systemd              Register and enable a systemd service.
  --start                Start or restart the service after installation.
  --user USER            User for the systemd service. Default: sudo user.
  --host HOST            Web listen host. Default: ${HOST_VALUE}
  --port PORT            Web port. Default: ${PORT_VALUE}
  --proxy-listen HOST    Proxy inbound listen host. Default: ${PROXY_LISTEN_HOST_VALUE}
  --proxy-public HOST    Public proxy host shown in links.
  --clash-api ADDR       Clash API listen address. Default: ${CLASH_API_ADDR_VALUE}
  --domain-strategy VAL  sing-box domain resolve strategy.
  -h, --help             Show this help.

Examples:
  bash scripts/install-linux.sh
  sudo bash scripts/install-linux.sh --systemd --start
  sudo bash scripts/install-linux.sh --systemd --host 0.0.0.0 --proxy-listen 0.0.0.0
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --systemd|--service)
      INSTALL_SYSTEMD=1
      shift
      ;;
    --start)
      START_SERVICE=1
      shift
      ;;
    --user)
      SERVICE_USER="${2:-}"
      shift 2
      ;;
    --host)
      HOST_VALUE="${2:-}"
      FORCED_CONFIG_KEYS+=("host")
      shift 2
      ;;
    --port)
      PORT_VALUE="${2:-}"
      FORCED_CONFIG_KEYS+=("port")
      shift 2
      ;;
    --proxy-listen)
      PROXY_LISTEN_HOST_VALUE="${2:-}"
      FORCED_CONFIG_KEYS+=("proxy_listen_host")
      shift 2
      ;;
    --proxy-public)
      PROXY_PUBLIC_HOST_VALUE="${2:-}"
      FORCED_CONFIG_KEYS+=("proxy_public_host")
      shift 2
      ;;
    --clash-api)
      CLASH_API_ADDR_VALUE="${2:-}"
      FORCED_CONFIG_KEYS+=("clash_api_addr")
      shift 2
      ;;
    --domain-strategy)
      DOMAIN_RESOLVE_STRATEGY_VALUE="${2:-}"
      FORCED_CONFIG_KEYS+=("domain_resolve_strategy")
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      VERSION="$1"
      shift
      ;;
  esac
done

if [ "$INSTALL_SYSTEMD" -eq 1 ] && [ "${EUID}" -ne 0 ]; then
  echo "systemd install requires root. Run: sudo bash scripts/install-linux.sh --systemd --start" >&2
  exit 1
fi

if [ -z "$SERVICE_USER" ] || [ "$SERVICE_USER" = "root" ]; then
  SERVICE_USER="$(logname 2>/dev/null || echo root)"
fi

cd "$ROOT"
mkdir -p bin config tmp

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -r requirements.txt

if [ -x proxycheck-api/proxycheck ]; then
  echo "proxycheck binary already exists, skipping build"
elif command -v go >/dev/null 2>&1; then
  echo "Building proxycheck binary..."
  (cd proxycheck-api && go build -o proxycheck ./cmd/proxycheck)
  chmod +x proxycheck-api/proxycheck
else
  echo "Warning: Go is not installed; local proxycheck detection will be unavailable." >&2
  echo "Install Go and run: cd proxycheck-api && go build -o proxycheck ./cmd/proxycheck" >&2
fi

if [ -x proxycheck-api/pool-router ]; then
  echo "pool-router binary already exists, skipping build"
elif command -v go >/dev/null 2>&1; then
  echo "Building pool-router binary..."
  (cd proxycheck-api && go build -o pool-router ./cmd/pool-router)
  chmod +x proxycheck-api/pool-router
else
  echo "Warning: Go is not installed; node pools and per-port traffic monitoring will be unavailable." >&2
  echo "Install Go and run: cd proxycheck-api && go build -o pool-router ./cmd/pool-router" >&2
fi

latest_sing_box_version() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL https://api.github.com/repos/SagerNet/sing-box/releases/latest \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))" 2>/dev/null
  fi
}

if [ -z "$VERSION" ]; then
  VERSION="$(latest_sing_box_version || true)"
fi
VERSION="${VERSION:-1.13.13}"

if [ -x bin/sing-box ]; then
  echo "sing-box binary already exists, skipping download"
else
  ARCH="$(uname -m)"
  case "$ARCH" in
    x86_64|amd64) ASSET_ARCH="amd64" ;;
    aarch64|arm64) ASSET_ARCH="arm64" ;;
    *)
      echo "Unsupported Linux architecture: $ARCH" >&2
      exit 1
      ;;
  esac

  ASSET="sing-box-${VERSION}-linux-${ASSET_ARCH}.tar.gz"
  URL="https://github.com/SagerNet/sing-box/releases/download/v${VERSION}/${ASSET}"
  ARCHIVE="tmp/${ASSET}"
  EXTRACT_DIR="tmp/sing-box-${VERSION}-linux-${ASSET_ARCH}"

  if [ ! -f "$ARCHIVE" ]; then
    if command -v curl >/dev/null 2>&1; then
      curl -fL "$URL" -o "$ARCHIVE"
    elif command -v wget >/dev/null 2>&1; then
      wget -O "$ARCHIVE" "$URL"
    else
      echo "curl or wget is required to download sing-box" >&2
      exit 1
    fi
  fi

  rm -rf "$EXTRACT_DIR"
  mkdir -p "$EXTRACT_DIR"
  tar -xzf "$ARCHIVE" -C "$EXTRACT_DIR"

  BINARY="$(find "$EXTRACT_DIR" -type f -name sing-box | head -n 1)"
  if [ -z "$BINARY" ]; then
    echo "sing-box binary not found in $ARCHIVE" >&2
    exit 1
  fi

  cp "$BINARY" bin/sing-box
  chmod +x bin/sing-box
fi

".venv/bin/python" - "$HOST_VALUE" "$PORT_VALUE" "$PROXY_LISTEN_HOST_VALUE" "$PROXY_PUBLIC_HOST_VALUE" "$CLASH_API_ADDR_VALUE" "$DOMAIN_RESOLVE_STRATEGY_VALUE" "${FORCED_CONFIG_KEYS[*]}" <<'PY'
import json
import sys
from pathlib import Path

keys = [
    "host",
    "port",
    "proxy_listen_host",
    "proxy_public_host",
    "clash_api_addr",
    "domain_resolve_strategy",
]
values = dict(zip(keys, sys.argv[1:]))
forced = set((sys.argv[7] if len(sys.argv) > 7 else "").split())
values["port"] = int(values["port"])
path = Path("config/app.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    data = {}
new_file = not path.exists()
for key, value in values.items():
    if new_file or key in forced or key not in data or data[key] in ("", None):
        data[key] = value
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

if [ "$INSTALL_SYSTEMD" -eq 1 ]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl is not available; cannot install systemd service" >&2
    exit 1
  fi

  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$ROOT"
  SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
  cat > "$SERVICE_PATH" <<EOF
[Unit]
Description=Proxy Pool Manager
Documentation=https://github.com/XD06/proxy-pool-manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${ROOT}
ExecStart=${ROOT}/.venv/bin/python main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME"
  if [ "$START_SERVICE" -eq 1 ]; then
    systemctl restart "$SERVICE_NAME"
  fi
fi

bin/sing-box version | head -n 1 || true
echo "Installed Proxy Pool Manager at $ROOT"
if [ "$INSTALL_SYSTEMD" -eq 1 ]; then
  echo "Service: $SERVICE_NAME"
  echo "Start: sudo systemctl start $SERVICE_NAME"
  echo "Status: sudo systemctl status $SERVICE_NAME"
  echo "Logs: journalctl -u $SERVICE_NAME -f"
else
  echo "Start: ./scripts/start-service.sh"
fi
echo "Web: http://127.0.0.1:${PORT_VALUE}"
