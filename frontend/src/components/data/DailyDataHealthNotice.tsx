import { useEffect, useRef } from 'react'
import { AlertTriangle } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { type DailyDataRevisions, type IntegrityAsset } from '@/lib/api'
import { useDailyIntegrity } from '@/lib/useSharedQueries'
import { DAILY_DATA_QUERY_PREFIXES } from '@/lib/queryKeys'

const LABELS: Record<IntegrityAsset, string> = { stock: '股票', etf: 'ETF', index: '指数' }

export function DailyDataHealthNotice() {
  const health = useDailyIntegrity({ poll: true })
  const qc = useQueryClient()
  const previous = useRef<string | null>(null)
  const revisions = health.data?.data_revisions
  const version = revisions ? JSON.stringify(revisions) : null
  useEffect(() => {
    if (version === null) return
    if (version !== previous.current && (previous.current !== null || Object.values(revisions ?? {}).some(v => v !== '0'))) {
      // 后台自动修补/跨设备更新也要失效历史查询, 不依赖用户停留在数据页。
      void qc.invalidateQueries({ predicate: q => DAILY_DATA_QUERY_PREFIXES.has(String(q.queryKey[0])) })
    }
    previous.current = version
  }, [version, qc])
  const assets = health.data?.blocking_assets ?? []
  const entries = health.data?.blocking_entries ?? []
  const total = health.data?.blocking_total ?? entries.length
  if (!assets.length) return null
  // 横幅是这条信息的**唯一**常驻出口（请求侧不再为 daily_integrity 弹 toast），
  // 所以它必须说清「哪一天、哪一类、什么毛病」，而不是只说「有东西没修完」。
  const shown = entries.slice(0, 3)
  return (
    <div role="alert" className="mx-3 mt-2 flex flex-wrap items-center gap-2 rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span>{assets.map(a => LABELS[a]).join('、')}日K存在未完成的修补或重算，受影响的计算暂不可用。</span>
      {shown.length > 0 && (
        <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-warning/80">
          {shown.map(e => (
            <span key={`${e.asset_type}-${e.day}-${e.kind}`} className="font-mono">
              {LABELS[e.asset_type]} {e.day} · {e.reason}
            </span>
          ))}
          {total > shown.length && <span>等 {total} 条</span>}
        </span>
      )}
      <Link className="ml-auto shrink-0 underline underline-offset-2" to="/data">前往数据页处理</Link>
    </div>
  )
}

export function DailyResultStaleNotice({ result, assetType = 'stock' }: { result: unknown; assetType?: IntegrityAsset }) {
  const health = useDailyIntegrity()
  if (result === null || typeof result !== 'object') return null
  const revisions = (result as { daily_data_revisions?: DailyDataRevisions }).daily_data_revisions
  const current = health.data?.data_revisions?.[assetType]
  if (!current || current === (revisions?.[assetType] ?? '0')) return null
  return (
    <div role="status" className="flex items-start gap-2 rounded-btn border border-warning/30 bg-warning/10 px-3 py-2 text-xs leading-relaxed text-warning">
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span>日K数据版本已变化，下方结果仍基于旧数据，请完成修补后重新运行验证。</span>
    </div>
  )
}
