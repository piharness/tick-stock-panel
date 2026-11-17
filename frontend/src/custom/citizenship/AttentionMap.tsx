/**
 * 全市场注意力全景图 —— 市场公民页面的核心可视化。
 *
 * 把全市场 5000+ 只票投进「规模分位 × 关注度分位」的二维空间：
 *   - x 轴 = 规模分位 p_scale（左=小盘，右=大盘）
 *   - y 轴 = 关注度分位 p_turnover（下=沉默，上=活跃）
 *   - 颜色 = 公民等级（core/active/background/silent）
 *   - 气泡大小 = 成交额（注意力体量）
 *   - 符号 = 状态迁移（升格 ▲ / 降格 ▼ / 停留 ●）
 *
 * 设计意图：一张图回答「今天全市场的注意力长什么样」——
 *   右上角是核心公民（大市值高关注），左下角是沉默黑洞，
 *   右上角边缘的「升格」标记是被重新关注的异动票，一眼可见。
 */
import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import type { CitizenshipLevel, CitizenshipRow } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'

// 等级颜色（双主题通用，取色偏中性、可读）
const LEVEL_COLOR: Record<CitizenshipLevel, string> = {
  core: '#F59E0B',       // 琥珀 — 流动性的绝对中心
  active: '#3B82F6',     // 蓝 — 活跃
  background: '#9CA3AF', // 灰 — 背景
  silent: '#71717A',     // 深灰 — 沉默黑洞
}

const LEVEL_LABEL: Record<CitizenshipLevel, string> = {
  core: '核心',
  active: '活跃',
  background: '背景',
  silent: '沉默',
}

interface Props {
  rows: CitizenshipRow[]
  height?: number
  onOpenStock: (symbol: string) => void
}

