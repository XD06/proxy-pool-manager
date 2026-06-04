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
INSTALL_DIR="/opt/${SERVICE_NAME}"
SYSTEMD_DIR="/etc/systemd/system"

# --- 检查 root ---
if [[ $EUID -ne 0 ]]; then
  echo "❌ 请用 sudo 运行: sudo bash $0"
  exit 1
fi

# --- 检查 service 文件 ---
if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "❌ 未找到 $SERVICE_SRC"
  exit 1
fi

echo "=== 安装 Proxy Pool Manager systemd 服务 ==="

# 1. 创建目标目录并复制项目文件（排除运行时文件）
mkdir -p "$INSTALL_DIR"
rsync -a --exclude='.venv' --exclude='tmp' --exclude='config/*.json' \
  --exclude='config/*.log' --exclude='config/*.db' \
  "$PROJECT_DIR"/* "$INSTALL_DIR/"
echo "✔ 项目文件已复制到 $INSTALL_DIR"

# 2. 调用基础安装脚本（venv + 依赖 + sing-box）
cd "$INSTALL_DIR"
bash "$INSTALL_DIR/scripts/install-linux.sh"
echo "✔ 基础安装完成"

# 3. 注册 systemd 服务
cp "$SERVICE_SRC" "${SYSTEMD_DIR}/${SERVICE_NAME}.service"
systemctl daemon-reload
echo "✔ systemd 服务已注册"

echo ""
echo "=== 安装完成 ==="
echo ""
echo "接下来:"
echo "  1. 编辑配置: vi ${INSTALL_DIR}/config/app.json"
echo "  2. 启动服务: sudo systemctl start ${SERVICE_NAME}"
echo "  3. 查看状态: sudo systemctl status ${SERVICE_NAME}"
echo "  4. 开机自启: sudo systemctl enable ${SERVICE_NAME}"
echo "  5. 查看日志: journalctl -u ${SERVICE_NAME} -f"
echo ""
echo "管理界面: http://<VPS_IP>:9100"
