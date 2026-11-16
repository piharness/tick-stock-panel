// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useDailyIntegrity } from '@/lib/useSharedQueries'
import { DailyDataHealthNotice, DailyResultStaleNotice } from './DailyDataHealthNotice'

vi.mock('@/lib/useSharedQueries', () => ({ useDailyIntegrity: vi.fn() }))
let host: HTMLDivElement
let root: Root
let client: QueryClient
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient()
})
afterEach(async () => { await act(async () => root.unmount()); client.clear(); host.remove() })

function health(blocking: string[] = [], stock = 'new', etf = '0', entries: unknown[] = []) {
  vi.mocked(useDailyIntegrity).mockReturnValue({ data: {
    report: null, blocking_assets: blocking, data_revisions: { stock, etf },
    blocking_entries: entries, blocking_total: entries.length,
  } } as ReturnType<typeof useDailyIntegrity>)
}

it('已知异常可在其他页面看到处理入口', async () => {
  health(['stock'])
  await act(async () => root.render(<QueryClientProvider client={client}><MemoryRouter><DailyDataHealthNotice /></MemoryRouter></QueryClientProvider>))
  expect(host.textContent).toContain('股票日K存在未完成的修补或重算')
  expect(host.querySelector('a')?.getAttribute('href')).toBe('/data')
})

it('横幅要说清哪一天、什么毛病——请求侧已经不弹 toast 了', async () => {
  health(['index'], '0', '0', [{
    asset_type: 'index', day: '2026-11-13', kind: 'sparse',
    reason: '分区仅覆盖 8 只, 低于同族应有个数量级 449', needs_raw: true,
  }])
  await act(async () => root.render(<QueryClientProvider client={client}><MemoryRouter><DailyDataHealthNotice /></MemoryRouter></QueryClientProvider>))
  expect(host.textContent).toContain('2026-11-13')
  expect(host.textContent).toContain('分区仅覆盖 8 只')
  // 只有 asset 名单时读者不知道该修哪一天，那正是以前只能靠 toast 找线索的原因
  expect(host.textContent).not.toContain('等 1 条')
})

it('仅对应资产的数据修正使结果过期', async () => {
  health()
  const result = { daily_data_revisions: { stock: 'old', etf: '0' } }
  await act(async () => root.render(<DailyResultStaleNotice result={result} assetType="stock" />))
  expect(host.textContent).toContain('仍基于旧数据')
  await act(async () => root.render(<DailyResultStaleNotice result={result} assetType="etf" />))
  expect(host.textContent).toBe('')
})

it('基于修正后数据的新结果不显示过期提示', async () => {
  health()
  await act(async () => root.render(<DailyResultStaleNotice result={{ daily_data_revisions: { stock: 'new' } }} />))
  expect(host.textContent).toBe('')
})
