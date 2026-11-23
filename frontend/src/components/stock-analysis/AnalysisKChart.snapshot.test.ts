// @vitest-environment node
/**
 * 图表里「未来信息」的两条边界。
 *
 * 这一段逻辑没有 DOM：它决定某一组价位线在时间轴上从哪一根画到哪一根。
 * jsdom 断言不了 canvas 长什么样，所以把判据抽成纯函数直接钉住：
 *
 *   1. 视窗停在历史段（showToday=false）→ 今日价位一条都不画。
 *      否则 2024 年那段 K 上会躺着用 2026 年数据算出的筹码密集区，
 *      而图上看不出这句话是"今天说的"。
 *   2. 视窗包含最新一根 → 只给最后两根赋值，画出来是贴右边缘的一小段，
 *      不是贯穿全屏的横线。
 */
import { describe, expect, it } from 'vitest'

import { snapshotLineData } from '@/components/stock-analysis/AnalysisKChart'

const dates = ['2024-01-02', '2024-01-03', '2024-01-04', '2026-11-16', '2026-11-17']

describe('snapshotLineData', () => {
  it('视窗不含最新一根时整条不画', () => {
    expect(snapshotLineData(dates, 1.02, false)).toEqual([null, null, null, null, null])
  })

  it('视窗包含最新一根时只画最后两根', () => {
    expect(snapshotLineData(dates, 1.02, true)).toEqual([null, null, null, 1.02, 1.02])
  })

  it('只有一根 K 时仍给值（单点画不出线段，但那是全部数据）', () => {
    expect(snapshotLineData(['2026-11-17'], 1.02, true)).toEqual([1.02])
  })

  it('长度与日期对齐 —— 少一个 null 就会让整条线左移一格', () => {
    expect(snapshotLineData(dates, 1.02, true)).toHaveLength(dates.length)
    expect(snapshotLineData(dates, 1.02, false)).toHaveLength(dates.length)
  })
})
