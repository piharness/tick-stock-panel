// @vitest-environment jsdom
// 结构回归：这一页的重构（六视图 → 一本手册的六章）。
// 最容易被改坏的不是内容，而是「结构契约」：
//   - 六个 tab 与六个 panel 是否一一对应；
//   - 非当前视图是否仍在 DOM 里（否则搜索、深链、档案测试都会静默失效）；
//   - 默认视图是否仍是「术 · 今天」（唯一每天要看的）；
//   - 四个档案折叠是否还在、索引能否展开对应折叠；
//   - 旧锚点（#buy / #sizing-audit / #archive-b）是否还落到同一块内容上。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StructuralEdge } from './index'
import { refusals } from './data'

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
  rules: [
    { confirm_days: 0, cagr_pct: 2.5, mdd_pct: -35.63, sharpe: 0.22, exposure_pct: 81.1, switches: 98, is_default: false },
    { confirm_days: 10, cagr_pct: 2.25, mdd_pct: -44.44, sharpe: 0.21, exposure_pct: 89.5, switches: 26, is_default: true },
  ],
  halves: [{ label: '2016–2021H1', days: 1335, base_cagr_pct: 4.96, rule_cagr_pct: 1.5, base_mdd_pct: -42.71, rule_mdd_pct: -44.44 }],
  placebo: { available: true, blocks: 13, flat_days: 278, p_value: 0.77 },
  checks: [
    { id: 'AC1', name: '状态标签稳定', want: '≤3%', got: '1.36%', pass: true },
    { id: 'AC3', name: '规则胜基准', want: '两段均胜', got: '两段都输', pass: false },
  ],
  equity: [],
  flat_stats: { flat_days: 278, flat_mean_bp: 11.1, in_mean_bp: 2.5 },
  neff: { year: '2026', down_overlap: 45, flat_overlap: 121, down_nonoverlap: 2, flat_nonoverlap: 6 },
  warning: '',
}))

const tail = vi.hoisted(() => ({
  available: true, year: 2026, cut_pct: 20, horizon: 20,
  cells: [{ mq: 3, tq: 0, n: 46773, p_tail_pct: 1.3, med_pct: -1.34 }],
  by_state: [
    { mkt_state: 'down', n: 100, p_tail_pct: 2.2, med_pct: 3.65 },
    { mkt_state: 'flat', n: 100, p_tail_pct: 9.7, med_pct: -4.74 },
  ],
  best: { mq: 3, tq: 0, n: 46773, p_tail_pct: 1.3, med_pct: -1.34 },
  worst: { mq: 4, tq: 4, n: 36944, p_tail_pct: 22.7, med_pct: -6.67 },
  ratio: 17.5,
}))

vi.mock('@/lib/api', () => ({
  api: {
    etfWatchAccount: async () => { throw new Error('404 Not Found') },
    etfWatchCashLeg: async () => { throw new Error('404 Not Found') },
    etfWatch: async () => { throw new Error('404 Not Found') },
    etfWatchReplay: async () => { throw new Error('404 Not Found') },
    positionArt: async () => art,
    positionArtTail: async () => tail,
  },
}))

// jsdom 没有 canvas，echarts.init 会抛。结构测试不关心图，只要求不崩。
vi.mock('echarts', () => ({
  init: () => ({ setOption: () => {}, resize: () => {}, dispose: () => {} }),
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
  window.history.replaceState(null, '', '#')
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
  window.history.replaceState(null, '', '#')
})

async function render() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <StructuralEdge />
      </QueryClientProvider>,
    )
  })
  await act(async () => { await new Promise(r => setTimeout(r, 20)) })
  return host
}

const panel = (el: HTMLElement, id: string) => el.querySelector(`#se-panel-${id}`)

it('六个 tab 与六个 panel 一一对应，默认视图是「执行」', async () => {
  const el = await render()
  const tabs = Array.from(el.querySelectorAll('[role="tab"]')) as HTMLButtonElement[]
  expect(tabs).toHaveLength(6)

  for (const t of tabs) {
    const id = t.getAttribute('aria-controls')!
    const p = el.querySelector(`#${id}`)
    expect(p, `tab ${t.id} 的 aria-controls 指向了不存在的节点`).not.toBeNull()
    expect(p!.getAttribute('role')).toBe('tabpanel')
    expect(p!.getAttribute('aria-labelledby')).toBe(t.id)
  }

  expect(el.querySelector('#se-tab-today')!.getAttribute('aria-selected')).toBe('true')
  expect(el.querySelector('#se-tab-today')!.textContent).toContain('执行')
  expect(panel(el, 'today')!.hasAttribute('hidden')).toBe(false)
  for (const k of ['mandate', 'evidence', 'rules', 'tools', 'archive']) {
    expect(panel(el, k)!.hasAttribute('hidden'), `${k} 不应该默认可见`).toBe(true)
  }
})

