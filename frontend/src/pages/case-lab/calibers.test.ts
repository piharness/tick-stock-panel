// @vitest-environment node
/**
 * 口径纯函数的单测 —— 锁住三类「看起来对、实际差一天/差一个数量级」的错。
 *
 * 1. 日期加减：`new Date('...+08:00')` + `setDate` + `toISOString` 会少一天。
 *    本项目已经在北京时区的「今天」上踩过一次，这里把跨月、跨年、闰日都锁住。
 * 2. 涨跌配色：A 股红涨绿跌，0 与缺失必须是中性色（不能因为 `v > 0` 为 false 就染绿）。
 * 3. 代码后缀补全：只做格式归一，不做校验；猜错时后端会如实返回 not_in_universe。
 */
import { describe, expect, it } from 'vitest'
import { bp, cnToday, normalizeSymbol, pctSigned, ratioPct, shiftDate, toneCls } from './calibers'

describe('cnToday: 北京日历', () => {
  it('返回 YYYY-MM-DD 且是合法日期', () => {
    const s = cnToday()
    expect(s).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    expect(Number.isNaN(Date.parse(`${s}T00:00:00Z`))).toBe(false)
  })
})

describe('shiftDate: 纯日期加减', () => {
  it('跨月不丢一天', () => {
    expect(shiftDate('2026-11-16', -120)).toBe('2026-07-19')
  })

  it('正负都对称', () => {
    expect(shiftDate('2026-03-01', -1)).toBe('2026-02-28')
    expect(shiftDate('2026-02-28', 1)).toBe('2026-03-01')
  })

  it('闰年 2 月', () => {
    expect(shiftDate('2024-02-28', 1)).toBe('2024-02-29')
    expect(shiftDate('2024-02-29', 1)).toBe('2024-03-01')
  })

  it('跨年', () => {
    expect(shiftDate('2026-12-31', 1)).toBe('2027-01-01')
    expect(shiftDate('2027-01-01', -1)).toBe('2026-12-31')
  })
})

describe('normalizeSymbol: 6 位代码补后缀', () => {
  it('沪市', () => {
    expect(normalizeSymbol('600001')).toBe('600001.SH')
    expect(normalizeSymbol('688981')).toBe('688981.SH')
    expect(normalizeSymbol('900901')).toBe('900901.SH')
  })

  it('深市', () => {
    expect(normalizeSymbol('000001')).toBe('000001.SZ')
    expect(normalizeSymbol('300750')).toBe('300750.SZ')
    expect(normalizeSymbol('200011')).toBe('200011.SZ')
  })

  it('北交所', () => {
    expect(normalizeSymbol('830799')).toBe('830799.BJ')
    expect(normalizeSymbol('430047')).toBe('430047.BJ')
  })

  it('已带后缀或非 6 位原样返回（只做格式归一，不做校验）', () => {
    expect(normalizeSymbol('002086.sz')).toBe('002086.SZ')
    // 带空格先 trim 再补后缀：输入框里手打空格是常态
    expect(normalizeSymbol(' 600001 ')).toBe('600001.SH')
    expect(normalizeSymbol('AAPL')).toBe('AAPL')
    expect(normalizeSymbol('')).toBe('')
  })
})

describe('涨跌配色：红涨绿跌', () => {
  it('正数红、负数绿', () => {
    expect(toneCls(1.2)).toBe('text-bull')
    expect(toneCls(-1.2)).toBe('text-bear')
  })

  it('0 与缺失是中性色，不能被染成绿色', () => {
    expect(toneCls(0)).toBe('text-secondary')
    expect(toneCls(null)).toBe('text-secondary')
    expect(toneCls(undefined)).toBe('text-secondary')
  })
})

describe('数值格式', () => {
  it('基点带符号，缺失不显示 0', () => {
    expect(bp(63.4)).toBe('+63.4bp')
    expect(bp(-58.8)).toBe('-58.8bp')
    expect(bp(null)).toBe('—')
  })

  it('百分数原值（后端已是 6.69 表示 6.69%）', () => {
    expect(pctSigned(6.69)).toBe('+6.69%')
    expect(pctSigned(-6.691)).toBe('-6.69%')
  })

  it('比例转百分数', () => {
    expect(ratioPct(0.5368)).toBe('54%')
    expect(ratioPct(null)).toBe('—')
  })
})
