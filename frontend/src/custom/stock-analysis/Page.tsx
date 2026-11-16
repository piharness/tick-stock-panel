import { useState, useEffect, useMemo, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Sparkles, LineChart, History as HistoryIcon, Loader2, ExternalLink, Bell, AlertTriangle,
} from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { EmptyState } from '@/components/EmptyState'
import { StockFinancialSearch } from '@/components/financials/StockFinancialSearch'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { LastStockChip } from '@/components/LastStockChip'
import {
  AnalysisKChart, type AnalysisKChartHandle,
  type PriceLevel, type LevelType, type ChartRange, type ChartVLine,
} from '@/components/stock-analysis/AnalysisKChart'
import { PriceAlertDialog } from '@/components/stock-analysis/PriceAlertDialog'
import { BarReadout, RangeBar, RangeStatsStrip, NameTrace } from '@/components/stock-analysis/RangeControls'
import { api, type ChartSegment, type ChartSeries } from '@/lib/api'
import { useLastStock } from '@/lib/useLastStock'
import { QK } from '@/lib/queryKeys'
import { klineDailyQueryOptions } from '@/lib/kline'
import {
  coverageGap, loadStoredRange, normalizeRange, presetRange, rangeStats, saveStoredRange,
  windowToIndex,
  type DateRange, type RangePreset, type StoredRange,
} from '@/lib/chartRange'
import { toast } from '@/components/Toast'
import {
  startAnalysis, findTodayReport, useHistoryReports,
  deleteReport, openHistoryReport, loadHistory,
} from '@/lib/stockAnalysisStore'

/**
 * 个股分析页 —— 日 K + 关键价位(压力/支撑/密集区/枢轴/前高前低)+ AI 四维分析。
 *
 * 与财务分析页的区别:
 *  - 以【行情 + 关键价位】为视觉主体(专用日 K 图表,不复用个股对话框图表)
 *  - AI 分析输出客观技术状态与风险提示(非买卖建议、非财务质量评级)
 *  - 报告胶囊用蓝色系,与财务分析(紫色)并存
 *
 * 时间轴(2026-11 加): 这一页原来固定"最近 250 根"，等于只承认一个窗口。
 * 要看「从华夏幸福到*ST华幸」这种跨年形态，必须能指定起止日；而指定之后
 * 立刻出现两个新问题，页面必须当场回答，不能假装没有：
 *   1. 这一段本地到底有没有？（日K表覆盖 2025-05 起，2024-09 起就是缺口）
 *   2. 缺口是谁的？（本地没铺到 → 可以整市场向前扩展；这只票没数据 → 补也没用）
 * 入口走 URL：?symbol=600340.SH&name=*ST华幸，博弈链路那边一键点进来。
 */

const DEFAULT_PRESET: RangePreset = '1Y'

function rangeFor(symbol: string): StoredRange {
  const stored = loadStoredRange(symbol)
  if (stored) return stored
  const r = presetRange(DEFAULT_PRESET)!
  return { preset: DEFAULT_PRESET, start: r.start, end: r.end }
}

function resolveRequested(value: StoredRange, tableEarliest: string | null | undefined): DateRange {
  if (value.preset === 'ALL') {
    return { start: tableEarliest || '1990-01-01', end: value.end }
  }
  if (value.preset === 'CUSTOM') {
    const fallback = presetRange(DEFAULT_PRESET)!
    return normalizeRange({ start: value.start, end: value.end }, fallback)
  }
  return presetRange(value.preset) ?? normalizeRange({ start: value.start, end: value.end }, presetRange(DEFAULT_PRESET)!)
}

