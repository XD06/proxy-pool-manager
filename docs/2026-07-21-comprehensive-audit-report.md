# Proxy Pool Manager 综合审计报告

- **日期**：2026-07-21
- **审查范围**：全仓库（Python `app/`、Go `proxycheck-api/`、前端 `static/`、配置与部署文件）
- **审查方式**：静态代码审查 + 官方 sing-box 文档核实 + **使用项目自带 `bin/sing-box.exe` v1.13.14 实测 `check` 验证**
- **约束**：只读审查；本报告涉及的验证均在 `tmp/` 临时目录进行且已清理，未改动任何项目代码/配置

---

## 0. 执行摘要

本次审计分两轮：第一轮为整体架构与安全/性能审查；第二轮聚焦**协议字段支持**与**不同导入方式导致节点不可用/测速失败**，并用 sing-box 二进制做了实测验证。

| 级别 | 数量 | 代表问题 |
|------|------|----------|
| 🔴 严重 | 4 | ①Reality `spider_x` 未知字段致 `check` 失败、一个坏节点阻断整个引擎启动（已实测）；②管理台无鉴权且监听 `0.0.0.0`；③代理端口开放无认证（开放代理）；④配置中含真实 JWT 令牌 |
| 🟠 中危 | 8 | TUIC `heartbeat` 裸数字致失败（已实测）；TUIC `udp_relay_mode`+`udp_over_stream` 冲突（已实测）；Clash-Reality 未设 `tls` 时静默丢弃 reality；subprocess 管道死锁隐患；任何映射改动全量断连；端口冲突校验缺口；同步测速无并发锁；会话永不过期 |
| 🟡 低危 | 9 | VMess `tls` 仅认 `"tls"` 字符串；跨导入去重可能覆盖变体；`server_ports` 单端口格式；端口探测不精确/TOCTOU；热路径端口探测开销；加权轮询每连接分配；多 worker 不安全；订阅 SSRF；防抖保存窗口 |

**最重要结论**：解析器为 VLESS-Reality 注入了 sing-box **根本不存在的 `spider_x` 字段**，而 sing-box 对未知字段是**硬失败**。由于 VLESS-Reality 分享链接普遍带 `spx`（spiderX）参数，**大量 Reality 节点在测速时会被判为“配置不兼容”，被分配到端口后更会导致整个 sing-box 引擎无法启动**——这正是“不同导入方式导致节点无法使用和测速”的根因。

---

## 第一部分 · 协议字段支持与导入方式问题（本轮重点）

### 验证方法与证据

用项目自带二进制对最小化配置执行 `sing-box check`（v1.13.14），得到权威结论：

| 测试 | 配置要点 | 结果 | sing-box 报错 |
|------|----------|------|---------------|
| A | VLESS Reality 含 `spider_x` | ❌ exit=1 | `outbounds[0].tls.reality.spider_x: json: unknown field "spider_x"` |
| B | 同 A 但去掉 `spider_x`（对照） | ✅ exit=0 | — |
| C | Hysteria2 同时含 `server_port`+`server_ports` | ✅ exit=0 | —（`server_port` 被忽略，非冲突） |
| D | TUIC `heartbeat:"10"`（裸数字） | ❌ exit=1 | `outbounds[0].heartbeat: time: missing unit in duration "10"` |
| E | TUIC 同时 `udp_relay_mode`+`udp_over_stream` | ❌ exit=1 | `initialize outbound[0]: udp_over_stream is conflict with udp_relay_mode` |
| F | 好节点 + 含 `spider_x` 的坏节点（同一份配置） | ❌ exit=1 | 整份配置解析失败，**好节点也无法运行** |

> 关键结论：sing-box 使用严格 JSON 解码，**任何未知字段/非法值/字段冲突都会让整份配置 `check` 失败**。项目在测速链路里用了二分预检（`preflight_nodes`）来隔离坏节点，但**正式启动链路（`generate_config`→`engine.start(check=True)`）没有隔离**，因此一个坏节点会拖垮整个引擎（测试 F 已证实）。

---

### P1 🔴 Reality 注入了不存在的 `spider_x` 字段（已实测）

