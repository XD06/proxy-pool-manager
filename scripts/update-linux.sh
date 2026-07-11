#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-proxy-pool-manager}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DO_PULL=1
RESTART_SERVICE=1

usage() {
  cat <<EOF
Usage: bash scripts/update-linux.sh [options]

Options:
  --no-pull       Skip git pull.
  --no-restart    Do not restart after updating dependencies.
  --service NAME  systemd service name. Default: ${SERVICE_NAME}
  -h, --help      Show this help.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --no-pull)
      DO_PULL=0
      shift
      ;;
    --no-restart)
      RESTART_SERVICE=0
      shift
      ;;
    --service)
      SERVICE_NAME="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cd "$ROOT"

if [ "$DO_PULL" -eq 1 ]; then
  git pull --ff-only
fi

bash scripts/install-linux.sh

if [ "$RESTART_SERVICE" -eq 0 ]; then
  echo "Updated dependencies. Restart skipped."
  exit 0
fi

if command -v systemctl >/dev/null 2>&1 && systemctl cat "$SERVICE_NAME" >/dev/null 2>&1; then
  sudo systemctl daemon-reload
  sudo systemctl restart "$SERVICE_NAME"
  sudo systemctl --no-pager --full status "$SERVICE_NAME" || true
else
  ./scripts/stop-service.sh || true
  ./scripts/start-service.sh
fi
