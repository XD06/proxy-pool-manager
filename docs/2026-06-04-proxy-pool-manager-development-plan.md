# Proxy Pool Manager — 实际开发文档

**日期：** 2026-06-04  
**状态：** Draft v1  
**基于：** `docs/2026-06-04-proxy-pool-manager-design.md`

---

## 1. 开发目标

本项目要实现一个本地代理端口管理工具：

- 用户导入代理节点或订阅。
- 系统解析节点并保存。
- 用户把节点固定绑定到本地 SOCKS5 端口。
- 程序生成 sing-box 配置并启动 sing-box。
- 代理流量直接走 sing-box，Python 只负责管理，不参与数据转发。
- Web UI 提供导入、分配、启动、停止、状态、出口 IP 查询。

第一版设计方向可行，但实际开发时要收敛范围，先做稳定 MVP，再扩展复杂协议和高级功能。

---

## 2. MVP 范围

### 2.1 第一阶段必须实现

| 功能 | 说明 |
|------|------|
| 本地 Web UI | `http://127.0.0.1:9000` 打开管理界面 |
| 单链接导入 | 支持 `vless://`、`vmess://`、`ss://`、`trojan://` |
| Clash YAML 导入 | 支持常见 `proxies:` 配置 |
| 节点保存 | 保存到 `config/assignments.json` |
| 端口分配 | 每个节点固定绑定一个本地 SOCKS5 端口 |
| sing-box 配置生成 | 生成 `config/sing-box.json` |
| 引擎启动/停止 | Python 管理 sing-box 子进程 |
| 出口 IP 查询 | 通过指定本地端口请求外部 IP 服务 |
| 基础测速 | 对节点可用性和延迟做基础检测 |

### 2.2 第一阶段暂不实现

| 功能 | 原因 |
|------|------|
| SSR | sing-box 当前主线 outbound 不应假设支持 SSR |
| Hysteria2/TUIC 完整兼容 | 参数多，放到第二阶段 |
| 多订阅合并 | 第一版先处理单次导入结果 |
| 热重载 | Windows 支持不一致，第一版用重启引擎 |
| 流量统计 | 不是核心需求 |
| 用户认证 | 本地工具，第一版仅监听 `127.0.0.1` |
| HTTP 代理端口 | 第一版只做 SOCKS5 |

---

## 3. 技术架构

### 3.1 进程关系

```text
Browser
  |
  | HTTP :9000
  v
Python FastAPI 管理层
  |
  | 写配置 / 启停进程
  v
sing-box 子进程
  |
  | SOCKS5 :8001/:8002/...
  v
远端代理节点
```

### 3.2 数据路径

正式代理流量路径：

```text
用户程序 -> 127.0.0.1:8001 -> sing-box inbound -> sing-box outbound -> 远端节点 -> 目标网站
```

Python 不处理代理流量，只处理：

- 订阅下载
- 节点解析
- 配置生成
- 引擎生命周期
- Web API
- 测速和出口 IP 查询

测速和出口 IP 查询可以由 Python 发起请求，因为这不是正式业务流量。

---

## 4. 项目结构

```text
proxy-pool-manager/
├── main.py
├── api.py
├── parser.py
├── generator.py
├── engine.py
├── store.py
├── tester.py
├── models.py
├── requirements.txt
├── templates/
│   └── index.html
├── config/
│   ├── assignments.json
│   └── sing-box.json
├── bin/
│   └── sing-box...
└── docs/
    ├── 2026-06-04-proxy-pool-manager-design.md
    └── 2026-06-04-proxy-pool-manager-development-plan.md
```

### 4.1 模块职责

| 文件 | 职责 |
|------|------|
| `main.py` | 启动入口，加载配置，启动 FastAPI |
| `api.py` | REST API 和静态 Web UI 路由 |
| `parser.py` | 把订阅、配置文本、单节点链接解析为内部节点对象 |
| `generator.py` | 把节点和端口映射生成 sing-box JSON |
| `engine.py` | 下载、启动、停止、监控 sing-box |
| `store.py` | 读写 `assignments.json`，处理损坏备份 |
| `tester.py` | 节点测速、出口 IP 查询 |
| `models.py` | Pydantic 数据模型和内部数据结构 |

