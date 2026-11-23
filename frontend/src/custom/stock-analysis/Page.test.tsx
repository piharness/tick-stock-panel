// @vitest-environment jsdom
/**
 * 个股分析页 —— 时间轴部分的硬承诺。
 *
 * 这一页原来只有一个窗口（最近 250 根）。加上"任意区间"之后，正确的行为
 * 不再是"图能画出来"，而是三件更容易做错的事：
 *   1. 从博弈链路带 ?symbol= 进来，必须落到那一只票，且按 URL 里的区间取数；
 *   2. 区间早于本地日K覆盖时，必须说清"是谁缺数据"（覆盖 vs 这只票），
 *      并且只给"整市场向前扩展"这一条路（单票回填会让历史分区只剩 1 个标的）；
 *   3. 简称变更只在有证据时出现，且必须是"两个日期之间的窗口"，不是某一天。
 *
 * 图表本身（ECharts/canvas）不在 jsdom 里跑：这里替换成探针，断言传进去的
 * rows / initialRange / vlines —— 图表的渲染由 AnalysisKChart 自己负责。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api, type KlineRow, type StockNameHistory, type StockLevels } from '@/lib/api'
import { saveStoredRange } from '@/lib/chartRange'

type ChartProps = {
  rows: KlineRow[]
  initialRange?: { start: string; end: string }
  vlines?: { date: string }[]
  ranges?: { label?: string }[]
  onHoverDate?: (d: string | null) => void
  onViewportChange?: (v: { start: number; end: number }) => void
}
const chartProps: { last: ChartProps | null } = { last: null }

// 图表的 ref 句柄也要能测（键盘平移/缩放/复位），所以 mock 用 forwardRef 透出 spy
// 图表的 ref 句柄已随键盘快捷键一起下线，mock 不再需要 forwardRef。
vi.mock('@/components/stock-analysis/AnalysisKChart', async () => {
  const React = await import('react')
  const AnalysisKChart = (p: ChartProps) => {
    chartProps.last = p
    return React.createElement('div', { 'data-testid': 'kchart' }, `${p.rows.length} bars`)
  }
  return { AnalysisKChart }
})

const { StockAnalysis } = await import('./Page')

const SYMBOL = '600340.SH'

function bar(date: string, close: number): KlineRow {
  return { date, open: close, high: close * 1.02, low: close * 0.98, close, volume: 1e5, amount: 1e8 }
}

const rows: KlineRow[] = [
  bar('2025-05-07', 2.48),
  bar('2025-12-02', 2.1),
  bar('2026-01-14', 1.9),
  bar('2026-09-29', 1.3),
  bar('2026-11-13', 1.08),
]

const levels: StockLevels = {
  levels: { sr: [], pivot: [], extreme: [], boll: [], keltner_s: [], keltner_m: [], keltner_l: [], atr_stop: [], gap: [], fib: [], round: [] } as never,
  close: 1.08,
  summary: '',
  symbol: SYMBOL,
  dates: ['2025-11-14', '2026-11-13'],
  series: {},
}

const nameHistory: StockNameHistory = {
  symbol: SYMBOL,
  observations: [],
  bands: [
    { name: '华夏幸福', first_seen: '2025-11-13', last_seen: '2025-12-02', n: 7 },
    { name: '*ST华幸', first_seen: '2026-01-14', last_seen: '2026-09-29', n: 9 },
  ],
  changes: [{ from: '华夏幸福', to: '*ST华幸', after: '2025-12-02', before: '2026-01-14', gap_days: 43 }],
  renamed: true,
  current: '*ST华幸',
  source: 'dragon_tiger_snapshots',
  snapshots: 243,
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  chartProps.last = null
  localStorage.clear()
  // 钉死区间，否则默认「1 年」会随今天漂移
  saveStoredRange(SYMBOL, { preset: 'CUSTOM', start: '2024-09-01', end: '2026-11-13' })

  vi.spyOn(api, 'klineDaily').mockResolvedValue({
    symbol: SYMBOL, name: '*ST华幸', rows, source: 'enriched',
  })
  // 请求起点早于本地覆盖时走的是「本地 + 上游前复权补齐」那条（见 Page 的 fillMode）
  vi.spyOn(api, 'stockAnalysisHistory').mockResolvedValue({
    symbol: SYMBOL, name: '*ST华幸', rows,
    segments: [
      { start: '2024-09-02', end: '2025-05-06', source: 'upstream', n: 159 },
      { start: '2025-05-07', end: '2026-11-13', source: 'local', n: 371 },
    ],
    coverage: {
      local_first: '2025-05-07', local_last: '2026-11-13',
      requested_start: '2024-09-01', requested_end: '2026-11-13',
    },
    upstream: { attempted: true, fetched_rows: 159, error: null },
  })
  vi.spyOn(api, 'stockAnalysisLevels').mockResolvedValue(levels)
  vi.spyOn(api, 'stockAnalysisNameHistory').mockResolvedValue(nameHistory)
  // 退市红线是新增的常驻 query：不 mock 会真的发请求，jsdom 里必失败并弹出
  // 「数据不可用」—— 那会把其它用例的 DOM 断言带偏。
  vi.spyOn(api, 'stockAnalysisRiskGuard').mockResolvedValue({
    symbol: SYMBOL, name: '*ST华幸', board: '主板', is_st: true, st_kind: '*ST',
    as_of: '2026-11-17',
    gates: [{
      id: 'par', label: '面值退市', rule: '连续 20 个交易日收盘价均低于 1.00 元',
      status: 'counting', value: 0.92, threshold: 1, distance_pct: -8,
      streak: 1, streak_limit: 20, detail: '已连续 1/20 个交易日低于面值',
    }],
    caveats: ['本面板只算规则距离，不给摘帽/退市概率'],
    counts: { verdict: '正在累计面值退市天数' },
  })
  vi.spyOn(api, 'dataStatus').mockResolvedValue({
    daily: { rows: 0, earliest_date: '2025-05-07', latest_date: '2026-11-13', symbols_covered: 5890, trading_days: 371 },
    enriched: { rows: 0, earliest_date: '2025-05-07', latest_date: '2026-11-13', symbols_covered: 5890, trading_days: 371 },
    storage: null,
  } as never)
  vi.spyOn(api, 'extendHistory').mockResolvedValue({ status: 'started', job_id: 'job-1' })

  host = document.createElement('div')
  document.body.appendChild(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
  client.clear()
  vi.restoreAllMocks()
})

async function render(url = `/stock-analysis?symbol=${SYMBOL}`) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[url]}>
        <QueryClientProvider client={client}>
          <StockAnalysis />
        </QueryClientProvider>
      </MemoryRouter>,
    )
  })
  for (let i = 0; i < 8; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
}

const text = () => host.textContent ?? ''

it('从 URL 带进来的 symbol 与区间：取数按这两端，不按"最近 250 根"', async () => {
  await render()
  expect(api.stockAnalysisHistory).toHaveBeenCalled()
  const call = vi.mocked(api.stockAnalysisHistory).mock.calls.at(-1)!
  expect(call[0]).toBe(SYMBOL)
  expect(call[1]).toBe('2024-09-01')
  expect(call[2]).toBe('2026-11-13')
  expect(chartProps.last?.initialRange).toEqual({ start: '2024-09-01', end: '2026-11-13' })
  // 名字来自接口（*ST华幸），不是 URL 里的旧名字
  expect(text()).toContain('*ST华幸')
})

it('区间早于本地覆盖：说清是"本地没铺到"，并标出上游补齐段来自哪里', async () => {
  await render()
  expect(text()).toContain('本地日K起于')
  expect(text()).toContain('2025-05-07')
  // 补齐段必须写明"上游前复权 / 未入库"，否则读者会以为这是库里的官方数据
  expect(text()).toContain('上游前复权')
  expect(text()).toContain('未入库')
  expect(text()).toContain('2024-09-02 ~ 2025-05-06')
  expect(text()).toContain('含上游段')
  expect(text()).toContain('改为入库（9 个月）')   // 2024-09-01 → 2025-05-07 共 248 天

  const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('改为入库'))!
  await act(async () => { btn.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  expect(api.extendHistory).toHaveBeenCalledWith(9, 'month')
})

it('上游补齐失败时不能假装有那段：明写只显示本地段', async () => {
  vi.mocked(api.stockAnalysisHistory).mockResolvedValue({
    symbol: SYMBOL, name: '*ST华幸', rows: rows.filter(r => r.date >= '2025-05-07'),
    segments: [{ start: '2025-05-07', end: '2026-11-13', source: 'local', n: 371 }],
    coverage: {
      local_first: '2025-05-07', local_last: '2026-11-13',
      requested_start: '2024-09-01', requested_end: '2026-11-13',
    },
    upstream: { attempted: true, error: 'ReadTimeout: timed out' },
  })
  await render()
  expect(text()).toContain('上游补齐失败')
  expect(text()).toContain('只显示本地段')
  expect(text()).not.toContain('含上游段')
})

it('简称变更只在有证据时出现，且写成两个日期之间的窗口', async () => {
  await render()
  expect(text()).toContain('简称轨迹')
  expect(text()).toContain('华夏幸福')
  expect(text()).toContain('改名发生在')
  expect(text()).toContain('2025-12-02')
  expect(text()).toContain('2026-01-14')
  expect(text()).toContain('43 天窗口')
  // 不能说成"某天改名"
  expect(text()).not.toContain('改名日：')
  // 图上标注：竖线落在"新简称首次见到"，底纹落在各简称的观测区间
  // （底纹不带文字 —— 名字在下面的简称轨迹里已经说过一次，图上再写会和 K 线抢位置）
  expect(chartProps.last?.vlines?.map(v => v.date)).toEqual(['2026-01-14'])
  expect(chartProps.last?.ranges).toEqual([
    { start: '2025-11-13', end: '2025-12-02', color: expect.any(String) },
    { start: '2026-01-14', end: '2026-09-29', color: expect.any(String) },
  ])
})

it('没有改名记录时不渲染简称轨迹（没有证据就不占屏幕）', async () => {
  vi.mocked(api.stockAnalysisNameHistory).mockResolvedValue({
    ...nameHistory, renamed: false, changes: [], bands: [nameHistory.bands[1]],
  })
  await render()
  expect(text()).not.toContain('简称轨迹')
})

it('区间统计同源：涨跌幅、回撤、距高点都从收盘序列算', async () => {
  await render()
  // 2.48 → 1.08 = -56.45%，峰值 2.48，期末距高点同样 -56.45%
  expect(text()).toContain('-56.45%')
  expect(text()).toContain('最大回撤')
  expect(text()).toContain('区间最高')
  expect(text()).toContain('2.53')      // 区间最高取 high（2.48×1.02），不是收盘
  expect(text()).toContain('区间最低')
  expect(text()).toContain('1.06')
})

it('切预设会重新取数：2 年 ≠ 自定义区间', async () => {
  await render()
  const before = vi.mocked(api.stockAnalysisHistory).mock.calls.length
  const twoYear = [...host.querySelectorAll('button')].find(b => b.textContent === '2年')!
  await act(async () => { twoYear.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  for (let i = 0; i < 4; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
  expect(vi.mocked(api.stockAnalysisHistory).mock.calls.length).toBeGreaterThan(before)
  const call = vi.mocked(api.stockAnalysisHistory).mock.calls.at(-1)!
  expect(call[2]).toBe(new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date()))
  // 区间记忆按标的持久化
  expect(localStorage.getItem(`stock-analysis:range:${SYMBOL}`)).toContain('2Y')
})

describe('缺口的两种长相不能混成一句「无数据」', () => {
  it('请求起点在覆盖内、首根仍然晚 → 说这只票，不说本地', async () => {
    // 起点必须落在本地覆盖之内（≥2025-05-07），否则判据会正确地判成"覆盖缺口"
    saveStoredRange(SYMBOL, { preset: 'CUSTOM', start: '2025-05-08', end: '2026-11-13' })
    vi.mocked(api.klineDaily).mockResolvedValue({
      symbol: SYMBOL, name: '*ST华幸', rows: [bar('2025-06-20', 2.4), ...rows], source: 'enriched',
    })
    await render()
    expect(text()).toContain('没有K线')
    expect(text()).toContain('停牌')
    expect(text()).not.toContain('向前补')
  })
})

// ===== 空 rows 不能把整页炸成「扩展暂时不可用」 =====

it('所选区间一根 K 都没有时：给空态，不能抛 TypeError 被边界吞掉', async () => {
  // 回归：`last.close` 在 rows=[] 时直接抛，ExtensionBoundary 接住后显示
  // 「扩展 official.stock-analysis 暂时不可用」—— 用户以为扩展坏了，其实是空数组。
  vi.mocked(api.stockAnalysisHistory).mockResolvedValue({
    symbol: SYMBOL, name: '*ST华幸', known: true, rows: [],
    segments: [],
    coverage: {
      local_first: '2024-08-12', local_last: '2026-11-13',
      requested_start: '2024-09-01', requested_end: '2026-11-13',
    },
    upstream: { attempted: false, fetched_rows: 0, error: null },
  })
  await render()
  expect(text()).toContain('所选区间没有日 K')
  expect(text()).toContain('换一段区间')
  expect(text()).not.toContain('没有这只票')
  // 关键：页面其余部分仍然在（标题/区间条/统计条不是渲染到一半就没了的）
  expect(text()).toContain('关键价位分析')
  expect(text()).toContain('1年')          // 预设按钮
  expect(text()).not.toContain('暂时不可用')
})

it('维表里没有这只票时，别让人反复换区间', async () => {
  vi.mocked(api.stockAnalysisHistory).mockResolvedValue({
    symbol: '999999.SH', name: undefined, known: false, rows: [],
    segments: [], upstream: { attempted: false, fetched_rows: 0, error: null },
  })
  await render()
  expect(text()).toContain('没有这只票')
  expect(text()).toContain('维表里查不到该代码')
})

it('用数字键盘连打 2024 0 9 0 1 能改区间（原生 date 控件在小键盘上输不进去）', async () => {
  await render()
  const start = host.querySelector('input[aria-label="区间起点"]') as HTMLInputElement
  expect(start).not.toBeNull()
  const nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!
  // 逐位输入，模拟用户按小键盘
  for (const step of ['2', '20', '202', '2024', '20240', '202409', '2024090', '20240901']) {
    await act(async () => {
      nativeSet.call(start, step)
      start.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }
  for (let i = 0; i < 4; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
  const call = vi.mocked(api.stockAnalysisHistory).mock.calls.at(-1)!
  expect(call[1]).toBe('2024-09-01')
})

// ===== 好用的三件小事：读数条、键盘、视窗统计 =====

it('光标读数条：没 hover 时显示最后一根，hover 后跟着走', async () => {
  await render()
  // 默认是最后一根
  expect(text()).toContain('2026-11-13（最新）')
  expect(text()).toContain('1.08')

  await act(async () => { chartProps.last?.onHoverDate?.('2026-01-14') })
  expect(text()).toContain('2026-01-14')
  expect(text()).not.toContain('（最新）')
  expect(text()).toContain('1.90')     // 那天的收盘
})

it('图区不再绑定键盘快捷键（把方向键还给页面滚动与输入框）', async () => {
  await render()
  // 负控：曾经有一个 tabIndex 包装层吃掉 ←/→/+/−/0，它必须真的不在了；
  // 只删提示文字而留着监听，同样会抢走方向键。
  expect(host.querySelector('[data-testid="chart-keyboard"]')).toBeNull()
  expect(text()).not.toContain('点一下图区后可用键盘')
  expect(text()).toContain('十字虚线跟随光标')
})

it('拖动缩放条后，统计条改口径为「当前视窗」而不是继续叫所选区间', async () => {
  await render()
  expect(text()).toContain('所选区间')
  expect(text()).toContain('-56.45%')   // 全区间（2.48 → 1.08）

  await act(async () => { chartProps.last?.onViewportChange?.({ start: 50, end: 100 }) })
  expect(text()).toContain('当前视窗')
  // 后半段：1.90 → 1.08 = -43.16%
  expect(text()).toContain('-43.16%')
})

it('换区间会把「当前视窗」清掉（旧百分比套到新数据上会算错段）', async () => {
  await render()
  await act(async () => { chartProps.last?.onViewportChange?.({ start: 50, end: 100 }) })
  expect(text()).toContain('当前视窗')

  const twoYear = [...host.querySelectorAll('button')].find(b => b.textContent === '2年')!
  await act(async () => { twoYear.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  for (let i = 0; i < 4; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  }
  expect(text()).toContain('所选区间')
  expect(text()).not.toContain('当前视窗')
})
