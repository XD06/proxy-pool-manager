# Proxy Pool Manager

本项目是一个本地代理端口管理工具。它把多个代理节点固定绑定到不同本地 SOCKS5 端口，让不同账户可以稳定使用不同出口 IP。

核心原则：

- 每个本地端口固定对应一个节点。
- 正式代理流量只经过 sing-box。
- Python 只负责导入、解析、生成配置、启动进程和 Web 管理。
- 状态和映射持久化到 `config/assignments.json`。

## 当前实现范围

已实现：

- FastAPI 管理服务。
- 纯 HTML/CSS/JS Web UI。
- 节点导入：
  - `vless://`
  - `vmess://`
  - `ss://`
  - `trojan://`
  - `hysteria2://`
  - Clash YAML 中的 `ss/vmess/vless/trojan/hysteria2`
  - Base64 订阅文本
- 端口映射保存。
- sing-box 配置生成。
- sing-box 二进制自动下载路径。
- `sing-box check` 校验后启动。
- 出口 IP 查询。
- 导入后节点测速：临时启动 sing-box，把节点挂到测试端口验证可用性、延迟和出口 IP。
- 同出口 IP 去重：测速后可自动只保留同一出口 IP 中最快的可用节点。
- 运行后端口验证：对已分配端口重新测试延迟和出口 IP。
- 节点清理：支持删除选中节点或清空已导入节点。
- 代理端口使用 sing-box `mixed` inbound，同时支持 HTTP proxy 和 SOCKS proxy。
- 单元测试和 API smoke test。

暂未实现：

- SSR。
- TUIC。
- 多订阅合并。
- HTTP 代理端口。
- 流量统计。
- 系统代理或 TUN 模式。

## 快速安装

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
.\scripts\start-service.ps1
```

如果已经手动下载了 sing-box Windows 压缩包：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1 -SingBoxZip "C:\Users\dsk\Downloads\sing-box-1.13.13-windows-amd64.zip"
```

Linux：

```bash
chmod +x scripts/*.sh
sudo bash scripts/install-linux.sh --systemd --start
```

安装脚本会创建 `.venv`、安装 Python 依赖、下载对应平台的 sing-box 核心，构建 Linux 版 `proxycheck-api/proxycheck`，生成默认 `config/app.json`，并注册 `proxy-pool-manager` systemd 服务为开机自启。

后续更新：

```bash
bash scripts/update-linux.sh
```

这会执行 `git pull --ff-only`、同步 Python 依赖，并重启 systemd 服务。没有 systemd 的环境仍可手动运行：

```bash
./scripts/install-linux.sh
./scripts/start-service.sh
```

Docker / VPS：

```bash
cp config/app.docker.example.json config/app.json
sed -i 's/change-this-admin-key/你的强管理密钥/' config/app.json
docker compose up -d --build
```

默认 compose 只把控制台绑定到宿主机 `127.0.0.1:9100`，适合在 VPS 上用 Nginx/Caddy 反代 HTTPS。代理端口需要给外部用户使用时，再在 `docker-compose.yml` 里打开对应端口范围，例如 `8001-8062:8001-8062`。

如果你要把 `9100:9100` 直接暴露到公网，必须设置 `admin_key` 或环境变量 `PPM_ADMIN_KEY`，否则控制台没有登录保护。

如果 Linux 服务器没有安装 Go，本地 proxycheck 检测会不可用。安装 Go 后执行：

```bash
cd proxycheck-api
go build -o proxycheck ./cmd/proxycheck
```

环境自检：

```bash
./scripts/doctor.sh
```

Windows：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

手动安装依赖也可以：

```powershell
python -m pip install -r requirements.txt
```

## 启动

```powershell
python main.py
```

然后打开：

```text
http://127.0.0.1:9000
```

Windows 使用脚本：

```powershell
.\scripts\start-service.ps1
.\scripts\status-service.ps1
.\scripts\stop-service.ps1
```

Linux 使用脚本：

```bash
sudo systemctl start proxy-pool-manager
sudo systemctl status proxy-pool-manager
sudo systemctl stop proxy-pool-manager
journalctl -u proxy-pool-manager -f
```

Web 端口和监听地址在这里改：

```text
config/app.json
```

详细运行、关闭、服务器部署说明见：

```text
docs/operation.md
```

## sing-box

启动引擎时，程序会按以下顺序寻找 sing-box：