**位置**：
- 链接/Base64 路径：`app/parser.py` `_tls_from_query()` L115-117
  ```python
  spider_x = params.get("spx") or params.get("spiderX")
  if spider_x:
      tls["reality"]["spider_x"] = unquote(spider_x)   # sing-box 无此字段
  ```
- Clash 路径：`app/parser.py` `_clash_reality()` L473-475
  ```python
  spider_x = reality_opts.get("spider-x") or reality_opts.get("spider_x")
  if spider_x:
      reality["spider_x"] = str(spider_x)              # 同样注入非法字段
  ```

**官方 schema**（sing-box TLS 文档 Outbound Reality）：仅 `enabled` / `public_key` / `short_id`（server 端另有 `handshake`/`private_key`/`max_time_difference`）。**没有 `spider_x`**——它是 Xray/v2ray 的 URL 参数，被原样搬进了 sing-box 配置。

**影响**：
1. 测速：VLESS-Reality（带 `spx`）节点在 `preflight_nodes` 中被判 `configuration incompatible`，界面显示失败/不可用。
2. 运行：一旦这类节点被分配到端口，`generate_config` 会把它写进 `config/sing-box.json`，`engine.start()` 默认 `check=True`（`app/engine.py` L468-469）→ **整份配置 `check` 失败 → 引擎完全无法启动**，其它正常节点一并瘫痪（测试 F 实测）。
3. 三种导入方式（链接 / Base64 订阅 / Clash YAML）**只要来源带 spiderX 就都中招**，普遍存在。

**修复方向**：解析时直接丢弃 `spx`/`spiderX`（sing-box 不需要）；或在 `generate_config`/`store` 迁移里剥离 `tls.reality.spider_x`。

---

### P2 🟠 TUIC `heartbeat` 未规范化为时长格式（已实测）

**位置**：`app/parser.py`
- 链接路径 `parse_tuic()` L299-301：`outbound["heartbeat"] = heartbeat`（原样，未过 `_duration()`）
- Clash 路径 L602-604：`outbound["heartbeat"] = str(heartbeat)`（原样）

**问题**：sing-box `heartbeat` 需 Go duration 格式（如 `"10s"`）。若分享链接写 `heartbeat=10`（裸秒数，常见），生成 `"heartbeat":"10"` → `check` 报 `missing unit in duration "10"`（测试 D 实测），该 TUIC 节点不可用/测速失败。

**对比**：AnyTLS 的 `idle_session_*` 字段正确地用了 `_duration()`（L344、L635）做规范化——TUIC heartbeat 漏掉了同样处理。

**修复方向**：`outbound["heartbeat"] = _duration(heartbeat)`。

---

### P3 🟠 TUIC `udp_relay_mode` 与 `udp_over_stream` 互斥未校验（已实测）

**位置**：`app/parser.py` `parse_tuic()` L292-296；Clash 路径 L597-601。

**问题**：两者在 sing-box 中互斥；若来源同时提供（或 `udp_over_stream` 为真且又带 `udp_relay_mode`），解析器会同时写入 → `check` 报 `udp_over_stream is conflict with udp_relay_mode`（测试 E 实测），节点不可用。

**修复方向**：设置 `udp_over_stream=true` 时不再写 `udp_relay_mode`（二选一）。

---

### P4 🟠 Clash-Reality 在未显式 `tls:true` 时静默丢弃 reality（跨导入差异）

**位置**：`app/parser.py` `_clash_tls()` L446-457 与 VLESS/VMess 分支 L507-512。

**问题**：`_clash_tls` 仅在 `proxy.get("tls")` 或 `servername`/`sni` 存在时才返回 TLS 块；`_clash_reality` 只有在 `_clash_tls` 返回非空时才被挂上（L509-511）。若某 Clash 节点用 `reality-opts` 但**未显式写 `tls: true`**（部分客户端如此），则 TLS 与 reality 一起被丢弃 → 变成“裸 VLESS 无 TLS/Reality”。

**跨导入差异**：同一服务器，用**链接**导入（`security=reality` 直接触发 TLS+reality）能连；用 **Clash** 导入（缺 `tls:true`）却生成错误出站 → 握手失败/测速失败。这正是“不同导入方式结果不同”的典型例子。

**修复方向**：`_clash_tls` 中把 `reality-opts` 存在也视为启用 TLS。

---

### P5 🟡 VMess 链接 TLS 仅识别 `"tls"` 字符串（跨导入差异）

