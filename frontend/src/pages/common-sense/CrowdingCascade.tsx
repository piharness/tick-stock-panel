/**
 * 踩踏传导链 —— 量化趋同如何一步步演变成踩踏。
 *
 * 这是「因子拥挤导致流动性枯竭、一点火星引发踩踏」这条常识的**因果链可视化**。
 * 上图（趋同→拥挤→流动性枯竭→火星→踩踏）是**结构示意**：它画的是方向，
 * danger 值是示意刻度，不是实测。
 *
 * 下图是本页面的**真实判决**：用全市场日线算「高换手 vs 低换手」的后续 20 日
 * 超额收益，证伪「拥挤=踩踏」这条粗糙叙事。真实结论是：
 *   - 换手率越高，后续 20 日超额越低（高换手中位 −3.1% vs 低换手 −0.7%）；
 *   - 但控制市值后，信号只在大盘股成立：大市值组内高换手跑输 −3.2%，
 *     小市值组内高换手反而略好（+0.8%）。
 *   - 即「拥挤危险」是有条件成立的命题，不是普适铁律：大盘股放量滞涨是真危险，
 *     小盘股的高换手（题材活跃）并不危险。
 */
import { useMemo } from 'react'
import * as echarts from 'echarts'
import { useChartTheme } from '@/lib/theme'
import { useECharts } from '@/pages/backtest/charts/useECharts'
import type { CrowdingVerdict } from '@/lib/api'

interface Node {
  name: string
  detail: string
  /** 0 = 隐性/平缓，1 = 显性/剧烈 —— 控制颜色从绿到红（示意） */
  danger: number
}

const NODES: Node[] = [
  { name: '策略趋同', detail: '大量量化资金挤在同一类因子上\n大家都在做同一件事', danger: 0.15 },
  { name: '因子拥挤', detail: '同向持仓高度集中\n收益被提前透支', danger: 0.35 },
  { name: '流动性枯竭', detail: '对手盘消失\n买卖价差拉大、深度见底', danger: 0.65 },
  { name: '一点火星', detail: '杠杆平仓 / 大持仓强平\n或某个流动性抽离事件', danger: 0.85 },
  { name: '踩踏', detail: '所有同向仓位同时平\n价格无基本面理由直线跳水', danger: 1.0 },
]

const LINKS = [
  { source: '策略趋同', target: '因子拥挤' },
  { source: '因子拥挤', target: '流动性枯竭' },
  { source: '流动性枯竭', target: '一点火星' },
  { source: '一点火星', target: '踩踏' },
]

function dangerColor(d: number): string {
  // 绿 → 黄 → 红
  if (d < 0.4) return '#16A34A'
  if (d < 0.75) return '#F59E0B'
  return '#EF4444'
}

