# Proxy Pool Manager - 开发记录

日期：2026-06-04

## 本次完成

按照 `docs/2026-06-04-proxy-pool-manager-development-plan.md` 实现了 MVP 骨架：

- 创建 Python/FastAPI 项目结构。
- 实现节点数据模型。
- 实现 `config/assignments.json` 持久化和损坏备份。
- 实现订阅/节点解析器。
- 实现 sing-box 配置生成器。
- 实现 sing-box 引擎管理器。
- 实现端口出口 IP 查询。
- 实现端口测速接口。
- 实现 REST API。
- 实现纯静态 Web UI。
- 补充 README。
- 补充单元测试和 API smoke test。
- 新增导入后临时 sing-box 节点测试。
- 新增同出口 IP 去重。
- 新增运行后端口验证界面。

## 当前文件

核心代码：

- `main.py`
- `app/api.py`
- `app/models.py`
- `app/store.py`
- `app/parser.py`
- `app/generator.py`
- `app/engine.py`
- `app/tester.py`
- `app/settings.py`

前端：

- `templates/index.html`
- `static/app.js`
- `static/style.css`

测试：

- `tests/test_parser.py`
- `tests/test_generator.py`
- `tests/test_store.py`
- `tests/test_api.py`
- `tests/test_tester.py`

## 已验证

执行：

```powershell
python -m compileall -q main.py app tests
python -m pytest -q
```

结果：

```text
15 passed
```

## 重要实现说明

### 测速

节点导入后可以直接测速。系统会临时生成 `config/sing-box-test.json`，临时启动独立 sing-box，把每个节点绑定到 `19001+` 测试端口：

```text
Python -> socks5://127.0.0.1:{19001+N} -> https://www.gstatic.com/generate_204
Python -> socks5://127.0.0.1:{19001+N} -> https://api.ipify.org
```

测试结果会记录：

- 是否可用
- 延迟
- 出口 IP
- 错误原因

如果启用“按 IP 去重”，系统只比较测试成功且有出口 IP 的节点。同一出口 IP 下保留延迟最低的节点。

运行后的端口验证使用正式映射端口：

```text
Python -> socks5://127.0.0.1:{port} -> 测试 URL / 出口 IP 服务
```

### sing-box 启动

启动前会生成 `config/sing-box.json`，然后执行：

```text
sing-box check -c config/sing-box.json
```

校验通过后才执行：

```text
sing-box run -c config/sing-box.json
```

### 配置生成

生成配置包含：

- SOCKS inbound
- 节点 outbound
- `direct` outbound
- `block` outbound
- inbound 到 outbound 的 route rule
- `experimental.clash_api`

## 已知限制

- 未做真实代理节点联调，因为没有可公开记录的测试节点凭据。
- 自动下载 sing-box 逻辑已实现，但没有在自动测试中触发验证。
- Hysteria2/TUIC/SSR 未实现。
- 暂无 Windows 服务或 Linux systemd 安装脚本。

## 下一步建议

1. 准备一组可用测试节点，完成真实端到端验证。
2. 验证 sing-box 自动下载和 `sing-box check`。
3. 对真实 Clash 订阅样本补 parser fixtures。
4. 增加端口占用检测。
5. 增加后台监控任务，处理 sing-box 运行中崩溃。

## 500 错误修复记录

用户点击“测速”和“启动引擎”时，前端报：

```text
POST /api/test 500
POST /api/start 500
```

根因：

- 项目 `bin/` 目录没有 sing-box。
- 系统 PATH 里也没有 sing-box。
- 程序尝试从 GitHub 自动下载 sing-box。
- GitHub API 返回 `403 rate limit exceeded`。
- `httpx.HTTPStatusError` 没有被转换成 `EngineError`，FastAPI 因未捕获异常返回 500。

处理：

- 将 v2rayN 自带的 sing-box 复制到 `bin/sing-box.exe`。
- `EngineManager.ensure_binary()` 增加 `SING_BOX_PATH` 和系统 PATH 检测。
- GitHub 下载失败时转为 `EngineError`，API 返回清晰错误，不再返回 500。
- 修复 `/api/test` 中空数组被误判为“测试全部节点”的问题。

验证：

```text
POST /api/test {"node_tags": []} -> 200
POST /api/test {"node_tags": ["node-vless-dbc58b02b4"]} -> 200，返回 alive=true、delay、exit_ip
POST /api/start -> 200，引擎运行
```

## 渐进式测速显示

用户反馈：

```text
点击测速没有反应，后面结果出来，所有测速完才会出现，应该测速一个出现一个。
```

根因：

- 旧版前端调用 `POST /api/test`。
- 后端会等全部节点测速结束后才返回响应。
- 节点多时，用户在 UI 上看不到任何中间进度。

处理：

- 新增 `POST /api/test/start`：
  - 立即创建测速 job。
  - 后台启动临时 sing-box 进行节点测试。
  - 每完成一个节点，就把结果写入 job。
- 新增 `GET /api/test/jobs/{job_id}`：
  - 返回 `total`、`completed`、`results`、`status`。
- 前端改为：
  - 点击后立即显示“测速中”。
  - 每秒轮询 job。
  - 每拿到一个节点结果，就更新对应表格行。
  - 完成后刷新节点列表和分配表。

验证：

```text
POST /api/test/start {"node_tags": []} -> 200，返回 job id
GET /api/test/jobs/{job_id} -> 200，返回 status=done
python -m pytest -q -> 16 passed
```

## 测速排序和启动优化

用户反馈：

```text
测速启动好像有点慢。
测速去重自动排序，速度越快的排前面。
分配界面应该显示去重排序测试后的节点。
```

处理：

- 临时测速 sing-box 启动跳过 `sing-box check`，正式启动仍保留 check。
- 临时测速启动等待从 1 秒降到 0.35 秒。
- 测试完成后保存节点顺序：
  1. 可用节点优先。
  2. 可用节点按延迟从低到高排序。
  3. 失败节点排在可用节点后。
  4. 未测试节点排最后。
- 按出口 IP 去重后，同样按测试结果排序。
- 分配界面读取 `/api/nodes`，因此会显示去重和排序后的节点顺序。

验证：

```text
python -m pytest -q -> 17 passed
```

## 测试端口冲突和卡住修复

用户反馈：

```text
start inbound/socks[port-19001]: listen tcp 127.0.0.1:19001: bind:
Only one usage of each socket address is normally permitted.
```

根因：

- 临时测速固定从 `19001` 开始绑定端口。
- 上一次测速残留、并发点击、或其他程序占用端口时会直接失败。
- 单节点请求只有 httpx 超时，没有外层硬超时，极端情况下最后几个节点会拖住 job。

处理：

- 临时测速端口改为动态扫描空闲端口，不再固定使用 `19001`。
- 如果 `19001` 被占用，会自动跳过。
- 同一时间只允许一个节点测速 job，重复点击返回 `409`。
- 每个节点增加 18 秒硬超时，超时后该节点标记失败，不拖住整个 job。
- 手动清理了 FastAPI 重启后遗留的项目 sing-box 进程，避免正式端口被旧进程占用。

验证：

```text
python -m pytest -q -> 18 passed
```

## 节点清理和端口验证增强

用户提出：

```text
需要增加清除导入的节点，或者选择哪些清除。
分配节点之后，需要有地方能够验证代理端口可用，确实走了这个 IP。
参考：curl --proxy "http://127.0.0.1:10808/" https://ipv4.webshare.io/
```

处理：

- 新增 `POST /api/nodes/delete`：
  - 支持删除选中节点。
  - 支持清空全部导入节点。
  - 删除节点时同步清理端口映射、测速缓存、出口 IP 缓存。
- Web UI 测试页新增：
  - 删除选中
  - 清空节点
- sing-box inbound 从 `socks` 改为 `mixed`：
  - 同一个本地端口同时支持 HTTP proxy 和 SOCKS proxy。
  - 可以直接使用 `curl --proxy "http://127.0.0.1:{port}/" https://ipv4.webshare.io/` 验证。
- 出口 IP 查询优先使用：
  - `https://ipv4.webshare.io/`
- 运行页每个端口显示对应 curl 验证命令。

验证：

```text
python -m pytest -q -> 19 passed
```

## Hysteria2 导入和出口 IP fallback

用户反馈：

```text
导入 hysteria2://... 成功了但是没有加入节点。
有时候查不到出口，使用其他网站，添加更多方式。
```

处理：

- 新增 `hysteria2://` 和 `hy2://` 链接解析。
- 支持 Hysteria2 URL 参数：
  - `sni`
  - `insecure=1`
  - `allowInsecure=1`
  - `mport=20000-55000`
  - `obfs`
  - `obfs-password`
- `mport=20000-55000` 会转换为 sing-box `server_ports: ["20000:55000"]`。
- Clash YAML 中新增 `type: hysteria2` 基础解析。
- 如果导入结果是 0 个节点，API 返回 400，不再显示“成功但没节点”。
- 出口 IP 查询增加多个 fallback：
  - `https://ipv4.webshare.io/`
  - `https://api.ipify.org?format=text`
  - `https://ipv4.icanhazip.com/`
  - `https://ifconfig.me/ip`
  - `https://checkip.amazonaws.com/`
  - `https://ident.me/`

验证：

```text
python -m pytest -q -> 21 passed
```

## 端口验证结果可视化

用户反馈：

```text
查询出口效果展示要看实际结果，而不是直接说查询完成。
要多来几个查询，比如访问 Google，或者其他，或者自定义访问。
失败或者正确要能够看到直观结果。
```

处理：

- `/api/test-ports` 支持传入 `urls`。
- 默认验证目标：
  - `https://ipv4.webshare.io/`
  - `https://www.google.com/generate_204`
  - `https://www.gstatic.com/generate_204`
  - `https://www.cloudflare.com/cdn-cgi/trace`
- 每个端口每个目标返回：
  - `ok`
  - `status_code`
  - `elapsed_ms`
  - `body_preview`
  - `error`
- 运行页增加自定义 URL 输入。
- 运行页会展示每个端口的详细验证表，而不是只显示“完成”。

验证：

```text
python -m pytest -q -> 22 passed
```

## 单端口验证和导出

用户反馈：

