# ProxyAdmin API

## 快速开始

```ts
import { ProxyAdmin } from './index.js'

const api = new ProxyAdmin(
  'http://127.0.0.1:8081',
  '你的 Bearer Token',
)
```

---

## `import(urls, replace?)`

导入节点，返回每个 URL 对应的 ID。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `urls` | `string \| string[]` | 是 | 一个或多个代理 URL |
| `replace` | `{ from: string; to: string }` | 否 | 替换 IP，例如 `{ from: '127.0.0.1', to: '172.17.0.1' }` |

**返回：** `Promise<ImportItem[]>`

```json
[
  {
    "url": "http://127.0.0.1:8001",
    "host": "172.17.0.1",
    "port": 8001,
    "id": 284
  },
  {
    "url": "http://127.0.0.1:8002",
    "host": "172.17.0.1",
    "port": 8002,
    "id": 285
  }
]
```

使用：
```ts
const items = await api.import([
  'http://127.0.0.1:8001',
  'http://127.0.0.1:8002',
], { from: '127.0.0.1', to: '172.17.0.1' })

// items[0].id → 284
// items[1].id → 285
```

---

## `query(ids, concurrency?)`

按 ID 查询质量检测结果，**逐个流式返回**，不用等全部完成。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ids` | `number[]` | 是 | ID 数组 |
| `concurrency` | `number` | 否 | 并发数，默认 10 |

**返回：** `AsyncGenerator<CheckResult>`

```json
{
  "id": 323,
  "exit_ip": "119.237.24.226",#出口ip
  "country": "香港",
  "score": 56,#ip得分
  "grade": "D",#ip基本，越往上越好
  "items": [
    {
      "target": "base_connectivity",
      "status": "pass",
      "latency_ms": 315,
      "message": "代理出口连通正常"
    },
    {
      "target": "openai",
      "status": "fail",
      "http_status": 403,
      "latency_ms": 509,
      "message": "非预期状态码: 403"
    },
    {
      "target": "anthropic",
      "status": "fail",
      "http_status": 403,
      "latency_ms": 707,
      "message": "非预期状态码: 403"
    },
    {
      "target": "gemini",
      "status": "pass",
      "http_status": 200,
      "latency_ms": 946,
      "message": "HTTP 200"
    }
  ]
}
```

`items` 中每个 `target` 的 `status` 含义：

| status | 含义 |
|---|---|
| `pass` | 可用 |
| `warn` | 有告警但可用 |
| `fail` | 不可用 |

使用：
```ts
for await (const r of api.query([284, 285, 286], 10)) {
  const openai = r.items.find(i => i.target === 'openai')
  const gemini = r.items.find(i => i.target === 'gemini')

  if (openai?.status !== 'fail') {
    console.log(`#${r.id} OpenAI 可用`)
  }
  if (gemini?.status !== 'fail') {
    console.log(`#${r.id} Gemini 可用`)
  }
}
```

---

## `retry(results, concurrency?)`

对上次查询中 `status = fail` 的节点重新检测。自动从传入的结果中筛选出 fail 的 ID，重新请求质量检测。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `results` | `CheckResult[]` | 是 | 上次 query 收集到的结果数组 |
| `concurrency` | `number` | 否 | 并发数，默认 10 |

**返回：** `AsyncGenerator<CheckResult>`（和 query 同一个结构）

使用：
```ts
// 先查一批
const results: CheckResult[] = []
for await (const r of api.query([284, 285, 286])) {
  results.push(r)
}

// 对其中 fail 的重新检测
for await (const r of api.retry(results)) {
  // 只重试了 fail 的节点，逐个返回
  const openai = r.items.find(i => i.target === 'openai')
  console.log(`#${r.id} openai=${openai?.status} grade=${r.grade}`)
}
```

---

## `remove(options, concurrency?)`

删除节点，支持三种模式。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `options` | `RemoveOptions` | 是 | 见下方 |
| `concurrency` | `number` | 否 | 并发数，默认 5 |

**`RemoveOptions` 三种模式（三选一）：**

```ts
// 模式 1：按 ID 删除
{ ids: number[] }

// 模式 2：删上次查询中 status=fail 的节点
{ failed: CheckResult[] }

// 模式 3：删所有未使用的节点（account_count === 0）
{ unused: true }
```

**返回：** `Promise<DeleteResult[]>`

```json
[
  { "id": 284, "success": true },
  { "id": 285, "success": false, "message": "proxy is in use by accounts" },
  { "id": 999, "success": true }
]
```

使用：
```ts
// 按 ID 删除
await api.remove({ ids: [284, 285] })

// 删 fail 节点
await api.remove({ failed: previousResults })

// 删所有未使用节点（使用中的 account_count > 0 会返回 fail）
await api.remove({ unused: true })
```

---

## 类型定义

```ts
interface ImportItem {
  url: string        // 原始 URL
  host: string       // 替换后的 host
  port: number       // 端口
  id: number         // 该节点对应的 ID
}

interface CheckResult {
  id: number         // 节点 ID
  exit_ip: string    // 出口 IP
  country: string    // 国家
  score: number      // 质量分数
  grade: string      // 等级 A/B/C/D/F
  items: {
    target: string       // 检测目标
    status: string       // pass / warn / fail
    http_status?: number // HTTP 状态码
    latency_ms: number   // 延迟（毫秒）
    message: string      // 说明
  }[]
}

interface DeleteResult {
  id: number         // 被删除的节点 ID
  success: boolean   // 是否成功
  message?: string   // 失败原因
}
```
