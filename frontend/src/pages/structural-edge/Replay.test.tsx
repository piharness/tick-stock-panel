// @vitest-environment jsdom
// 历史回放三块：单日快照、六只轨迹、首次触发后的效果。
// 测试只钉「页面把三类问题分开回答」以及「换日期会换快照」，不重复测 scan 的数值。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { EtfWatchIndustry, EtfWatchReplay, EtfWatchStatus, EtfWatchTrack } from '@/lib/api'
import { ReplayPanel } from './Replay'

const fake = vi.hoisted(() => ({ fail: false, payload: null as unknown as EtfWatchReplay }))

vi.mock('@/lib/api', () => ({
  api: {
    etfWatchReplay: async () => {
      if (fake.fail) throw new Error('404 Not Found')
      return fake.payload
    },
  },
}))

const DAYS = ['2026-11-13', '2026-11-16', '2026-11-17', '2026-11-18', '2026-11-19', '2026-11-20']

function ind(status: EtfWatchStatus, best = '科创半导体ETF华夏'): EtfWatchIndustry {
  return {
    industry: '电子', dd: -0.42, vol_pct: 0.96, stale_days: 0, n_etf: 30,
    best_symbol: '588170.SH', best_name: best, amt20: 7.8e9,
    r1: status !== 'NEUTRAL', r2: status !== 'BLOCKED', r3: true, f6: true,
    status, reasons: status === 'CANDIDATE' ? [] : ['F2 低波阴跌（volpct 52%）'],
  }
}

function snapshot(day: string, status: EtfWatchStatus, state: string) {
  return {
    meta: {
      asof: day, generated_at: '2026-11-21 12:00:00', mode: 'replay', note: '',
      dd_convention: '行业净值距 250 日高点', vol_convention: '20 日波动的 500 日分位',
      thresholds: { low: -0.35, deep: -0.5, vol_hi: 0.66, min_amt20: 5e7, cooldown: 60, hold: 20 },
      replay: true, rule_version: 'F1/F2/F3/F6 @ 2026-11-22',
    },
    market: { state, hs300_close: 3954.57, ma60: 4000, vol_pct: 0.41, trend_up: false, f3_active: false },
    counts: { industries: 20, in_band: 8, signal: 6, candidate: status === 'CANDIDATE' ? 6 : 3, deep: 1, clusters_today: 1, note: '' },
    industries: [ind(status)],
  }
}

function buildPayload(): EtfWatchReplay {
  const tracks: EtfWatchTrack[] = [{
    industry: '电子', name: '科创半导体ETF华夏', symbol: '588170.SH',
    first_trigger: '2026-11-16', last_trigger: '2026-11-20', trigger_count: 4,
    max_consecutive: 3, left_censored: false,
    carrier_at_first: '科创半导体ETF华夏', carrier_at_first_symbol: '588170.SH',
    carrier_changed: false, segments: [{ start: '2026-11-16', end: '2026-11-20', days: 3 }],
    days: DAYS.map(d => ({
      date: d, status: (d === '2026-11-13' ? 'NEUTRAL' : 'CANDIDATE') as EtfWatchStatus,
      dd: -0.42, vol_pct: 0.96, amt20: 7.8e9, best_symbol: '588170.SH',
      best_name: '科创半导体ETF华夏', r1: true, r2: true, r3: true, f6: true,
      reasons: [], market_state: '下行·高波',
    })),
  }]
  const outcomes = [{
    industry: '电子', name: '科创半导体ETF华夏', symbol: '588170.SH',
    first_trigger: '2026-11-16', entry: '2026-11-17', carrier: '科创半导体ETF华夏',
    to_end: 0.04, entry_to_end: 0.03, ret5: 0.10, ret10: 0.12, ret20: 0.15,
    mae: -0.02, mfe: 0.18, mae_close: -0.005, mfe_close: 0.17, note: '—',
  }]
  return {
    meta: {
      generated_at: '2026-11-21 12:00:00', end: '2026-11-20', start: '2026-11-13',
      sessions: 6, lookback: 6, rule_version: 'F1/F2/F3/F6 @ 2026-11-22',
      mode: 'replay', note: '', dd_convention: '行业净值距 250 日高点',
      vol_convention: '20 日波动的 500 日分位',
      thresholds: { low: -0.35, deep: -0.5, vol_hi: 0.66, min_amt20: 5e7, cooldown: 60, hold: 20 },
      mae_convention: '盘中极值：进场日之后的日线 low/high',
      mae_gap: { n: 1, median_delta_bp: -150, worst: { industry: '电子', mae: -0.02, mae_close: -0.005, delta_bp: -150 } },
      point_in_time: { liquidity: '当日 amt20', industry_classification: '当前名称映射' },
    },
    sessions: DAYS,
    snapshots: {
      '2026-11-13': snapshot('2026-11-13', 'NEUTRAL', '下行·高波'),
      '2026-11-16': snapshot('2026-11-16', 'CANDIDATE', '上行·低波'),
      '2026-11-17': snapshot('2026-11-17', 'CANDIDATE', '下行·高波'),
      '2026-11-18': snapshot('2026-11-18', 'CANDIDATE', '下行·高波'),
      '2026-11-19': snapshot('2026-11-19', 'CANDIDATE', '下行·高波'),
      '2026-11-20': snapshot('2026-11-20', 'CANDIDATE', '下行·低波'),
    },
    tracks,
    outcomes,
  }
}

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  fake.fail = false
  fake.payload = buildPayload()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

async function render() {
  await act(async () => { root.render(<ReplayPanel active />) })
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
  return host
}

it('加载后同时给出单日快照、信号轨迹与买点效果', async () => {
  const el = await render()
  const text = el.textContent ?? ''
  expect(text).toContain('单日回放：那一天收盘能看见什么')
  expect(text).toContain('候选行业')
  expect(text).toContain('科创半导体ETF华夏')
  expect(text).toContain('信号轨迹：从哪一天开始满足')
  expect(text).toContain('首次触发')
  expect(text).toContain('2026-11-16')
  expect(text).toContain('买点效果：首次触发之后发生了什么')
  expect(text).toContain('+10.0%')  // 5 日
  expect(text).toContain('+15.0%')  // 20 日
  expect(text).toContain('MAE 盘中')
  expect(text).toContain('收盘 −0.5%')   // 盘中口径旁的收盘口径审计值
  expect(text).toContain('口径差')
})

it('换交易日期会换单日快照', async () => {
  const el = await render()
  const select = el.querySelector('select') as HTMLSelectElement
  expect(select).not.toBeNull()
  await act(async () => {
    select.value = '2026-11-16'
    select.dispatchEvent(new Event('change', { bubbles: true }))
  })
  expect(el.textContent).toContain('上行·低波')
})

it('回放未生成时给出命令，不显示空表', async () => {
  fake.fail = true
  const el = await render()
  expect(el.textContent).toContain('历史回放未生成')
  expect(el.textContent).toContain('etf_watch_replay.py')
})