---

## 5. 数据模型

### 5.1 节点模型

内部节点对象保存为接近 sing-box outbound 的结构，但要额外保留显示字段。

```python
class ProxyNode(BaseModel):
    tag: str
    name: str
    type: str
    server: str
    server_port: int
    outbound: dict
```

要求：

- `tag` 必须唯一。
- `name` 用于 UI 显示。
- `outbound` 是可直接写入 sing-box `outbounds` 的 dict。
- `outbound["tag"]` 必须等于 `tag`。

### 5.2 持久化文件

`config/assignments.json`：

```json
{
  "version": 1,
  "updated_at": "2026-06-04T12:00:00Z",
  "subscription_url": null,
  "nodes": [],
  "port_mappings": {
    "8001": {
      "node_tag": "hkg-01"
    }
  },
  "latency_cache": {
    "hkg-01": {
      "alive": true,
      "delay": 152,
      "checked_at": "2026-06-04T12:00:00Z"
    }
  },
  "exit_ip_cache": {
    "8001": {
      "ip": "203.0.113.1",
      "checked_at": "2026-06-04T12:00:00Z"
    }
  }
}
```

损坏处理：

1. 读取 JSON 失败时，把原文件复制为 `assignments.json.bak-YYYYMMDD-HHMMSS`。
2. 创建空状态。
3. API 返回 warning，UI 显示配置已重置。

---

## 6. sing-box 配置生成

### 6.1 正式运行配置

生成 `config/sing-box.json`。

必须包含：

- `log`
- `experimental.clash_api`
- `inbounds`
- `outbounds`
- `route`

示例：

```json
{
  "log": {
    "level": "warn",
    "output": "config/sing-box.log",
    "disabled": false
  },
  "experimental": {
    "clash_api": {
      "external_controller": "127.0.0.1:9090",
      "secret": ""
    },
    "cache_file": {
      "enabled": true,
      "path": "config/cache.db"
    }
  },
  "inbounds": [
    {
      "type": "socks",
      "tag": "port-8001",
      "listen": "127.0.0.1",
      "listen_port": 8001,
      "sniff": false
    }
  ],
  "outbounds": [
    {
      "type": "vless",
      "tag": "hkg-01",
      "server": "1.2.3.4",
      "server_port": 443,
      "uuid": "00000000-0000-0000-0000-000000000000",
      "tls": {
        "enabled": true,
        "server_name": "example.com"
      }
    },
    {
      "type": "direct",
      "tag": "direct"
    },
    {
      "type": "block",
      "tag": "block"
    }
  ],
  "route": {
    "rules": [
      {
        "inbound": ["port-8001"],
        "action": "route",
        "outbound": "hkg-01"
      }
    ],
    "final": "direct",
    "auto_detect_interface": true
  }
}
```

### 6.2 生成规则

1. 每个端口生成一个 `socks` inbound。
2. 每个被映射节点生成一个 outbound。
3. 所有节点 outbound 后追加 `direct` 和 `block`。
4. 每个端口生成一条 route rule：

```json
{
  "inbound": ["port-8001"],
  "action": "route",
  "outbound": "node-tag"
}
```

5. 未匹配流量走 `direct`，但正常情况下不应该有未匹配 inbound。

### 6.3 配置校验

启动前必须执行：

```text
sing-box check -c config/sing-box.json
```

只有校验通过才允许启动。

---

## 7. 解析器实现

### 7.1 输入类型判断

`POST /api/import` 接收：

```json
{
  "url": "https://example.com/sub"
}
```

或：

```json
{
  "text": "vless://..."
}
```

判断顺序：

1. 如果有 `url`，用 `httpx` 下载文本。
2. 如果文本包含 `proxies:`，按 Clash YAML 解析。
3. 如果文本可 Base64 解码且解码后含 `://`，按 Base64 订阅解析。
4. 如果文本逐行包含 `://`，按单链接列表解析。
5. 以上都失败则返回格式错误。

