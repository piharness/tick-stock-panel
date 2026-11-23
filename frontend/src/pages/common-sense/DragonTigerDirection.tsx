/**
 * 龙虎榜方向实证 —— 把「聪明钱先于散户定价、方向不完全一致」落成可证伪的数字。
 *
 * 市场常识页里「信息时间差」与「博弈推演」两条常识，原本各画了一张硬编码的
 * 示意（信息六层瀑布 / 五个角色的买入倾向）。示意能讲清「方向」，但讲不清
 * 「方向到底有多强、什么时候反」。本地唯一带「决策主体身份」的高频数据是
 * 龙虎榜（机构 / 游资 / 全榜净买），本组件把它的两个同向率画出来：
 *
 *   - 机构 vs 全榜：机构净买方向与「含散户的全市场净买」是否一致；
 *   - 机构 vs 游资：机构与游资席位是否同向。
 *
 * 实证发现（本地 244 个交易日缓存，2025-11 ~ 2026-11）：
 *   机构 vs 全榜 同向率 ≈ 67%，机构 vs 游资 同向率 ≈ 41% ——
 *   「聪明钱与谁反向」的真相是「机构 vs 游资」更常反向，而非「机构 vs 散户」。
 *   这修正了「机构永远和散户对着干」的粗糙叙事。
 *
 * 诚实边界：龙虎榜是稀疏事件数据（仅上榜票、事后披露），本统计是「上榜票里
 * 的方向关系」，不是全市场机构行为。caveats 必须展示，不能放大结论。
 */
import { useMemo } from 'react'
import * as echarts from 'echarts'
import { useChartTheme } from '@/lib/theme'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import type { DragonTigerDirectionStats, DragonTigerVerdict } from '@/lib/api'

export function DragonTigerDirection({
  data, verdict, loading, error,
}: {
  data: DragonTigerDirectionStats | undefined
  verdict: DragonTigerVerdict | undefined
  loading: boolean
  error: boolean
}) {
  const ct = useChartTheme()

  const option = useMemo(() => {
    const series = data?.series ?? []
    return {
      backgroundColor: 'transparent',
      grid: { left: 40, right: 40, top: 16, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 11 },
        formatter: (ps: any) => {
          const p = Array.isArray(ps) ? ps[0] : ps
          if (!p) return ''
          const idx = p.dataIndex
          const s = series[idx]
          if (!s) return ''
          return `${s.date}<br/>机构与全榜同向 ${s.same}/${s.total} = ${(s.rate * 100).toFixed(0)}%`
        },
      },
      xAxis: {
        type: 'category',
        data: series.map(s => s.date.slice(5)),
        axisLabel: { color: ct.text, fontSize: 9, interval: Math.max(0, Math.floor(series.length / 8) - 1) },
        axisLine: { lineStyle: { color: ct.border } },
      },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        axisLabel: { color: ct.text, fontSize: 10, formatter: '{value}%' },
        splitLine: { lineStyle: { color: ct.grid } },
      },
      series: [
        {
          type: 'line',
          name: '机构 vs 全榜 同向率',
          data: series.map(s => +(s.rate * 100).toFixed(1)),
          smooth: true,
          symbol: 'none',
          lineStyle: { color: '#3B82F6', width: 1.5 },
          itemStyle: { color: '#3B82F6' },
          markLine: {
            silent: true,
            symbol: 'none',
            data: [{ yAxis: 50, label: { formatter: '50%（随机线）', color: '#999', fontSize: 9 }, lineStyle: { color: '#999', type: 'dashed', width: 1 } }],
          },
        },
      ],
    } as echarts.EChartsOption
  }, [data, ct])

  const ref = useECharts(option, [data, ct])

  if (loading && !data) {
    return <div className="rounded border border-border/40 bg-base/30 px-3 py-2 text-[11px] text-muted">正在算龙虎榜方向一致性…</div>
  }
  if (error && !data) {
    return <div className="rounded border border-border/40 bg-base/30 px-3 py-2 text-[11px] text-muted">龙虎榜数据不可用（接口失败）。</div>
  }
  if (!data || data.days === 0) {
    return <div className="rounded border border-border/40 bg-base/30 px-3 py-2 text-[11px] text-muted">本地无龙虎榜缓存，无法实证方向关系。</div>
  }

  const oa = data.org_vs_all
  const oh = data.org_vs_hotmoney

  return (
    <div>
      <div className="grid grid-cols-2 gap-2">
        <div className="rounded border border-sky-500/20 bg-sky-500/[0.04] px-2.5 py-2">
          <div className="text-[10px] text-muted">机构 vs 全榜 同向率</div>
          <div className="mt-0.5 text-[18px] font-semibold text-sky-600">
            {oa.rate != null ? `${(oa.rate * 100).toFixed(1)}%` : '—'}
          </div>
          <div className="text-[9px] text-muted/70">{oa.same} / {oa.total} 次</div>
        </div>
        <div className="rounded border border-amber-500/20 bg-amber-500/[0.04] px-2.5 py-2">
          <div className="text-[10px] text-muted">机构 vs 游资 同向率</div>
          <div className="mt-0.5 text-[18px] font-semibold text-amber-600">
            {oh.rate != null ? `${(oh.rate * 100).toFixed(1)}%` : '—'}
          </div>
          <div className="text-[9px] text-muted/70">{oh.same} / {oh.total} 次</div>
        </div>
      </div>

      <div className="mt-2">
        <div ref={ref} style={{ height: 160 }} />
      </div>

      <p className="mt-1 text-[10px] leading-relaxed text-secondary">
        <b className="text-foreground">实证发现：</b>
        机构与全榜同向约 {oa.rate != null ? `${(oa.rate * 100).toFixed(0)}%` : '—'}，
        但与游资同向仅约 {oh.rate != null ? `${(oh.rate * 100).toFixed(0)}%` : '—'} ——
        「聪明钱与谁反向」的真相是<b>机构 vs 游资</b>更常相反，而非「机构 vs 散户」。
        这修正了「机构永远和散户对着干」的粗糙叙事。
      </p>
      {data.caveats?.length ? (
        <p className="mt-1 text-[9px] leading-relaxed text-muted/70">{data.caveats.join(' ')}</p>
      ) : null}

      <DragonTigerVerdictPanel verdict={verdict} />
    </div>
  )
}