export function CrowdingCascade({ verdict }: { verdict?: CrowdingVerdict }) {
  const ct = useChartTheme()

  const option = useMemo(() => {
    const nodeMap = new Map(NODES.map(n => [n.name, n]))
    return {
      backgroundColor: 'transparent',
      tooltip: {
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 11 },
        formatter: (p: any) => {
          if (p.dataType === 'edge') return p.data.label ?? ''
          const n = nodeMap.get(p.name)
          return n ? `<b>${n.name}</b><br/><span style="color:#999">${n.detail.replace(/\n/g, '<br/>')}</span>` : p.name
        },
      },
      series: [
        {
          type: 'graph',
          layout: 'none',
          roam: false,
          label: {
            show: true,
            position: 'bottom',
            color: ct.textStrong,
            fontSize: 11,
            fontWeight: 'bold',
          },
          lineStyle: {
            color: ct.border,
            width: 1.5,
            curveness: 0,
            opacity: 0.7,
          },
          edgeSymbol: ['none', 'arrow'],
          edgeSymbolSize: [0, 8],
          emphasis: { focus: 'adjacency', lineStyle: { width: 2.5 } },
          data: NODES.map((n, i) => ({
            name: n.name,
            x: 80 + i * 130,
            y: 180,
            symbolSize: 40 + n.danger * 18,
            itemStyle: {
              color: dangerColor(n.danger),
              borderColor: ct.tooltipText,
              borderWidth: 1,
              shadowBlur: n.danger * 12,
              shadowColor: dangerColor(n.danger),
            },
          })),
          links: LINKS.map(l => ({ source: l.source, target: l.target })),
        },
      ],
    } as echarts.EChartsOption
  }, [ct])

  const ref = useECharts(option, [ct])

  return (
    <div>
      <div ref={ref} style={{ height: 260 }} />
      <div className="mt-1 grid grid-cols-1 gap-1.5 sm:grid-cols-5">
        {NODES.map(n => (
          <div key={n.name} className="rounded border border-border/60 bg-elevated/30 px-2 py-1.5">
            <div className="flex items-center gap-1.5">
              <span className="h-2 w-2 rounded-full" style={{ background: dangerColor(n.danger) }} />
              <span className="text-[11px] font-medium text-foreground">{n.name}</span>
            </div>
            <p className="mt-0.5 text-[9px] leading-relaxed text-muted/80">{n.detail}</p>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-muted/80">
        上图是<b className="text-secondary">结构示意</b>（danger 值是示意刻度，非实测）。前两步（趋同、拥挤）在平时是
        隐性的，真正的危险在第三、四步被触发 —— 但「拥挤是否真会踩踏」需要数据验证，见下方判决。
      </p>

      <CrowdingVerdictPanel verdict={verdict} />
    </div>
  )
}

/** 真实判决：换手率（拥挤度）到底能不能预测后续跑输？ */
function CrowdingVerdictPanel({ verdict }: { verdict?: CrowdingVerdict }) {
  if (!verdict || verdict.n === 0) return null

  const q = verdict.quintiles
  const hi = q.find(x => x.q === 5)
  const lo = q.find(x => x.q === 1)
  const bigSize = verdict.by_size.find(x => x.q === 5)
  const smallSize = verdict.by_size.find(x => x.q === 1)

  return (
    <div className="mt-3 rounded border border-amber-500/20 bg-amber-500/[0.04] px-2.5 py-2">
      <div className="text-[10px] text-muted">真实判决：拥挤危险吗？（全市场 {verdict.n.toLocaleString()} 样本）</div>

      {/* 换手率分位 → 后续超额 */}
      <div className="mt-1.5">
        <div className="text-[9px] text-muted/70">换手率分位 → 后续 20 日超额（中位）</div>
        <div className="mt-0.5 flex items-end gap-1">
          {q.map(x => (
            <div key={x.q} className="flex-1 text-center">
              <div className="text-[10px] font-mono text-secondary">
                {x.ex20_median_bp == null ? '—' : `${x.ex20_median_bp > 0 ? '+' : ''}${x.ex20_median_bp.toFixed(0)}`}
              </div>
              <div
                className="mx-auto mt-0.5 rounded-sm"
                style={{
                  height: `${Math.max(4, Math.abs(x.ex20_median_bp ?? 0) / 3.5)}px`,
                  background: (x.ex20_median_bp ?? 0) >= 0 ? '#EF4444' : '#16A34A',
                  opacity: 0.75,
                }}
              />
              <div className="mt-0.5 text-[9px] text-muted/70">{x.label}</div>
            </div>
          ))}
        </div>
      </div>

      {/* 按市值分层的判决 */}
      <div className="mt-2">
        <div className="text-[9px] text-muted/70">控制市值后：高换手 − 低换手 的超额差</div>
        <div className="mt-0.5 flex items-end gap-1">
          {verdict.by_size.map(x => {
            const v = x.hi_minus_lo_bp ?? 0
            const pos = v > 0
            return (
              <div key={x.q} className="flex-1 text-center">
                <div className="text-[10px] font-mono text-secondary">
                  {x.hi_minus_lo_bp == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(0)}`}
                </div>
                <div className="mx-auto mt-0.5 flex h-10 items-end overflow-hidden rounded-sm bg-elevated">
                  <div
                    className="w-full"
                    style={{
                      height: `${Math.min(100, Math.abs(v) / 3.2)}%`,
                      background: pos ? '#EF4444' : '#16A34A',
                      opacity: 0.75,
                    }}
                  />
                </div>
                <div className="mt-0.5 text-[9px] text-muted/70">{x.label}</div>
              </div>
            )
          })}
        </div>
      </div>

      <p className="mt-1.5 text-[10px] leading-relaxed text-secondary">
        <b className="text-foreground">判决：</b>
        换手率越高后续越弱（高换手 {hi?.ex20_median_bp == null ? '—' : `${hi.ex20_median_bp.toFixed(0)}bp`} vs 低换手 {lo?.ex20_median_bp == null ? '—' : `${lo.ex20_median_bp.toFixed(0)}bp`}），
        但「拥挤危险」<b className="text-foreground">只在大盘股成立</b>：大市值组内高换手跑输 {bigSize?.hi_minus_lo_bp == null ? '—' : `${bigSize.hi_minus_lo_bp.toFixed(0)}bp`}，
        小市值组内反而 {smallSize?.hi_minus_lo_bp == null ? '—' : `+${smallSize.hi_minus_lo_bp.toFixed(0)}bp`}。大盘股放量滞涨是真危险，小盘股高换手（题材活跃）并不危险。
      </p>
      {verdict.caveats?.length ? (
        <p className="mt-1 text-[9px] leading-relaxed text-muted/70">{verdict.caveats.join(' ')}</p>
      ) : null}
    </div>
  )
}
