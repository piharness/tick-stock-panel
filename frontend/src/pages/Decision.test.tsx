// @vitest-environment jsdom
/**
 * 决策账本页冒烟测试 — 只验证"三态必须分开显示"这一条契约。
 * 这条契约是页面存在的理由：UNKNOWN 与 PASS 混在一起，就等于没有门禁。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type DecisionAutoConfig, type DecisionOverview, type Preferences } from '@/lib/api'
import { toast } from '@/components/Toast'
import { Decision } from './Decision'

vi.mock('@/components/Toast', () => ({ toast: vi.fn() }))

const overview: DecisionOverview = {
  as_of: '2026-11-04',
  gates: {
    as_of: '2026-11-04',
    decision: 'GO_WITH_UNKNOWNS',
    blocked: false,
    checks: [
      { id: 'K1_LOSS', rule: '纸面账户回撤 >= 15%', status: 'PASS', detail: '未开通纸面账户', evidence: {} },
      { id: 'K2_ASSUMPTION', rule: '持仓出现假设破坏', status: 'UNKNOWN', detail: 'instruments 不可读', evidence: {} },
      { id: 'K3_NO_RECORD', rule: '决策卡字段完整', status: 'PASS', detail: '账本为空', evidence: {} },
      { id: 'K4_DATA_STALE', rule: 'as_of 当日行情已落盘', status: 'PASS', detail: '已覆盖至 2026-11-04', evidence: {} },
    ],
    notes: ['无法评估：K2_ASSUMPTION（未阻断）'],
  },
  cards: 0,
  latest_cards: [],
  settlements: 0,
  score: {
    n: 0, not_filled: 0, pending: 0, hit_rate: null, brier: null, brier_const: null,
    skill: null, logloss: null, ret_median: null, excess_mean: null, excess_lcb: null,
    verdict: 'INSUFFICIENT', reason: '已结算样本 0 < 8',
  },
}

let host: HTMLDivElement
let root: Root
let client: QueryClient

const autoConfig: DecisionAutoConfig = {
  enabled: true, strategy_id: 'trend_breakout', top_n: 20, horizon: 20,
  basis: 'median', account_id: 'default', allow_prior: false,
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.spyOn(api, 'decisionOverview').mockResolvedValue(overview)
  vi.spyOn(api, 'decisionScore').mockResolvedValue({ score: overview.score })
  // 自动发卡卡片的依赖：不 mock 会在 jsdom 里发真请求
  vi.spyOn(api, 'preferences').mockResolvedValue({ decision_auto: autoConfig } as unknown as Preferences)
  vi.spyOn(api, 'strategyList').mockResolvedValue(
    { strategies: [{ id: 'trend_breakout', name: '趋势突破' }] } as unknown as Awaited<ReturnType<typeof api.strategyList>>)
  vi.spyOn(api, 'updateDecisionAutoConfig').mockResolvedValue(autoConfig)
  vi.spyOn(api, 'decisionProduce').mockResolvedValue({ status: 'created', card_id: 'card_test' })
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
      <QueryClientProvider client={client}>
        <Decision />
      </QueryClientProvider>,
    )
  })
  // react-query 的 promise 解析 + 重渲染要跨几个微任务/宏任务，单次 await 不够
  for (let i = 0; i < 5; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
}

it('三态分别渲染，UNKNOWN 不被当成 PASS', async () => {
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('决策账本')
  expect(text).toContain('GO · 带未知')
  expect(text).toContain('K2_ASSUMPTION')
  expect(text).toContain('UNKNOWN')
  expect(text).toContain('无法评估')
})

it('空账本显示说明而不是伪造数据', async () => {
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('还没有决策卡')
  expect(text).toContain('样本不足')
  expect(text).not.toContain('NaN')
})

it('有效样本不足时 skill 降级为「不可判定」，但数字仍然显示', async () => {
  // 386 条观测 / 40 天 / 20 日持有期：n_eff=1，skill 是读数不是结论。
  // 隐藏数字会让人去别处找同一个数（顶部 skill、/pwin），那里没有这个语境；
  // 所以降级显示而不是删除——「不可判定」比「空白」更难被误读。
  const settled: DecisionOverview = {
    ...overview,
    cards: 1,
    settlements: 386,
    score: {
      ...overview.score,
      n: 386,
      days: 40,
      n_eff: 1,
      hac_lag: 19,
      n_eff_reliable: false,
      // 读数与结论分开：下界为正（skill 是 +33%），但结论是不可判定。
      skill_lcb: 0.1723,
      skill_lcb_positive: true,
      significance: 'UNKNOWN',
      significant: false,
      hit_rate: 0.1995,
      brier: 0.2004,
      brier_const: 0.25,
      skill: 0.3316,
      verdict: 'BEATS_CONSTANT',
      reason: 'Brier skill +33.16% > 0（基线 随机 0.5）',
    },
  }
  vi.mocked(api.decisionOverview).mockResolvedValue(settled)
  vi.mocked(api.decisionScore).mockResolvedValue({ score: settled.score })

  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('有效样本')
  expect(text).toContain('lag 19')
  expect(text).toContain('不可判定')
  expect(text).toContain('33.2%')          // 数字没有被删掉
  expect(text).toContain('不重叠窗口数')     // 构造值必须说明来源
})

it('median 口径：结论成立才打勾', async () => {
  vi.mocked(api.decisionOverview).mockResolvedValue({
    ...overview,
    score_median: {
      ...overview.score,
      n: 386, days: 40, n_eff: 44, skill: 0.33, skill_lcb: 0.17,
      significance: 'PASS', significant: true,
    },
  })

  await render()
  expect(host.textContent ?? '').toContain('33.0% ✓')
})

it('median 口径：不可判定打问号，不能借 ✓ 冒充结论', async () => {
  vi.mocked(api.decisionOverview).mockResolvedValue({
    ...overview,
    score_median: {
      ...overview.score,
      n: 386, days: 40, n_eff: 1, skill: 0.33, skill_lcb: 0.17,
      significance: 'UNKNOWN', significant: false,
    },
  })

  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('33.0% ?')
  expect(text).not.toContain('✓')
})

it('自动发卡开关：显示绑定策略，点击即 PUT enabled（部分更新）', async () => {
  await render()
  const box = host.querySelector('input[type="checkbox"]') as HTMLInputElement
  expect(box).not.toBeNull()
  expect(box.checked).toBe(true)                       // mock 配置 enabled=true
  expect(host.textContent).toContain('绑定策略')
  expect(host.textContent).toContain('趋势突破')          // 策略下拉有可选项
  await act(async () => { box.click() })               // 取消勾选 → PUT enabled=false
  for (let i = 0; i < 3; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
  expect(api.updateDecisionAutoConfig).toHaveBeenCalledWith({ enabled: false })
})

it('立即生产：调 produce 端点，created 时提示卡号且算成功', async () => {
  await render()
  const btn = [...host.querySelectorAll('button')]
    .find(b => b.textContent?.includes('立即生产'))
  expect(btn).not.toBeUndefined()
  await act(async () => { btn!.click() })
  for (let i = 0; i < 3; i++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  }
  expect(api.decisionProduce).toHaveBeenCalled()
  expect(toast).toHaveBeenCalledWith(expect.stringContaining('已发卡'), 'success')
  expect(toast).toHaveBeenCalledWith(expect.stringContaining('card_test'), 'success')
})
