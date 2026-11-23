// @vitest-environment jsdom
/**
 * 退市红线面板 —— unknown 不能长得像 safe。
 *
 * 这一块的信息价值全在「区分四种状态」：已触发 / 正在累计 / 接近 / 算不出来。
 * 如果算不出来的项被渲染成灰色对勾，一只已退市、本地查不到行情的票就会显示
 * 成「一切正常」—— 这比不显示更危险，因为它给了确定感。
 *
 * 与 Page.test.tsx 同一种写法（createRoot + act），不引入新的测试库。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { DelistingGuardPanel } from '@/components/stock-analysis/DelistingGuardPanel'
import type { StockRiskGuard } from '@/lib/api'

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

function render(props: Parameters<typeof DelistingGuardPanel>[0]) {
  act(() => root.render(<DelistingGuardPanel {...props} />))
  return host.textContent ?? ''
}

const stGuard: StockRiskGuard = {
  symbol: '600340.SH', name: '*ST华幸', board: '主板', is_st: true, st_kind: '*ST',
  as_of: '2026-11-17',
  gates: [
    {
      id: 'par', label: '面值退市', rule: '连续 20 个交易日收盘价均低于 1.00 元',
      status: 'counting', value: 0.92, threshold: 1, distance_pct: -8,
      streak: 1, streak_limit: 20, detail: '已连续 1/20 个交易日低于面值',
    },
    {
      id: 'equity', label: '净资产为负', rule: '最近一个会计年度经审计期末净资产为负',
      status: 'triggered', value: -4.831e9, threshold: 0, distance_pct: null,
      streak: 1, streak_limit: null, detail: '2025-12-31 净资产 -48.31 亿元，已触发退市风险警示',
    },
  ],
  caveats: ['本面板只算规则距离，不给摘帽/退市概率'],
  counts: { verdict: '已触发净资产为负条件', equity_trend: '2026-09-30 净资产 -127.80 亿元' },
}

describe('DelistingGuardPanel', () => {
  it('ST 股展示面值倒计时与美元值', () => {
    const text = render({ data: stGuard, loading: false, error: false })
    expect(text).toContain('退市红线')
    expect(text).toContain('0.92 元')
    expect(text).toContain('1/20')
    expect(text).toContain('-48.31 亿')
    expect(text).toContain('已触发净资产为负条件')
  })

  it('不给概率的声明必须出现在页面上', () => {
    const text = render({ data: stGuard, loading: false, error: false })
    expect(text).toContain('不给摘帽/退市概率')
  })

  it('普通股四条全安全时不占版面', () => {
    const calm: StockRiskGuard = {
      ...stGuard, is_st: false, st_kind: '', counts: {},
      gates: [{ ...stGuard.gates[0], id: 'par', status: 'safe', value: 11.8, streak: 0, detail: '距面值红线 +1080%' }],
    }
    const text = render({ data: calm, loading: false, error: false })
    expect(text).toBe('')
  })

  it('接口失败不静默消失：要说明「不显示不等于没风险」', () => {
    const text = render({ data: undefined, loading: false, error: true })
    expect(text).toContain('退市红线数据不可用')
    expect(text).toContain('不等于没有风险')
  })

  it('全 unknown 时给出原因而不是判成安全', () => {
    const unknown: StockRiskGuard = {
      ...stGuard, counts: { verdict: '本地数据不足，无法判定' },
      gates: [{
        id: 'par', label: '面值退市', rule: '连续 20 个交易日收盘价均低于 1.00 元',
        status: 'unknown', value: null, threshold: 1, distance_pct: null,
        streak: null, streak_limit: 20, detail: '无日线数据',
      }],
    }
    const text = render({ data: unknown, loading: false, error: false })
    expect(text).toContain('本地数据不足，无法判定')
    expect(text).toContain('无日线数据')
    expect(text).not.toContain('未触及')
  })

  it('加载中先占一行，不闪烁', () => {
    const text = render({ data: undefined, loading: true, error: false })
    expect(text).toContain('正在算退市红线')
  })
})