**位置**：`app/parser.py` `parse_vmess()` L365-371：`if data.get("tls") == "tls":`。

**问题**：VMess 分享 JSON 的 `tls` 字段有多种写法（`"tls"`/`"1"`/`true`/`"true"`）。仅当等于字符串 `"tls"` 才启用 TLS；其它写法下面向 TLS 服务器的 VMess 节点不会启用 TLS → 连接/测速失败。Clash 路径用 `_clash_tls`（按真值判断）更宽松 → **同节点两种导入结果可能不同**。

---

### P6 🟡 `server_ports` 单端口格式与端口跳跃

**位置**：`app/parser.py` Hysteria2：链接 L231-233、Clash L559-561：`str(ports).replace("-", ":")`。

**说明**：范围（`20000-50000`→`20000:50000`）正确；但若来源给单端口（`mport=443`→`"443"`）则不是合法的 `START:END`，可能 `check` 失败。同时含 `server_port`+`server_ports` 本身**不冲突**（测试 C 已证实 `server_port` 被忽略），故此项仅限“单端口写法”边缘场景。

---

### P7 🟡 节点去重键过窄，跨导入可能覆盖变体

**位置**：`app/parser.py` `_node_tag()` L53-55（键=`server`+`server_port`+`credential`）；`api.py` `api_import` L1263-1266（跨导入按 tag 覆盖）。

**说明**：tag 不含传输/TLS/端口跳跃信息。同一 `server:port:uuid` 的两个不同变体（如 ws 版与 grpc 版）会同 tag。**单次导入**内会加指纹后缀共存（`parse_text` L751-762）；但**跨导入**（订阅刷新 vs 粘贴）按 tag 直接覆盖，可能用坏变体覆盖掉可用变体。边缘但真实。

---

### 协议字段支持结论

- **可用且字段映射正确**：VLESS / VMess / Trojan / Shadowsocks / AnyTLS / 传输层（ws/grpc/http/httpupgrade）——已逐字段对照官方 schema，未见非法字段。
- **存在字段级缺陷**：Reality（`spider_x` 致命，P1）、TUIC（`heartbeat` 格式 P2、udp 模式冲突 P3）。
- **跨导入方式差异**：Reality（P1 三方式皆中招 / P4 Clash 特有丢弃）、VMess TLS（P5）、去重覆盖（P7）。
- **未支持（已在 README 说明）**：SSR、多订阅合并为一、TUN、节点池 UDP。
- 无协议字段级的校验/清洗层：非法值/未知字段直到 `sing-box check` 才暴露；正式启动链路缺少像测速那样的坏节点隔离。

---

## 第二部分 · 安全问题

### S1 🔴 管理控制台无鉴权且监听 0.0.0.0
- `config/app.json` L2-4：`host=0.0.0.0`、`proxy_listen_host=0.0.0.0`，且**无 `admin_key`**。
- `app/api.py` `auth_enabled()` L211-212：`admin_key` 为空即整体关闭鉴权；中间件 `require_auth` L227-237 随即放行。
- **后果**：局域网/公网任何人无需密码即可完全控制管理台（导入、改映射、启停、doctor、读出口 IP/流量）。README 明确要求暴露时必须设 `admin_key`，而当前实际配置未设。

### S2 🔴 代理端口是无认证的开放代理
- `app/generator.py` L112-119 生成的 `mixed` inbound **无 `users`**（无账号密码）；Go 边缘路由 `main.go handle()` 为纯 L4 转发、无认证。
- 配合 `proxy_listen_host=0.0.0.0`（Dockerfile L19 亦默认），任何能连到端口者都可白嫖出口 → IP 封禁、滥用与法律风险。产品层无代理级鉴权。

### S3 🔴 配置文件含真实 ProxyAdmin JWT 令牌
- `config/app.json` L10：明文保存 `role:admin` 的 Bearer JWT。虽 `.gitignore` L9 忽略 `config/*.json`（大概率未进 git），但明文躺在工作区，打包/备份即泄露。**建议按已泄露处理并轮换**。

