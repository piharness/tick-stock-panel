// @vitest-environment jsdom
/**
 * 时间轴口径 —— 这里每条断言都对应一个会让人看错图的错法。
 */
import { describe, expect, it } from 'vitest'
import {
  clampToDates, coverageGap, daysBetween, isValidIso, normalizeRange, panWindow,
  presetRange, rangeStats, shiftMonths, windowToIndex, zoomWindow,
} from './chartRange'

describe('预设区间：按日历月回溯，不是按天数拍', () => {
  it('1Y 回到去年同月同日（闰年 2-29 不越界）', () => {
    expect(presetRange('1Y', '2026-11-13')).toEqual({ start: '2025-11-13', end: '2026-11-13' })
    expect(presetRange('1Y', '2024-02-29')).toEqual({ start: '2023-02-28', end: '2024-02-29' })
  })

  it('月末不溢出：5-31 往前 3 个月是 2-29（闰年）而不是 3-02', () => {
    expect(shiftMonths('2024-05-31', -3)).toBe('2024-02-29')
    expect(shiftMonths('2026-05-31', -3)).toBe('2026-02-28')
    expect(shiftMonths('2026-03-31', -1)).toBe('2026-02-28')
  })

  it('2Y 是 24 个自然月，ALL/CUSTOM 没有默认区间', () => {
    expect(presetRange('2Y', '2026-11-13')).toEqual({ start: '2024-11-13', end: '2026-11-13' })
    expect(presetRange('ALL', '2026-11-13')).toBeNull()
    expect(presetRange('CUSTOM', '2026-11-13')).toBeNull()
  })
})

describe('区间边界：非法输入不静默变成 NaN 图', () => {
  it('日期格式与真实存在性都要过', () => {
    expect(isValidIso('2026-11-13')).toBe(true)
    expect(isValidIso('2026-02-30')).toBe(false)
    expect(isValidIso('2026-1-3')).toBe(false)
    expect(isValidIso('')).toBe(false)
  })

  it('起点晚于终点就交换，非法值退回默认', () => {
    const fb = { start: '2025-01-01', end: '2026-01-01' }
    expect(normalizeRange({ start: '2026-05-01', end: '2026-01-01' }, fb))
      .toEqual({ start: '2026-01-01', end: '2026-05-01' })
    expect(normalizeRange({ start: 'bad', end: '2026-01-01' }, fb).start).toBe('2025-01-01')
  })

  it('daysBetween 跨年不差一天', () => {
    expect(daysBetween('2025-12-31', '2026-01-01')).toBe(1)
    expect(daysBetween('2024-02-28', '2024-03-01')).toBe(2)
    expect(daysBetween('x', '2026-01-01')).toBeNull()
  })
})

describe('缺口：本地没铺到 ≠ 这只票没数据', () => {
  it('请求起点早于本地表起点 → table（可以去补历史）', () => {
    expect(coverageGap('2024-09-01', '2025-05-07', '2025-05-07'))
      .toEqual({ kind: 'table', days: 248 })
  })

  it('请求起点在覆盖内、首根仍然晚 → symbol（停牌/上市晚，补也没用）', () => {
    expect(coverageGap('2025-06-01', '2025-06-20', '2025-05-07'))
      .toEqual({ kind: 'symbol', days: 19 })
  })

  it('首根不晚于起点就没有缺口', () => {
    expect(coverageGap('2025-05-07', '2025-05-07', '2025-05-07').kind).toBe('none')
    expect(coverageGap('2024-01-01', null, '2025-05-07').kind).toBe('none')
  })
})

