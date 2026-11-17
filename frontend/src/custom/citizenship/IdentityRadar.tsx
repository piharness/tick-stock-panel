/**
 * 个股三轴流动性身份雷达图 —— 回答「它为什么是这个等级」。
 *
 * 三根轴：规模 / 关注度 / 成交额 的当日全市场分位（0~1，1=最高）。
 * 雷达图的形状一眼揭示这只票的「流动性身份轮廓」：
 *   - 核心公民：三轴都高（近似正三角形撑满）
 *   - 活跃公民：关注度或成交额突出（某一轴外凸）
 *   - 沉默公民：三轴都贴边（极小三角形）
 */
import { useEffect, useMemo, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import type { CitizenshipRow } from '@/lib/api'
import { useChartTheme } from '@/lib/theme'

interface Props {
  row: CitizenshipRow | null
  height?: number
}

const LEVEL_COLOR: Record<string, string> = {
  core: '#F59E0B',
  active: '#3B82F6',
  background: '#9CA3AF',
  silent: '#71717A',
}

export function IdentityRadar({ row, height = 220 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const ct = useChartTheme()

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

  const color = useMemo(() => (row ? LEVEL_COLOR[row.level] ?? '#3B82F6' : '#3B82F6'), [row])

  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    if (!row) {
      chart.clear()
      return
    }

    const option: EChartsOption = {
      backgroundColor: 'transparent',
      animation: true,
      animationDuration: 500,
      radar: {
        indicator: [
          { name: '规模', max: 1 },
          { name: '关注度', max: 1 },
          { name: '成交额', max: 1 },
        ],
        center: ['50%', '52%'],
        radius: '68%',
        axisName: { color: ct.textStrong, fontSize: 11 },
        splitArea: { areaStyle: { color: ['transparent'] } },
        splitLine: { lineStyle: { color: ct.grid } },
        axisLine: { lineStyle: { color: ct.grid } },
      },
      series: [
        {
          type: 'radar',
          symbol: 'circle',
          symbolSize: 5,
          data: [
            {
              value: [row.p_scale, row.p_turnover, row.p_amount],
              name: row.name ?? row.symbol,
              areaStyle: { color, opacity: 0.15 },
              lineStyle: { color, width: 2 },
              itemStyle: { color },
            },
          ],
        },
      ],
    }
    chart.setOption(option, true)
  }, [row, ct, color])

  return (
    <div
      ref={containerRef}
      style={{ width: '100%', height }}
      role="img"
      aria-label="个股三轴流动性身份雷达图：规模、关注度、成交额的当日全市场分位"
    />
  )
}