```text
单个测试结果可以简洁一点放在上面。
全部测试结果可以放在下面。
查询完成可用之后，可以选择导出功能，选择标准格式导出。
```

处理：

- 运行页新增顶部 `quickResult` 简洁结果区。
- 单端口“查出口”：
  - 只查该端口出口 IP。
  - 结果直接显示在上方。
- 单端口“验证”：
  - 调用 `/api/test-ports`，传入 `ports: [port]`。
  - 上方显示出口 IP 和目标成功数量。
  - 下方详细验证表保留每个目标的状态码、耗时、响应片段、错误。
- “验证全部端口”：
  - 详细结果仍展示在下方。
- 新增导出：
  - 代理列表
  - CSV
  - JSON
  - curl 命令

验证：

```text
python -m pytest -q -> 22 passed
```

## 节点测速支持自定义目标 URL

用户反馈：

```text
加入测速功能，测试是否能够到目标网站以及响应的速度。
结合自定义 URL。
从我选择自定义 URL，所有节点就只测试这个自定义 URL。
页面也需要测速排序，不过这个排序测速是到目标网站的响应时间。
现在最大的问题就是测试显示的结果很混乱。
```

处理：

- 测试页新增“测试目标 URL”输入框。
- 如果填写目标 URL：
  - 所有选中节点只请求这个 URL。
  - 不再混入出口 IP 查询。
  - 记录目标 HTTP 状态码、响应时间、响应片段、错误。
  - 节点排序按该目标 URL 的响应时间排序。
- 如果目标 URL 留空：
  - 保持原来的出口 IP/连通性测速。
- 节点列表结果展示简化：
  - 状态
  - 延迟
  - 目标结果

验证：

```text
python -m pytest -q -> 22 passed
```

## 自定义 URL 验证行为修正和端口表排序

用户反馈：

```text
自定义验证 URL 时，只测自定义 URL，不要再测 Google 等默认目标。
每个端口行中添加测试结果和响应时间。
失败排最后，成功按响应时间从小到大排序。
节点测试界面增加一键测试未测速节点，一键删除测试失败节点。
```

处理：

- 运行页自定义 URL 填写后，验证目标变为仅该 URL。
- 默认目标只在自定义 URL 留空时使用。
- 端口表新增“验证结果”列。
- 端口表排序：
  - 成功端口排前。
  - 成功端口按响应时间升序。
  - 失败端口排后。
  - 未测试端口最后。
- 节点测试页新增：
  - 测试未测速
  - 删除失败

验证：

```text
python -m pytest -q -> 22 passed
node --check static/app.js -> passed
```

## SOCKS curl 验证格式修正

用户验证：

```text
curl --proxy "http://127.0.0.1:8006/" https://ipv4.webshare.io/ -> 成功
curl --proxy "socks5://127.0.0.1:8006/" https://ipv4.webshare.io/ -> schannel TLS 失败
```

排查：

- `mixed` inbound 支持 HTTP proxy 和 SOCKS5。
- `curl --proxy socks5://...` 会在本机解析目标域名，再把 IP 交给代理。
- 部分节点对这种 IP 直连方式会出现 TLS 握手失败。
- `curl --proxy socks5h://...` 会把域名交给代理侧解析，更接近浏览器代理行为。

处理：

- UI 的 `复制 SOCKS` 改为复制 `socks5h://127.0.0.1:{port}`。
- 导出保留 `socks5_proxy`，并新增 `socks5h_proxy`。
- CSV 导出增加 `socks5h_proxy` 列。

验证：

```text
curl --proxy "socks5h://127.0.0.1:8006" https://ipv4.webshare.io/ -> 203.10.99.58
curl --proxy "socks5h://127.0.0.1:8009" https://ipv4.webshare.io/ -> 43.156.235.29
python -m pytest -q -> 29 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
```

补充说明：

- `socks5://` 是标准 SOCKS5 链接，给普通客户端使用。
- `socks5h://` 是 curl/libcurl 约定，表示由代理端解析 DNS，不是所有客户端都认识。
- UI 已调整为：
  - `复制 SOCKS`：复制标准 `socks5://127.0.0.1:{port}`
  - `curl SOCKS`：复制 curl 验证用 `socks5h://127.0.0.1:{port}`

## 启动关闭脚本和运行说明

用户反馈：

```text
这个服务不知道怎么启动、怎么关闭，也不知道配置文件在哪里。
```

处理：

- 新增运行文档：`docs/operation.md`
- 新增 Windows 脚本：
  - `scripts/start-service.ps1`
  - `scripts/stop-service.ps1`
  - `scripts/status-service.ps1`
- README 增加脚本入口和运行文档入口。
- 配置改为支持环境变量：
  - `PPM_HOST`：Web 服务监听地址，默认 `127.0.0.1`
  - `PPM_PORT`：Web 服务端口，默认 `9000`
  - `PPM_PROXY_LISTEN_HOST`：代理端口监听地址，默认 `127.0.0.1`
  - `PPM_CLASH_API_ADDR`：sing-box Clash API 地址，默认 `127.0.0.1:9090`
- sing-box inbound 的 `listen` 从固定 `127.0.0.1` 改为读取 `PPM_PROXY_LISTEN_HOST`。

常用命令：

```powershell
.\scripts\start-service.ps1
.\scripts\status-service.ps1
.\scripts\stop-service.ps1
```

服务器允许外部访问代理端口：

```powershell
.\scripts\start-service.ps1 -ProxyListenHost 0.0.0.0
```

验证：

```text
python -m pytest -q -> 29 passed
python -m compileall -q main.py app tests -> passed
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\status-service.ps1 -> passed
```

补充：

- 新增 `config/app.json` 作为固定配置文件：
  - `host`
  - `port`
  - `proxy_listen_host`
  - `clash_api_addr`
- 配置读取优先级：
  1. 环境变量
  2. `config/app.json`
  3. 代码默认值
- `scripts/start-service.ps1`、`stop-service.ps1`、`status-service.ps1` 默认读取 `config/app.json`。

## 手机和平板端前端适配

用户要求：

```text
优化前端界面，适配手机端和平板端。
```

处理：

- 平板端：
  - 主体宽度收窄，间距降低。
  - 顶部 hero、分栏、section header 改为单列。
  - 操作按钮自动换行并撑满可用宽度。
  - 输入框在窄屏下占满行宽。
- 手机端：
  - 顶部 tab 变为 sticky 横向滚动导航。
  - 操作按钮变为 2 列网格，超窄屏变为 1 列。
  - 状态卡、标题、说明文字缩小，避免挤压。
  - quick result 改为纵向堆叠。
  - 表格保持横向滚动，避免列内容压烂。
  - curl 代码块缩短显示宽度，保留copy。
- 交互状态：
  - 禁用按钮增加 wait 光标和透明度。
  - 可复制内容保留 copy 光标和虚线下划线。

验证：

```text
python -m pytest -q -> 29 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
```

## 引擎端口就绪状态优化

用户反馈：

```text
服务重启后刷新页面，复制 curl 到 cmd 测试，部分端口提示 Could not connect to server。
过一会儿或重新打开后又能通。
```

判断：

- `sing-box` 进程 running 不等于所有本地端口都已经完成监听。
- 旧 UI 只显示进程是否运行，用户可能在端口启动窗口期复制命令测试。
- Windows 上服务重启还可能让项目 sing-box 先以孤儿进程形式存在，状态需要更细。

处理：

- `/api/status` 增加：
  - `expected_ports`
  - `listening_ports`
  - `ready`
- `/api/start` 启动 sing-box 后等待映射端口监听，最多等待 8 秒。
- 保存分配后自动重启引擎时，也等待端口监听。
- 前端引擎状态显示：
  - 未运行
  - 启动中 `#pid listening/expected`
  - 运行中 `#pid`
- 端口监听检测在 Windows 使用 `Get-NetTCPConnection`，不主动连代理端口，避免制造无意义连接日志。

验证：

```text
python -m pytest -q -> 29 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
```

## 端口表复制交互优化

用户提出：

```text
curl 命令可以copy，手动去 cmd 验证。
端口号copy本机 http://localhost:8006 等。
```

处理：

- 运行页端口表：
  - 点击端口号复制 `http://localhost:{port}/`
  - 点击 curl 命令复制完整命令
  - 新增 `复制 SOCKS`，复制 `socks5://127.0.0.1:{port}`
- 复制成功或失败会显示在顶部 `quickResult`。
- 可复制内容增加 dotted underline 和 copy 光标。
- `验证全部端口` 按钮验证中会禁用并显示 `验证中...`。
- 导出里的 curl 命令改为跟随当前自定义验证 URL，不再固定 `ipv4.webshare.io`。

验证：

```text
python -m pytest -q -> 28 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
```

## 验证端口时引擎状态误报修复

用户反馈：

```text
开启引擎后点击验证端口，不知道是验证完了还是验证中，引擎自己关闭了。
```

排查结果：

- 服务日志没有出现 `/api/stop`，验证端口没有主动关闭引擎。
- Windows 进程中存在本项目旧 `sing-box.exe`：
  - `ExecutablePath` 在 `proxy-pool-manager/bin/sing-box.exe`
  - 父进程是旧 Python 服务进程
- 当前 FastAPI 服务内存里的 `EngineManager` 没有这个旧进程句柄，因此状态接口可能显示未运行，而端口实际上仍被旧进程监听。
- v2rayN 自己的 sing-box 路径不同，未被项目清理逻辑影响。

处理：

- `EngineManager.status()` 增加项目 sing-box 进程识别：
  - 只匹配本项目 `bin/sing-box.exe`
  - 或命令行包含本项目 `config/sing-box.json`
- `EngineManager.stop()` 会清理本项目自己的孤儿 sing-box 进程。
- `EngineManager.start()` 启动前会先调用 `stop()`，因此会先清理旧项目进程，再启动当前服务可跟踪的新进程。
- 前端“验证全部端口”期间：
  - 按钮变为 `验证中...`
  - 按钮禁用，避免重复点击
  - 顶部结果持续显示 `验证中；进度 X/Y`

验证：

```text
python -m pytest -q -> 28 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
```

## 自定义 URL 验证显示 0/0 修复

用户反馈：