it('六章按决策链排列，且每个 tab 带自己的更新频率', async () => {
  const el = await render()
  const tabs = Array.from(el.querySelectorAll('[role="tab"]')) as HTMLButtonElement[]
  const labels = tabs.map(t => (t.textContent ?? '').trim())
  for (const [i, s] of ['总则', '证据', '规则', '执行', '反证', '工具'].entries()) {
    expect(labels[i].startsWith(s), `第 ${i + 1} 个 tab 应为 ${s}，实际 ${labels[i]}`).toBe(true)
  }
  // 频率必须写在 tab 上：总则/反证冻结，执行每日，规则预注册
  expect(labels[0]).toContain('冻结')
  expect(labels[3]).toContain('每日')
  expect(labels[2]).toContain('预注册')
  expect(labels[5]).toContain('按需')
})

it('切换 tab 只改变可见性，所有面板内容常驻 DOM', async () => {
  const el = await render()
  const rules = el.querySelector('#se-tab-rules') as HTMLButtonElement
  await act(async () => { rules.click() })

  expect(rules.getAttribute('aria-selected')).toBe('true')
  expect(panel(el, 'rules')!.hasAttribute('hidden')).toBe(false)
  expect(panel(el, 'today')!.hasAttribute('hidden')).toBe(true)
  // 档案在另一个视图里，但内容仍应可被检索/测试读到
  expect(el.textContent).toContain('这五个月的账')
  expect(el.textContent).toContain('被迫流账本')
})

it('键盘左右箭头可以在六章之间移动', async () => {
  const el = await render()
  const today = el.querySelector('#se-tab-today') as HTMLButtonElement
  await act(async () => {
    today.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }))
  })
  // 章节顺序：总则 → 证据 → 规则 → 执行 → 反证 → 工具
  expect(el.querySelector('#se-tab-archive')!.getAttribute('aria-selected')).toBe('true')

  const rules = el.querySelector('#se-tab-rules') as HTMLButtonElement
  await act(async () => {
    rules.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))
  })
  expect(el.querySelector('#se-tab-tools')!.getAttribute('aria-selected')).toBe('true')
})

it('深链 #evidence 直接打开证据章；旧锚点 #sizing-audit 也落到证据章', async () => {
  window.history.replaceState(null, '', '#evidence')
  let el = await render()
  expect(el.querySelector('#se-tab-evidence')!.getAttribute('aria-selected')).toBe('true')
  expect(panel(el, 'evidence')!.hasAttribute('hidden')).toBe(false)

  await act(async () => root.unmount())
  window.history.replaceState(null, '', '#sizing-audit')
  root = createRoot(host)
  el = await render()
  expect(el.querySelector('#se-tab-evidence')!.getAttribute('aria-selected')).toBe('true')
})

it('旧 tab 深链 #dao / #qi 仍然打开对应的新章', async () => {
  window.history.replaceState(null, '', '#dao')
  let el = await render()
  expect(el.querySelector('#se-tab-mandate')!.getAttribute('aria-selected')).toBe('true')

  await act(async () => root.unmount())
  window.history.replaceState(null, '', '#qi')
  root = createRoot(host)
  el = await render()
  expect(el.querySelector('#se-tab-tools')!.getAttribute('aria-selected')).toBe('true')
})

it('旧锚点 #buy 仍然打开规则视图（改名不能把书签打断）', async () => {
  window.history.replaceState(null, '', '#buy')
  const el = await render()
  expect(el.querySelector('#se-tab-rules')!.getAttribute('aria-selected')).toBe('true')
  expect(panel(el, 'rules')!.hasAttribute('hidden')).toBe(false)
})

it('深链 #archive-b 打开反证章并展开 B 卷', async () => {
  window.history.replaceState(null, '', '#archive-b')
  const el = await render()
  await act(async () => { await new Promise(r => setTimeout(r, 180)) })
  expect(panel(el, 'archive')!.hasAttribute('hidden')).toBe(false)
  expect((el.querySelector('#archive-b') as HTMLDetailsElement).open).toBe(true)
})

it('总则给出「你在赌什么」与六条不做，且每条都能跳到证据', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('约束集')
  expect(text).toContain('六条不做')
  for (const s of ['不预测方向', '不用「状态」调仓位', '不买个股', '不等确认，也不等月末', '不满仓、不单押', '不追二手信息']) {
    expect(text).toContain(s)
  }
})

