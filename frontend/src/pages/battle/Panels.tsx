/**
 * 博弈链路的面板：扫描 / 时间轴 / 规则台 / 账本 / 名册 / 信息链。
 *
 * 分工：口径（单位、判据、四态含义）全部来自 calibers.ts 与后端，这里只排版。
 * 四条原则写进组件而不是注释里：
 *   1. 先扫后钻——页面第一块是「今天谁上榜」，不是「请先选一只票」；
 *   2. 没有数据的对象不隐藏，但也不占主屏：标 no_data + 说清缺哪张表，收进一处；
 *   3. 每个数字带 n 与 t——没有样本量的结论不能读；
 *   4. 同一个数字只出现一次：分档表归扫描层，账本不再重复一遍。
 */
import {
  AlertTriangle, ChevronRight, ExternalLink, Eye, EyeOff, HelpCircle, Sigma,
} from 'lucide-react'
import type {
  BoardEvent, BoardRule, EvidenceState, InfoChainItem, LedgerGap, MissingTable,
  RuleState, ScanName, ScanSeat, SeatRow, TableGameLedger, TableGameObject, TableGameScan,
} from '@/lib/api'
import { cn } from '@/lib/cn'
import { bp, isSignificant, ratio, stabilityText, verdictBadge, verdictStyle, yi } from './calibers'

// ===== 通用小件 =====

const EV: Record<EvidenceState, { label: string; cls: string; hint: string }> = {
  observed: {
    label: '可观测', cls: 'bg-accent/10 text-accent border-accent/30',
    hint: '本地数据能直接看到它的动作',
  },
  proxied: {
    label: '只有代理', cls: 'bg-warning/10 text-warning border-warning/30',
    hint: '看得见形状，看不见具体是谁——代理变量有归因风险',
  },
  no_data: {
    label: '无数据', cls: 'bg-elevated text-muted border-border',
    hint: '本地没有这张表，此处不给数字',
  },
}

const RULE: Record<RuleState, { label: string; cls: string }> = {
  triggered: { label: '成立', cls: 'bg-warning/15 text-warning' },
  clear: { label: '不成立', cls: 'bg-elevated text-muted' },
  derived: { label: '算术', cls: 'bg-accent/10 text-accent' },
  no_data: { label: '无数据', cls: 'bg-elevated text-muted line-through decoration-border' },
}

function SectionHead({ title, thesis, right }: {
  title: string
  thesis: string
  right?: React.ReactNode
}) {
  return (
    <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-1">
      <h2 className="text-[13px] font-semibold text-foreground">{title}</h2>
      <p className="min-w-0 flex-1 text-[10px] leading-relaxed text-muted">{thesis}</p>
      {right}
    </div>
  )
}

function BpValue({ v, digits = 1 }: { v: number | null | undefined; digits?: number }) {
  return (
    <span className={cn(
      'font-mono tabular-nums',
      v == null ? 'text-muted' : v >= 0 ? 'text-bull' : 'text-bear',
    )}>
      {v == null ? '—' : bp(v, digits)}
    </span>
  )
}

/** 「+43.5bp t=1.6 n=453」——席位自己的历史，样本不足时明说而不是省略 */
function SeatHistory({ s }: { s: ScanSeat }) {
  if (s.n == null || s.bp == null) {
    return <span className="text-[9px] text-muted">样本不足</span>
  }
  return (
    <span className="flex items-baseline gap-1">
      <BpValue v={s.bp} />
      <span className={cn('font-mono text-[9px]', isSignificant(s.t) ? 'text-secondary' : 'text-muted')}>
        t={s.t ?? '—'}
      </span>
      <span className="font-mono text-[9px] text-muted">n={s.n}</span>
    </span>
  )
}

// ===== 扫描层：今天谁上榜 =====