### S4 🟠 会话永不过期、限流键取错
- `app/routes/auth.py`：`sessions` 集合无 TTL（L25-36、L77-78），令牌签发后直到进程重启一直有效；`failures` 只增不清。
- 限流用 `request.client.host`（L57），反代（Nginx/Caddy）后全部变成反代 IP → 5 次锁定变全局锁定，且未读 `X-Forwarded-For`。

### S5 🟡 订阅导入 SSRF
- `app/parser.py` `import_nodes()` L767-780：服务端拉取订阅 URL 且跟随重定向。配合 S1 无鉴权时，可被用于探测/触达内网服务。

**安全正面**：登录用 `hmac.compare_digest`+限流锁定（`routes/auth.py` L67-88）；前端全程 `escapeHtml`（`static/helpers.js` L12-20）**XSS 已缓解**；subprocess 全用 exec 非 shell、PowerShell 路径有引号转义 → **无命令注入**。

---

## 第三部分 · 可靠性 / 性能 / 并发

### R1 🟠 sing-box 子进程用 PIPE 但运行期从不读取 → 管道死锁隐患
- `app/engine.py` `start()` L474-495：`stdout=PIPE, stderr=PIPE`，仅在“启动即退出”时读一次；正常运行期无人排空管道。若 sing-box 大量输出（panic/goroutine dump）填满 ~64KB 管道缓冲 → 进程阻塞写、无法服务 → **冻结**。
- **对比**：`app/pool_router.py` L142-147 正确使用 `DEVNULL`。同项目两处不一致，建议统一为 `DEVNULL`。

### R2 🟠 任何映射/池变更都全量重启，断开所有端口连接
- `app/api.py` `restart_runtime()` L620-628、`restart_for_pool_change()` L1884-1889；Go 路由无热加载（`main.go` L110-130 配置改动需整体重启）。
- **后果**：改一条映射/一个池成员，会掐断**所有端口上所有用户**的活动连接，与“稳定出口”目标相悖。

### R3 🟠 端口冲突校验缺口（保留端口未拦截）
- `app/api.py` `assign` L1777-1793 仅校验 `1024–65535`+节点存在；`validate_pool_request` L1870-1873 仅查已占用映射/池端口。
- **未排除**：Web 端口（9000）、Clash API（`app.json` 为 10000）、pool-router 控制端口（9091）。映射到这些端口能保存成功，但启动时因绑定冲突抛错（400）——“能存进去、一启动就报错”。`ports/check` 里其实已有 `reserved-clash-api` 判断（L734-741），只是没接到保存校验。

### R4 🟠 同步测速接口无并发锁，与异步测速共享同一配置路径
- `app/api.py` `/api/test` L1447-1462 无 `active_test_job`/锁保护（而 `/api/test/start` 有）；两者共用 `SING_BOX_TEST_CONFIG_PATH`，`tester.py` L371-372 起始 `cleanup_engine.stop()` 会按配置路径杀掉另一个测速的临时 sing-box → 并发时互相打断、结果错乱。

### R5 🟠 代理连接无空闲/读超时
- Go `main.go handle()` L281-294：建连后裸 `io.Copy` 双向拷贝，无 idle timeout/SetDeadline。半死后端会让 goroutine 与 `Active` 计数无限期挂起。dial 有 3s 超时，但连上后无兜底。

### R6 🟡 其它
- **端口可用性检测不精确/TOCTOU**：`engine.py` `_can_bind_tcp_port` L610-616 固定绑 `0.0.0.0`，而 sing-box 常只监听 `127.0.0.1`；“检测→关闭→启动”间有竞态。
- **热路径端口探测开销**：`engine_status_payload` L678-682 在“运行但未走 Go 路由”（典型 Windows 本地）时，每次 `/api/status` 都对全部端口线程池 TCP 探测；端口多且高频轮询时偏重（router 模式用集合求交则很轻）。
- **加权轮询每连接分配**：`main.go pickFromSchedule` L211-231 每连接按权重（≤100）展开切片，高权重+高并发下有 GC 压力。
- **多 worker 不安全**：状态/Job/会话/进程句柄均在进程内；默认单 worker（`main.py` L13）无碍，加 `--workers` 会出错，建议文档标注。
- **重启后流量少计**：`traffic.py sample` L45-61 用累计计数器算增量，router 每次重启计数器归零，重启后首个采样窗口（~15s）流量被 `max(0,…)` 吞掉；量级小。
- **防抖保存窄窗口**：`api.py _run_debounced_save` L253-274 极端时序下一次改动可能延后到下次改动/关机才落盘（关机 `flush_save` 兜底，基本无永久丢失）。