```text
输入自定义 URL 后点击验证，立刻显示：
可用 0/0；目标：https://rawchat.cn
好像没有验证。
```

根因：

- 当前端口映射存在，但 sing-box 引擎未运行时，后端只返回每个端口的失败 `results`。
- 后端没有为这些失败端口返回 `details`。
- 前端最终用 `details.length` 统计总数，因此显示成 `0/0`。

处理：

- 引擎未运行时，后端也会为每个端口生成验证详情：
  - 目标 URL
  - `ok=false`
  - 错误：`sing-box is not running`
- 前端最终统计优先使用 job 的 `total`，不再只依赖 `details.length`。
- 这样同样场景会显示为 `可用 0/15`，并在详细结果中看到每个端口失败原因。

验证：

```text
python -m pytest -q -> 26 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
```

## 端口验证和分配后自动重启修正

用户反馈：

```text
自定义验证 URL 后，验证全部端口仍然像是在测 Google 等默认目标。
重新分配端口之后需要自动重启引擎，否则新 port 测不过，手动重启后才行。
```

处理：

- 运行页自定义验证 URL 填写后：
  - “验证全部端口”只提交该自定义 URL。
  - 开始验证前清空旧的详细结果，避免上一次默认目标结果残留。
  - 每行 curl 示例跟随当前自定义 URL，不再固定显示 `ipv4.webshare.io`。
  - 顶部简洁结果显示本次实际验证目标和可用数量。
- 端口行的“验证结果”优先显示目标 HTTP 状态、响应片段或失败错误。
- `/api/test-ports` 增加回归测试，确认传入自定义 URL 时不会混入默认 Google/gstatic/Cloudflare 目标。
- 保存端口分配时，如果 sing-box 引擎正在运行：
  - 有端口映射：自动重新生成配置并重启引擎。
  - 映射为空：自动停止引擎。
  - 同步清理已不存在端口的出口 IP 缓存。

验证：

```text
python -m pytest -q -> 24 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
GET /static/app.js -> 已返回新版脚本
GET /api/status -> 200
```

## 端口验证逐个显示

用户反馈：

```text
测速、测试端口，测一个显示一个，不要等到全部出现才显示。
```

处理：

- 新增端口验证后台任务接口：
  - `POST /api/test-ports/start`
  - `GET /api/test-ports/jobs/{job_id}`
- “验证全部端口”改为：
  - 先创建验证 job。
  - 前端轮询 job。
  - 每拿到一个端口结果，就立即更新端口表、详细结果表和顶部进度。
  - 已完成端口会立刻参与排序，成功端口按响应时间靠前，失败端口靠后。
- 原 `POST /api/test-ports` 保留，用于单端口验证和兼容调用。
- 后端每完成一个端口就写入 latency 缓存和出口 IP 缓存，刷新页面后仍能看到已完成结果。

验证：

```text
python -m pytest -q -> 25 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
```

## 移动端表格溢出修正

用户反馈：

```text
ui/ux 和移动端，手机端的做的不行，很多都超出屏幕。
```

处理：

- 给节点表、分配表、端口表、验证详情表的每个 `td` 增加 `data-label`。
- 640px 以下不再保留宽表横向滚动：
  - 隐藏 `thead`。
  - 每个 `tr` 展示为一张卡片。
  - 每个 `td` 用 `td::before { content: attr(data-label) }` 显示字段名。
  - 操作按钮在卡片内改成自适应网格。
- 420px 以下进一步改成单列字段布局，避免窄手机两列挤压。
- 长命令、节点 tag、出口 IP、错误信息、按钮文字统一允许换行，避免撑出屏幕。
- 输入框、按钮、select 统一 `min-width: 0`，避免 flex/grid 子项在移动端保持固有宽度。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 29 passed
GET /api/status -> ready=true, listening_ports=8001-8015
```

备注：

- 本次尝试使用内置浏览器做移动端截图验证，但当前环境返回 `Browser is not available: iab`。
- 本机 `playwright-cli` 可运行，但该封装将 URL 当作 selector 处理，未能完成截图；已通过 CSS 断点和服务接口验证改动生效。

## 控制台 UI/UX 紧凑化

用户反馈：

```text
整体的 ui/ux 设计还是不行，然后移动端布局需要紧凑一点。
```

处理：

- 设计方向从展示型页面调整为本地管理控制台：
  - 顶部大标题区改成紧凑状态栏。
  - 背景装饰减少，界面更接近后台工具。
  - 面板、标签栏、状态卡、表格行高整体压缩。
- 移动端优化：
  - 640px 以下隐藏面板说明文字，把空间留给操作和结果。
  - 导入页 URL 输入和导入按钮在手机上改成上下堆叠，避免贴边和误触。
  - 表格卡片 padding、gap、按钮高度继续压缩。
  - 420px 以下仍保留操作按钮两列，避免按钮列表过长。
- 交互层级：
  - 主按钮 hover 保持深色主操作状态。
  - 停止、删除、清空等危险操作使用弱红色语义。
  - 可复制内容 hover 有背景反馈。
- 静态资源版本更新到 `20260605-console-2`，避免浏览器继续使用旧 CSS。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 29 passed
GET / -> contains 20260605-console-2
Chrome headless 390px 运行页 -> scrollWidth == innerWidth, active=dashboard, rows=15
Chrome headless 768px 导入页 -> screenshot generated
```

## sing-box 主引擎被临时测速误杀修复

用户反馈：

```text
运行一段时间后，控制台虽然显示正在运行，但是 curl 连接端口失败。
重新点击启动引擎后又全部可以。
```

根因：

- 主引擎和临时测速都使用同一个 `bin/sing-box.exe`。
- `EngineManager._managed_processes()` 之前按 `ExecutablePath == bin/sing-box.exe` 判断“项目管理的 sing-box”。
- 临时测速结束执行 `engine.stop()` 时，会清理“同路径 sing-box 孤儿进程”，这可能把主运行引擎一起杀掉。
- 主程序虽然有 `monitor()` 方法，但服务启动时没有启动 monitor，所以主引擎被误杀后不会自动恢复，只能手动点“启动引擎”。

处理：

- `EngineManager` 增加 `managed_config_path`。
- 进程归属改为只按命令行中的配置文件路径判断：
  - 主引擎只匹配 `config/sing-box.json`。
  - 临时测速只匹配 `config/sing-box-test.json`。
  - 不再按 `sing-box.exe` 可执行文件路径粗暴匹配。
- `create_app()` 中主引擎 manager 绑定 `SING_BOX_CONFIG_PATH`。
- FastAPI lifespan 启动 `engine_manager.monitor(SING_BOX_CONFIG_PATH)`，服务运行期间如果主 sing-box 异常退出，会尝试恢复。
- 前端新增 5 秒一次轻量状态轮询，只更新顶部状态，减少“前端显示旧状态”的概率。
- 新增回归测试覆盖“按配置文件清理孤儿进程”的行为。

验证：

```text
python -m pytest -q -> 30 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
主引擎 PID=33260
触发单节点临时测速 -> job_status=done
测速后主引擎 PID 仍为 33260, ready=true
curl --proxy http://127.0.0.1:8005 https://rawchat.cn -> Found
curl --proxy http://127.0.0.1:8007 https://rawchat.cn -> Found
curl --proxy http://127.0.0.1:8013 https://rawchat.cn -> Found
curl --proxy http://127.0.0.1:8014 https://rawchat.cn -> Found
```

## sing-box 内核升级到 1.13.13

用户提供：

```text
C:\Users\dsk\Downloads\sing-box-1.13.13-windows-amd64.zip
```

处理：

- 当前项目内核版本：`sing-box 1.13.4`。
- 临时解压用户下载的 zip，确认新版本：`sing-box 1.13.13`。
- 使用 1.13.13 检查当前配置：
  - `config/sing-box.json` -> passed
  - `config/sing-box-test.json` -> passed
- 备份旧内核：
  - `bin/sing-box-1.13.4-backup-20260605-040151.exe`
- 替换 `bin/sing-box.exe` 为 1.13.13。
- 重启 Web 服务和主引擎。

验证：

```text
bin/sing-box.exe version -> 1.13.13
GET /api/status -> ready=true, pid=32676
listening_ports -> 8001-8015
sing-box check -c config/sing-box.json -> passed
curl --proxy http://127.0.0.1:8005 https://rawchat.cn -> Found
curl --proxy http://127.0.0.1:8007 https://rawchat.cn -> Found
curl --proxy http://127.0.0.1:8013 https://rawchat.cn -> Found
```

## 复制链接地址不再硬编码 127.0.0.1

用户反馈：

```text
程序中都是固化 127.0.0.1，配置文件无论设置什么，复制等等需要用到链接的地方都会是这个。
```

根因：

- `proxy_listen_host` 只用于 sing-box inbound 的 `listen`。
- 前端复制链接、curl 命令、导出内容仍然在 `static/app.js` 中硬编码 `127.0.0.1` / `localhost`。
- `0.0.0.0` 是监听地址，不是客户端连接地址，不能直接复制给别人使用。

处理：

- 新增配置项：`proxy_public_host`。
  - 留空：自动使用访问 Web 管理界面的 hostname。
  - 填写：固定使用该公网 IP 或域名。
- `/api/status` 返回：
  - `proxy_listen_host`
  - `proxy_public_host`
  - `proxy_connect_host`
- 前端所有复制、curl、导出链接改为使用 `proxy_connect_host`：
  - 点击端口复制 HTTP 代理
  - `复制 SOCKS`
  - `curl SOCKS`
  - curl 验证命令
  - 导出代理列表 / CSV / JSON / curl
