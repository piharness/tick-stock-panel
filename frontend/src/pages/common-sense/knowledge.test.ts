// @vitest-environment node
/**
 * 知识库与实时锚点的「结构闸门」。
 *
 * 这个文件存在的理由不是覆盖率，而是防止三类会**静默**发生的事故：
 *   1. 关联指向一条不存在的条目 —— UI 上表现为「点了没反应」，没人会报 bug；
 *   2. 条目的 anchor 写了不存在的 id —— UI 上表现为「今日无数据」，看起来像上游挂了；
 *   3. change_pct 被当成百分数用 —— 阈值差 100 倍，得出的却是「市场结构」。
 * 第 3 条本项目已经付过一次学费，所以这里用一个小样本把它钉死。
 */
import { describe, expect, it } from 'vitest'
import type { MarketSnapshotRow } from '@/lib/api'
import { ALL_ANCHORS, computeAnchor, FRICTION_RATE } from './anchors'
import { KNOWLEDGE, LAYERS } from './knowledge'

const ids = new Set(KNOWLEDGE.map(k => k.id))
const anchorIds = new Set<string>(ALL_ANCHORS)

const row = (over: Partial<MarketSnapshotRow>): MarketSnapshotRow => ({
  symbol: '600000.SH',
  name: '浦发银行',
  close: 10,
  change_pct: 0,
  amount: 1e8,
  turnover_rate: 1,
  market_cap: 3e10,
  consecutive_limit_ups: 0,
  ...over,
})

const ctx = (rows: MarketSnapshotRow[]) => ({ rows, asOf: '2026-11-17' })

describe('knowledge: 图结构完整性', () => {
  it('id 唯一', () => {
    expect(ids.size).toBe(KNOWLEDGE.length)
  })

  it('每条关联都指向真实存在的条目，且不自指', () => {
    const dangling: string[] = []
    for (const k of KNOWLEDGE) {
      for (const r of k.related) {
        if (!ids.has(r.to)) dangling.push(`${k.id} → ${r.to}`)
        if (r.to === k.id) dangling.push(`${k.id} 自指`)
      }
    }
    expect(dangling).toEqual([])
  })

  it('五层每层至少 6 条（体系不允许有空层）', () => {
    for (const l of LAYERS) {
      const n = KNOWLEDGE.filter(k => k.layer === l.id).length
      expect(n, `层「${l.label}」只有 ${n} 条`).toBeGreaterThanOrEqual(6)
    }
  })

  it('层序号连续从 1 开始，且 toNext 都写了（层间因果不能断）', () => {
    LAYERS.forEach((l, i) => {
      expect(l.index).toBe(i + 1)
      expect(l.toNext.length).toBeGreaterThan(10)
      expect(l.question.length).toBeGreaterThan(4)
    })
  })

  it('每条都写了「常见误解」与「自查入口」——缺一样就退回成话术', () => {
    const bad = KNOWLEDGE.filter(k => !k.misconception?.trim() || !k.check?.trim() || !k.basis?.trim())
    expect(bad.map(k => k.id)).toEqual([])
  })

  it('硬规则条目必须有生效时间（规则会过时，不标就是埋雷）', () => {
    const bad = KNOWLEDGE.filter(k => k.certainty === 'hard-rule' && !k.effective)
    expect(bad.map(k => k.id)).toEqual([])
  })
})

describe('knowledge ↔ anchors: 双向索引', () => {
  it('条目引用的 anchor 都真实存在', () => {
    const bad = KNOWLEDGE.filter(k => k.anchor && !anchorIds.has(k.anchor))
    expect(bad.map(k => `${k.id}:${k.anchor}`)).toEqual([])
  })

  it('每个 anchor 至少被一条常识引用（算了却没人用的数字不许上屏）', () => {
    const used = new Set(KNOWLEDGE.map(k => k.anchor).filter(Boolean))
    const orphan = ALL_ANCHORS.filter(a => !used.has(a))
    expect(orphan).toEqual([])
  })

  it('「今日事实」的点击映射不会落空：每个 anchor 都能找到第一条引用它的常识', () => {
    for (const a of ALL_ANCHORS) {
      expect(KNOWLEDGE.find(k => k.anchor === a), `anchor ${a} 无对应条目`).toBeTruthy()
    }
  })
})

