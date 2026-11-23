// @vitest-environment jsdom
/**
 * 短线雷达 —— 标签隔离的结构契约。
 *
 * 这一页最容易改坏的不是样式，而是「隔离」本身：
 *   1. 五个标签页与计数是否来自同一份 summary；
 *   2. 切到某个标签后，屏幕上是否只剩这一类的票（而不是在 5,572 行里加个高亮）；
 *   3. 观察池/执行窗口是否只出现在「全部」这一屏，而不是跟着标签重复五遍；
 *   4. 白马没有历史读数时必须说「无历史背书」，不能显示成 0%；
 *   5. 换标签是否清掉上一屏的主场筛选（否则会读成「该标签没有票」）。
 *
 * 这些断言都不看图，只看 DOM。样式回归由截图负责，语义回归由这里负责。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
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
      { arena: 'institution', label: '机构主场', hint: '大市值 × 低换手', count: 4, share: 4 / 6, dd20: 0.0272, med_ret20: -0.0076, levels: { core: 1, active: 2, background: 1, silent: 0 } },
      { arena: 'quant', label: '量化主场', hint: '大市值 × 高换手', count: 1, share: 1 / 6, dd20: 0.0498, med_ret20: -0.0155, levels: { core: 0, active: 1, background: 0, silent: 0 } },
      { arena: 'hotmoney', label: '游资主场', hint: '小市值 × 高换手', count: 1, share: 1 / 6, dd20: 0.0699, med_ret20: -0.0261, levels: { core: 0, active: 1, background: 0, silent: 0 } },
      { arena: 'nowhere', label: '无人区', hint: '小市值 × 低换手', count: 0, share: 0, dd20: 0.0310, med_ret20: -0.0089, levels: { core: 0, active: 0, background: 0, silent: 0 } },
    ],
    levels: [
      { level: 'core', label: '核心', hint: '', count: 1, share: 1 / 6 },
      { level: 'active', label: '活跃', hint: '', count: 4, share: 4 / 6 },
      { level: 'background', label: '背景', hint: '', count: 1, share: 1 / 6 },
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
    transitions: { up: 1, down: 1, stay: 4, new: 0 },
    risk: { high: 0, watch: 0, none: 6 },
    risk_absent: 0,
    financial_period: '2026-09-30',
  },
  focus: {
    budget: { market: 6, struct: 4, cold: 2, picks: 0 },
    stages: { cold: 4, warming: 0, hot: 2, fading: 0 },
    picks: [],
    seat_backing: [],
    emotion: { available: false },
    excluded: { st: 0, bse: 0, thin: 0, young: 0, falling: 0, hot: 2, warming: 0, fading: 0 },
  },
  rows: [
    row({ symbol: '600000.SH', name: '浦发银行', arena: 'institution', tags: ['bluechip', 'leader'], level: 'core' }),
    row({ symbol: '300750.SZ', name: '宁德时代', arena: 'institution', tags: ['bluechip'] }),
    row({ symbol: '000001.SZ', name: '平安银行', arena: 'institution', tags: ['whitehorse'] }),
    row({ symbol: '002415.SZ', name: '海康威视', arena: 'institution', tags: ['leader'] }),
    row({ symbol: '603082.SH', name: '北自科技', arena: 'hotmoney', tags: ['yaogu'], stage: 'hot' }),
    row({ symbol: '300123.SZ', name: '妖王电子', arena: 'quant', tags: ['yaogu'], stage: 'hot' }),
  ],
  disclaimer: '',
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'shortlineRadar').mockResolvedValue(fixture)
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

async function render() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <ShortlineRadar />
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

async function switchTo(el: HTMLElement, key: string) {
  await act(async () => {
    tab(el, key).click()
    await new Promise(r => setTimeout(r, 0))
  })
}

const rowsWith = (el: HTMLElement, text: string) =>
  Array.from(el.querySelectorAll('tbody tr')).filter(tr => tr.textContent?.includes(text))

it('默认停在「全部」：五个标签页带计数，观察池与执行窗口都在这一屏', async () => {
  const el = await render()
  const tabs = Array.from(el.querySelectorAll('[role="tab"]')) as HTMLButtonElement[]
  expect(tabs).toHaveLength(5)
  expect(tabs.map(t => t.id)).toEqual([
    'radar-tab-all', 'radar-tab-bluechip', 'radar-tab-whitehorse', 'radar-tab-leader', 'radar-tab-yaogu',
  ])
  expect(tab(el, 'all').getAttribute('aria-selected')).toBe('true')
  // 计数取自 summary：全部 6、蓝筹 2、白马 1、龙头 2、妖股 2
  expect(tab(el, 'all').textContent).toContain('6')
  expect(tab(el, 'bluechip').textContent).toContain('2')
  expect(tab(el, 'whitehorse').textContent).toContain('1')

  expect(el.textContent).toContain('今日观察池')
  expect(el.textContent).toContain('执行窗口')
  expect(el.textContent).toContain('机构主场')
})

it('切到蓝筹：屏幕上只剩蓝筹的票，观察池不再重复出现', async () => {
  const el = await render()
  await switchTo(el, 'bluechip')

  expect(tab(el, 'bluechip').getAttribute('aria-selected')).toBe('true')
  expect(el.textContent).toContain('浦发银行')
  expect(el.textContent).toContain('宁德时代')
  expect(el.textContent).not.toContain('平安银行')
  expect(el.textContent).not.toContain('北自科技')
  // 卡片标题才是「这一屏有没有这块」；页脚那句「观察池与执行窗口见全部」是索引，不算重复
  const headings = Array.from(el.querySelectorAll('h2')).map(h => h.textContent)
  expect(headings).not.toContain('今日观察池')
  expect(headings).not.toContain('执行窗口')

  // 每一行只出现一次：标签是视图隔离，不是把同一行复制到四个格子里
  expect(rowsWith(el, '浦发银行')).toHaveLength(1)
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
  const expanded = Array.from(el.querySelectorAll('section > button[aria-expanded="true"]'))
  expect(expanded).toHaveLength(1)
  expect(expanded[0].textContent).toContain('量化主场')
})

it('换标签会清掉上一屏的主场筛选', async () => {
  const el = await render()
  const band = el.querySelector('button[title*="机构主场"]') as HTMLButtonElement
  expect(band).not.toBeNull()
  await act(async () => {
    band.click()
    await new Promise(r => setTimeout(r, 0))
  })
  expect(el.querySelector('[data-testid="radar-result"]')?.textContent).toContain('机构主场')

  await switchTo(el, 'bluechip')
  // 筛选没带过来：不会出现「蓝筹 + 机构」这种跨屏条件，结果条也不再出现
  expect(el.querySelector('[data-testid="radar-result"]')).toBeNull()
  expect(el.textContent).not.toContain('主场：机构主场')
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
  expect(el.textContent).toContain('prereg-heat-sort.md')
})

it('排序体检：回填期显示样本内标记与门槛，不显示裁决结论', async () => {
  vi.spyOn(api, 'shortlineRadar').mockResolvedValue({
    ...fixture,
    focus: { ...fixture.focus, sort_audit: AUDIT_BACKFILL },
  })
  const el = await render()
  expect(el.textContent).toContain('heat-sort-2026-11-24')
  expect(el.textContent).toContain('回填对照 500 天（样本内，不参与裁决）')
  expect(el.textContent).toContain('前向可评估 0/6 月')
  expect(el.textContent).toContain('-140.9bp')
  expect(el.textContent).toContain('+115.1bp')
  expect(el.textContent).toContain('-209.5bp')
})

it('排序体检：前向有数据时主读数切到 forward，裁决线按 forward 月数判', async () => {
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
  expect(el.textContent).toContain('前向登记 30 天')
  expect(el.textContent).not.toContain('回填对照 500 天（样本内，不参与裁决）')
  expect(el.textContent).toContain('+12.5bp')
})
