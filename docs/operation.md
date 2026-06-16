# Proxy Pool Manager 运行说明

## 配置在哪里

主要配置和运行文件：

- `config/app.json`：Web 服务端口、监听地址、代理端口监听地址、复制链接使用的公开地址。
- `config/assignments.json`：节点、测速结果、端口分配。Web UI 保存的数据都在这里。
- `config/sing-box.json`：点击“启动引擎”时自动生成的 sing-box 正式配置。
- `config/sing-box-test.json`：节点测速时临时生成的 sing-box 测试配置。
- `config/sing-box.log`：sing-box 日志。
- `bin/sing-box.exe`：项目使用的 sing-box 核心。
- `tmp/server.out.log` / `tmp/server.err.log`：Web 服务日志。

不要手改 `config/sing-box.json`，它会被程序重新生成。要改节点和端口，用 Web UI。

`config/app.json` 示例：

```json
{
  "host": "127.0.0.1",
  "port": 9000,
  "proxy_listen_host": "127.0.0.1",
  "proxy_public_host": "",
  "clash_api_addr": "127.0.0.1:9090",
  "domain_resolve_strategy": ""
}
```

字段说明：

- `host`：Web 管理界面监听地址。
- `port`：Web 管理界面端口。
- `proxy_listen_host`：sing-box 代理端口监听地址。本机用 `127.0.0.1`；给其他机器连用 `0.0.0.0`。
- `proxy_public_host`：复制链接、导出、curl 命令里使用的连接地址。留空时自动使用访问 Web 界面的 hostname；也可以固定写服务器公网 IP 或域名。
- `clash_api_addr`：sing-box Clash API 地址。
- `domain_resolve_strategy`：可选域名解析策略。默认留空；VPS 上访问 Google reset 时可试 `ipv4_only`。

## Windows 启动

首次安装：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

如果已经下载了 Windows 版 sing-box 压缩包：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1 -SingBoxZip "C:\Users\dsk\Downloads\sing-box-1.13.13-windows-amd64.zip"
```

安装脚本会创建 `.venv`、安装 `requirements.txt`、复制 `bin\sing-box.exe`、缺失时生成 `config\app.json`。如果项目 sing-box 正在运行，安装脚本会要求先停止服务，避免替换内核失败。

本机使用：

```powershell
.\scripts\start-service.ps1
```

打开：

```text
http://127.0.0.1:9000
```

如果 PowerShell 拦截脚本执行，可以用：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-service.ps1
```

## Windows 关闭

关闭 Web 服务和本项目 sing-box：

```powershell
.\scripts\stop-service.ps1
```

只关闭 Web 服务，保留 sing-box 引擎：

```powershell
.\scripts\stop-service.ps1 -KeepEngine
```

## 查看状态

```powershell
.\scripts\status-service.ps1
```

状态里重点看：

- `running`：sing-box 进程是否运行。
- `ready`：分配的端口是否都已经监听。
- `expected_ports`：应该监听的端口。
- `listening_ports`：实际已经监听的端口。

只有 `running=true` 且 `ready=true`，再复制 curl 去 CMD 验证。

## Linux 安装和启动

Linux 版 sing-box 内核和 Windows 不一样，不能直接复用 `sing-box.exe`。安装脚本会根据 CPU 架构自动下载：

- `x86_64/amd64`：`sing-box-版本-linux-amd64.tar.gz`
- `aarch64/arm64`：`sing-box-版本-linux-arm64.tar.gz`

首次安装：

```bash
chmod +x scripts/*.sh
sudo bash scripts/install-linux.sh --systemd --start
```

指定 sing-box 版本：

```bash
sudo bash scripts/install-linux.sh --systemd --start 1.13.13
```

日常更新：

```bash
bash scripts/update-linux.sh
```

这会执行 `git pull --ff-only`、同步依赖，并重启 `proxy-pool-manager` systemd 服务。

启动、状态、关闭：

```bash
sudo systemctl start proxy-pool-manager
sudo systemctl status proxy-pool-manager
sudo systemctl stop proxy-pool-manager
journalctl -u proxy-pool-manager -f
```

环境自检：

```bash
./scripts/doctor.sh
```

Windows 自检：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\doctor.ps1
```

Linux 脚本同样使用 `.venv`，sing-box 放在 `bin/sing-box`。没有 systemd 的环境可以继续使用 `./scripts/start-service.sh`、`./scripts/status-service.sh`、`./scripts/stop-service.sh` 手动运行。

本地 proxycheck 检测使用平台二进制：

- Windows：`proxycheck-api/proxycheck.exe`
- Linux：`proxycheck-api/proxycheck`

`scripts/install-linux.sh` 会在服务器已安装 Go 时自动构建 Linux 二进制。如果提示 Go 不存在，先安装 Go，然后执行：

```bash
cd proxycheck-api
go build -o proxycheck ./cmd/proxycheck
```

也可以通过环境变量指定已有二进制：

```bash
export PROXYCHECK_BIN=/path/to/proxycheck
```

## Docker 部署到 VPS

Docker 适合外网 VPS。默认 compose 会把 Web 控制台只绑定到宿主机本地地址，交给 Nginx/Caddy 做 HTTPS 反代：

```bash
cp config/app.docker.example.json config/app.json
docker compose up -d --build
```

默认端口：

```text
Web 控制台: 127.0.0.1:9100 -> 容器 9100
代理端口: 默认不公开，需要时在 docker-compose.yml 里打开
```

`config/app.json` 推荐写法：

```json
{
  "host": "0.0.0.0",
  "port": 9100,
  "proxy_listen_host": "0.0.0.0",
  "proxy_public_host": "你的域名或VPS公网IP",
  "clash_api_addr": "127.0.0.1:9090",
  "domain_resolve_strategy": ""
}
```

如果只是公开 Web 控制台，不给外部用户连接代理端口，不需要打开 `8001-8062`。如果要把分配的代理端口也给外部使用，在 `docker-compose.yml` 里取消对应端口范围注释：

```yaml
ports:
  - "127.0.0.1:9100:9100"
  - "8001-8062:8001-8062"
