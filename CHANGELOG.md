# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)：写给人看的变更摘要，同类归类。细节以 `git log` 为准。本仓库无版本 tag，以下按日期倒序归纳（`main` 分支）。

## [未发布]

### 已知问题

- `tests/test_frontend_contract.py::test_engine_start_stop_is_one_accessible_switch` 失败：测试断言 `static/app.js` 含 `running ? "/api/stop" : "/api/start"`，实际已演进为 `needsRecovery || !running ? "/api/start" : "/api/stop"`。开关契约本身（单 `engineToggleBtn` / `role="switch"`）仍通过，判为测试字符串过时（2026-09-05 实测 168 项：167 passed，1 failed）。

## [2026-08-04]

### 新增

- 节点池可零固定映射运行，池支持快捷启停开关。
- 引擎故障恢复并入主启停开关。

### 变更

- 池开关改为紧凑无图标胶囊样式，状态并入开关显示。
- 节点操作菜单互斥（一次只展开一个）。

### 修复

- 池快捷开关在升级路径下保持兼容。

## [2026-07-26 – 2026-07-29]

### 变更

- 测速性能：共享 httpx SSL 上下文，提高并发，启动时跳过被占用端口。

### 修复

- 移除已废弃 ProxyAdmin 功能并加固运行时。
- 加固流量存储、Job 清理、归档解压、Web 导出。
- 加固协议解析、端口分配、Web 响应。

### 移除

- 下线 ProxyAdmin 相关接口与前端。

## [2026-07-16 – 2026-07-19]

### 新增

- 流量监控：按公开端口 / 节点池 / 节点统计上下行、实时连接、历史趋势与安全事件。
- 节点池轮询：HTTP/SOCKS5 TCP 普通与加权轮询、手动前进、成员排空、拨号失败跳过。

### 文档

- 新增流量与节点池、可靠性与性能设计（见 `docs/superpowers/specs/` 与归档）。

## [2026-07-12 – 2026-07-13]

### 新增

- AnyTLS 节点支持（含设计文档）。
- 节点支持与 sing-box 管理增强（检测更新、下载替换、一键回退）。
- 多源 GeoIP 回退（geojs / ipwho / freeipapi / ipinfo / ip-api）。
- 前端契约测试：锁定 `app.js` 引用的 DOM id / `data-tab` / API 路径。

### 变更

- UI 改为浅色 Toolbench 控制台风格，4 Tab：监控 / 节点 / 分配 / 运行。
- 状态栏与导航 sticky + 透明毛玻璃，仪表盘与表格布局稳定化。

## [2026-06-30 – 2026-07-01]

### 新增

- AI 连通性检测：经代理端口测试 GPT / Claude / Gemini 可达性（后简化为内联结果展示）。

### 变更

- 抽取 `helpers.js` 纯工具函数；节点/端口表事件委托 + 指纹跳过渲染。
- 从 `api.py` 抽取 `schemas` / `utils` / 鉴权路由。
- P0 安全修复 + P1 架构优化。

## [2026-06-13 – 2026-06-21]

### 新增

- 管理台 `admin_key` 鉴权（`config/app.json` 或 `PPM_ADMIN_KEY`）与登录页。
- Linux 部署与 Docker 支持（`Dockerfile` / `docker-compose.yml`，控制台 `9100`）。
- 端口压实（compaction）操作与紧凑可用性检查。

### 变更

- 低资源控制面性能优化；节点测速可靠性加固（socks5h 探针、绑定 localhost、诊断输出）。

### 修复

- 登录页闪烁、已登录刷新门控、本地检测列恢复。

## [2026-06-04 – 2026-06-08]

### 新增

- MVP：多协议导入（VLESS / VMess / SS / Trojan，后续扩展 Hysteria2 / TUIC / AnyTLS / Clash YAML / Base64 订阅）、临时引擎测速、同出口 IP 去重、端口固定映射、sing-box 配置生成 + `check` 校验 + 启停监控、出口 IP 查询、运行后端口验证、纯静态 Web UI。
- `config/assignments.json` 持久化（含损坏备份）与 `config/app.json`（环境变量覆盖）。
- Windows / Linux 一键安装与启停脚本（`scripts/`，含 systemd），`docs/operation.md` 运行手册。
- pytest  suite（解析 / 生成 / 持久化 / 引擎 / API smoke 起步 15 项，后续扩展至 168 项）。

### 文档

- 初始设计、开发计划、开发日志（已移入 `docs/archive/`，仅本地保留）。
