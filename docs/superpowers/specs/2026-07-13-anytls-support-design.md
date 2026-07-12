# AnyTLS 节点支持设计

日期：2026-07-13
状态：待实现

## 目标

在不改变现有 API、状态结构、端口分配、测速流程和其他协议行为的前提下，为 Proxy Pool Manager 增加 AnyTLS 节点的完整支持：

- 导入 `anytls://` URI；
- 导入包含 AnyTLS URI 的明文或 Base64 订阅；
- 导入 Clash/Mihomo YAML 中的 `type: anytls` 节点；
- 使用现有 sing-box 临时引擎完成测速和出口 IP 检测；
- 通过现有端口映射生成可运行的 AnyTLS outbound；
- 在前端正确显示 AnyTLS 协议标签。

## 非目标

- 不重构现有协议分发为注册表；
- 不修改 API 请求或响应结构；
- 不修改 `ProxyNode`、`AppState` 或持久化文件格式；
- 不增加 AnyTLS 专用测速器；
- 不增加新的前端交互、筛选器或设置项；
- 不支持 AnyTLS 服务端配置。

## 兼容基础

项目当前内置 sing-box 1.13.13，能够识别 AnyTLS outbound。现有 `generate_config()` 会将节点的 `outbound` 复制进 sing-box 配置，因此 AnyTLS 只需在解析阶段生成合法 outbound，即可复用现有配置校验、测速、端口分配和运行链路。

## URI 导入

### 输入格式

```text
anytls://PASSWORD@SERVER:PORT?QUERY#NAME
```

URI userinfo 中 `@` 前的值作为 AnyTLS password。节点名称使用 URL 解码后的 fragment。

### 标准输出

```json
{
  "type": "anytls",
  "server": "example.com",
  "server_port": 443,
  "password": "password",
  "tls": {
    "enabled": true,
    "server_name": "cdn.example.com",
    "insecure": true
  }
}
```

### 查询参数映射

| URI 参数 | sing-box 字段 | 规则 |
|---|---|---|
| `security` | `tls.enabled` | `tls` 或存在 TLS 相关参数时启用；AnyTLS 默认启用 TLS |
| `sni`, `peer` | `tls.server_name` | 优先 `sni` |
| `insecure`, `allowInsecure`, `skip-cert-verify` | `tls.insecure` | 使用现有 truthy 规则 |
| `fp` | `tls.utls.fingerprint` | 同时设置 `utls.enabled=true` |
| `alpn` | `tls.alpn` | 逗号分隔，允许重复 query 参数 |
| `idle_session_check_interval`, `idle-session-check-interval` | `idle_session_check_interval` | duration 规范化 |
| `idle_session_timeout`, `idle-session-timeout` | `idle_session_timeout` | duration 规范化 |
| `min_idle_session`, `min-idle-session` | `min_idle_session` | 转为非负整数 |

`type=tcp` 和 `headerType=none` 不生成 transport；它们对 sing-box AnyTLS outbound 不是必需字段。

### 基础校验

导入时要求：

- server 非空；
- port 在 1–65535；
- password 非空。

无效节点沿用现有行为：跳过节点并在 `warnings` 中报告，不阻止同一订阅内其他节点导入。

## Base64 订阅

不新增独立 Base64 实现。将 `anytls` 加入现有 `SUPPORTED_LINK_SCHEMES` 后，继续复用：

1. 订阅文本噪声清理；
2. Base64 解码；
3. 逐行 scheme 检测；
4. `parse_link()` 分发；
5. tag 去重。

## Clash/Mihomo YAML 导入

在现有 `parse_clash_yaml()` 中增加 `ptype == "anytls"` 分支。

支持字段：

- `server`
- `port`
- `password`
- `sni` / `servername`
- `skip-cert-verify`
- `client-fingerprint`
- `alpn`
- `idle-session-check-interval`
- `idle-session-timeout`
- `min-idle-session`

转换规则：

- `sni` 或 `servername` → `tls.server_name`；
- `skip-cert-verify` → `tls.insecure`；
- `client-fingerprint` → `tls.utls`；
- 数字 duration（例如 `30`）→ sing-box 字符串 duration（`"30s"`）；
- 已带单位的 duration 字符串保持不变；
- `min-idle-session` 转为非负整数；
- 缺失必填字段时跳过并生成 warning。

## 节点模型和标识

AnyTLS 节点继续使用现有 `ProxyNode`：

```python
ProxyNode(
    tag="node-anytls-...",
    name="...",
    type="anytls",
    server="...",
    server_port=443,
    outbound={...},
)
```

不修改现有 tag 算法，以避免影响已有节点的持久化标识和端口映射。

## 测速与运行

不增加协议分支。链路保持：

```text
ProxyNode
  → generate_config()
  → sing-box check
  → 临时 mixed inbound
  → AnyTLS outbound
  → HTTP 测试目标
  → 延迟、状态、出口 IP
```

正式运行同样通过现有端口映射生成 mixed inbound，并路由到 AnyTLS outbound。

## 前端

只修改协议视觉映射：

- `static/helpers.js` 的已知协议列表加入 `anytls`；
- `static/style.css` 增加 `.proto.anytls` 样式。

节点表、端口表、搜索、状态刷新和事件绑定不变。

## 测试策略

使用虚构域名和凭据，不将用户提供的真实节点写入仓库。

新增测试：

1. 标准 AnyTLS URI；
2. URL 编码名称；
3. SNI、insecure、fingerprint、ALPN；
4. idle-session 参数两种命名形式；
5. Base64 AnyTLS 订阅；
6. Clash/Mihomo AnyTLS YAML；
7. 数字 duration 转换；
8. 缺少 password 等必填字段时产生 warning；
9. `generate_config()` 输出正确 AnyTLS outbound；
10. 使用项目内置 sing-box 执行 `check`；
11. 运行现有完整 pytest 和前端契约测试。

## 预期修改文件

- `app/parser.py`
- `static/helpers.js`
- `static/style.css`
- `tests/test_parser.py`
- 必要时 `tests/test_generator.py`
- `README.md`（更新协议支持说明）

不计划修改：

- `app/models.py`
- `app/generator.py`
- `app/tester.py`
- `app/engine.py`
- `app/api.py`
- API schemas 和模板结构

## 验收标准

- 用户提供格式的 URI 能导入为一个 `type=anytls` 节点；
- 其 Base64 文本能通过现有订阅入口导入；
- Mihomo `type: anytls` YAML 能导入；
- 生成配置通过 sing-box 1.13.13 `check`；
- AnyTLS 节点可走现有测速流程并可分配端口运行；
- 前端显示独立 AnyTLS 协议标签；
- 所有原有测试通过；
- VLESS、VMess、Shadowsocks、Trojan、Hysteria2 和 TUIC 的解析结果不发生变化。