### 7.2 MVP 协议

第一阶段实现：

- `vless://`
- `vmess://`
- `ss://`
- `trojan://`
- Clash YAML 中的 `ss/vmess/vless/trojan`

第二阶段实现：

- `hysteria2://`
- `tuic://`

暂不实现：

- `ssr://`

### 7.3 去重和命名

去重规则：

- 第一阶段按 `type + server + server_port + credential` 去重。
- 不只按 `server:port` 去重，因为同一服务器端口可能有不同 UUID 或密码。

tag 生成规则：

1. 优先从节点名生成 slug。
2. 去掉特殊字符。
3. 小写化。
4. 如果重复，追加 `-2`、`-3`。
5. 如果没有名称，使用 `node-1`、`node-2`。

---

## 8. 测速和出口 IP

### 8.1 MVP 测速方案

第一版不要强依赖 sing-box Clash API 的 urltest 延迟读取，优先实现稳定方案：

1. 为待测节点生成临时 sing-box 配置。
2. 每个待测节点绑定一个临时本地 SOCKS5 端口，例如 `19001` 起。
3. 启动临时 sing-box。
4. Python 通过对应 SOCKS5 端口请求 `https://www.gstatic.com/generate_204`。
5. 记录耗时。
6. 停止临时 sing-box。

优点：

- 返回结果完全由程序控制。
- 不依赖 sing-box API 返回格式。
- 失败原因容易归类。

缺点：

- 节点很多时较慢。

并发限制：

- 默认最多并发 10 个节点。
- 单节点超时 8 秒。
- 总测速超时 60 秒。

### 8.2 出口 IP 查询

`GET /api/ports/{port}/ip`：

1. 检查端口是否已分配。
2. 检查 sing-box 是否运行。
3. 如果缓存未过期，直接返回。
4. 通过 `socks5://127.0.0.1:{port}` 请求 IP 服务。
5. 保存 60 秒缓存。

推荐服务：

- `https://api.ipify.org`
- `https://ipv4.icanhazip.com`

失败时返回：

```json
{
  "port": 8001,
  "node_tag": "hkg-01",
  "exit_ip": null,
  "error": "timeout"
}
```

---

## 9. 引擎管理

### 9.1 sing-box 二进制

默认下载源使用官方仓库：

```text
https://github.com/SagerNet/sing-box/releases
```

下载策略：

1. 启动时检查 `bin/` 下是否已有对应平台二进制。
2. 如果没有，提示 UI 正在下载。
3. 下载后解压并设置可执行权限。
4. Windows 使用 `.exe`，Linux 使用无后缀二进制。

平台识别：

- Windows amd64
- Linux amd64

ARM、macOS 放到后续阶段。

### 9.2 启动流程

```text
POST /api/start
  |
  |-- 保存当前 assignments.json
  |-- 生成 config/sing-box.json
  |-- sing-box check -c config/sing-box.json
  |-- 如果已有进程，先 stop
  |-- 启动 sing-box run -c config/sing-box.json
  |-- 等待 1 秒检查进程状态
  |-- 返回运行状态
```

### 9.3 停止流程

```text
POST /api/stop
  |
  |-- terminate
  |-- 等待最多 3 秒
  |-- 未退出则 kill
  |-- 清理进程状态
```

### 9.4 崩溃监控

FastAPI 启动后创建后台任务：

- 每 5 秒检查 sing-box 进程。
- 如果用户期望状态是 running，但进程退出，则尝试重启。
- 连续失败 3 次后进入 fatal 状态。
- UI 显示最近一次 stderr 摘要。

---

## 10. REST API

### 10.1 页面

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | 返回 Web UI |

