// @vitest-environment jsdom
// 结构回归：机会雷达（/handbook，原手册的「执行」章独立成页）。
//
// 它与图书馆（nav.test.tsx）的契约对称：
//   - 雷达必须回答「今天有没有可下注的」——一个数字 + 候选卡 + 账户指令；
//   - 反例登记与扫描器必须同屏（不能相加、不能互相替代）——这是合并「仓位艺术」
//     时定下的原则，拆页不能把它拆丢；
//   - 参考性内容（明细/账本/现金腿/四步）默认折叠，但常驻 DOM 可检索；
//   - 理论锚点必须指向图书馆（#rules / #sizing-audit / #replay-day）。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Daily } from './Daily'

const art = vi.hoisted(() => ({
  available: true,
  as_of: '2026-11-20',
  definition: {
    index: '全市场等权', state: 'down / flat / up', rule: 'state(t-1)==down 且持续 ≥10 日 → 空仓',
    confirm_days: 10, cost_one_way_bp: 20, lag: 't-1',
  },
  today: {
    date: '2026-11-20', state: 'down', state_age: 14, ret60_pct: -19.51,
    advice: '空仓', stance: 0, since: '2026-11-16', days_in_stance: 5,
    trigger: '下行段结束（首个非 down 日）即回场',
  },
  chosen: { cagr_pct: 2.25, mdd_pct: -44.44, sharpe: 0.21, exposure_pct: 89.5, switches: 26 },
  benchmark: { cagr_pct: 5.31, mdd_pct: -42.71, sharpe: 0.34 },
  stress: { cost_bp: 40, cagr_pct: 1.76, mdd_pct: -45.33 },
  rules: [],
  halves: [],
  placebo: { available: true, blocks: 13, flat_days: 278, p_value: 0.77 },
  checks: [
    { id: 'AC3', name: '规则胜基准', want: '两段均胜', got: '两段都输', pass: false },
  ],
  equity: [],
  flat_stats: { flat_days: 278, flat_mean_bp: 11.1, in_mean_bp: 2.5 },
  neff: { year: '2026', down_overlap: 45, flat_overlap: 121, down_nonoverlap: 2, flat_nonoverlap: 6 },
  warning: '',
}))

const payload = vi.hoisted(() => ({
  meta: {
    asof: '2026-11-20', generated_at: '2026-11-20 17:30:00', mode: 'record-only', note: '',
    dd_convention: '行业净值距 250 日高点', vol_convention: '20 日已实现波动的 500 日分位',
    store_last_session: '2026-11-20',
    thresholds: { low: -0.35, deep: -0.5, vol_hi: 0.66, min_amt20: 3e7, cooldown: 5, hold: 20 },
  },
  market: { state: 'down', hs300_close: 3890.5, ma60: 3900, vol_pct: 0.7, trend_up: false, f3_active: true },
  counts: { industries: 30, in_band: 12, signal: 5, candidate: 2, deep: 1, clusters_today: 1, note: '' },
  industries: [
    {
      industry: '电子', dd: -0.382, vol_pct: 0.93, stale_days: 0, n_etf: 42,
      best_symbol: '159363', best_name: '创业板人工智能ETF华宝', amt20: 9.6e8,
      r1: true, r2: true, r3: true, f6: true, status: 'CANDIDATE', reasons: [],
    },
    {
      industry: '军工', dd: -0.46, vol_pct: 0.66, stale_days: 0, n_etf: 21,
      best_symbol: '512710', best_name: '军工龙头ETF富国', amt20: 3.7e8,
      r1: true, r2: true, r3: true, f6: true, status: 'CANDIDATE', reasons: [],
    },
    {
      industry: '食品饮料', dd: -0.2, vol_pct: 0.4, stale_days: null, n_etf: 15,
      best_symbol: '515170', best_name: '食品饮料ETF', amt20: 2e8,
      r1: false, r2: false, r3: true, f6: false, status: 'NEUTRAL', reasons: ['未到位置'],
    },
  ],
  ledger: {
    events: [
      {
        type: 'event' as const, id: 'e1', date: '2026-11-20', industry: '电子', dd: -0.382,
        vol_pct: 0.93, state: 'down', signal: true, qualified: true, r3: true, f6: true,
        best_symbol: '159363', best_name: '创业板人工智能ETF华宝',
      },
    ],
    settles: [],
    v1: { rule: '', counting: '', left_tail_mapping: '', required: 3, state: 'WAITING', progress: '0/3' },
    low_vol_band_entries: 0,
  },
  data: {
    etf: { base: 'kline_etf_daily', base_last: '2026-11-20', part_days_added: [], last: '2026-11-20', rows: 1 },
    index: { base: 'index_daily_full', base_last: '2026-11-20', part_days_added: [], last: '2026-11-20' },
    lag_sessions: 0,
  },
}))

