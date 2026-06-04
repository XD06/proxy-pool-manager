# Proxy Pool Manager — 设计文档

**日期：** 2026-06-04
**状态：** Draft v2

---

## 1. 核心思想

**一句话：把多个代理节点分别绑定到不同本地端口，每个端口固定对应一个节点。**

```
你有 30 个代理节点（来自订阅），
你有 10 个账户要访问同一个 API，
每个账户需要不同的出口 IP。

→ 账户1 → 127.0.0.1:8001 → 节点A
→ 账户2 → 127.0.0.1:8002 → 节点B
→ ...
→ 账户10 → 127.0.0.1:8010 → 节点J
```

**关键设计约束：**
- 每个端口 = **固定绑定**一个节点（不是轮换、不是负载均衡）
- 端口映射关系 **持久化**，重启后不变
- Python 管理层 **不参与数据路径**，流量零损耗
- 支持 **导入订阅**（自动解析所有节点）和 **单节点链接** 混合使用

---

## 2. 用户完整流程

```
1. 启动服务 (python main.py)
       │
2. 打开 Web 界面 http://127.0.0.1:9000
       │
3. 导入节点
   ├─ 贴订阅链接 → 自动解析出 N 个节点
   ├─ 或贴单节点链接 (vless://vmess://ss://trojan://...)
   └─ 或直接粘贴 Clash 配置文本
       │
4. 自动测速
   ├─ 每个节点测试延迟 → gstatic.com/generate_204
   └─ 显示：节点名 | 类型 | ✅/❌ | 延迟ms (绿/黄/红)
       │
5. 选择节点 → 绑定端口
   ├─ 从列表勾选需要的节点
   ├─ 系统自动分配 8001~8010 端口
   └─ 确认映射关系
       │
6. 启动引擎
   ├─ 生成 sing-box 配置文件
   ├─ 启动 sing-box 进程
   └─ 开始提供服务
       │
7. 使用
   ├─ 各账户请求 → http://127.0.0.1:8001~8010
   ├─ 每个端口出口 IP 不同
   └─ 随时在 Web UI 查看状态
```

---

## 3. 工具目标

### 3.1 功能目标

| 编号 | 目标 | 说明 |
|------|------|------|
| F1 | 多格式导入 | 支持 Clash 订阅、Base64 订阅、单节点链接（vless/vmess/ss/trojan/hysteria2/tuic） |
| F2 | 节点测速 | 导入后自动检测每个节点的可用性和延迟 |
| F3 | 端口绑定 | 每个选中节点绑定到一个独立的本地 SOCKS5 端口 |
| F4 | 持久化 | 节点列表和端口映射关系保存到文件，下次启动自动恢复 |
| F5 | 状态查询 | 提供 REST API 查询端口→节点映射和出口 IP |
| F6 | Web UI | 可视化管理，颜色标记状态信息 |
| F7 | 跨平台 | Windows + Linux 均可运行 |

### 3.2 性能目标

| 指标 | 目标 | 如何保证 |
|------|------|---------|
| 延迟增加 | < 5ms | Python 不参与数据路径，全部由 sing-box (Go) 原生转发 |
| 内存占用 | < 50MB | sing-box 单二进制 ~15MB，Python 管理层常驻 ~30MB |
| CPU 占用 | < 5% 日常 | 管理层仅处理 API 查询，绝大部分 CPU 由 sing-box 消耗 |
| 连接并发 | 无上限 | sing-box 原生 epoll/kqueue/IOCP 异步 I/O |

### 3.3 非目标

- 不做系统级代理（不走 TUN 模式）
- 不做负载均衡（每个端口固定一个节点，不轮换）
- 不做流量记录/统计
- 不做多订阅合并（一次只处理一个订阅）

---

## 4. 整体架构

