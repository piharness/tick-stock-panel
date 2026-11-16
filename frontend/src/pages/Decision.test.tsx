// @vitest-environment jsdom
/**
 * 决策账本页冒烟测试 — 只验证"三态必须分开显示"这一条契约。
 * 这条契约是页面存在的理由：UNKNOWN 与 PASS 混在一起，就等于没有门禁。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { api, type DecisionAutoConfig, type DecisionOverview, type DecisionSlateResponse, type Preferences } from '@/lib/api'
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

/** 一份「名单就绪但落后两个交易日」的响应。
 *
 *  这个 fixture 刻意让 `stale_at_build=false` 与 `stale=true` 并存：
 *  页面必须读后者。写盘那一刻它确实是最新的——今天再看就旧了，
 *  把旧结论当结论返回，等于让停滞永远沉默。 */
const slateResponse: DecisionSlateResponse = {
  slate: {
    schema_revision: 1,
    as_of: '2026-11-11',
    entry_date: null,
    strategy_id: 'l1_gap_reversal',
    status: 'ready',
    top_n: 20,
    n_rows: 1,
    rows: [{
      symbol: '600519.SH', rank: 1, mom20_pct: 12.34, prev_close_raw: 100.0,
      limit_price: 99.0, gap_threshold_pct: -1.0, day_amount_yi: 2.1,
      tradable: true, in_card: true, p_win: 0.47,
      card_limit_price: 99.0, limit_mismatch: false,
    }],
    truncation: { top_n: 20, rule: 'mom20 rank <= 20', source: 'l1_cohort.L1_TOP_N' },
    caliber: {},
    panel_since: '2026-09-01',
    warnings: [],
    stale_at_build: false,
    lag_sessions_at_build: 0,
    expected_as_of_at_build: '2026-11-11',
    built_at: '2026-11-11T16:00:00+08:00',
    expected_as_of: '2026-11-13',
    lag_sessions: 2,
    stale: true,
  },
  card: null,
  head: null,
  // 覆盖条：两周里每天都成立。断言的落点是「序列存在」而不是某个数字——
  // 数字会随真实数据变，而「近 N 日里有几个缺口」这句话必须一直在。
  series: {
    lookback: 20,
    sessions: 2,
    ready: 2,
    empty: 0,
    missing: [],
    unreadable: [],
    not_ready: [],
    holes: [],
    head: {
      as_of: '2026-11-11', present: true, status: 'ready', n_rows: 1,
      built_at: '2026-11-11T16:00:00+08:00', origin: 'pipeline', reason: null,
    },
    days: [
      { as_of: '2026-11-10', present: true, status: 'ready', n_rows: 20,
        built_at: null, origin: 'pipeline', reason: null },
      { as_of: '2026-11-11', present: true, status: 'ready', n_rows: 1,
        built_at: null, origin: 'pipeline', reason: null },
    ],
  },
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
  // 名单面板的依赖：不 mock 会在 jsdom 里发真请求
  vi.spyOn(api, 'decisionSlate').mockResolvedValue(slateResponse)
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
          <Decision />
        </QueryClientProvider>
      </MemoryRouter>,
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
      // 不许写成 `BEATS_CONSTANT`：那是 3ca7e765 之前的状态（判决只看点估计、
      // 显著性只看下界，两根轴独立返回）。后端现在由 `verdict_of()` 单点产出，
      // `UNKNOWN` 时 verdict 只能是 `UNKNOWN`——把不可能组合留在 fixture 里，
      // 等于给「回退到旧口径」留了一条绿灯。
      verdict: 'UNKNOWN',
      reason: '等效独立样本 1 < 30（或不可靠）：分数可读，结论不可判定',
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
  // n_eff=1 时 51.6% 的命中率不许被写成「跑赢」：数字可读，结论不可判定。
  // 后端 verdict 已经与显著性同轴（3ca7e765），前端这层是文案闸——
  // 否则读者只记住那个「跑赢」。
  expect(text).not.toContain('跑赢')
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

it('名单落后时显示落后天数——status=ready 不许盖住 stale', async () => {
  // 11-15 的现场：卡有效、页面显示「已封存 10 张卡」，但名单停在两个交易日前。
  // 「有效」和「最新」是两根轴，合并任何一根都会让停滞变成沉默。
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('每日名单')
  expect(text).toContain('600519.SH')
  expect(text).toContain('名单就绪')
  expect(text).toContain('落后 2 个交易日')
  expect(text).toContain('2026-11-13')  // 行情已到的日期必须出现在告警里
})

it('最近一次运行没成立时，仍给出最近一份成立的名单并说明病因', async () => {
  // 两件事必须同时成立：可执行的名单不许被一条运行状态藏掉，
  // 而「这不是今天新算的」也不许被藏掉。只用其中一句都会骗人。
  vi.mocked(api.decisionSlate).mockResolvedValue({
    ...slateResponse,
    head: {
      ...slateResponse.slate,
      as_of: '2026-11-13',
      status: 'skipped_not_closed',
      rows: [],
      n_rows: 0,
      reason: 'quote_ts=11:20:00 早于 15:00:00，是盘中快照',
    },
    series: { ...slateResponse.series, holes: ['2026-11-13'], not_ready: ['2026-11-13'] },
  })
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('最近一次运行')
  expect(text).toContain('2026-11-13')
  expect(text).toContain('盘中快照')
  expect(text).toContain('600519.SH')          // 可执行名单仍在页面上
  expect(text).toContain('缺口')
  // 有更具体的病因时不再用笼统的「落后 N 天 + 去查管道」盖住它。
  expect(text).not.toContain('先查盘后管道 Step 2.9')
})

it('名单不存在时说的是「还没生成」，不是「加载中」', async () => {
  vi.mocked(api.decisionSlate).mockRejectedValue(new Error('名单不存在'))
  await render()
  const text = host.textContent ?? ''
  expect(text).toContain('名单还没生成')
  expect(text).toContain('Step 2.9')
})