let throwWatch: boolean
vi.mock('@/lib/api', () => ({
  api: {
    etfWatch: async () => {
      if (throwWatch) throw new Error('404 Not Found')
      return payload
    },
    etfWatchAccount: async () => ({
      meta: {
        generated_at: '2026-11-20 17:31', policy: '', engine: '', adoption: '2026-11-20', init: 600000,
        defensive: { symbol: '511010', name: '国债ETF', method: '' }, rules: {},
      },
      asof: '2026-11-20',
      state: {
        equity: 611801, cash: 0,
        defensive: { value: 499700, weight: 0.8166 },
        leg1: { value: 112101, positions: [{ industry: '电子', symbol: '588170.SH', entry: '2026-11-20', exit: null, value: 112101 }] },
        trades_closed: 0,
      },
      instructions: [],
      equity_tail: [],
    }),
    watchlistGroups: async () => ({ groups: [{ id: 'g-pool', name: '观察池', color: 'sky' }] }),
    watchlistGroupCreate: async () => { throw new Error('should not create when pool exists') },
    etfWatchCashLeg: async () => { throw new Error('404 Not Found') },
    etfWatchReplay: async () => { throw new Error('404 Not Found') },
    positionArt: async () => art,
    positionArtTail: async () => { throw new Error('404 Not Found') },
  },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  window.history.replaceState(null, '', '/handbook')
  throwWatch = false
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

async function render() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={['/handbook']}>
          <Daily />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  return host
}

it('接口未就绪时给命令与占位数字，不空白也不报错', async () => {
  throwWatch = true
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('还没有盘后扫描结果')
  expect(text).toContain('python analysis/etf_watch.py')
  // 空态必须顺手说清「不要回测」——否则读者会用回测去填这 3 个坑
  expect(text).toContain('不要回测')
  expect(text).toContain('账户指令')
})

// 这两条从 StructuralEdge.test.tsx 搬来（2026-11-27）：拆页后判定表与账本口径归雷达，
// 那边只剩快照，断言跟着组件走，否则守的是一个已经不渲染这些东西的页面。
it('判定表把被拦的理由原样带出，而不是只留一个状态标签', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('未到位置')
  expect(text).toContain('理由')
})

it('账本口径写明「数簇不数行」，折叠收起时也在 DOM 里', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('数簇不数行')
  expect(text).toContain('同月多行业记为 1 簇')
})

it('有数据时：日期 + 判定数 + 候选卡 + 三项微指标', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('11 月 20 日')
  expect(text).toContain('个行业四条全过')
  expect(text).toContain('候选行业')
  // 候选卡按回撤深度排：更深的军工在前
  const cards = Array.from(el.querySelectorAll('section')).find(s => s.textContent?.includes('候选行业'))
  expect(cards?.textContent).toContain('军工')
  expect(cards?.textContent).toContain('创业板人工智能ETF华宝')
  // 交互与金额：加自选默认指向「观察池」分组、六格金额占位（账户未就绪时显示 —）
  const addBtn = cards?.querySelector('[aria-label*="加入自选观察池"]') as HTMLButtonElement | null
  expect(addBtn).not.toBeNull()
  expect(addBtn!.title).toContain('观察池')
  expect(text).toContain('一格(权益/6)')
  // 段首/延续分组：电子今天触发（段首），军工是延续 —— 两组都要标出来
  expect(text).toContain('段首 · 今天首次进入区间')
  expect(text).toContain('延续 · 已在区间内')
  expect(text).toContain('账本内首次触发')
  // 「明天要干什么」必须一眼可答：hero 结论句 + 明日待办清单 + 触发预案
  expect(text).toContain('段首 1 个 —— 明日开盘按限价买入')
  expect(text).toContain('明日待办')
  expect(text).toContain('明日无操作')
  expect(text).toContain('触发预案')
  expect(text).toContain('今天已有段首 1 个')
  expect(text).toContain('区间内')
  expect(text).toContain('12/30')
})

it('反例登记与账户指令同屏，且「不能相加」的原则没有在拆页时丢掉', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('反例登记')
  expect(text).toContain('不构成指令')
  expect(text).toContain('它今天说')
  expect(text).toContain('不能相加')
})

it('参考区默认折叠但常驻 DOM；折叠标题齐全', async () => {
  const el = await render()
  const folds = Array.from(el.querySelectorAll('details')) as HTMLDetailsElement[]
  expect(folds.length).toBeGreaterThanOrEqual(4)
  for (const d of folds) expect(d.open).toBe(false)
  const text = el.textContent ?? ''
  for (const s of ['判定明细', '事件账本', '现金腿', '如果今天触发']) {
    expect(text).toContain(s)
  }
})

it('理论锚点指向图书馆而不是本页（拆分后规则/验收/回放都在 /library）', async () => {
  const el = await render()
  const links = Array.from(el.querySelectorAll('a')).map(a => a.getAttribute('href'))
  expect(links).toContain('/library')
  expect(links).toContain('/library#rules')
  expect(links).toContain('/library#sizing-audit')
  expect(links).toContain('/library#replay-day')
})