```
┌─────────────────────────────────────────────────┐
│                 用户应用层                        │
│                                                   │
│  账户1 → socks5://127.0.0.1:8001                 │
│  账户2 → socks5://127.0.0.1:8002                 │
│  ...                                             │
│  账户N → socks5://127.0.0.1:{8000+N}             │
└───────────────────────┬─────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────┐
│              sing-box 引擎（Go 原生）             │
│                                                   │
│  每个 inbound(socks) → route → 固定 outbound      │
│                                                   │
│  :8001 ──▶ node-1 (vless)                         │
│  :8002 ──▶ node-2 (ss)                            │
│  :8003 ──▶ node-3 (trojan)                        │
│  ...                                              │
│                                                   │
│  管理 API: http://127.0.0.1:9090                   │
└──────────────┬────────────────────────────────────┘
               │ HTTP API (sing-box 内置)
┌──────────────▼────────────────────────────────────┐
│          Python 管理层（FastAPI :9000）             │
│                                                   │
│  ┌──────────┐  ┌───────────┐  ┌───────────┐      │
│  │ parser   │→ │ generator │→ │ engine    │      │
│  │ 解析订阅  │  │ 生成配置   │  │ 管理进程   │      │
│  └──────────┘  └───────────┘  └───────────┘      │
│                                                   │
│  Web UI (静态 HTML+JS)  ←─→  REST API             │
│                                                   │
│  GET  /api/nodes             查节点列表+测速结果    │
│  GET  /api/ports             查端口映射            │
│  GET  /api/ports/{port}/ip   查某端口的出口IP      │
│  POST /api/import            导入订阅/链接         │
│  PUT  /api/assign            分配节点到端口        │
│  POST /api/start             启动引擎              │
│  POST /api/stop              停止引擎              │
│  GET  /api/status            引擎运行状态          │
└──────────────────────────────────────────────────┘
```

### 核心架构原则

**Python 不在数据路径上。** 所有代理流量直接走 sing-box，Python 只做三件事：

1. **启动前**：解析订阅 → 生成配置
2. **运行时**：接受 API 查询 → 从 sing-box API 获取状态 → 返回给用户
3. **异常时**：监控 sing-box 进程 → 崩溃时自动重启

这意味着即使 Python 层挂了，正在进行的代理连接不会中断（只是没法查状态了）。

---

## 5. 引擎层：sing-box

### 5.1 选择理由

| 对比项 | sing-box | mihomo (Clash Meta) |
|--------|----------|---------------------|
| 多 inbound→outbound 路由 | **一等公民设计**，route.rules 原生支持 | 需要 `listeners`，较新且不够稳定 |
| 配置格式 | JSON，程序生成友好 | YAML，格式复杂 |
| 二进制大小 | ~15MB | ~30MB |
| 协议支持 | SS/VMess/VLESS/Trojan/Hysteria2/TUIC/... | 同样全支持 |
| 性能 | 优 | 略优（但差异很小） |

**结论：** sing-box 更适合我们这个场景。多端口路由是它的原生能力，不是后加的 workaround。

### 5.2 配置结构（生成后）

生成的 sing-box 配置格式如下：

