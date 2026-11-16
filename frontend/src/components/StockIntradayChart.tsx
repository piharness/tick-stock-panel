import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { api, type MinuteKlineRow, type MinuteNoneReason, minuteNoneRetryable } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { klineMinuteQueryOptions, minuteRefetchInterval } from '@/lib/kline'
import { EChartsIntraday } from '@/components/EChartsIntraday'
import type { DailySummary } from '@/lib/intraday-chart'

/** 空数据的病因 → 给用户看的一句话 (与后端 reason 码一一对应) */
const NONE_REASON_TEXT: Record<MinuteNoneReason, string> = {
  provider_call_failed: '分钟源这次请求抛错, 多为通道临时故障, 稍等再试',
  provider_unavailable: '分钟源插件当前不可用 (依赖缺失或未配置)',
  provider_no_dataset: '当前分钟源没有声明分钟数据集',
  provider_bad_datetime: '分钟源返回的时间口径不合法, 已被契约拒绝',
  no_minute_capability: '未选中可用分钟源, 且 TickFlow 无分钟权限',
  provider_no_rows: '分钟源没有这一天的数据 (停牌, 或超出该源历史深度)',
  no_rows: '本地与数据源都没有这一天的分钟数据',
}

interface Props {
  symbol: string
  date: string | null
  height?: number
  prevClose?: number
  dailySummary?: DailySummary
  className?: string
  onPriceHover?: (price: number | null) => void
  onPriceDoubleClick?: (price: number, currentPrice: number) => void
  currentPrice?: number
  priceLines?: { value: number; label?: string; color?: string }[]
  /** 自动刷新间隔(ms)。undefined/0 = 不轮询(默认)。个股对话框盘中实时刷新时传入。 */
  refetchIntervalMs?: number
}

export function StockIntradayChart({
  symbol,
  date,
  height = 520,
  prevClose,
  dailySummary,
  className,
  onPriceHover,
  onPriceDoubleClick,
  currentPrice,
  priceLines,
  refetchIntervalMs,
}: Props) {
  const qc = useQueryClient()
  const [minuteDismissed, setMinuteDismissed] = useState(false)

  const minute = useQuery({
    // 轮询上下文 (个股详情) 传 live: 当日盘中后端直接实时拉取最新K,
    // 避免读到分钟增量落盘的上一轮本地分区; 历史日期后端自行忽略 live。
    ...klineMinuteQueryOptions(symbol, date ?? undefined, refetchIntervalMs != null),
    enabled: !!symbol && !!date,
    refetchInterval: minuteRefetchInterval(refetchIntervalMs),
  })

  const fetchMinute = useMutation({
    mutationFn: () => api.syncMinuteSingle(symbol),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['kline-minute', symbol] })
      qc.invalidateQueries({ queryKey: QK.klineMinute(symbol, date ?? '') })
      setMinuteDismissed(false)
    },
  })

  const minuteRows: MinuteKlineRow[] = useMemo(() => minute.data?.rows ?? [], [minute.data?.rows])
  // source=none 表示本地无数据且 TickFlow 也拉不到 (停牌/复牌延迟/非交易日)
  // 此时不弹"是否获取"询问窗, 只做静态提示, 避免误导用户去拉明知拉不到的数据
  const sourceIsNone = minute.data?.source === 'none'
  // 指数分钟K无本地存储且不支持落库获取 (后端 sync_minute_single 显式拒绝), 不显示获取按钮
  const isIndex = minute.data?.asset_type === 'index'
  const noneReason = minute.data?.reason
  const retryable = minuteNoneRetryable(noneReason)
  const reasonText = NONE_REASON_TEXT[noneReason ?? 'no_rows']

  useEffect(() => {
    setMinuteDismissed(false)
    onPriceHover?.(null)
  }, [date, onPriceHover])

  if (!symbol || !date) return null

  return (
    <div className={className} style={{ height, flexShrink: 0 }}>
      {minute.isLoading && <div className="text-xs text-muted py-2">分时加载中…</div>}
      {!minute.isLoading && minuteRows.length === 0 && (
        <>
          {fetchMinute.isPending ? (
            <div className="flex items-center justify-center h-full gap-2 text-xs text-accent">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              <span>正在获取分钟K数据…</span>
            </div>
          ) : isIndex ? (
            // 指数: 分钟K仅支持实时读取, 无落库获取入口
            <div className="flex items-center justify-center h-full text-xs text-muted">指数暂无分钟数据</div>
          ) : sourceIsNone ? (
            // 拉空分两档: 「这次没拉到」可重试, 「源给不出这一天」重试无用。
            // 二者原来合成一句「数据源未提供」—— 通道故障被说成源的能力缺失,
            // 用户于是去换数据源, 而正确动作是再拉一次。
            <div className="flex flex-col items-center justify-center h-full gap-3">
              <div className="text-xs text-muted">
                {retryable
                  ? '分钟数据暂时没取到（数据源请求失败）'
                  : '该日暂无分钟数据（数据源未提供）'}
              </div>
              {retryable && minute.data?.reason && (
                <div className="text-[10px] text-muted/70">{reasonText}</div>
              )}
              <button
                onClick={() => fetchMinute.mutate()}
                className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs font-medium hover:bg-elevated/80 transition-colors duration-150"
              >
                {retryable ? '重新获取' : '再试一次'}
              </button>
            </div>
          ) : minuteDismissed ? (
            <div className="flex flex-col items-center justify-center h-full gap-3">
              <div className="text-xs text-muted">暂无分钟数据</div>
              <button
                onClick={() => setMinuteDismissed(false)}
                className="px-4 py-1.5 rounded-btn bg-accent/90 text-white text-xs font-medium hover:bg-accent transition-colors duration-150"
              >
                获取分钟K
              </button>
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-full gap-4">
              <div className="text-sm text-foreground">是否立即获取最近5日分钟K？</div>
              <div className="flex items-center gap-3">
                <button
                  onClick={() => fetchMinute.mutate()}
                  className="px-4 py-1.5 rounded-btn bg-accent/90 text-white text-xs font-medium hover:bg-accent transition-colors duration-150"
                >
                  确定
                </button>
                <button
                  onClick={() => setMinuteDismissed(true)}
                  className="px-4 py-1.5 rounded-btn bg-elevated text-secondary text-xs hover:bg-elevated/80 transition-colors duration-150"
                >
                  取消
                </button>
              </div>
            </div>
          )}
        </>
      )}
      {minuteRows.length > 0 && (
        <>
          {minute.data?.source === 'local_partial' && (
            <div className="mb-1 flex items-center gap-2 rounded border border-warning/30 bg-warning/5 px-2 py-1 text-[10px] text-warning">
              <span>
                该日分钟数据不完整：{minute.data.actual ?? minuteRows.length}/{minute.data.expected ?? 240} 根
              </span>
              <button
                type="button"
                onClick={() => fetchMinute.mutate()}
                disabled={fetchMinute.isPending}
                className="ml-auto shrink-0 rounded border border-warning/40 px-1.5 py-0.5 hover:bg-warning/10 disabled:opacity-50"
              >
                {fetchMinute.isPending ? '补齐中…' : '尝试补齐'}
              </button>
            </div>
          )}
          <EChartsIntraday
            data={minuteRows}
            height={height}
            prevClose={minute.data?.prev_close ?? prevClose}
            dailySummary={dailySummary}
            date={date}
            priceLimit={minute.data?.price_limit ?? undefined}
            onPriceHover={onPriceHover}
            onPriceDoubleClick={onPriceDoubleClick}
            currentPrice={currentPrice}
            priceLines={priceLines}
          />
        </>
      )}
    </div>
  )
}
