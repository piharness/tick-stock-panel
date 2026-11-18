// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { AutoLoopStrip } from './AutoLoopStrip'
import { api, type LabLoopStatus, type LabReport } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: { labReport: vi.fn(), labAlphaStatus: vi.fn(), labAlphaRun: vi.fn() },
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.clearAllMocks()
  // 展开区会拉调度状态：不 mock 就是真请求
  vi.mocked(api.labAlphaStatus).mockResolvedValue({
    config: {
      enabled: true, alpha_search_day: 1,
      alpha_search_profile: 'balanced', alpha_search_max_formulas: 360,
    },
    schedule: {
      latest_claim: 'alpha-2026-11',
      latest: { status: 'done', started_at: '2026-11-18T16:44:45', finished_at: '2026-11-18T16:48:30', counts: { evaluated: 360, confirmed_themes: 14 } },
      running: false,
      running_claim: null,
    },
    latest: null,
  })
  vi.mocked(api.labAlphaRun).mockResolvedValue({ status: 'started', claim: 'alpha-manual-20261118-165000' })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
})

const ALPHA = {
  run_id: 'run-1',
  status: 'ok',
  created_at: '2026-11-09T12:45:55',
  generated: 360,
  confirmed: 16,
  confirmed_themes: 11,
  min_dsr: 0.9,
  dsr_pass: 0,
  top_themes: [
    { candidate_id: 'a', formula: 'rank(ts_corr(rsi_14, vol_ratio_5d, 5))', direction: 'low' as const, confirmation_ic: -0.0395, dsr: 0.21 },
  ],
}

function report(overrides: Partial<LabLoopStatus> = {}): LabReport {
  return {
    generated_at: '2026-11-18T06:00:00Z',
    mining: { last_run: null },
    guardian: { profiles: 0, by_status: {}, alerts: 0, last_run: null },
    combinations: { suggestions: [] },
    slots: { active_mined: 0, active_mined_ids: [], limit: 10, full: false },
    loop: {
      available: true,
      as_of: '2026-11-18',
      freshness: [
        { page: 'factor', label: '因子', subject: '因子研究产出', last_at: '2026-11-09T12:45:55',
          age_days: 9, state: 'aging', fresh_days: 7, stale_days: 21, detail: { alpha_run: ALPHA } },
        { page: 'strategy', label: '策略', subject: '候选沉淀', last_at: null, age_days: null,
          state: 'never', fresh_days: 7, stale_days: 21, detail: { candidates: 0 } },
        { page: 'backtest', label: '回测', subject: '样本外复验', last_at: null, age_days: null,
          state: 'never', fresh_days: 2, stale_days: 5, detail: { profiles: 0 } },
      ],
      autonomy: {
        mining: { enabled: false, weekday: 4, profile: 'balanced' },
        guardian: { enabled: false },
        decision: { enabled: false },
        any_enabled: false,
      },
      admission: { total: 0, admitted: 0, blocked: 0, items: [] },
      linkage: {
        ledger: { enabled: false, execute: false, strategy_id: null, lu_rebound_enabled: false },
        paper: { accounts: 0, enabled_rules: 0, by_strategy: {} },
      },
      next_actions: [{ page: 'strategy', level: 'info', text: '策略页还没有任何候选沉淀' }],
      ...overrides,
    },
  }
}

async function render(page: 'strategy' | 'factor' | 'backtest') {
  await act(async () => root.render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <AutoLoopStrip page={page} />
      </QueryClientProvider>
    </MemoryRouter>,
  ))
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })
  return host.textContent ?? ''
}

it('显示本页新鲜度、开关状态与主动搜索的 DSR 结论', async () => {
  vi.mocked(api.labReport).mockResolvedValue(report())
  const text = await render('factor')
  expect(text).toContain('本页待更新')
  expect(text).toContain('9 天前')
  expect(text).toContain('研究自动化：未开启')
  // confirmed 16 个不等于有结论: DSR 全不过必须显式说出来
  expect(text).toContain('360 试验')
  expect(text).toContain('11 主题')
  expect(text).toContain('无一过 DSR 门槛')
})

it('展开后给出三页明细与下一步', async () => {
  vi.mocked(api.labReport).mockResolvedValue(report())
  await render('factor')
  const toggle = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('详情'))
  expect(toggle).toBeDefined()
  await act(async () => toggle!.click())
  const text = host.textContent ?? ''
  expect(text).toContain('三页新鲜度')
  expect(text).toContain('门槛 7/21 天')
  expect(text).toContain('策略页还没有任何候选沉淀')
  expect(text).toContain('rank(ts_corr(rsi_14, vol_ratio_5d, 5))')
})