```jsonc
{
  // 日志
  "log": {
    "level": "warn",        // 只记录警告和错误
    "output": "sing-box.log",
    "disabled": false
  },

  // 入站：每个端口一个
  "inbounds": [
    {
      "type": "socks",
      "tag": "port-8001",
      "listen": "127.0.0.1",      // 只监听本地
      "listen_port": 8001,
      "sniff": false               // 不嗅探流量，降低开销
    },
    {
      "type": "socks",
      "tag": "port-8002",
      "listen": "127.0.0.1",
      "listen_port": 8002
    }
  ],

  // 出站：每个节点一个
  "outbounds": [
    {
      "tag": "node-1",
      "type": "vless",
      "server": "104.16.158.61",
      "server_port": 2053,
      "uuid": "0c3d2134-83c8-...",
      "tls": {
        "enabled": true,
        "server_name": "mv.203065.xyz",
        "utls": {
          "enabled": true,
          "fingerprint": "chrome"
        }
      },
      "transport": {
        "type": "ws",
        "path": "/proxyip=154.16.183.190",
        "headers": {
          "Host": "mv.203065.xyz"
        }
      }
    },
    {
      "tag": "node-2",
      "type": "shadowsocks",
      "server": "192.168.1.1",
      "server_port": 8388,
      "method": "aes-256-gcm",
      "password": "your-password"
    },
    {
      "tag": "node-3",
      "type": "trojan",
      "server": "trojan.example.com",
      "server_port": 443,
      "password": "your-trojan-password",
      "tls": {
        "enabled": true,
        "server_name": "trojan.example.com"
      }
    }
  ],

  // 路由：把每个端口绑定到对应的节点
  "route": {
    "rules": [
      {
        "inbound": ["port-8001"],
        "outbound": "node-1"
      },
      {
        "inbound": ["port-8002"],
        "outbound": "node-2"
      },
      {
        "inbound": ["port-8003"],
        "outbound": "node-3"
      }
    ],
    // 兜底：没有匹配规则的端口走 DIRECT
    "final": "direct",
    "auto_detect_interface": true
  }
}
```

### 5.3 生命周期管理

```
Python 调用 engine.start(config_path)
  │
  ├─ 如果 sing-box 二进制不存在 → 自动下载
  │
  ├─ 启动子进程: sing-box run -c config.json
  │
  ├─ 检查进程是否正常启动
  │   ├─ 成功 → 返回
  │   └─ 失败 → 重试 3 次，每次间隔 2 秒
  │
  └─ 启动守护协程
      ├─ 每 5 秒检查进程存活
      ├─ 如果崩溃 → 自动重启（最多 3 次）
      └─ 超过 3 次 → 标记为致命错误，通知用户

Python 调用 engine.stop()
  │
  ├─ 发送 SIGTERM（Linux）或 TerminateProcess（Windows）
  ├─ 等待 3 秒
  ├─ 如果未退出 → 强制 SIGKILL
  └─ 清理临时文件

配置变更时 (engine.reload)
  ├─ 重新生成 config.json
  ├─ Linux: 发送 SIGHUP → sing-box 热重载
  └─ Windows: stop() → start()（Windows 不支持 SIGHUP）
```

### 5.4 性能分析

**为什么这个架构快：**

```
典型代理工具的数据路径：
  应用 → [HTTP Client] → Python 转发 → 节点 → 目标网站
                          ↑ Python 处理每个字节，有 GIL 限制

本工具的数据路径：
  应用 → [sing-box in]:8001 → [sing-box out] → 目标网站
          ↑ Go 原生 epoll，零拷贝转发，没有中间层
```

- sing-box 内部用 `io.Copy`（Go）直接转发 TCP 流，不走用户态缓冲
- 每个 inbound 独立 goroutine，互不阻塞
- Python 层在**完全独立的进程中**，不影响流量

---

## 6. 解析器模块（parser.py）

### 6.1 输入格式

这是最复杂的模块，需要支持所有常见格式。输入可能是：

1. **订阅 URL**（HTTP 请求，返回各种格式）
2. **配置文本**（直接粘贴到 Web UI 中）

自动检测逻辑：

```
获取输入
  │
  ├─ 如果是 URL → 发起 HTTP GET 请求获取内容
  │
  ├─ 判断内容格式：
  │   ├─ 包含 "proxies:" → 当作 Clash YAML 解析
  │   ├─ 可被 Base64 解码（且解码后含 "://"）→ 按 Base64 订阅解析
  │   ├─ 包含 "://" 行 → 按行解析，每行一个链接
  │   └─ 都不是 → 报错
  │
  └─ 输出：节点 dict 列表
```

### 6.2 各格式详细解析规则

#### 6.2.1 Clash YAML 订阅