- 更新 `docs/operation.md`，明确 `0.0.0.0` 只能监听，客户端要用服务器 IP 或域名。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 31 passed
GET /api/status -> proxy_listen_host=0.0.0.0, proxy_connect_host=127.0.0.1 when accessed locally
```

## 真实 sing-box 配置被测试污染导致端口只剩 8001

用户反馈：

```text
未重启前，curl 8013/8001 等出现连接失败或 TLS 错误。
要求先保留现场，不要重启。
```

现场证据：

- `/api/status`：
  - `running=true`
  - `ready=false`
  - `expected_ports=8001-8015`
  - `listening_ports=[8001]`
- Windows TCP 监听：
  - 项目 sing-box PID 只监听 `0.0.0.0:8001`
- 当前 `config/sing-box.json`：
  - 只有 1 个 inbound：`port-8001`
  - outbound 是测试用的 `example.com` / `00000000-0000-0000-0000-000000000000`
- `config/assignments.json`：
  - 仍然有 15 个端口映射。
- 用当前应用状态重新生成配置，应该得到 15 个 inbound。

根因：

- 之前运行 pytest 时，API 测试直接使用真实 `SING_BOX_CONFIG_PATH`。
- 测试中的假节点和假端口把真实 `config/sing-box.json` 覆盖成了测试配置。
- 后续主 sing-box 读到这个被污染的配置，所以只监听 8001；Web 状态里的映射仍然是 15 个，产生状态漂移。

处理：

- `tests/test_api.py` 增加 autouse fixture：
  - 每个 API 测试把 `api_module.SING_BOX_CONFIG_PATH` 指向 `tmp_path/sing-box.json`。
  - 测试不再写真实 `config/sing-box.json`。
- `/api/status` 增加：
  - `config_ports`
  - `config_matches_state`
- 前端引擎状态增加“配置不一致”显示：
  - 如果运行配置端口数量和应用映射数量不一致，不再误显示“启动中”。
- 静态资源版本更新到 `20260605-config-drift-1`。

验证：

```text
python -m pytest -q -> 31 passed
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
测试前后 config/sing-box.json SHA256 一致 -> 测试不再污染真实配置
```

备注：

- 按用户要求，本次诊断期间没有重启当前服务和 sing-box。
- 当前现场要恢复，需要重新启动引擎，让程序用 `assignments.json` 重新生成 `config/sing-box.json` 并重启 sing-box。

## Windows / Linux 快速安装脚本

用户问题：

```text
最后一个就是安装的问题了，如何快速的安装到 windows或者linux中，内核不一样吧
```

实现：

- 新增 `scripts/install-windows.ps1`：
  - 创建 `.venv`。
  - 安装 `requirements.txt`。
  - 下载或使用指定 zip 安装 Windows `sing-box.exe`。
  - 缺失时生成默认 `config/app.json`。
  - 替换内核前检查项目 sing-box 是否仍在运行，避免覆盖正在使用的二进制。
  - 覆盖旧内核前自动备份到 `bin/sing-box-backup-时间戳.exe`。
- 新增 `scripts/install-linux.sh`：
  - 创建 `.venv`。
  - 安装 `requirements.txt`。
  - 按 `uname -m` 自动选择 `linux-amd64` 或 `linux-arm64` sing-box。
  - 缺失时生成默认 `config/app.json`。
  - 替换内核前检查项目 sing-box 是否仍在运行。
  - 覆盖旧内核前自动备份到 `bin/sing-box-backup-时间戳`。
- 新增 Linux 运行脚本：
  - `scripts/start-service.sh`
  - `scripts/stop-service.sh`
  - `scripts/status-service.sh`
- 优化 Windows 运行脚本：
  - `scripts/start-service.ps1` 优先使用 `.venv\Scripts\python.exe`。
  - `scripts/status-service.ps1` 显示配置中的 Web host。
- 更新 `README.md` 和 `docs/operation.md`：
  - 增加 Windows/Linux 一键安装。
  - 明确 Windows 用 `.exe`，Linux 用无后缀二进制。
  - 明确 Linux x64/ARM64 对应的 sing-box release 包。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
PowerShell Parser.ParseFile scripts/*.ps1 -> passed
bash -n scripts/*.sh -> passed
python -m pytest -q -> 31 passed
测试前后 config/sing-box.json SHA256 一致 -> 测试不污染真实配置
```

## 状态刷新和启停体感慢的性能优化

用户反馈：

```text
现在感觉性能有点问题，启动和暂停引擎都很慢，有时候网页控制台刷新也很慢。
```

现场和诊断：

- 日志里 `/api/status` 请求非常密集，并且可能同时来自本机和局域网访问地址。
- 原 `/api/status` 热路径在 Windows 上每次都会启动 PowerShell 执行 `Get-NetTCPConnection`。
- 状态轮询频繁时，这个子进程开销会直接拖慢网页刷新。
- 当引擎状态显示运行但端口实际不通时，原 socket 探测按端口串行超时，15 个端口会接近 1 秒。
- 启动/停止后前端又做完整刷新，会连带拉 `/api/status`、`/api/nodes`、`/api/ports`，进一步放大卡顿。

处理：

- `_listening_local_ports()` 去掉 PowerShell 子进程，改为纯 socket 探测。
- 端口探测改成并发执行，避免 15 个端口串行等待超时。
- 引擎未运行时不再探测端口。
- `EngineManager.stop()` 的优雅退出等待从 3 秒降到 1 秒，超时后直接 kill。
- 前端后台轮询从 5 秒改为 15 秒。
- 页面不可见时暂停状态轮询。
- 启动/停止引擎后只刷新状态，不再完整重绘节点和端口表。
- 端口验证结束后直接使用本地结果更新表格，减少额外完整刷新。
- `uvicorn.run()` 关闭 access log，避免状态轮询刷屏写日志。
- 增加 `test_listening_local_ports_uses_socket_probe`，覆盖 socket 端口探测。

基准：

```text
优化前：/api/status 在端口不通场景约 930ms
优化后：/api/status 在同场景约 64ms
```

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 32 passed
测试前后 config/sing-box.json SHA256 一致 -> 测试不污染真实配置
```

## app.json 修改后 sing-box 仍使用旧 Clash API 端口

用户反馈：

```text
FATAL start service: finish-start clash server: external controller listen error:
listen tcp 127.0.0.1:9090: bind: Only one usage of each socket address...
点击启动引擎后这是什么问题？我不是已经改了端口号吗？
```

现场：

- `config/app.json` 已经是：
  - `clash_api_addr = 127.0.0.1:10000`
- 但 `config/sing-box.json` 仍然写着：
  - `experimental.clash_api.external_controller = 127.0.0.1:9090`

根因：

- `app/settings.py` 在 Web 后端启动时读取 `config/app.json`，并把 `CLASH_API_ADDR`、`PROXY_LISTEN_HOST`、`PROXY_PUBLIC_HOST` 固定成模块常量。
- 用户在 Web 后端运行期间修改 `config/app.json` 后，点击“启动引擎”仍使用旧常量生成 sing-box 配置。
- 所以用户虽然已经把 Clash API 改成 `10000`，但旧后端仍按 `9090` 写配置并启动 sing-box。

处理：

- `app/settings.py` 增加运行时读取函数：
  - `current_proxy_listen_host()`
  - `current_proxy_public_host()`
  - `current_clash_api_addr()`
- `app/generator.py` 改为生成 sing-box 配置时动态读取：
  - inbound `listen`
  - Clash API `external_controller`
- `app/api.py` 状态和复制地址也改为动态读取代理监听/公开地址。
- 增加测试：`test_generate_config_uses_current_runtime_addresses`。

验证：

```text
当前 config/app.json -> clash_api_addr=127.0.0.1:10000
内存生成 sing-box 配置 -> external_controller=127.0.0.1:10000
内存生成 sing-box 配置 -> inbound listen=0.0.0.0
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 33 passed
测试前后 config/sing-box.json SHA256 一致 -> 测试不污染真实配置
```

说明：

- sing-box 配置相关项现在会在点击“启动引擎”时重新读取 `config/app.json`。
- Web 服务自己的 `host/port` 仍然需要重启 Web 服务才能生效，因为它们决定 uvicorn 监听在哪里。

## Base64 订阅响应混入尾部文本导致无法解析

用户反馈：

```text
访问链接得到的是一大段 Base64 内容，末尾混入 zheg1fefji1https://...
无法解析这个订阅链接
```

根因：

- 订阅响应主体前半段是标准 Base64 订阅，解码后是多行 `hysteria2://` 和 `vless://` 节点。
- 响应末尾混入了额外非 Base64 文本和订阅 URL。
- 原解析逻辑只尝试把整段文本作为 Base64 解码；只要尾部混入杂质，整段解码失败，就不会进入节点解析。

处理：

- 新增 `BASE64_SUBSCRIPTION_RE`。
- 新增 `_decode_base64_subscription_text()`：
  - 优先尝试整段 Base64 解码。
  - 失败后从混合文本中提取最长 Base64 候选块。
  - 只接受解码后包含受支持节点协议的内容。
- `parse_text()` 改为使用宽容 Base64 订阅解码。
- 新增测试：`test_parse_base64_subscription_with_trailing_noise`。

验证：

```text
python -m pytest tests/test_parser.py -q -> 8 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
python -m pytest -q -> 34 passed
测试前后 config/sing-box.json SHA256 一致 -> 测试不污染真实配置
```

## 直接导入订阅响应文本可以，但 URL 导入失败

用户反馈：

```text
我发现直接导入这个访问后的文本可以，解析不了链接
```

判断：

- 解析器已经可以解析访问后的订阅文本。
- URL 导入失败更可能发生在 HTTP 拉取层：程序拿到的响应和浏览器访问拿到的不一致。
- 常见原因：
  - 订阅服务返回跳转，httpx 原来没有开启 `follow_redirects`。
  - 订阅服务根据 `User-Agent` 判断客户端，默认 Python/httpx UA 可能拿到 HTML、错误页或空响应。
  - 订阅服务需要更宽泛的 `Accept`。

处理：

- `import_nodes(url=...)` 的 `httpx.AsyncClient` 调整为：
  - `timeout=30`
  - `follow_redirects=True`
  - `User-Agent: Clash.Meta/1.18.0 ProxyPoolManager/1.0`
  - `Accept: text/plain, application/octet-stream, application/yaml, text/yaml, */*`
- 新增测试：`test_import_nodes_url_uses_subscription_client_options`。

验证：

```text
python -m pytest tests/test_parser.py -q -> 9 passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 35 passed
测试前后 config/sing-box.json SHA256 一致 -> 测试不污染真实配置
```

## VPS 代理端口能查出口 IP，但访问 Google 被 reset

用户反馈：

