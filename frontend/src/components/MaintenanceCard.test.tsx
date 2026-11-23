// @vitest-environment jsdom
// 自动维护卡的三条契约：
//   1. 已最新 → 不启动任务（空任务会占单飞槽）；
//   2. 落后 → 自动跑一次，且同一「应收盘日」不重复（失败后也不自动重试）；
//   3. 有任务在跑 → 接管显示进度，按钮不可再点。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { MaintenanceCard } from './MaintenanceCard'
import { api, type MaintenanceStatus } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: { maintenance: vi.fn(), maintenanceRun: vi.fn(), pipelineJobCancel: vi.fn() },
}))

const base: MaintenanceStatus = {
  now: '2026-11-23T19:50',
  expected: '2026-11-23',
  phase: 'closed',
  stale: false,
  domains: [
    { id: 'kline', label: '行情数据', asof: '2026-11-23', stale: false, detail: '' },
    { id: 'handbook', label: '投资手册', asof: '2026-11-23', stale: false, detail: '' },
  ],
  job: null,
  last: null,
}
const stale: MaintenanceStatus = {
  ...base,
  stale: true,
  domains: [
    { ...base.domains[0], stale: true, asof: '2026-11-20' },
    { ...base.domains[1], stale: true, asof: '2026-11-20', detail: '落后：回放' },
  ],
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  sessionStorage.clear()
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

const render = async () => {
  await act(async () => {
    root.render(<QueryClientProvider client={client}><MaintenanceCard /></QueryClientProvider>)
  })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

it('已最新时不启动任务，按钮显示「已是最新」', async () => {
  vi.mocked(api.maintenance).mockResolvedValue(base)
  await render()
  expect(api.maintenanceRun).not.toHaveBeenCalled()
  expect(host.textContent).toContain('已是最新')
})

it('落后时自动跑一次，同一应收盘日不重复', async () => {
  vi.mocked(api.maintenance).mockResolvedValue(stale)
  vi.mocked(api.maintenanceRun).mockResolvedValue({ skipped: false, job_id: 'j1', status: stale })
  await render()
  expect(api.maintenanceRun).toHaveBeenCalledTimes(1)

  await act(async () => { await client.invalidateQueries() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  expect(api.maintenanceRun).toHaveBeenCalledTimes(1)
})

it('盘中的落后不自动跑（避免与实时行情抢数据源），留手动按钮', async () => {
  vi.mocked(api.maintenance).mockResolvedValue({ ...stale, phase: 'pre_close' })
  await render()
  expect(api.maintenanceRun).not.toHaveBeenCalled()
  expect(host.textContent).toContain('更新数据')
})

it('有任务在跑时接管进度，按钮不可点', async () => {
  vi.mocked(api.maintenance).mockResolvedValue({
    ...stale,
    job: { id: 'j9', status: 'running', stage: 'handbook', progress: 72, message: '单日回放', started_at: null },
  })
  await render()
  expect(host.textContent).toContain('更新中 72%')
  expect(host.textContent).toContain('投资手册')
  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('更新中'))
  expect(btn?.disabled).toBe(true)
  expect(api.maintenanceRun).not.toHaveBeenCalled()
})

it('上次失败且已无任务时，把原因留在卡片上', async () => {
  vi.mocked(api.maintenance).mockResolvedValue({
    ...stale,
    last: { id: 'j8', status: 'failed', error: '手册链失败（exit 1）：单日回放落后', finished_at: '2026-11-23T19:40' },
  })
  await render()
  expect(host.textContent).toContain('上次更新失败')
  expect(host.textContent).toContain('单日回放落后')
})