```
检测：内容包含 "proxies:" 关键字
解析：用 PyYAML 加载，遍历 proxies 列表
映射：

Clash YAML → sing-box outbound

type: ss       → type: shadowsocks
type: ssr      → type: shadowsocksr
type: vmess    → type: vmess
type: vless    → type: vless
type: trojan   → type: trojan
type: hysteria2 → type: hysteria2
type: tuic     → type: tuic
type: socks5   → type: socks (直连)

每个字段逐项映射（server/port/uuid/password/tls/transport/ws-opts/...）
```

#### 6.2.2 vless:// 链接

```
格式：vless://UUID@HOST:PORT?参数#名称

解析步骤：
1. 按 @ 分割 → 前面是 UUID，后面是 HOST:PORT?参数#名称
2. 按 : 分割 HOST:PORT → 拿到服务器和端口
3. 解析 query string:
   - security=tls/no   → tls.enabled
   - type=tcp/ws/grpc   → transport.type
   - host=...           → transport.headers.Host
   - path=...           → transport.path
   - fp=chrome/...      → tls.utls.fingerprint
   - sni=...            → tls.server_name
   - encryption=...     → 记录 encryption 字段
   - flow=...           → flow 字段 (如 xtls-rprx-vision)

示例：
vless://0c3d2134-83c8-46a5-8380-f631829fc247@104.16.158.61:2053
  ?security=tls
  &type=ws
  &host=mv.203065.xyz
  &fp=chrome
  &sni=mv.203065.xyz
  &path=/proxyip%3D154.16.183.190

→ sing-box outbound:
{
  "type": "vless",
  "server": "104.16.158.61",
  "server_port": 2053,
  "uuid": "0c3d2134-...",
  "tls": {
    "enabled": true,
    "server_name": "mv.203065.xyz",
    "utls": { "enabled": true, "fingerprint": "chrome" }
  },
  "transport": {
    "type": "ws",
    "path": "/proxyip=154.16.183.190",
    "headers": { "Host": "mv.203065.xyz" }
  }
}
```

#### 6.2.3 vmess:// 链接

```
格式：vmess://BASE64

解析步骤：
1. 取 vmess:// 后面的部分
2. Base64 解码 → 得到 JSON
3. 解析 JSON 字段:
   - add → server
   - port → server_port
   - id → uuid
   - aid → alter_id（VMess 旧协议，新版本通常为 0）
   - net → transport.type (tcp/ws/grpc/h2)
   - type → 伪装类型 (none/http)
   - host → transport.headers.Host
   - path → transport.path
   - tls → tls.enabled ("tls" → true)
   - sni → tls.server_name
   - fp → tls.utls.fingerprint
   - scy → security (aes-128-gcm/chacha20-poly1305/zero/none)
   - ps → 节点名称
   - v → 版本号 (通常为 2)
```

#### 6.2.4 ss:// 链接

```
格式：ss://BASE64(method:password)@HOST:PORT#名称

或：ss://BASE64(method:password@HOST:PORT)#名称

解析步骤：
1. Base64 解码 userinfo 部分 (method:password)
2. 提取 server, server_port
3. 如果含 plugin → 解析 plugin 参数

→ sing-box outbound:
{
  "type": "shadowsocks",
  "server": "host",
  "server_port": 8388,
  "method": "aes-256-gcm",
  "password": "your-password",
  "plugin": ""  // 如果有 plugin 字段
}
```

#### 6.2.5 trojan:// 链接

```
格式：trojan://PASSWORD@HOST:PORT?参数#名称

解析步骤：
1. userinfo = password
2. query params:
   - security=tls → tls.enabled=true
   - type=tcp/ws/grpc → transport.type
   - host=... → transport.headers.Host
   - path=... → transport.path
   - sni=... → tls.server_name
   - fp=... → tls.utls.fingerprint
   - alpn=http/1.1 → tls.alpn

→ sing-box outbound:
{
  "type": "trojan",
  "server": "host",
  "server_port": 443,
  "password": "your-password",
  "tls": { "enabled": true, "server_name": "example.com" }
}
```

