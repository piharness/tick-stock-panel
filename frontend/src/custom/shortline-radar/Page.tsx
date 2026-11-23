/**
 * 短线雷达 —— 博弈角色主场 + 属性标签视图 + 今日观察池。
 *
 * 定位：这是**观察池**，不是买入名单。实证（`docs/touchpoint-holding-drawdown.md`）：
 * heat 最高 1/5 的 20 日超额是 −1.50%/t=−5.03（1,853 天、8/8 年负、控制市值后保留），
 * 所以「按 heat 取前 15 买入」是负期望；页面的价值在「今天谁在动、流动性够不够、
 * 谁在买」，不在「买哪只」。短线执行口径另见页内「执行窗口」卡。
 *
 * 三层结构，回答三个不同的问题：
 *
 *   0. 视图（屏幕轴）：**现在看哪一类？**
 *      全部 / 蓝筹 / 白马 / 龙头 / 妖股，互斥切换。它只是屏幕隔离，
 *      不是分类：五个 tab 的计数不能相加（重叠见下）。
 *
 *   1. 主场（分组轴）：**这里平时是谁的地盘？**
 *      两个结构轴（20 日换手率中位 × 20 日流通市值中位）各切一刀：
 *        机构主场  大市值 × 低换手 —— 长钱/被动按权重调仓的痕迹
 *        量化主场  大市值 × 高换手 —— 高频交易盘接力的痕迹
 *        游资主场  小市值 × 高换手 —— 打板/题材资金的痕迹
 *        无人区    小市值 × 低换手 —— 没有定价者，卖出即砸价
 *      用中位而非当日值：当日换手分类的日间切换率 9.35%，中位版 1.37%。
 *
 *   2. 属性标签（隔离视图）：**这只票是什么货色？**
 *      蓝筹 / 白马 / 龙头 / 妖股。这四个词不在同一维度上（当前 362 只命中 2 个、
 *      33 只命中 3 个，蓝筹×龙头 287 只），所以**不能做互斥分类**，只能做过滤视图：
 *      顶部标签页一次只显示一个标签，四张表不在同一屏里并排；同一只票仍会出现在
 *      多个标签页里，但任何一屏只有一份。标签页回答「这一类长什么样」，
 *      不是「把市场切成四块」。
 *
 *      布局上也以标签为主轴：一屏只讲一个标签的只数、左尾、中位与主场分布，
 *      四个主场在标签内部再分组 —— 标签是「看哪一类」，主场是「谁的地盘」。
 *      观察池与执行窗口是全市场口径，不随标签变化，只放在「全部」页。
 *
 * 反直觉但必须写在页面上的一条：「龙头」不是安全标签。它比全市场更危险
 * （dd20 6.23% vs 4.04%），因为它是动量标签，而动量会反转。页面用「vs 大盘」
 * 把这件事直接标出来，不让用户把「强势」误当「稳健」。
 *
 * 归属：扩展域官方页面，经 custom/shortline-radar/extension.tsx 注册路由与导航；
 * 对核心能力均为只读 import（api/QK/组件/格式化），不承载核心域语义。
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowDownWideNarrow, AlertTriangle, ChevronDown, RefreshCw, Search, TrendingDown, TrendingUp, X,
} from 'lucide-react'
import {
  api,
  type ShortlineRadarArena,
  type ShortlineRadarArenaSummary,
  type ShortlineRadarData,
  type ShortlineRadarFocus,
  type ShortlineRadarLevel,
  type ShortlineRadarRisk,
  type ShortlineRadarRow,
  type ShortlineRadarStage,
  type ShortlineRadarTag,
  type ShortlineRadarTagSummary,
  type SortAudit,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { DateInput } from '@/components/DateInput'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { SHORTLINE_RADAR_PAGE_SUBTITLE, SHORTLINE_RADAR_PAGE_TITLE } from './meta'

// ===== 主场（配色是前端关注点；文案须与后端 ARENA_LABELS 一致）=====
const ARENA_ORDER: ShortlineRadarArena[] = ['institution', 'quant', 'hotmoney', 'nowhere']

const ARENA_META: Record<ShortlineRadarArena, {
  label: string; short: string; dot: string; bar: string; soft: string
}> = {
  institution: { label: '机构主场', short: '机构', dot: 'bg-violet-500', bar: 'bg-violet-500/70', soft: 'bg-violet-500/[0.06]' },
  quant: { label: '量化主场', short: '量化', dot: 'bg-cyan-500', bar: 'bg-cyan-500/70', soft: 'bg-cyan-500/[0.06]' },
  hotmoney: { label: '游资主场', short: '游资', dot: 'bg-orange-500', bar: 'bg-orange-500/70', soft: 'bg-orange-500/[0.06]' },
  nowhere: { label: '无人区', short: '无人', dot: 'bg-zinc-400', bar: 'bg-zinc-400/70', soft: 'bg-zinc-500/[0.05]' },
}

// ===== 属性标签：互斥视图，不是分类 =====
const TAG_ORDER: ShortlineRadarTag[] = ['bluechip', 'whitehorse', 'leader', 'yaogu']

const TAG_META: Record<ShortlineRadarTag, { label: string; chip: string; dot: string }> = {
  bluechip: { label: '蓝筹', chip: 'border-blue-500/25 bg-blue-500/[0.07] text-blue-600', dot: 'bg-blue-500' },
  whitehorse: { label: '白马', chip: 'border-emerald-500/25 bg-emerald-500/[0.07] text-emerald-600', dot: 'bg-emerald-500' },
  leader: { label: '龙头', chip: 'border-rose-500/25 bg-rose-500/[0.07] text-rose-600', dot: 'bg-rose-500' },
  yaogu: { label: '妖股', chip: 'border-purple-500/25 bg-purple-500/[0.07] text-purple-600', dot: 'bg-purple-500' },
}

/** 视图键：all = 全市场；其余一次只看一个标签。互斥是**屏幕**互斥，不是分类互斥。 */
type ViewKey = 'all' | ShortlineRadarTag

const VIEWS: { key: ViewKey; label: string; dot: string | null }[] = [
  { key: 'all', label: '全部', dot: null },
  ...TAG_ORDER.map(key => ({ key, label: TAG_META[key].label, dot: TAG_META[key].dot })),
]

// ===== 位次（当日成交额/换手分位）=====
const LEVEL_ORDER: ShortlineRadarLevel[] = ['core', 'active', 'background', 'silent']

const LEVEL_META: Record<ShortlineRadarLevel, { label: string; chip: string; bar: string }> = {
  core: { label: '核心', chip: 'border-amber-500/30 bg-amber-500/10 text-amber-600', bar: 'bg-amber-500' },
  active: { label: '活跃', chip: 'border-sky-500/30 bg-sky-500/10 text-sky-600', bar: 'bg-sky-500' },
  background: { label: '背景', chip: 'border-border bg-elevated/50 text-muted', bar: 'bg-zinc-400' },
  silent: { label: '沉默', chip: 'border-border bg-elevated/30 text-muted/70', bar: 'bg-zinc-500' },
}

const RISK_META: Record<ShortlineRadarRisk, { label: string; chip: string; hint: string }> = {
  high: { label: 'ST', chip: 'border-red-500/30 bg-red-500/10 text-red-600', hint: 'ST / *ST：下一站是退市整理期' },
  watch: { label: '警示', chip: 'border-amber-500/25 bg-amber-500/[0.08] text-amber-600', hint: '流通市值 < 10 亿，或成交额 < 2000 万' },
  none: { label: '—', chip: 'text-muted/25', hint: '无风险标记' },
}

type SortKey = 'amount' | 'mcap' | 'ret20' | 'heat'

const SORTS: { key: SortKey; label: string }[] = [
  { key: 'amount', label: '成交额' },
  { key: 'mcap', label: '市值' },
  { key: 'ret20', label: '20日涨幅' },
  { key: 'heat', label: '热度分' },
]

