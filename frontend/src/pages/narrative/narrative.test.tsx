// @vitest-environment jsdom
// 叙事账本（/narrative）—— 只测对读者有约束的事：
// 1. 首屏结论与五个卷锚点都在（删掉一卷时测试要红）；
// 2. 冻结表行数与 data.ts 一致（改数不会让页面悄悄少一行）；
// 3. 关键读数必须在页面上：−17.3（剂量跨度）/ −33.3（配对）/ 1.10（波动比）/ +291bp（活口在 /chip）；
// 4. 实时块：数据到了要显示 as_of；某块 ok=false 时要写「不可用」而不是白屏或顶替上一日。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { Narrative } from './Narrative'
import { QK } from '@/lib/queryKeys'
import { DOSE, KILL, LAW, LEGS, PAIR, TIERS } from './data'

const state = vi.hoisted(() => ({ payload: null as unknown }))
const ledger = vi.hoisted(() => ({
  as_of: '2026-11-27',
  blocks: {
    temp: {
      ok: true, as_of: '2026-11-27', phase: 'repair', state: 'strong', score: 86,
      limit_up: 428, limit_down: 9, broken_limit: 31, seal_rate: 0.8664, max_consecutive: 6,
      ladder_completeness: 0.5, up_count: 5204, down_count: 168, total_amount_yi: 20065,
      first_board: 302, ge2_count: 126, promo_rate: 0.41,
      pct: { limit_up: 100, seal_rate: 98.4, total_amount: 36, score: 94.4, max_consecutive: 80.8 },
      sample_days: 250,
    },
    mainline: {
      ok: true, as_of: '2026-11-27',
      rows: [
        { rank: 1, member: '共封装光学(CPO)', limit_up_count: 66, ge2_count: 4, max_boards: 6, rungs_filled: 2, score: 97.3, leader_symbol: '603960.SH', leader_name: '克来机电', r5_pct: 13.23, r20_pct: 1.55, amt_share_pct: 1.15 },
        { rank: 2, member: '算力租赁', limit_up_count: 46, ge2_count: 5, max_boards: 3, rungs_filled: 2, score: 94.3, leader_symbol: '002197.SZ', leader_name: '证通电子', r5_pct: 8.57, r20_pct: 2.98, amt_share_pct: 0.33 },
      ],
    },
    flows: {
      ok: true,
      margin: { ok: true, as_of: '2026-11-19', balance_yi: 21927, pct_float: 2.52, pct_float_rank: 8.4, net5_yi: -811 },
      dragon: { ok: true, as_of: '2026-11-25', days: 5, net_yi: 25.42, stocks: 330 },
      block: { ok: true, as_of: '2026-11-25', amt5_yi: 72.6, neg_share: 50.2, neg_med: -6.93, deals: 276 },
      etf: { ok: true, as_of: '2026-11-27', share_pct: 24.03, amt_yi: 6380, market_amt_yi: 26550 },
    },
    index: {
      ok: true, as_of: '2026-11-27',
      rows: [
        { symbol: '000001.SH', name: '上证指数', close: 3606.49, ret1_pct: 3.73, vol20_pct: 19.7, vol_rank: 53.5, on5_pct: -0.23, id5_pct: 2.29, window_days: 8772 },
        { symbol: '000905.SH', name: '中证500', close: 6313.63, ret1_pct: 7.68, vol20_pct: 43.8, vol_rank: 91, on5_pct: 0.82, id5_pct: 4.27, window_days: 5318 },
      ],
      etf_rows: [
        { symbol: '510300.SH', name: '沪深300ETF', index_symbol: '000300.SH', close: 4.162, ret1_pct: 5.13, vol20_pct: 22.8, vol_rank: 84.2, r5_pct: 3.58, track5_pp: 0.84, amt_yi: 123.7, window_days: 129 },
        { symbol: '512100.SH', name: '中证1000ETF', index_symbol: '000852.SH', close: 2.464, ret1_pct: 9.56, vol20_pct: 52.6, vol_rank: 100, r5_pct: 7.13, track5_pp: 1.21, amt_yi: 138.2, window_days: 129 },
      ],
    },
    words: {
      ok: true, as_of: '2026-11-25', days: 5,
      rows: [{ word: '人形机器人', n: 17, net_yi: 4.74 }, { word: '算力', n: 7, net_yi: 4.01 }],
    },
  },
  disclaimer: '本页是读数，不是信号。',
}))