function SeatCell({ row }: { row: ScanName }) {
  const buys = row.buy_seats
  const sells = row.sell_seats
  return (
    <span className="block min-w-0 space-y-0.5">
      {buys.map(s => (
        <span key={`b-${s.seat}`} className="flex min-w-0 items-baseline gap-1.5">
          <span className="shrink-0 rounded bg-bull/10 px-1 text-[9px] text-bull">买</span>
          <span className="min-w-0 max-w-[76px] truncate text-[10px] text-secondary" title={s.seat}>
            {s.seat}
          </span>
          <span className="shrink-0 font-mono text-[9px] text-muted">{s.net_yi.toFixed(2)}亿</span>
          <SeatHistory s={s} />
        </span>
      ))}
      {sells.slice(0, 1).map(s => (
        <span key={`s-${s.seat}`} className="flex min-w-0 items-baseline gap-1.5">
          <span className="shrink-0 rounded bg-bear/10 px-1 text-[9px] text-bear">卖</span>
          <span className="min-w-0 max-w-[76px] truncate text-[10px] text-muted" title={s.seat}>
            {s.seat}
          </span>
          <span className="shrink-0 font-mono text-[9px] text-muted">{s.net_yi.toFixed(2)}亿</span>
          <SeatHistory s={s} />
        </span>
      ))}
      {!buys.length && !sells.length && (
        <span className="block text-[9px] leading-tight text-muted">
          无具名游资席位
          {row.org_net_yi != null && row.org_net_yi > 0 && (
            <span className="text-secondary">
              ：机构专用净买 {row.org_net_yi.toFixed(2)} 亿（混合桶，拆不开是谁）
            </span>
          )}
        </span>
      )}
    </span>
  )
}

/**
 * 单票自己的历史：多数票一年只上榜几次，所以这一列经常是「样本不足」。
 *
 * 实测（2026-11-13 前 12 只）：0 只的 n ≥ 20，最大 n=19。也就是说这一列
 * 永远给出同一个答案——那是装饰，不是信息。列已经删掉，改成标题里的一句话：
 * 「前 12 只里 0 只自身样本 ≥20」——用缺口的分布说明为什么只能用全市场基准。
 */
function ownCoverage(names: ScanName[]): string {
  const covered = names.filter(n => n.own?.enough).length
  const maxN = names.reduce((m, n) => Math.max(m, n.own?.n ?? 0), 0)
  return `前 ${names.length} 只里 ${covered} 只自身样本 ≥20（最大 n=${maxN}）`
}

