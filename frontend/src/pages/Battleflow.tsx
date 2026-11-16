/**
 * 博弈链路 —— 谁在和我做对手，他的约束有多硬，我什么时候才能看到他的动作。
 *
 * 三层结构，按读者的动作顺序排，而不是按数据的时间顺序：
 *   ① 今日可扫描 —— 今天谁上榜、谁在买、买方席位历史上赚不赚（先扫）
 *   ② 明早的判定 —— 竞价缺口分档：低开接、高开弃、一字板买不进（再定条件）
 *   ③ 个股实例 / 账本 / 名册 —— 选中一只票之后才需要读的东西（后钻）
 *
 * 为什么这么排：旧版第一屏是 12 个参与者的散文，读者要滚过 2700 字才知道
 * 今天该看哪只票；而那一屏每天一模一样，信息增量为零。扫描层不是"多一块
 * 内容"，它是这一页缺失的入口——没有它，页面只能等别人告诉你看哪只票。
 *
 * 口径纪律：本页不新增任何计算。数字来自 /api/table-game/scan 与
 * /api/table-game/{overview,board}；分档结论（take/weak/avoid/unbuyable）
 * 由后端一次判完，前端只上色。重算入口 `python -m app.services.table_game --scan`。
 *
 * 单位（本页同时出现四种，必须显式转换，禁止用「小于 1 就乘 100」猜）：
 *   scan.chg_pct         百分数  10.08  → +10.08%（pctRaw）
 *   scan.*_yi            亿元     已除以 1e8（yi）
 *   账本 bp              基点     41.6 → +41.6bp（后端已 ×1e4）
 *   median_gap_pct       百分数  -0.9  → -0.9%
 */
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Clock, ExternalLink, Loader2, RefreshCw, Search, X,
} from 'lucide-react'
import {
  api,
  type TableGameScan,
} from '@/lib/api'
import { PageHeader } from '@/components/PageHeader'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import {
  bp, phaseHint, ratio, verdictBadge,
} from './battle/calibers'
import { GateTable, InfoChain, Ledger, Roster, Scan, Timeline, RuleLab, Unobservable } from './battle/Panels'

// 口径纯函数与类型从 calibers 再导出：单测一直从这里 import，
// 页面结构可以改，判据的入口不能改。
export { bp, pct, pctRaw, ratio, yi, isSignificant, phaseHint, stabilityText, verdictBadge, verdictStyle } from './battle/calibers'
export type { VerdictStyle } from './battle/calibers'

const PHASE_ORDER = ['preopen', 'morning', 'afternoon', 'close_final'] as const

/**
 * 榜单新鲜度：扫描层读的是本地落盘的龙虎榜，而落盘发生在「有人请求当天榜单」时。
 * 旧页面的六段里有一次 dragonTiger 调用顺带完成了这件事；删掉六段后如果不补，
 * 页面会安静地停在前一天——这是最典型的「删掉一个看不见的副作用」类回归。
 *
 * 所以这里显式做一次：滞后 ≥1 个交易日就拉一次，成功后再让扫描层重读。
 */
function useBoardFreshness(scan: TableGameScan | undefined) {
  const qc = useQueryClient()
  const need = !!scan && scan.lag_days != null && scan.lag_days >= 1
  const pull = useQuery({
    queryKey: ['battle', 'lhb-pull', scan?.as_of],
    queryFn: () => api.dragonTiger(scan?.as_of),
    enabled: need,
    staleTime: 10 * 60_000,
    retry: false,
    refetchOnWindowFocus: false,
  })
  useEffect(() => {
    if (pull.data) void qc.invalidateQueries({ queryKey: ['battle', 'scan'] })
  }, [pull.data, qc])
  return { pulling: pull.isFetching, tried: pull.isFetched }
}