#### 6.2.6 hysteria2:// 链接

```
格式：hysteria2://PASSWORD@HOST:PORT?参数#名称

解析步骤：
1. userinfo = password/auth
2. query params:
   - insecure=1 → skip-cert-verify=true
   - obfs=salamander → obfs 类型
   - obfs-password=... → obfs 密码
   - pinSHA256=... → 证书固定
   - up=... → 上行带宽
   - down=... → 下行带宽
   - sni=... → tls.server_name

→ sing-box outbound:
{
  "type": "hysteria2",
  "server": "host",
  "server_port": 443,
  "password": "your-password",
  "tls": { "enabled": true, "server_name": "..." },
  "obfs": { "type": "salamander", "password": "..." }
}
```

#### 6.2.7 tuic:// 链接

```
格式：tuic://UUID@HOST:PORT?参数#名称

类似 vless，但有 TUIC 特有参数：
  - token=... → 替代 UUID
  - congestion_control=bbr → 拥塞控制算法
  - alpn=h3 → ALPN
  - udp_relay_mode=native → UDP 中继模式

→ sing-box outbound:
{
  "type": "tuic",
  "server": "host",
  "server_port": 443,
  "uuid": "...",
  "tls": { "enabled": true, "server_name": "..." }
}
```

### 6.3 输出格式

所有格式解析后，统一输出为 sing-box outbound 格式的 dict 列表：

```python
[
    {
        "tag": "节点名称（去重）",
        "type": "vless",      # 协议类型
        "server": "1.2.3.4",  # 服务器地址
        "server_port": 443,   # 端口
        # ... 协议特定字段（完整保留原配置的所有参数）
    },
    # 每个节点一个
]
```

### 6.4 错误处理

| 情况 | 处理 |
|------|------|
| URL 无法连接 | 显示超时/连接失败错误，不阻塞其他操作 |
| URL 返回非 200 | 显示 HTTP 状态码错误 |
| 内容不是有效格式 | 尝试所有解析器，都失败则报错 |
| 单条链接格式错误 | 跳过该条，继续解析其他，显示警告 |
| 链接重复 | 去重（按 server:port 判断） |
| 链接没有名称 | 自动生成名称（如 "未命名-1"） |
| Base64 解码失败 | 尝试多种 Base64 变体，都失败则跳过 |

---

## 7. 节点测速模块

### 7.1 流程

```
用户点击"导入"或手动触发"测速"
  │
  ├─ 取所有已解析节点
  ├─ 生成临时测速配置
  ├─ 启动 sing-box（测速模式）
  ├─ 等待 sing-box 完成初始 URL 测试（最多 15 秒）
  ├─ 从 sing-box API 读取延迟数据
  ├─ 停止测速 sing-box
  └─ 返回测速结果
```

### 7.2 测速配置

```jsonc
{
  "log": { "level": "error" },
  "inbounds": [],
  "outbounds": [
    { "tag": "node-1", ... },
    { "tag": "node-2", ... },
    // 所有节点
    { "tag": "direct", "type": "direct" }
  ],
  "route": {
    "rules": [{ "outbound": "auto-test" }],
    "final": "auto-test"
  }
}
```

sing-box 的 `urltest` outbound 会自动探测所有子节点的延迟，无需 inbound。

### 7.3 返回数据

```python
{
    "node-1": { "alive": true,  "delay": 152 },  # 152ms
    "node-2": { "alive": true,  "delay": 89 },   # 89ms
    "node-3": { "alive": false, "delay": null },  # 不可用
}
```

延迟按颜色显示：
- <200ms → 绿色（快）
- 200~500ms → 黄色（中等）
- >500ms → 红色（慢）
- 不可用 → 红色 ❌