export function Scan({
  scan, active, onPick, onAnalyze, loading, error,
}: {
  scan: TableGameScan | undefined
  active: string
  onPick: (symbol: string) => void
  /** 跳到个股分析：这一页只回答"谁在对面"，价格形态在个股分析里看 */
  onAnalyze: (symbol: string, name?: string | null) => void
  loading: boolean
  error: boolean
}) {
  if (loading) {
    return <div className="rounded-card border border-border bg-surface px-3 py-6 text-center text-[11px] text-muted">
      正在读取今日榜单与席位历史…
    </div>
  }
  if (error || !scan) {
    return <div className="rounded-card border border-danger/30 bg-danger/5 px-3 py-3 text-[11px] text-danger">
      扫描层读取失败。下面各节仍可单独使用，但「今天谁上榜」这一块不可用。
    </div>
  }
  if (scan.state === 'no_data') {
    return (
      <div className="rounded-card border border-dashed border-border bg-surface px-3 py-4 text-center">
        <p className="text-[11px] text-muted">本地还没有任何龙虎榜数据，扫描层为空。</p>
        <p className="mt-1 text-[10px] text-muted">这不是「今天没有机会」，是没有榜单可读。</p>
      </div>
    )
  }

  const stale = scan.state === 'stale'
  return (
    <section>
      <SectionHead
        title="今日可扫描"
        thesis={`${scan.counts.total} 只上榜，${scan.counts.positive} 只净买入，按净额降序取前 ${scan.counts.shown}。`
          + '席位列是这个席位近一年在同类事件上的次日超额。'
          + `${ownCoverage(scan.names)}——所以判定只能用全市场条件基准，不能按单票历史。`}
        right={<span className="text-[9px] text-muted">点一行看这只票的事件时间轴</span>}
      />

      {stale && (
        <div className="mb-2 flex gap-1.5 rounded-btn bg-warning/10 px-2 py-1.5 text-[10px] leading-relaxed text-warning">
          <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
          <span>
            面板已到 {scan.as_of}，榜单却停在 {scan.trade_date}（滞后 {scan.lag_days} 个交易日）。
            T 日榜盘后才发布，滞后 1 天属正常；<b>滞后 ≥2 天不是发布节奏，是取数链路</b>。
          </span>
        </div>
      )}

      <div className="overflow-hidden rounded-card border border-border bg-surface">
        {/* 表头 */}
        <div className="hidden gap-2 border-b border-border px-3 py-1.5 text-[9px] text-muted sm:grid sm:grid-cols-[minmax(96px,1fr)_58px_62px_minmax(0,1.6fr)_44px]">
          <span>标的 / 榜单日 {scan.trade_date}</span>
          <span className="text-right">T 日</span>
          <span className="text-right">净额</span>
          <span>买方席位（该席位历史次日超额）</span>
          <span className="text-right">个股</span>
        </div>
        <div className="divide-y divide-border">
          {scan.names.map(n => (
            <div
              key={n.symbol}
              // 整行可点=选中（下方时间轴/规则台随之切换）；
              // 行尾的「分析」是另一件事：离开本页去看这只票的价格形态。
              // 两者不能挤在同一个 <button> 里（嵌套 button 在 HTML 里非法）。
              role="button"
              tabIndex={0}
              onClick={() => onPick(n.symbol)}
              onKeyDown={e => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(n.symbol) }
              }}
              className={cn(
                'grid w-full cursor-pointer grid-cols-[minmax(96px,1fr)_58px_62px_44px] items-start gap-2 px-3 py-1.5 text-left transition-colors hover:bg-elevated/50 sm:grid-cols-[minmax(96px,1fr)_58px_62px_minmax(0,1.6fr)_44px]',
                active === n.symbol && 'bg-accent/5',
              )}
            >
              <span className="min-w-0">
                <span className={cn(
                  'block truncate text-[11px]',
                  active === n.symbol ? 'font-medium text-accent' : 'text-foreground',
                )}>
                  {n.name ?? n.symbol}
                </span>
                <span className="block font-mono text-[9px] text-muted">{n.symbol}</span>
              </span>
              <span className={cn(
                'text-right font-mono text-[11px]',
                (n.chg_pct ?? 0) >= 0 ? 'text-bull' : 'text-bear',
              )}>
                {n.chg_pct == null ? '—' : `${n.chg_pct >= 0 ? '+' : ''}${n.chg_pct.toFixed(2)}%`}
              </span>
              <span className={cn(
                'text-right font-mono text-[11px]',
                (n.net_yi ?? 0) >= 0 ? 'text-bull' : 'text-bear',
              )}>
                {yi(n.net_yi)}
              </span>
              <span className="col-span-3 sm:col-span-1">
                <SeatCell row={n} />
              </span>
              <span className="col-span-1 flex justify-end">
                <button
                  onClick={e => { e.stopPropagation(); onAnalyze(n.symbol, n.name) }}
                  title="在个股分析中打开（看这只票的价格形态与关键价位）"
                  className="inline-flex items-center gap-0.5 rounded border border-border px-1.5 py-0.5 text-[9px] text-secondary transition-colors hover:border-accent/40 hover:bg-accent/10 hover:text-accent"
                >
                  分析
                  <ExternalLink className="h-2.5 w-2.5" />
                </button>
              </span>
            </div>
          ))}
          {!scan.names.length && (
            <div className="px-3 py-4 text-center text-[11px] text-muted">该交易日没有净买入上榜股。</div>
          )}
        </div>
      </div>
    </section>
  )
}

// ===== 明早的分档规则 =====

/**
 * 竞价缺口分档。这不是预测，是条件表：条件（明早 9:25 的缺口）明天才出现。
 *
 * 每一档都带前后半段切分——「方向为正」和「可当规则用」是两件事，
 * 后者要求两段都显著。低开档通过了，平开档没有，高开档是负的。
 */