// ===== 阶段：唯一区分「有预测价值」与「已被兑现」的字段 =====
// 色阶刻意与主场区分：阶段是「温度」，主场是「身份」。
// 文案须与后端 STAGE_LABELS 一致（前端只负责配色）；口径定义见 /api/shortline-radar/levels。
const STAGE_META: Record<ShortlineRadarStage, { label: string; chip: string; dot: string }> = {
  cold: { label: '待启动', chip: 'border-emerald-500/30 bg-emerald-500/[0.08] text-emerald-600', dot: 'bg-emerald-500' },
  warming: { label: '已启动', chip: 'border-amber-500/30 bg-amber-500/[0.08] text-amber-600', dot: 'bg-amber-500' },
  hot: { label: '正在炒', chip: 'border-orange-500/30 bg-orange-500/[0.08] text-orange-600', dot: 'bg-orange-500' },
  fading: { label: '退潮', chip: 'border-zinc-500/25 bg-zinc-500/[0.06] text-zinc-500', dot: 'bg-zinc-400' },
}

/** 单格默认渲染行数上限：四格全量 5,572 行 × 9 列 ≈ 5 万单元格，会明显卡顿。 */
const ROW_LIMIT = 150

function pctBar(p: number): string {
  return `${Math.round(Math.min(1, Math.max(0, p)) * 100)}%`
}

/** 左尾概率的读法：<3% 温和，3~7% 偏高，>7% 危险 */
function ddTone(dd: number): string {
  if (dd >= 0.07) return 'text-red-600'
  if (dd >= 0.03) return 'text-amber-600'
  return 'text-muted'
}

/** 相对大盘的差值。lowerIsBetter 区分「左尾越低越好」与「收益越高越好」。 */
function Delta({ value, base, lowerIsBetter }: { value: number | null; base: number; lowerIsBetter: boolean }) {
  if (value === null) return <span className="text-[9px] text-muted/40">无背书</span>
  const d = value - base
  const good = lowerIsBetter ? d < 0 : d > 0
  return (
    <span className={cn('font-mono text-[10px] tabular-nums', good ? 'text-emerald-600' : 'text-red-600')}>
      {d < 0 ? '−' : '+'}{Math.abs(d * 100).toFixed(2)}pp
    </span>
  )
}

// ===== 市场结构带：一条连续的构成带，宽度 = 只数占比 =====
function MarketBand({
  arenas, total, activeArena, onPick,
}: {
  arenas: ShortlineRadarArenaSummary[]
  total: number
  activeArena: ShortlineRadarArena | null
  onPick: (a: ShortlineRadarArena) => void
}) {
  return (
    <div className="overflow-hidden rounded-card border border-border bg-surface/50">
      <div className="flex h-[80px] w-full">
        {ARENA_ORDER.map(key => {
          const s = arenas.find(a => a.arena === key)
          if (!s || s.count === 0) return null
          const meta = ARENA_META[key]
          const dim = activeArena !== null && activeArena !== key
          // 窄段放不下三行标签：按占比降级信息密度，而不是让文字互相挤压
          const wide = s.share >= 0.18
          const mid = s.share >= 0.11
          return (
            <button
              key={key}
              type="button"
              onClick={() => onPick(key)}
              style={{ flexGrow: s.count, flexBasis: 0 }}
              className={cn(
                'group relative flex min-w-0 flex-col justify-center gap-0.5 px-2.5 text-left transition-all duration-200',
                'border-r border-border/40 last:border-r-0',
                meta.soft,
                dim ? 'opacity-30' : 'opacity-100',
                activeArena === key && 'ring-1 ring-inset ring-border',
              )}
              title={`${s.label} · ${s.hint}（点击只看这一格）`}
            >
              <div className="flex items-center gap-1.5">
                <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', meta.dot)} />
                <span className="truncate text-[11px] font-medium text-foreground">{s.label}</span>
                {mid && (
                  <span className="ml-auto shrink-0 font-mono text-[10px] tabular-nums text-muted">
                    {(s.share * 100).toFixed(0)}%
                  </span>
                )}
              </div>
              <div className="flex items-baseline gap-1">
                <span className="font-mono text-[16px] leading-tight tabular-nums text-foreground">
                  {s.count.toLocaleString()}
                </span>
                {wide && <span className="text-[9px] text-muted/60">只</span>}
                {wide && (
                  <>
                    <span className="ml-1 text-[9px] text-muted/50">左尾</span>
                    <span className={cn('font-mono text-[10px] tabular-nums', ddTone(s.dd20))}>
                      {(s.dd20 * 100).toFixed(2)}%
                    </span>
                  </>
                )}
              </div>
              {!wide && mid && (
                <div className={cn('font-mono text-[9px] tabular-nums', ddTone(s.dd20))}>
                  {(s.dd20 * 100).toFixed(2)}%
                </div>
              )}
              {/* 底部占比条，让「宽度」这件事有第二重编码 */}
              <div className="absolute inset-x-0 bottom-0 h-[2px] bg-elevated/60">
                <div className={cn('h-full', meta.bar)} style={{ width: pctBar(s.count / total) }} />
              </div>
            </button>
          )
        })}
      </div>
    </div>
  )
}

/** 折叠行里的等级构成微条：让折叠态也有信息密度，而不是四根空条 */
function LevelMix({ levels, total }: { levels: Record<ShortlineRadarLevel, number>; total: number }) {
  if (!total) return null
  return (
    <div className="hidden h-1 w-16 overflow-hidden rounded-full bg-elevated sm:flex">
      {LEVEL_ORDER.map(lv => {
        const n = levels[lv] ?? 0
        if (!n) return null
        return (
          <div key={lv} className={cn('h-full', LEVEL_META[lv].bar)}
            style={{ width: `${(n / total) * 100}%` }} title={`${LEVEL_META[lv].label} ${n} 只`} />
        )
      })}
    </div>
  )
}

/**
 * 标签视图条 —— 页面的主轴。
 *
 * 互斥切换，不是分类：蓝筹与龙头实测重叠 287 只，做成四个分类格会给出错误断言。
 * 这里只是「一次看一类」，所以同一只票可以出现在多个视图里，但同一屏只有一份。
 * 计数取自后端 summary.tags（与 rows 实测一致），不在前端重算第二个真相。
 */
function ViewTabs({
  counts, active, onChange,
}: {
  counts: Record<ViewKey, number>
  active: ViewKey
  onChange: (v: ViewKey) => void
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const move = (e: ReactKeyboardEvent<HTMLButtonElement>, i: number) => {
    const n = VIEWS.length
    const next = e.key === 'ArrowRight' ? (i + 1) % n
      : e.key === 'ArrowLeft' ? (i - 1 + n) % n
        : e.key === 'Home' ? 0
          : e.key === 'End' ? n - 1
            : null
    if (next === null) return
    e.preventDefault()
    onChange(VIEWS[next].key)
    refs.current[next]?.focus()
  }
  return (
    <div role="tablist" aria-label="属性标签视图" className="flex min-w-0 flex-1 items-stretch gap-0 overflow-x-auto">
      {VIEWS.map((v, i) => {
        const on = active === v.key
        return (
          <button
            key={v.key}
            ref={el => { refs.current[i] = el }}
            type="button"
            role="tab"
            id={`radar-tab-${v.key}`}
            aria-selected={on}
            aria-controls="radar-panel"
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(v.key)}
            onKeyDown={e => move(e, i)}
            className={cn(
              'relative flex shrink-0 items-baseline gap-1.5 px-3 py-2 text-[12px] transition-colors',
              on ? 'text-foreground' : 'text-muted hover:text-secondary',
            )}
          >
            {v.dot && (
              <span className={cn('h-1.5 w-1.5 shrink-0 self-center rounded-full transition-opacity',
                v.dot, on ? 'opacity-100' : 'opacity-40')} />
            )}
            <span className={on ? 'font-medium' : undefined}>{v.label}</span>
            <span className={cn('font-mono text-[10px] tabular-nums', on ? 'text-muted' : 'text-muted/50')}>
              {counts[v.key].toLocaleString()}
            </span>
            {on && <span className="absolute inset-x-2 -bottom-px h-[2px] rounded-full bg-accent" />}
          </button>
        )
      })}
    </div>
  )
}

