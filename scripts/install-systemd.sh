#!/usr/bin/env bash
# ============================================================
# Install Proxy Pool Manager as a systemd service
# Usage: sudo bash scripts/install-systemd.sh
# ============================================================
set -euo pipefail

SERVICE_NAME="proxy-pool-manager"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_SRC="${SCRIPT_DIR}/${SERVICE_NAME}.service"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_USER="${SERVICE_USER:-${SUDO_USER:-}}"
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == "root" ]]; then
  SERVICE_USER="$(logname 2>/dev/null || echo root)"
fi

# --- 检查 root ---
if [[ $EUID -ne 0 ]]; then
  echo "❌ 请用 sudo 运行: sudo bash $0"
  exit 1
fi

echo "=== 安装 Proxy Pool Manager systemd 服务 ==="

# 1. 安装依赖（venv + pip + sing-box）
echo "✔ 安装基础依赖..."
cd "$PROJECT_DIR"
bash "$SCRIPT_DIR/install-linux.sh"
echo "✔ 基础安装完成"

# 2. 修复权限（服务以安装用户运行，确保能读写项目文件）
chown -R "${SERVICE_USER}:${SERVICE_USER}" "$PROJECT_DIR"

# 3. 写入 systemd 服务文件（指向项目当前目录）
cat > "${SYSTEMD_DIR}/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Proxy Pool Manager
Documentation=https://github.com/XD06/proxy-pool-manager
After=network.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PROJECT_DIR}/.venv/bin/python3 main.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
Environment=PPM_HOST=0.0.0.0
Environment=PPM_PORT=9100
Environment=PPM_PROXY_LISTEN_HOST=0.0.0.0
Environment=PPM_CLASH_API_ADDR=127.0.0.1:9090
Environment=PPM_DOMAIN_RESOLVE_STRATEGY=

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
echo "✔ systemd 服务已注册并设为开机自启"

echo ""
echo "=== 安装完成 ==="
echo ""
echo "启动服务: sudo systemctl start ${SERVICE_NAME}"
echo "查看状态: sudo systemctl status ${SERVICE_NAME}"
echo "查看日志: journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "管理界面: http://<本机IP>:9100"