export function GateTable({ buckets, nEvent, days }: {
  buckets: LedgerGap[]
  nEvent: number
  days?: number
}) {
  if (!buckets.length) {
    return <div className="text-[10px] text-muted">没有分档样本，明早的判定条件暂不可用。</div>
  }
  const ruleable = buckets.filter(b => b.verdict === 'take' && b.stable === true)
  const weak = buckets.filter(b => b.verdict === 'weak' || (b.verdict === 'take' && b.stable === false))
  return (
    <div>
      <div className="overflow-hidden rounded-card border border-border bg-surface">
        <div className="grid grid-cols-[minmax(0,1fr)_64px_56px_52px_46px] gap-2 border-b border-border px-3 py-1.5 text-[9px] text-muted">
          <span>明早 09:25 的竞价缺口</span>
          <span>判定</span>
          <span className="text-right">档内超额</span>
          <span className="text-right">t</span>
          <span className="text-right">占比</span>
        </div>
        <div className="divide-y divide-border">
          {buckets.map(b => {
            const st = verdictStyle(b.verdict)
            return (
              <div key={b.label} className="grid grid-cols-[minmax(0,1fr)_64px_56px_52px_46px] items-baseline gap-2 px-3 py-1.5">
                <span className="min-w-0">
                  <span className="flex items-baseline gap-1.5">
                    <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', st.dot)} />
                    <span className="truncate text-[11px] text-foreground">{b.label}</span>
                  </span>
                  <span className="block pl-3 text-[9px] leading-tight text-muted">
                    {stabilityText(b)} · n={b.n}
                    {b.halves?.length ? ` · 前半 ${bp(b.halves[0].bp)} / 后半 ${bp(b.halves[1].bp)}` : ''}
                  </span>
                </span>
                <span className={cn('text-[10px]', st.cls)} title={st.action}>
                  {verdictBadge(b.verdict, b.stable)}
                </span>
                <span className="text-right"><BpValue v={b.bp} /></span>
                <span className={cn(
                  'text-right font-mono text-[10px]',
                  isSignificant(b.t) ? 'text-secondary' : 'text-muted',
                )}>
                  {b.t ?? '—'}
                </span>
                <span className="text-right font-mono text-[9px] text-muted">
                  {b.share_pct == null ? '—' : `${b.share_pct}%`}
                </span>
              </div>
            )
          })}
        </div>
      </div>
      <p className="mt-1.5 text-[10px] leading-relaxed text-secondary">
        {ruleable.length > 0 && (
          <>
            可当规则用的只有 <b className="text-foreground">{ruleable.map(b => b.label).join('、')}</b>
            ——前后半段都同向显著。
          </>
        )}
        {weak.length > 0 && (
          <> {weak.map(b => b.label).join('、')} 方向为正但样本外不稳，只能当倾向。</>
        )}
        {nEvent > 0 && (
          <span className="text-muted">
            {' '}样本：{days ?? 0} 个交易日 {nEvent} 次 1 日榜事件；收益口径是「次日开盘买→收盘卖」。
          </span>
        )}
      </p>
    </div>
  )
}

// ===== 个股时间轴 =====

const KIND_DOT: Record<string, string> = {
  limit_up: 'bg-bull',
  limit_down: 'bg-bear',
  volume_spike: 'bg-warning',
  turnover_spike: 'bg-accent',
  gap: 'bg-secondary',
  lhb: 'bg-accent',
  high: 'bg-bull/60',
  low: 'bg-bear/60',
}

/**
 * 事件时间轴。左列是日期，右列是「谁 / 做了什么 / 你什么时候能看到」。
 *
 * 排序是倒序：最近的在最上面。因为这一页不是历史课，是「昨天发生了什么、
 * 明天会轮到我什么」。
 */
