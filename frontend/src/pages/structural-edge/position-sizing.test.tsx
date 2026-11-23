// @vitest-environment jsdom
// 仓位择时验收：从原「仓位艺术」页迁移过来的三件事 ——
//   1. 今日读数、六种实现与验收结论必须同屏（否则「它不行」只是口号）；
//   2. 切换出场确认天数会重新取数（参数只有一处，法层与反例登记共用）；
//   3. 数据不可用时给空态，不空白、不报错页。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type PositionArtPayload, type PositionArtTail } from '@/lib/api'
import { StructuralEdge } from './index'

vi.mock('echarts', () => ({
  init: (dom: HTMLElement) => ({
    dom,
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
    isDisposed: () => false,
    getDom: () => dom,
    getZr: () => ({ on: vi.fn(), off: vi.fn() }),
  }),
}))

function payload(confirmDays: number, advice: '空仓' | '在场'): PositionArtPayload {
  return {
    available: true,
    as_of: '2026-11-20',
    definition: {
      index: '全市场等权',
      state: 'down / flat / up',
      rule: `state(t-1)==down 且持续 ≥${confirmDays} 日 → 空仓`,
      confirm_days: confirmDays,
      cost_one_way_bp: 20,
      lag: 't-1',
    },
    today: {
      date: '2026-11-20',
      state: 'down',
      state_age: 14,
      ret60_pct: -19.51,
      advice,
      stance: advice === '空仓' ? 0 : 1,
      since: '2026-11-16',
      days_in_stance: 5,
      trigger: '下行段结束（首个非 down 日）即回场',
    },
    chosen: { cagr_pct: 2.25, mdd_pct: -44.44, sharpe: 0.21, exposure_pct: 89.5, switches: 26 },
    benchmark: { cagr_pct: 5.31, mdd_pct: -42.71, sharpe: 0.34 },
    stress: { cost_bp: 40, cagr_pct: 1.76, mdd_pct: -45.33 },
    rules: [
      { confirm_days: 0, cagr_pct: 2.5, mdd_pct: -35.63, sharpe: 0.22, exposure_pct: 81.1, switches: 98, is_default: false },
      { confirm_days: 5, cagr_pct: 2.57, mdd_pct: -44.34, sharpe: 0.23, exposure_pct: 86.1, switches: 40, is_default: false },
      { confirm_days: 10, cagr_pct: 2.25, mdd_pct: -44.44, sharpe: 0.21, exposure_pct: 89.5, switches: 26, is_default: true },
    ],
    halves: [
      { label: '2016–2021H1', days: 1335, base_cagr_pct: 4.96, rule_cagr_pct: 1.5, base_mdd_pct: -42.71, rule_mdd_pct: -44.44 },
    ],
    placebo: { available: true, blocks: 13, flat_days: 278, p_value: 0.77 },
    checks: [
      { id: 'AC1', name: '状态标签稳定', want: '≤3%', got: '1.36%', pass: true },
      { id: 'AC3', name: '规则胜基准', want: '两段均胜', got: '两段都输', pass: false },
    ],
    equity: [
      { date: '2016-01-05', base: 1, rule: 1 },
      { date: '2026-11-20', base: 1.7548, rule: 1.2731 },
    ],
    flat_stats: { flat_days: 278, flat_mean_bp: 11.1, in_mean_bp: 2.5 },
    neff: { year: '2026', down_overlap: 45, flat_overlap: 121, down_nonoverlap: 2, flat_nonoverlap: 6 },
    warning: '独立事件只有 13 段空仓',
  }
}

const tail: PositionArtTail = {
  available: true,
  year: 2026,
  cut_pct: 20,
  horizon: 20,
  cells: [{ mq: 3, tq: 0, n: 46773, p_tail_pct: 1.3, med_pct: -1.34 }],
  by_state: [
    { mkt_state: 'down', n: 100, p_tail_pct: 2.2, med_pct: 3.65 },
    { mkt_state: 'flat', n: 100, p_tail_pct: 9.7, med_pct: -4.74 },
  ],
  best: { mq: 3, tq: 0, n: 46773, p_tail_pct: 1.3, med_pct: -1.34 },
  worst: { mq: 4, tq: 4, n: 36944, p_tail_pct: 22.7, med_pct: -6.67 },
  ratio: 17.5,
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  vi.spyOn(api, 'etfWatch').mockRejectedValue(new Error('404 Not Found'))
  vi.spyOn(api, 'etfWatchAccount').mockRejectedValue(new Error('404 Not Found'))
  vi.spyOn(api, 'etfWatchReplay').mockRejectedValue(new Error('404 Not Found'))
  vi.spyOn(api, 'positionArt').mockImplementation(async (n: number) => payload(n, n === 10 ? '空仓' : '在场'))
  vi.spyOn(api, 'positionArtTail').mockResolvedValue(tail)
  window.history.replaceState(null, '', '#evidence')
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '#')
})

async function settle() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 20))
  })
}

async function render() {
  await act(async () =>
    root.render(
      <QueryClientProvider client={client}>
        <StructuralEdge />
      </QueryClientProvider>,
    ),
  )
  await settle()
}

const text = () => host.textContent ?? ''
const button = (label: string) =>
  [...host.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent?.trim() === label)

it('证据章同屏给出六种实现、净值与验收结论', async () => {
  await render()
  expect(text()).toContain('仓位择时的验收')
  expect(text()).toContain('下行满 10 日离场')
  expect(text()).toContain('全程持有')
  expect(text()).toContain('未通过')
  expect(text()).toContain('状态标签稳定')
  expect(text()).toContain('独立事件只有 13 段空仓')
})

it('切换确认天数会重新取数，反例登记同步更新', async () => {
  await render()
  expect(api.positionArt).toHaveBeenCalledWith(10)
  await act(async () => button('5 日')!.click())
  await settle()
  expect(api.positionArt).toHaveBeenCalledWith(5)
  expect(text()).toContain('在场')
})

it('数据不可用时给空态而不是空白页', async () => {
  vi.spyOn(api, 'positionArt').mockResolvedValue({ available: false, reason: '日线分区不足: 120' })
  await render()
  expect(text()).toContain('日线分区不足')
})