```text
curl --proxy "http://43.156.235.29:9517/" https://ipv4.webshare.io/
-> 146.235.35.124

curl --proxy "http://43.156.235.29:9517/" https://google.com
-> curl: (56) Recv failure: Connection was reset
```

判断：

- `ipv4.webshare.io` 能返回出口 IP，说明公网客户端可以连接 VPS 代理端口，且代理链路至少可以访问一个简单 HTTPS 目标。
- `google.com` reset 发生在目标站点访问阶段，不等同于端口未开放。
- 需要区分：
  - VPS 本机通过 `127.0.0.1:端口` 是否也失败。
  - 只有公网访问失败，还是 VPS 本机也失败。
  - 是否只有 Google/gstatic 失败，Cloudflare/IP 查询类目标正常。

处理：

- 在 `docs/operation.md` 增加 VPS 诊断命令：
  - VPS 本机测试 `127.0.0.1:端口`。
  - 客户端测试 `服务器IP:端口`。
  - 分别测试 `ipv4.webshare.io`、`www.google.com/generate_204`、`www.gstatic.com/generate_204`、`www.cloudflare.com/cdn-cgi/trace`。
- 新增配置项 `domain_resolve_strategy`：
  - 默认空字符串，不改变现有行为。
  - 可设置为 `ipv4_only`，用于排查/规避 VPS 上 Google 目标走 IPv6 或 DNS 结果异常的问题。
- 实现方式：
  - 没有使用 outbound 上已废弃的 `domain_strategy` 字段。
  - 使用 sing-box 1.13 合法的 route `resolve` action：
    - 先按 inbound 做 `resolve`，设置 `strategy=ipv4_only`。
    - 再 route 到对应节点 outbound。
- 安装脚本默认 `config/app.json` 增加 `domain_resolve_strategy` 字段。

验证：

```text
PPM_DOMAIN_RESOLVE_STRATEGY=ipv4_only 生成临时 sing-box 配置
bin/sing-box.exe check -c tmp/sing-box-ipv4-check.json -> passed
python -m pytest tests/test_generator.py -q -> 5 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
python -m pytest -q -> 35 passed
测试前后 config/sing-box.json SHA256 一致 -> 测试不污染真实配置
```

## 自定义 URL 后 curl 验证命令仍固定为出口 IP URL

用户反馈：

```text
自定义url填入后，curl验证不会改变，还是固定为
curl --proxy "http://127.0.0.1:8006/" https://ipv4.webshare.io/
```

根因：

- 之前为了让“查出口”始终使用 `https://ipv4.webshare.io/`，新增了 `EXIT_IP_CHECK_URL`。
- 但表格里的“curl 验证”和导出 curl 也误用了这个固定常量。
- 导致自定义验证 URL 只影响实际验证请求，不影响 UI 中展示/复制/导出的 curl 命令。

处理：

- `renderPortsTable()` 中的 curl 验证命令改为使用 `activeValidationUrl()`。
- `exportRows()` 中的 curl 导出也改为使用 `activeValidationUrl()`。
- “查出口”仍保留固定出口 IP 查询目标。
- 静态资源版本更新到 `20260605-curl-target-1`。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 35 passed
```

## 代理列表导出格式去除注释并增加 SOCKS5 列表

用户反馈：

```text
优化一下订阅导出的格式，不要在后面加一些奇奇怪怪的东西
socks5://user:pass@192.168.1.1:1080
http://192.168.1.1:8080
这种标准格式每行一个
```

处理：

- “HTTP 列表”导出改为每行一个标准 URI：
  - `http://host:port`
- 新增 “SOCKS5 列表”：
  - `socks5://host:port`
- 新增 “HTTP+SOCKS5”：
  - 每个端口输出两行，分别是 HTTP 和 SOCKS5。
- 去掉原来的尾部注释：
  - 不再输出 `# 节点名 出口IP`。
- CSV/JSON/curl 导出保留结构化字段。
- 静态资源版本更新到 `20260605-export-clean-1`。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 35 passed
```

## 分配界面增加一键清空端口分配

用户反馈：

```text
分配界面需要有一个一键清空的按钮，清空原有的端口分配
```

处理：

- “分配”页新增 `清空分配` 按钮。
- 点击后：
  - 清空所有端口输入框。
  - 取消所有分配勾选。
  - 调用 `PUT /api/assign` 保存空映射。
  - 运行页端口表同步变为空。
  - 状态栏刷新映射数量。
- 清空后禁用分配表的“自动勾选可用节点”行为，避免刚清空又被 UI 自动勾回。
- 静态资源版本更新到 `20260605-clear-assign-1`。
- 新增测试：`test_api_assign_can_clear_mappings`。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 36 passed
```

## 测试/分配/运行界面联动细节修复

用户反馈：

```text
1. 测试界面只选可用按钮失灵，应让没通过的节点不出现在分配界面。
2. 测试界面新增一键测试失败节点。
3. 分配界面保存新映射后应自动重启引擎。
4. 运行界面测试自定义 URL，刚点击有绿色，测完全部红色。
5. 运行界面自定义 URL 验证结果太详细，破坏布局；需要能移除不想要的映射并重启引擎。
```

处理：

- 测试页 `只选可用`：
  - 选中测试通过节点。
  - 设置分配页过滤器，只显示可用节点。
  - 自动切换到分配页。
- 测试页新增 `测试失败节点`：
  - 只重新测速已有失败结果的节点。
  - 不再需要重新测试全部节点。
- 分配页保存映射：
  - 继续复用后端 `PUT /api/assign` 的自动重启逻辑。
  - 前端完成提示会明确显示 `已自动重启引擎` 或 `已停止引擎`。
- 运行页验证状态：
  - 增加 `validatingPorts` 状态。
  - 验证中端口显示 `验证中`，不再沿用旧的绿色可用状态造成误解。
- 运行页验证结果：
  - 由大表格改成紧凑 chip 摘要。
  - 只显示目标、成功/失败、状态码、耗时。
  - 不再展示长响应体/错误文本撑破布局。
- 运行页新增 `移除映射`：
  - 端口行和验证结果卡片都可移除当前端口映射。
  - 调用 `PUT /api/assign` 保存剩余映射。
  - 如果引擎运行中，后端自动重启引擎。
- 静态资源版本更新到 `20260605-flow-polish-1`。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 36 passed
```

## curl 验证命令恢复简洁出口 IP 查询

用户要求：

```text
把 curl --proxy "http://127.0.0.1:8006/" https://ipv4.webshare.io/ 恢复简洁模式
```

处理：

- 前端新增固定常量：
  - `EXIT_IP_CHECK_URL = "https://ipv4.webshare.io/"`
- 运行页“curl 验证”列固定生成：
  - `curl --proxy "http://host:port/" https://ipv4.webshare.io/`
- 导出格式中的 curl 命令也固定使用 `https://ipv4.webshare.io/`。
- 自定义验证 URL 仍只影响“验证”按钮和“验证全部端口”，不再影响表格里的简洁 curl。
- 静态资源版本更新为 `20260605-curl-simple-1`。

验证：

```text
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
python -m pytest -q -> 35 passed
测试前后 config/sing-box.json SHA256 一致 -> 测试不污染真实配置
```

## 启动引擎时报端口占用并伴随测速临时进程残留

用户反馈：

```text
FATAL start service: start inbound/mixed[port-8001]:
listen tcp4 0.0.0.0:8001: bind: Only one usage of each socket address...

运行界面验证端口全部失败，提示引擎没开，然后打开不了。
初步判断是上面几个功能修改造成的。
```

诊断：

- 日志中先发现 `EngineManager.start()` 并发 `start/stop` 时出现：
  - `AttributeError: 'NoneType' object has no attribute 'poll'`
- 这是后端生命周期竞态：
  - 前端新增“保存映射自动重启”“运行页移除映射”“测速失败节点”“验证任务”等功能后，请求更容易交错。
  - 一个请求正在 `start()` 的 settle 阶段，另一个请求可能执行 `stop()` 并把 `self.process` 清空。
- 现场还发现本项目 `config/sing-box-test.json` 的测速临时 sing-box 进程残留。
- 同时 Windows 上 `8001-8004` 和 `10000` 没有显示监听进程，但 Python 直接 bind 也失败，说明端口已处于系统不可绑定状态。

处理：

- `EngineManager` 增加 `asyncio.Lock`：
  - 串行化 `start()` / `stop()`。
  - `start()` 使用局部 `proc` 检查进程状态，避免 `self.process` 被并发清空。
- `EngineManager.start()` 新增启动前端口预检查：
  - 解析 sing-box 配置中的 inbound `listen_port`。
  - 同时解析 `experimental.clash_api.external_controller` 端口。
  - 启动前最多等待 6 秒，直到端口可 bind。
  - 如果仍不可用，返回明确错误：
    - `Ports are not available: ...`
  - 不再让 sing-box 半启动后抛 FATAL。
- 正式引擎启动/运行中保存映射重启时，若节点测速任务正在运行，返回 409：
  - 避免测速临时引擎与正式引擎生命周期互相踩。
- 增加回归测试：
  - 并发 `start/stop` 不再污染 `self.process`。
  - 配置端口解析包含 inbound 和 clash API。
  - 端口不可用时不会调用 `Popen` 拉起 sing-box。
  - 节点测速运行中启动正式引擎会返回 409。

验证：

```text
python -m pytest -q -> 40 passed
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
```

## 端口容错增强与节点快速测速

用户反馈：

```text
确实增加更多容错，比如端口是否占用等等问题。
测速的我觉得有点问题，我在 v2ray 中测速很快，控制台测速慢的要死，测出来的速度也不准确。
```

诊断：

- 原节点测速同时做了两件事：
  - 访问 `https://www.gstatic.com/generate_204` 测响应。
  - 继续访问多个出口 IP 查询站点。
- 出口 IP 查询会被远端限流、超时或重置，导致普通测速被拖慢。
- 节点多时原实现同时发起所有测试，容易造成本机 sing-box、系统端口和远端节点拥塞。
- 原节点测速使用 SOCKS 代理路径，而运行页和用户 curl 主要使用 HTTP mixed 代理路径，测试路径不一致。

处理：