export function Timeline({ events, emptyHint }: { events: BoardEvent[]; emptyHint: string }) {
  if (!events.length) {
    return (
      <div className="rounded-card border border-dashed border-border bg-surface px-3 py-6 text-center">
        <p className="text-[11px] text-muted">{emptyHint}</p>
        <p className="mt-1 text-[10px] text-muted">
          安静本身是信息：没有异动、没有上榜、没有换手极值，说明这只票现在不在任何人的主战场里。
        </p>
      </div>
    )
  }
  let lastDate = ''
  return (
    <div className="relative">
      <span className="absolute left-[38px] top-2 bottom-2 w-px bg-border" aria-hidden />
      <div className="space-y-1">
        {events.map((e, i) => {
          const showDate = e.date !== lastDate
          lastDate = e.date
          return (
            <div key={`${e.date}-${e.kind}-${i}`} className="relative flex gap-2">
              <div className="w-[36px] shrink-0 pt-1 text-right">
                {showDate && (
                  <div className="font-mono text-[9px] leading-tight text-muted">
                    {e.date.slice(5)}
                  </div>
                )}
              </div>
              <span className={cn(
                'relative z-10 mt-[5px] h-1.5 w-1.5 shrink-0 rounded-full ring-2 ring-surface',
                KIND_DOT[e.kind] ?? 'bg-muted',
              )} />
              <div className="min-w-0 flex-1 pb-1">
                <div className="flex flex-wrap items-baseline gap-x-1.5">
                  <span className="text-[11px] text-foreground">{e.title}</span>
                  <span className="rounded bg-elevated px-1 text-[9px] text-secondary">{e.actor_label}</span>
                  {e.evidence === 'proxied' && (
                    <span className="text-[9px] text-warning" title="看到的是形状，不是具体的人">代理</span>
                  )}
                </div>
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span className="min-w-0 flex-1 text-[10px] leading-relaxed text-muted">{e.detail}</span>
                  <span className="shrink-0 font-mono text-[9px] text-accent/80">{e.seen_at}</span>
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ===== 规则台 =====

/** 九条行业常识在这一只票上的判定。claim 默认折叠：先给数，再给解释。 */
export function RuleLab({ rules }: { rules: BoardRule[] }) {
  const hit = rules.filter(r => r.state === 'triggered').length
  return (
    <section>
      <SectionHead
        title="常识检验台"
        thesis={`把行业里流传的九条说法，逐条落到这一只票上：能算的给数，算不了的写「无数据」。`
          + `今日成立 ${hit} 条——成立的越多，说明这只票同时具备越多「有人在做局」的形状；但形状不是证据。`}
      />
      <div className="divide-y divide-border rounded-card border border-border bg-surface">
        {rules.map(r => {
          const st = RULE[r.state]
          return (
            <div key={r.id} className="px-3 py-2">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className={cn('shrink-0 rounded px-1.5 py-0.5 text-[9px]', st.cls)}>{st.label}</span>
                <span className="text-[11px] font-medium text-foreground">{r.label}</span>
                {r.value != null && (
                  <span className="font-mono text-[11px] text-foreground">{r.value}</span>
                )}
                {r.unit && <span className="text-[9px] text-muted">{r.unit}</span>}
                {r.state === 'no_data' && (
                  <span className="flex items-center gap-0.5 text-[9px] text-muted">
                    <EyeOff className="h-2.5 w-2.5" />本仓库看不到
                  </span>
                )}
              </div>
              {r.detail && (
                <div className="mt-0.5 font-mono text-[9px] leading-relaxed text-muted">{r.detail}</div>
              )}
              <details className="group mt-1">
                <summary className="flex cursor-pointer list-none items-center gap-1 text-[9px] text-accent/80 hover:text-accent">
                  <HelpCircle className="h-2.5 w-2.5" />为什么这是一条常识
                </summary>
                <p className="mt-1 border-l-2 border-accent/40 pl-2 text-[10px] leading-relaxed text-secondary">
                  {r.claim}
                </p>
              </details>
            </div>
          )
        })}
      </div>
    </section>
  )
}

// ===== 账本 =====

function SeatLine({ s }: { s: SeatRow }) {
  return (
    <div className="flex items-baseline gap-2 text-[10px]">
      <span className="w-[72px] shrink-0 truncate text-secondary" title={s.seat}>{s.seat}</span>
      <span className="w-[64px] shrink-0 text-right"><BpValue v={s.bp} /></span>
      <span className={cn(
        'w-[52px] shrink-0 text-right font-mono text-[9px]',
        isSignificant(s.t) ? 'text-secondary' : 'text-muted',
      )}>
        t={s.t == null ? '—' : s.t.toFixed(1)}
      </span>
      <span className="w-[46px] shrink-0 text-right font-mono text-[9px] text-muted">n={s.n}</span>
      <span className="hidden w-[56px] shrink-0 text-right font-mono text-[9px] text-muted sm:block">
        {s.symbols} 只
      </span>
    </div>
  )
}

/**
 * 账本 —— 全页唯一给出「跟买到底赚不赚」的地方。
 *
 * 最重要的不是 +41.6bp，是「对照组自检 = -0.0bp」那一格：
 * 它成立时，上面那个数字才是「上榜这件事本身带来的」，而不是「高波动股天生命好」。
 *
 * 分档表不在这里重复：它属于扫描层的「明早规则」，同一份数据只摆一次。
 */
export function Ledger({ ledger }: { ledger: TableGameLedger }) {
  const controlOk = ledger.control_bp != null && Math.abs(ledger.control_bp) <= 5
  const top = ledger.seats.slice(0, 6)
  // 席位少于 7 个时 top 已经把它们全列了，再取 tail 会把同一行印两遍。
  const bottom = ledger.seats.length > 6 ? ledger.seats.slice(-3) : []
  // 「席位有没有技能」不能靠眼睛看排行榜，只能数 t。下面这句是数出来的，不是写死的。
  const sig = ledger.seats.filter(s => isSignificant(s.t)).length
  const lo = ledger.seats.length ? Math.min(...ledger.seats.map(s => s.bp ?? 0)) : 0
  const hi = ledger.seats.length ? Math.max(...ledger.seats.map(s => s.bp ?? 0)) : 0
  return (
    <section>
      <SectionHead
        title="跟买账本"
        thesis={`${ledger.window_days} 个交易日、${ledger.n_event} 次 1 日榜事件。收益口径一律是`
          + '「T+1 开盘买 → T+1 收盘卖」——这是散户真正能成交的那一半，也是唯一没有前视的口径。'}
        right={<span className="flex items-center gap-1 text-[9px] text-muted">
          <Sigma className="h-3 w-3" />超额 = 减去同日同「涨跌幅×换手」桶的非上榜股均值
        </span>}
      />

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <div className="rounded-card border border-border bg-surface px-3 py-2">
          <div className="text-[10px] text-muted">上榜股次日（全体）</div>
          <div className="mt-0.5 flex items-baseline gap-1.5">
            <span className="text-base"><BpValue v={ledger.excess_bp} /></span>
            <span className="font-mono text-[10px] text-muted">t={ledger.t ?? '—'}</span>
          </div>
          <div className="mt-0.5 font-mono text-[9px] text-muted">n={ledger.n_event}</div>
        </div>
        <div className="rounded-card border border-border bg-surface px-3 py-2">
          <div className="text-[10px] text-muted">剔除一字板（能买到）</div>
          <div className="mt-0.5 flex items-baseline gap-1.5">
            <span className="text-base"><BpValue v={ledger.tradeable_bp} /></span>
            <span className="font-mono text-[10px] text-muted">t={ledger.tradeable_t ?? '—'}</span>
          </div>
          <div className="mt-0.5 font-mono text-[9px] text-muted">n={ledger.tradeable_n ?? '—'}</div>
        </div>
        <div className="rounded-card border border-border bg-surface px-3 py-2">
          <div className="text-[10px] text-muted">改成持两日</div>
          <div className="mt-0.5 flex items-baseline gap-1.5">
            <span className="text-base"><BpValue v={ledger.hold2_bp} /></span>
            <span className="font-mono text-[10px] text-muted">t={ledger.hold2_t ?? '—'}</span>
          </div>
          <div className="mt-0.5 font-mono text-[9px] text-muted">n={ledger.hold2_n ?? '—'}</div>
        </div>
        <div className={cn(
          'rounded-card border px-3 py-2',
          controlOk ? 'border-accent/30 bg-accent/5' : 'border-danger/40 bg-danger/5',
        )}>
          <div className="text-[10px] text-muted">对照组自检（非上榜股）</div>
          <div className="mt-0.5 flex items-baseline gap-1.5">
            <span className="text-base"><BpValue v={ledger.control_bp} /></span>
            <span className={cn('text-[10px]', controlOk ? 'text-accent' : 'text-danger')}>
              {controlOk ? '通过' : '未通过'}
            </span>
          </div>
          <div className="mt-0.5 text-[9px] leading-tight text-muted">
            必须 ≈0。不为 0 说明分桶没起作用，上面三个数字全部作废。
          </div>
        </div>
      </div>

      <div className="mt-2 grid gap-2 lg:grid-cols-[1fr_1fr]">
        <div className="rounded-card border border-border bg-surface px-3 py-2">
          <div className="mb-1 text-[11px] font-medium text-foreground">机构 vs 游资（谁在买的第二层）</div>
          <div className="space-y-1">
            {ledger.splits.map(s => (
              <div key={s.label} className="flex items-baseline gap-2 text-[10px]">
                <span className="w-[58px] shrink-0 text-secondary">{s.label}</span>
                <span className="w-[62px] shrink-0 text-right"><BpValue v={s.bp} /></span>
                <span className={cn(
                  'w-[46px] shrink-0 text-right font-mono text-[9px]',
                  isSignificant(s.t) ? 'text-secondary' : 'text-muted',
                )}>
                  t={s.t == null ? '—' : s.t.toFixed(1)}
                </span>
                <span className="w-[46px] shrink-0 text-right font-mono text-[9px] text-muted">n={s.n}</span>
              </div>
            ))}
          </div>
          <p className="mt-1.5 text-[9px] leading-relaxed text-muted">
            最反直觉的一条：游资<u>净卖出</u>的票，次日比它净买入的还强。
            出货不等于见顶——它只说明有人在这个价位完成了交换。
          </p>
          <div className="mt-2 border-t border-border pt-1.5">
            <div className="text-[10px] text-muted">窗口对半切（样本外稳定性）</div>
            <div className="mt-1 space-y-0.5">
              {ledger.halves.map(h => (
                <div key={h.label} className="flex items-baseline gap-2 text-[10px]">
                  <span className="w-[46px] shrink-0 text-secondary">{h.label}</span>
                  <span className="w-[62px] shrink-0 text-right"><BpValue v={h.bp} /></span>
                  <span className="font-mono text-[9px] text-muted">
                    {h.from}~{h.to} · t={h.t ?? '—'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div className="rounded-card border border-border bg-surface px-3 py-2">
          <div className="mb-1 flex flex-wrap items-baseline gap-2">
            <span className="text-[11px] font-medium text-foreground">席位账本</span>
            <span className="text-[9px] text-muted">
              按昵称披露的游资席位，样本 ≥80 才入榜（n 小的均值是故事，不是统计）
            </span>
          </div>
          <div className="grid gap-x-6 gap-y-0.5 sm:grid-cols-2">
            <div>
              {top.map(s => <SeatLine key={s.seat} s={s} />)}
            </div>
            {bottom.length > 0 && (
              <div>
                {bottom.map(s => <SeatLine key={s.seat} s={s} />)}
              </div>
            )}
          </div>
          <p className="mt-1 border-t border-border pt-1 text-[9px] leading-relaxed text-muted">
            {ledger.seats.length} 个入榜席位首尾差 {Math.round(hi - lo)}bp，但只有 {sig} 个的 |t| ≥ 2。
            排行榜看着有高低，多数差异落在噪声里——更像「各自习惯做哪种形态的票」，不是稳定的技能。
          </p>
        </div>
      </div>

      <p className="mt-1.5 text-[9px] leading-relaxed text-muted">
        口径限制（会削弱上面的显著性，不改变方向）：同一交易日多只上榜股彼此相关，
        t 值未做按日聚类调整；扣费前、按开盘价成交、未计冲击成本；dump 为存活快照。
        重算：<code className="font-mono">python -m app.services.table_game --scan</code>。
      </p>
    </section>
  )
}

// ===== 名册（只留有数字的三个） =====

/** 有数字的对象：卡片。没有数字的走 Unobservable，不在这里占用主屏。 */
export function Roster({ objects }: { objects: TableGameObject[] }) {
  const withStats = objects.filter(o => o.stats.length > 0)
  if (!withStats.length) return null
  return (
    <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
      {objects.filter(o => o.stats.length === 0 && o.evidence === 'observed').map(o => (
        // 标了可观测却没有数字，本身就是要暴露的事，不该静默隐藏
        <div key={o.id} className="rounded-card border border-dashed border-warning/40 bg-surface p-2.5">
          <div className="flex items-baseline gap-1.5">
            <span className="truncate text-xs font-semibold text-foreground">{o.name}</span>
            <span className="shrink-0 rounded border border-warning/30 bg-warning/10 px-1 py-px text-[9px] text-warning">
              待补
            </span>
          </div>
          <p className="mt-1 text-[10px] leading-relaxed text-muted">
            标为可观测，但本地没有算出它的数字——这句话本身就是缺陷，不要当成结论。
          </p>
        </div>
      ))}
      {withStats.map(o => (
        <div key={o.id} className="rounded-card border border-border bg-surface p-2.5">
          <div className="flex items-baseline gap-1.5">
            <span className="truncate text-xs font-semibold text-foreground">{o.name}</span>
            <span className="shrink-0 text-[9px] text-muted">{o.group}</span>
            <span className={cn('ml-auto shrink-0 rounded border px-1 py-px text-[9px]', EV[o.evidence].cls)}>
              {EV[o.evidence].label}
            </span>
          </div>
          <p className="mt-1 line-clamp-2 text-[10px] leading-relaxed text-secondary">
            <span className="text-muted">被迫单 </span>{o.hard}
          </p>
          <div className="mt-1.5 space-y-0.5">
            {o.stats.map(s => (
              <div key={s.label} className="flex flex-wrap items-baseline gap-1.5 text-[10px]">
                <span className="text-muted">{s.label}</span>
                {s.bp != null && <BpValue v={s.bp} />}
                {s.pct != null && <span className="font-mono text-foreground">{ratio(s.pct)}</span>}
                {s.pct_raw != null && (
                  <span className={cn('font-mono', s.pct_raw >= 0 ? 'text-bull' : 'text-bear')}>
                    {s.pct_raw >= 0 ? '+' : ''}{s.pct_raw}%
                  </span>
                )}
                {s.n != null && <span className="font-mono text-[9px] text-muted">n={s.n}</span>}
                {s.t != null && (
                  <span className={cn('font-mono text-[9px]', isSignificant(s.t) ? 'text-secondary' : 'text-muted')}>
                    t={s.t}
                  </span>
                )}
              </div>
            ))}
          </div>
          {o.stats.some(s => s.note) && (
            <p className="mt-1 text-[9px] leading-tight text-muted">
              {o.stats.map(s => s.note).filter(Boolean).join('；')}
            </p>
          )}
        </div>
      ))}
    </div>
  )
}

// ===== 看不见的部分 =====

/**
 * 名册里没有数字的对象 + 本地缺的表，放在同一个 <details> 里。
 *
 * 为什么不删掉：删掉会让人以为「这些不重要」。为什么默认折叠：
 * 一屏 9 段「他为什么不可观测」的散文会立刻变成噪音，而它一年只读一次。
 */
export function Unobservable({ objects, tables }: {
  objects: TableGameObject[]
  tables: MissingTable[]
}) {
  const rest = objects.filter(o => o.stats.length === 0 && o.evidence !== 'observed')
  const observed = objects.filter(o => o.evidence === 'observed').length
  return (
    <details className="rounded-card border border-dashed border-border bg-surface">
      <summary className="flex cursor-pointer flex-wrap items-center gap-2 px-3 py-2 text-[11px]">
        <span className="font-medium text-foreground">看不见的部分：{rest.length} 个对象 + {tables.length} 张缺表</span>
        <span className="text-[10px] text-muted">
          {objects.length} 个参与者里只有 {observed} 个的动作在本地能直接看到
        </span>
        <ChevronRight className="ml-auto h-3 w-3 text-muted" />
      </summary>
      <div className="border-t border-border px-3 pb-3 pt-2">
        <div className="grid gap-1.5 sm:grid-cols-2">
          {rest.map(o => (
            <div key={o.id} className="rounded-btn border border-border px-2 py-1.5">
              <div className="flex items-baseline gap-1.5">
                <span className="text-[11px] text-secondary">{o.name}</span>
                <span className="text-[9px] text-muted">{o.group}</span>
                <span className={cn('ml-auto shrink-0 rounded border px-1 py-px text-[9px]', EV[o.evidence].cls)}>
                  {EV[o.evidence].label}
                </span>
              </div>
              <p className="mt-0.5 text-[10px] leading-relaxed text-muted">
                <span className="text-secondary">被迫单 </span>{o.hard}
              </p>
              <p className="mt-0.5 flex gap-1 text-[10px] leading-relaxed text-warning/90">
                <AlertTriangle className="mt-px h-2.5 w-2.5 shrink-0" />
                <span>{o.lie}</span>
              </p>
            </div>
          ))}
        </div>
        <div className="mt-2 border-t border-border pt-2">
          <div className="text-[10px] text-muted">补上任意一张，上面就会有一个对象从「无数据」变成「可观测」：</div>
          <div className="mt-1 grid gap-1 sm:grid-cols-2 xl:grid-cols-3">
            {tables.map(m => (
              <div key={m.name} className="flex items-start gap-1.5 rounded-btn border border-dashed border-border px-2 py-1">
                <Eye className="mt-px h-3 w-3 shrink-0 text-muted" />
                <div className="min-w-0">
                  <div className="text-[10px] text-secondary">{m.name}</div>
                  <div className="text-[9px] leading-relaxed text-muted">{m.unlocks}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </details>
  )
}

// ===== 信息链（压成一条） =====

/**
 * 信息到达顺序。时滞由制度决定（可查），不是估计。
 *
 * 压成一行是因为它恒定不变：八行散文每天一模一样，读者的第二次访问
 * 不会再读第二遍，却要为它付出半屏滚动。
 */
export function InfoChain({ items }: { items: InfoChainItem[] }) {
  if (!items.length) return null
  return (
    <div className="rounded-card border border-border bg-surface px-3 py-2">
      <div className="mb-1 text-[10px] text-muted">
        同一件事到你这里要走这么远——越往后，故事越完整，价格越接近尾声
      </div>
      <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1">
        {items.map((it, i) => (
          <span key={`${it.at}-${i}`} className="flex items-baseline gap-1">
            {i > 0 && <span className="text-[9px] text-muted/50">→</span>}
            <span className={cn(
              'rounded px-1.5 py-0.5 text-[9px]',
              i <= 2 ? 'bg-accent/10 text-accent' : i <= 4 ? 'bg-warning/10 text-warning' : 'bg-elevated text-muted',
            )} title={`${it.who} · ${it.note}`}>
              {it.at} {it.layer}
              <span className="ml-1 font-mono opacity-70">{it.lag}</span>
            </span>
          </span>
        ))}
      </div>
    </div>
  )
}
