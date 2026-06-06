#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-}"
if [ -z "$VERSION" ]; then
  VERSION=$(curl -sL https://api.github.com/repos/SagerNet/sing-box/releases/latest \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))" 2>/dev/null \
    || echo "1.13.13")
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p bin config tmp

CONFIG_PATH="$ROOT/config/sing-box.json"
if command -v pgrep >/dev/null 2>&1 && pgrep -f "sing-box.*$CONFIG_PATH" >/dev/null 2>&1; then
  echo "Project sing-box is still running. Stop it first: ./scripts/stop-service.sh" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  python3 -m venv .venv
fi

".venv/bin/python" -m pip install --upgrade pip
".venv/bin/python" -m pip install -r requirements.txt

# 如果 bin/sing-box 已存在且可执行，跳过下载
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
      curl -L "$URL" -o "$ARCHIVE"
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

  if [ -f bin/sing-box ]; then
    cp bin/sing-box "bin/sing-box-backup-$(date +%Y%m%d-%H%M%S)"
  fi
  cp "$BINARY" bin/sing-box
  chmod +x bin/sing-box
fi

if [ ! -f config/app.json ]; then
  cat > config/app.json <<'JSON'
{
  "host": "127.0.0.1",
  "port": 9100,
  "proxy_listen_host": "127.0.0.1",
  "proxy_public_host": "",
  "clash_api_addr": "127.0.0.1:9090",
  "domain_resolve_strategy": ""
}
JSON
fi

echo "Installed Proxy Pool Manager dependencies."
bin/sing-box version
echo "Start with: ./scripts/start-service.sh"