```

更新容器：

```bash
git pull --ff-only
docker compose up -d --build
```

Caddy 反代示例：

```text
proxy.example.com {
  reverse_proxy 127.0.0.1:9100
}
```

Nginx 反代示例：

```nginx
server {
    listen 443 ssl http2;
    server_name proxy.example.com;

    location / {
        proxy_pass http://127.0.0.1:9100;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 部署到服务器给别人用

默认只允许本机使用：

```text
Web: 127.0.0.1:9000
Proxy: 127.0.0.1:8001...
```

如果要让其他机器连接代理端口，启动时把代理监听地址改成 `0.0.0.0`：

```powershell
.\scripts\start-service.ps1 -ProxyListenHost 0.0.0.0
```

Linux：

```bash
sudo bash scripts/install-linux.sh --systemd --start --proxy-listen 0.0.0.0
```

也可以直接修改 `config/app.json`：

```json
{
  "host": "127.0.0.1",
  "port": 9000,
  "proxy_listen_host": "0.0.0.0",
  "proxy_public_host": "服务器IP或域名",
  "clash_api_addr": "127.0.0.1:9090",
  "domain_resolve_strategy": ""
}
```

注意：`0.0.0.0` 只表示“服务监听所有网卡”，不能作为客户端连接地址。别人连接时必须使用服务器 IP 或域名。

如果 Web 管理界面也要远程访问：

```powershell
.\scripts\start-service.ps1 -HostName 0.0.0.0 -ProxyListenHost 0.0.0.0
```

Linux：

```bash
sudo bash scripts/install-linux.sh --systemd --start --host 0.0.0.0 --proxy-listen 0.0.0.0
```

给别人用的标准代理链接：

```text
http://服务器IP:8001
socks5://服务器IP:8001
```

`socks5h://` 只适合 curl/libcurl 手动验证，不是通用客户端标准。

## VPS 上 IP 查询成功但 Google reset

如果出现：

```text
curl --proxy "http://服务器IP:9517/" https://ipv4.webshare.io/
-> 正常返回出口 IP

curl --proxy "http://服务器IP:9517/" https://google.com
-> Recv failure: Connection was reset
```

这说明公网代理端口基本可连，问题更可能发生在“通过节点访问目标网站”阶段，不是端口没开放。

先在 VPS 本机测试：

```bash
curl -v --proxy "http://127.0.0.1:9517/" https://ipv4.webshare.io/ --max-time 15
curl -v --proxy "http://127.0.0.1:9517/" https://www.google.com/generate_204 --max-time 15
curl -v --proxy "http://127.0.0.1:9517/" https://www.gstatic.com/generate_204 --max-time 15
curl -v --proxy "http://127.0.0.1:9517/" https://www.cloudflare.com/cdn-cgi/trace --max-time 15
```

再从你的电脑测试公网地址：

```powershell
curl.exe -v --proxy "http://服务器IP:9517/" https://www.google.com/generate_204 --max-time 15
curl.exe -v --proxy "http://服务器IP:9517/" https://www.gstatic.com/generate_204 --max-time 15
curl.exe -v --proxy "http://服务器IP:9517/" https://www.cloudflare.com/cdn-cgi/trace --max-time 15
```

判断：

- VPS 本机 `127.0.0.1:端口` 也访问 Google 失败：问题在 VPS 到节点或节点到目标网站。
- VPS 本机成功，但公网 `服务器IP:端口` 失败：问题在公网访问 VPS 这段、防火墙、运营商或安全组。
- `ipv4.webshare.io` 成功但 Google/gstatic 失败：说明不是简单端口问题，而是特定目标、DNS/IPv6、节点策略或链路重置。

可尝试强制目标域名解析为 IPv4。修改 `config/app.json`：

```json
{
  "domain_resolve_strategy": "ipv4_only"
}
```

然后在 Web UI 重新点击“启动引擎”，让程序重新生成 `config/sing-box.json`。如果要恢复默认行为，把它改回空字符串：

```json
{
  "domain_resolve_strategy": ""
}
```

## 安全注意

当前代理端口没有用户名密码认证。不要把 `0.0.0.0:8001...` 直接暴露到公网。

如果要给别人长期使用，至少做其中一种限制：

- 只开放给固定 IP。
- 用云服务器安全组限制来源。
- 放在 VPN / 内网 / SSH 隧道后面。
- 后续给本项目增加代理认证。
