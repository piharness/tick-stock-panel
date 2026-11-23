// @vitest-environment jsdom
// 现金腿卡：页面必须显示「按目前权益该买多少份」，且份数只能来自 cash_leg.json。
// 一旦页面自己再取整一次，同一笔单会在账户脚本、CLI、页面三处出现三个数字 —— 不报错，只在对账时变成缺口。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { CashLegCard } from './Execution'
import type { EtfWatchCashLeg } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: { etfWatch: async () => { throw new Error('404') } },
}))
vi.mock('echarts', () => ({ init: () => ({ setOption: () => {}, resize: () => {}, dispose: () => {} }) }))

const leg: EtfWatchCashLeg = {
  meta: {
    generated_at: '2026-11-23 11:30:00',
    asof: '2026-11-20',
    policy: '短融打底 + 国债锚（R1/R2/R3）',
    engine: 'analysis/etf_watch_cash_leg.py::plan（唯一计算点）',
    bond: { symbol: '511010.SH', name: '511010 国债ETF国泰', duration: 4.3, weight: 0.4 },
    short: { symbol: '511360.SH', name: '511360 短融ETF' },
    rules: {
      R1: '国债市值 = bond_w × 权益',
      R2: '短融市值 = 权益 − 腿1 − 国债 − 现金',
      R3: '缺钱先卖短融；短融不足 1 手才卖国债',
    },
    prices: {
      '511010.SH': { close: 141.411, asof: '2026-11-20' },
      '511360.SH': { close: 114.189, asof: '2026-11-20' },
    },
  },
  equity: 600_000,
  leg1: 0,
  available: 600_000,
  bond_w: 0.4,
  bond_target: 240_000,
  bond_amount: 240_399,
  short_amount: 353_986,
  residual: 5_615,
  deviation: 399,
  need_rebalance: false,
  rebalance_min: 3_000,
  duration_years: 1.72,
  orders: [
    { no: 1, action: 'BUY', symbol: '511010.SH', name: '511010 国债ETF国泰', shares: 1_700,
      price: 141.411, amount: 240_399, fee: 12.02, why: '40% 权益锚（久期 4.3）' },
    { no: 2, action: 'BUY', symbol: '511360.SH', name: '511360 短融ETF', shares: 3_100,
      price: 114.189, amount: 353_986, fee: 17.7, why: '吸收剩余（R2）' },
  ],
}

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: vi.fn() })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

async function render(node: React.ReactElement) {
  await act(async () => { root.render(node) })
  return host
}

it('四种状态与两条下单指令可见，份数/金额/佣金原样渲染', async () => {
  const el = await render(<CashLegCard leg={leg} err="" />)
  const text = el.textContent!
  expect(text).toContain('国债锚 40%')
  expect(text).toContain('240,399')
  expect(text).toContain('353,986')
  expect(text).toContain('1,700')
  expect(text).toContain('3,100')
  expect(text).toContain('12.02')
  expect(text).toContain('17.70')
  expect(text).toContain('1.72 年')
  expect(text).toContain('5,615')
})

it('三条资金规则在场，且直接来自 payload（页面不复制定义）', async () => {
  const el = await render(<CashLegCard leg={leg} err="" />)
  const text = el.textContent!
  expect(text).toContain('R1')
  expect(text).toContain(leg.meta.rules.R1)
  expect(text).toContain(leg.meta.rules.R2)
  expect(text).toContain(leg.meta.rules.R3)
})

it('换标的的入口写清楚：改脚本顶部参数，流程不动', async () => {
  const el = await render(<CashLegCard leg={leg} err="" />)
  const text = el.textContent!
  expect(text).toContain('etf_watch_cash_leg.py')
  expect(text).toContain('BOND')
  expect(text).toContain('BOND_W')
  expect(text).toContain('溢价')
})

it('快照缺失时不炸，给出会顺带产出它的那条命令', async () => {
  const el = await render(<CashLegCard leg={null} err="404 Not Found" />)
  const text = el.textContent!
  expect(text).toContain('etf_watch_account.py')
  expect(text).not.toContain('1,700')
})