describe('anchors: 口径与单位', () => {
  it('空表返回 null，而不是 0（0 会被读成「没有风险」）', () => {
    for (const a of ALL_ANCHORS) {
      expect(computeAnchor(a, ctx([])), `${a} 在空表上返回了非 null`).toBeNull()
    }
  })

  it('change_pct 是小数：10% 的涨跌幅必须渲染成 10.00%，不是 0.10%', () => {
    const rows = [row({ symbol: '600001.SH', name: 'ST测试', change_pct: 0.1 })]
    const a = computeAnchor('st-move', ctx(rows))
    expect(a?.value).toBe('10.00%')
  })

  it('5% 涨跌停指纹：只要有一只 ST 越界，锚点必须报出来', () => {
    const rows = [
      row({ symbol: '600001.SH', name: 'ST甲', change_pct: -0.0601 }),
      row({ symbol: '600002.SH', name: 'ST乙', change_pct: 0.02 }),
    ]
    const a = computeAnchor('st-move', ctx(rows))
    expect(a?.read).toContain('超过 5%')
    expect(a?.tone).toBe('danger')
  })

  it('涨跌家数与中位数不对「全部上涨」说谎', () => {
    const rows = [
      row({ symbol: '600001.SH', change_pct: 0.01 }),
      row({ symbol: '600002.SH', change_pct: -0.03 }),
      row({ symbol: '600003.SH', change_pct: -0.05 }),
    ]
    const a = computeAnchor('breadth', ctx(rows))
    expect(a?.value).toBe('1 / 2')
    expect(a?.read).toContain('-3.00%')
  })

  it('市值红线按板块分档：创业板 3 亿不算破线，主板 3 亿算', () => {
    const rows = [
      row({ symbol: '300001.SZ', name: '创业票', market_cap: 3.2e8 }),
      row({ symbol: '600001.SH', name: '主板票', market_cap: 3.2e8 }),
    ]
    const a = computeAnchor('mcap-line', ctx(rows))
    expect(a?.value).toBe('1')
    expect(a?.read).toContain('主板票')
  })

  it('面值线只数严格小于 1 元的，等于 1 元不算', () => {
    const rows = [
      row({ symbol: '600001.SH', close: 1.0 }),
      row({ symbol: '600002.SH', close: 0.99 }),
    ]
    expect(computeAnchor('par-line', ctx(rows))?.value).toBe('1')
  })

  it('摩擦成本随成交额线性变化，费率就是 FRICTION_RATE', () => {
    const rows = [row({ amount: 1e12 })]
    const a = computeAnchor('friction', ctx(rows))
    expect(a?.value).toBe(((1e12 * FRICTION_RATE) / 1e8).toFixed(1))
    expect(a?.sample).toContain('0.10%')
  })

  it('涨停只认后端连板列，不靠 change_pct 重新判定', () => {
    // 涨 10% 但后端说不是涨停（例如新股无涨跌幅）→ 不该被算成涨停
    const rows = [
      row({ symbol: '600001.SH', change_pct: 0.1, consecutive_limit_ups: 0 }),
      row({ symbol: '600002.SH', change_pct: 0.02, consecutive_limit_ups: 3 }),
    ]
    const a = computeAnchor('limit-up', ctx(rows))
    expect(a?.value).toBe('1 / 3')
  })

  it('每个锚点都给出可核对的口径（N + 列名 + 日期）', () => {
    const rows = [
      row({ symbol: '600001.SH', change_pct: 0.01, amount: 2e8, turnover_rate: 2 }),
      row({ symbol: '300001.SZ', change_pct: -0.01, amount: 5e6, turnover_rate: 8, market_cap: 1e8 }),
    ]
    for (const a of ALL_ANCHORS) {
      const r = computeAnchor(a, ctx(rows))
      if (!r) continue
      expect(r.sample, `${a} 的 sample 缺日期`).toContain('2026-11-17')
      expect(r.sample, `${a} 的 sample 缺样本量`).toContain('N=')
      expect(r.read.length, `${a} 的读法太短`).toBeGreaterThan(10)
    }
  })
})
