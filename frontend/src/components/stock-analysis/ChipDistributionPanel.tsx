import type { ChipDistribution } from '@/lib/api'

/**
 * 筹码分布 —— 换手率衰减模型摊开后的完整分布图。
 *
 * 这个面板**不是预测工具**，它是价位解释工具。三条纪律：
 *
 *  1. 图上画的是「成本在哪里」，不是「会涨到哪」。分布由 close/volume/turnover
 *     唯一决定，任何一个能拿到日 K 的人都能算出同一张图 —— 它不含新信息。
 *     页脚那句"不构成收益预测"不是免责声明，是这个数据的事实。
 *  2. 现价线以上＝套牢盘（潜在供给），以下＝获利盘（潜在卖压）。同一个词
 *     "抛压"在这里有两个方向，所以两种颜色必须分开看，不能合成一个"抛压分数"。
 *  3. POC（筹码最重的价位）与 K 线上的那条「成交密集区(POC)」是同一个数：
 *     后端共用 `_chip_weights` 内核，两边不可能漂移。
 */

const UP = '#C74040' // 获利盘（现价以下）
const DOWN = '#2D9B65' // 套牢盘（现价以上）

export function ChipDistributionPanel({ chip }: { chip: ChipDistribution | null | undefined }) {
  if (!chip || !chip.prices?.length) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-5 text-[11px] text-muted">
        筹码分布不可用 —— 日 K 样本不足（少于 20 根）。
      </div>
    )
  }

  const n = chip.prices.length
  const maxW = Math.max(...chip.weights, 1e-9)
  const lo = chip.prices[0]
  const hi = chip.prices[n - 1]
  const span = hi - lo || 1
  // 现价在分布中的纵向位置（价格越高越靠上），clamp 防止现价跑到分布之外
  const closePos = Math.min(1, Math.max(0, (chip.close - lo) / span))
  const profit = chip.profit_ratio
  const trapped = chip.trapped_ratio
  const costGap = chip.avg_cost > 0 ? (chip.close / chip.avg_cost - 1) * 100 : null

  // 从高到低渲染（价格轴向上递增）
  const rows = [...chip.prices.map((p, i) => ({ p, w: chip.weights[i] }))].reverse()

  return (
    <div className="rounded-lg border border-border/40 bg-base/30 overflow-hidden" data-testid="chip-distribution">
      <div className="px-4 py-2.5 border-b border-border/30 flex items-center gap-2">
        <span className="text-[13px] font-medium text-foreground">筹码分布</span>
        <span className="text-[10px] text-muted">
          换手率衰减模型 · 近 {chip.lookback} 个交易日
        </span>
        <span className="ml-auto text-[10px] font-mono text-muted/70">
          POC {chip.poc.toFixed(2)} · 均价 {chip.avg_cost.toFixed(2)}
        </span>
      </div>

      <div className="flex gap-3 p-4">
        {/* 左：横向分布。价格轴向上递增，现价线横穿 */}
        <div className="flex-1 min-w-0">
          <div className="relative">
            {rows.map(r => {
              const isProfit = r.p <= chip.close
              const isPoc = Math.abs(r.p - chip.poc) < 1e-6
              return (
                <div key={r.p} className="flex items-center gap-[3px] h-[7px] group">
                  <span className="w-10 shrink-0 text-right text-[9px] leading-none font-mono text-muted/70">
                    {r.p.toFixed(2)}
                  </span>
                  <div className="flex-1 h-full relative">
                    <div
                      className="h-full rounded-r-[1px] transition-opacity"
                      style={{
                        width: `${Math.max((r.w / maxW) * 100, 0.6)}%`,
                        background: isProfit ? UP : DOWN,
                        opacity: isPoc ? 0.95 : isProfit ? 0.62 : 0.42,
                        outline: isPoc ? `1px solid ${isProfit ? UP : DOWN}` : undefined,
                      }}
                      title={`${r.p.toFixed(2)} · ${(r.w * 100).toFixed(1)}%${isPoc ? ' · POC' : ''}`}
                    />
                  </div>
                </div>
              )
            })}
            {/* 现价线：从价格标签右侧一直拉到最右 */}
            <div
              className="pointer-events-none absolute left-10 right-0 border-t border-dashed"
              style={{
                top: `${(1 - closePos) * 100}%`,
                borderColor: 'rgb(var(--foreground) / 0.55)',
              }}
            >
              <span className="absolute -top-[7px] right-0 rounded-sm bg-surface px-1 text-[9px] font-mono text-foreground">
                现价 {chip.close.toFixed(2)}
              </span>
            </div>
          </div>
        </div>

        {/* 右：派生读数 */}
        <div className="w-44 shrink-0 space-y-2.5 text-[10px]">
          {/* 获利盘 / 套牢盘 双色条 */}
          <div>
            <div className="flex items-center justify-between text-muted mb-0.5">
              <span>获利盘</span>
              <span className="font-mono text-foreground">{(profit * 100).toFixed(1)}%</span>
            </div>
            <div className="flex h-1.5 rounded-full overflow-hidden bg-elevated/60">
              <div style={{ width: `${profit * 100}%`, background: UP, opacity: 0.75 }} />
              <div style={{ width: `${trapped * 100}%`, background: DOWN, opacity: 0.6 }} />
            </div>
            <div className="flex items-center justify-between text-muted mt-0.5">
              <span>套牢盘</span>
              <span className="font-mono text-foreground">{(trapped * 100).toFixed(1)}%</span>
            </div>
          </div>

          <Row label="均价偏离" value={costGap == null ? '—' : `${costGap >= 0 ? '+' : ''}${costGap.toFixed(1)}%`}
               hint="现价相对筹码加权平均成本" />
          <Row label="90% 筹码带"
               value={chip.concentration == null ? '—' : `${(chip.concentration * 100).toFixed(0)}%`}
               hint="90% 筹码所在价格带宽度 / 中位价，越小越集中" />

          <div className="pt-1 border-t border-border/30 space-y-1">
            <div className="text-muted mb-1">近处套牢盘</div>
            {([['+5%', chip.overhead.p5], ['+10%', chip.overhead.p10], ['+20%', chip.overhead.p20]] as const).map(
              ([k, v]) => (
                <div key={k} className="flex items-center justify-between">
                  <span className="text-muted/80">{k} 以内</span>
                  <span className="font-mono text-foreground">{(v * 100).toFixed(1)}%</span>
                </div>
              ),
            )}
          </div>
        </div>
      </div>

      <p className="px-4 pb-3 text-[10px] leading-relaxed text-muted/80">
        筹码分布由价格与换手率唯一决定，只说明「成本堆在哪里」，<span className="text-foreground/70">不是收益预测</span>。
        现价以上是套牢盘（解套抛压），以下是获利盘（止盈抛压）—— 两侧都是抛压，方向相反。
      </p>
    </div>
  )
}

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-center justify-between" title={hint}>
      <span className="text-muted">{label}</span>
      <span className="font-mono text-foreground">{value}</span>
    </div>
  )
}
