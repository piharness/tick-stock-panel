/**
 * 退市生死全景图 —— 全市场个股的「价格 × 市值」风险地图。
 *
 * 这是「*ST华幸早被判死刑」这条常识的**全市场版本**：不讲一只票，而是把
 * 全市场每一只票按其最新「收盘价（对数轴）× 市值（对数轴）」散布开，
 * 叠加两条硬红线：
 *   - 面值线：close = 1.00 元（左侧越过即进入「面值退市倒数区」）
 *   - 市值线：主板 5 亿 / 创业·科创·北交所 3 亿（下方越过即进入「市值退市倒数区」）
 *
 * 一眼能看出：
 *   1. 有多少只票已经压在死亡线左侧/下方（点开即见名单）
 *   2. ST 股（红点）在风险区的聚集程度 —— 幸存者偏差的直观反例
 *   3. 那批「散户还在心存侥幸」的票，在地图上的确切位置
 *
 * 数据来自 /api/screener/market-snapshot（前端聚合，不改后端）：
 *   close / total_shares / market_cap / name（ST 前缀）
 *
 * 诚实边界：这是「规则距离」的快照，不是「概率预测」。面值线左侧只表示
 * 「已进入倒数」，不代表「必然退市」；同样，紧贴线外也不代表安全。
 * 市值口径用当日 market_cap（snapshot 已算），与退市红线面板的
 * 「连续 20 日」是两回事 —— 本图是横截面，不是逐日计数。
 */
import { useMemo, useState } from 'react'
import * as echarts from 'echarts'
import { ShieldAlert } from 'lucide-react'
import { useChartTheme } from '@/lib/theme'
import type { MarketSnapshotRow } from '@/lib/api'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import { boardOf, isSt, MCAP_LINE_MAIN, MCAP_LINE_OTHER, PAR_LINE } from './market'