### 10.2 节点

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/import` | 导入 URL 或文本 |
| `GET` | `/api/nodes` | 获取节点列表 |
| `POST` | `/api/test` | 对指定节点测速 |

`POST /api/import` 响应：

```json
{
  "ok": true,
  "count": 12,
  "nodes": [],
  "warnings": []
}
```

### 10.3 端口

| 方法 | 路径 | 说明 |
|------|------|------|
| `PUT` | `/api/assign` | 保存端口映射 |
| `GET` | `/api/ports` | 获取端口映射 |
| `GET` | `/api/ports/{port}/ip` | 查询出口 IP |

`PUT /api/assign` 请求：

```json
{
  "mappings": {
    "8001": "hkg-01",
    "8002": "jpn-01"
  }
}
```

校验：

- 端口必须是整数。
- 端口范围默认 `1024-65535`。
- 不允许重复端口。
- `node_tag` 必须存在。
- 如果端口已被其他进程占用，返回错误，不自动偷偷改端口。

说明：第一版不建议自动递增端口，因为这会让用户以为自己绑定的是 `8001`，实际变成 `8011`。端口被占用应明确提示，由用户重新分配。

### 10.4 引擎

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/start` | 启动 sing-box |
| `POST` | `/api/stop` | 停止 sing-box |
| `GET` | `/api/status` | 查询运行状态 |

`GET /api/status` 响应：

```json
{
  "running": true,
  "pid": 1234,
  "uptime_seconds": 3600,
  "fatal": false,
  "last_error": null
}
```

---

## 11. Web UI

第一版使用纯 HTML、CSS、JS，无构建步骤。

### 11.1 页面结构

三个 Tab：

1. 导入
2. 分配
3. 仪表盘

### 11.2 导入页

功能：

- 输入订阅 URL。
- 粘贴配置文本。
- 点击导入。
- 显示节点表格。
- 显示解析 warning。
- 支持手动测速。

节点表格字段：

- 名称
- 类型
- 地址
- 状态
- 延迟

### 11.3 分配页

功能：

- 勾选节点。
- 输入起始端口。
- 自动生成端口映射。
- 手动修改端口。
- 保存配置。
- 启动引擎。

### 11.4 仪表盘

功能：

- 显示引擎运行状态。
- 显示端口到节点的映射。
- 显示出口 IP。
- 刷新状态。
- 停止引擎。

---

## 12. Python 依赖

`requirements.txt`：

```text
fastapi>=0.110.0
uvicorn>=0.29.0
httpx>=0.27.0
httpx[socks]>=0.27.0
pyyaml>=6.0
pydantic>=2.0.0
```

说明：

- `httpx[socks]` 用于通过本地 SOCKS5 端口做测速和出口 IP 查询。
- 如果安装解析冲突，可以改用 `python-socks`。

---

## 13. 开发顺序

### 13.1 第 1 步：项目骨架

交付：

- `requirements.txt`
- `main.py`
- `api.py`
- `models.py`
- 空 Web UI

验收：

- `python main.py` 后能访问 `http://127.0.0.1:9000`
- `GET /api/status` 返回正常 JSON

### 13.2 第 2 步：持久化

交付：

- `store.py`
- `assignments.json` 自动创建
- 损坏备份逻辑

验收：

- 新启动时能创建空配置。
- 修改节点和端口后能保存。
- 重启后状态能恢复。

### 13.3 第 3 步：解析器

交付：

- `parser.py`
- 单链接解析
- Clash YAML 解析
- Base64 订阅解析

验收：

- 能解析常见 `vless/vmess/ss/trojan` 链接。
- 单条坏链接不会导致整个导入失败。
- 重复节点会去重。

### 13.4 第 4 步：配置生成器

交付：

- `generator.py`
- `config/sing-box.json` 生成

验收：

- 生成的 JSON 可被 `sing-box check -c` 通过。
- 每个端口对应正确 inbound。
- 每个 inbound route 到正确 outbound。

### 13.5 第 5 步：引擎管理

交付：

- `engine.py`
- sing-box 下载或本地发现
- 启动、停止、状态查询

验收：

- `POST /api/start` 后本地端口可连接。
- `POST /api/stop` 后端口释放。
- 配置错误时返回清晰错误。

### 13.6 第 6 步：测速和出口 IP

交付：

- `tester.py`
- `POST /api/test`
- `GET /api/ports/{port}/ip`