/**
 * 标签内的主场分布 —— 只给只数与占比，不给 dd20/中位。
 *
 * 后端只提供「标签整体」与「主场整体」两套历史读数，没有 tag×arena 的交叉读数；
 * 在这一格上显示主场的历史左尾，会让人读成「这个标签在这个主场里的风险」。
 * 要显示就等真算了交叉再说，这里只做定位，不做背书。
 */
function ArenaMix({
  counts, total, active, onPick,
}: {
  counts: { arena: ShortlineRadarArena; count: number }[]
  total: number
  active: ShortlineRadarArena | null
  onPick: (a: ShortlineRadarArena) => void
}) {
  const shown = counts.filter(c => c.count > 0)
  const denom = total || 1
  if (shown.length === 0) {
    return (
      <div className="flex h-[38px] items-center border-t border-border/40 px-4 text-[10px] text-muted/60">
        该标签今日没有命中的票
      </div>
    )
  }
  return (
    <div className="flex h-[38px] w-full border-t border-border/40" role="group" aria-label="该标签的主场分布">
      {shown.map(({ arena, count }) => {
        const meta = ARENA_META[arena]
        const dim = active !== null && active !== arena
        return (
          <button
            key={arena}
            type="button"
            onClick={() => onPick(arena)}
            aria-label={`${meta.label} ${count.toLocaleString()} 只`}
            aria-pressed={active === arena}
            style={{ flexGrow: count, flexBasis: 0 }}
            title={`${meta.label} ${count.toLocaleString()} 只 · 占该标签 ${(count / denom * 100).toFixed(1)}% · 点击只看这一格`}
            className={cn(
              'relative flex min-w-0 items-center gap-1.5 border-r border-border/30 px-1.5 text-left transition-all duration-200 last:border-r-0 sm:px-2',
              meta.soft,
              dim ? 'opacity-35' : 'opacity-100 hover:opacity-80',
            )}
          >
            <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', meta.dot)} />
            <span className={cn('hidden truncate text-[10px] text-secondary sm:inline')}>{meta.short}</span>
            <span className="font-mono text-[11px] tabular-nums text-foreground">{count.toLocaleString()}</span>
            <span className="ml-auto shrink-0 font-mono text-[9px] tabular-nums text-muted/70">
              {Math.round(count / denom * 100)}%
            </span>
            {active === arena && <span className="absolute inset-x-0 bottom-0 h-[2px] bg-foreground/40" />}
          </button>
        )
      })}
    </div>
  )
}

/**
 * 标签页头 —— 每个标签一屏时的第一块：只数、左尾、中位、主场分布。
 *
 * 三条口径必须写清楚，否则会被读成「蓝筹稳、龙头强」：
 *   1. 左尾/中位是 34 期 fwd20 的历史读数，不是预测；
 *   2. 白马没有 PIT 财报序列 ⇒ 没有历史读数，显示「无历史背书」而不是 0；
 *   3. 龙头是动量标签，左尾比大盘更高（6.23% vs 4.04%）—— 强不等于稳。
 */
function TagHeader({
  tag, summary, arenaCounts, market, financialPeriod, activeArena, onPickArena, onGoAll,
}: {
  tag: ShortlineRadarTag
  summary: ShortlineRadarTagSummary
  arenaCounts: { arena: ShortlineRadarArena; count: number }[]
  market: { dd20: number; med_ret20: number }
  financialPeriod: string | null
  activeArena: ShortlineRadarArena | null
  onPickArena: (a: ShortlineRadarArena) => void
  onGoAll: () => void
}) {
  const meta = TAG_META[tag]
  const dd = summary.dd20
  const med = summary.med_ret20
  const validated = summary.validated && dd !== null && med !== null
  const riskier = dd !== null && dd > market.dd20
  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface/40">
      <div className="flex flex-col gap-2 px-4 pb-2.5 pt-3 sm:flex-row sm:flex-wrap sm:items-start sm:gap-x-6">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className={cn('h-2 w-2 self-center rounded-full', meta.dot)} />
            <h2 className="text-[13px] font-medium text-foreground">{meta.label}</h2>
            <span className="font-mono text-[19px] leading-none tabular-nums text-foreground">
              {summary.count.toLocaleString()}
            </span>
            <span className="text-[10px] text-muted">
              只 · 占全市场 {(summary.share * 100).toFixed(1)}%
            </span>
          </div>
          <p className="mt-1 max-w-none text-[10px] leading-relaxed text-muted/70 sm:max-w-[560px]">{summary.hint}</p>
        </div>
        <div className="flex w-full shrink-0 items-start justify-between gap-5 sm:w-auto sm:justify-start">
          <div>
            <div className="text-[9px] tracking-wider text-muted/50">历史左尾 · fwd20 跌 &gt;20%</div>
            <div className="mt-0.5 flex items-baseline gap-1.5">
              {dd === null ? (
                <span className="text-[11px] text-muted/50">无历史背书</span>
              ) : (
                <>
                  <span className={cn('font-mono text-[15px] leading-none tabular-nums', ddTone(dd))}>
                    {(dd * 100).toFixed(2)}%
                  </span>
                  <Delta value={dd} base={market.dd20} lowerIsBetter />
                </>
              )}
            </div>
          </div>
          <div>
            <div className="text-[9px] tracking-wider text-muted/50">fwd20 中位</div>
            <div className="mt-0.5 flex items-baseline gap-1.5">
              {med === null ? (
                <span className="text-[11px] text-muted/50">—</span>
              ) : (
                <>
                  <span className={cn('font-mono text-[15px] leading-none tabular-nums',
                    med < 0 ? 'text-emerald-600' : 'text-red-600')}>
                    {(med * 100).toFixed(2)}%
                  </span>
                  <Delta value={med} base={market.med_ret20} lowerIsBetter={false} />
                </>
              )}
            </div>
          </div>
        </div>
      </div>

      <ArenaMix counts={arenaCounts} total={summary.count} active={activeArena} onPick={onPickArena} />

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/50 bg-elevated/20 px-4 py-1.5 text-[9.5px] leading-relaxed text-muted/70">
        <span>
          {!validated
            ? `无历史背书：该标签依赖财报${financialPeriod ? `（${financialPeriod}）` : ''}，只有单期数据，不做历史读数。`
            : riskier
              ? `比大盘更危险：左尾高 ${((dd - market.dd20) * 100).toFixed(2)}pp —— 这是动量/波动标签，强势会反转。`
              : `比大盘更安全：左尾低 ${((market.dd20 - dd) * 100).toFixed(2)}pp，但仍是历史读数，不是预测。`}
        </span>
        <span className="ml-auto">
          观察池与执行窗口是全市场口径，见
          <button type="button" onClick={onGoAll} className="ml-1 underline underline-offset-2 hover:text-foreground">
            全部
          </button>
        </span>
      </div>
    </section>
  )
}

/** 明细工具条：排序 + 搜索。标签已升到视图条，这里不再放属性 chip。 */
function DetailToolbar({
  sortKey, onSort, keyword, onKeyword, canClear, onClear,
}: {
  sortKey: SortKey
  onSort: (k: SortKey) => void
  keyword: string
  onKeyword: (v: string) => void
  canClear: boolean
  onClear: () => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-card border border-border bg-surface/40 px-3 py-2">
      <div className="flex items-center gap-1">
        <ArrowDownWideNarrow className="h-3 w-3 text-muted/50" />
        {SORTS.map(s => (
          <button key={s.key} type="button" onClick={() => onSort(s.key)}
            aria-pressed={sortKey === s.key}
            className={cn('rounded px-1.5 py-0.5 text-[10px] transition-colors',
              sortKey === s.key ? 'bg-elevated text-foreground' : 'text-muted/60 hover:text-secondary')}>
            {s.label}
          </button>
        ))}
      </div>
      {canClear && (
        <button type="button" onClick={onClear}
          className="text-[10px] text-muted/60 underline underline-offset-2 hover:text-foreground">
          清空筛选
        </button>
      )}
      <div className="relative ml-auto">
        <Search className="pointer-events-none absolute left-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-muted/40" />
        <input
          value={keyword}
          onChange={e => onKeyword(e.target.value)}
          placeholder="代码 / 名称 / 行业"
          aria-label="搜索代码、名称或行业"
          className="w-40 rounded-btn border border-border bg-elevated py-0.5 pl-6 pr-5 text-[10px] text-secondary placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/40"
        />
        {keyword && (
          <button type="button" onClick={() => onKeyword('')} aria-label="清空搜索"
            className="absolute right-1 top-1/2 -translate-y-1/2 text-muted/50 hover:text-foreground">
            <X className="h-3 w-3" />
          </button>
        )}
      </div>
    </div>
  )
}