export function DelistingLandscape({ rows }: { rows: MarketSnapshotRow[] }) {
  const ct = useChartTheme()

  const { normal, st, atRisk } = useMemo(() => {
    const normal: [number, number][] = []
    const stPts: [number, number][] = []
    const risk: { name: string; symbol: string; close: number; mcap: number; why: string }[] = []
    for (const r of rows) {
      const close = r.close
      const mcap = r.market_cap
      if (close == null || mcap == null || close <= 0 || mcap <= 0) continue
      const board = boardOf(r.symbol)
      const line = board === 'main' ? MCAP_LINE_MAIN : MCAP_LINE_OTHER
      const x = Math.log10(close)
      const y = Math.log10(mcap)
      const stFlag = isSt(r.name)
      const reasons: string[] = []
      if (close < PAR_LINE) reasons.push(`面值 ${close.toFixed(3)} < 1.00 元`)
      if (mcap < line) reasons.push(`市值 ${(mcap / 1e8).toFixed(2)} 亿 < ${(line / 1e8).toFixed(0)} 亿`)
      if (reasons.length > 0) {
        risk.push({ name: r.name ?? r.symbol, symbol: r.symbol, close, mcap, why: reasons.join('；') })
      }
      if (stFlag) stPts.push([x, y])
      else normal.push([x, y])
    }
    return { normal, st: stPts, atRisk: risk }
  }, [rows])

  // 风险名单按「离死线距离」排序：先面值、再市值，越危险越靠前。
  const riskList = useMemo(
    () =>
      [...atRisk].sort((a, b) => {
        const aPar = a.close < PAR_LINE ? 1 : 0
        const bPar = b.close < PAR_LINE ? 1 : 0
        if (aPar !== bPar) return bPar - aPar
        return a.mcap - b.mcap
      }),
    [atRisk],
  )

  const option = useMemo(() => {
    const logPar = Math.log10(PAR_LINE)
    const logMain = Math.log10(MCAP_LINE_MAIN)
    const logOther = Math.log10(MCAP_LINE_OTHER)

    return {
      backgroundColor: 'transparent',
      grid: { left: 52, right: 16, top: 16, bottom: 40 },
      tooltip: {
        trigger: 'item',
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 11 },
        formatter: (p: any) => {
          const close = Math.pow(10, p.value[0])
          const mcap = Math.pow(10, p.value[1])
          return `${p.seriesName}<br/>收盘价 ${close.toFixed(3)} 元<br/>市值 ${(mcap / 1e8).toFixed(2)} 亿`
        },
      },
      xAxis: {
        type: 'value',
        name: '收盘价（元，对数轴）',
        nameTextStyle: { color: ct.text, fontSize: 10 },
        axisLabel: { color: ct.text, fontSize: 10 },
        axisLine: { lineStyle: { color: ct.border } },
        splitLine: { lineStyle: { color: ct.grid } },
        min: logPar - 0.7,
        max: logPar + 1.6,
      },
      yAxis: {
        type: 'value',
        name: '市值（元，对数轴）',
        nameTextStyle: { color: ct.text, fontSize: 10 },
        axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => `${(Math.pow(10, v) / 1e8).toFixed(0)}亿` },
        axisLine: { lineStyle: { color: ct.border } },
        splitLine: { lineStyle: { color: ct.grid } },
        min: logMain - 1.2,
        max: logMain + 3.0,
      },
      series: [
        {
          name: '普通股',
          type: 'scatter',
          data: normal,
          symbolSize: 4,
          itemStyle: { color: ct.fillSubtle, opacity: 0.55 },
          emphasis: { itemStyle: { color: '#3B82F6' } },
          markLine: {
            silent: true,
            symbol: 'none',
            animation: false,
            data: [
              { xAxis: logPar, label: { formatter: '面值线 1.00 元', position: 'insideEndTop', color: '#EF4444', fontSize: 10 }, lineStyle: { color: '#EF4444', type: 'dashed', width: 1 } },
              { yAxis: logMain, label: { formatter: '主板市值线 5 亿', position: 'insideEndLeft', color: '#F59E0B', fontSize: 10 }, lineStyle: { color: '#F59E0B', type: 'dashed', width: 1 } },
              { yAxis: logOther, label: { formatter: '创业/科创/北交 3 亿', position: 'insideEndLeft', color: '#F59E0B', fontSize: 10 }, lineStyle: { color: '#F59E0B', type: 'dotted', width: 1 } },
            ],
          },
        },
        {
          name: 'ST / *ST',
          type: 'scatter',
          data: st,
          symbolSize: 5,
          itemStyle: { color: '#EF4444', opacity: 0.85 },
          emphasis: { itemStyle: { color: '#DC2626' } },
          z: 3,
        },
      ],
    } as echarts.EChartsOption
  }, [normal, st, ct])

  const ref = useECharts(option, [normal, st, ct])

  const [showList, setShowList] = useState(false)

  return (
    <div>
      <div ref={ref} style={{ height: 380 }} />
      {riskList.length > 0 && (
        <button
          onClick={() => setShowList(s => !s)}
          className="mt-2 inline-flex items-center gap-1 rounded-btn border border-red-500/30 bg-red-500/[0.06] px-2 py-1 text-[10px] text-red-600 transition-colors hover:bg-red-500/10"
        >
          <ShieldAlert className="h-3 w-3" />
          {showList ? '收起' : '展开'}正在逼近死线的 {riskList.length} 只票
        </button>
      )}
      {showList && riskList.length > 0 && (
        <div className="mt-2 max-h-56 overflow-auto rounded border border-border/60 bg-elevated/20">
          <table className="w-full text-left text-[10px]">
            <thead className="sticky top-0 bg-surface">
              <tr className="border-b border-border/60 text-[9px] uppercase text-muted/70">
                <th className="px-2 py-1.5 font-medium">名称</th>
                <th className="px-2 py-1.5 font-medium">代码</th>
                <th className="px-2 py-1.5 text-right font-medium">收盘价</th>
                <th className="px-2 py-1.5 text-right font-medium">市值</th>
                <th className="px-2 py-1.5 font-medium">触及红线</th>
              </tr>
            </thead>
            <tbody>
              {riskList.map(r => (
                <tr key={r.symbol} className="border-b border-border/30 last:border-0">
                  <td className="px-2 py-1 font-medium text-foreground">{r.name}</td>
                  <td className="px-2 py-1 font-mono text-muted">{r.symbol}</td>
                  <td className="px-2 py-1 text-right font-mono text-red-600">{r.close.toFixed(3)}</td>
                  <td className="px-2 py-1 text-right font-mono text-secondary">{(r.mcap / 1e8).toFixed(2)} 亿</td>
                  <td className="px-2 py-1 text-secondary">{r.why}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-muted">
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full bg-red-500" />ST / *ST
        </span>
        <span className="flex items-center gap-1">
          <span className="h-2 w-2 rounded-full" style={{ background: ct.fillSubtle }} />普通股
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-0 w-4 border-t border-dashed border-red-500" />面值线 1.00 元
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-0 w-4 border-t border-dashed border-amber-500" />市值线
        </span>
      </div>
    </div>
  )
}

/** 风险名单导出：供页面展示「正在逼近死线」的票 */
export function riskCount(rows: MarketSnapshotRow[]): { belowPar: number; belowMcap: number; st: number } {
  let belowPar = 0
  let belowMcap = 0
  let st = 0
  for (const r of rows) {
    if (isSt(r.name)) st++
    if (r.close != null && r.close > 0 && r.close < PAR_LINE) belowPar++
    if (r.market_cap != null && r.market_cap > 0) {
      const line = boardOf(r.symbol) === 'main' ? MCAP_LINE_MAIN : MCAP_LINE_OTHER
      if (r.market_cap < line) belowMcap++
    }
  }
  return { belowPar, belowMcap, st }
}