---

## 8. Web UI 设计

### 8.1 设计原则

- **颜色标记信息**：关键信息不用纯文字，用颜色区分
  - ✅ 绿色：正常、可用、延迟低
  - ⚠️ 黄色：延迟中等、有警告
  - ❌ 红色：不可用、错误、延迟高
  - ℹ️ 蓝色：信息提示
- **布局清晰**：三个 Tab 页，每个 Tab 一个独立功能
- **无框架**：纯 HTML + CSS + JS，无构建步骤
- **响应式**：桌面浏览器为主

### 8.2 页面布局

#### Tab 1：导入（Import）

```
┌─────────────────────────────────────────────────┐
│ [导入]  [分配]  [仪表盘]                          │
├─────────────────────────────────────────────────┤
│ 订阅链接: [___________________________] [导入]    │
│ 或粘贴配置文本:                                   │
│ ┌─────────────────────────────────────────────┐ │
│ │                                             │ │
│ │ (textarea)                                   │ │
│ │                                             │ │
│ └─────────────────────────────────────────────┘ │
│ [解析节点]                                       │
├─────────────────────────────────────────────────┤
│ 节点列表（共 25 个）                              │
│ ┌──────┬──────┬────────┬────────┬───────────┐  │
│ │ 节点名 │ 类型  │ 地址    │ 状态    │ 延迟      │  │
│ ├──────┼──────┼────────┼────────┼───────────┤  │
│ │ 香港01│ VLESS│ 1.2.3.4│ ✅ 可用 │ 🟢 152ms │  │
│ │ 日本02│  SS  │ 4.5.6.7│ ✅ 可用 │ 🟡 312ms │  │
│ │ 美国03│Trojan│ 8.9.0.1│ ❌ 超时 │ 🔴 N/A   │  │
│ └──────┴──────┴────────┴────────┴───────────┘  │
└─────────────────────────────────────────────────┘
```

#### Tab 2：分配（Assign）

```
┌─────────────────────────────────────────────────┐
│ [导入]  [分配]  [仪表盘]                          │
├─────────────────────────────────────────────────┤
│ 勾选要使用的节点：                                 │
│ ┌─────────────────────────────────────────────┐ │
│ │ ☑ 香港01  🟢 152ms  ──→ 端口 8001          │ │
│ │ ☑ 日本02  🟡 312ms  ──→ 端口 8002          │ │
│ │ ☐ 美国03  🔴 N/A                            │ │
│ │ ☑ 新加坡  🟢 89ms   ──→ 端口 8003          │ │
│ │ ...                                         │ │
│ └─────────────────────────────────────────────┘ │
│                                                │
│ 起始端口: [8001]  [自动分配]                     │
│                                                │
│ ┌─────────────────────────────────────────────┐ │
│ │ 端口  │ 节点    │ 状态    │ 出口IP            │ │
│ ├───────┼────────┼────────┼──────────────────┤ │
│ │ 8001  │ 香港01  │ ✅ 运行 │ 203.0.113.1     │ │
│ │ 8002  │ 日本02  │ ✅ 运行 │ 198.51.100.2   │ │
│ │ 8003  │ 新加坡  │ ✅ 运行 │ 192.0.2.3      │ │
│ └───────┴────────┴────────┴──────────────────┘ │
│                                                │
│ [保存配置]  [启动引擎]                            │
└─────────────────────────────────────────────────┘
```

#### Tab 3：仪表盘（Dashboard）

