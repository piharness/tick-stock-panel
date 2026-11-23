/**
 * 资金集中度佐证 —— 把「因子拥挤 → 流动性枯竭」的因果链里「拥挤」这一环，
 * 落成一个可算的横截面事实：成交额集中度。
 *
 * 踩踏传导链的因果方向（趋同 → 拥挤 → 枯竭 → 火星 → 踩踏）是金融学共识，
 * 但「拥挤」平时是隐性的、看不见的。本组件用全市场成交额的集中度做它的
 * 横截面代理：如果前 100 只票吃掉了全市场 1/4 以上的成交额，说明资金高度
 * 集中在少数标的上 —— 这些票一旦有一个触发点（强平/抽离），流动性会最先
 * 枯竭，因为它们平时的「活跃」正是「拥挤」的证据。
 *
 * 诚实边界：集中度只能证明「资金集中在少数票」，不能证明「接下来会踩踏」。
 * 它是「拥挤」的必要但不充分证据 —— 平滑与集中是脆弱的前奏，不是触发。
 * 数据来自 /api/screener/market-snapshot 的 amount（当日全市场成交额横截面）。
 */
import { useMemo } from 'react'
import type { MarketSnapshotRow } from '@/lib/api'

const BUCKETS = [10, 50, 100, 300] as const

export function ConcentrationPanel({ rows }: { rows: MarketSnapshotRow[] }) {
  const stats = useMemo(() => {
    const amounts = rows
      .map(r => r.amount)
      .filter((a): a is number => a != null && a > 0 && Number.isFinite(a))
      .sort((a, b) => b - a)
    if (amounts.length === 0) return null
    const total = amounts.reduce((s, a) => s + a, 0)
    const buckets = BUCKETS.map(n => {
      const top = amounts.slice(0, n).reduce((s, a) => s + a, 0)
      return { n, ratio: total > 0 ? top / total : 0, top }
    })
    return { total, count: amounts.length, buckets }
  }, [rows])

  if (!stats) {
    return <div className="rounded border border-border/40 bg-base/30 px-3 py-2 text-[11px] text-muted">无成交额数据，无法算集中度。</div>
  }

  return (
    <div>
      <div className="mb-2 flex items-center gap-2 text-[10px] text-muted">
        <span>全市场 <b className="text-foreground">{stats.count.toLocaleString()}</b> 只，成交额合计 <b className="text-foreground">{(stats.total / 1e8).toFixed(0)}</b> 亿</span>
      </div>
      <div className="space-y-1.5">
        {stats.buckets.map(b => (
          <div key={b.n} className="flex items-center gap-2">
            <span className="w-16 shrink-0 text-right text-[10px] text-secondary">前 {b.n} 只</span>
            <div className="relative h-3 flex-1 overflow-hidden rounded-sm bg-elevated">
              <div
                className="absolute inset-y-0 left-0 rounded-sm"
                style={{
                  width: `${Math.min(100, b.ratio * 100)}%`,
                  background: b.ratio > 0.25 ? '#EF4444' : b.ratio > 0.15 ? '#F59E0B' : '#3B82F6',
                  opacity: 0.75,
                }}
              />
            </div>
            <span className="w-12 shrink-0 text-right font-mono text-[10px] text-foreground">{(b.ratio * 100).toFixed(1)}%</span>
          </div>
        ))}
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-secondary">
        <b className="text-foreground">读法：</b>
        前 100 只票吃掉了全市场 {((stats.buckets.find(b => b.n === 100)?.ratio ?? 0) * 100).toFixed(1)}% 的成交额 ——
        资金高度集中在少数标的上。这些票平时的「活跃」正是「拥挤」的证据：一旦一个触发点
        （强平/流动性抽离）出现，它们会最先失去对手盘。
      </p>
      <p className="mt-1 text-[9px] leading-relaxed text-muted/70">
        · 集中度只证明「资金集中在少数票」，不证明「接下来会踩踏」；它是拥挤的必要不充分证据。
      </p>
    </div>
  )
}
