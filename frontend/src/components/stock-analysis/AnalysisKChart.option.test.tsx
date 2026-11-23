// @vitest-environment jsdom
/**
 * 图表的「十字虚线」与「今日价位不铺满全图」—— 直接断言最终 option。
 *
 * 这两件事都发生在 canvas 里，jsdom 看不到像素；但它们是**配置**决定的：
 * 十字线靠 x/y 轴各一条 dashed axisPointer，今日价位靠 series 的 null 前缀。
 * 所以这里 mock 掉 echarts.init，把 setOption 收到的 option 接住检查。
 *
 * 为什么不用 ECharts 的 SSR 截图：那条路要额外依赖 canvas，且在 CI 里不稳定；
 * 而真正会回归的东西（某次重构把 lineStyle 改成默认实线、或把今日价位改回
 * 贯穿全图）在 option 层面一眼可见。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { KlineRow } from '@/lib/api'

const mockState = {
  option: null as any,
  zoomEnd: 100,
  handlers: {} as Record<string, () => void>,
}
const captured = mockState

vi.mock('echarts', () => ({
  init: () => ({
    setOption: (o: any) => { mockState.option = o },
    on: (ev: string, fn: () => void) => { mockState.handlers[ev] = fn },
    getOption: () => ({ dataZoom: [{ start: 0, end: mockState.zoomEnd }] }),
    dispatchAction: () => {},
    resize: () => {},
    dispose: () => {},
  }),
}))

const { AnalysisKChart } = await import('@/components/stock-analysis/AnalysisKChart')

const rows: KlineRow[] = [
  { date: '2026-11-11', open: 1.1, high: 1.12, low: 1.08, close: 1.12, volume: 1e5, amount: 1e8 },
  { date: '2026-11-12', open: 1.1, high: 1.11, low: 1.07, close: 1.1, volume: 1e5, amount: 1e8 },
  { date: '2026-11-13', open: 1.08, high: 1.09, low: 1.06, close: 1.08, volume: 1e5, amount: 1e8 },
  { date: '2026-11-16', open: 1.02, high: 1.03, low: 1.0, close: 1.02, volume: 1e5, amount: 1e8 },
  { date: '2026-11-17', open: 0.95, high: 0.96, low: 0.92, close: 0.92, volume: 1e5, amount: 1e8 },
]

const levels = {
  sr: [{ value: 1.05, label: '成交密集区(POC)', type: 'sr' as const, side: 'resistance' as const, strength: 'strong' as const }],
  pivot: [], extreme: [], boll: [], keltner_s: [], keltner_m: [], keltner_l: [],
  atr_stop: [], gap: [], fib: [], round: [],
}

let host: HTMLDivElement
let root: Root

const render = async (props: Record<string, unknown> = {}) => {
  await act(async () => {
    root.render(<AnalysisKChart rows={rows} levels={levels} height={400} {...props} />)
  })
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  mockState.option = null
  mockState.zoomEnd = 100
  mockState.handlers = {}
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

describe('图表配置', () => {
  it('十字虚线：透明 tooltip 激活 cross，主线与横线都是 dashed', async () => {
    await render()
    const { tooltip, axisPointer, xAxis, yAxis } = captured.option

    // ECharts 只把 cross 开放给 tooltip 层，所以这里必须是一个"看得见十字、
    // 看不到 tooltip 内容"的壳子：formatter 空串 + 透明背景 + 字号 0。
    expect(tooltip.trigger).toBe('axis')
    expect(tooltip.axisPointer.type).toBe('cross')
    expect(tooltip.axisPointer.crossStyle.type).toBe('dashed')
    expect(tooltip.backgroundColor).toBe('transparent')
    expect(tooltip.formatter()).toBe('')

    expect(axisPointer.lineStyle.type).toBe('dashed')
    expect(axisPointer.label.backgroundColor).toBeTruthy()
    // 轴端数值标签要给出来，否则读数只能靠下方读数条，比弹窗少一层即时反馈
    expect(axisPointer.label.fontFamily).toContain('JetBrains Mono')

    // 轴上不再各挂一条 line（那是第一版的写法，能出十字但不带轴端标签）
    expect(xAxis[0].axisPointer).toBeUndefined()
    expect(yAxis[0].axisPointer).toBeUndefined()
  })

  it('成交量区只要竖线，不要横线与日期标签', async () => {
    await render()
    const { xAxis, yAxis } = captured.option
    expect(xAxis[1].axisPointer.label.show).toBe(false)
    expect(yAxis[1].axisPointer.show).toBe(false)
  })

  it('绘图区不再留 144px 标签带 —— 价位文字交给下方面板', async () => {
    await render()
    // 主图被压窄是"不如弹窗好看"的主因：right 从 144 收到 67
    expect(captured.option.grid[0].right).toBeLessThan(80)
    const line = captured.option.series.find((s: any) => s.type === 'line' && s.endLabel?.show)
    expect(line).toBeUndefined()   // 默认只开布林带且无 series 数据 → 没有外部标签
  })

  it('y 轴留边距、不画轴线和刻度', async () => {
    await render()
    const y = captured.option.yAxis[0]
    expect(y.boundaryGap).toEqual([0.04, 0.04])
    expect(y.axisLine.show).toBe(false)
    expect(y.axisTick.show).toBe(false)
  })

  it('默认不铺价位横线：只开布林带，图上只有 K 与成交量', async () => {
    await render()
    expect(captured.option.series).toHaveLength(2)
  })

  it('打开「今日价位」后只画最后两根，不是贯穿全图', async () => {
    await render()
    const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('压力支撑')) as HTMLButtonElement
    expect(btn).toBeTruthy()
    await act(async () => { btn.click() })

    const line = captured.option.series.find((s: any) => s.name === '成交密集区(POC)')
    expect(line).toBeTruthy()
    const data = line.data as (number | null)[]
    expect(data).toHaveLength(rows.length)
    expect(data.slice(0, rows.length - 2).every(v => v === null)).toBe(true)
    expect(data.slice(-2)).toEqual([1.05, 1.05])
  })

  it('拖到历史段后，今日价位随 datazoom 消失（不是只在首屏判断一次）', async () => {
    await render()
    const btn = [...host.querySelectorAll('button')].find(b => b.textContent?.includes('压力支撑')) as HTMLButtonElement
    await act(async () => { btn.click() })
    expect(captured.option.series.some((s: any) => s.name === '成交密集区(POC)')).toBe(true)

    // 模拟把缩放条拖成 [0, 50]：视窗不再包含最新一根
    mockState.zoomEnd = 50
    await act(async () => { mockState.handlers['datazoom']?.() })

    expect(captured.option.series.some((s: any) => s.name === '成交密集区(POC)')).toBe(false)

    // 拖回最新一根，它要回来 —— 否则只是"永远不会画"
    mockState.zoomEnd = 100
    await act(async () => { mockState.handlers['datazoom']?.() })
    expect(captured.option.series.some((s: any) => s.name === '成交密集区(POC)')).toBe(true)
  })
})