```
┌─────────────────────────────────────────────────┐
│ [导入]  [分配]  [仪表盘]                          │
├─────────────────────────────────────────────────┤
│ 引擎状态: 🟢 运行中  运行时间: 2h 15m             │
├─────────────────────────────────────────────────┤
│ 端口  │ 节点    │ 类型  │ 出口IP        │ 延迟   │
│───────┼────────┼───────┼───────────────┼───────┤
│ 8001  │ 香港01 │ VLESS│ 203.0.113.1  │ 152ms │
│ 8002  │ 日本02 │ SS   │ 198.51.100.2 │ 312ms │
│ 8003  │ 新加坡 │ Trojan│ 192.0.2.3   │ 89ms  │
│ 8004  │ 美国04 │ Hyst2 │ 1.2.3.4     │ 450ms │
├─────────────────────────────────────────────────┤
│ [刷新]  [停止引擎]                                │
└─────────────────────────────────────────────────┘
```

---

## 9. 持久化设计（config/assignments.json）

保存完整的运行时状态，让工具可以退出后恢复：

```jsonc
{
  // 版本号，用于未来兼容
  "version": 1,

  // 元数据
  "updated_at": "2026-06-04T12:00:00Z",

  // 订阅来源（可选，用于重新导入）
  "subscription_url": "https://www.yfjc.xyz/api/v1/client/subscribe?token=...",

  // 完整的节点列表（含配置，用于重新生成 sing-box 配置）
  "nodes": [
    {
      "tag": "hkg-01",
      "name": "香港01",
      "type": "vless",
      "server": "104.16.158.61",
      "server_port": 2053,
      "uuid": "0c3d2134-...",
      "tls": { "enabled": true, "server_name": "mv.203065.xyz" },
      "transport": { "type": "ws", "path": "/proxyip=..." }
    }
  ],

  // 端口映射
  "port_mappings": {
    "8001": { "node_tag": "hkg-01" },
    "8002": { "node_tag": "jpn-02" },
    "8003": { "node_tag": "sgp-03" }
  },

  // 上次测速结果缓存
  "latency_cache": {
    "hkg-01": { "alive": true, "delay": 152, "checked_at": "2026-06-04T12:00:00Z" },
    "jpn-02": { "alive": true, "delay": 312, "checked_at": "2026-06-04T12:00:00Z" }
  }
}
```

**启动恢复流程：**

```
main.py start
  │
  ├─ 读取 assignments.json
  │   ├─ 文件存在且有效 → 恢复节点列表和端口映射
  │   ├─ 文件损坏 → 备份为 assignments.json.bak，提示用户
  │   └─ 文件不存在 → 空状态，引导用户导入
  │
  ├─ 从 nodes + port_mappings 生成 sing-box 配置
  │
  └─ 启动 sing-box
```

---

## 10. REST API 完整规范

| 方法 | 路径 | 请求 | 响应 | 说明 |
|------|------|------|------|------|
| `GET` | `/` | - | HTML | Web 界面首页 |
| `POST` | `/api/import` | `{"url": "..."}` 或 `{"text": "..."}` | `{nodes: [...], count: N}` | 导入订阅/链接 |
| `POST` | `/api/test` | `{"nodes": [...]}` | `{results: {tag: {alive, delay}}}` | 对指定节点列表测速 |
| `GET` | `/api/nodes` | - | `{nodes: [{tag, name, type, alive, delay}]}` | 获取所有节点+状态 |
| `PUT` | `/api/assign` | `{mappings: {8001: "tag-1", 8002: "tag-2"}}` | `{ok: true}` | 分配端口映射 |
| `GET` | `/api/ports` | - | `{ports: {8001: {node_tag, node_name, exit_ip}}}` | 查询当前端口映射 |
| `GET` | `/api/ports/{port}/ip` | - | `{port: 8001, node_tag: "...", exit_ip: "..."}` | 查询某端口的出口 IP |
| `POST` | `/api/start` | - | `{ok: true}` | 启动引擎 |
| `POST` | `/api/stop` | - | `{ok: true}` | 停止引擎 |
| `GET` | `/api/status` | - | `{running: true, uptime: "2h15m"}` | 引擎状态 |

**出口 IP 查询原理：**

