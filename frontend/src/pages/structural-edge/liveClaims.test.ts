// 活读数的契约测试：两件事，缺一不可。
//
//  1) 映射本身要对：V1 只数「已结算」、广度必须同时给出「区间内」与「四条全过」、
//     现金腿用数据日而不是生成时间、缺数据时返回 null 而不是编一个日期。
//  2) 这些读数不许被再抄回源码。这条不是风格检查，是事故检查 ——
//     「2026-11-20 广度 6」与当日快照 in_band=8 同时存在于页面上，
//     「2026-11-23 的候选方案」把生成时间当成了数据日。
//     任何一次手抄都会让同一句话有两个真相，所以这里直接禁止字面量。
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import type { EtfWatchAccount, EtfWatchCashLeg, EtfWatchPayload } from '@/lib/api'
import { accountReading, asOfDrift, breadthReading, cashLegReading, coverageGap, v1Reading } from './live'

const DIR = fileURLToPath(new URL('.', import.meta.url))
const read = (file: string) => readFileSync(DIR + file, 'utf8')

const payload = {
  meta: { asof: '2026-11-20', generated_at: '2026-11-22 13:47:37', mode: 'record-only' },
  market: { state: '下行·低波' },
  counts: { industries: 20, in_band: 8, signal: 6, candidate: 6, deep: 1, clusters_today: 1 },
  ledger: {
    events: [],
    settles: [],
    v1: {
      rule: '3 个事件簇', counting: '按自然月', left_tail_mapping: 'MAE',
      required: 3, settled_clusters: 0, signal_clusters: 1, progress: '0/3', state: 'WAITING',
    },
    low_vol_band_entries: 0,
  },
  data: { etf: {}, index: {}, lag_sessions: 0 },
} as unknown as EtfWatchPayload

const account = {
  meta: { adoption: '2026-11-20', init: 600000, generated_at: '2026-11-23 15:05:07' },
  asof: '2026-11-20',
  state: { equity: 600000, cash: 600000, defensive: { value: 0, weight: 0 }, leg1: { value: 0, positions: [] } },
} as unknown as EtfWatchAccount

const cashLeg = {
  meta: { asof: '2026-11-20', generated_at: '2026-11-23 15:05:09', policy: '短融打底 + 国债锚（R1/R2/R3）' },
  bond_w: 0.4,
} as unknown as EtfWatchCashLeg

/** 禁止回流的字面量：file + 正则 + 为什么。新增一条时把事故写进 why，别只写「不要这样写」。 */
const FORBIDDEN: Array<{ file: string; pattern: RegExp; why: string }> = [
  {
    file: 'data.ts',
    pattern: /0\s*\/\s*3/,
    why: 'V1 进度来自 payload.ledger.v1.settled_clusters；手抄一次，账本走到 1/3 时页面还写 0/3',
  },
  {
    file: 'data.ts',
    pattern: /广度\s*[0-9]/,
    why: '广度有两个口径（区间内 / 四条全过），来自 payload.counts；合成一个数就必然漂移',
  },
  {
    file: 'data.ts',
    pattern: /20\d{2}-\d{2}-\d{2}\s*启动/,
    why: '账户启动日来自 account.meta.adoption',
  },
  {
    file: 'Execution.tsx',
    pattern: /20\d{2}-\d{2}-\d{2}\s*的候选方案/,
    why: '现金腿的数据日是 meta.asof；meta.generated_at 只是生成时间，不是数据日',
  },
  {
    file: 'Execution.tsx',
    pattern: /自\s*20\d{2}-\d{2}-\d{2}\s*收盘/,
    why: '账户启动日来自 account.meta.adoption',
  },
]

describe('活读数不许手抄', () => {
  for (const rule of FORBIDDEN) {
    it(`${rule.file} 不含手抄读数 ${rule.pattern}`, () => {
      expect(read(rule.file), rule.why).not.toMatch(rule.pattern)
    })
  }
})

describe('live.ts 映射', () => {
  it('V1 只数已结算的簇，不把信号簇当进度', () => {
    const v1 = v1Reading(payload)
    expect(v1?.settled).toBe(0)
    expect(v1?.settled).not.toBe(payload.ledger.v1.signal_clusters)
    expect(v1?.progress).toBe('0 / 3')
  })

  it('V1 缺账本时返回 null，而不是 0/3', () => {
    expect(v1Reading(null)).toBeNull()
    expect(v1Reading({ ...payload, ledger: undefined } as unknown as EtfWatchPayload)).toBeNull()
  })

  it('广度同时给出区间内与四条全过，两个数不合并', () => {
    const b = breadthReading(payload)
    expect(b?.inBand).toBe(8)
    expect(b?.signal).toBe(6)
    expect(b?.text).toContain('区间内 8')
    expect(b?.text).toContain('四条全过 6')
  })

  it('广度缺快照时返回 null', () => {
    expect(breadthReading(null)).toBeNull()
  })

  it('账户读数带启动日与初始资金（万元）', () => {
    const a = accountReading(account)
    expect(a?.adoption).toBe('2026-11-20')
    expect(a?.initWan).toBe(60)
  })

  it('现金腿用数据日，不用生成时间', () => {
    const leg = cashLegReading(cashLeg)
    expect(leg?.asof).toBe('2026-11-20')
    expect(leg?.asof).not.toBe(cashLeg.meta.generated_at.slice(0, 10))
  })

  it('两个产出物数据日不同才报差异', () => {
    expect(asOfDrift('账户', '2026-11-20', '2026-11-20')).toBeNull()
    expect(asOfDrift('账户', '2026-11-20', '2026-11-21')).toBe('账户 2026-11-20 ≠ 扫描 2026-11-21')
  })

  it('分区已到 11-23、扫描停在 11-20 时报告链路缺口', () => {
    const stale = { ...payload, meta: { ...payload.meta, asof: '2026-11-20', store_last_session: '2026-11-23' } } as EtfWatchPayload
    const gap = coverageGap(stale)
    expect(gap?.storeLast).toBe('2026-11-23')
    expect(gap?.text).toContain('本地行情已到 2026-11-23')
    expect(gap?.text).toContain('扫描只到 2026-11-20')
  })

  it('扫描追上分区时不报警；没有该字段时也不猜', () => {
    const same = { ...payload, meta: { ...payload.meta, asof: '2026-11-23', store_last_session: '2026-11-23' } } as EtfWatchPayload
    expect(coverageGap(same)).toBeNull()
    expect(coverageGap(payload)).toBeNull()
  })
})