export function Battleflow() {
  const overview = useQuery({
    queryKey: QK.overviewMarket(),
    queryFn: () => api.overviewMarket(),
    refetchOnWindowFocus: false,
    refetchInterval: 60_000,
  })
  const scan = useQuery({
    queryKey: ['battle', 'scan'],
    queryFn: () => api.tableGameScan(),
    refetchOnWindowFocus: false,
    staleTime: 5 * 60_000,
  })
  // 名册与账本：读一年日线 + 一年龙虎榜，自带 staleTime，避免每次切页都重算 1.4M 行。
  const game = useQuery({
    queryKey: ['battle', 'table-game'],
    queryFn: () => api.tableGameOverview(),
    refetchOnWindowFocus: false,
    staleTime: 30 * 60_000,
  })

  const fresh = useBoardFreshness(scan.data)

  const [symbol, setSymbol] = useState<string | null>(null)
  const [typing, setTyping] = useState('')
  const activeSymbol = symbol ?? scan.data?.names?.[0]?.symbol ?? '000001.SZ'
  const navigate = useNavigate()

  /**
   * 去个股分析看这只票的价格形态。
   *
   * 本页给的是「谁在对面、他的约束有多硬」，但读者下一个动作几乎总是
   * 「那它长什么样」——此前只能手抄代码再粘到个股分析页的搜索框里。
   * 名字一并带过去，是为了让落地页在接口回来之前就能显示正确的标题。
   */
  const openAnalysis = (sym: string, nm?: string | null) => {
    const q = new URLSearchParams({ symbol: sym })
    if (nm) q.set('name', nm)
    navigate(`/stock-analysis?${q.toString()}`)
  }

  const board = useQuery({
    queryKey: ['battle', 'board', activeSymbol],
    queryFn: () => api.tableGameBoard(activeSymbol),
    refetchOnWindowFocus: false,
    staleTime: 30 * 60_000,
  })

  const phase = phaseHint(overview.data?.quote_status?.market_phase)
  const g = game.data
  const b = board.data
  const meta = b?.meta

  // 首屏结论：三条判断，不是三个数字仪表盘。
  const top = useMemo(() => {
    const s = scan.data
    if (!s) return null
    const ruleable = s.gate.buckets.filter(x => x.verdict === 'take' && x.stable === true)
    const avoid = s.gate.buckets.filter(x => x.verdict === 'avoid')
    const unbuyable = s.gate.buckets.some(x => x.verdict === 'unbuyable')
    const best = ruleable[0] ?? s.gate.buckets.find(x => x.verdict === 'take') ?? null
    return { scan: s, ruleable, avoid, unbuyable, best }
  }, [scan.data])

  const loading = scan.isLoading || game.isLoading

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center bg-base">
        <div className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在装配今日对手盘…
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-full bg-base pb-6">
      <PageHeader
        title="博弈链路"
        subtitle="同一根 K 线，六种钱读出六种意思。这一页不预测明天，只回答三件事：今天谁在对面、他的约束有多硬、你什么时候才看得到他的动作。"
        right={
          <div className="flex items-center gap-2">
            <span className="hidden font-mono text-[11px] text-muted sm:inline">
              {scan.data?.trade_date ?? overview.data?.as_of ?? '—'}
            </span>
            <button
              onClick={() => {
                void overview.refetch(); void scan.refetch()
                void game.refetch(); void board.refetch()
              }}
              className="flex items-center gap-1 rounded-btn border border-border bg-surface px-2.5 py-1.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', (scan.isFetching || fresh.pulling) && 'animate-spin')} /> 刷新
            </button>
          </div>
        }
      />

      {/* 交易时段条 —— 回答「现在能做什么」，不是「现在几点」 */}
      <div className="border-b border-border px-5 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <Clock className="h-3.5 w-3.5 text-muted" />
          {PHASE_ORDER.map((p, i) => {
            const h = phaseHint(p)
            const active = phase.index === i
            const done = phase.index > i && phase.index >= 0
            return (
              <div key={p} className="flex items-center gap-2">
                {i > 0 && <span className="text-[10px] text-muted/50">→</span>}
                <span className={cn(
                  'rounded-btn px-1.5 py-0.5 text-[10px]',
                  active ? 'bg-accent/15 font-medium text-accent'
                    : done ? 'text-secondary' : 'text-muted',
                )}>
                  {h.label}
                </span>
              </div>
            )
          })}
          <span className="ml-auto text-[10px] text-secondary">{phase.todo}</span>
        </div>
      </div>

      {/* 三条判断：一屏读完，不用滚 */}
      <div className="grid grid-cols-1 gap-2 px-5 py-3 sm:grid-cols-3">
        <div className="rounded-card border border-border bg-surface px-3 py-2">
          <div className="text-[10px] text-muted">今天谁在对面（{top?.scan.trade_date ?? '—'}）</div>
          <div className="mt-0.5 truncate text-[13px] text-foreground">
            {top ? (
              <>
                <span className="font-mono">{top.scan.counts.total}</span> 只上榜 ·
                <span className="ml-1 font-mono">{top.scan.counts.positive}</span> 只净买入
              </>
            ) : '—'}
          </div>
          <div className="mt-0.5 truncate text-[10px] text-secondary">
            {top?.scan.names.slice(0, 3).map(n => n.name ?? n.symbol).join(' · ') || '无榜单'}
          </div>
        </div>
        <div className={cn(
          'rounded-card border px-3 py-2',
          top?.best ? 'border-accent/30 bg-accent/5' : 'border-border bg-surface',
        )}>
          <div className="text-[10px] text-muted">明早什么条件才接</div>
          <div className="mt-0.5 text-[13px] text-foreground">
            {top?.best ? (
              <>
                <span className="font-medium">{verdictBadge(top.best.verdict, top.best.stable)}</span>
                <span className="ml-1 font-mono text-[11px] text-secondary">{bp(top.best.bp)}</span>
              </>
            ) : '—'}
          </div>
          <div className="mt-0.5 truncate text-[10px] text-secondary">
            {top?.ruleable.length
              ? `${top.ruleable.length} 档通过样本外检验；${top.avoid.length} 档为负`
                + `${top.unbuyable ? '；一字板买不进' : ''}`
              : '没有通过样本外检验的档位——只能当倾向'}
          </div>
        </div>
        <div className="rounded-card border border-border bg-surface px-3 py-2">
          <div className="text-[10px] text-muted">跟买到底赚不赚（近 {g?.ledger.window_days ?? '—'} 日）</div>
          <div className="mt-0.5 text-[13px] text-foreground">
            <span className="font-mono">{bp(g?.ledger.excess_bp)}</span>
            <span className="ml-1 font-mono text-[11px] text-secondary">t={g?.ledger.t ?? '—'}</span>
          </div>
          <div className="mt-0.5 truncate text-[10px] text-secondary">
            持两日只剩 {bp(g?.ledger.hold2_bp)}——「拿住会更好」是错觉
          </div>
        </div>
      </div>

      {/* ===== ① 扫描 + ② 明早的判定 ===== */}
      <div className="px-5">
        <Scan
          scan={scan.data}
          active={activeSymbol}
          onPick={setSymbol}
          onAnalyze={openAnalysis}
          loading={scan.isLoading}
          error={scan.isError}
        />
        <div className="mt-3">
          <div className="mb-2 flex flex-wrap items-baseline gap-2">
            <h2 className="text-[13px] font-semibold text-foreground">明早 09:25 的判定</h2>
            <span className="text-[10px] text-muted">
              缺口是明天才出现的条件，这里是「同条件的历史结果」，不是预测
            </span>
          </div>
          <GateTable
            buckets={scan.data?.gate.buckets ?? []}
            nEvent={scan.data?.gate.n_event ?? 0}
            days={scan.data?.gate.window_days}
          />
        </div>
      </div>

      {/* ===== ③ 个股实例：时间轴 + 规则台 ===== */}
      <div className="mt-5 px-5">
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <h2 className="text-[13px] font-semibold text-foreground">个股实例</h2>
          <span className="text-[10px] text-muted">
            换一只票，同一套对象会给出完全不同的答案——这才是「博弈」这个词有用的地方
          </span>
          <div className="ml-auto flex items-center gap-1.5">
            <div className="flex items-center gap-1 rounded-btn border border-border bg-surface px-2 py-1">
              <Search className="h-3 w-3 text-muted" />
              <input
                value={typing}
                onChange={e => setTyping(e.target.value.toUpperCase())}
                onKeyDown={e => {
                  if (e.key === 'Enter' && /^\d{6}(\.(SH|SZ|BJ))?$/.test(typing.trim())) {
                    const raw = typing.trim()
                    const code = raw.includes('.') ? raw
                      : /^(6|9)/.test(raw) ? `${raw}.SH`
                        : /^(0|2|3)/.test(raw) ? `${raw}.SZ` : `${raw}.BJ`
                    setSymbol(code)
                    setTyping('')
                  }
                }}
                placeholder="代码，回车切换"
                className="w-[104px] bg-transparent font-mono text-[10px] text-foreground outline-none placeholder:text-muted"
              />
              {typing && (
                <button onClick={() => setTyping('')} className="text-muted hover:text-foreground">
                  <X className="h-3 w-3" />
                </button>
              )}
            </div>
            <button
              onClick={() => openAnalysis(activeSymbol, meta?.name)}
              title="在个股分析中打开这只票（任意区间日K + 关键价位）"
              className="inline-flex items-center gap-1 rounded-btn border border-accent/40 bg-accent/10 px-2 py-1 text-[10px] text-accent transition-colors hover:bg-accent/20"
            >
              个股分析
              <ExternalLink className="h-3 w-3" />
            </button>
            {scan.data?.names.slice(0, 4).map(it => (
              <button
                key={it.symbol}
                onClick={() => setSymbol(it.symbol)}
                className={cn(
                  'rounded-btn border px-1.5 py-1 text-[10px] transition-colors',
                  activeSymbol === it.symbol
                    ? 'border-accent/50 bg-accent/10 text-accent'
                    : 'border-border bg-surface text-secondary hover:bg-elevated',
                )}
              >
                {it.name ?? it.symbol}
              </button>
            ))}
          </div>
        </div>

        <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-card border border-border bg-surface px-3 py-2">
          <span className="text-sm font-semibold text-foreground">
            {meta?.name ?? activeSymbol}
          </span>
          <span className="font-mono text-[10px] text-muted">{activeSymbol}</span>
          {meta?.close != null && (
            <span className="font-mono text-[12px] text-foreground">{meta.close.toFixed(2)}</span>
          )}
          {meta?.float_cap != null && (
            <span className="font-mono text-[10px] text-secondary">
              流通 {(meta.float_cap / 1e8).toFixed(1)} 亿
            </span>
          )}
          {meta?.cap_rank != null && (
            <span className="font-mono text-[10px] text-muted">市值第 {meta.cap_rank} 百分位</span>
          )}
          {meta?.turnover_rate != null && (
            <span className="font-mono text-[10px] text-muted">换手 {meta.turnover_rate.toFixed(1)}%</span>
          )}
          {meta?.roe != null && (
            <span className="font-mono text-[10px] text-muted">ROE {meta.roe.toFixed(1)}%</span>
          )}
          {board.isLoading && <Loader2 className="h-3 w-3 animate-spin text-muted" />}
          {b?.state === 'not_in_universe' && (
            <span className="text-[10px] text-warning">本地日线里没有这只票（已退市或代码有误）</span>
          )}
        </div>

        <div className="grid gap-2 lg:grid-cols-[1fr_1.05fr]">
          <div className="rounded-card border border-border bg-surface p-3">
            <div className="mb-2 flex items-baseline gap-2">
              <span className="text-[11px] font-medium text-foreground">事件时间轴</span>
              <span className="text-[9px] text-muted">右端是「你什么时候能看到」</span>
            </div>
            <Timeline
              events={b?.events ?? []}
              emptyHint={board.isLoading ? '正在读取该标的近 60 日事件…' : '近 60 日没有可观测的博弈事件'}
            />
          </div>
          <RuleLab rules={b?.rules ?? []} />
        </div>
      </div>

      {/* ===== ④ 账本 + 名册 ===== */}
      <div className="mt-5 px-5">
        {g ? <Ledger ledger={g.ledger} /> : (
          <div className={cn(
            'rounded-card border px-3 py-2 text-[11px]',
            game.isError ? 'border-danger/30 bg-danger/5 text-danger' : 'border-border bg-surface text-muted',
          )}>
            {game.isError
              ? '账本读取失败（名册与不可观测清单同样不可用）。上面的扫描层与个股实例仍可读。'
              : '账本读取中（首次装配要读一年日线）…'}
          </div>
        )}
      </div>

      <div className="mt-4 px-5">
        <div className="mb-2 flex flex-wrap items-baseline gap-2">
          <h2 className="text-[13px] font-semibold text-foreground">对手盘名册</h2>
          <span className="text-[10px] text-muted">
            只保留本地能给出数字的对象；没有数字的不删，收在下面「看不见的部分」里
          </span>
        </div>
        {g ? <Roster objects={g.objects} /> : null}
      </div>

      {/* ===== ⑤ 看不见的部分 + ⑥ 信息链 ===== */}
      <div className="mt-4 px-5">
        {g && <Unobservable objects={g.objects} tables={g.missing_tables} />}
      </div>

      {g?.retail && (
        <p className="px-5 pt-3 text-[10px] leading-relaxed text-secondary">
          散户体感代理：全市场 {g.retail.n} 只票里，<b className="text-foreground">{ratio(g.retail.under_pct)}</b>
          的现价低于自己近 20 日均价，中位差 {g.retail.median_gap_pct}%。
          这就是「指数还行、我却一直在亏」的算术来源——不是错觉。
        </p>
      )}

      <div className="mt-3 px-5">
        {g && <InfoChain items={g.info_chain} />}
      </div>

      <p className="px-5 pt-4 text-[10px] leading-relaxed text-muted">
        本页的「可观测 / 只有代理 / 无数据」是三种不同的东西，不能混读：
        标「无数据」的对象不是不重要，是本地缺那张表（十大股东、解禁、大宗交易、质押、ETF 份额）。
        扫描层、名册与账本的重算入口是 <code className="font-mono">python -m app.services.table_game --scan</code>，
        与页面同源；账本里的每个数字都能用那条命令复现。
      </p>
    </div>
  )
}