// ===== 单行 =====
// activeTag：当前标签视图。已经按它筛过，所以列里只显示**其他**标签 ——
// 这一列在标签页里回答的是「它还同时是什么」，不是把同一个 chip 重复 150 次。
function Row({
  r, onOpenStock, activeTag,
}: {
  r: ShortlineRadarRow
  onOpenStock: (s: string) => void
  activeTag: ShortlineRadarTag | null
}) {
  const hot = r.turn_ratio >= 2
  const cold = r.turn_ratio <= 0.5
  const young = r.n_days < 10
  const shownTags = activeTag ? r.tags.filter(t => t !== activeTag) : r.tags
  return (
    <tr
      onClick={() => onOpenStock(r.symbol)}
      className="group cursor-pointer border-b border-border/25 text-[11px] transition-colors last:border-0 hover:bg-accent/[0.04]"
    >
      <td className="py-1.5 pl-3 pr-2">
        <div className="flex items-baseline gap-1.5">
          <span className="font-medium text-foreground">{r.name ?? r.symbol}</span>
          <span className="font-mono text-[9px] text-muted/70">{r.symbol}</span>
          {young && (
            <span className="rounded border border-border/60 px-0.5 text-[8px] leading-tight text-muted/60"
              title={`窗口内仅 ${r.n_days} 个交易日（次新）：主场用已有天数估计`}>新</span>
          )}
          <span className={cn('rounded border px-1 text-[8.5px] font-medium', STAGE_META[r.stage].chip)}
            title={r.stage === 'hot'
              ? `近 20 日已涨停 ${r.lu20} 天：预测价值已被兑现，不在观察池里`
              : r.stage === 'cold'
                ? '近 20 日无涨停且涨幅在 ±15% 内：唯一有历史 lift 的一段'
                : r.stage === 'warming' ? '未涨停但 20 日涨幅已超 15%' : '20 日跌超 20%，高波动来自出货'}>
            {STAGE_META[r.stage].label}
          </span>
        </div>
        <div className="mt-0.5 truncate text-[9px] text-muted/55">{r.industry ?? '未分类'}</div>
      </td>
      <td className="px-2 py-1.5">
        <div className="flex flex-wrap items-center gap-1">
          {shownTags.length === 0 ? (
            <span className="text-[9px] text-muted/25">—</span>
          ) : (
            shownTags.map(t => (
              <span key={t} className={cn('rounded border px-1 py-px text-[9px] font-medium', TAG_META[t].chip)}>
                {TAG_META[t].label}
              </span>
            ))
          )}
        </div>
      </td>
      <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">{fmtBigNum(r.mcap)}</td>
      <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">{fmtBigNum(r.amount)}</td>
      <td className="px-2 py-1.5 text-right">
        <div className="font-mono tabular-nums text-secondary">{r.turnover_rate.toFixed(1)}%</div>
        <div className={cn('font-mono text-[9px] tabular-nums',
          hot ? 'text-orange-600' : cold ? 'text-muted/40' : 'text-muted/60')}
          title={`当日换手 ${r.turnover_rate.toFixed(2)}% ÷ 20 日中位 ${r.turn_med.toFixed(2)}%`}>
          ×{r.turn_ratio.toFixed(1)}
        </div>
      </td>
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-2" title="20 日市值中位 / 20 日换手中位 的当日分位 —— 两轴决定主场">
          {[{ k: '市', p: r.p_scale }, { k: '换', p: r.p_turn_med }].map(a => (
            <div key={a.k} className="flex items-center gap-1">
              <span className="w-3 text-[9px] leading-none text-muted/50">{a.k}</span>
              <div className="relative h-1 w-10 overflow-hidden rounded-full bg-elevated">
                <div className="absolute inset-y-0 left-0 rounded-full bg-accent/60" style={{ width: pctBar(a.p) }} />
              </div>
            </div>
          ))}
        </div>
      </td>
      <td className="px-2 py-1.5">
        <span className={cn('rounded border px-1 py-px text-[9px] font-medium', LEVEL_META[r.level].chip)}>
          {LEVEL_META[r.level].label}
        </span>
      </td>
      <td className="px-1.5 py-1.5">
        {r.risk === 'none' ? (
          <span className="text-[9px] text-muted/20">—</span>
        ) : (
          <span className={cn('rounded border px-1 py-px text-[9px] font-medium', RISK_META[r.risk].chip)}
            title={r.risk_reason ?? RISK_META[r.risk].hint}>
            {RISK_META[r.risk].label}
          </span>
        )}
      </td>
      <td className="py-1.5 pl-1 pr-3">
        {r.transition === 'down' ? (
          <span className="inline-flex text-red-600/80" title="当日位次较前一交易日下降"><TrendingDown className="h-3 w-3" /></span>
        ) : r.transition === 'up' ? (
          <span className="inline-flex text-emerald-600/80" title="当日位次较前一交易日上升"><TrendingUp className="h-3 w-3" /></span>
        ) : r.transition === 'new' ? (
          <span className="text-[9px] text-muted/50" title="前一交易日无此票">新</span>
        ) : (
          <span className="text-[9px] text-muted/15">—</span>
        )}
      </td>
    </tr>
  )
}