function fmtAmt(v: number): string {
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(1)}亿`
  if (v >= 10_000) return `${(v / 10_000).toFixed(0)}万`
  return v.toFixed(0)
}

export function AttentionMap({ rows, height = 460, onOpenStock }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const ct = useChartTheme()

  // 按等级分序列，每个序列内部按迁移符号再分
  const seriesData = useMemo(() => {
    const byLevel: Record<CitizenshipLevel, CitizenshipRow[]> = {
      core: [], active: [], background: [], silent: [],
    }
    for (const r of rows) byLevel[r.level]?.push(r)
    return byLevel
  }, [rows])

  useEffect(() => {
    if (!containerRef.current) return
    const chart = echarts.init(containerRef.current)
    chartRef.current = chart
    const onResize = () => chart.resize()
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return

    const levels: CitizenshipLevel[] = ['core', 'active', 'background', 'silent']
    const series: EChartsOption['series'] = []

    for (const lvl of levels) {
      const lvlRows = seriesData[lvl]
      if (!lvlRows.length) continue

      // 按迁移分组：升格 / 降格 / 停留+新进
      const up = lvlRows.filter(r => r.transition === 'up')
      const down = lvlRows.filter(r => r.transition === 'down')
      const stay = lvlRows.filter(r => r.transition !== 'up' && r.transition !== 'down')

      const mk = (list: CitizenshipRow[], sym: 'arrow' | 'roundRect' | 'circle'): any => ({
        type: 'scatter' as const,
        name: `${LEVEL_LABEL[lvl]}${sym === 'arrow' ? '·升格' : sym === 'roundRect' ? '·降格' : ''}`,
        symbol: sym === 'arrow' ? 'triangle' : sym === 'roundRect' ? 'rect' : 'circle',
        symbolSize: 6,
        symbolRotate: sym === 'arrow' ? 0 : undefined,
        itemStyle: {
          color: LEVEL_COLOR[lvl],
          opacity: sym === 'arrow' ? 1 : sym === 'roundRect' ? 1 : 0.45,
          borderColor: sym === 'arrow' ? '#10B981' : sym === 'roundRect' ? '#EF4444' : 'transparent',
          borderWidth: sym === 'arrow' || sym === 'roundRect' ? 1.5 : 0,
        },
        emphasis: {
          itemStyle: { borderColor: ct.textStrong, borderWidth: 1.5, opacity: 1 },
          scale: 1.6,
        },
        // 迁移信号「先弹出来」：升格/降格带入场动画 + 依次点亮，停留点静默铺底
        animation: sym !== 'circle',
        animationDuration: sym === 'arrow' ? 900 : sym === 'roundRect' ? 700 : 0,
        animationDelay: (idx: number) => (sym === 'arrow' ? idx * 2 : sym === 'roundRect' ? idx * 1 : 0),
        animationEasing: 'cubicOut',
        data: list.map(r => ({
          value: [r.p_scale, r.p_turnover, Math.max(1, Math.log10(r.amount))],
          symbolSize: 3 + Math.sqrt(r.amount) / 1400,
          name: r.name ?? r.symbol,
          symbol: r.symbol,
          level: lvl,
          transition: r.transition,
          mcap: r.mcap,
          amount: r.amount,
          turnover: r.turnover_rate,
        })),
        large: list.length > 2000,
        largeThreshold: 2000,
        progressive: 3000,
        // 升格/降格永远绘制在最上层，不被停留点遮挡
        z: sym === 'arrow' ? 30 : sym === 'roundRect' ? 20 : 10,
      })
      if (stay.length) series.push(mk(stay, 'circle'))
      if (down.length) series.push(mk(down, 'roundRect'))
      if (up.length) series.push(mk(up, 'arrow'))
    }

    const option: EChartsOption = {
      backgroundColor: 'transparent',
      animation: true,
      animationDuration: 400,
      grid: { left: 44, right: 16, top: 16, bottom: 40 },
      xAxis: {
        type: 'value', min: 0, max: 1,
        name: '规模分位 →',
        nameLocation: 'middle', nameGap: 28,
        nameTextStyle: { color: ct.text, fontSize: 11 },
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => `${Math.round(v * 100)}%` },
        splitLine: { show: false },
        axisLine: { lineStyle: { color: ct.border } },
      },
      yAxis: {
        type: 'value', min: 0, max: 1,
        name: '关注度分位 →',
        nameLocation: 'middle', nameGap: 34,
        nameTextStyle: { color: ct.text, fontSize: 11 },
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => `${Math.round(v * 100)}%` },
        splitLine: { lineStyle: { color: ct.grid } },
        axisLine: { lineStyle: { color: ct.border } },
      },
      tooltip: {
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
        formatter: (p: any) => {
          const d = p.data
          if (!d) return ''
          const tMap: Record<string, string> = { up: '升格', down: '降格', new: '新进', stay: '停留' }
          return [
            `<b>${d.name}</b> <span style="opacity:.6">${d.symbol}</span>`,
            `等级：${LEVEL_LABEL[d.level as CitizenshipLevel]} · ${tMap[d.transition] ?? ''}`,
            `规模分位 ${(d.value[0] * 100).toFixed(0)}% · 关注分位 ${(d.value[1] * 100).toFixed(0)}%`,
            `流通市值 ${fmtAmt(d.mcap)} · 成交 ${fmtAmt(d.amount)}`,
            `换手 ${d.turnover.toFixed(1)}%`,
          ].join('<br/>')
        },
      },
      series,
    }

    chart.setOption(option, true)
    chart.off('click')
    chart.on('click', (p: any) => {
      if (p.data?.symbol) onOpenStock(p.data.symbol)
    })
  }, [seriesData, ct, onOpenStock])

  return (
    <div
      ref={containerRef}
      style={{ width: '100%', height }}
      role="img"
      aria-label="全市场注意力全景图：横轴为规模分位，纵轴为关注度分位，颜色为公民等级，气泡大小代表成交额"
    />
  )
}