验收：

- 可用节点返回延迟。
- 不可用节点返回失败原因。
- 出口 IP 查询走对应端口。

### 13.7 第 7 步：Web UI 完整联调

交付：

- 导入页
- 分配页
- 仪表盘

验收：

- 用户能通过 UI 完成：导入 -> 分配 -> 启动 -> 查询出口 IP -> 停止。

---

## 14. 测试策略

### 14.1 单元测试

重点测试：

- `parser.py`
- `generator.py`
- `store.py`

推荐测试文件：

```text
tests/
├── test_parser_vless.py
├── test_parser_vmess.py
├── test_parser_ss.py
├── test_parser_trojan.py
├── test_generator.py
└── test_store.py
```

### 14.2 集成测试

需要本地 sing-box 二进制。

测试：

1. 生成一个 direct outbound 配置。
2. 启动 sing-box。
3. 通过本地 SOCKS5 端口访问 `https://api.ipify.org`。
4. 停止 sing-box。

### 14.3 手工验收

准备一个测试订阅或几条测试节点链接：

1. 导入节点。
2. 检查节点数量。
3. 分配端口 `8001-8003`。
4. 启动引擎。
5. 用 curl 验证：

```text
curl --socks5 127.0.0.1:8001 https://api.ipify.org
curl --socks5 127.0.0.1:8002 https://api.ipify.org
```

6. 确认不同端口返回不同出口 IP。

---

## 15. 错误处理要求

| 场景 | 行为 |
|------|------|
| 订阅 URL 超时 | 返回 `import_failed`，保留原节点 |
| 部分链接解析失败 | 返回 warnings，成功节点正常导入 |
| 没有可用节点 | 允许保存节点，但启动前提示没有端口映射 |
| 端口被占用 | 保存映射时返回错误 |
| sing-box 不存在 | 尝试下载，失败则提示手动放到 `bin/` |
| `sing-box check` 失败 | 不启动进程，返回 stderr 摘要 |
| 进程启动后立即退出 | 返回启动失败和日志摘要 |
| 出口 IP 查询失败 | 返回 `exit_ip: null` 和错误原因 |

---

## 16. 安全边界

第一版只做本地工具：

- Web 服务只监听 `127.0.0.1:9000`。
- sing-box inbound 只监听 `127.0.0.1`。
- 不开放局域网访问。
- 不记录代理流量内容。
- 不上传节点配置。
- `assignments.json` 可能包含敏感代理凭据，不应提交到 git。

需要加入 `.gitignore`：

```text
config/*.json
config/*.log
config/*.db
bin/
```

---

## 17. 风险和处理

| 风险 | 影响 | 处理 |
|------|------|------|
| 节点格式差异大 | 解析失败 | 第一版只支持主流字段，失败返回 warning |
| sing-box 配置字段随版本变化 | 启动失败 | 固定默认版本，启动前执行 `sing-box check` |
| GitHub 下载失败 | 无法启动 | 支持用户手动放置二进制 |
| 测速慢 | UI 等待久 | 并发限制 + 超时 + 后台任务 |
| 端口冲突 | 启动失败 | 分配时提前检测 |
| Windows 进程清理不彻底 | 端口残留 | stop 后检查端口释放 |

---

## 18. 第二阶段扩展

第一版稳定后再做：

1. Hysteria2/TUIC 完整解析。
2. Clash API 的 urltest 集成。
3. 多订阅合并。
4. HTTP 代理端口。
5. 更完整的节点健康检查。
6. 流量统计。
7. 动态切换端口对应节点。
8. Linux systemd 服务。
9. Windows 托盘程序。

---

## 19. 完成定义

MVP 完成必须满足：

1. Windows 上可运行。
2. Linux 上可运行，至少通过基础验证。
3. Web UI 能完成完整流程。
4. 至少支持 `vless/vmess/ss/trojan` 四类节点。
5. 端口映射重启后不丢失。
6. Python 不参与正式代理数据路径。
7. `sing-box check` 通过后才启动。
8. 出错时 UI 能显示清晰错误，不静默失败。