it('状态不可用时明说，而不是静默不渲染', async () => {
  vi.mocked(api.labReport).mockResolvedValue(
    report({ available: false, error: 'boom' }),
  )
  const text = await render('backtest')
  expect(text).toContain('研究闭环状态不可用')
  expect(text).toContain('boom')
  expect(text).toContain('页面数据本身不受影响')
})

it('从未产出时不冒充新鲜', async () => {
  vi.mocked(api.labReport).mockResolvedValue(report())
  const text = await render('backtest')
  expect(text).toContain('本页无产出')
  expect(text).not.toContain('本页新鲜')
})

const MARKET_OFF = {
  state: 'risk_off' as const,
  as_of: '2026-11-18',
  breadth_20d: 0.4143,
  eq_ma20_bp: -49.75,
  window: 20,
  samples: 5536,
  threshold: 0.5,
  note: '近 20 日仅 41.4% 的个股上涨, 等权日均 -49.7bp: β 暴露本身是负期望',
  evidence: {
    fwd20_median_pct: -0.11,
    fwd20_positive_pct: 49,
    same_state_days: 1646,
    benchmark_10y_total_pct: -13.3,
    source: 'analysis/_diag_regime_gate.py --days 2400',
  },
}

it('环境逆风时在顶栏标出广度，但不改写准入结论', async () => {
  vi.mocked(api.labReport).mockResolvedValue(
    report({
      market: MARKET_OFF,
      admission: {
        total: 1,
        admitted: 1,
        blocked: 0,
        items: [
          {
            strategy_id: 'mined_headwind',
            verdict: 'admitted',
            stage: 'admitted',
            reasons: ['守护健康的样本外策略'],
            status: 'healthy',
            baseline_kind: 'mining_gate',
            degradation_pct: 0.03,
            last_checked_at: '2026-11-17T08:00:00+00:00',
            last_verdict: 'healthy',
            in_ledger: false,
            in_paper: false,
            regime: 'headwind',
          },
        ],
      },
    }),
  )
  const text = await render('factor')
  expect(text).toContain('β 逆风')
  expect(text).toContain('广度 41%')
  // 准入仍然通过: 环境不是策略的错
  expect(text).toContain('准入 1/1')
})

it('展开后必须写明「环境只描述、不阻断」，并把该状态的历史前瞻一起给出', async () => {
  vi.mocked(api.labReport).mockResolvedValue(report({ market: MARKET_OFF }))
  await render('factor')
  const toggle = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('详情'))
  await act(async () => toggle!.click())
  const text = host.textContent ?? ''
  expect(text).toContain('市场环境')
  expect(text).toContain('β 暴露本身是负期望')
  // 只写「逆风」不写「该状态 20 日后中位 -0.11%」会被读成「要跌了」
  expect(text).toContain('该状态历史 20 日后中位')
  expect(text).toContain('-0.11%')
  expect(text).toContain('仅描述环境，不否决准入')
})

it('环境未知时不显示 β 结论，避免把「读不到」当成「没问题」', async () => {
  vi.mocked(api.labReport).mockResolvedValue(
    report({ market: { ...MARKET_OFF, state: 'unknown', breadth_20d: null, eq_ma20_bp: null } }),
  )
  const text = await render('factor')
  expect(text).not.toContain('β 逆风')
  expect(text).not.toContain('β 正常')
})

it('有持仓拥挤度时给出读数，而不是只说「环境差」', async () => {
  vi.mocked(api.labReport).mockResolvedValue(
    report({
      market: MARKET_OFF,
      crowding: {
        available: true,
        quantile: 0.8,
        crowded_share: 0.2,
        note: '换手率前 20% 的股票占 20.0%；近 120 日该组中位 -36.5% vs 最低组 -25.8%',
      },
      admission: {
        total: 1,
        admitted: 1,
        blocked: 0,
        items: [
          {
            strategy_id: 'mined_crowded',
            verdict: 'admitted',
            stage: 'admitted',
            reasons: ['守护健康的样本外策略'],
            status: 'healthy',
            baseline_kind: 'mining_gate',
            degradation_pct: 0.02,
            last_checked_at: '2026-11-17T08:00:00+00:00',
            last_verdict: 'healthy',
            in_ledger: false,
            in_paper: false,
            regime: 'headwind',
            crowding: {
              pct: 0.86,
              max_pct: 0.99,
              covered: 20,
              card_as_of: '2026-11-17',
              card_id: 'dc-1',
              crowded: true,
            },
          },
        ],
      },
    }),
  )
  await render('factor')
  const toggle = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('详情'))
  await act(async () => toggle!.click())
  const text = host.textContent ?? ''
  expect(text).toContain('持仓拥挤度最高 86%')
  expect(text).toContain('主要在高换手区选股')
  // 拥挤度是暴露读数，不能变成第二条阻断理由
  expect(text).toContain('准入 1/1')
})

