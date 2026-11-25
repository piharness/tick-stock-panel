// @vitest-environment jsdom
/**
 * 短线雷达 —— 结构与口径契约。
 *
 * 这一页最容易改坏的不是样式，而是「隔离」与「口径」：
 *   1. 五个标签页与计数是否来自同一份 summary；
 *   2. 切到某个标签后，屏幕上是否只剩这一类的票（而不是在 5,576 行里加个高亮）；
 *   3. 观察池/可执行动作是否只出现在「全市场」这一屏；
 *   4. 白马没有历史读数时必须说「无历史背书」，不能显示成 0%；
 *   5. 换标签是否清掉上一屏的筛选（否则会读成「该标签没有票」）；
 *   6. 摘要里的「变化/风险」是否真的能筛明细（第四版新增的出口）；
 *   7. 漏斗每一步的删除量是否落在同一批票上（旧版把全市场阶段计数混进单步）；
 *   8. ?tag= 是否能直接打开某一屏（extension 的旧路径重定向一直在保留 query）。
 *
 * 这些断言都不看图，只看 DOM。样式回归由截图负责，语义回归由这里负责。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { api, type ShortlineRadarData, type ShortlineRadarRow } from '@/lib/api'

const { ShortlineRadar } = await import('./Page')

function row(p: Partial<ShortlineRadarRow> & Pick<ShortlineRadarRow, 'symbol' | 'arena' | 'tags'>): ShortlineRadarRow {
  return {
    name: p.symbol,
    level: 'active',
    stage: 'cold',
    industry: '测试行业',
    mcap: 1e10,
    turnover_rate: 1.5,
    amount: 1e8,
    turn_med: 1.2,
    turn_ratio: 1.25,
    n_days: 20,
    p_scale: 0.5,
    p_turn_med: 0.5,
    p_turnover: 0.5,
    p_amount: 0.5,
    p_amp: 0.5,
    amp20: 0.03,
    lu20: 0,
    heat: 0.5,
    vol60: 0.02,
    ret20: 0.01,
    lu_days: 0,
    roe: 0.1,
    revenue_yoy: 0.1,
    net_income_yoy: 0.1,
    gross_margin: 0.3,
    debt_to_asset_ratio: 0.4,
    report_period: '2026-09-30',
    prev_level: 'active',
    transition: 'stay',
    risk: 'none',
    risk_reason: null,
    ...p,
  }
}

const fixture: ShortlineRadarData = {
  as_of: '2026-11-20',
  prev_as_of: '2026-11-19',
  window_days: 20,
  stats_window: 60,
  summary: {
    arenas: [
      { arena: 'institution', label: '机构主场', hint: '大市值 × 低换手', count: 4, share: 4 / 6, dd20: 0.0272, med_ret20: -0.0076, levels: { core: 1, active: 3, background: 0, silent: 0 } },
      { arena: 'quant', label: '量化主场', hint: '大市值 × 高换手', count: 1, share: 1 / 6, dd20: 0.0498, med_ret20: -0.0155, levels: { core: 0, active: 1, background: 0, silent: 0 } },
      { arena: 'hotmoney', label: '游资主场', hint: '小市值 × 高换手', count: 1, share: 1 / 6, dd20: 0.0699, med_ret20: -0.0261, levels: { core: 0, active: 1, background: 0, silent: 0 } },
      { arena: 'nowhere', label: '无人区', hint: '小市值 × 低换手', count: 0, share: 0, dd20: 0.0310, med_ret20: -0.0089, levels: { core: 0, active: 0, background: 0, silent: 0 } },
    ],
    levels: [
      { level: 'core', label: '核心', hint: '', count: 1, share: 1 / 6 },
      { level: 'active', label: '活跃', hint: '', count: 5, share: 5 / 6 },
      { level: 'background', label: '背景', hint: '', count: 0, share: 0 },
      { level: 'silent', label: '沉默', hint: '', count: 0, share: 0 },
    ],
    tags: [
      { tag: 'bluechip', label: '蓝筹', hint: '20 日市值前 20% + 换手后 50%：大而稳', count: 2, share: 2 / 6, dd20: 0.0098, med_ret20: -0.0034, validated: true },
      { tag: 'whitehorse', label: '白马', hint: 'ROE≥12% 且营收净利同比为正', count: 1, share: 1 / 6, dd20: null, med_ret20: null, validated: false },
      { tag: 'leader', label: '龙头', hint: '行业市值前 20% 且涨幅前 20%', count: 2, share: 2 / 6, dd20: 0.0623, med_ret20: -0.0202, validated: true },
      { tag: 'yaogu', label: '妖股', hint: '60 日内 ≥3 个涨停', count: 2, share: 2 / 6, dd20: 0.0887, med_ret20: -0.0321, validated: true },
    ],
    stages: { cold: 4, warming: 0, hot: 2, fading: 0 },
    market: { dd20: 0.0404, med_ret20: -0.0112 },
    total: 6,
    transitions: { up: 1, down: 1, stay: 3, new: 1 },
    risk: { high: 1, watch: 1, none: 4 },
    risk_absent: 3,
    financial_period: '2026-09-30',
  },
  focus: {
    budget: { market: 6, struct: 4, cold: 2, picks: 0 },
    stages: { cold: 4, warming: 0, hot: 2, fading: 0 },
    heat_cut: 0.75,
    top_n: 15,
    picks: [],
    seat_backing: [],
    emotion: { available: false },
    excluded: { non_cold: 2, st: 0, bse: 0, thin: 0, young: 0, falling: 0, hot: 2, warming: 0, fading: 0 },
    lock: {
      criteria: {
        turn60_target: 0.96, turn60_tol: 0.35, ret20_target: -0.035, ret20_tol: 0.025,
        lock_ratio: 0.2, lock_max_capital: 8e8, min_amount60: 1.5e7,
        recognition_cut: 0.6, max_n: 10,
      },
      budget: { market: 6, hard: 4, matched: 2 },
      excluded: { st: 1, bse: 0, young: 0, moved: 1, thin: 0, band: 0 },
      picks: [
        {
          symbol: '603309.SH', name: '维力医疗', industry: '医疗器械', arena: 'nowhere',
          mcap: 2.96e9, turn60: 0.897, ret20: -0.0374, amount: 5.47e6, amt60: 2.87e7,
          lock_capital: 5.91e8, lock_days: 20.6, turn_q: 1, tr_ratio: 0.21,
          lu250: 0, lhb_days: 0,
          recognition: 0.19, high_recognition: false, in_pool: false,
          conflicts: ['热度分 0.24 < 0.75（换手项被本形态主动压低）', '当日成交额 547 万 < 3000 万'],
        },
        {
          symbol: '605108.SH', name: '同庆楼', industry: '酒店餐饮', arena: 'hotmoney',
          mcap: 2.82e9, turn60: 1.082, ret20: -0.0582, amount: 3.4e7, amt60: 3.43e7,
          lock_capital: 5.64e8, lock_days: 16.4, turn_q: 2, tr_ratio: 0.9,
          lu250: 3, lhb_days: 2,
          recognition: 0.64, high_recognition: true, in_pool: false,
          conflicts: ['热度分 0.40 < 0.75（换手项被本形态主动压低）'],
        },
      ],
      readout: {
        base_periods: 65, sample: '2021-04..2026-09',
        fwd20_ex: 0.011, fwd20_t: 1.45, ctrl_ex: 0.0049, diff_t: 0.94,
        fwd20_med: -0.0015, fwd20_p10: -0.0504,
        lu_rate: 0.036, ctrl_lu_rate: 0.114, lu_t: -5.28, lu_pos_periods: 2,
        script: 'analysis/_diag_lock_pattern.py',
      },
    },
  },
  rows: [
    row({ symbol: '600000.SH', name: '浦发银行', arena: 'institution', tags: ['bluechip', 'leader'], level: 'core', transition: 'up' }),
    row({ symbol: '300750.SZ', name: '宁德时代', arena: 'institution', tags: ['bluechip'] }),
    row({ symbol: '000001.SZ', name: '平安银行', arena: 'institution', tags: ['whitehorse'], transition: 'down', risk: 'watch', risk_reason: '流通市值 < 10 亿' }),
    row({ symbol: '002415.SZ', name: '海康威视', arena: 'institution', tags: ['leader'] }),
    row({ symbol: '603082.SH', name: '北自科技', arena: 'hotmoney', tags: ['yaogu'], stage: 'hot', transition: 'new' }),
    row({ symbol: '300123.SZ', name: '妖王电子', arena: 'quant', tags: ['yaogu'], stage: 'hot', risk: 'high', risk_reason: 'ST / *ST' }),
  ],
  disclaimer: '',
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'shortlineRadar').mockResolvedValue(fixture)
  vi.spyOn(api, 'watchlistList').mockResolvedValue({ symbols: [] })
  host = document.createElement('div')
  document.body.appendChild(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.restoreAllMocks()
})

async function render(path = '/shortline-radar') {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[path]}>
          <ShortlineRadar />
        </MemoryRouter>
      </QueryClientProvider>,
    )
  })
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  return host
}

function tab(el: HTMLElement, key: string): HTMLButtonElement {
  const node = el.querySelector(`#radar-tab-${key}`)
  if (!node) throw new Error(`tab ${key} 不存在`)
  return node as HTMLButtonElement
}

async function click(el: HTMLElement | null) {
  if (!el) throw new Error('目标节点不存在')
  await act(async () => {
    (el as HTMLElement).click()
    await new Promise(r => setTimeout(r, 0))
  })
}

async function switchTo(el: HTMLElement, key: string) {
  await click(tab(el, key))
}

const rowsWith = (el: HTMLElement, text: string) =>
  Array.from(el.querySelectorAll('tbody tr')).filter(tr => tr.textContent?.includes(text))

it('默认停在「全市场」：五个标签页带计数，状态条 / 观察池 / 可执行动作都在这一屏', async () => {
  const el = await render()
  const tabs = Array.from(el.querySelectorAll('[role="tab"]')) as HTMLButtonElement[]
  expect(tabs).toHaveLength(5)
  expect(tabs.map(t => t.id)).toEqual([
    'radar-tab-all', 'radar-tab-bluechip', 'radar-tab-whitehorse', 'radar-tab-leader', 'radar-tab-yaogu',
  ])
  expect(tab(el, 'all').textContent).toContain('全市场')
  expect(tab(el, 'all').getAttribute('aria-selected')).toBe('true')
  // 计数取自 summary：全市场 6、蓝筹 2、白马 1、龙头 2、妖股 2
  expect(tab(el, 'all').textContent).toContain('6')
  expect(tab(el, 'bluechip').textContent).toContain('2')
  expect(tab(el, 'whitehorse').textContent).toContain('1')

  expect(el.textContent).toContain('今日观察池')
  // 「执行窗口」不再是常驻卡片：压成一行「可执行动作只有两个」，完整规则在抽屉里
  expect(el.textContent).toContain('可执行动作只有两个')
  expect(el.textContent).not.toContain('执行窗口')
  expect(el.textContent).toContain('机构主场')
  // 变化与风险从摘要变成入口：这两个数字旧版在页面上根本没有出口
  expect(el.textContent).toContain('上移 1')
  expect(el.textContent).toContain('下移 1')
  expect(el.textContent).toContain('另有 3 只 ST 今日无成交')
})

it('全市场视图不显示逐行合并的标签列：标签只属于标签页', async () => {
  const el = await render()
  // 表头：全市场视图没有「属性 / 其他标签」列
  expect(el.textContent).not.toContain('其他标签')
  expect(el.textContent).not.toContain('属性')
  // 默认展开的游资格里有「北自科技」（带妖股标签）：没有标签列就不该出现 chip
  expect(rowsWith(el, '北自科技')).toHaveLength(1)
  expect(rowsWith(el, '北自科技')[0].textContent).not.toContain('妖股')

  await switchTo(el, 'bluechip')
  // 标签页里才有「其他标签」，回答「它还同时是什么」
  expect(el.textContent).toContain('其他标签')
  expect(rowsWith(el, '浦发银行')[0].textContent).toContain('龙头')
})

it('切到蓝筹：屏幕上只剩蓝筹的票，观察池不再重复出现', async () => {
  const el = await render()
  await switchTo(el, 'bluechip')

  expect(tab(el, 'bluechip').getAttribute('aria-selected')).toBe('true')
  expect(el.textContent).toContain('浦发银行')
  expect(el.textContent).toContain('宁德时代')
  expect(el.textContent).not.toContain('平安银行')
  expect(el.textContent).not.toContain('北自科技')
  // 卡片标题才是「这一屏有没有这块」；抽屉里的规则说明不算重复
  const headings = Array.from(el.querySelectorAll('h2')).map(h => h.textContent)
  expect(headings).not.toContain('今日观察池')
  expect(headings).not.toContain('执行窗口')

  // 每一行只出现一次：标签是视图隔离，不是把同一行复制到四个格子里
  expect(rowsWith(el, '浦发银行')).toHaveLength(1)
  // 状态条的变化/风险计数必须跟着这一屏走，否则「ST 193」点下去只筛出 1 行
  expect(el.textContent).toContain('今日变化（本标签）')
  expect(el.textContent).toContain('上移 1')
  // 已经按蓝筹筛过，列里显示的是「其他标签」
  expect(el.textContent).toContain('其他标签')
  expect(rowsWith(el, '浦发银行')[0].textContent).toContain('龙头')
  expect(rowsWith(el, '宁德时代')[0].textContent).not.toContain('龙头')
})

it('白马没有 PIT 序列时显示「无历史背书」，不给 0%', async () => {
  const el = await render()
  await switchTo(el, 'whitehorse')

  expect(el.textContent).toContain('无历史背书')
  expect(el.textContent).toContain('财报')
  // 标签页头不应把 null 渲染成 0.00%
  expect(el.textContent).not.toContain('0.00%')
})

it('妖股的主场分布只列有票的两格，且默认只展开票最多的一格', async () => {
  const el = await render()
  await switchTo(el, 'yaogu')

  const mix = el.querySelector('[role="group"][aria-label="该标签的主场分布"]')
  expect(mix).not.toBeNull()
  const segments = Array.from(mix!.querySelectorAll('button'))
  expect(segments.map(b => b.getAttribute('aria-label'))).toEqual([
    expect.stringContaining('量化主场'),
    expect.stringContaining('游资主场'),
  ])
  // 两格各 1 只，默认展开第一格；另一格折叠
  const expanded = Array.from(el.querySelectorAll('[data-arena] > button[aria-expanded="true"]'))
  expect(expanded).toHaveLength(1)
  expect(expanded[0].textContent).toContain('量化主场')
})

it('换标签会清掉上一屏的主场筛选', async () => {
  const el = await render()
  const band = el.querySelector('button[title*="机构主场"]') as HTMLButtonElement
  expect(band).not.toBeNull()
  await click(band)
  expect(el.querySelector('[data-testid="radar-result"]')?.textContent).toContain('4')
  expect(el.querySelector('[aria-label="取消筛选 机构主场"]')).not.toBeNull()

  await switchTo(el, 'bluechip')
  // 筛选没带过来：不会出现「蓝筹 + 机构」这种跨屏条件，结果条也不再出现
  expect(el.querySelector('[data-testid="radar-result"]')).toBeNull()
  expect(el.querySelector('[aria-label="取消筛选 机构主场"]')).toBeNull()
})

it('标签页内搜索只在本标签内计数', async () => {
  const el = await render()
  await switchTo(el, 'bluechip')

  const input = el.querySelector('input[aria-label="搜索代码、名称或行业"]') as HTMLInputElement
  expect(input).not.toBeNull()
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!
  await act(async () => {
    setter.call(input, '宁德')
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await new Promise(r => setTimeout(r, 0))
  })

  const result = el.querySelector('[data-testid="radar-result"]')
  expect(result?.textContent).toContain('本标签命中')
  expect(result?.textContent).toContain('1')
  expect(result?.textContent).toContain('/ 2 只')
  expect(el.textContent).not.toContain('浦发银行')
})

it('标签页支持左右方向键切换', async () => {
  const el = await render()
  await act(async () => {
    tab(el, 'all').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
    await new Promise(r => setTimeout(r, 0))
  })
  expect(tab(el, 'bluechip').getAttribute('aria-selected')).toBe('true')
})

it('?tag= 直接打开对应视图（旧路径重定向保留 query，这里必须认）', async () => {
  const el = await render('/shortline-radar?tag=yaogu')
  expect(tab(el, 'yaogu').getAttribute('aria-selected')).toBe('true')
  expect(el.textContent).toContain('妖王电子')
  expect(el.textContent).not.toContain('浦发银行')
  expect(Array.from(el.querySelectorAll('h2')).map(h => h.textContent)).not.toContain('今日观察池')
})

it('状态条的变化/风险可以筛明细，再点一次取消', async () => {
  const el = await render()
  const up = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('上移 1'))
  await click(up ?? null)
  expect(el.textContent).toContain('浦发银行')
  expect(el.textContent).not.toContain('宁德时代')
  expect(el.querySelector('[data-testid="radar-result"]')?.textContent).toContain('/ 6 只')

  await click(up ?? null)
  expect(el.querySelector('[data-testid="radar-result"]')).toBeNull()

  const watch = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('警示 1'))
  await click(watch ?? null)
  expect(el.textContent).toContain('平安银行')
  expect(el.textContent).not.toContain('浦发银行')
})

it('只看自选：自选接口有票时才出现开关，开启后只剩自选', async () => {
  vi.spyOn(api, 'watchlistList').mockResolvedValue({
    symbols: [{ symbol: '300750.SZ', added_at: '2026-11-01' }],
  })
  const el = await render()
  const toggle = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('只看自选'))
  expect(toggle).toBeTruthy()
  await click(toggle ?? null)
  expect(el.textContent).toContain('宁德时代')
  expect(el.textContent).not.toContain('浦发银行')
  expect(el.querySelector('[data-testid="radar-result"]')?.textContent).toContain('1')
})

it('漏斗每一步的删除量落在同一批票上（519 → 55 这一步不能混入全市场阶段计数）', async () => {
  const el = await render()
  // struct 4 − cold 2 = 2；fixture 的逻辑否定全为 0 ⇒ 这 2 只全部是「非未启动」
  const text = el.textContent ?? ''
  expect(text).toContain('热度达标')
  expect(text).toContain('非未启动 2')
  expect(text).not.toContain('正在炒 2')
})

const AUDIT_BACKFILL = {
  generated_at: '2026-11-23',
  prereg_id: 'heat-sort-2026-11-24',
  rule_current: 'heat-v1',
  rule_shadow: 'heat-v2-noactivity',
  note: 'x',
  gate: { min_months: 6, lift_delta_bp: 50, t: 1.65, net_bp: 0 },
  all: {
    months: 24, cur_lift_bp: -140.9, cur_t: -2.98,
    shadow_lift_bp: -25.8, shadow_t: -0.44,
    paired_delta_bp: 115.1, paired_t: 1.85, shadow_net_bp: -209.5,
  },
  forward: null,
  days_registered: 500, fwd_days: 0, backfill_days: 500,
  days_evaluated: 461, days_pending: 39,
}

it('排序体检：无产出物时显示「未生成」与预注册文档路径', async () => {
  const el = await render()
  expect(el.textContent).toContain('排序体检未生成')
  // 完整说明在抽屉里；打开后必须给出预注册文件
  const open = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('口径 · 定义 · 研究依据'))
  await click(open ?? null)
  expect(el.textContent).toContain('prereg-heat-sort.md')
})

it('排序体检：回填期只报状态，完整四格在抽屉里且标样本内', async () => {
  vi.spyOn(api, 'shortlineRadar').mockResolvedValue({
    ...fixture,
    focus: { ...fixture.focus, sort_audit: AUDIT_BACKFILL },
  })
  const el = await render()
  // 首屏只留一行：现行 / 候选 / 配对
  expect(el.textContent).toContain('回填 500 天（样本内）')
  expect(el.textContent).toContain('-140.9bp')
  expect(el.textContent).toContain('+115.1bp')
  expect(el.textContent).not.toContain('-209.5bp')

  const open = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('口径 · 定义 · 研究依据'))
  await click(open ?? null)
  expect(el.textContent).toContain('heat-sort-2026-11-24')
  expect(el.textContent).toContain('回填对照 500 天（样本内，不参与裁决）')
  expect(el.textContent).toContain('前向可评估 0/6 月')
  expect(el.textContent).toContain('-209.5bp')
})

it('排序体检：前向有数据时首行切到 forward，裁决线按 forward 月数判', async () => {
  vi.spyOn(api, 'shortlineRadar').mockResolvedValue({
    ...fixture,
    focus: {
      ...fixture.focus,
      sort_audit: {
        ...AUDIT_BACKFILL,
        fwd_days: 30,
        forward: {
          months: 1, cur_lift_bp: 12.5, cur_t: null, shadow_lift_bp: -3.2,
          shadow_t: null, paired_delta_bp: null, paired_t: null, shadow_net_bp: null,
        },
      },
    },
  })
  const el = await render()
  expect(el.textContent).toContain('前向 1/6 月')
  expect(el.textContent).not.toContain('回填 500 天（样本内）')
  expect(el.textContent).toContain('+12.5bp')
})

// ===== 重点关注（锁仓横盘）：独立口径，不进池 =====
//
// 这一块存在的前提是「两条口径互斥」：池要 heat≥0.75（高换手），本形态要
// 60 日均换手 ≤1.3%。所以测试必须同时锁住三件事——块存在、冲突被逐条写出、
// 历史负读数跟着显示。少任何一条，页面就会把它读成「明天要涨」。

it('重点关注：单独一块、写明与观察池旧标准的冲突、辨识度更高的带标记', async () => {
  const el = await render()
  expect(el.textContent).toContain('重点关注 · 锁仓横盘')
  expect(el.textContent).toContain('独立口径 · 不进观察池')
  // 主对象 = 离参照读数最近的那只
  expect(el.textContent).toContain('维力医疗')
  expect(el.textContent).toContain('0.90%')          // 60 日均换手
  expect(el.textContent).toContain('-3.7%')          // 20 日涨幅（跌绿，但文案仍是负号）
  // 冲突必须逐条列出，并说明因此单独列出
  expect(el.textContent).toContain('与观察池旧标准冲突 2 项')
  expect(el.textContent).toContain('热度分 0.24 < 0.75')
  expect(el.textContent).toContain('当日成交额 547 万 < 3000 万')
  // 辨识度更高的同形态票要带标记
  expect(el.textContent).toContain('同庆楼')
  expect(el.textContent).toContain('★')
  // 历史读数：不能说成「即将启动」
  expect(el.textContent).toContain('安静不等于要动')
  expect(el.textContent).toContain('3.6%')
  expect(el.textContent).toContain('11.4%')
  // 换手档：把卡片锚到「放量 × 换手档」的历史读数上
  expect(el.textContent).toContain('换手档 Q1/5')
  expect(el.textContent).toContain('0.2× 均量')
})

it('重点关注：切到标签页后不再出现（与观察池同一屏规则）', async () => {
  const el = await render()
  expect(el.textContent).toContain('重点关注 · 锁仓横盘')
  await switchTo(el, 'bluechip')
  expect(el.textContent).not.toContain('重点关注 · 锁仓横盘')
})