- 普通节点测速改为“快速 URL 延迟”：
  - 默认只测 `https://www.gstatic.com/generate_204`。
  - 自定义 URL 存在时只测自定义 URL。
  - 不再默认查询出口 IP。
  - 改用 HTTP mixed 代理路径：`http://127.0.0.1:port`。
  - 单节点超时缩短到 5-6 秒。
  - 并发限制为 12，避免大量节点同时压垮本地临时 sing-box。
- `测速并按 IP 去重` 才额外查询出口 IP：
  - 只有需要同出口 IP 去重时才付出出口 IP 查询成本。
- 新增端口可用性 API：
  - `POST /api/ports/check`
  - 返回每个端口 `available/busy/invalid`。
- 正式引擎未运行时，如果 Clash API 端口已被占用：
  - 自动生成不带 `experimental.clash_api` 的 sing-box 配置。
  - 代理端口优先启动，不再因为非核心控制端口被占用而整体失败。
- 分配页“自动分配”改为异步分配可用端口：
  - 调用 `/api/ports/check`。
  - 自动跳过当前系统不可绑定端口。
  - 可用端口不足时给出明确提示。
- 静态资源版本更新为：
  - `20260605-speed-port-guard-1`

验证：

```text
python -m pytest -q -> 43 passed
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
```

## 端口分配深度容错与状态语义整理

用户反馈：

```text
10000 原来就是配置文件中的值，现在说它忙碌？
状态太混乱，bug 太多，尤其是端口分配上：
一不能清楚，二不能自己避让，需要深刻优化。
```

问题：

- `10000` 是 `config/app.json` 中的 Clash API 控制端口，不是代理映射端口。
- Windows 返回 `10000` 不可绑定时，旧 UI 只显示 busy，未解释它是控制端口。
- 前端原“自动分配”只是从起始端口递增，无法真正知道系统是否可绑定。
- 清空分配后，前端仍可能保留分配筛选、验证结果、验证中状态，造成“看起来没清干净”。

处理：

- `/api/ports/check` 的状态语义细化：
  - `available`：可绑定。
  - `busy`：系统不可绑定。
  - `reserved-clash-api`：配置中的 Clash API 控制端口，不能分配给代理。
  - `project-listening`：本项目引擎正在监听。
  - `invalid`：端口非法。
- 新增 `/api/ports/allocate`：
  - 后端从起始端口开始扫描。
  - 自动跳过已使用端口。
  - 自动跳过 `reserved-clash-api`。
  - 自动跳过系统不可绑定端口。
  - 返回实际分配端口和被避让端口详情。
- 前端“自动分配”改为调用 `/api/ports/allocate`：
  - 不再自己猜端口。
  - 成功后提示避让了多少不可用端口。
- 前端“清空分配”增强：
  - 清空端口输入。
  - 清空端口映射。
  - 清空验证结果。
  - 清空验证中状态。
  - 解除“只显示可用节点”的分配过滤。
- Web 后端重启后，如果检测到已有本项目 sing-box 进程：
  - 状态仍显示运行中。
  - 不再把 `project sing-box process detected` 塞进 `last_error`，避免误导为异常。
- 静态资源版本更新为：
  - `20260605-port-allocator-1`

验证：

```text
python -m pytest -q -> 44 passed
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
```

## 清空分配后无法再次自动分配

用户反馈：

```text
分配界面，点击清除分配后，无法再分配端口
```

根因：

- 清空分配时前端会取消所有节点勾选。
- 自动分配只处理已勾选且没有端口的节点。
- 所以清空后再点自动分配，会出现没有目标节点可分配的状态。

处理：

- 自动分配增强：
  - 如果当前没有已勾选目标，自动选择当前分配表中的可用节点。
  - 如果没有可用节点，则选择当前分配表中的全部节点。
  - 再调用后端 `/api/ports/allocate` 分配可用端口。
- 静态资源版本更新为：
  - `20260605-auto-assign-after-clear-1`

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 44 passed
```

## 集成 ProxyAdmin 质量检测 API

用户要求：

```text
了解 proxy-port-api 的接口，内容可靠。
集成到运行界面，参数暴露给用户填写，并把返回结果好好展示。
```

文档确认：

- `import(urls, replace?)`
  - 导入 HTTP 代理端口。
  - 返回 `url / host / port / id`。
- `query(ids, concurrency?)`
  - 对 ID 做质量检测。
  - 逐个返回检测结果。
  - 包含 `exit_ip / country / score / grade / items[]`。
- `remove(options, concurrency?)`
  - 删除指定 ID、失败节点或未使用节点。

处理：

- 后端新增 ProxyAdmin 集成接口：
  - `POST /api/proxy-admin/check/start`
  - `GET /api/proxy-admin/jobs/{job_id}`
  - `POST /api/proxy-admin/remove`
- 后端直接用 Python `httpx` 调用 ProxyAdmin HTTP API，不依赖 Node 运行时。
- 运行界面新增 ProxyAdmin 检测区域，暴露参数：
  - API 地址
  - Bearer Token
  - 代理 Host
  - 替换 From
  - 替换 To
  - 并发数
- 支持操作：
  - 导入并检测
  - 重试失败
  - 删除失败
  - 删除未使用
- 检测结果展示：
  - ID
  - host:port
  - 出口 IP
  - 国家
  - score
  - grade
  - 每个 target 的 `pass / warn / fail`、HTTP 状态码、延迟、message
- 结果按 job 轮询更新，一个结果返回一个结果展示。
- 静态资源版本更新为：
  - `20260606-proxy-admin-1`

验证：

```text
python -m pytest -q -> 46 passed
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
```

## ProxyAdmin 结果合并到端口表

用户反馈：

```text
功能可以，显示不行。
需要将 proxyAdminResults 集成到 id="portsTable"，集成到它们自己对应的行。
现在太乱了，错误折叠展开破坏布局。
```

处理：

- 前端新增端口级索引：
  - 根据 `proxyAdminImported[].port` 与 `proxyAdminResults[id]` 建立映射。
- `portsTable` 新增 `ProxyAdmin` 列：
  - 每个端口行直接展示对应质量检测结果。
  - 展示 `grade / score / exit_ip / country`。
  - 目标检测以短标签展示：
    - `base`
    - `oa`
    - `claude`
    - `gemini`
  - `pass / warn / fail` 用颜色区分。
- 详细错误不再展开成大块内容：
  - 放入行内 `title` tooltip。
  - 避免撑破表格和移动端布局。
- 原独立结果区改成紧凑摘要：
  - 通过数量
  - 失败数量
  - 提示“详细结果已合并到端口表”
- 静态资源版本更新为：
  - `20260606-proxy-admin-inline-1`

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 46 passed
```

## 保存 ProxyAdmin 检测配置

用户反馈：

```text
优化一下保存配置，ProxyAdmin 检测的配置，api，token 等，不然每次都要重新输入
```

处理：

- 后端新增配置接口：
  - `GET /api/proxy-admin/config`
  - `PUT /api/proxy-admin/config`
- 配置保存到 `config/app.json` 的 `proxy_admin` 子对象。
- 保存内容：
  - `base_url`
  - `token`
  - `proxy_host`
  - `replace_from`
  - `replace_to`
  - `concurrency`
- 写入时保留 `app.json` 原有字段，例如：
  - `host`
  - `port`
  - `proxy_listen_host`
  - `clash_api_addr`
- 前端运行页：
  - 页面加载自动读取并回填 ProxyAdmin 配置。
  - 新增 `保存配置` 按钮。
  - 执行导入检测、重试失败、删除失败、删除未使用前会自动保存当前配置。
- 静态资源版本更新为：
  - `20260606-proxy-admin-config-1`

验证：

```text
python -m pytest -q -> 47 passed
node --check static/app.js -> passed
python -m compileall -q main.py app tests -> passed
```

## ProxyAdmin 配置与结果展示细节优化

处理：

- CSS 静态资源版本从旧的 `20260605-flow-polish-1` 更新到：
  - `20260606-proxy-admin-config-1`
  - 避免浏览器继续缓存旧样式。
- 删除失败节点后：
  - 前端同步移除已删除 ID 对应的 ProxyAdmin 结果。
  - 端口表立即刷新，不再残留旧失败状态。
- 删除未使用节点后：
  - 如果返回 ID 命中当前展示结果，也同步清理。
- ProxyAdmin 配置区新增提示：
  - 配置保存到本机 `config/app.json`。
  - 如果 Web 面板暴露到公网，不建议保存长期有效 Token。

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 47 passed
python -m compileall -q main.py app tests -> passed
```

## ProxyAdmin 运行表排序与行内删除

用户反馈：

```text
运行界面 ProxyAdmin 检测测试后，等级和分数高的，能通过的，放前面，fail 的放后面。
curl SOCKS 按钮去掉，换成移除代理，点击会删除远端的数据，调用删除接口。
```

处理：

- 运行页端口表排序新增 ProxyAdmin 优先级：
  - 已通过检测的端口排最前。
  - 同为通过时按等级 `A/B/C/D/F` 排序。
  - 同等级按分数高低排序。
  - 失败或包含失败目标的结果排最后。
  - 没有 ProxyAdmin 检测结果的端口放在通过和失败之间，并继续按本地延迟排序。
- 移除端口表里的 `curl SOCKS` / `socks5h://...` 复制按钮。
- 新增 `移除代理` 按钮：
  - 按当前端口找到对应 ProxyAdmin 导入 ID。
  - 调用 `/api/proxy-admin/remove` 删除远端代理。
  - 成功后同步清理当前页面中的 ProxyAdmin 检测结果和导入记录。
  - 不删除本地端口映射，避免误伤本地分配。
- 静态资源版本更新为：
  - `20260606-proxy-admin-actions-1`

验证：

```text
node --check static/app.js -> passed
```

## GeoIP 地区检测

用户希望在测试阶段兼顾性能和准确性地检测地区。

处理：

- 新增 `app/geoip.py`：
  - 使用在线 `ip-api.com` 查询国家、城市、ASN、运营商。
  - 跳过内网、保留地址等非公网 IPv4。
  - 结果缓存到状态文件，默认 TTL 为 168 小时。
  - 查询失败只记录错误，不影响测速主流程。
