// @vitest-environment jsdom
// 账户指令卡：启用后今日页必须能回答「明天收盘做什么」。
// 快照缺失（404）不得炸页面；有快照时指令与状态必须可见。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { AccountCard } from './Execution'
import type { EtfWatchAccount } from '@/lib/api'

vi.mock('@/lib/api', () => ({
  api: { etfWatch: async () => { throw new Error('404') } },
}))
vi.mock('echarts', () => ({ init: () => ({ setOption: () => {}, resize: () => {}, dispose: () => {} }) }))

const account: EtfWatchAccount = {
  meta: {
    generated_at: '2026-11-22 16:00:00',
    policy: 'JB0：腿 1 六格 + 债券 B&H 吃剩余现金',
    engine: 'etf_watch_leg2_rule.replay_joint',
    adoption: '2026-11-20',
    init: 1_000_000,
    defensive: { symbol: '511010.SH', name: '511010 国债ETF国泰' },
    rules: {},
  },
  asof: '2026-11-20',
  state: {
    equity: 1_000_000,
    cash: 1_000_000,
    defensive: { value: 0, weight: 0 },
    leg1: { value: 0, positions: [] },
    trades_closed: 0,
  },
  instructions: [
    { no: 1, action: 'BUY', what: '159530.SZ 机器人ETF易方达（机械）', est_amount: 166_667,
      why: '腿 1 信号 2026-11-20 段首 · slot=权益/6', when: '下一交易日 09:30–14:50 限价',
      limit_price: 1.145 },
    { no: 2, action: 'BUY', what: '511010 国债ETF国泰', est_amount: 500_000,
      why: '债券 B&H：收盘后保持 债券市值 = 权益 − 腿 1 持仓', when: '腿 1 成交之后' },
    { no: 3, action: 'HOLD', what: '511010 国债ETF国泰', est_amount: 1_500,
      why: '差额低于 3,000 元对齐阈值，暂不动', when: '累计到阈值再对齐' },
  ],
  equity_tail: [],
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

it('渲染四项状态与全部指令', async () => {
  const el = await render(<AccountCard account={account} err="" />)
  const text = el.textContent!
  expect(text).toContain('权益')
  expect(text).toContain('1,000,000')
  expect(text).toContain('债券 511010')
  expect(text).toContain('159530.SZ 机器人ETF易方达（机械）')
  expect(text).toContain('166,667')
  expect(text).toContain('500,000')
  expect(text).toContain('买入')
  expect(text).toContain('限价 1.145')
  expect(text).toContain('执行层与 V1 判定层独立')
})

it('快照缺失时不炸页面，给出下一步命令', async () => {
  const el = await render(<AccountCard account={null} err="404 Not Found" />)
  const text = el.textContent!
  expect(text).toContain('etf_watch_account.py')
  expect(text).not.toContain('159530')
})