it('总则的六条不做都带环境依赖标签，且 05 制度环境有真实锚点', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  // 三档标签必须都在：制度无关 / 依赖托底 / 反向增强
  for (const s of ['制度环境', '制度无关', '依赖托底', '反向增强']) {
    expect(text).toContain(s)
  }
  // 漏一条就是「有结论没被标适用域」—— 这是标签系统的失效方式，静默且无报错
  for (const r of refusals) {
    expect(r.env, `第 ${r.no} 条缺环境标签`).toMatch(/^(free|depends|stronger)$/)
  }
  expect(el.querySelector('#env'), '05 · 制度环境的锚点不存在，02 节的跳转会静默失效').not.toBeNull()
})

it('六条不做的每一条都能落到真实存在的章与锚点（总则不许悬空）', async () => {
  const el = await render()
  for (const r of refusals) {
    expect(el.querySelector(`#se-panel-${r.to}`), `第 ${r.no} 条指向了不存在的章 ${r.to}`).not.toBeNull()
    expect(el.querySelector(`#${r.anchor}`), `第 ${r.no} 条的锚点 #${r.anchor} 不存在`).not.toBeNull()
  }
})

it('执行章的反例登记与扫描并列，并明确「不构成指令」', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('反例登记')
  expect(text).toContain('不构成指令')
  expect(text).toContain('空仓')
  // 它与扫描器不是同一条规则，必须写出「不能相加、不能替代」
  expect(text).toContain('不能相加')
})

it('证据章保留仓位择时的五项验收，未通过的项必须显示「未通过」', async () => {
  const el = await render()
  expect(el.textContent).toContain('仓位择时的验收')
  expect(el.textContent).toContain('未通过')
  expect(el.textContent).toContain('全程持有')
  // 构成轴的 25 格差与状态差必须同屏，否则「真轴是构成」无法核对
  expect(el.textContent).toContain('17.5')
})

it('组合层必须回答「为什么是国债不是红利/黄金」', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('第二条腿')
  for (const s of ['宽基', '红利', '黄金', '国债 511010']) expect(text).toContain(s)
  expect(text).toContain('启用（JB0）')
})

it('买点六个问题全部在场，缺少任一条都会让规则失去定义力', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  for (const q of ['买什么', '在什么位置', '在什么形态', '在什么时点', '买多少', '什么情况下不买']) {
    expect(text).toContain(q)
  }
})

it('持有与退出在场：固定 20 日基准与「四种退出全输」的裁决', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('持有与退出')
  expect(text).toContain('固定 20 日')
  expect(text).toContain('候选失效即走')
  expect(text).toContain('dd 回到 −20% 即走')
})

it('反证默认折叠为四组案卷，索引卡能展开对应案卷', async () => {
  const el = await render()
  const folds = Array.from(el.querySelectorAll('details')) as HTMLDetailsElement[]
  // 回放口径折叠也在 DOM 里（器层），档案四组必须都在且默认收起
  const archiveFolds = folds.filter(d => d.id.startsWith('archive-'))
  expect(archiveFolds).toHaveLength(4)
  for (const d of archiveFolds) expect(d.open).toBe(false)

  expect(el.textContent).toContain('被迫流账本')
  expect(el.textContent).toContain('这五个月的账')

  const card = el.querySelector('[data-archive-index="b"]') as HTMLAnchorElement
  expect(card).not.toBeNull()
  await act(async () => { card.click() })
  expect((el.querySelector('#archive-b') as HTMLDetailsElement).open).toBe(true)
})

it('跨章链接能打开案卷 B，而不是只切换 tab', async () => {
  const el = await render()
  const link = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('打开案卷 B'))
  expect(link).toBeTruthy()
  await act(async () => {
    link!.click()
    await new Promise(r => setTimeout(r, 160))
  })
  expect(panel(el, 'archive')!.hasAttribute('hidden')).toBe(false)
  expect((el.querySelector('#archive-b') as HTMLDetailsElement).open).toBe(true)
})

it('工具章的回放未生成时给出命令，不静默空白', async () => {
  const el = await render()
  const tab = el.querySelector('#se-tab-tools') as HTMLButtonElement
  await act(async () => {
    tab.click()
    await new Promise(r => setTimeout(r, 0))
  })
  expect(panel(el, 'tools')!.hasAttribute('hidden')).toBe(false)
  expect(el.textContent).toContain('历史回放未生成')
  expect(el.textContent).toContain('etf_watch_replay.py')
})

it('活数据未就绪时页面仍完整渲染，不出现空白或报错页', async () => {
  const el = await render()
  expect(el.textContent).toContain('还没有盘后扫描结果')
  expect(el.textContent).toContain('买点：六个问题，一次答完')
  expect(el.textContent).toContain('不买清单')
})
