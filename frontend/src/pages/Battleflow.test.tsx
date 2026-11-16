// @vitest-environment jsdom
/**
 * 博弈链路 — 只验证这一页的几条硬承诺：
 *   1. 单位：chg_pct 是百分数、bp 是基点、占比不带符号；走错口径会差 100 倍；
 *   2. 扫描层：买方席位必须带自己的 n 与 bp；单票历史不够时写「样本不足」，
 *      不能拿 n=16 的均值当结论；
 *   3. 榜单滞后 ≥2 个交易日的形状必须点名「不是发布节奏」——2026-11 那次
 *      月分区回归就是被写成「T 日盘后才公布的硬约束」，于是永远没人修；
 *   4. 分档口径只有一个定义点：一字板是「买不进」，不是「不接」；
 *   5. 没有数据时不能说成「今天没有机会」。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  api, type DragonTigerPayload, type OverviewMarket, type TableGameBoard,
  type TableGameOverview, type TableGameScan,
} from '@/lib/api'
import {
  Battleflow, isSignificant, pct, pctRaw, ratio, stabilityText, verdictBadge, verdictStyle, yi,
} from './Battleflow'

// ===== 纯函数：口径与边界 =====

describe('口径：百分数、比率、基点不能互换', () => {
  it('pctRaw 处理百分数制（scan.chg_pct = 10.08 已经是百分数）', () => {
    expect(pctRaw(10.08)).toBe('+10.08%')
    expect(pctRaw(-4.92)).toBe('-4.92%')
    expect(pctRaw(undefined)).toBe('—')
  })

  it('pct 处理小数制（change_pct = 0.1002）', () => {
    expect(pct(0.1002)).toBe('+10.02%')
    expect(pct(null)).toBe('—')
  })

  it('ratio 不带符号：占比与概率不是涨跌幅', () => {
    expect(ratio(59.8)).toBe('59.8%')
    // 同一批数字走错口径就是 100 倍差：59.8% vs +5980.00%
    expect(pct(59.8)).toBe('+5980.00%')
  })

  it('bp 与 亿：账本用基点，榜单净额用亿', () => {
    expect(pctRaw(41.6)).toBe('+41.60%')
    expect(yi(2.4)).toBe('+2.40 亿')
    expect(yi(-0.18)).toBe('-0.18 亿')
    expect(yi(null)).toBe('—')
  })

  it('|t| ≥ 2 才算显著——「方向为正」和「可当规则」是两件事', () => {
    expect(isSignificant(2)).toBe(true)
    expect(isSignificant(-2.01)).toBe(true)
    expect(isSignificant(1.95)).toBe(false)
    expect(isSignificant(null)).toBe(false)
  })
})

describe('分档措辞：买不进 ≠ 不接', () => {
  it('四态各有自己的标签，一字板不能被写成「不接」', () => {
    expect(verdictStyle('take').label).toBe('可接')
    expect(verdictStyle('avoid').label).toBe('不接')
    expect(verdictStyle('unbuyable').label).toBe('买不进')
    expect(verdictStyle('weak').label).toContain('不显著')
    expect(verdictStyle('unbuyable').action).toContain('物理上成交不了')
  })

  it('档内显著但样本外不稳时，徽标必须两个事实都说', () => {
    expect(verdictBadge('take', true)).toBe('可接')
    expect(verdictBadge('take', false)).toBe('可接（不稳）')
    expect(verdictBadge('take', null)).toBe('可接')
    expect(verdictBadge('unbuyable', true)).toBe('买不进')
  })

  it('样本外三态：只有前后半段都显著才叫稳定', () => {
    const base = { label: 'x', n: 10, bp: 1, t: 1, share_pct: 1 }
    expect(stabilityText({ ...base, stable: true })).toBe('前后半段都成立')
    expect(stabilityText({ ...base, stable: false })).toBe('后半段不再显著')
    expect(stabilityText(base)).toBe('窗口太短，切不动两段')
  })
})

// ===== 挂载 =====

let host: HTMLDivElement
let root: Root
let client: QueryClient

const overview = {
  as_of: '2026-11-13',
  quote_status: { market_phase: 'close_final' },
} as unknown as OverviewMarket

const buckets = [
  {
    label: '次日低开 < 0%', n: 7471, bp: 72.5, t: 10.87, share_pct: 59.8,
    verdict: 'take' as const, stable: true,
    halves: [
      { label: '前半段', n: 3482, bp: 67.8, t: 7.2 },
      { label: '后半段', n: 3989, bp: 76.5, t: 8.15 },
    ],
  },
  {
    label: '次日平开 0~3%', n: 3202, bp: 34.2, t: 3.46, share_pct: 25.6,
    verdict: 'take' as const, stable: false,
    halves: [
      { label: '前半段', n: 1615, bp: 27.1, t: 1.95 },
      { label: '后半段', n: 1587, bp: 41.4, t: 2.94 },
    ],
  },
  {
    label: '次日高开 3~9.8%', n: 1441, bp: -45.0, t: -2.8, share_pct: 11.5,
    verdict: 'avoid' as const, stable: false,
    halves: [
      { label: '前半段', n: 744, bp: -47.4, t: -2.2 },
      { label: '后半段', n: 697, bp: -42.4, t: -1.77 },
    ],
  },
  {
    label: '次日一字板 ≥ 9.8%（买不进）', n: 370, bp: -181.7, t: -6.4, share_pct: 3.0,
    verdict: 'unbuyable' as const, stable: true,
    halves: [
      { label: '前半段', n: 203, bp: -160.9, t: -4.29 },
      { label: '后半段', n: 167, bp: -206.9, t: -4.77 },
    ],
  },
]

const scan = {
  as_of: '2026-11-13',
  trade_date: '2026-11-13',
  state: 'ok',
  lag_days: 0,
  counts: { total: 46, positive: 23, shown: 2 },
  names: [
    {
      symbol: '002086.SZ', name: '东方海洋', chg_pct: 10.02, net_yi: 2.4,
      org_net_yi: -0.41, hot_net_yi: 0.38,
      buy_seats: [{ seat: '成都系', net_yi: 0.51, bp: 43.5, t: 1.62, n: 453 }],
      sell_seats: [{ seat: '温州帮', net_yi: -0.18, bp: 35.3, t: 2.02, n: 1239 }],
      // 只有 16 个历史事件：页面必须写「样本不足」，不能给 bp
      own: { n: 16, bp: 125.8, t: 1.31, enough: false },
    },
    {
      symbol: '600519.SH', name: '贵州茅台', chg_pct: -1.2, net_yi: 0.9,
      org_net_yi: 0.75, hot_net_yi: null,
      buy_seats: [], sell_seats: [],
      own: { n: 46, bp: 156.6, t: 1.78, enough: true },
    },
  ],
  gate: {
    buckets, median_gap_pct: -0.9, n_event: 12484, tradeable_bp: 48.4, window_days: 250,
  },
} as unknown as TableGameScan

const gameOverview = {
  as_of: '2026-11-13',
  window: { days: 250, start: '2025-11-12', end: '2026-11-12' },
  objects: [
    {
      id: 'national', name: '国家队', group: '公共资金',
      capital: '财政/央行流动性', horizon: '3-10 年',
      hard: '只在指数失稳时买；买完不能轻易卖',
      evidence: 'no_data',
      trace: '本仓库没有 ETF 份额、十大股东、汇金持仓三张表。',
      lie: '你会把龙虎榜的「机构专用」当成它。',
      stats: [],
    },
    {
      id: 'quant', name: '量化 / 程序化', group: '短线资金',
      capital: '产品化资金', horizon: '分钟到 1 日',
      hard: '策略容量上限；回撤触线降频',
      evidence: 'observed',
      trace: '龙虎榜里真实存在「量化抢筹」类席位昵称。',
      lie: '「量化砸盘」是最省事的归因。',
      stats: [{
        label: '量化席位净买后次日', bp: 67.8, t: 1.92, n: 296,
        note: '按席位昵称匹配（量化抢筹），不是机构分类',
      }],
    },
    {
      id: 'hot_money', name: '游资', group: '短线资金',
      capital: '自有 + 配资', horizon: '1-3 个交易日',
      hard: 'T+1 无法当日卖出；涨停板排队买不进',
      evidence: 'observed',
      trace: '本地唯一直接可观测的对手方。',
      lie: '游资会在涨停板对倒制造人气。',
      stats: [{ label: '净卖后次日', bp: 52.4, n: 4700, t: 6.05 }],
    },
  ],
  ledger: {
    n_event: 12484, excess_bp: 41.6, t: 8.04,
    control_bp: 0, control_n: 1_300_000,
    hold2_bp: 1.2, hold2_t: 0.4, hold2_n: 12484,
    tradeable_bp: 48.4, tradeable_t: 8.8, tradeable_n: 12114,
    median_gap_pct: -0.9,
    splits: [
      { label: '游资净买', n: 6332, bp: 38.1, t: 5.29 },
      { label: '游资净卖', n: 4700, bp: 52.4, t: 6.05 },
    ],
    gap_buckets: buckets,
    halves: [
      { label: '前半段', n: 6044, bp: 35.1, t: 4.85, from: '2025-11-12', to: '2026-05-15' },
      { label: '后半段', n: 6440, bp: 47.6, t: 6.46, from: '2026-05-18', to: '2026-11-12' },
    ],
    seats: [
      { seat: '欢乐海岸', n: 86, symbols: 64, bp: 167.7, t: 3.04, net_med_yi: -0.01 },
      { seat: '炒股养家', n: 344, symbols: 280, bp: -20.4, t: -0.73, net_med_yi: 0.12 },
    ],
    window_days: 250,
  },
  retail: { n: 5576, under_pct: 72, median_gap_pct: -2.36 },
  info_chain: [
    { at: 'T 日 15:00', layer: '收盘价', who: '所有人', lag: '实时', note: '收盘价是唯一定版数据。' },
    { at: 'T 日 盘后', layer: '龙虎榜', who: '游资', lag: '+30 分钟', note: '唯一能拿到具体是谁的数据。' },
  ],
  missing_tables: [
    { name: '十大流通股东', unlocks: '国家队 / 社保的真实持仓' },
    { name: '限售解禁与减持公告', unlocks: '产业资本的被迫卖压' },
  ],
} as unknown as TableGameOverview

const board = {
  symbol: '002086.SZ',
  state: 'ok',
  as_of: '2026-11-12',
  meta: {
    symbol: '002086.SZ', name: '东方海洋', close: 4.82,
    float_cap: 7.6e9, cap_rank: 66, roe: -5.45, turnover_rate: 38.7, amount: 2.9e9,
  },
  events: [
    {
      date: '2026-11-12', kind: 'limit_up', actor: 'hot_money', actor_label: '游资 / 打板资金',
      title: '涨停（第 1 板）', detail: '换手 35.1%', value: 0.1,
      seen_at: 'T 日 15:00（封板即可见）', evidence: 'observed',
    },
  ],
  rules: [
    {
      id: 'wash', label: '对敲 / 换手堆积', state: 'triggered',
      value: 98, unit: '换手率分位',
      detail: '近 5 日换手第 98 百分位；触发条件：换手 ≥80 且振幅 ≤25',
      claim: '对敲的痕迹不是大单，而是换手很高但振幅很小。',
    },
    {
      id: 'knife', label: '飞刀（连续跌停后接盘）', state: 'no_data',
      value: null, unit: '%（5 日均值）', detail: '空射程不落盘：没有样本时不显示 0%',
      claim: '跌多了就会反弹，是本页要打掉的第一个直觉。',
    },
  ],
} as unknown as TableGameBoard

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'overviewMarket').mockResolvedValue(overview)
  vi.spyOn(api, 'tableGameScan').mockResolvedValue(scan)
  vi.spyOn(api, 'tableGameOverview').mockResolvedValue(gameOverview)
  vi.spyOn(api, 'tableGameBoard').mockResolvedValue(board)
  // 新鲜度补偿会去拉榜单：默认给一个同日结果，避免测试里走真网络
  vi.spyOn(api, 'dragonTiger').mockResolvedValue({
    state: 'ok', requested_date: '2026-11-13', trade_date: '2026-11-13', all: { stock_items: [] },
  } as unknown as DragonTigerPayload)
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

async function render() {
  await act(async () => {
    root.render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <Battleflow />
        </QueryClientProvider>
      </MemoryRouter>,
    )
  })
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

const text = () => host.textContent ?? ''

it('首屏先回答「今天谁在对面」，而不是先让人选一只票', async () => {
  await render()
  expect(text()).toContain('博弈链路')
  expect(text()).toContain('今日可扫描')
  expect(text()).toContain('46')
  expect(text()).toContain('23')
  expect(text()).toContain('东方海洋')
  expect(text()).toContain('+10.02%')     // chg_pct 走百分数制
  expect(text()).toContain('+2.40 亿')
  expect(text()).not.toContain('NaN')
})

it('买方席位必须带自己的样本量与超额，不能只给个名字', async () => {
  await render()
  expect(text()).toContain('成都系')
  expect(text()).toContain('+43.5bp')
  expect(text()).toContain('n=453')
  expect(text()).toContain('t=1.62')
})

it('单票历史几乎永远不够用：把它写成一句话，而不是留一列永远写「样本不足」', async () => {
  await render()
  // 实测 2026-11-13 前 12 只里 0 只 n ≥ 20 —— 那一列恒等于同一个答案，是装饰。
  // 夹具里 2 只：一只 n=46 够用，一只 n=16 不够，覆盖度必须算出来。
  expect(text()).toContain('前 2 只里 1 只自身样本 ≥20（最大 n=46）')
  expect(text()).toContain('不能按单票历史')
  // 单票 bp 不再渲染（它本来就不该被读成结论）
  expect(text()).not.toContain('+125.8bp')
  expect(text()).not.toContain('+156.6bp')
})

it('没有具名游资席位时，要说清买方在「机构专用」这个混合桶里', async () => {
  await render()
  expect(text()).toContain('无具名游资席位')
  expect(text()).toContain('混合桶')
})

it('分档表：只有低开档可当规则，一字板写「买不进」而不是「不接」', async () => {
  await render()
  expect(text()).toContain('明早 09:25 的判定')
  expect(text()).toContain('可当规则用的只有')
  expect(text()).toContain('次日低开 < 0%')
  expect(text()).toContain('后半段不再显著')
  expect(text()).toContain('可接（不稳）')     // 平开档：overall 显著，样本外不稳
  expect(text()).toContain('买不进')
  expect(text()).toContain('-181.7bp')
  // 一字板的判定是 unbuyable，不是 avoid
  expect(text()).not.toContain('不接 · 物理上成交不了')
})

it('榜单滞后 ≥2 个交易日时必须说「不是发布节奏」——这句话在 2026-11 被写错过', async () => {
  vi.mocked(api.tableGameScan).mockResolvedValue({
    ...scan, state: 'stale', lag_days: 14, trade_date: '2026-10-30',
  } as TableGameScan)
  await render()
  expect(text()).toContain('2026-10-30')
  expect(text()).toContain('不是发布节奏')
  expect(text()).toContain('取数链路')
  // 旧文案把取数故障解释成制度约束，于是缺陷永远不被修
  expect(text()).not.toContain('硬约束')
})

it('滞后时先补拉一次榜单，再让扫描层重读（删掉六段不能顺手删掉新鲜度）', async () => {
  vi.mocked(api.tableGameScan).mockResolvedValue({
    ...scan, state: 'stale', lag_days: 1, trade_date: '2026-11-12',
  } as TableGameScan)
  await render()
  expect(api.dragonTiger).toHaveBeenCalled()
})

it('没有榜单数据时是「没有榜单可读」，不是「今天没有机会」', async () => {
  vi.mocked(api.tableGameScan).mockResolvedValue({
    ...scan, state: 'no_data', trade_date: null, lag_days: null, names: [],
    counts: { total: 0, positive: 0, shown: 0 },
  } as TableGameScan)
  await render()
  expect(text()).toContain('本地还没有任何龙虎榜数据')
  expect(text()).toContain('不是「今天没有机会」')
})

it('账本必须给出对照组自检；分档表不在账本里重复出现', async () => {
  await render()
  expect(text()).toContain('对照组自检')
  expect(text()).toContain('通过')
  expect(text()).toContain('+41.6bp')
  expect(text()).toContain('t=8.04')
  // 持两日几乎归零，和「拿住会更好」的直觉相反
  expect(text()).toContain('+1.2bp')
})

it('名册只保留有数字的对象；标了可观测却没数字的必须暴露出来', async () => {
  await render()
  expect(text()).toContain('量化 / 程序化')
  expect(text()).toContain('+67.8bp')
  expect(text()).toContain('t=1.92')
  // 昵称匹配是近似，口径必须写在页面上
  expect(text()).toContain('不是机构分类')
  expect(text()).toContain('游资')
})

it('看不见的部分默认折叠，但内容必须在（无数据 ≠ 不重要）', async () => {
  await render()
  expect(text()).toContain('看不见的部分')
  expect(text()).toContain('国家队')
  expect(text()).toContain('无数据')
  expect(text()).toContain('十大流通股东')
})

it('每个事件都必须写「你什么时候能看到」', async () => {
  await render()
  expect(text()).toContain('涨停（第 1 板）')
  expect(text()).toContain('T 日 15:00（封板即可见）')
})

it('规则无样本时给 no_data，不能渲染成 0', async () => {
  await render()
  expect(text()).toContain('飞刀（连续跌停后接盘）')
  expect(text()).toContain('本仓库看不到')
  expect(text()).toContain('空射程不落盘')
  expect(text()).not.toContain('0%（5 日均值）')
})

it('个股不在本地日线里时明说，不渲染成空图表', async () => {
  vi.mocked(api.tableGameBoard).mockResolvedValue({
    symbol: '999999.SH', state: 'not_in_universe', meta: {} as never, events: [], rules: [],
  } as unknown as TableGameBoard)
  await render()
  expect(text()).toContain('本地日线里没有这只票')
})

it('扫描层读取失败时，其余各节仍要能读，不能整页白屏', async () => {
  vi.mocked(api.tableGameScan).mockRejectedValue(new Error('scan down'))
  await render()
  expect(text()).toContain('扫描层读取失败')
  expect(text()).toContain('对照组自检')
  expect(text()).toContain('量化 / 程序化')
})

it('账本读取失败时说清哪些还能用，不能一直显示「读取中」', async () => {
  vi.mocked(api.tableGameOverview).mockRejectedValue(new Error('overview down'))
  await render()
  expect(text()).toContain('账本读取失败')
  expect(text()).toContain('扫描层与个股实例仍可读')
  // 扫描层不受账本影响
  expect(text()).toContain('成都系')
})

// ===== 去个股分析：两个动作必须分开 =====

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{`${loc.pathname}${loc.search}`}</div>
}

async function renderWithProbe() {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/battle']}>
        <QueryClientProvider client={client}>
          <LocationProbe />
          <Battleflow />
        </QueryClientProvider>
      </MemoryRouter>,
    )
  })
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

const loc = () => host.querySelector('[data-testid="loc"]')?.textContent ?? ''

it('点「分析」离开本页去看价格形态：symbol 与名字都要带过去', async () => {
  await renderWithProbe()
  const btn = host.querySelector('button[title*="价格形态"]') as HTMLButtonElement | null
  expect(btn).not.toBeNull()
  await act(async () => { btn!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  const href = loc()
  expect(href.startsWith('/stock-analysis?')).toBe(true)
  expect(href).toContain('symbol=002086.SZ')
  expect(href).toContain('name=%E4%B8%9C%E6%96%B9%E6%B5%B7%E6%B4%8B')  // 东方海洋
})

it('点行本体只切换个股实例，不跳页（选中 ≠ 导航）', async () => {
  await renderWithProbe()
  expect(loc()).toBe('/battle')
  const row = host.querySelector('[role="button"][tabindex="0"]') as HTMLElement | null
  expect(row).not.toBeNull()
  await act(async () => { row!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  expect(loc()).toBe('/battle')
  expect(api.tableGameBoard).toHaveBeenCalledWith('002086.SZ')
})

it('行内不能再嵌套 button —— 结构上保证「选中」与「跳转」是两件事', async () => {
  await renderWithProbe()
  expect(host.querySelectorAll('button button').length).toBe(0)
})
