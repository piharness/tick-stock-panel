/**
 * 注意力预算演变趋势图 —— 近 N 个交易日，全市场注意力结构如何变化。
 *
 * 展示两条信息：
 *   1. 等级占比堆叠面积图：核心/活跃/背景/沉默 每日占比的演变，
 *      一眼看出「市场在变热（核心活跃扩张）还是变冷（沉默扩张）」。
 *   2. 迁移计数（升格/降格）：每日有多少票被重新关注、多少被抛弃，
 *      是「关注度变化方向」的市场级温度计。
 */
import { useEffect, useRef } from 'react'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import { useQuery } from '@tanstack/react-query'
import { api, type CitizenshipHistory } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'

const LEVEL_COLOR: Record<string, string> = {
  core: '#F59E0B',
  active: '#3B82F6',
  background: '#9CA3AF',
  silent: '#52525B',
}

const LEVEL_LABEL: Record<string, string> = {
  core: '核心', active: '活跃', background: '背景', silent: '沉默',
}

const LEVEL_ORDER = ['core', 'active', 'background', 'silent']

interface Props {
  days?: number
  height?: number
}

export function AttentionTrend({ days = 30, height = 220 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ECharts | null>(null)
  const ct = useChartTheme()

  const q = useQuery<CitizenshipHistory>({
    queryKey: QK.citizenshipHistory(days),
    queryFn: () => api.citizenshipHistory(days),
    staleTime: 5 * 60_000,
  })

  const series = q.data?.series ?? []

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
    if (!series.length) {
      chart.clear()
      return
    }

    const dates = series.map(p => (p.as_of ?? '').slice(5))  // MM-DD
    // 各等级占比（%）
    const levelPct: Record<string, number[]> = { core: [], active: [], background: [], silent: [] }
    for (const p of series) {
      const total = p.total || 1
      const m: Record<string, number> = {}
      for (const l of p.levels) m[l.level] = l.count
      for (const lv of LEVEL_ORDER) levelPct[lv].push(((m[lv] ?? 0) / total) * 100)
    }
    // 迁移计数
    const up = series.map(p => p.transitions?.up ?? 0)
    const down = series.map(p => p.transitions?.down ?? 0)

    const option: EChartsOption = {
      backgroundColor: 'transparent',
      animation: true,
      animationDuration: 400,
      tooltip: {
        trigger: 'axis',
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 12 },
      },
      legend: {
        show: true, top: 0, left: 0,
        textStyle: { color: ct.text, fontSize: 10 },
        itemWidth: 10, itemHeight: 10,
      },
      grid: { left: 44, right: 16, top: 28, bottom: 24 },
      xAxis: {
        type: 'category', data: dates,
        axisLabel: { color: ct.text, fontSize: 10, interval: Math.ceil(dates.length / 8) },
        axisLine: { lineStyle: { color: ct.border } },
      },
      yAxis: [
        {
          type: 'value', max: 100,
          name: '占比 %',
          nameTextStyle: { color: ct.text, fontSize: 10 },
          axisLabel: { color: ct.text, fontSize: 10 },
          splitLine: { lineStyle: { color: ct.grid } },
        },
        {
          type: 'value',
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      series: [
        ...LEVEL_ORDER.map(lv => ({
          name: LEVEL_LABEL[lv],
          type: 'line' as const,
          stack: 'pct',
          areaStyle: { opacity: 0.25 },
          smooth: true,
          symbol: 'none',
          lineStyle: { width: 1.5, color: LEVEL_COLOR[lv] },
          itemStyle: { color: LEVEL_COLOR[lv] },
          data: levelPct[lv],
        })),
        {
          name: '升格',
          type: 'bar' as const,
          yAxisIndex: 1,
          data: up,
          itemStyle: { color: '#10B981', opacity: 0.7 },
          barMaxWidth: 6,
        },
        {
          name: '降格',
          type: 'bar' as const,
          yAxisIndex: 1,
          data: down.map(v => -v),
          itemStyle: { color: '#EF4444', opacity: 0.7 },
          barMaxWidth: 6,
        },
      ],
    }

    chart.setOption(option, true)
  }, [series, ct])

  return (
    <div
      ref={containerRef}
      style={{ width: '100%', height }}
      role="img"
      aria-label="近30个交易日全市场注意力预算演变趋势图"
    />
  )
}
