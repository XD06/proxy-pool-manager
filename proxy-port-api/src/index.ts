interface CheckItem {
  target: string
  status: string
  http_status?: number
  latency_ms: number
  message: string
}

export interface CheckResult {
  id: number
  exit_ip: string
  country: string
  score: number
  grade: string
  items: CheckItem[]
}

export interface ImportItem {
  url: string
  host: string
  port: number
  id: number
}

export interface DeleteResult {
  id: number
  success: boolean
  message?: string
}

export interface RemoveOptions {
  ids?: number[]
  failed?: CheckResult[]
  unused?: boolean
}

export class ProxyAdmin {
  private baseURL: string
  private token: string

  constructor(baseURL: string, token: string) {
    this.baseURL = baseURL.replace(/\/+$/, '')
    this.token = token
  }

  private headers() {
    return { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json', Accept: 'application/json, text/plain, */*' }
  }

  private async api<T>(method: string, path: string, body?: any, retries = 3): Promise<{ code: number; message: string; data: T }> {
    for (let i = 0; i < retries; i++) {
      try {
        const opts: RequestInit = { method, headers: this.headers() }
        if (body) opts.body = JSON.stringify(body)
        const res = await fetch(`${this.baseURL}${path}`, opts)
        return await res.json()
      } catch (e) {
        if (i === retries - 1) throw e
        await new Promise(r => setTimeout(r, 1000 * (i + 1)))
      }
    }
    throw new Error('unreachable')
  }

  private async listAll(): Promise<Map<string, { id: number; host: string; port: number }>> {
    const map = new Map<string, any>()
    let page = 1
    const pageSize = 200
    for (;;) {
      const res = await this.api<{ items: any[]; total: number; pages: number }>(
        'GET', `/api/v1/admin/proxies?page=${page}&page_size=${pageSize}&sort_by=id&sort_order=desc`,
      )
      for (const p of res.data.items) map.set(`${p.host}:${p.port}`, p)
      if (page >= res.data.pages) break
      page++
    }
    return map
  }

  /** 导入节点，返回 URL → ID 映射 */
  async import(
    urls: string | string[],
    replace?: { from: string; to: string },
  ): Promise<ImportItem[]> {
    const list = (Array.isArray(urls) ? urls : [urls]).map(u => {
      const p = new URL(u)
      let host = p.hostname
      if (replace && host === replace.from) host = replace.to
      return { url: u, host, port: Number(p.port) }
    })

    await this.api('POST', '/api/v1/admin/proxies/batch', {
      proxies: list.map(n => ({ protocol: 'http', host: n.host, port: n.port, username: '', password: '' })),
    })

    const proxyMap = await this.listAll()

    return list.map(n => {
      const p = proxyMap.get(`${n.host}:${n.port}`)
      return { url: n.url, host: n.host, port: n.port, id: p?.id ?? 0 }
    })
  }

  /** 按 ID 查询质量检测结果，逐个返回 */
  async *query(ids: number[], concurrency = 10): AsyncGenerator<CheckResult> {
    if (ids.length === 0) return
    const queue: CheckResult[] = []
    let completed = 0
    let idx = 0

    const run = async () => {
      while (idx < ids.length) {
        const id = ids[idx++]
        for (let retry = 0; retry < 3; retry++) {
          try {
            const res = await this.api<any>('POST', `/api/v1/admin/proxies/${id}/quality-check`)
            queue.push({
              id: res.data.proxy_id,
              exit_ip: res.data.exit_ip || '',
              country: res.data.country || '',
              score: res.data.score,
              grade: res.data.grade,
              items: (res.data.items || []).map((i: any) => ({
                target: i.target,
                status: i.status,
                http_status: i.http_status,
                latency_ms: i.latency_ms,
                message: i.message,
              })),
            })
            break
          } catch {
            if (retry === 2) queue.push({ id, exit_ip: '-', country: '-', score: 0, grade: 'ERR', items: [] })
            else await new Promise(r => setTimeout(r, 1000))
          }
        }
        completed++
      }
    }

    const workers = Array.from({ length: Math.min(concurrency, ids.length) }, () => run())

    while (completed < ids.length) {
      if (queue.length > 0) {
        yield queue.shift()!
      } else {
        await new Promise(r => setTimeout(r, 20))
      }
    }
    while (queue.length > 0) yield queue.shift()!
    await Promise.all(workers)
  }

  /** 对上次查询中 status= fail 的节点重新检测 */
  async *retry(prev: CheckResult[], concurrency = 10): AsyncGenerator<CheckResult> {
    const failed = prev.filter(r => r.items.some(i => i.status === 'fail')).map(r => r.id)
    if (failed.length === 0) return
    yield* this.query(failed, concurrency)
  }

  /** 删除节点：按 ID / 删 fail 节点 / 删全部未使用 */
  async remove(opts: RemoveOptions, concurrency = 5): Promise<DeleteResult[]> {
    let ids: number[]

    if (opts.ids) {
      ids = opts.ids
    } else if (opts.failed) {
      ids = opts.failed.filter(r => r.items.some(i => i.status === 'fail')).map(r => r.id)
    } else if (opts.unused) {
      const proxyMap = await this.listAll()
      ids = [...proxyMap.values()].filter((p: any) => p.account_count === 0).map((p: any) => p.id)
    } else {
      return []
    }

    if (ids.length === 0) return []

    const results: DeleteResult[] = []
    let idx = 0

    await Promise.all(Array.from({ length: Math.min(concurrency, ids.length) }, async () => {
      while (idx < ids.length) {
        const id = ids[idx++]
        for (let retry = 0; retry < 3; retry++) {
          try {
            const res = await this.api<{ message: string }>('DELETE', `/api/v1/admin/proxies/${id}`)
            results.push({ id, success: res.code === 0, message: res.code === 0 ? undefined : res.message })
            break
          } catch {
            if (retry === 2) results.push({ id, success: false, message: 'network error' })
            else await new Promise(r => setTimeout(r, 1000))
          }
        }
      }
    }))

    return results
  }
}