describe('区间统计：三个数必须同源（收盘），否则一屏自相矛盾', () => {
  it('涨跌幅是收盘→收盘，隔夜跳空不算成区间收益', () => {
    const s = rangeStats([
      { date: '2026-01-05', close: 10, high: 11, low: 9.5 },
      { date: '2026-01-06', close: 11, high: 11.2, low: 10.8 },
    ])
    expect(s.pct).toBeCloseTo(10, 6)      // 11/10-1，不是 11/10(open) 之类的混口径
    expect(s.n).toBe(2)
  })

  it('最大回撤按收盘序列的峰谷，方向为正', () => {
    const s = rangeStats([
      { date: 'd1', close: 10 }, { date: 'd2', close: 20 },
      { date: 'd3', close: 10 }, { date: 'd4', close: 15 },
    ])
    expect(s.maxDrawdown).toBeCloseTo(50, 6)   // 20 → 10
    expect(s.fromHigh).toBeCloseTo(-25, 6)     // 15 / 20 - 1
    expect(s.high).toBe(20)
    expect(s.highDate).toBe('d2')
    expect(s.low).toBe(10)
    expect(s.lowDate).toBe('d1')               // 先到先得，不取最后一个低点
  })

  it('单根 K 不报回撤（没有第二个点就没有回撤）', () => {
    const s = rangeStats([{ date: 'd1', close: 10 }])
    expect(s.maxDrawdown).toBeNull()
    expect(s.pct).toBeCloseTo(0, 6)
  })

  it('成交额缺失时不给 0，给 null', () => {
    expect(rangeStats([{ date: 'd1', close: 10 }, { date: 'd2', close: 11 }]).amount).toBeNull()
    expect(rangeStats([
      { date: 'd1', close: 10, amount: 1e8 },
      { date: 'd2', close: 11, amount: 2e8 },
    ]).amount).toBe(3e8)
  })

  it('空区间全 null，不抛', () => {
    expect(rangeStats([]).n).toBe(0)
    expect(rangeStats([]).pct).toBeNull()
  })
})

describe('区间端点裁剪：观测日可能落在图表数据之外', () => {
  const dates = ['2026-01-05', '2026-01-06', '2026-01-07', '2026-01-08']

  it('两端都越界时钳到首尾', () => {
    expect(clampToDates(dates, '2025-01-01', '2027-01-01')).toEqual(['2026-01-05', '2026-01-08'])
  })

  it('只有一端越界时只钳那一端', () => {
    expect(clampToDates(dates, '2026-01-06', '2027-01-01')).toEqual(['2026-01-06', '2026-01-08'])
    expect(clampToDates(dates, '2025-01-01', '2026-01-07')).toEqual(['2026-01-05', '2026-01-07'])
  })

  it('落在两根之间取外扩的邻根（不返回 null），空 dates 返回 null', () => {
    expect(clampToDates(dates, '2026-01-06', '2026-01-07')).toEqual(['2026-01-06', '2026-01-07'])
    expect(clampToDates([], '2026-01-06', '2026-01-07')).toBeNull()
  })
})

describe('视窗：平移与缩放的边界只有一处定义', () => {
  it('平移保持宽度，撞到左端就贴 0', () => {
    expect(panWindow({ start: 10, end: 40 }, -1)).toEqual({ start: 0, end: 30 })
  })

  it('平移撞到右端就贴 100，不会滑出数据之外', () => {
    expect(panWindow({ start: 80, end: 95 }, 1)).toEqual({ start: 85, end: 100 })
  })

  it('缩放以中心为锚；放到最宽就是 0~100', () => {
    expect(zoomWindow({ start: 40, end: 60 }, 2)).toEqual({ start: 30, end: 70 })
    expect(zoomWindow({ start: 40, end: 60 }, 10)).toEqual({ start: 0, end: 100 })
  })

  it('缩放有下限，不会缩成一根（minWidth 默认 2）', () => {
    const w = zoomWindow({ start: 40, end: 60 }, 0.001)
    expect(w.end - w.start).toBeCloseTo(2, 6)
  })

  it('视窗 → 行下标：两端向外取整，首末根都能算进去', () => {
    expect(windowToIndex({ start: 0, end: 100 }, 5)).toEqual([0, 4])
    expect(windowToIndex({ start: 50, end: 100 }, 5)).toEqual([2, 4])
    expect(windowToIndex({ start: 0, end: 0 }, 5)).toEqual([0, 0])
    expect(windowToIndex({ start: 0, end: 100 }, 0)).toEqual([0, 0])
  })
})
