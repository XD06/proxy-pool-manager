import { ProxyAdmin } from './index.js'

const api = new ProxyAdmin(
  'http://127.0.0.1:8081',
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoxLCJlbWFpbCI6ImFkbWluQHN1YjJhcGkubG9jYWwiLCJyb2xlIjoiYWRtaW4iLCJ0b2tlbl92ZXJzaW9uIjo1MTQ0NjQyNTM3NzM4NTcxNzM2LCJleHAiOjE3ODA3MTczNTgsIm5iZiI6MTc4MDYzMDk1OCwiaWF0IjoxNzgwNjMwOTU4fQ.UPnn1Riq2U5VICIV8yzEfUPY-Bnfiz8iN5KBBA_Zy3c',
)

// 1. 按 ID 删除
console.log('1. 按 ID 删除')
const r1 = await api.remove({ ids: [999] })
console.log(JSON.stringify(r1), '\n')

// 2. 删 fail 节点
console.log('2. 删 fail 节点（先查一批，再删其中 fail 的）')
const items = await api.import(
  ['http://127.0.0.1:8001', 'http://127.0.0.1:8002'],
  { from: '127.0.0.1', to: '172.17.0.1' },
)
const results: any[] = []
for await (const r of api.query(items.map(i => i.id), 5)) results.push(r)
const r2 = await api.remove({ failed: results })
console.log(JSON.stringify(r2), '\n')

// 3. 删全部未使用（只展示会删多少，不实际执行）
console.log('3. 统计全部未使用节点')
const proxyMap = await (await fetch('http://127.0.0.1:8081/api/v1/admin/proxies?page=1&page_size=500&sort_by=id&sort_order=desc', {
  headers: { Authorization: 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoxLCJlbWFpbCI6ImFkbWluQHN1YjJhcGkubG9jYWwiLCJyb2xlIjoiYWRtaW4iLCJ0b2tlbl92ZXJzaW9uIjo1MTQ0NjQyNTM3NzM4NTcxNzM2LCJleHAiOjE3ODA3MTczNTgsIm5iZiI6MTc4MDYzMDk1OCwiaWF0IjoxNzgwNjMwOTU4fQ.UPnn1Riq2U5VICIV8yzEfUPY-Bnfiz8iN5KBBA_Zy3c' },
})).json()
const all = proxyMap.data.items
const unused = all.filter((p: any) => p.account_count === 0)
const used = all.filter((p: any) => p.account_count > 0)
console.log(`总共 ${all.length} 个, 未使用 ${unused.length} 个, 使用中 ${used.length} 个`)
console.log('使用中的 ID:', used.map((p: any) => p.id))
