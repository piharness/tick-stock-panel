import { describe, expect, it } from 'vitest'
import fixtures from './priceLimitFixtures.json'
import { boardLimitRate } from './price-limit'

/**
 * `boardLimitRate` 是后端 `app/price_limits.py::board_limit_pct` 的第二份实现
 * （分时图窄栏要画涨跌停带，不能为一个数发一次请求）。第二份的存在是合理的，
 * 但「注释里写着与后端一致」不是判据 —— 只有拿后端算出来的表逐项比才是。
 *
 * 基准表由 `python analysis/_verify_price_limit_fixtures.py --write` 生成，
 * 覆盖标的宇宙里每个板块前缀的首尾两只（含只有一只的 302 / 689 边角板块）。
 * 后端改了板块规则而没重跑生成脚本 → `backend/tests/test_price_limit_frontend_parity.py` 红；
 * 重跑了脚本但没改前端 → 这里红。
 */

type Fixture = { universe: number; groups: Record<string, { count: number; rate: number }>; cases: Record<string, number> }

const data = fixtures as unknown as Fixture
const cases = Object.entries(data.cases)

/** 与生成脚本同一分组键：北交所用 BJ，其余取前三位。 */
function prefixKey(symbol: string): string {
  return symbol.endsWith('.BJ') ? 'BJ' : symbol.slice(0, 3)
}

describe('boardLimitRate 与后端基准表逐项一致', () => {
  it('基准表非空，且每个板块前缀在用例里都有代表', () => {
    // 空表会让下面的逐项断言退化成空循环 —— 那时「全绿」只说明没测。
    expect(cases.length).toBeGreaterThan(0)
    const covered = new Set(cases.map(([symbol]) => prefixKey(symbol)))
    const declared = new Set(Object.keys(data.groups))
    expect([...declared].sort()).toEqual([...covered].sort())
  })

  it('板块前缀表里只有一个的边角板块也在（302 / 689 各只有一只）', () => {
    const singletons = Object.entries(data.groups).filter(([, g]) => g.count === 1).map(([k]) => k)
    for (const key of singletons) {
      expect(cases.some(([symbol]) => prefixKey(symbol) === key)).toBe(true)
    }
  })

  for (const [symbol, rate] of cases) {
    it(`${symbol} → ${rate}`, () => {
      expect(boardLimitRate(symbol)).toBeCloseTo(rate, 10)
    })
  }
})
