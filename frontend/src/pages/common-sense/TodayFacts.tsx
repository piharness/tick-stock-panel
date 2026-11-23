/**
 * 今日市场事实 —— 把整页的「实时层」提到最上面。
 *
 * 为什么要单独做成一条横带：上一版的问题是「大部分内容固定」，读者滚一遍就再也不会回来。
 * 这一版让页面每次打开都先呈现 9 个**当天才会变**的数字，并且每个数字都能点回它对应的
 * 那条常识——数字与知识互相索引，而不是两个互不相干的区块。
 *
 * 三条纪律：
 *   · 每个数字都带「口径」（N / 列名 / 日期），没口径的数字不许上屏；
 *   · 算不出来就显示「今日数据不足」，绝不显示 0 —— 0 会被读成「没有风险」；
 *   · 不做任何阈值判定（涨停、跌停由后端的 price_limits.py 判定），这里只做描述统计，
 *     免得前端成为「同一规则的第二定义点」。
 */
import { useMemo } from 'react'
import { Activity, ArrowUpRight } from 'lucide-react'
import type { MarketSnapshotRow } from '@/lib/api'
import { cn } from '@/lib/cn'
import { ALL_ANCHORS, computeAnchor, type AnchorId, type AnchorResult } from './anchors'

const TONE_VALUE: Record<AnchorResult['tone'], string> = {
  danger: 'text-red-600',
  warn: 'text-amber-600',
  ok: 'text-emerald-600',
  neutral: 'text-foreground',
}

export function TodayFacts({
  rows, asOf, onPick,
}: {
  rows: MarketSnapshotRow[]
  asOf: string | null
  onPick: (id: AnchorId) => void
}) {
  const anchors = useMemo(
    () => ALL_ANCHORS.map(id => computeAnchor(id, { rows, asOf })),
    [rows, asOf],
  )

  return (
    <section className="rounded-card border border-border bg-surface/80 p-3">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <Activity className="h-3.5 w-3.5 text-accent" />
        <h2 className="text-[12px] font-medium text-foreground">今日市场事实</h2>
        <span className="text-[10px] text-muted">
          {asOf ? `${asOf} 全市场横截面 · 点任意数字跳到它对应的那条常识` : '等待全市场快照…'}
        </span>
      </div>

      <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
        {anchors.map((a, i) => {
          const id = ALL_ANCHORS[i]
          if (!a) {
            return (
              <div
                key={id}
                className="rounded border border-border/50 bg-base/30 px-2 py-1.5"
                title="今日数据不足：缺列或全为空值，不给 0（0 会被读成「没有风险」）"
              >
                <div className="text-[10px] text-muted/70">{ANCHOR_LABEL[id]}</div>
                <div className="mt-0.5 font-mono text-[12px] text-muted/50">今日数据不足</div>
              </div>
            )
          }
          return (
            <button
              key={id}
              onClick={() => onPick(id)}
              className="group rounded border border-border/60 bg-base/20 px-2 py-1.5 text-left transition-colors hover:border-accent/40 hover:bg-elevated/40"
            >
              <div className="flex items-center gap-1">
                <span className="text-[10px] text-secondary">{a.label}</span>
                <ArrowUpRight className="h-2.5 w-2.5 text-muted/30 transition-colors group-hover:text-accent" />
              </div>
              <div className="mt-0.5 flex items-baseline gap-1">
                <span className={cn('font-mono text-[15px] font-semibold leading-none', TONE_VALUE[a.tone])}>
                  {a.value}
                </span>
                {a.unit && <span className="text-[9px] text-muted">{a.unit}</span>}
              </div>
              <p className="mt-1 line-clamp-2 text-[9px] leading-relaxed text-muted" title={a.read}>
                {a.read}
              </p>
              <p className="mt-0.5 truncate font-mono text-[8px] text-muted/50" title={a.sample}>
                {a.sample}
              </p>
            </button>
          )
        })}
      </div>
    </section>
  )
}

/** 数据不足时的占位标题（锚点算不出来时仍要显示「这里本该有什么」）。 */
const ANCHOR_LABEL: Record<AnchorId, string> = {
  breadth: '今日涨跌家数',
  friction: '今日摩擦成本',
  'st-move': 'ST 今日最大波动',
  'par-line': '今日收盘 < 1 元',
  'mcap-line': '今日市值低于红线',
  illiquid: '今日成交额 < 1000 万',
  'turnover-tail': '换手率 p50 / p99',
  'limit-up': '今日涨停 / 最高连板',
  concentration: '前 100 只成交额占比',
}
