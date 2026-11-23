/**
 * 退市「连续 20 日」倒计时 —— 把「破线」升级成「规则距离计数」。
 *
 * 横截面快照只能回答「今天破没破线」，而交易所规则要的是「连续 20 个
 * 交易日」。一只票破面值 1 天和 19 天，对散户是两种完全不同的危险等级，
 * 但快照里长得一模一样。本组件接 /api/stock-analysis/delisting-countdown，
 * 直接列出「当前正在倒数」的每一只票，并画 20 格倒计时条。
 *
 * 诚实边界：
 *   - 只含「已连续 ≥1 日破线」的票，未破线的票不占版面；
 *   - streak 是「从最新一根往回数连续跌破」的天数，中间断一天就归零；
 *   - 面值用原始价 raw_close 判，市值按板块阈值（主板 5 亿 / 其余 3 亿）。
 */
import { useMemo, useState } from 'react'
import { Hourglass, ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '@/lib/cn'
import type { DelistingCountdown as DelistingCountdownData, DelistingCountdownRow } from '@/lib/api'

/** 危险等级：离 20 日死线越近越红。 */
function dangerTone(streak: number, limit: number): { text: string; bar: string; label: string } {
  const ratio = streak / limit
  if (ratio >= 0.9) return { text: 'text-red-600', bar: 'bg-red-500', label: '濒临终止' }
  if (ratio >= 0.5) return { text: 'text-orange-600', bar: 'bg-orange-500', label: '高危' }
  if (ratio >= 0.25) return { text: 'text-amber-600', bar: 'bg-amber-500', label: '警戒' }
  return { text: 'text-amber-600/80', bar: 'bg-amber-400', label: '刚入倒数' }
}

function CountdownBar({ streak, limit }: { streak: number; limit: number }) {
  const tone = dangerTone(streak, limit)
  return (
    <div className="flex items-center gap-1.5">
      <div className="flex flex-1 gap-[2px]">
        {Array.from({ length: limit }).map((_, i) => (
          <span
            key={i}
            className={cn('h-[4px] flex-1 rounded-sm', i < streak ? tone.bar : 'bg-border/50')}
          />
        ))}
      </div>
      <span className={cn('shrink-0 font-mono text-[10px]', tone.text)}>{streak}/{limit}</span>
    </div>
  )
}

function Row({ row }: { row: DelistingCountdownRow }) {
  const [open, setOpen] = useState(false)
  const mainStreak = Math.max(row.par_streak, row.mcap_streak)
  const tone = dangerTone(mainStreak, row.streak_limit)
  const reasons: string[] = []
  if (row.par_streak > 0) reasons.push(`面值 ${row.close?.toFixed(3) ?? '?'} 元，连续 ${row.par_streak} 日 < 1 元`)
  if (row.mcap_streak > 0) reasons.push(`市值 ${((row.mcap ?? 0) / 1e8).toFixed(2)} 亿，连续 ${row.mcap_streak} 日破 ${row.board === '主板' ? 5 : 3} 亿线`)

  return (
    <div className="overflow-hidden rounded border border-border/60 bg-surface/40">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-elevated/30"
      >
        <span className="w-3 shrink-0 text-muted/50">{open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}</span>
        <span className="w-24 shrink-0 font-medium text-foreground">{row.symbol}</span>
        <span className="w-12 shrink-0 text-[10px] text-muted">{row.board}</span>
        <span className="w-16 shrink-0 text-right font-mono text-[11px] text-secondary">{row.close?.toFixed(3)}</span>
        <span className="flex-1 min-w-0"><CountdownBar streak={mainStreak} limit={row.streak_limit} /></span>
        <span className={cn('w-16 shrink-0 text-right text-[10px] font-medium', tone.text)}>{tone.label}</span>
      </button>
      {open && (
        <div className="space-y-1.5 border-t border-border/40 px-3 py-2">
          <div className="text-[10px] leading-relaxed text-secondary">
            触及红线：{reasons.length ? reasons.join('；') : '—'}
          </div>
          {row.par_streak > 0 && (
            <div className="flex items-center gap-2 text-[10px] text-muted">
              <span className="w-16 shrink-0">面值</span>
              <span className="flex-1"><CountdownBar streak={row.par_streak} limit={row.streak_limit} /></span>
            </div>
          )}
          {row.mcap_streak > 0 && (
            <div className="flex items-center gap-2 text-[10px] text-muted">
              <span className="w-16 shrink-0">市值</span>
              <span className="flex-1"><CountdownBar streak={row.mcap_streak} limit={row.streak_limit} /></span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function DelistingCountdown({
  data, loading, error,
}: {
  data: DelistingCountdownData | undefined
  loading: boolean
  error: boolean
}) {
  const rows = data?.countdown ?? []

  const maxStreak = useMemo(() => Math.max(0, ...rows.map(r => Math.max(r.par_streak, r.mcap_streak))), [rows])

  if (loading && !data) {
    return (
      <div className="rounded border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted">
        正在算全市场「连续 20 日」倒计时…
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="rounded border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted">
        倒计时数据不可用（接口失败）—— 不代表没有票在倒数。
      </div>
    )
  }

  if (rows.length === 0) {
    return (
      <div className="rounded border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted">
        当前无个股连续破面值/市值线。这不是「安全」，只是「今天没有票在倒数」——
        规则是动态的，明天可能就有。
      </div>
    )
  }

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-muted">
        <span className="flex items-center gap-1">
          <Hourglass className="h-3 w-3 text-warning" />
          正在连续破线的 <b className="text-foreground">{rows.length}</b> 只票（数据截至 {data?.as_of}）
        </span>
        <span>
          最危险的一只已连续 <b className="text-red-600">{maxStreak}</b> / 20 日
        </span>
      </div>
      <div className="space-y-1.5">
        {rows.map(r => <Row key={r.symbol} row={r} />)}
      </div>
      {data?.caveats?.length ? (
        <p className="mt-2 text-[9px] leading-relaxed text-muted/70">
          {data.caveats.map(c => `· ${c}`).join(' ')}
        </p>
      ) : null}
      <p className="mt-2 text-[9px] leading-relaxed text-muted/70">
        · 倒计时从最新一根往回数连续跌破的天数，中间断一天就归零；这是「规则距离」，不是「退市概率」。
      </p>
    </div>
  )
}
