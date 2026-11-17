/**
 * 结构异常扫描 —— 「最近 N 日，哪些票同时长出了多种不该同时出现的结构」。
 *
 * 这一块不是选股器。排序只看命中了多少种**不同**判据，不看历史收益：
 * 用全样本收益给当期打分，等于把未来信息放回过去（判据统计里那些 bp
 * 是「命中之后发生了什么」，不能反过来当作对当期的预测权重）。
 *
 * 行动出口只有一个：点进去看那一段到底长什么样。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ArrowRight, Loader2, RefreshCw } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { pctSigned, toneCls } from './calibers'

export function ScanPanel({ onPick }: {
  onPick: (symbol: string, start: string, end: string) => void
}) {
  const [lookback, setLookback] = useState(60)
  const q = useQuery({
    queryKey: QK.caseLabScan(lookback),
    queryFn: () => api.caseLabScan(undefined, lookback, 30),
    refetchOnWindowFocus: false,
    staleTime: 5 * 60_000,
  })

  const data = q.data
  const rows = data?.names ?? []

  return (
    <div className="px-5 py-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[11px] text-muted">
          <span>回看</span>
          {[20, 40, 60, 120].map(n => (
            <button
              key={n}
              onClick={() => setLookback(n)}
              className={cn(
                'rounded-btn border px-2 py-0.5 font-mono transition-colors',
                lookback === n
                  ? 'border-accent/40 bg-accent/10 text-accent'
                  : 'border-border bg-surface text-secondary hover:bg-elevated',
              )}
            >
              {n}日
            </button>
          ))}
          <span className="ml-2 font-mono text-muted">
            {data?.start ?? '—'} → {data?.as_of ?? '—'}
          </span>
        </div>
        <div className="flex items-center gap-3 text-[11px] text-muted">
          {data?.counts?.candidates != null && (
            <span className="font-mono">候选 {data.counts.candidates} · 显示 {data.counts.shown}</span>
          )}
          <button
            onClick={() => void q.refetch()}
            className="flex items-center gap-1 rounded-btn border border-border bg-surface px-2 py-1 text-[11px] text-secondary hover:bg-elevated hover:text-foreground"
          >
            <RefreshCw className={cn('h-3 w-3', q.isFetching && 'animate-spin')} /> 重扫
          </button>
        </div>
      </div>

      {data?.disclaimer && (
        <div className="mb-3 flex items-start gap-2 rounded-card border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] leading-relaxed text-secondary">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
          <span>{data.disclaimer}</span>
        </div>
      )}

      {q.isLoading && (
        <div className="flex items-center gap-2 py-10 text-xs text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在扫 {lookback} 个交易日的结构与分位…
        </div>
      )}
      {q.isError && (
        <div className="py-10 text-xs text-danger">扫描失败：{(q.error as Error).message}</div>
      )}
      {!q.isLoading && !q.isError && rows.length === 0 && (
        <div className="py-10 text-xs text-muted">
          这段时间没有票同时命中两种以上结构。可能是市场太安静，也可能是本地日线还没覆盖到这段区间。
        </div>
      )}

      {rows.length > 0 && (
        <div className="overflow-x-auto rounded-card border border-border bg-surface">
          <table className="w-full text-left text-[11px]">
            <thead className="text-muted">
              <tr className="border-b border-border">
                <th className="px-3 py-1.5 font-medium">标的</th>
                <th className="px-2 py-1.5 font-medium">命中的结构</th>
                <th className="px-2 py-1.5 text-right font-medium">区间涨幅</th>
                <th className="px-2 py-1.5 text-right font-medium">平均换手</th>
                <th className="px-2 py-1.5 text-right font-medium">累计成交</th>
                <th className="px-2 py-1.5 text-right font-medium">最后命中</th>
                <th className="px-3 py-1.5" />
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr
                  key={r.symbol}
                  className="cursor-pointer border-b border-border/60 last:border-0 hover:bg-elevated/60"
                  onClick={() => data?.start && onPick(r.symbol, data.start, data.as_of)}
                >
                  <td className="px-3 py-1.5">
                    <div className="flex items-baseline gap-2">
                      <span className="font-mono text-foreground">{r.symbol}</span>
                      <span className="text-secondary">{r.name ?? '—'}</span>
                      <span className={cn(
                        'rounded border px-1 font-mono text-[10px]',
                        r.kinds >= 4
                          ? 'border-danger/40 bg-danger/10 text-danger'
                          : 'border-border bg-elevated text-secondary',
                      )}>
                        {r.kinds}种
                      </span>
                    </div>
                  </td>
                  <td className="px-2 py-1.5">
                    <div className="flex flex-wrap gap-1">
                      {r.hits.map(h => (
                        <span
                          key={h.id}
                          className="rounded border border-border bg-elevated px-1 py-0.5 text-[10px] text-secondary"
                          title={`命中了 ${h.count} 次`}
                        >
                          {h.label}
                        </span>
                      ))}
                    </div>
                  </td>
                  <td className={cn('px-2 py-1.5 text-right font-mono', toneCls(r.cum_ret_pct))}>
                    {pctSigned(r.cum_ret_pct)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-secondary">
                    {r.turn_avg_pct.toFixed(2)}%
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-secondary">
                    {r.amount_yi.toFixed(1)}亿
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono text-muted">{r.last_date}</td>
                  <td className="px-3 py-1.5 text-right">
                    <span className="inline-flex items-center gap-0.5 text-[10px] text-accent">
                      解剖 <ArrowRight className="h-3 w-3" />
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="mt-3 text-[10px] leading-relaxed text-muted">
        判据全部只用 T 日及之前的数据。「命中」只说明结构同时出现，不说明有人操纵；
        同一形态在消息落地、指数调仓、复牌补跌里也会出现。
      </p>
    </div>
  )
}