通过 sing-box 内置 API 查询不到出口 IP，需要用程序发起请求：

```
GET /api/ports/{port}/ip

1. 确定该端口对应的 outbound tag
2. 通过该端口发起 HTTP 请求到 https://ipv4.webshare.io/（或类似服务）
3. 返回响应中的 IP 地址
4. 缓存结果（60 秒有效期）
```

---

## 11. 依赖

### Python 依赖（requirements.txt）

```
fastapi>=0.110.0      # Web 框架
uvicorn>=0.29.0        # ASGI 服务器
httpx>=0.27.0          # HTTP 客户端（用于下载订阅）
pyyaml>=6.0            # YAML 解析（用于 Clash 格式）
```

只有 4 个依赖，总安装体积 < 10MB。

### 外部依赖

- **sing-box 二进制**（自动下载，~15MB）
- 下载源：GitHub Releases（MetaCubeX/sing-box）
- 版本：可配置，默认最新稳定版

---

## 12. 错误处理

| 场景 | 检测时机 | 处理方式 | 用户看到 |
|------|---------|---------|---------|
| 订阅 URL 不可达 | POST /api/import | 重试 1 次，间隔 3 秒 | 🔴 "无法连接，请检查网络或 URL" |
| 订阅内容格式无法解析 | POST /api/import | 尝试所有解析器 | 🔴 "无法识别此格式" |
| 部分节点解析失败 | POST /api/import | 跳过，继续解析其他 | 🟡 "已解析 28/30 个节点，2 个格式错误已跳过" |
| 端口被占用 | PUT /api/assign | 自动递增端口号 | 🟡 "端口 8001 已被占用，已改为 8011" |
| sing-box 启动失败 | POST /api/start | 重试 3 次 | 🔴 "引擎启动失败，错误信息：..." |
| sing-box 运行中崩溃 | engine 守护协程 | 自动重启（最多 3 次） | 🟡 "引擎已自动重启" 或 🔴 "引擎已崩溃 3 次，请手动检查" |
| sing-box 二进制不存在 | POST /api/start | 自动下载 | 🟡 "正在下载 sing-box..." |
| 互联网连接中断 | 所有 API | 返回错误 | 🔴 "请求失败，请检查网络" |
| assignments.json 损坏 | main.py 启动 | 备份原文件，创建空配置 | 🟡 "配置文件已损坏，已备份为 assignments.json.bak" |

---

## 13. 项目文件结构

```
proxy-pool-manager/
│
├── main.py                  # 入口：CLI 参数解析 + 启动 FastAPI
├── parser.py                # 订阅解析器：所有链接格式 → sing-box outbound
├── generator.py             # 配置生成器：节点列表 → sing-box.json
├── engine.py                # 引擎管理器：启动/停止/监控 sing-box 进程
├── api.py                   # FastAPI 应用：REST API + Web UI 路由
├── requirements.txt         # Python 依赖
│
├── templates/
│   └── index.html           # 单页 Web UI（导入/分配/仪表盘）
│
├── config/
│   ├── assignments.json     # 持久化：节点列表 + 端口映射
│   └── sing-box.json        # 生成的 sing-box 引擎配置
│
├── bin/
│   ├── sing-box-windows-amd64.exe   # 自动下载
│   └── sing-box-linux-amd64         # 自动下载
│
├── docs/
│   └── 2026-06-04-proxy-pool-manager-design.md   # 本文档
│
└── README.md                # 使用说明
```

---

## 14. 未来可能的扩展（暂不做）

- **动态切换节点**：不重启直接换端口的节点（通过 sing-box API 切换 outbound）
- **多订阅合并**：一次导入多个订阅，节点统一管理
- **流量统计**：记录每个端口使用了多少流量
- **健康检查告警**：节点挂了时通知用户
- **HTTP 代理支持**：除了 SOCKS5，也提供 HTTP 代理端口
- **用户认证**：给代理端口加用户名密码