- 状态模型新增：
  - `LatencyResult.geoip`
  - `ExitIpCache.geoip`
  - `AppState.geoip_cache`
- 后端接入：
  - 节点测速拿到出口 IP 后调度后台 GeoIP 查询。
  - 端口验证拿到出口 IP 后附带已缓存地区，并触发后台查询。
  - 单独“查出口”接口会同步查询 GeoIP，方便立即看到地区。
- 前端展示：
  - 节点测试表新增地区列。
  - 分配表新增地区列。
  - 运行端口表新增地区列。
  - 导出 JSON/CSV 增加 `geoip` 字段。
- 静态资源版本更新为：
  - `20260606-geoip-1`

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 51 passed
python -m compileall -q main.py app tests -> passed
```

## 临时测速端口占用修复

问题：

```text
Ports are not available: 19001, 19002, ...
```

说明：

- `19001+` 不是用户分配给账号使用的映射端口。
- 它们是节点测速时临时 sing-box 使用的内部端口。
- 正常情况下测速完成后会自动停止，不应该长期占用。

原因：

- 如果上一次测速的临时 sing-box 残留，下一次测速可能遇到 `19001+` 占用。
- 原端口探测只检查单一绑定场景，和 sing-box 启动前的端口检查不完全一致，可能误判端口可用。

处理：

- 每次节点测速前，先清理 `sing-box-test.json` 对应的残留临时 sing-box。
- 测速临时端口分配时同时检查：
  - `0.0.0.0`
  - `127.0.0.1`
- 如果某个 `19001+` 被占用，会自动跳过，不会影响用户映射端口池。

验证：

```text
python -m pytest tests/test_tester.py tests/test_api.py -q -> 40 passed
python -m compileall -q main.py app tests -> passed
```

## 节点测试表长错误和出口 IP 展示优化

问题：

```html
<span class="latency-pill latency-unknown">http://cp.cloudflare.com/generate_204: HTTP 502; ...</span>
```

以及节点测试表只显示地区，看不到出口 IP。

处理：

- 延迟列失败时只显示 `失败`，不再把完整错误塞进小标签。
- 完整错误保留在 `title`，鼠标悬停可查看。
- 节点测试表新增 `出口 IP` 列。
- 运行验证表的延迟列也加上相同的 hover 详情。

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 64 passed
```

## ProxyAdmin 名称前缀去重和补空位

问题：

```text
名称前缀不能重复。远端如果已有代理12/代理13，或者删除了代理12，下次新增不能和已有名称冲突。
```

处理：

- ProxyAdmin 上传前先拉取远端代理列表。
- 按当前 `名称前缀` 提取已有编号。
- 新增名称使用最小可用编号，并追加地区后缀：
  - `代理1-jp.Tokyo`
  - `代理2-US.LosAngeles`
  - `代理3-none`
- 已有名称会按编号占位：
  - 已有 `代理1-jp.Tokyo, 代理2-US.LosAngeles, 代理4-none` -> 新增先用 `代理3-...`
  - 已有 `代理12-US.xx, 代理13-JP.xx` -> 不会重复使用 `代理12/代理13`
  - 删除了 `代理12-...` 但 `代理13-...` 还在 -> 下次可补 `代理12-...`
- 地区后缀来源：
  - 优先使用端口对应节点的 GeoIP。
  - 有国家和城市：`国家.城市`
  - 只有国家：`国家`
  - 没有地区：`none`
- 自定义前缀单独计算，不受其他前缀影响。
- 创建后如果远端返回的名称仍是 `default` 或不是期望名称：
  - 自动尝试 `PUT /api/v1/admin/proxies/{id}` 更新名称。
  - 如果 `PUT` 不可用，再尝试 `PATCH /api/v1/admin/proxies/{id}`。
  - 更新失败不影响整批检测，但会在导入结果中记录 `name_update_error`。

验证：

```text
python -m pytest tests/test_api.py tests/test_proxy_admin.py -q -> 33 passed
python -m compileall -q main.py app tests -> passed
```

## 节点测速出口 IP 和地区空白修复

问题：

```text
测速界面查询地区和出口 IP 没有生效，结果全是空白。
```

原因：

- 前端已传 `include_geoip=true`，问题不在开关。
- 出口 IP 查询直接使用响应正文，遇到 `Found`、HTML、空响应或非 IP 文本时，容易得不到有效 IP。
- GeoIP 查询之前是后台异步补充，测速任务刚完成时前端立即刷新，可能还没写回 `job.results`。

处理：

- 新增 `extract_public_ipv4()`：
  - 从响应正文中提取 IPv4。
  - 跳过内网、回环、保留地址和无效文本。
  - 当前查询服务无有效 IP 时自动尝试下一个出口 IP 服务。
- 节点测速任务在 `include_geoip=true` 时，会在任务完成前同步补齐 GeoIP，并更新 `job.results`。
- 前端最终拿到的测速结果会直接包含：
  - `exit_ip`
  - `geoip`

验证：

```text
python -m pytest tests/test_api.py tests/test_tester.py -q -> 40 passed
python -m compileall -q main.py app tests -> passed
```

## 自动分配端口后输入框被清空修复

问题：

```text
分配端口提示已分配成功，但分配表里看不见端口。
```

原因：

- 自动分配端口时，端口先写入当前页面的输入框。
- 这时映射还没有点击保存，内存里的 `ports` 状态仍是旧状态。
- 之前为了立即排序，在自动分配后调用了 `renderAssignTable()`。
- 表格重绘时从旧 `ports` 状态读取端口，导致刚写入输入框的未保存端口被清空。

处理：

- 取消自动分配后的即时重绘。
- 自动分配结果保留在输入框中，用户点击保存映射后再由后端状态刷新并排序。

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 63 passed
python -m compileall -q main.py app tests -> passed
```

## GeoIP 覆盖率与布局修复

用户反馈：

```text
地区很多没有显示，7 个只有 1 个显示了地区；结果太长破坏布局。
```

原因：

- 端口验证自定义 URL 时，如果目标页面不是 IP 查询接口，响应体里不会包含出口 IP。
- 没有出口 IP 就无法查询 GeoIP，所以只有之前查过出口 IP 或命中缓存的少数端口显示地区。
- 前端直接显示完整地区、ASN、运营商文本，容易撑宽表格。

处理：

- 端口验证成功但没有解析到出口 IP 时，兜底调用已有出口 IP 查询逻辑。
- 兜底查询增加 3 秒超时，避免某个端口拖慢整个批量验证。
- `/api/ports` 发现已有出口 IP 但没有 GeoIP 时，会触发后台补查。
- GeoIP 结果增加：
  - `summary`：完整说明，用于 hover title。
  - `compact`：短格式，用于表格显示。
- 前端地区列改成短标签，例如：
  - `US · Mountain View · Google`
  - `JP · Tokyo`
  - `未知`
- CSS 限制地区标签最大宽度，超出省略，完整信息放 hover。
- 静态资源版本更新为：
  - `20260606-geoip-compact-1`

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 52 passed
python -m compileall -q main.py app tests -> passed
```

## Cloudflare 204 主测速与自动回退

用户提供新测速命令：

```text
curl -o /dev/null -s -w "... timing ..." http://cp.cloudflare.com/generate_204
```

验证：

- 当前机器直连 `http://cp.cloudflare.com/generate_204`：
  - HTTP 204
  - total 约 0.44s
- 当前机器直连旧 `https://www.gstatic.com/generate_204`：
  - HTTP 000
  - 该环境下不如 Cloudflare 稳定

处理：

- 新增主测速 URL：
  - `http://cp.cloudflare.com/generate_204`
- 节点测速默认顺序改为：
  - Cloudflare 204
  - gstatic 204
  - Google 204
- 默认端口验证 URL 把 Cloudflare 204 放到第一位。
- 如果主测速 URL 失败：
  - 自动尝试备用 URL。
  - 控制台输出 `[测速] primary test URL failed; switched to ...`。
  - 返回结果记录 fallback notice，便于排查。
- 自定义 URL 不使用 fallback，保持用户指定目标的精确验证语义。

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 54 passed
python -m compileall -q main.py app tests -> passed
```

## 节点测速目标选择与多目标综合排序

用户反馈：

```text
测速界面有好几个测速按钮是否都替换成新的测速逻辑？
保留几个测速链接，测速界面用户可以自己选择测速链接作为本次测速。
不选择默认 http://cp.cloudflare.com/generate_204。
如果用户选择多种测速，需要统计不同的延迟，综合排序。
```

处理：

- 测试页新增“测速链接”多选框：
  - Cloudflare 204：`http://cp.cloudflare.com/generate_204`
  - gstatic 204：`https://www.gstatic.com/generate_204`
  - Google 204：`https://www.google.com/generate_204`
- 自定义测速 URL 仍保留，可以和内置目标一起参与本次测速。
- 不选择任何测速链接、也不填写自定义 URL 时：
  - 默认使用 Cloudflare 204。
  - 如果失败，自动 fallback 到 gstatic / Google。
- 选择一个或多个测速链接时：
  - 严格按用户选择的目标测试。
  - 多目标会保存每个目标的状态、HTTP 状态码和延迟。
  - 节点表显示 `成功 x/y · 平均 nms`。
- 综合排序规则：
  - 可用节点排前。
  - 多目标成功数量越多越靠前。
  - 成功数量相同，平均延迟越低越靠前。
  - 失败节点排后。

验证：

```text
node --check static/app.js -> passed
python -m pytest -q -> 56 passed
python -m compileall -q main.py app tests -> passed
```

## 测试页 UX 打磨

用户反馈：

```text
测试界面还是需要打磨优化，nodeTestTargets 这部分显示不行。
前面找个地方显示多少节点可用、不可用的总览信息，不然用户要向下翻。
```

处理：

- 测试页顶部新增结果总览：
  - 总节点
  - 可用
  - 失败
  - 未测
  - 测速中
  - 平均延迟
  - 本次测速目标
- 原生多选框改为更紧凑的目标 chip：
  - Cloudflare
  - gstatic
  - Google
- 操作按钮拆到独立按钮区，减少和输入控件混在一起的视觉噪音。
- 自定义测速 URL 保留，目标 chip 和自定义 URL 会实时更新“本次目标”总览。
- 手机端：
  - 汇总区自动两列排列。
  - 目标 chip 和按钮自动换行。
  - 避免横向挤出屏幕。