/* ---------- 主动搜索调度：还会不会继续产出 ---------- */

async function openDetail() {
  const toggle = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('详情'))
  await act(async () => toggle!.click())
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

it('展开后给出调度计划与上次跑的时间，不只在折叠态报战果', async () => {
  vi.mocked(api.labReport).mockResolvedValue(report())
  await render('factor')
  await openDetail()
  const text = host.textContent ?? ''
  expect(text).toContain('主动搜索调度')
  expect(text).toContain('每月 1 日')
  expect(text).toContain('360 试验')
  expect(text).toContain('alpha-2026-11')
  expect(text).toContain('空闲')
})

it('从未触发时说的是「从未触发」，不是「失败」', async () => {
  vi.mocked(api.labAlphaStatus).mockResolvedValue({
    config: { enabled: true, alpha_search_day: 5, alpha_search_profile: 'balanced', alpha_search_max_formulas: 120 },
    schedule: { latest_claim: null, latest: null, running: false },
    latest: null,
  })
  vi.mocked(api.labReport).mockResolvedValue(report())
  await render('factor')
  await openDetail()
  const text = host.textContent ?? ''
  expect(text).toContain('从未触发')
  expect(text).not.toContain('失败：')
})

it('失败任务的错误必须写出来，不能只显示 status=failed', async () => {
  vi.mocked(api.labAlphaStatus).mockResolvedValue({
    config: { enabled: true, alpha_search_day: 1, alpha_search_profile: 'balanced', alpha_search_max_formulas: 360 },
    schedule: {
      latest_claim: 'alpha-2026-11',
      latest: { status: 'failed', started_at: '2026-11-01T18:00:00', error: 'RuntimeError: batch failed' },
      running: false,
    },
    latest: null,
  })
  vi.mocked(api.labReport).mockResolvedValue(report())
  await render('factor')
  await openDetail()
  const text = host.textContent ?? ''
  expect(text).toContain('失败')
  expect(text).toContain('batch failed')
})

it('关闭状态下明说「已关闭（手工仍可触发）」，不装作在跑', async () => {
  vi.mocked(api.labAlphaStatus).mockResolvedValue({
    config: { enabled: false, alpha_search_day: 1, alpha_search_profile: 'balanced', alpha_search_max_formulas: 360 },
    schedule: { latest_claim: null, latest: null, running: false },
    latest: null,
  })
  vi.mocked(api.labReport).mockResolvedValue(report())
  await render('factor')
  await openDetail()
  expect(host.textContent ?? '').toContain('已关闭（手工仍可触发）')
})

it('正在跑时按钮禁用 —— 同一时间只允许一个任务', async () => {
  vi.mocked(api.labAlphaStatus).mockResolvedValue({
    config: { enabled: true, alpha_search_day: 1, alpha_search_profile: 'balanced', alpha_search_max_formulas: 360 },
    schedule: { latest_claim: 'alpha-2026-11', latest: { status: 'running' }, running: true, running_claim: 'alpha-2026-11' },
    latest: null,
  })
  vi.mocked(api.labReport).mockResolvedValue(report())
  await render('factor')
  await openDetail()
  const text = host.textContent ?? ''
  expect(text).toContain('正在跑')
  const runBtn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('现在跑一次'))
  expect(runBtn).toBeTruthy()
  expect((runBtn as HTMLButtonElement).disabled).toBe(true)
})

it('点「现在跑一次」真的发请求，并把 claim 回显给人', async () => {
  vi.mocked(api.labReport).mockResolvedValue(report())
  await render('factor')
  await openDetail()
  const runBtn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('现在跑一次'))
  await act(async () => { runBtn!.click() })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  expect(api.labAlphaRun).toHaveBeenCalledTimes(1)
  expect(host.textContent ?? '').toContain('alpha-manual-20261118-165000')
})

it('全部跳过时说「本次无新试验」，不说成「0 主题」', async () => {
  // evaluated=0 + skipped_prior>0 = 同窗口的试验被去重了，不是市场没信号。
  vi.mocked(api.labAlphaStatus).mockResolvedValue({
    config: { enabled: true, alpha_search_day: 1, alpha_search_profile: 'balanced', alpha_search_max_formulas: 360 },
    schedule: {
      latest_claim: 'alpha-manual-20261118-170456',
      latest: { status: 'done', started_at: '2026-11-18T17:04:56',
                counts: { evaluated: 0, skipped_prior: 360, confirmed_themes: 0 } },
      running: false,
    },
    latest: null,
  })
  vi.mocked(api.labReport).mockResolvedValue(report())
  await render('factor')
  await openDetail()
  const text = host.textContent ?? ''
  expect(text).toContain('本次无新试验')
  expect(text).toContain('同窗口已跑过')
  expect(text).not.toContain('0 试验 → 0 主题')
})