/** 第二层判决：机构方向有没有预测力 —— 跟着聪明钱走，能赚钱吗？ */
function DragonTigerVerdictPanel({ verdict }: { verdict: DragonTigerVerdict | undefined }) {
  const v = verdict?.verdict
  if (!verdict || !v || verdict.days === 0) return null

  const t = v.t
  const sig = t >= 2 ? '显著' : t >= 1 ? '弱信号' : '不显著'
  const diff = v.diff_bp
  const diffText = diff > 0 ? `机构买入略胜 ${diff.toFixed(0)}bp` : `机构卖出略胜 ${Math.abs(diff).toFixed(0)}bp`

  return (
    <div className="mt-2 rounded border border-red-500/20 bg-red-500/[0.04] px-2.5 py-2">
      <div className="text-[10px] text-muted">第二层：机构方向有预测力吗？</div>
      <div className="mt-1 grid grid-cols-3 gap-2">
        <div>
          <div className="text-[9px] text-muted/70">机构买 20日超额</div>
          <div className={`text-[13px] font-semibold ${v.org_buy_ex20_bp >= 0 ? 'text-red-500' : 'text-emerald-600'}`}>
            {v.org_buy_ex20_bp >= 0 ? '+' : ''}{v.org_buy_ex20_bp.toFixed(0)}bp
          </div>
        </div>
        <div>
          <div className="text-[9px] text-muted/70">机构卖 20日超额</div>
          <div className={`text-[13px] font-semibold ${v.org_sell_ex20_bp >= 0 ? 'text-red-500' : 'text-emerald-600'}`}>
            {v.org_sell_ex20_bp >= 0 ? '+' : ''}{v.org_sell_ex20_bp.toFixed(0)}bp
          </div>
        </div>
        <div>
          <div className="text-[9px] text-muted/70">上榜 20日超额中位</div>
          <div className={`text-[13px] font-semibold ${v.all_ex20_median_bp >= 0 ? 'text-red-500' : 'text-emerald-600'}`}>
            {v.all_ex20_median_bp >= 0 ? '+' : ''}{v.all_ex20_median_bp.toFixed(0)}bp
          </div>
        </div>
      </div>
      <p className="mt-1.5 text-[10px] leading-relaxed text-secondary">
        <b className="text-foreground">判决：</b>
        机构买 vs 机构卖差异仅 {Math.abs(diff).toFixed(0)}bp，t={t.toFixed(2)}（{sig}），
        {diffText}。但两组都是负超额——真正的信号不是「谁对」，而是
        <b className="text-foreground">「上龙虎榜」本身</b>：异动之后均值回归，中位 −{Math.abs(v.all_ex20_median_bp).toFixed(0)}bp，
        与谁在买、谁在卖无关。
      </p>
      <p className="mt-1 text-[10px] leading-relaxed text-secondary">
        这证伪了「跟着聪明钱（机构）走」：<b className="text-foreground">身份标签（机构/游资）不携带独立于价格的信息。</b>
      </p>
      {verdict.caveats?.length ? (
        <p className="mt-1 text-[9px] leading-relaxed text-muted/70">{verdict.caveats.join(' ')}</p>
      ) : null}
    </div>
  )
}
