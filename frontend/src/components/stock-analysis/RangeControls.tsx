import { CalendarRange, Loader2 } from 'lucide-react'
import { DateInput } from '@/components/DateInput'
import { cn } from '@/lib/cn'
import {
  RANGE_PRESETS, type CoverageGap, type DateRange, type RangePreset, type RangeStats,
  type StoredRange, daysBetween, isValidIso,
} from '@/lib/chartRange'
import type { StockNameHistory } from '@/lib/api'

/**
 * 个股分析的时间轴工具条 —— 预设 / 自定义起止 / 覆盖诚实提示。
 *
 * 三件事都写在这里而不是散在页面里：
 *   1. 区间怎么改（改的是"看哪一段"，不是"拉多少根"）；
 *   2. 这一段本地到底有没有数据（没有就说清楚缺的是覆盖还是这只票本身）；
 *   3. 缺口能不能补（能补的只有"整市场向前扩展历史"，不能给单只票开洞）。
 */

const fmtDay = (iso: string | null | undefined) => (iso ? iso : '—')

export function RangeBar({
  value, onChange, requested, tableEarliest, rowsFirst, rowsLast, tradeDays,
  gap, loading, onExtend, extending, upstream,
}: {
  value: StoredRange
  onChange: (next: StoredRange) => void
  requested: DateRange
  tableEarliest: string | null | undefined
  rowsFirst: string | null
  rowsLast: string | null
  tradeDays: number
  gap: CoverageGap
  loading?: boolean
  onExtend: (months: number) => void
  extending?: boolean
  /** 本地没有的那一段是否已经用上游前复权补上（只读，未入库） */
  upstream?: { start: string; end: string; n: number; error?: string | null } | null
}) {
  const pickPreset = (preset: RangePreset) => {
    if (preset === 'CUSTOM') return
    onChange({ ...value, preset })
  }

  // 改成自定义时，另一端要用**当前生效**的那一端（requested），不是 state 里
  // 可能已经过期的 value.end —— 预设是"从今天往前推"，今天变了 value 不会跟着变。
  const setStart = (start: string) => {
    if (!isValidIso(start)) return
    onChange({ preset: 'CUSTOM', start, end: requested.end })
  }
  const setEnd = (end: string) => {
    if (!isValidIso(end)) return
    onChange({ preset: 'CUSTOM', start: requested.start, end })
  }

  const spanDays = daysBetween(requested.start, requested.end)
  // 扩展按月下发（后端 compute_offset 里 1 月 = 30 天），所以按 30 取上整就够；
  // 不要再加缓冲月 —— 多补一个月等于多拉一整月的全市场日K。
  const monthsToExtend = gap.kind === 'table' ? Math.max(1, Math.ceil(gap.days / 30)) : 0
  // 只有真的补上（n>0）才算"这一段来自上游"；失败时 n=0，必须走下面那条警告，
  // 否则会出现"0 根来自上游前复权"这种自相矛盾的句子。
  const hasFill = !!upstream && upstream.n > 0

  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <CalendarRange className="h-3.5 w-3.5 shrink-0 text-muted" />
        <div className="flex items-center rounded-btn border border-border/60 bg-base/40 p-0.5">
          {RANGE_PRESETS.map(p => (
            <button
              key={p.key}
              onClick={() => pickPreset(p.key)}
              title={p.months
                ? `从北京今天往前推 ${p.months} 个自然月（右端=今天，不含未收盘的今日K）`
                : '本地已入库的全部区间（不向上游另取）；要看更早的请用 2 年或自定义起止'}
              className={cn(
                'h-6 rounded px-2 text-[10px] transition-colors',
                value.preset === p.key
                  ? 'bg-sky-500/20 font-medium text-sky-300'
                  : 'text-secondary hover:bg-elevated hover:text-foreground',
              )}
            >
              {p.label}
            </button>
          ))}
        </div>

        <div className="flex items-center gap-1 font-mono text-[10px] text-secondary">
          <DateInput
            value={requested.start}
            max={requested.end}
            onChange={setStart}
            ariaLabel="区间起点"
            allowEmpty={false}
            className="h-6 w-[104px] rounded border border-border/60 bg-base/40 px-1 text-[10px] text-foreground"
            focusClassName="focus-within:border-sky-400/50"
          />
          <span className="text-muted">→</span>
          <DateInput
            value={requested.end}
            min={requested.start}
            onChange={setEnd}
            ariaLabel="区间终点"
            allowEmpty={false}
            className="h-6 w-[104px] rounded border border-border/60 bg-base/40 px-1 text-[10px] text-foreground"
            focusClassName="focus-within:border-sky-400/50"
          />
        </div>

        <span className="font-mono text-[10px] text-muted">
          {loading
            ? <Loader2 className="inline h-3 w-3 animate-spin" />
            : <>{tradeDays} 个交易日{spanDays != null ? ` · ${spanDays} 自然日` : ''}</>}
        </span>

        {value.preset === 'ALL' && (
          <span className="text-[9px] text-muted">
            「全区间」= 本地已入库的全部（不向上游另取）；要看更早的用 2 年或自定义起止
          </span>
        )}
        {value.preset !== 'CUSTOM' && value.preset !== 'ALL' && (
          <span className="text-[9px] text-muted">预设从北京今天往前推，右端不含未收盘的今日 K</span>
        )}
      </div>

      {gap.kind === 'table' && hasFill && (
        <div className="flex flex-wrap items-center gap-2 rounded-btn border border-sky-400/30 bg-sky-400/[0.07] px-2 py-1 text-[10px] leading-relaxed text-sky-200/90">
          <span>
            本地日K起于 <b className="font-mono">{fmtDay(tableEarliest)}</b>；
            更早的 <b className="font-mono">{upstream.start} ~ {upstream.end}</b>
            （{upstream.n} 根）本次从<b>上游前复权</b>临时取来，未入库。
          </span>
          <button
            onClick={() => onExtend(monthsToExtend)}
            disabled={extending}
            className="inline-flex items-center gap-1 rounded border border-sky-400/40 px-1.5 py-0.5 font-medium hover:bg-sky-400/15 disabled:opacity-40"
            title="把这段真正写进本地库（整市场一起补）。需要除权因子权限（Pro）：没权限时后端会拒绝，因为未复权历史会让送转在图上变成假跳空；只读补齐用上游前复权，没有这个问题。"
          >
            {extending ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
            改为入库（{monthsToExtend} 个月）
          </button>
        </div>
      )}

      {gap.kind === 'table' && !hasFill && (
        <div className="flex flex-wrap items-center gap-2 rounded-btn border border-warning/30 bg-warning/[0.07] px-2 py-1 text-[10px] leading-relaxed text-warning">
          <span>
            本地日K起于 <b className="font-mono">{fmtDay(tableEarliest)}</b>，比你要的
            <b className="font-mono"> {requested.start} </b>晚了 {gap.days} 天 ——
            这一段不是"没这只票"，是本地没铺到。
            {loading ? '正在向上游取这一段…' : ''}
          </span>
          <button
            onClick={() => onExtend(monthsToExtend)}
            disabled={extending}
            className="inline-flex items-center gap-1 rounded border border-warning/40 px-1.5 py-0.5 font-medium hover:bg-warning/15 disabled:opacity-40"
            title="整市场向前扩展（全市场一起补，不能只给一只票开洞：单票回填会让历史分区只剩 1 个标的，横截面统计静默退化）"
          >
            {extending ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
            向前补 {monthsToExtend} 个月
          </button>
        </div>
      )}

      {gap.kind === 'table' && upstream?.error && (
        <div className="rounded-btn border border-danger/30 bg-danger/5 px-2 py-1 text-[10px] leading-relaxed text-danger">
          上游补齐失败（{upstream.error}）—— 只显示本地段。这通常是上游超时：一次要两年会被拒，
          缩小到 6 个月再试，或直接「改为入库」。
        </div>
      )}

      {gap.kind === 'symbol' && (
        <div className="rounded-btn border border-border/60 bg-base/40 px-2 py-1 text-[10px] leading-relaxed text-muted">
          这只票在 <b className="font-mono text-secondary">{requested.start}</b> ~
          <b className="font-mono text-secondary"> {fmtDay(rowsFirst)} </b>
          之间没有K线（停牌 / 上市晚 / 已退市）。这不是数据缺口，补历史也补不出来。
        </div>
      )}

      {rowsFirst && rowsLast && (
        <div className="text-[9px] text-muted">
          实际返回 <b className="font-mono text-secondary">{rowsFirst}</b> ~
          <b className="font-mono text-secondary"> {rowsLast}</b>
        </div>
      )}
    </div>
  )
}

/**
 * 区间统计 —— 三个数同源（收盘），不允许「涨了 5% 但离高点 -20%」这种自相矛盾的组合。
 *
 * scope 是必须显式传的：图上一拖缩放，看见的就不再是"所选区间"。
 * 两个数用同一个标题会让人拿 A 的解释去读 B 的数值。
 */
export function RangeStatsStrip({ stats, scope = 'selected' }: { stats: RangeStats; scope?: 'selected' | 'visible' }) {
  if (stats.n === 0) return null
  const pctCls = (stats.pct ?? 0) >= 0 ? 'text-bull' : 'text-bear'
  const ddCls = (stats.fromHigh ?? 0) >= 0 ? 'text-bull' : 'text-bear'
  const fmtPct = (v: number | null) => (v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`)
  const yi = stats.amount != null ? stats.amount / 1e8 : null
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border/40 bg-base/20 px-3 py-1.5 text-[10px]">
      {/* 明确"这是所选区间"：拖动缩放条之后图上的可见窗口会变窄，
          而这几个数是按整个所选区间算的 —— 不写清楚就会有人拿它解释缩放后的形态 */}
      <span className="text-muted/70" title={scope === 'visible'
        ? '拖动缩放条后，这里统计的是当前看得见的那一段（不是上面选的区间）'
        : '上面选的区间（拖动缩放条不影响这里的数字）'}>
        {scope === 'visible' ? '当前视窗' : '所选区间'}
      </span>
      <Cell label="区间涨跌（收盘→收盘）">
        <span className={cn('font-mono font-medium', pctCls)}>{fmtPct(stats.pct)}</span>
      </Cell>
      <Cell label="最大回撤">
        <span className="font-mono text-secondary">
          {stats.maxDrawdown == null ? '—' : `-${stats.maxDrawdown.toFixed(2)}%`}
        </span>
      </Cell>
      <Cell label="距区间最高收盘">
        <span className={cn('font-mono', ddCls)}>{fmtPct(stats.fromHigh)}</span>
      </Cell>
      <Cell label="区间最高">
        <span className="font-mono text-secondary">{stats.high?.toFixed(2) ?? '—'}</span>
        <span className="ml-1 font-mono text-[9px] text-muted">{stats.highDate}</span>
      </Cell>
      <Cell label="区间最低">
        <span className="font-mono text-secondary">{stats.low?.toFixed(2) ?? '—'}</span>
        <span className="ml-1 font-mono text-[9px] text-muted">{stats.lowDate}</span>
      </Cell>
      {yi != null && (
        <Cell label="成交额合计">
          <span className="font-mono text-secondary">{yi >= 1 ? `${yi.toFixed(1)} 亿` : `${(yi * 1e4).toFixed(0)} 万`}</span>
        </Cell>
      )}
    </div>
  )
}

function Cell({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-baseline gap-1">
      <span className="text-muted">{label}</span>
      {children}
    </span>
  )
}

/**
 * 简称轨迹 —— 只在「真的看到过两个不同简称」时出现。
 *
 * 龙虎榜快照是稀疏观测，所以这里给的是**区间**：改名发生在
 * 前一个简称最后见到之后、后一个简称首次见到之前。写死某一天就是编。
 */
export function NameTrace({ data }: { data: StockNameHistory | undefined }) {
  if (!data || !data.renamed) return null
  return (
    <div className="rounded-lg border border-border/40 bg-base/20 px-3 py-1.5 text-[10px] leading-relaxed">
      <span className="text-muted">简称轨迹</span>
      <span className="ml-1 text-muted/60">（依据本地 {data.snapshots} 份龙虎榜快照里的历史简称）</span>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1">
        {data.bands.map((b, i) => (
          <span key={`${b.name}-${b.first_seen}`} className="inline-flex items-baseline gap-1">
            {i > 0 && <span className="text-muted/50">→</span>}
            <span className="font-medium text-foreground">{b.name}</span>
            <span className="font-mono text-[9px] text-muted">
              {b.first_seen} ~ {b.last_seen}（见到 {b.n} 次）
            </span>
          </span>
        ))}
      </div>
      {data.changes.map(c => (
        <div key={`${c.from}-${c.to}`} className="mt-0.5 text-muted">
          改名发生在 <b className="font-mono text-secondary">{c.after}</b> 之后、
          <b className="font-mono text-secondary"> {c.before}</b> 之前
          {c.gap_days != null ? `（${c.gap_days} 天窗口）` : ''}
          —— 快照只在那天上榜时才有观测，具体哪一天本地没有证据。
        </div>
      ))}
    </div>
  )
}


/**
 * 光标读数条 —— 鼠标所在那根 K 的开高低收/涨跌幅/量额。
 *
 * 图本身不弹浮窗（会正好挡住你看的那几根），改成图上方一行固定位置：
 * 移出去就回落到最后一根，于是"最新一根"永远有地方读。
 */
export function BarReadout({
  row, prevClose, hovered, rowsUnitHint,
}: {
  row: { date: string; open: number; high: number; low: number; close: number; volume?: number; amount?: number } | null
  prevClose?: number
  hovered: boolean
  rowsUnitHint?: string
}) {
  if (!row) return null
  const pct = prevClose && prevClose > 0 ? (row.close / prevClose - 1) * 100 : null
  const up = pct == null ? row.close >= row.open : pct >= 0
  const cls = up ? 'text-bull' : 'text-bear'
  const fmtVol = (v?: number) => {
    if (v == null) return '—'
    if (v >= 1e8) return `${(v / 1e8).toFixed(2)} 亿手`
    if (v >= 1e4) return `${(v / 1e4).toFixed(1)} 万手`
    return `${v.toFixed(0)} 手`
  }
  const fmtAmt = (v?: number) => {
    if (v == null) return '—'
    if (v >= 1e8) return `${(v / 1e8).toFixed(2)} 亿`
    if (v >= 1e4) return `${(v / 1e4).toFixed(0)} 万`
    return `${v.toFixed(0)}`
  }
  const price = (v: number) => v.toFixed(2)
  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-x-1 rounded-lg border border-border/40 bg-base/20 px-3 py-1 font-mono text-[10px] tabular-nums"
      title={`十字光标所在的那根 K${rowsUnitHint ? `　${rowsUnitHint}` : ''}`}
    >
      <span className={hovered ? 'text-foreground' : 'text-muted'}>
        {row.date}{hovered ? '' : '（最新）'}
      </span>
      <span className="text-muted">开 <span className="text-secondary">{price(row.open)}</span></span>
      <span className="text-muted">高 <span className="text-secondary">{price(row.high)}</span></span>
      <span className="text-muted">低 <span className="text-secondary">{price(row.low)}</span></span>
      <span className="text-muted">收 <span className={`font-medium ${cls}`}>{price(row.close)}</span></span>
      <span className={`${cls} font-medium`}>{pct == null ? '—' : `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`}</span>
      <span className="text-muted">量 <span className="text-secondary">{fmtVol(row.volume)}</span></span>
      <span className="text-muted">额 <span className="text-secondary">{fmtAmt(row.amount)}</span></span>
    </div>
  )
}