---

## 第四部分 · 端口分层与冲突总览

| 用途 | 监听 | 说明 |
|------|------|------|
| Web 管理台 | `host`:9000 | 当前为 0.0.0.0（见 S1） |
| Clash API | 127.0.0.1:10000 | `_config_listen_ports` 会纳入可用性检查 |
| pool-router 控制口 | 127.0.0.1:9091 | — |
| sing-box 入站 | router 模式 127.0.0.1:18000+（内部）；直连模式 `proxy_listen_host`:8001+ | 见 `sing-box.json` |
| pool-router 公网监听 | `proxy_listen_host`:8001+ | 对外入口 |
| 测速临时引擎 | 127.0.0.1:19001+（动态查空） | — |
| 预检临时引擎 | 30000+（临时目录） | — |

- 公网端口重复、端口范围、池空成员均有校验（`generator.py` L21-51）；池内部端口 18000 起自动避开已用端口。
- 主要缺口在 **R3（保留端口未拦截）**；分层设计合理，无致命静默冲突。

---

## 第五部分 · 值得肯定

1. 数据面/控制面分离，Python 不在代理路径上，架构正确。
2. 状态原子落盘（tmp+replace）+ 损坏自动备份（`store.py` L110-145）。
3. sing-box 更新有 check→备份→替换→失败回退完整事务（`engine.py` L347-453），下载后校验版本。
4. 测速侧坏节点二分隔离（`tester.py preflight` L319-356）、同出口 IP 去重、批处理并发+超时兜底。
5. 前端 XSS 转义到位；`proxycheck.go` 限制响应体与超时。
6. 常见协议（除 Reality/TUIC 的字段缺陷外）字段映射正确、覆盖完整。

---

## 第六部分 · 修复优先级建议

| 优先级 | 动作 | 关联 |
|--------|------|------|
| **P0** | 解析/生成阶段剥离 `tls.reality.spider_x`；正式启动链路也做坏节点隔离（或至少给出“哪个节点导致 check 失败”） | P1 |
| **P0** | 给 `config/app.json` 设强 `admin_key`（或 `PPM_ADMIN_KEY`），非必要把 `host` 改回 `127.0.0.1`；轮换 S3 的 JWT | S1/S3 |
| **P0** | 明确“代理端口=开放代理”，仅在防火墙放开必要端口段；评估 Go 路由加基础认证/IP 白名单 | S2 |
| **P1** | TUIC `heartbeat` 过 `_duration()` 规范化；`udp_over_stream` 与 `udp_relay_mode` 二选一 | P2/P3 |
| **P1** | `_clash_tls` 把 `reality-opts` 视为启用 TLS；VMess `tls` 按真值判断 | P4/P5 |
| **P1** | 引擎 subprocess 改 `DEVNULL`；`assign`/池保存增加保留端口校验；同步 `/api/test` 补并发锁 | R1/R3/R4 |
| **P2** | Go 路由加连接空闲超时；会话加 TTL、限流读 `X-Forwarded-For` | R5/S4 |
| **P3** | 评估热加载以避免每次改动全量断连；去重键纳入传输/TLS 指纹 | R2/P7 |

---

## 附录 · 验证环境与方法

- 二进制：`bin/sing-box.exe`，`sing-box version 1.13.14`（go1.26.4，windows/amd64）。
- 方法：构造最小化 sing-box 配置置于 `tmp/`，执行 `sing-box check -c <file>`，比对退出码与报错；对照组仅差异目标字段以隔离变量。
- 覆盖：Reality `spider_x`（A/B）、Hysteria2 双端口（C）、TUIC heartbeat 裸数字（D）、TUIC udp 模式冲突（E）、坏节点污染整份配置（F）。
- 所有临时配置在验证后已删除，未改动任何项目代码或配置。

> 总体评价：工程完成度与代码质量在同类工具中偏上。**最紧急的是 P1（Reality `spider_x` 导致大量节点不可用并阻断引擎启动）与 S1/S2（无鉴权 + 开放代理）**；其余为字段健壮性与可靠性/性能改进项。