export function StockAnalysis() {
  const [searchParams, setSearchParams] = useSearchParams()
  const { last: lastStock, remember: rememberStock } = useLastStock('stock-analysis')

  // 入口参数优先于"上次看的票"：从博弈链路点进来必须落到那一只，
  // 否则用户会看到自己三天前看过的一只不相干的票，还以为点错了。
  const [symbol, setSymbol] = useState<string>(() => searchParams.get('symbol') || lastStock?.symbol || '')
  const [name, setName] = useState<string>(() => {
    const fromUrl = searchParams.get('name') || ''
    if (fromUrl) return fromUrl
    const sym = searchParams.get('symbol')
    return sym && sym === lastStock?.symbol ? lastStock.name : ''
  })

  const [checking, setChecking] = useState(false)
  const [confirmReport, setConfirmReport] = useState<{ id: string; created_at: string; focus: string } | null>(null)
  const [previewSymbol, setPreviewSymbol] = useState<string | null>(null)
  const [showPriceAlerts, setShowPriceAlerts] = useState(false)

  // 进入页面立即加载历史报告(供右侧常驻列表)。store 内部有 historyLoaded 去重, 重复调用安全。
  useEffect(() => { loadHistory() }, [])

  // 外部改 URL（浏览器前进/后退、别的页面带参跳转）也要落到那只票
  useEffect(() => {
    const sym = searchParams.get('symbol') || ''
    if (!sym || sym === symbol) return
    setSymbol(sym)
    setName(searchParams.get('name') || '')
    rememberStock(sym, searchParams.get('name') || '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  const onSelect = (sym: string, nm: string) => {
    setSymbol(sym)
    setName(nm)
    setConfirmReport(null)
    setShowPriceAlerts(false)
    rememberStock(sym, nm)
    const next = new URLSearchParams(searchParams)
    next.set('symbol', sym)
    if (nm) next.set('name', nm); else next.delete('name')
    setSearchParams(next, { replace: true })
  }

  const handleAnalyze = async () => {
    if (!symbol || checking) return
    setChecking(true)
    try {
      // 当日已分析过 → 二次确认(查看今日报告 / 重新分析)
      const today = await findTodayReport(symbol)
      if (today) {
        setConfirmReport({ id: today.id, created_at: today.created_at, focus: today.focus })
      } else {
        await doAnalysis()
      }
    } catch {
      await doAnalysis()
    } finally {
      setChecking(false)
    }
  }

  const doAnalysis = async () => {
    const r = await startAnalysis(symbol, name)
    if (r.error) toast(r.error, 'error')
  }

  return (
    <>
      <PageHeader
        title="个股分析"
        subtitle="日 K · 任意区间 · 关键价位 · AI 四维分析(技术 / 基本面 / 财务 / 消息面)"
        right={
          <div className="flex items-center gap-2">
            <LastStockChip stock={lastStock} onSelect={onSelect} />
          </div>
        }
      />

      <div className="w-full px-8 py-6 space-y-6">
        {/* 搜索栏 */}
        <div className="flex items-center gap-3">
          <div className="w-72">
            <StockFinancialSearch onSelect={onSelect} assetTypes="stock,index" />
          </div>
          {symbol && (
            <>
              <button
                onClick={() => setPreviewSymbol(symbol)}
                title="查看个股日 K 详情"
                className="group flex items-center gap-2 text-sm rounded-md px-1.5 py-0.5 -mx-1.5 hover:bg-elevated transition-colors"
              >
                <span className="text-foreground font-medium group-hover:text-sky-300 transition-colors">{name || symbol}</span>
                <span className="text-[10px] font-mono text-muted">{symbol}</span>
                <ExternalLink className="h-3 w-3 text-muted opacity-0 group-hover:opacity-100 transition-opacity" />
              </button>
              <button
                onClick={handleAnalyze}
                disabled={checking}
                title="让模型读这只票的日K+关键价位+财务表，输出技术/基本面/财务/消息面四维分析（客观状态与风险，不是买卖建议）。需要先在设置里配好 AI key；当天已生成过会先问你要看历史还是重算。"
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn bg-gradient-to-r from-sky-500/25 to-blue-500/15 border border-sky-400/30 text-sky-300 text-xs font-medium hover:from-sky-500/35 hover:to-blue-500/25 transition-all disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {checking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                AI 个股分析
              </button>
              <button
                onClick={() => setShowPriceAlerts(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-btn border border-sky-400/25 bg-sky-400/[0.08] text-sky-300 text-xs font-medium hover:border-sky-400/40 hover:bg-sky-400/[0.12] transition-all"
                title="给这只票设价格上穿/下穿提醒（本地轮询触发，到点弹通知与声音）"
              >
                <Bell className="h-3.5 w-3.5" />
                点位提醒
              </button>
            </>
          )}
        </div>

        {/* 主体:左侧当前个股看板 + 右侧常驻历史报告 */}
        <div className="grid grid-cols-[1fr_288px] gap-6 items-start">
          <div className="min-w-0">
            {!symbol ? (
              <EmptyState
                icon={LineChart}
                title="选择一只股票开始分析"
                hint="搜索代码或名称,查看日 K 与关键价位,并可让 AI 进行技术面 / 基本面 / 财务面 / 消息面四维综合分析。"
              />
            ) : (
              <StockAnalysisBoard
                key={symbol}
                symbol={symbol}
                name={name}
                onName={nm => { setName(nm); rememberStock(symbol, nm) }}
              />
            )}
          </div>
          <HistorySidebar />
        </div>
      </div>

      {/* 二次确认:已有历史报告 */}
      {confirmReport && (
        <ConfirmModal
          report={confirmReport}
          onView={() => { openHistoryReport(confirmReport.id); setConfirmReport(null) }}
          onRedo={async () => { setConfirmReport(null); await doAnalysis() }}
          onClose={() => setConfirmReport(null)}
        />
      )}

      {/* 个股日 K 详情对话框(点击名称/代码打开) */}
      <StockPreviewDialog
        symbol={previewSymbol}
        name={previewSymbol === symbol ? name : undefined}
        triggerInfo={null}
        onClose={() => setPreviewSymbol(null)}
      />

      {showPriceAlerts && symbol && (
        <PriceAlertDialog
          key={symbol}
          symbol={symbol}
          name={name}
          onClose={() => setShowPriceAlerts(false)}
        />
      )}
    </>
  )
}

// ===== 分析看板:区间 + 日 K + 关键价位 =====
function StockAnalysisBoard({ symbol, name, onName }: {
  symbol: string
  name: string
  onName: (name: string) => void
}) {
  const qc = useQueryClient()

  // 区间按标的记忆:换股票回到默认 1 年,切走再回来还是上次那一段
  const [range, setRange] = useState<StoredRange>(() => rangeFor(symbol))
  const chartRef = useRef<AnalysisKChartHandle>(null)
  /** 光标所在那根 K 的日期（null=鼠标不在图上，读数条回落到最后一根） */
  const [hoverDate, setHoverDate] = useState<string | null>(null)
  /** 当前可视窗口（百分比）。null = 还没拖过，等于整个所选区间 */
  const [viewport, setViewport] = useState<{ start: number; end: number } | null>(null)
  const changeRange = (next: StoredRange) => {
    const v = next.preset === 'CUSTOM' ? next : { ...next }
    setRange(v)
    saveStoredRange(symbol, v)
  }

  // 点过「向前补历史」之后记下时刻：缺口消失前每 20s 看一眼覆盖边界，
  // 后台任务落盘的那一刻这一页就自己恢复，不需要用户记住回来刷新。
  const [extendWatch, setExtendWatch] = useState<number | null>(null)
  const requestedRef = useRef<DateRange>({ start: '', end: '' })

  // 本地日K表的覆盖边界（不是这只票的边界）：缺口到底能不能补，全靠这个数
  const status = useQuery({
    queryKey: QK.dataStatus,
    queryFn: api.dataStatus,
    staleTime: 60_000,
    refetchOnWindowFocus: false,
    refetchInterval: q => {
      if (!extendWatch) return false
      const earliest = (q.state.data as { daily?: { earliest_date?: string | null } } | undefined)?.daily?.earliest_date
      const ask = requestedRef.current.start
      // '1990-01-01' 这种无界起点（ALL）永远"缺口存在"，不参与轮询
      const stillMissing = !!earliest && ask > '1991-01-01' && earliest >= ask
      const withinBudget = Date.now() - extendWatch < 30 * 60_000
      return stillMissing && withinBudget ? 20_000 : false
    },
  })
  const tableEarliest = status.data?.daily?.earliest_date ?? null

  const requested = useMemo(() => resolveRequested(range, tableEarliest), [range, tableEarliest])
  requestedRef.current = requested

  // 换区间 = 换了一批 K 线，上一批的缩放百分比对新数据没有意义（更别说统计条会用错段）。
  // 图表内部按数据签名自己会重置视图，这里同步把页面的视窗/光标清掉。
  useEffect(() => {
    setViewport(null)
    setHoverDate(null)
  }, [requested.start, requested.end])

  /**
   * 请求起点早于本地日K覆盖 → 走「本地 + 上游前复权补齐」那条接口。
   *
   * 为什么不在本地为空时整段回退（`/api/kline/daily` 的做法）：那条回退分支用
   * `days`（默认 120）而不是请求区间，一个两年的请求会静默拿到 150 根；而且
   * 它返回的是上游原始价，遇到送转就是一根假跳空。`/history` 明确按区间分段
   * 拼接，并把每段来源写进响应。
   */
  const fillMode = !!tableEarliest && requested.start < tableEarliest
  const kline = useQuery<ChartSeries>({
    ...(fillMode
      ? {
          queryKey: ['stock-analysis', 'history', symbol, requested.start, requested.end],
          queryFn: () => api.stockAnalysisHistory(symbol, requested.start, requested.end),
        }
      : klineDailyQueryOptions(symbol, requested)),
    enabled: !!symbol,
    // 补齐段是"临时取来的"，5 分钟内不复取；入库路径走「向前补历史」那个按钮
    staleTime: fillMode ? 5 * 60_000 : 60_000,
    refetchOnWindowFocus: false,
    placeholderData: (prev: any, prevQuery: any) =>
      (prevQuery?.queryKey?.[1] === symbol ? prev : undefined),
  })

  const levelsQ = useQuery({
    queryKey: QK.stockLevels(symbol),
    queryFn: () => api.stockAnalysisLevels(symbol, 250),
    enabled: !!symbol,
    staleTime: 60_000,
  })

  // 简称轨迹：只在龙虎榜快照见过 ≥2 个简称时才有内容（有证据才显示）
  const nameHistory = useQuery({
    queryKey: ['stock-analysis', 'name-history', symbol],
    queryFn: () => api.stockAnalysisNameHistory(symbol),
    enabled: !!symbol,
    staleTime: 30 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })

  // 名称以接口/维表为准（URL 里带的名字可能来自另一张表或旧缓存）
  useEffect(() => {
    const nm = kline.data?.name || nameHistory.data?.current
    if (nm && nm !== name) onName(nm)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kline.data?.name, nameHistory.data?.current])

  const extend = useMutation({
    mutationFn: (months: number) => api.extendHistory(months, 'month'),
    onSuccess: () => {
      setExtendWatch(Date.now())
      toast('已开始「向前扩展历史」（整市场后台任务）—— 落盘后本页会自动重取', 'success')
      void qc.invalidateQueries({ queryKey: QK.pipelineJobs })
      void qc.invalidateQueries({ queryKey: QK.dataStatus })
    },
    // 失败不在这里再弹一次：`request()` 已把后端 detail 原样 toast 出来。
    // 409 的 detail 本身写清了原因（缺除权因子 → 会写入未复权价 → 假跳空）与两条正路。
  })

  if (kline.isLoading && !kline.data) {
    return <div className="flex items-center justify-center py-20"><Loader2 className="h-5 w-5 animate-spin text-muted" /></div>
  }

  if (kline.isError) {
    return (
      <EmptyState
        icon={AlertTriangle}
        title="日 K 数据加载失败"
        hint="请检查网络或数据源配置后重试。"
      />
    )
  }

  const rows = kline.data?.rows ?? []
  const rowsFirst = rows.length ? String(rows[0].date).slice(0, 10) : null
  const rowsLast = rows.length ? String(rows[rows.length - 1].date).slice(0, 10) : null
  const segments: ChartSegment[] = kline.data?.segments ?? []
  const upstreamSeg = segments.find(s => s.source === 'upstream') ?? null
  // 维表里没有这只票 ⇒ 空态该说"没有这只票"，而不是"换一段区间"（换了也没用）
  const unknownSymbol = kline.data?.known === false
  const gap = coverageGap(requested.start, rowsFirst, tableEarliest)
  const stats = rangeStats(rows)

  // 视窗统计：拖动缩放条之后，"现在看见的这段"才是他在判断的那段。
  // 下标换算与图表的平移/缩放共用 chartRange 里的同一组纯函数。
  const viewSlice = (() => {
    if (!viewport || rows.length === 0) return null
    const [i0, i1] = windowToIndex(viewport, rows.length)
    if (i1 - i0 + 1 >= rows.length) return null   // 和整个区间一样，就别再算一遍
    return { i0, i1 }
  })()
  const visibleStats = viewSlice ? rangeStats(rows.slice(viewSlice.i0, viewSlice.i1 + 1)) : stats

  // 读数条：光标那根；没有光标就是最后一根
  const readIdx = (() => {
    if (!hoverDate) return rows.length - 1
    const idx = rows.findIndex(r => String(r.date).slice(0, 10) === hoverDate)
    return idx >= 0 ? idx : rows.length - 1
  })()
  const readRow = readIdx >= 0 ? rows[readIdx] : null
  const readPrev = readIdx > 0 ? rows[readIdx - 1] : undefined

  const levels = (levelsQ.data?.levels ?? {}) as Record<LevelType, PriceLevel[]>
  const levelDates = levelsQ.data?.dates ?? []
  const levelSpan = levelDates.length > 1
    ? `${levelDates[0].slice(0, 10)} ~ ${levelDates[levelDates.length - 1].slice(0, 10)}`
    : null

  // 涨跌色:最后一根 K 线收 vs 前一根收(无前日则按开收判断)
  // rows 可能为空（该股在该区间没有K线 / 停牌 / 退市）：这里必须留空态分支，
  // 否则 `last.close` 直接抛 TypeError，被 ExtensionBoundary 吞成
  // 「扩展 official.stock-analysis 暂时不可用」—— 一句让人以为扩展坏了的话。
  const last = rows[rows.length - 1]
  const prev = rows[rows.length - 2]
  const curClose = levelsQ.data?.close
  const isUp = last ? (prev ? last.close >= prev.close : last.close >= last.open) : true

  // 简称变更竖线 + 名称区间：只画有证据的事实，日期落在非交易日的由图表吸附
  const vlines: ChartVLine[] = []
  const nameRanges: ChartRange[] = []
  const nh = nameHistory.data
  if (nh?.renamed) {
    for (const c of nh.changes) {
      vlines.push({ date: c.before, label: `${c.to}?`, color: '#EAB308' })
    }
    // 同一简称的观测区间画成底纹（不写字：名字已经在下方"简称轨迹"里说过一次，
    // 图上再写一遍既重复又和 K 线抢位置）。越界端点由图表钳到首/末根。
    for (const b of nh.bands) {
      nameRanges.push({ start: b.first_seen, end: b.last_seen, color: 'rgba(148,163,184,0.06)' })
    }
  }

  return (
    <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
      <div className="px-4 py-3 border-b border-border/40">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <LineChart className="h-4 w-4 text-sky-400 shrink-0" />
            <span className="text-sm font-medium text-foreground">关键价位分析</span>
            {kline.data?.source === 'live' && (
              <span
                className="rounded bg-warning/10 px-1 py-0.5 text-[9px] text-warning"
                title={kline.data?.degraded_reason || '本地没有这只票的日K，已改用上游实时拉取'}
              >
                非本地
              </span>
            )}
            {upstreamSeg && (
              <span
                className="rounded bg-sky-400/10 px-1 py-0.5 text-[9px] text-sky-300"
                title={`${upstreamSeg.start} ~ ${upstreamSeg.end} 来自上游前复权（未入库）`}
              >
                含上游段
              </span>
            )}
          </div>
          <div className="flex items-baseline gap-2 shrink-0">
            <span className="text-[10px] text-muted">{stats.n} 个交易日</span>
            <span className="text-[10px] text-muted/60">·</span>
            <span className="text-[10px] text-muted">当前价</span>
            <span className={`text-[16px] leading-6 font-mono font-bold ${isUp ? 'text-bull' : 'text-bear'}`}>
              {curClose?.toFixed(2) ?? '—'}
            </span>
          </div>
        </div>

        <div className="mt-2.5">
          <RangeBar
            value={range}
            onChange={changeRange}
            requested={requested}
            tableEarliest={tableEarliest}
            rowsFirst={rowsFirst}
            rowsLast={rowsLast}
            tradeDays={stats.n}
            gap={gap}
            loading={kline.isFetching}
            onExtend={m => extend.mutate(m)}
            extending={extend.isPending}
            upstream={upstreamSeg
              ? {
                  start: upstreamSeg.start,
                  end: upstreamSeg.end,
                  n: upstreamSeg.n,
                  error: kline.data?.upstream?.error ?? null,
                }
              : (kline.data?.upstream?.error
                  ? { start: requested.start, end: tableEarliest ?? requested.end, n: 0, error: kline.data.upstream.error }
                  : null)}
          />
        </div>
      </div>

      <div className="p-3 space-y-2">
        <RangeStatsStrip
          stats={visibleStats}
          scope={viewSlice ? 'visible' : 'selected'}
        />

        {rows.length === 0 ? (
          // 空态要分清"这段区间没有"还是"这只票根本没有"：
          // 上游也补不出东西（attempted 是真的、且拿到 0 行）就不是区间选错了。
          unknownSymbol ? (
            <EmptyState
              icon={LineChart}
              title="没有这只票"
              hint="本地维表里查不到该代码 —— 多半是写错了，或已退市并从维表中移除。换一只试试。"
            />
          ) : (
            <EmptyState
              icon={LineChart}
              title="所选区间没有日 K"
              hint="换一段区间试试；把起点移到本地覆盖之前也可以 —— 那一段会从上游前复权临时补齐。"
            />
          )
        ) : (
          <>
            <BarReadout
              row={readRow}
              prevClose={readPrev?.close}
              hovered={!!hoverDate}
            />
            {/* 点一下图区（获得焦点）之后：←/→ 平移、+/- 缩放、0 复位。
                不做全局监听 —— 那会把页面滚动和输入框里的方向键一起吃掉。 */}
            <div
              tabIndex={0}
              data-testid="chart-keyboard"
              onKeyDown={e => {
                const h = chartRef.current
                if (!h) return
                if (e.key === 'ArrowLeft') { e.preventDefault(); h.pan(-0.25) }
                else if (e.key === 'ArrowRight') { e.preventDefault(); h.pan(0.25) }
                else if (e.key === '+' || e.key === '=') { e.preventDefault(); h.zoom(0.7) }
                else if (e.key === '-' || e.key === '_') { e.preventDefault(); h.zoom(1.4) }
                else if (e.key === '0') { e.preventDefault(); h.reset() }
              }}
              className="rounded-md outline-none focus-visible:ring-1 focus-visible:ring-sky-400/40"
              title="点一下图区后可用键盘：←/→ 平移、+/- 缩放、0 复位"
            >
              <AnalysisKChart
                ref={chartRef}
                rows={rows}
                levels={levels}
                series={levelsQ.data?.series}
                seriesDates={levelsQ.data?.dates}
                defaultLevelTypes={['sr', 'pivot', 'keltner_s']}
                initialRange={requested}
                ranges={nameRanges}
                vlines={vlines}
                onHoverDate={setHoverDate}
                onViewportChange={setViewport}
                height={480}
              />
            </div>
            <p className="text-[9px] leading-relaxed text-muted">
              点一下图区后可用键盘：←/→ 平移 · <kbd className="font-mono">+</kbd>/<kbd className="font-mono">-</kbd> 缩放 ·
              <kbd className="font-mono"> 0 </kbd> 复位。纵轴可切「对数」——两年看腰斩/翻倍时，
              线性轴会把低价段压成一条直线。
            </p>
          </>
        )}

        <NameTrace data={nh} />

        {levelSpan && (
          <p className="text-[9px] leading-relaxed text-muted">
            关键价位（压力支撑 / 枢轴 / 通道）按最近 {levelDates.length} 个交易日重算，
            样本 {levelSpan}，与上面选的区间无关 —— 它们是"今天的位置"，不是那一段的历史水位。
          </p>
        )}
      </div>
    </div>
  )
}

// ===== 左侧常驻:历史报告侧栏(所有股票,按时间倒序平铺) =====
function HistorySidebar() {
  const { reports, loaded } = useHistoryReports()

  return (
    <aside className="self-start sticky top-0">
      <div className="rounded-card border border-border/60 bg-surface/40 overflow-hidden">
        <div className="px-3 py-2.5 border-b border-border/40 flex items-center gap-2">
          <HistoryIcon className="h-3.5 w-3.5 text-sky-400 shrink-0" />
          <span className="text-xs font-medium text-foreground">历史报告</span>
          {loaded && reports.length > 0 && (
            <span className="ml-auto text-[10px] text-muted">{reports.length}</span>
          )}
        </div>

        {!loaded ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          </div>
        ) : reports.length === 0 ? (
          <div className="px-3 py-10 text-center">
            <p className="text-xs text-muted">还没有任何个股分析报告</p>
            <p className="text-[10px] text-muted/60 mt-1">选一只股票,点「AI 个股分析」生成</p>
          </div>
        ) : (
          <div className="max-h-[calc(100vh-220px)] overflow-y-auto p-2 space-y-1.5">
            {reports.map(r => (
              <div
                key={r.id}
                className="group rounded-lg border border-border/40 bg-elevated/20 p-2.5 hover:border-border hover:bg-elevated/40 transition-colors"
              >
                <div className="flex items-center justify-between gap-2">
                  <button
                    onClick={() => openHistoryReport(r.id)}
                    className="flex-1 text-left min-w-0"
                  >
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="text-xs font-medium text-foreground truncate">{r.name || r.symbol}</span>
                      <span className="text-[10px] font-mono text-muted shrink-0">{r.symbol}</span>
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-[10px] text-muted">
                      <span>{fmtRelative(r.created_at)}</span>
                      {r.close != null && <span className="font-mono">价 {r.close.toFixed(2)}</span>}
                      {r.focus && <span className="text-sky-300/70 truncate">关注: {r.focus}</span>}
                    </div>
                    {r.summary && (
                      <div className="mt-1 text-[11px] text-muted truncate">{r.summary}</div>
                    )}
                  </button>
                  <button
                    onClick={() => { deleteReport(r.id); toast('已删除', 'success') }}
                    className="shrink-0 text-[10px] text-muted/60 hover:text-danger transition-colors px-1 py-0.5 opacity-0 group-hover:opacity-100"
                    title="删除"
                  >
                    删除
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </aside>
  )
}

// ===== 二次确认弹窗 =====
function ConfirmModal({ report, onView, onRedo, onClose }: {
  report: { id: string; created_at: string; focus: string }
  onView: () => void
  onRedo: () => void
  onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="w-full max-w-sm bg-surface border border-border rounded-2xl p-5 shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 mb-2">
          <HistoryIcon className="h-4 w-4 text-sky-400" />
          <span className="text-sm font-medium text-foreground">该个股已有分析报告</span>
        </div>
        <p className="text-xs text-secondary leading-relaxed mb-1">
          最近一次报告生成于 <span className="text-foreground">{fmtRelative(report.created_at)}</span>。
        </p>
        {report.focus && <p className="text-xs text-muted mb-1">关注点: {report.focus}</p>}
        <p className="text-xs text-muted mb-4">可直接查看历史,或重新生成一份新报告。</p>
        <div className="flex gap-2">
          <button onClick={onView}
            className="flex-1 h-8 rounded-lg bg-elevated border border-border text-xs text-secondary hover:text-foreground transition-colors">
            查看历史
          </button>
          <button onClick={onRedo}
            className="flex-1 h-8 rounded-lg bg-gradient-to-r from-sky-500/20 to-blue-500/15 border border-sky-400/30 text-xs text-sky-300 hover:from-sky-500/30 transition-all">
            重新分析
          </button>
        </div>
      </div>
    </div>
  )
}

function fmtRelative(iso: string): string {
  try {
    const t = new Date(iso).getTime()
    const diff = Date.now() - t
    if (diff < 60_000) return '刚刚'
    if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`
    if (diff < 86400_000) return `${Math.floor(diff / 3600_000)} 小时前`
    if (diff < 7 * 86400_000) return `${Math.floor(diff / 86400_000)} 天前`
    return new Date(iso).toLocaleDateString('zh-CN')
  } catch { return iso }
}