- 静态资源版本更新为：
  - `20260606-test-ux-1`

验证：

```text
GET / -> contains 20260606-test-ux-1, nodeTestOverview, node-target-check
node --check static/app.js -> passed
python -m pytest -q -> 56 passed
python -m compileall -q main.py app tests -> passed
```

## 测试页出口与地区查询开关

用户反馈：

```text
地区很多时候没有正确显示，兼顾速度和完整信息。
```

处理：

- 测试页新增默认开启的 `出口/地区` 开关。
- 普通节点测速：
  - 开启时：测速成功后同时查询出口 IP，并触发 GeoIP 地区查询。
  - 关闭时：只测目标 URL 延迟，速度更快。
- `测速并按 IP 去重` 仍然强制查询出口 IP，保证去重逻辑可靠。
- 顶部总览新增“地区模式”：
  - `查出口/地区`
  - `只测速`
- API 新增 `include_geoip` 参数，兼容旧 `prune_same_ip` 逻辑。
- 静态资源版本更新为：
  - `20260606-test-geo-toggle-1`

验证：

```text
GET / -> contains 20260606-test-geo-toggle-1, nodeTestGeo, 出口/地区
node --check static/app.js -> passed
python -m pytest -q -> 57 passed
python -m compileall -q main.py app tests -> passed
```

## 获取最快可用代理 API

用户需求：

```text
添加一个 api，访问可以获得一个最快可用的代理
```

处理：

- 新增接口：
  - `GET /api/proxy/fastest`
- 默认只在引擎运行且映射端口 ready 时返回代理，避免返回实际不可连接的端口。
- 可选参数：
  - `scheme=http|socks5`，默认 `http`
  - `require_running=true|false`，默认 `true`
- 选择规则：
  - 只从已分配端口里选择。
  - 只选择最近测速可用的节点。
  - 多目标测速时，目标成功数量多的优先。
  - 成功数量相同，延迟低的优先。
  - 延迟相同，端口小的优先。
- 返回内容包括：
  - `proxy`
  - `http_proxy`
  - `socks5_proxy`
  - `host`
  - `port`
  - `node_tag`
  - `node_name`
  - `delay`
  - `exit_ip`
  - `geoip`
  - `latency`

示例：

```text
GET /api/proxy/fastest
GET /api/proxy/fastest?scheme=socks5
GET /api/proxy/fastest?require_running=false
```

验证：

```text
python -m pytest -q -> 59 passed
python -m compileall -q main.py app tests -> passed
```

## 最快代理 API 状态同步修复

问题：

```text
api 能正常运行但是无法拉取最新的引擎和最新节点信息
```

原因：

- 服务启动时只加载一次 `config/assignments.json` 到内存。
- 如果节点、映射、测速缓存被其他流程或重启后的新实例写入文件，当前进程里的 `/api/nodes`、`/api/ports`、`/api/proxy/fastest` 仍可能读取旧快照。
- `/api/proxy/fastest` 之前只返回代理和简要节点字段，外部调用方无法直接看到本次选择时的引擎状态和完整节点摘要。

处理：

- 增加状态文件热同步：
  - `GET /api/status`
  - `GET /api/nodes`
  - `GET /api/ports`
  - `GET /api/proxy/fastest`
- 读取接口执行前检查 `assignments.json` 修改时间；如果文件比内存新，就自动重载到当前进程。
- `/api/proxy/fastest` 响应新增：
  - `node`：完整节点摘要，不包含 sing-box outbound 原始配置。
  - `engine`：当前引擎状态。
  - `state_updated_at`：状态文件更新时间。
  - `state_refreshed`：本次请求是否触发了状态文件重载。

验证：

```text
python -m pytest tests/test_api.py -q -> 28 passed
python -m compileall -q main.py app tests -> passed
```

## 分配界面端口排序

问题：

```text
分配界面也需要自动按端口大小排序
```

处理：

- 分配表渲染时按端口数值升序排序。
- 已分配端口的节点排在前面，未分配节点排在后面并保持原节点顺序。
- 自动分配新端口后立即重新渲染分配表，让新增端口顺序马上生效。
- 前端收集映射时按端口升序提交。
- 后端 `/api/assign` 保存和返回映射时也按端口升序，保证 API、状态文件和界面顺序一致。

验证：

```text
node --check static/app.js -> passed
python -m pytest tests/test_api.py -q -> 29 passed
python -m compileall -q main.py app tests -> passed
```

## ProxyAdmin 管理与本地检测模块边界整理

问题：

```text
ProxyAdmin 和本地 proxycheck-api 都有检测能力，如果混在一个流程里，后续会很难删除或替换其中任意一块。
```

处理：

- 新增 `app/proxy_check.py`：
  - 调用本地 `proxycheck-api/proxycheck.exe` 或 Linux 下的 `proxycheck-api/proxycheck`。
  - 支持通过 `PROXYCHECK_BIN` 指定自定义二进制路径。
  - Linux 部署时如果没有无扩展名二进制，需要在 `proxycheck-api` 目录执行 `go build -o proxycheck ./cmd/proxycheck`。
  - 将本地检测结果规范化为前端已有的 `score`、`grade`、`exit_ip`、`country`、`items` 结构。
- `app/proxy_admin.py` 保留 ProxyAdmin 远端职责：
  - 拉取远端代理列表。
  - 创建代理。
  - 修正远端名称。
  - 删除代理。
  - 调用远端 `/quality-check`。
- `/api/proxy-admin/check/start` 的流程改为：
  - 先上传代理到 ProxyAdmin。
  - 上传成功后，使用 ProxyAdmin 远端 `/quality-check` 检测。
- `重试失败` 改为 `check_only` 模式：
  - 前端传入失败端口和已有 `port -> ProxyAdmin id` 映射。
  - 后端跳过远端上传，只对已有远端 id 重跑 ProxyAdmin 远端质量检测。
  - 避免失败重试时在 ProxyAdmin 里重复创建代理。
- 本地 `proxycheck-api` 检测模块暂时独立保留，后续应接到单独的本地检测区域，不混入 `proxy-admin-box`。
- 运行页 section-head 新增本地检测入口：
  - 端口下拉框自动使用当前映射端口。
  - `检测` 按钮调用 `POST /api/proxy-check`。
  - 后端通过本地 `proxycheck-api` 检测 `http://127.0.0.1:{port}/`。
  - 结果在 section-head 内紧凑展示 `grade / score / exit_ip / country` 和 `gpt / claude / gemini` 状态。
  - 检测前先检查 `127.0.0.1:{port}` 是否监听；未监听直接返回 `ERR` 和明确原因，避免显示误导性的 `B / 78`。

本地验证：

```text
proxycheck-api\proxycheck.exe -proxy http://127.0.0.1:8001/ -json -timeout 30
-> score 100, grade A, exit_ip 108.181.23.255, country 美国/US

python adapter check_proxy_quality("http://127.0.0.1:8001/", 8001, 30)
-> 8001 A 100 108.181.23.255 美国
```

验证：

```text
python -m pytest -q -> 72 passed
python -m compileall -q main.py app tests -> passed
node --check static/app.js -> passed
proxycheck-api\proxycheck.exe -h -> passed
```

## ProxyAdmin 容错、最快代理实时验证、状态展示与安装整理

处理内容：

- ProxyAdmin 调用拆到 `app/proxy_admin.py`，减少 `app/api.py` 里直接堆叠外部 API 细节。
- ProxyAdmin 单个代理上传失败时：
  - 失败端口会产生对应错误结果。
  - 其他端口继续上传和质量检测。
  - 任务最终仍可完成，不会因为一个端口失败中断整批。
- 运行界面处理无效 ProxyAdmin ID：
  - 上传失败的本地错误项不会再被拿去远端重试或删除。
- `/api/proxy/fastest` 增加实时验证参数：
  - `check=true`
  - `target_url=https://example.com/ping`
  - 开启后会按缓存排序逐个验证候选端口，返回第一个实时可用代理。
- 顶部状态区新增 `监听`：
  - 显示 `实际监听端口数/期望映射端口数`。
  - 如果有缺失端口，鼠标悬停可看到异常端口列表。
- Linux/systemd 安装整理：
  - `install-systemd.sh` 不再硬编码 `User=dsk`。
  - 默认使用 `sudo` 发起用户，也可通过 `SERVICE_USER=xxx` 指定。
  - systemd 环境变量补齐 `PPM_CLASH_API_ADDR` 和 `PPM_DOMAIN_RESOLVE_STRATEGY`。
  - `install-linux.sh` 默认配置补齐 `domain_resolve_strategy`。

示例：

```text
GET /api/proxy/fastest?check=true&target_url=https://www.google.com/generate_204
```

验证：

```text
node --check static/app.js -> passed
python -m pytest tests/test_api.py -q -> 31 passed
python -m compileall -q main.py app tests -> passed
```

## ProxyAdmin 命名上传优化

问题：

```text
批量上传有个问题，上传无法命名，导致上传后 name 都叫 default。
```

原因：

- 原实现参考 `proxy-port-api` 使用 `POST /api/v1/admin/proxies/batch`。
- batch 接口适合一次上传多个代理，但无法传每个代理的 `name`。
- 上传后还需要再拉列表反查 `host:port -> id`，链路更绕。

处理：

- ProxyAdmin 导入改为逐个调用：
  - `POST /api/v1/admin/proxies`
- 每个代理创建时传入：
  - `name`
  - `protocol`
  - `host`
  - `port`
  - `username`
  - `password`
- `name` 默认使用顺序名称：
  - `代理1`
  - `代理2`
  - `代理3`
- 运行界面新增 `名称前缀` 配置，可自定义为：
  - `测试1`
  - `测试2`
  - `测试3`
- 创建接口直接返回 `data.id`，后续质量检测直接用这个 ID，不再依赖列表反查。
- 逐个上传仍保留并发控制，避免大量端口时速度太慢。

验证：

```text
node --check static/app.js -> passed
python -m pytest tests/test_api.py -q -> 29 passed
python -m compileall -q main.py app tests -> passed
```
