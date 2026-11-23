// @vitest-environment node
/**
 * 个股判据的「结构闸门」。
 *
 * 这一层最容易出的不是算错，而是**少一条**：某只票没有龙虎榜，后端就少返回一条，
 * 页面于是只画 8 格——没有报错、没有空白提示，读者只会以为「本来就只有 8 条」。
 * 所以这里的断言不是覆盖率，而是把「九条必须永远是九条」「缺的那条要能说出原因」
 * 这两件事钉死在类型与测试里。
 *
 * 后半段测的是「缺数据」与「零」不能互相顶替：no_data 要显示原因，
 * 不能因为 value 是 null 就渲染成 0（0 会被读成「没有风险」）。
 */
import { describe, expect, it } from 'vitest'
import type { BoardRule, TableGameBoard } from '@/lib/api'
import { KNOWLEDGE } from './knowledge'
import {
  PROBES, PROBE_ORDER, probeItemId, probeLookup, probeSpec, probeTally, probeValueText,
} from './probes'

const rule = (over: Partial<BoardRule> & { id: string }): BoardRule => ({
  label: over.id,
  claim: 'claim',
  state: 'clear',
  ...over,
})

const board = (rules: BoardRule[], over: Partial<TableGameBoard> = {}): TableGameBoard => ({
  symbol: '600000.SH',
  state: 'ok',
  as_of: '2026-11-17',
  meta: {
    symbol: '600000.SH', name: '测试股', close: 10, float_cap: 1e9,
    cap_rank: 50, roe: 5, turnover_rate: 2, amount: 1e8,
  },
  events: [],
  rules,
  ...over,
})

/** 生成「合规」的九条：全部 clear。 */
const nineRules = () => PROBE_ORDER.map(id => rule({ id }))

describe('probes: 契约与索引', () => {
  it('恰好九条，且 id 不重复', () => {
    expect(PROBE_ORDER).toHaveLength(9)
    expect(new Set(PROBE_ORDER).size).toBe(9)
  })

  it('每条都有 label / question / needs（空字符串会让界面出现没有标题的格子）', () => {
    for (const p of PROBES) {
      expect(p.label.trim().length, `${p.id} 缺 label`).toBeGreaterThan(0)
      expect(p.question.trim().length, `${p.id} 缺 question`).toBeGreaterThan(6)
      expect(p.needs.trim().length, `${p.id} 缺 needs`).toBeGreaterThan(4)
    }
  })

  it('probeSpec 对未知 id 抛错（而不是返回 undefined 让界面渲染空壳）', () => {
    // @ts-expect-error 故意传非法 id
    expect(() => probeSpec('nope')).toThrow()
  })

  it('九条判据每条都被一条常识挂载，且只挂一条（点判定 → 跳条目必须一对一）', () => {
    for (const id of PROBE_ORDER) {
      const owners = KNOWLEDGE.filter(k => k.probe === id)
      expect(owners.length, `判据 ${id} 挂了 ${owners.length} 条常识`).toBe(1)
      expect(probeItemId(id)).toBe(owners[0].id)
    }
  })

  it('条目引用的 probe 都在 PROBE_ORDER 里（拼错会静默变成「没有判据」）', () => {
    const known = new Set<string>(PROBE_ORDER)
    const bad = KNOWLEDGE.filter(k => k.probe && !known.has(k.probe))
    expect(bad.map(k => `${k.id}:${k.probe}`)).toEqual([])
  })
})

describe('probes: lookup 的三态', () => {
  it('没有数据源时给 unavailable + 原因，而不是 null', () => {
    for (const id of PROBE_ORDER) {
      const r = probeLookup(undefined, id)
      expect(r.kind).toBe('unavailable')
      if (r.kind === 'unavailable') expect(r.reason.length).toBeGreaterThan(4)
    }
  })

  it('票不在本地库里 → unavailable 且说明是在市名单问题', () => {
    const r = probeLookup(board([], { state: 'not_in_universe' }), 'accum')
    expect(r.kind).toBe('unavailable')
    if (r.kind === 'unavailable') expect(r.reason).toContain('本地日线')
  })

  it('后端少返回一条时不许静默：必须报「契约缺一条」', () => {
    const partial = nineRules().filter(r => r.id !== 'divergence')
    const r = probeLookup(board(partial), 'divergence')
    expect(r.kind).toBe('unavailable')
    if (r.kind === 'unavailable') expect(r.reason).toContain('契约缺一条')
  })

  it('正常路径把后端那条原样交出来（不在前端改状态）', () => {
    const rules = nineRules().map(r => (r.id === 'accum' ? { ...r, state: 'triggered' as const } : r))
    const r = probeLookup(board(rules), 'accum')
    expect(r.kind).toBe('rule')
    if (r.kind === 'rule') expect(r.rule.state).toBe('triggered')
  })
})

describe('probes: 计数口径', () => {
  it('九条里少一条 → missing 计 1，总数仍是 9（页面不会变成 8 格）', () => {
    const t = probeTally(board(nineRules().filter(r => r.id !== 'knife')))
    expect(t.total).toBe(9)
    expect(t.missing).toBe(1)
    expect(t.hit + t.clear + t.derived + t.blind + t.missing).toBe(9)
  })

  it('四种状态各自入账，no_data 不计入「不成立」', () => {
    const rules = nineRules().map((r, i) => {
      if (i === 0) return { ...r, state: 'triggered' as const }
      if (i === 1) return { ...r, state: 'derived' as const }
      if (i === 2) return { ...r, state: 'no_data' as const }
      return r
    })
    const t = probeTally(board(rules))
    expect(t).toMatchObject({ total: 9, hit: 1, derived: 1, blind: 1, clear: 6, missing: 0 })
  })
})

describe('probes: 数值渲染', () => {
  it('null 渲染成「—」，不是 0（0 会被读成「没有」）', () => {
    expect(probeValueText(rule({ id: 'x', value: null }))).toBe('—')
    expect(probeValueText(rule({ id: 'x' }))).toBe('—')
  })

  it('整数不带小数点，小数保留两位（斜率 53.4 不该显示成 53.40 或 5340）', () => {
    expect(probeValueText(rule({ id: 'x', value: 3 }))).toBe('3')
    expect(probeValueText(rule({ id: 'x', value: 53.42 }))).toBe('53.42')
  })

  it('字符串值原样保留（跌停日之类由后端定格式）', () => {
    expect(probeValueText(rule({ id: 'x', value: '2026-11-16' }))).toBe('2026-11-16')
  })
})