// ===== 主场区块 =====
function ArenaSection({
  summary, rows, collapsed, onToggle, onOpenStock, titleNote, activeTag,
}: {
  summary: ShortlineRadarArenaSummary
  rows: ShortlineRadarRow[]
  collapsed: boolean
  onToggle: () => void
  onOpenStock: (s: string) => void
  titleNote?: string
  /** 当前标签视图；非空时隐藏主场整体的历史读数（后端没有 tag×arena 交叉读数） */
  activeTag: ShortlineRadarTag | null
}) {
  const [showAll, setShowAll] = useState(false)
  const meta = ARENA_META[summary.arena]
  if (rows.length === 0) return null
  const visible = showAll ? rows : rows.slice(0, ROW_LIMIT)
  // 位次构成按**当前显示的这些行**算，而不是主场整体：标签视图里两者不是同一批票。
  const levelCounts = rows.reduce((acc, r) => {
    acc[r.level] += 1
    return acc
  }, { core: 0, active: 0, background: 0, silent: 0 } as Record<ShortlineRadarLevel, number>)
  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface/40">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-elevated/30"
      >
        <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', meta.dot)} />
        <span className="text-[12px] font-medium text-foreground">{meta.label}</span>
        <span className="font-mono text-[10px] text-muted/70">
          {(activeTag ? rows.length : summary.count).toLocaleString()}
        </span>
        {titleNote && <span className="text-[10px] text-muted/50">· {titleNote}</span>}
        <span className="ml-auto flex shrink-0 items-center gap-2.5">
          <LevelMix levels={levelCounts} total={rows.length} />
          {!activeTag && (
            <>
              <span className="flex items-center gap-1 text-[9px]" title="历史 fwd20 跌超 20% 的概率">
                <span className="text-muted/50">左尾</span>
                <span className={cn('font-mono tabular-nums', ddTone(summary.dd20))}>
                  {(summary.dd20 * 100).toFixed(2)}%
                </span>
              </span>
              <span className="hidden items-center gap-1 text-[9px] lg:flex" title="历史 fwd20 收益中位">
                <span className="text-muted/50">中位</span>
                <span className={cn('font-mono tabular-nums', summary.med_ret20 < 0 ? 'text-emerald-600' : 'text-red-600')}>
                  {(summary.med_ret20 * 100).toFixed(2)}%
                </span>
              </span>
            </>
          )}
        </span>
        <ChevronDown className={cn('h-3.5 w-3.5 shrink-0 text-muted/60 transition-transform duration-200', !collapsed && 'rotate-180')} />
      </button>
      {!collapsed && (
        // 展开后给表格自己的滚动区：602 行 × 34px ≈ 5 屏，全铺开就把「一屏一个标签」变成五屏。
        <div className="max-h-[62vh] overflow-auto border-t border-border/40">
          <table className="w-full min-w-[900px] border-collapse text-left">
            <thead className="sticky top-0 z-10 bg-surface">
              <tr className="border-b border-border/40 text-[9px] uppercase tracking-wider text-muted/50">
                <th className="py-1.5 pl-3 pr-2 font-medium">股票 / 行业</th>
                <th className="px-2 py-1.5 font-medium">{activeTag ? '其他标签' : '属性'}</th>
                <th className="px-2 py-1.5 text-right font-medium">流通市值</th>
                <th className="px-2 py-1.5 text-right font-medium">成交额</th>
                <th className="px-2 py-1.5 text-right font-medium">换手</th>
                <th className="px-2 py-1.5 font-medium">坐标</th>
                <th className="px-2 py-1.5 font-medium">位次</th>
                <th className="px-1.5 py-1.5 font-medium">风险</th>
                <th className="py-1.5 pl-1 pr-3 font-medium">变化</th>
              </tr>
            </thead>
            <tbody>
              {visible.map(r => <Row key={r.symbol} r={r} onOpenStock={onOpenStock} activeTag={activeTag} />)}
            </tbody>
          </table>
          {rows.length > visible.length && (
            <div className="flex items-center justify-center gap-2 border-t border-border/40 py-1.5">
              <span className="text-[9px] text-muted/60">
                已显示 {visible.length} / {rows.length} 只
              </span>
              <button type="button" onClick={() => setShowAll(true)}
                className="rounded-btn border border-border bg-elevated px-1.5 py-0.5 text-[9px] text-secondary hover:text-foreground">
                显示全部
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

/**
 * 今日观察池 —— 页面的第一屏。
 *
 * 为什么把它放在最上面，而不是给表格加一列「游资评分」：
 * 5,572 行加一列，用户的注意力没有被分配，只是多了一个能排序的字段。
 * 这里改成漏斗：每一层都显示「删掉了多少、删掉的是什么」。
 * 漏斗的价值不在剩下的 15 只，而在「为什么其余 5,557 只今天不该看」。
 *
 * 但它不是买入名单：heat 最高 1/5 的 20 日超额 −1.50%/t=−5.03（8/8 年负）。
 * 短线执行口径见下方「执行窗口」，那是事件驱动，不是「买这 15 只」。
 */
/** 排序体检条：现行排序键相对「同日随机 15 只」的前向读数。
 *  数据只读产出物（backend/app/services/shortline_audit.py），本组件不算数、不裁决 ——
 *  判据冻结在 docs/structural-edge/prereg-heat-sort.md，评估日由人执行。
 *  为什么要有这一条：回测显示现行排序比同日随机 15 只差 79bp/20日（z=−15.1），
 *  排序在反向工作；回测留出已被审计看过，唯一干净的样本外是前向登记。 */
function SortAuditStrip({ audit }: { audit: SortAudit | null | undefined }) {
  if (!audit) {
    return (
      <div className="border-t border-emerald-500/10 px-4 py-2 text-[10px] leading-relaxed text-muted/70">
        排序体检未生成 —— 17:10 定时任务（或 <code>shortline_audit.rebuild</code>）首次运行后出现。
        预注册：<code>docs/structural-edge/prereg-heat-sort.md</code>
      </div>
    )
  }
  const g = audit.gate
  // 裁决只看前向；前向还没数据时展示回填对照，但要标明它是样本内。
  const hasFwd = (audit.forward?.months ?? 0) > 0
  const w = hasFwd ? audit.forward! : audit.all
  const gateMonths = audit.forward?.months ?? 0
  const pending = gateMonths < g.min_months
  const winLabel = hasFwd ? `前向登记 ${audit.fwd_days} 天` : `回填对照 ${audit.backfill_days} 天（样本内，不参与裁决）`
  const cell = (
    label: string,
    v: number | null,
    opts?: { t?: number | null; hint?: string },
  ) => (
    <div key={label} className="min-w-[104px]" title={opts?.hint}>
      <div className={cn(
        'font-mono text-[13px] font-medium leading-none tabular-nums',
        v == null ? 'text-muted' : v >= 0 ? 'text-emerald-700' : 'text-red-700',
      )}>
        {v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}bp`}
      </div>
      <div className="mt-1 text-[9.5px] text-muted">
        {label}
        {opts?.t != null && <span className="ml-1 font-mono">t {opts.t}</span>}
      </div>
    </div>
  )
  return (
    <div className="border-t border-emerald-500/15 bg-amber-500/[0.03] px-4 py-2.5">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="text-[11px] font-medium text-foreground">排序体检</span>
        <span className="font-mono text-[9px] text-muted/70">{audit.prereg_id}</span>
        <span className="ml-auto text-[9.5px] text-muted">
          {pending
            ? `进行中 · 前向可评估 ${gateMonths}/${g.min_months} 月（不到裁决线，只看不判）`
            : `已到裁决线 · 前向 ${gateMonths} 个可评估月，按预注册判据人工裁决`}
        </span>
      </div>
      <div className="mt-2 flex flex-wrap gap-x-5 gap-y-2">
        {cell('现行 lift（选中−池）', w.cur_lift_bp, { t: w.cur_t, hint: '现行排序键每天 15 只相对同日池子随机 15 只的超额，月均' })}
        {cell('候选 lift', w.shadow_lift_bp, { t: w.shadow_t, hint: `候选 ${audit.rule_shadow} 同口径` })}
        {cell('配对差（候选−现行）', w.paired_delta_bp, { t: w.paired_t, hint: '同一批月份逐月相减' })}
        {cell('候选净超额', w.shadow_net_bp, { hint: '选中−全市场，扣双边 12bp（红线 ≥0）' })}
      </div>
      <div className="mt-2 text-[9.5px] leading-relaxed text-muted/75">
        {winLabel} · 共登记 {audit.days_registered} 天 / 可评估 {audit.days_evaluated} 天 ·
        门槛：配对差 ≥ {g.lift_delta_bp}bp 且 t &gt; {g.t}，红线 net ≥ {g.net_bp}bp ·
        判据与历史回测见 <code>prereg-heat-sort.md</code>
      </div>
    </div>
  )
}

function FocusBand({
  focus, asOf, onOpenStock,
}: {
  focus: ShortlineRadarFocus
  asOf: string | null
  onOpenStock: (s: string) => void
}) {
  const b = focus.budget
  const steps = [
    { label: '全市场', n: b.market, cls: 'text-muted' },
    { label: '活跃达标', n: b.struct, cls: 'text-sky-600' },
    { label: '未启动', n: b.cold, cls: 'text-emerald-600' },
    { label: '观察池', n: b.picks, cls: 'text-emerald-700' },
  ]
  const ex = focus.excluded
  const excludedText = [
    ex.hot > 0 && `正在炒 ${ex.hot.toLocaleString()}`,
    ex.fading > 0 && `退潮 ${ex.fading.toLocaleString()}`,
    ex.warming > 0 && `已启动 ${ex.warming.toLocaleString()}`,
    ex.falling > 0 && `下跌中继 ${ex.falling}`,
    ex.young > 0 && `次新 ${ex.young}`,
    ex.st > 0 && `ST ${ex.st}`,
    ex.bse > 0 && `北交所 ${ex.bse}`,
    ex.thin > 0 && `成交额不足 ${ex.thin}`,
  ].filter(Boolean) as string[]

  const emo = focus.emotion
  const emoTone: Record<string, string> = {
    green: 'border-emerald-500/20 bg-emerald-500/[0.08] text-emerald-700',
    amber: 'border-amber-500/25 bg-amber-500/[0.08] text-amber-700',
    red: 'border-red-500/25 bg-red-500/[0.08] text-red-700',
  }
  const emoDot: Record<string, string> = {
    green: 'bg-emerald-500', amber: 'bg-amber-500', red: 'bg-red-500',
  }

  return (
    <section className="overflow-hidden rounded-card border border-emerald-500/20 bg-emerald-500/[0.025]">
      {emo?.available && (
        <div className={cn('flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-[11px]', emoTone[emo.state ?? 'amber'])}>
          <span className="flex items-center gap-1.5">
            <span className={cn('h-2 w-2 rounded-full', emoDot[emo.state ?? 'amber'])} />
            <span className="font-medium">情绪闸门 · {emo.label}</span>
          </span>
          <span className="tabular-nums">
            连板晋级率 {(emo.upgrade_rate! * 100).toFixed(1)}%
            <span className="text-muted/70">（今日 {emo.n_lianban_today}/{emo.n_lu_today} 连板）</span>
          </span>
          <span className="tabular-nums text-muted/80">
            近 {emo.window_days} 日分位 {(emo.pct! * 100).toFixed(0)}%
          </span>
          <span className="ml-auto text-muted">{emo.hint}</span>
        </div>
      )}
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-emerald-500/15 px-4 py-2.5">
        <div className="flex items-baseline gap-2">
          <h2 className="text-[13px] font-medium text-foreground">今日观察池</h2>
          <span className="text-[11px] text-muted">明日候选 · 不是买入信号 · {asOf ?? '—'}</span>
        </div>
        <span className="text-[10px] text-muted/70">
          未启动 = 20 日内无涨停 且 |20 日涨幅| &lt; 15%
        </span>
      </div>

      <div className="flex flex-wrap items-stretch gap-0 px-4 py-3">
        {steps.map((s, i) => (
          <div key={s.label} className="flex items-stretch">
            <div className="min-w-[92px]">
              <div className={cn('text-[19px] font-medium leading-none tabular-nums', s.cls)}>
                {s.n.toLocaleString()}
              </div>
              <div className="mt-1 text-[10px] text-muted">{s.label}</div>
            </div>
            {i < steps.length - 1 && (
              <div className="flex flex-col items-center justify-center px-3 text-[9px] text-muted/50">
                <span>÷</span>
                <span className="tabular-nums">
                  {steps[i + 1].n > 0 ? (s.n / steps[i + 1].n).toFixed(1) : '—'}
                </span>
              </div>
            )}
          </div>
        ))}
      </div>

      {excludedText.length > 0 && (
        <div className="border-t border-emerald-500/10 px-4 py-1.5 text-[10px] leading-relaxed text-muted/70">
          被删掉的是：{excludedText.join(' · ')}
        </div>
      )}

      <SortAuditStrip audit={focus.sort_audit} />

      {focus.picks.length === 0 ? (
        <div className="border-t border-emerald-500/10 px-4 py-6 text-center text-[12px] text-muted">
          今日没有票同时满足「活跃达标 + 未启动 + 可退出」。
          <span className="mt-1 block text-[10px] text-muted/60">
            空仓也是一个结论：漏斗空说明今天没有值得盯的位置。
          </span>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-1.5 border-t border-emerald-500/10 p-2.5 sm:grid-cols-3 lg:grid-cols-5">
          {focus.picks.map(p => (
            <button
              key={p.symbol}
              onClick={() => onOpenStock(p.symbol)}
              className="rounded-btn border border-border/70 bg-surface/60 px-2 py-1.5 text-left transition-colors hover:border-emerald-500/40 hover:bg-emerald-500/[0.04]"
            >
              <div className="flex items-baseline justify-between gap-1">
                <span className="truncate text-[11.5px] font-medium text-foreground">
                  {p.name ?? p.symbol}
                </span>
                <span className="shrink-0 font-mono text-[8.5px] text-muted/55">
                  {p.symbol.split('.')[0]}
                </span>
              </div>
              <div className="mt-1 flex items-center gap-1">
                <span className={cn('h-1.5 w-1.5 rounded-full', ARENA_META[p.arena].dot)} />
                <span className="text-[9px] text-muted">{ARENA_META[p.arena].label}</span>
                <span className={cn('rounded border px-1 text-[8.5px] font-medium', STAGE_META[p.stage].chip)}>
                  {STAGE_META[p.stage].label}
                </span>
              </div>
              <div className="mt-1 flex gap-2 font-mono text-[9.5px] tabular-nums text-secondary">
                <span title="20 日均振幅">振 {(p.amp20 * 100).toFixed(1)}%</span>
                <span title="20 日换手中位">换 {p.turn_med.toFixed(1)}%</span>
                <span title="当日成交额">{fmtBigNum(p.amount)}</span>
              </div>
              {p.reasons.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-0.5">
                  {p.reasons.slice(0, 3).map(t => (
                    <span key={t} className="rounded bg-elevated/70 px-1 text-[8.5px] text-muted/80">
                      {t}
                    </span>
                  ))}
                </div>
              )}
            </button>
          ))}
        </div>
      )}

      {focus.seat_backing.length > 0 && (
        <div className="border-t border-amber-500/15 px-4 py-2.5">
          <div className="flex items-baseline gap-2">
            <h3 className="text-[11px] font-medium text-foreground">席位接力</h3>
            <span className="text-[9px] text-muted">
              今日被高声誉游资席位净买入 · 与未启动池正交（这是「谁在买」，不是「还没动」）
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {focus.seat_backing.map(b => (
              <button
                key={b.symbol}
                onClick={() => onOpenStock(b.symbol)}
                className="rounded-btn border border-amber-500/20 bg-amber-500/[0.04] px-2 py-1.5 text-left transition-colors hover:border-amber-500/40 hover:bg-amber-500/[0.07]"
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate text-[11px] font-medium text-foreground">{b.name ?? b.symbol}</span>
                  <span className="shrink-0 font-mono text-[8.5px] text-muted/55">{b.symbol.split('.')[0]}</span>
                </div>
                <div className="mt-0.5 truncate text-[8.5px] text-muted/70" title={b.seat_name}>
                  {b.seat_name}
                </div>
                <div className="mt-0.5 font-mono text-[9px] tabular-nums text-secondary">
                  <span className="text-emerald-600">声誉 +{(b.rep20 * 100).toFixed(1)}%</span>
                  {' · '}净买 {fmtBigNum(b.net ?? 0)}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="border-t border-emerald-500/10 bg-emerald-500/[0.02] px-4 py-1.5 text-[9.5px] leading-relaxed text-muted/65">
        热度分 = 振幅 / 换手 / 市值反向 / 当日成交额 四项分位等权，只用于排序，<span className="text-foreground/70">不是上涨概率</span>。
        实测（2026-06..11 样本外 117 个交易日、4,052 次「涨停+游资净买」）：
        未启动段前 20 名的未来 5 日打板率是基准的 6.98 倍（23.3bp → 162bp）；
        而不剔已涨停的前 20 名里，<span className="text-foreground/70">95.9% 在 20 日内已经涨停过</span>——那是追高榜。
        <span className="text-foreground/70">但打板率不是收益</span>：热度最高 1/5 的 20 日超额 −1.50%/t=−5.03（8/8 年负），所以这里只做观察池，不做买入名单。
      </div>
    </section>
  )
}

/**
 * 执行窗口 —— 短线唯一有正期望证据的口径，但它是**事件驱动**，不是「买观察池」。
 *
 * 数据来自 docs/hotmoney-quant-game.md（2010-2026，换手板 = 收盘涨停且非一字）：
 *   T 日封板 → T+1 开盘卖 +1.45%（2026，17 年唯一稳定为正）
 *   T+1 日内跌 >5% → T+2 开盘买、T+2 收盘卖 +0.836%/t=12.69（2018-2026，九年全正）
 * 代价：T+2 盘中最低点中位 −2.69%、p10 −6.80%，22% 跌破 −5%。
 *
 * 放在观察池下面，是为了让「观察」和「执行」分开：观察池回答「今天谁在动」，
 * 执行窗口回答「什么条件下才可下单、下单后要承受什么」。
 */
function ExecutionWindow() {
  const rows = [
    { label: 'T 日封板 → T+1 开盘卖', value: '+1.45%', note: '2026 · 17 年唯一稳定为正' },
    { label: 'T 日封板 → 持有 5 日', value: '+0.78%', note: '低于 T+1；carry 恒为负' },
    { label: 'T+1 日内跌 >5% → T+2 开盘买、收盘卖', value: '+0.836%', note: 't=12.69 · 九年全正' },
    { label: '同上，剔除 T+2 低开 >9%', value: '+0.764%', note: 't=9.21' },
  ]
  return (
    <section className="rounded-card border border-border bg-surface/40">
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-border px-4 py-2.5">
        <div className="flex items-baseline gap-2">
          <h2 className="text-[13px] font-medium text-foreground">执行窗口</h2>
          <span className="text-[11px] text-muted">事件驱动 · 不是「买这 15 只」</span>
        </div>
        <span className="text-[10px] text-muted/70">全市场换手板口径，不是本页观察池</span>
      </div>
      <div className="grid gap-x-6 gap-y-1.5 px-4 py-2.5 sm:grid-cols-2">
        {rows.map(r => (
          <div key={r.label} className="flex items-baseline justify-between gap-3 border-b border-border/40 py-1">
            <span className="text-[11px] text-secondary">{r.label}</span>
            <span className="shrink-0 font-mono text-[12px] tabular-nums text-foreground">{r.value}</span>
            <span className="shrink-0 text-[9.5px] text-muted/70">{r.note}</span>
          </div>
        ))}
      </div>
      <div className="border-t border-border/60 px-4 py-2 text-[10px] leading-relaxed text-muted/75">
        <span className="text-foreground/80">代价：</span>T+2 开盘后日内最低点中位 −2.69%、p10 −6.80%，22% 的样本盘中跌破 −5%。
        <span className="text-foreground/80"> 不要做：</span>T+1 收盘买 → T+2 开盘卖（隔夜恒负）；追 4 板以上（溢价腰斩）；把缩量涨停当信号（价差 1.74pp → 0.41pp）。
        <span className="text-foreground/80"> 指数是给散户看的</span>；短线看的是换手、封板、承接和流动性。
      </div>
    </section>
  )
}

export function ShortlineRadar() {
  const [asOf, setAsOf] = useState('')
  const [view, setView] = useState<ViewKey>('all')
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [preview, setPreview] = useState<string | null>(null)
  const [arenaFilter, setArenaFilter] = useState<ShortlineRadarArena | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>('amount')
  const [keyword, setKeyword] = useState('')
  const panelRef = useRef<HTMLDivElement | null>(null)

  const q = useQuery<ShortlineRadarData>({
    queryKey: QK.shortlineRadar(asOf || undefined),
    queryFn: () => api.shortlineRadar(asOf || undefined),
    staleTime: 60_000,
  })
  const data = q.data
  const activeTag: ShortlineRadarTag | null = view === 'all' ? null : view

  /** 标签页上的计数：全部取 total，其余取后端 summary（与 rows 实测一致）。 */
  const viewCounts = useMemo(() => {
    const c = { all: 0, bluechip: 0, whitehorse: 0, leader: 0, yaogu: 0 } as Record<ViewKey, number>
    if (!data) return c
    c.all = data.summary.total
    for (const t of data.summary.tags) c[t.tag] = t.count
    return c
  }, [data])

  /** 换标签 = 换一屏。主场筛选与折叠属于上一屏，沿用会让人误读成「该标签没有票」。 */
  const switchView = (v: ViewKey) => {
    setView(v)
    setArenaFilter(null)
    setOpen({})
  }

  // 在一张 150 行的表里切标签时，新的一屏不该从半截开始；已经在顶部就不动。
  useEffect(() => {
    const el = panelRef.current
    if (el && el.getBoundingClientRect().top < 40) el.scrollIntoView?.({ block: 'start' })
  }, [view])

  const filtered = useMemo(() => {
    const rows = data?.rows ?? []
    const kw = keyword.trim().toLowerCase()
    return rows.filter(r => {
      if (activeTag && !r.tags.includes(activeTag)) return false
      if (arenaFilter && r.arena !== arenaFilter) return false
      if (kw) {
        const hay = `${r.symbol} ${r.name ?? ''} ${r.industry ?? ''}`.toLowerCase()
        if (!hay.includes(kw)) return false
      }
      return true
    })
  }, [data, activeTag, arenaFilter, keyword])

  const buckets = useMemo(() => {
    const map: Record<string, ShortlineRadarRow[]> = {}
    for (const r of filtered) (map[r.arena] ??= []).push(r)
    for (const k of Object.keys(map)) {
      map[k].sort((a, b) => {
        if (sortKey === 'mcap') return b.mcap - a.mcap
        if (sortKey === 'ret20') return (b.ret20 ?? -9) - (a.ret20 ?? -9)
        if (sortKey === 'heat') return (b.heat ?? 0) - (a.heat ?? 0)
        return b.amount - a.amount
      })
    }
    return map
  }, [filtered, sortKey])

  /** 该标签的主场分布：不看关键词与主场筛选，否则「分布」会跟着筛选变。 */
  const tagArenaCounts = useMemo(() => {
    if (!activeTag || !data) return []
    const c: Partial<Record<ShortlineRadarArena, number>> = {}
    for (const r of data.rows) {
      if (r.tags.includes(activeTag)) c[r.arena] = (c[r.arena] ?? 0) + 1
    }
    return ARENA_ORDER.map(a => ({ arena: a, count: c[a] ?? 0 }))
  }, [data, activeTag])

  const tagSummary = activeTag ? data?.summary.tags.find(t => t.tag === activeTag) ?? null : null

  /** 标签重叠：脚注里的数字来自当前 rows，不抄文档里的旧值。 */
  const overlap = useMemo(() => {
    let two = 0
    let three = 0
    for (const r of data?.rows ?? []) {
      if (r.tags.length === 2) two += 1
      else if (r.tags.length >= 3) three += 1
    }
    return { two, three }
  }, [data])

  const total = data?.summary.total ?? 0
  const base = activeTag ? tagSummary?.count ?? 0 : total
  const hit = filtered.length
  const narrowing = arenaFilter !== null || keyword.trim() !== ''

  /** 标签页只自动展开票最多的一格：四格全开就是四张长表，又会回到「一屏塞满」。 */
  const autoOpenArena = useMemo(() => {
    let best: { a: ShortlineRadarArena; n: number } | null = null
    for (const a of ARENA_ORDER) {
      const n = (buckets[a] ?? []).length
      if (n > 0 && (!best || n > best.n)) best = { a, n }
    }
    return best?.a ?? null
  }, [buckets])

  const clearFilters = () => {
    setArenaFilter(null)
    setKeyword('')
  }

  return (
    <div className="min-h-full bg-base pb-8">
      <PageHeader
        title={SHORTLINE_RADAR_PAGE_TITLE}
        subtitle={
          asOf
            ? `${SHORTLINE_RADAR_PAGE_SUBTITLE} · 回看 ${asOf}`
            : `${SHORTLINE_RADAR_PAGE_SUBTITLE} · 20 日换手中位 × 市值中位 · 数据截至 ${data?.as_of ?? '—'}`
        }
        right={
          <div className="flex items-center gap-2">
            <DateInput
              value={asOf}
              onChange={setAsOf}
              ariaLabel="回看日期"
              placeholder="回看某天"
              className="rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary"
            />
            <button
              onClick={() => q.refetch()}
              disabled={q.isFetching}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
              title="刷新"
            >
              <RefreshCw className={cn('h-3 w-3', q.isFetching && 'animate-spin')} />刷新
            </button>
          </div>
        }
      />

      <div className="mx-auto max-w-[1360px] px-4 pb-4 sm:px-6">
        {q.isLoading && !data ? (
          <div className="flex h-48 items-center justify-center">
            <div className="flex items-center gap-2 text-sm text-muted">
              <RefreshCw className="h-4 w-4 animate-spin" /> 正在计算全市场博弈角色主场…
            </div>
          </div>
        ) : !data && q.isError ? (
          <div className="mt-4 flex flex-col items-center justify-center gap-2 rounded-card border border-red-500/25 bg-red-500/[0.04] px-6 py-12">
            <div className="text-sm font-medium text-foreground">加载失败</div>
            <p className="text-xs text-muted">{q.error?.message ?? '未知错误'}</p>
            <button onClick={() => q.refetch()}
              className="mt-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary hover:text-foreground">
              重试
            </button>
          </div>
        ) : !data || data.rows.length === 0 ? (
          <div className="mt-4 flex flex-col items-center justify-center gap-3 rounded-card border border-border bg-surface/60 px-6 py-16">
            <div className="text-sm font-medium text-foreground">暂无数据</div>
            <p className="text-xs text-muted">需要日 K 与标的维表，请先前往「数据」页同步</p>
          </div>
        ) : (
          <>
            {/* ① 主轴：标签视图条。一次只显示一个标签，行不在同一屏里重复摆放。 */}
            <div className="sticky top-0 z-20 -mx-4 border-b border-border/70 bg-base/95 backdrop-blur sm:-mx-6">
              <div className="flex items-center gap-3 px-4 pt-1 sm:px-6">
                <ViewTabs counts={viewCounts} active={view} onChange={switchView} />
                <span className="hidden shrink-0 pb-2 text-[9px] leading-tight text-muted/50 lg:block">
                  标签是过滤视图，不是分类
                </span>
              </div>
            </div>

            <div
              ref={panelRef}
              id="radar-panel"
              role="tabpanel"
              aria-labelledby={`radar-tab-${view}`}
              className="scroll-mt-10 space-y-2.5 pt-2.5"
            >
              {q.isError && (
                <div className="flex items-center gap-2 rounded-btn border border-amber-500/25 bg-amber-500/[0.06] px-3 py-1.5 text-[10px] text-amber-700">
                  <AlertTriangle className="h-3 w-3 shrink-0" />
                  刷新失败（{q.error?.message ?? '未知错误'}），当前显示的是上一次成功的数据
                  <button onClick={() => q.refetch()} className="ml-auto underline underline-offset-2">重试</button>
                </div>
              )}

              {/* ② 全部页：市场级的三块（观察池 / 执行窗口 / 结构带）。标签页不重复它们。 */}
              {activeTag === null ? (
                <>
                  <FocusBand focus={data.focus} asOf={data.as_of} onOpenStock={setPreview} />
                  <ExecutionWindow />
                  <MarketBand
                    arenas={data.summary.arenas}
                    total={total}
                    activeArena={arenaFilter}
                    onPick={a => setArenaFilter(prev => (prev === a ? null : a))}
                  />
                </>
              ) : tagSummary ? (
                <TagHeader
                  tag={activeTag}
                  summary={tagSummary}
                  arenaCounts={tagArenaCounts}
                  market={data.summary.market}
                  financialPeriod={data.summary.financial_period}
                  activeArena={arenaFilter}
                  onPickArena={a => setArenaFilter(prev => (prev === a ? null : a))}
                  onGoAll={() => switchView('all')}
                />
              ) : null}

              {/* ③ 明细工具条：标签已在上面，这里只剩排序与搜索 */}
              <DetailToolbar
                sortKey={sortKey}
                onSort={setSortKey}
                keyword={keyword}
                onKeyword={setKeyword}
                canClear={narrowing}
                onClear={clearFilters}
              />

              {/* ④ 结果条只在真的收窄时出现；不收窄时计数已由标签页与结构带给出 */}
              {narrowing && (
                <div data-testid="radar-result" className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-1 text-[10px] text-muted">
                  <span>
                    {activeTag ? '本标签命中' : '全市场命中'}
                    <span className="ml-1 font-mono text-[13px] tabular-nums text-foreground">
                      {hit.toLocaleString()}
                    </span>
                    <span className="text-muted/50"> / {base.toLocaleString()} 只</span>
                  </span>
                  {arenaFilter && (
                    <span>· 主场：<span className="text-secondary">{ARENA_META[arenaFilter].label}</span></span>
                  )}
                  {keyword.trim() && <span>· 搜索「{keyword.trim()}」</span>}
                  {hit === 0 && <span className="text-amber-600">· 这一屏没有命中</span>}
                </div>
              )}

              {/* ⑤ 明细：标签内部再按主场分组 */}
              {hit === 0 ? (
                <div className="flex flex-col items-center justify-center gap-2 rounded-card border border-border bg-surface/40 px-6 py-14">
                  <div className="text-[13px] font-medium text-foreground">这一屏没有同时满足这些条件的票</div>
                  <p className="text-[11px] text-muted">
                    {activeTag ? `「${TAG_META[activeTag].label}」共 ${base} 只，` : `全市场共 ${base} 只，`}
                    关键词或主场筛选把它们排除了。
                  </p>
                  <div className="mt-1 flex items-center gap-2">
                    <button onClick={clearFilters}
                      className="rounded-btn border border-border bg-elevated px-2 py-1 text-[10px] text-secondary hover:text-foreground">
                      清空筛选
                    </button>
                    {activeTag && (
                      <button onClick={() => switchView('all')}
                        className="rounded-btn border border-border bg-elevated px-2 py-1 text-[10px] text-secondary hover:text-foreground">
                        切到全部
                      </button>
                    )}
                  </div>
                </div>
              ) : (
                <div className="space-y-1.5">
                  {ARENA_ORDER.map(key => {
                    const s = data.summary.arenas.find(a => a.arena === key)
                    if (!s) return null
                    const rows = buckets[key] ?? []
                    // 标签页：默认只开票最多的一格；全部页：筛选时展开，否则折叠
                    const isOpen = open[key] ?? (activeTag ? key === autoOpenArena : narrowing)
                    return (
                      <ArenaSection
                        key={key}
                        summary={s}
                        rows={rows}
                        collapsed={!isOpen}
                        onToggle={() => setOpen(o => ({ ...o, [key]: !isOpen }))}
                        onOpenStock={setPreview}
                        activeTag={activeTag}
                        titleNote={
                          activeTag
                            ? (keyword.trim()
                              ? `命中 ${rows.length} / 主场 ${s.count}`
                              : (s.count > 0 ? `占该主场 ${Math.round(rows.length / s.count * 100)}%` : undefined))
                            : (narrowing && rows.length !== s.count ? `命中 ${rows.length} / ${s.count}` : undefined)
                        }
                      />
                    )
                  })}
                </div>
              )}

              {/* ⑥ 口径脚注 */}
              <div className="space-y-1 px-1 pt-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[9px] text-muted/50">
                  <span className="text-muted/40">位次：</span>
                  {LEVEL_ORDER.map(lv => (
                    <span key={lv} className="flex items-center gap-1">
                      <span className={cn('h-1.5 w-1.5 rounded-full', LEVEL_META[lv].bar)} />
                      {LEVEL_META[lv].label}
                    </span>
                  ))}
                  <span className="text-muted/30">·</span>
                  <span>换手 ×N = 当日换手 ÷ 20 日中位</span>
                </div>
                <p className="text-[9px] leading-relaxed text-muted/40">
                  主场由 20 日中位决定（日间切换率 1.37%），当日读数只说明今天。
                  标签页是互斥的<b>视图</b>，不是分类：当前 {overlap.two} 只同时带 2 个标签、
                  {overlap.three} 只带 3 个，它们会出现在多个标签页里 —— 任何一屏只有一份，
                  但把四个标签当成四块市场会数重。
                  {data.summary.financial_period
                    ? `白马依据 ${data.summary.financial_period} 财报（无 PIT 序列，故不给历史读数）。`
                    : '该日期尚无已公告财报，白马标签不可用。'}
                  左尾与中位取自 2024-01–2026-10 共 34 个基期的 fwd20，是历史读数，不是预测；
                  观察池与执行窗口是全市场口径，只在「全部」页出现。
                </p>
              </div>
            </div>
          </>
        )}
      </div>

      <StockPreviewDialog
        symbol={preview}
        triggerInfo={null}
        onClose={() => setPreview(null)}
      />
    </div>
  )
}