// 只换 api，保留真实的标签常量（阶段/状态的文案不在这里再抄一份）。
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { ...actual.api, narrativeLedger: async () => state.payload } }
})

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(async () => {
  state.payload = ledger
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // 直接种缓存：这里测的是「数据到了怎么渲染」，异步链路由 react-query 自己保证。
  client.setQueryData(QK.narrativeLedger, ledger)
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <Narrative />
      </QueryClientProvider>,
    )
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0))
  })
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  client.clear()
})

it('首屏：结论带 + 四个摘要 + 可执行动作 0', () => {
  const text = host.textContent ?? ''
  expect(text).toContain('一张收据，不是一个理由')
  expect(text).toContain('三条腿 · 零个信号')
  expect(text).toContain('叙事不是账单，是收据')
  expect(text).toContain('当前可执行动作')
  expect(text).toContain('−17bp')
  expect(text).toContain('1.10 vs 1.10')
  expect(text).toContain('B · 单 regime')
})

it('五卷锚点齐全（删一卷要红）', () => {
  for (const id of ['reframe', 'law', 'legs', 'dose', 'pair', 'bounds', 'live', 'mainline', 'flows', 'index', 'words', 'judge', 'kill', 'scope']) {
    expect(host.querySelectorAll(`#${id}`).length).toBe(1)
  }
})

it('冻结表行数与 data.ts 一致', () => {
  expect(host.querySelectorAll('#law tbody tr').length).toBe(LAW.length)
  expect(host.querySelectorAll('#legs tbody tr').length).toBe(LEGS.length)
  expect(host.querySelectorAll('#dose tbody tr').length).toBe(DOSE.length)
  expect((host.querySelector('#pair table') as HTMLElement).querySelectorAll('tbody tr').length).toBe(PAIR.length)
  expect(host.querySelectorAll('#kill tbody tr').length).toBe(KILL.length)
  const tierTable = host.querySelectorAll('#pair table')[1]
  expect(tierTable?.querySelectorAll('tbody tr').length).toBe(TIERS.length)
})

it('关键读数在页面上：剂量跨度 / 配对差 / 活口不在本页', () => {
  const dose = host.querySelector('#dose')?.textContent ?? ''
  expect(dose).toContain('−17.3')
  expect(dose).toContain('39.9')
  expect(dose).toContain('22.6')
  const pair = host.querySelector('#pair')?.textContent ?? ''
  expect(pair).toContain('−33.3')
  expect(pair).toContain('−41.2')
  const legs = host.querySelector('#legs')?.textContent ?? ''
  expect(legs).toContain('+291bp')
})

it('实时块：温度 / 主线 / 资金 / 指数 / 词频都按 as_of 显示', () => {
  const text = host.textContent ?? ''
  // 温度
  expect(text).toContain('428')
  expect(text).toContain('86.6%')
  expect(text).toContain('5,204')
  // 主线
  expect(text).toContain('共封装光学(CPO)')
  expect(text).toContain('克来机电')
  expect(text).toContain('+13.23%')
  // 资金四路各自的 as_of（不能并成一天）
  expect(text).toContain('2026-11-19')
  expect(text).toContain('2026-11-25')
  expect(text).toContain('24.03%')
  // 指数
  expect(text).toContain('中证500')
  expect(text).toContain('+7.68%')
  // 宽基 ETF：波动分位与 5 日跟踪偏离
  expect(text).toContain('沪深300ETF')
  expect(text).toContain('+0.84pp')
  // 词频
  expect(text).toContain('人形机器人')
  // 页脚把每块 as_of 再列一遍
  expect(text).toContain('温度 2026-11-27')
  expect(text).toContain('词频 2026-11-25')
})

it('分块降级：某块失败时写「不可用」，不静默顶替', async () => {
  state.payload = {
    ...ledger,
    blocks: {
      ...ledger.blocks,
      flows: { ok: false, error: 'margin 缺列' },
      words: { ok: false, error: '无龙虎榜缓存' },
    },
  }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(QK.narrativeLedger, {
    ...ledger,
    blocks: {
      ...ledger.blocks,
      flows: { ok: false, error: 'margin 缺列' },
      words: { ok: false, error: '无龙虎榜缓存' },
    },
  })
  const host2 = document.createElement('div')
  document.body.appendChild(host2)
  const root2 = createRoot(host2)
  await act(async () => {
    root2.render(
      <QueryClientProvider client={qc}>
        <Narrative />
      </QueryClientProvider>,
    )
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0))
  })
  const text = host2.textContent ?? ''
  expect(text).toContain('资金块不可用')
  expect(text).toContain('词频块不可用')
  // 失败块不能拿别的块顶替：页脚仍写「缺」
  expect(text).toContain('缺')
  act(() => root2.unmount())
  host2.remove()
})
