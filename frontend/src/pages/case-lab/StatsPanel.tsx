/**
 * 判据检验 —— 每条手法的「命中之后发生了什么」。
 *
 * 三件事必须同时出现，缺一个这张卡就不能读：
 *   ① 命中样本量（可买入 / 全部）与日均命中数
 *   ② 1 / 5 / 20 日的**超额**（相对同日 × 动量五档 × 成交额五档的对照组）
 *   ③ 中位数与胜率 —— 均值会被少数极端值拉走，中位数不会
 *
 * t 值一律标注「参考」：同一只票连续多日命中时，日序列仍高度自相关，
 * 这里没有做 Newey-West 调整。把 t 当显著性证据是这一类页面最常见的错误。
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, Loader2, RefreshCw } from 'lucide-react'
import { api, type FpWindowStat, type FingerprintStatItem } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { FAMILY_META, bp, ratioPct, tHint, tText, toneCls } from './calibers'

const WINDOWS = ['1', '5', '20'] as const
const WINDOW_LABEL: Record<string, string> = { '1': 'T+1', '5': 'T+5', '20': 'T+20' }

export function StatsPanel() {
  const defs = useQuery({
    queryKey: QK.caseLabFingerprints(),
    queryFn: () => api.caseLabFingerprints(),
    staleTime: Infinity,
  })
  const stats = useQuery({
    queryKey: QK.caseLabStats(),
    queryFn: () => api.caseLabStats(),
    refetchOnWindowFocus: false,
    staleTime: 10 * 60_000,
  })
  const [rebuilding, setRebuilding] = useState(false)

  const rebuild = async () => {
    setRebuilding(true)
    try {
      await api.caseLabRebuildStats()
      await stats.refetch()
    } finally {
      setRebuilding(false)
    }
  }

  const meta = new Map((defs.data?.items ?? []).map(d => [d.id, d]))
  const items = stats.data?.items ?? []
  const s = stats.data

  return (
    <div className="px-5 py-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-[11px]">
        <div className="flex flex-wrap items-center gap-3 text-muted">
          <span>
            窗口 <span className="font-mono text-secondary">{s?.years ?? '—'}</span> 年
            {s?.bars != null && <> · <span className="font-mono text-secondary">{s.bars}</span> 个交易日</>}
            {s?.rows != null && <> · <span className="font-mono text-secondary">{(s.rows / 1e4).toFixed(0)}万</span> 行</>}
          </span>
          <span className="font-mono">截至 {s?.as_of ?? '—'}</span>
          {s?.generated_at && <span className="font-mono text-[10px]">重算于 {s.generated_at}</span>}
        </div>
        <button
          onClick={() => void rebuild()}
          disabled={rebuilding}
          className="flex items-center gap-1 rounded-btn border border-border bg-surface px-2.5 py-1 text-[11px] text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw className={cn('h-3 w-3', rebuilding && 'animate-spin')} />
          {rebuilding ? '重算中…（约 6 秒）' : '重算'}
        </button>
      </div>

      <div className="mb-3 flex items-start gap-2 rounded-card border border-warning/30 bg-warning/5 px-3 py-2 text-[11px] leading-relaxed text-secondary">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
        <span>
          超额 = 命中样本的收益 − <b>同日 × 20 日动量五档 × 成交额五档</b>对照组均值。
          T+1 一律是「T+1 开盘买 → 收盘卖」，并剔除开盘即涨停买不进的样本；
          可买入比例低于 100% 的判据，说明有一部分命中根本买不到。
          <span className="text-muted"> t 值只作参考：命中常连续多日发生在同一只票上，日序列仍自相关。</span>
        </span>
      </div>

      {stats.isLoading && (
        <div className="flex items-center gap-2 py-10 text-xs text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 首次打开要算一遍全历史（约 6 秒），之后读盘上缓存…
        </div>
      )}
      {stats.isError && (
        <div className="py-10 text-xs text-danger">统计读取失败：{(stats.error as Error).message}</div>
      )}

      <div className="space-y-3">
        {items.map(it => (
          <FpCard key={it.id} item={it} def={meta.get(it.id)} />
        ))}
      </div>

      {!stats.isLoading && items.length === 0 && (
        <div className="py-10 text-xs text-muted">没有可读的统计。点「重算」生成。</div>
      )}
    </div>
  )
}

function FpCard({
  item, def,
}: {
  item: FingerprintStatItem
  def?: { thesis: string; rule: string; caveat: string; family: string }
}) {
  const fam = FAMILY_META[item.family]
  return (
    <section className="rounded-card border border-border bg-surface p-3">
      <div className="mb-2 flex flex-wrap items-baseline gap-2">
        <span
          className="inline-block h-2 w-2 rounded-full"
          style={{ background: fam?.color ?? '#64748b' }}
        />
        <h3 className="text-[13px] font-medium text-foreground">{item.label}</h3>
        <span className="rounded border border-border bg-elevated px-1.5 py-0.5 text-[9px] text-secondary">
          {fam?.label ?? item.family}
        </span>
        <span className="ml-auto flex items-center gap-3 font-mono text-[10px] text-muted">
          <span>命中 {item.n_raw} 行</span>
          <span>可买入 {ratioPct(item.buyable_ratio)}</span>
          <span>占全样本 {ratioPct(item.hit_rate, 2)}</span>
          {item.state === 'insufficient' && <span className="text-warning">样本不足</span>}
        </span>
      </div>

      {def && (
        <div className="mb-2 space-y-1 text-[10px] leading-relaxed">
          <div className="text-secondary">{def.thesis}</div>
          <div className="rounded border border-border/60 bg-elevated/40 px-2 py-1 font-mono text-[10px] text-secondary">
            {def.rule}
          </div>
          <div className="flex gap-1 text-muted">
            <span className="shrink-0 text-warning">它会骗你：</span>
            <span>{def.caveat}</span>
          </div>
        </div>
      )}

      <div className="overflow-x-auto">
        <table className="w-full text-left text-[10px]">
          <thead className="text-muted">
            <tr className="border-b border-border">
              <th className="py-1 pr-2 font-medium">窗口</th>
              <th className="px-2 py-1 text-right font-medium">超额均值</th>
              <th className="px-2 py-1 text-right font-medium">超额中位</th>
              <th className="px-2 py-1 text-right font-medium">胜率</th>
              <th className="px-2 py-1 text-right font-medium" title="有命中的交易日数，不是独立事件数——同一只票常连续多日命中">命中日数</th>
              <th className="px-2 py-1 text-right font-medium">显著性</th>
            </tr>
          </thead>
          <tbody>
            {WINDOWS.map(w => {
              const st: FpWindowStat | undefined = item.fwd[w]
              const hint = tHint(st?.t)
              return (
                <tr key={w} className="border-b border-border/40 last:border-0">
                  <td className="py-1 pr-2 font-mono text-secondary">{WINDOW_LABEL[w]}</td>
                  <td className={cn('px-2 py-1 text-right font-mono', toneCls(st?.mean_bp))}>
                    {bp(st?.mean_bp)}
                  </td>
                  <td className={cn('px-2 py-1 text-right font-mono', toneCls(st?.median_bp))}>
                    {bp(st?.median_bp)}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-secondary">
                    {st?.win_rate != null ? `${(st.win_rate * 100).toFixed(0)}%` : '—'}
                  </td>
                  <td className="px-2 py-1 text-right font-mono text-muted">{st?.n_days ?? '—'}</td>
                  <td className="px-2 py-1 text-right">
                    <span className={cn('font-mono text-[9px]', hint.cls)}>
                      {tText(st?.t)} · {hint.label}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </section>
  )
}