1. 环境变量 `SING_BOX_PATH`
2. `bin/sing-box.exe` 或 `bin/sing-box`
3. 系统 PATH 中的 `sing-box`
4. 自动从 `SagerNet/sing-box` GitHub Releases 下载

如果自动下载失败，可以手动下载对应平台的 sing-box，并放到：

```text
bin/sing-box.exe
```

Windows 使用 `.exe`，Linux 使用无后缀二进制。项目内置安装脚本会按系统下载：

- Windows x64：`sing-box-版本-windows-amd64.zip`
- Linux x64：`sing-box-版本-linux-amd64.tar.gz`
- Linux ARM64：`sing-box-版本-linux-arm64.tar.gz`

如果你已经安装 v2rayN，并且想复用它自带的 sing-box，可以这样启动：

```powershell
$env:SING_BOX_PATH="D:\dsk\Documents\v2rayN-windows-64-desktop\v2rayN-windows-64\bin\sing_box\sing-box.exe"
python main.py
```

## 使用流程

1. 启动 `python main.py`。
2. 打开 Web UI。
3. 在“导入”页粘贴订阅 URL 或节点文本。
4. 在“测试”页点击“测速选中节点”。
   - 如果填写“测试目标 URL”，所有选中节点只访问这个 URL。
   - 这时排序依据是目标 URL 的响应时间。
   - 如果留空，则执行默认出口 IP/连通性测速。
5. 如需去重，点击“测速并按 IP 去重”。
6. 在“分配”页把可用节点分配到端口。
7. 保存映射。
8. 点击“启动引擎”。
9. 在“运行”页点击“验证全部端口”。
10. 使用本地 SOCKS5 端口：

```powershell
curl.exe --socks5 127.0.0.1:8001 https://api.ipify.org
```

也可以用 HTTP proxy 方式验证端口出口 IP：

```powershell
curl.exe --proxy "http://127.0.0.1:8001/" https://ipv4.webshare.io/
```

出口 IP 查询会按顺序尝试多个服务：

- `https://ipv4.webshare.io/`
- `https://api.ipify.org?format=text`
- `https://ipv4.icanhazip.com/`
- `https://ifconfig.me/ip`
- `https://checkip.amazonaws.com/`
- `https://ident.me/`

运行页的“验证全部端口”会显示每个目标的：

- 是否成功
- HTTP 状态码
- 耗时
- 响应片段
- 错误信息

默认验证目标：

- `https://ipv4.webshare.io/`
- `https://www.google.com/generate_204`
- `https://www.gstatic.com/generate_204`
- `https://www.cloudflare.com/cdn-cgi/trace`

也可以输入自定义 URL 一起验证。

运行页也支持单端口操作：

- “查出口”：只查询当前端口出口 IP，结果显示在上方简洁结果区。
- “验证”：只验证当前端口的多个目标，简洁结果显示在上方，详细结果合并到下方验证表。

导出支持：

- 代理列表
- CSV
- JSON
- curl 命令

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | Web UI |
| `GET` | `/api/status` | 服务和引擎状态 |
| `POST` | `/api/import` | 导入 URL 或文本 |
| `GET` | `/api/nodes` | 节点列表 |
| `POST` | `/api/nodes/delete` | 删除选中节点或清空节点 |
| `POST` | `/api/test` | 临时启动 sing-box 测试导入节点，可选按出口 IP 去重 |
| `POST` | `/api/test-ports` | 对已映射端口做运行后验证 |
| `PUT` | `/api/assign` | 保存端口映射 |
| `GET` | `/api/ports` | 查询端口映射 |
| `GET` | `/api/ports/{port}/ip` | 查询端口出口 IP |
| `GET` | `/api/proxy/fastest` | 返回最快可用代理；可加 `check=true` 做实时验证 |
| `POST` | `/api/start` | 启动 sing-box |
| `POST` | `/api/stop` | 停止 sing-box |

## 测试

```powershell
python -m compileall -q main.py app tests
python -m pytest -q
```

当前测试覆盖：

- 单节点链接解析。
- Clash YAML 解析。
- Hysteria2 链接解析。
- sing-box 配置生成。
- 状态持久化。
- API 导入、分配、状态查询 smoke test。
- 同出口 IP 去重逻辑。

## 安全说明

- Web 服务默认只监听 `127.0.0.1:9000`。
- sing-box inbound 默认只监听 `127.0.0.1`。
- `config/assignments.json` 会保存代理凭据，不要提交到 git。
- 本项目不记录代理请求内容。
