/**
 * 短线雷达 —— 先回答「明天买不买」，再回答「今天谁在动」。
 *
 * 定位（2026-11-28 之后）：**一个买点 + 一张排除清单 + 一个观察池**。
 * 这是筹码页的写法：结论先行、只留一个候选、其余进排除表。
 *
 *   买点：涨停次日砸盘反转（T 日换手板 + T+1 日内 ≤ −5% → T+2 开盘买、收盘卖）。
 *         口径唯一产地 `app/decision/lu_rebound.py`；本页只读清单，不重算信号，
 *         并补上账本面板没有的三件事：代价（p10）、成本（毛→净）、仓位刻度。
 *   观察池：heat 最高 1/5 的 20 日超额 −1.50%/t=−5.03（8/8 年负），按 heat 取前 15
 *         买入是负期望（`docs/touchpoint-holding-drawdown.md`）。所以它仍在，
 *         但只回答「今天谁在动、流动性够不够」，不回答「买哪只」。
 *
 * 第四版的信息架构按**使用顺序**而不是研究顺序排：
 *
 *   0. 明日买点 —— 明天开盘做什么、按什么价、亏多少、什么时候必须走。
 *      事件在 T+1 收盘定稿，不需要盘中盯 5%；旧版把它挡在页外是口径误读。
 *
 *   1. 状态条  —— 今天能不能做、昨天到今天变了什么、哪些不能碰。
 *      情绪闸门 + 位次迁移 + 风险（含「ST 且今日无成交」）。
 *
 *   2. 观察池  —— 全市场漏斗 → 15 只候选。漏斗每一步都写「这一步筛掉多少、筛掉谁」，
 *      并且**每步的分子分母同一批票**：旧版把全市场阶段计数（正在炒 763 / 退潮 508）
 *      混进「519 → 55」这一步，三项加起来比该步删掉的还多，属于跨口径相减。
 *
 *   3. 结构带 + 明细 —— 四个主场谁的地盘、各自历史读数、逐票明细。
 *      默认展开游资主场（短线的主场），不是四格全折叠。
 *
 *   4. 口径抽屉 —— 排序体检、动作对比、定义与来源。研究依据默认收起：
 *      它每天不变，不该和每天变的数字抢第一屏。
 *
 * 归属：扩展域官方页面，经 custom/shortline-radar/extension.tsx 注册路由与导航；
 * 对核心能力均为只读 import（api/QK/组件/格式化），不承载核心域语义。
 */
import {
  useEffect, useMemo, useRef, useState,
  type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type RefObject,
} from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle, ArrowDown, ArrowDownWideNarrow, ArrowUp, ChevronDown, RefreshCw, Search, X,
} from 'lucide-react'
import {
  api,
  type CrossYearCandidate,
  type CrossYearWatch,
  type DecisionLuCandidates,
  type DecisionLuStatus,
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
import { CROSS_YEAR_SCREEN, CROSS_YEAR_STATS } from './crossYearCandidates'
import { LU_EXCLUSIONS, LU_STUDY } from './luBuyPoint'
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
  // 「全市场」不是「四个标签的并集」：标签互斥只是**屏幕**互斥，一只票可能同时属于两三个标签，
  // 四页计数相加 ≠ 全市场。旧名「全部」会把人引向并集，所以改成口径名。
  { key: 'all', label: '全市场', dot: null },
  ...TAG_ORDER.map(key => ({ key, label: TAG_META[key].label, dot: TAG_META[key].dot })),
]

// ===== 位次（当日成交额/换手分位）=====
const LEVEL_ORDER: ShortlineRadarLevel[] = ['core', 'active', 'background', 'silent']

const LEVEL_META: Record<ShortlineRadarLevel, { label: string; chip: string; bar: string }> = {
  core: { label: '核心', chip: 'border-amber-500/30 bg-amber-500/10 text-amber-700', bar: 'bg-amber-500' },
  active: { label: '活跃', chip: 'border-sky-500/25 bg-sky-500/[0.08] text-sky-700', bar: 'bg-sky-500' },
  background: { label: '背景', chip: 'border-border bg-elevated/60 text-muted', bar: 'bg-zinc-400' },
  silent: { label: '沉默', chip: 'border-border/70 bg-elevated/30 text-muted/70', bar: 'bg-zinc-500' },
}

const RISK_META: Record<ShortlineRadarRisk, { label: string; chip: string; hint: string }> = {
  high: { label: 'ST', chip: 'border-red-500/30 bg-red-500/10 text-red-600', hint: 'ST / *ST：下一站是退市整理期' },
  watch: { label: '警示', chip: 'border-amber-500/25 bg-amber-500/[0.08] text-amber-700', hint: '流通市值 < 10 亿，或成交额 < 2000 万' },
  none: { label: '—', chip: 'text-muted/25', hint: '无风险标记' },
}

// 四个排序键都必须是表里能看见的列：排一个看不见的值，用户无法核对顺序。
type SortKey = 'amount' | 'mcap' | 'ret20' | 'turn'

const SORTS: { key: SortKey; label: string }[] = [
  { key: 'amount', label: '成交额' },
  { key: 'mcap', label: '市值' },
  { key: 'ret20', label: '20日涨幅' },
  { key: 'turn', label: '换手倍数' },
]

// ===== 阶段：唯一区分「有预测价值」与「已被兑现」的字段 =====
// 色阶刻意与主场区分：阶段是「温度」，主场是「身份」。
// 文案须与后端 STAGE_LABELS 一致（前端只负责配色）；口径定义见 /api/shortline-radar/levels。
const STAGE_META: Record<ShortlineRadarStage, { label: string; chip: string; dot: string }> = {
  cold: { label: '未启动', chip: 'border-emerald-500/30 bg-emerald-500/[0.08] text-emerald-700', dot: 'bg-emerald-500' },
  warming: { label: '已启动', chip: 'border-amber-500/30 bg-amber-500/[0.08] text-amber-700', dot: 'bg-amber-500' },
  hot: { label: '正在炒', chip: 'border-orange-500/30 bg-orange-500/[0.08] text-orange-700', dot: 'bg-orange-500' },
  fading: { label: '退潮', chip: 'border-zinc-500/25 bg-zinc-500/[0.06] text-zinc-500', dot: 'bg-zinc-400' },
}

/** 单格默认渲染行数上限：四格全量 5,576 行 × 7 列 ≈ 4 万单元格，会明显卡顿。 */
const ROW_LIMIT = 150

/** 筛选维度：只保留「今天有什么变化」相关的三个 + 关键词。 */
type Trend = 'up' | 'down' | 'new'
type RiskFlag = 'high' | 'watch'

interface Narrow {
  arena: ShortlineRadarArena | null
  trend: Trend | null
  risk: RiskFlag | null
  keyword: string
}

const EMPTY_NARROW: Narrow = { arena: null, trend: null, risk: null, keyword: '' }

function pctBar(p: number): string {
  return `${Math.round(Math.min(1, Math.max(0, p)) * 100)}%`
}

function fmtPct(v: number, digits = 2): string {
  return `${(v * 100).toFixed(digits)}%`
}

/** 左尾概率的读法：<3% 温和，3~7% 偏高，>7% 危险。只用于「左尾」这一列，不当涨跌色。 */
function ddTone(dd: number): string {
  if (dd >= 0.07) return 'text-red-600'
  if (dd >= 0.03) return 'text-amber-600'
  return 'text-muted'
}

/**
 * 与全市场同口径的差值。
 *
 * 原版把「中位 −0.34%」按涨跌上绿色、把「+0.78pp」按好坏也上绿色 ——
 * 同一行两颗绿字表达相反含义。现在只给差值上色，原值一律用前景色：
 * 绿 = 这件指标比大盘好，红 = 比大盘差，和涨跌无关。
 */
function Delta({ value, base, lowerIsBetter }: { value: number; base: number; lowerIsBetter: boolean }) {
  const d = value - base
  const tone = d === 0 ? 'text-muted' : (lowerIsBetter ? d < 0 : d > 0) ? 'text-emerald-600' : 'text-red-600'
  return (
    <span className={cn('whitespace-nowrap font-mono text-[10px] tabular-nums', tone)}
      title="与全市场同口径的差值；颜色只表示好/差，不表示涨跌">
      vs 大盘 {d < 0 ? '−' : '+'}{Math.abs(d * 100).toFixed(2)}pp
    </span>
  )
}

/** 小号开关（状态条与工具条共用）：同一个 chip 再点一次 = 取消。 */
function TinyToggle({
  active, onClick, title, children,
}: {
  active: boolean
  onClick: () => void
  title?: string
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      title={title}
      className={cn(
        'inline-flex items-center gap-0.5 rounded border px-1 py-px text-[10px] tabular-nums transition-colors',
        active
          ? 'border-accent/45 bg-accent/10 text-foreground'
          : 'border-border/60 bg-elevated/40 text-muted hover:text-secondary',
      )}
    >
      {children}
    </button>
  )
}

// ===== 市场结构带：一条连续的构成带，宽度 = 只数占比 =====
// 只回答「谁的地盘多大」；历史读数（中位）留给下面的主场行，避免同一屏把同一组数字说两遍。
function MarketBand({
  arenas, total, activeArena, onPick,
}: {
  arenas: ShortlineRadarArenaSummary[]
  total: number
  activeArena: ShortlineRadarArena | null
  onPick: (a: ShortlineRadarArena) => void
}) {
  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface/50" aria-label="主场结构">
      <div className="flex h-[72px] w-full">
        {ARENA_ORDER.map(key => {
          const s = arenas.find(a => a.arena === key)
          if (!s || s.count === 0) return null
          const meta = ARENA_META[key]
          const dim = activeArena !== null && activeArena !== key
          // 窄段放不下三行标签：按占比降级信息密度，而不是让文字互相挤压
          const wide = s.share >= 0.10
          const mid = s.share >= 0.08
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
                <span className="font-mono text-[17px] leading-tight tabular-nums text-foreground">
                  {s.count.toLocaleString()}
                </span>
                {wide && (
                  <>
                    <span className="ml-1 text-[9px] text-muted/60">左尾</span>
                    <span className={cn('font-mono text-[10px] tabular-nums', ddTone(s.dd20))}>
                      {fmtPct(s.dd20)}
                    </span>
                  </>
                )}
              </div>
              {!wide && mid && (
                <div className={cn('font-mono text-[9px] tabular-nums', ddTone(s.dd20))}>{fmtPct(s.dd20)}</div>
              )}
              {/* 底部占比条：让「宽度」这件事有第二重编码 */}
              <div className="absolute inset-x-0 bottom-0 h-[2px] bg-elevated/60">
                <div className={cn('h-full', meta.bar)} style={{ width: pctBar(s.count / total) }} />
              </div>
            </button>
          )
        })}
      </div>
    </section>
  )
}

/** 折叠行里的位次构成微条：6px 高，够看见比例；旧版 1px 实际看不见。 */
function LevelMix({ levels, total }: { levels: Record<ShortlineRadarLevel, number>; total: number }) {
  if (!total) return null
  return (
    <span className="hidden h-1.5 w-[72px] overflow-hidden rounded-full bg-elevated sm:flex"
      title={LEVEL_ORDER.map(lv => `${LEVEL_META[lv].label} ${levels[lv] ?? 0}`).join(' · ')}>
      {LEVEL_ORDER.map(lv => {
        const n = levels[lv] ?? 0
        if (!n) return null
        return <span key={lv} className={cn('h-full', LEVEL_META[lv].bar)} style={{ width: `${(n / total) * 100}%` }} />
      })}
    </span>
  )
}

/**
 * 标签视图条 —— 页面的主轴。
 *
 * 互斥切换，不是分类：蓝筹与龙头实测重叠 241 只（2026-11-24），做成四个分类格会给出错误断言。
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
    <div role="tablist" aria-label="属性标签视图"
      className="grid min-w-0 flex-1 grid-cols-5 items-stretch gap-0 sm:flex sm:overflow-x-auto">
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
              'relative flex min-w-0 items-baseline justify-center gap-1 px-1 py-2 text-[12px] transition-colors sm:shrink-0 sm:justify-start sm:gap-1.5 sm:px-3',
              on ? 'text-foreground' : 'text-muted hover:text-secondary',
            )}
          >
            {v.dot && (
              <span className={cn('hidden h-1.5 w-1.5 shrink-0 self-center rounded-full transition-opacity sm:inline-block',
                v.dot, on ? 'opacity-100' : 'opacity-40')} />
            )}
            <span className={on ? 'font-medium' : undefined}>{v.label}</span>
            <span className={cn('font-mono text-[9.5px] tabular-nums sm:text-[10px]', on ? 'text-muted' : 'text-muted/50')}>
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
 * 状态条 —— 每天都要回答、旧版没有出口的三件事。
 *
 *   情绪：今天能不能做（后端已算，原版只塞在观察池卡片顶上）。
 *   变化：昨天到今天谁上移/下移/新进（summary.transitions 一直有，页面从没显示）。
 *   风险：ST / 警示 / **ST 且今日无成交**。最后一项只对持仓者有意义：
 *         这些票在 rows 里根本不存在，表格永远搜不到，但它们是「卖不掉」的那批。
 *
 * 变化与风险可点击筛选明细 —— 让摘要成为入口，而不是又一段只读文字。
 */
function StatusBar({
  focus, summary, scope, narrow, onTrend, onRisk,
}: {
  focus: ShortlineRadarFocus
  summary: ShortlineRadarData['summary']
  /** 本屏口径（标签页 = 该标签内），保证「ST 193」点下去真能筛出 193 行 */
  scope: { tr: Record<Trend, number>; rk: Record<RiskFlag, number>; total: number }
  narrow: Narrow
  onTrend: (t: Trend) => void
  onRisk: (r: RiskFlag) => void
}) {
  const emo = focus.emotion
  const tr = scope.tr
  const rk = scope.rk
  const scoped = summary.total > 0 && scope.total !== summary.total
  const tone = emo.available ? (emo.state ?? 'amber') : null
  const dot = tone === 'green' ? 'bg-emerald-500' : tone === 'red' ? 'bg-red-500' : 'bg-amber-500'
  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface/50" aria-label="今日状态">
      <div className="grid grid-cols-1 divide-y divide-border/40 lg:grid-cols-[1.4fr_1fr_1.15fr] lg:divide-x lg:divide-y-0">
        {/* 情绪：状态本身不上红绿底色，避免和涨跌色混淆；用点 + 文字 */}
        <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 px-3 py-2">
          {emo.available ? (
            <>
              <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-foreground"
                title={emo.hint}>
                <span className={cn('h-2 w-2 rounded-full', dot)} />
                情绪 · {emo.label}
              </span>
              <span className="font-mono text-[11px] tabular-nums text-secondary"
                title="今日连板晋级率在近 120 个交易日里的分位；先看分位，再看绝对值">
                近 {emo.window_days ?? 0} 日分位 {((emo.pct ?? 0) * 100).toFixed(0)}%
              </span>
              <span className="text-[10px] text-muted">
                晋级率 {fmtPct(emo.upgrade_rate ?? 0, 1)}
              </span>
              <span className="text-[9.5px] text-muted">
                今日涨停 {emo.n_lu_today ?? 0}（连板 {emo.n_lianban_today ?? 0}）
              </span>
            </>
          ) : (
            <span className="text-[10px] text-muted">情绪闸门不可用（交易日不足 30 个）</span>
          )}
        </div>

        {/* 变化：点击 = 只看这批票 */}
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 px-3 py-2">
          <span className="mr-0.5 text-[9px] tracking-wider text-muted/60">
            今日变化{scoped ? '（本标签）' : ''}
          </span>
          <TinyToggle active={narrow.trend === 'up'} onClick={() => onTrend('up')}
            title="位次较前一交易日上升（流动性位次，不是涨跌）">
            <ArrowUp className="h-2.5 w-2.5" />上移 {tr.up.toLocaleString()}
          </TinyToggle>
          <TinyToggle active={narrow.trend === 'down'} onClick={() => onTrend('down')}
            title="位次较前一交易日下降">
            <ArrowDown className="h-2.5 w-2.5" />下移 {tr.down.toLocaleString()}
          </TinyToggle>
          <TinyToggle active={narrow.trend === 'new'} onClick={() => onTrend('new')}
            title="前一交易日无此票（新上市或数据恢复）">
            新进 {tr.new.toLocaleString()}
          </TinyToggle>
        </div>

        {/* 风险：前两个可筛明细；无成交 ST 不在表里，只作提示 */}
        <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 px-3 py-2">
          <span className="mr-0.5 text-[9px] tracking-wider text-muted/60">
            风险{scoped ? '（本标签）' : ''}
          </span>
          <TinyToggle active={narrow.risk === 'high'} onClick={() => onRisk('high')}
            title="ST / *ST（表格里带「ST」标）">
            ST {rk.high.toLocaleString()}
          </TinyToggle>
          <TinyToggle active={narrow.risk === 'watch'} onClick={() => onRisk('watch')}
            title="流通市值 < 10 亿，或成交额 < 2000 万">
            警示 {rk.watch.toLocaleString()}
          </TinyToggle>
          {summary.risk_absent > 0 && (
            <span className="text-[9.5px] text-amber-700/90"
              title="全量 ST 名单中当日无成交（停牌/退市整理）的票：不在下方表格里，持仓者需单独核对">
              全市场另有 {summary.risk_absent.toLocaleString()} 只 ST 今日无成交
            </span>
          )}
        </div>
      </div>
    </section>
  )
}

/** 漏斗的一格：数字 + 名字 + 「这一步筛掉了谁」。 */
function FunnelStep({
  n, label, drop, last,
}: {
  n: number
  label: string
  drop: string
  last?: boolean
}) {
  return (
    <div className={cn('px-3 py-2', last ? 'bg-accent/[0.045]' : 'bg-surface/40')}>
      <div className="flex items-baseline gap-1.5">
        <span className={cn('font-mono text-[18px] leading-none tabular-nums',
          last ? 'text-foreground' : 'text-secondary')}>
          {n.toLocaleString()}
        </span>
        <span className={cn('text-[10px]', last ? 'font-medium text-foreground' : 'text-muted')}>{label}</span>
      </div>
      <div className="mt-1 min-h-[26px] text-[9px] leading-[1.35] text-muted/75">{drop}</div>
    </div>
  )
}

/**
 * 今日观察池 —— 第一屏的主体。
 *
 * 15 张卡片本身只占三分之一高度，其余全是「为什么是它们」与「为什么不是别人」。
 * 这一版把解释压成三步漏斗（每步自己闭口），卡片去掉逐张重复的主场/阶段 chip，
 * 席位与热度口径默认收起 —— 展开是选择，不是默认。
 */
function FocusBand({
  focus, asOf, heatCut, topN, onOpenStock, onOpenMethod,
}: {
  focus: ShortlineRadarFocus
  asOf: string | null
  heatCut: number
  topN: number
  onOpenStock: (s: string) => void
  onOpenMethod: () => void
}) {
  const [seatsOpen, setSeatsOpen] = useState(false)
  const b = focus.budget
  const ex = focus.excluded

  // 每一步的删除量必须落在同一批票上：struct − cold = non_cold + 五道逻辑否定。
  // 后端保证这六项互斥且覆盖；旧版把全市场阶段计数（正在炒 763 等）混进这一步，
  // 三项加起来比该步删掉的还多。
  const stageDrop = ex.non_cold
  const stageDropText = [
    stageDrop > 0 && `非未启动 ${stageDrop.toLocaleString()}`,
    ex.falling > 0 && `下跌中继 ${ex.falling}`,
    ex.bse > 0 && `北交所 ${ex.bse}`,
    ex.young > 0 && `次新 ${ex.young}`,
    ex.st > 0 && `ST ${ex.st}`,
    ex.thin > 0 && `成交额不足 ${ex.thin}`,
  ].filter(Boolean).join(' · ') || '无'

  const picks = focus.picks
  const arenaCount = useMemo(() => {
    const m = new Map<ShortlineRadarArena, number>()
    for (const p of picks) m.set(p.arena, (m.get(p.arena) ?? 0) + 1)
    return [...m.entries()].sort((a, c) => c[1] - a[1])
  }, [picks])
  const dominantArena = arenaCount[0]?.[0] ?? null
  const arenaText = arenaCount
    .map(([a, n]) => `${ARENA_META[a].short} ${n}`)
    .join(' / ')

  const allCold = picks.length > 0 && picks.every(p => p.stage === 'cold')
  // 席位数据盘后才有，as_of 当天往往还拿不到；把披露日写在标题上，避免「今日」被读成 as_of。
  const seatDate = focus.seat_backing.reduce((m, s) => (s.date > m ? s.date : m), '')
  const seatLag = seatDate && asOf
    ? Math.max(0, Math.round((Date.parse(asOf) - Date.parse(seatDate)) / 86400000))
    : 0

  return (
    <section className="overflow-hidden rounded-card border border-emerald-500/20 bg-emerald-500/[0.02]">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-4 pb-2 pt-2.5">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h2 className="text-[13px] font-medium text-foreground">今日观察池</h2>
          <span className="text-[10px] text-muted">
            注意力前 15 · 不是买入名单 · {asOf ?? '—'}
          </span>
        </div>
        {picks.length > 0 && (
          <span className="text-[9.5px] text-muted/80">
            {allCold ? '全部未启动' : '含非未启动'} · 主场 {arenaText}
          </span>
        )}
      </div>

      <SortAuditBanner audit={focus.sort_audit} onOpen={onOpenMethod} />

      {/* 漏斗：四步各自闭口 */}
      <div className="mx-4 mb-2 grid grid-cols-2 gap-px overflow-hidden rounded-btn border border-border/60 bg-border/40 sm:grid-cols-4">
        <FunnelStep n={b.market} label="全市场"
          drop={`热度分 < ${heatCut} 的 ${Math.max(0, b.market - b.struct).toLocaleString()} 只`} />
        <FunnelStep n={b.struct} label="热度达标" drop={stageDropText} />
        <FunnelStep n={b.cold} label="未启动"
          drop={b.cold > b.picks ? `按热度取前 ${topN}，其余 ${(b.cold - b.picks).toLocaleString()} 只` : `共 ${b.cold} 只，全部入选`} />
        <FunnelStep n={b.picks} label="观察池" last
          drop={b.picks > 0 ? '同质但容量不足：只看不买' : '今天没有值得盯的位置'} />
      </div>

      {picks.length === 0 ? (
        <div className="border-t border-emerald-500/10 px-4 py-6 text-center">
          <div className="text-[12px] text-foreground">今日没有票同时满足「热度达标 + 未启动 + 可退出」</div>
          <p className="mt-1 text-[10px] text-muted/70">空仓也是一个结论：漏斗空说明今天没有值得盯的位置。</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-1.5 px-2.5 pb-2.5 sm:grid-cols-3 lg:grid-cols-5">
          {picks.map(p => (
            <button
              key={p.symbol}
              type="button"
              onClick={() => onOpenStock(p.symbol)}
              className="rounded-btn border border-border/60 bg-surface/70 px-2 py-1.5 text-left transition-colors hover:border-emerald-500/40 hover:bg-emerald-500/[0.04]"
            >
              <div className="flex items-baseline justify-between gap-1">
                <span className="truncate text-[11.5px] font-medium text-foreground">{p.name ?? p.symbol}</span>
                <span className="shrink-0 font-mono text-[9px] text-muted/60">{p.symbol.split('.')[0]}</span>
              </div>
              <div className="mt-0.5 flex items-baseline justify-between gap-1 text-[9.5px] text-muted">
                <span className="truncate">{p.industry ?? '未分类'}</span>
                <span className="shrink-0 font-mono tabular-nums text-secondary">{fmtBigNum(p.amount)}</span>
              </div>
              <div className="mt-1 flex items-baseline gap-2 font-mono text-[10px] tabular-nums text-secondary">
                <span title="20 日均振幅">振 {p.amp20 == null ? '—' : fmtPct(p.amp20, 1)}</span>
                <span title="20 日换手中位">换 {p.turn_med.toFixed(1)}%</span>
                {/* 15 只里 14 只同主场，逐张写「游资」没有信息量；只标例外 */}
                {p.arena !== dominantArena && (
                  <span className="ml-auto flex items-center gap-0.5 text-[9px] text-muted">
                    <span className={cn('h-1.5 w-1.5 rounded-full', ARENA_META[p.arena].dot)} />
                    {ARENA_META[p.arena].short}
                  </span>
                )}
              </div>
              {p.reasons.length > 0 && (
                <div className="mt-0.5 truncate text-[9px] text-muted/60" title={p.reasons.join(' · ')}>
                  {p.reasons.slice(0, 2).join(' · ')}
                </div>
              )}
            </button>
          ))}
        </div>
      )}

      {focus.seat_backing.length > 0 && (
        <div className="border-t border-border/50">
          <button
            type="button"
            aria-expanded={seatsOpen}
            onClick={() => setSeatsOpen(v => !v)}
            className="flex w-full items-center gap-2 px-4 py-1.5 text-left transition-colors hover:bg-elevated/30"
          >
            <span className="text-[10px] font-medium text-foreground">席位背书 {focus.seat_backing.length}</span>
            <span className={cn('text-[10px]', seatLag > 0 ? 'text-amber-700/90 dark:text-amber-400/90' : 'text-muted/70')}
              title={seatDate ? `席位数据日期 ${seatDate}；as_of ${asOf ?? '—'}` : undefined}>
              {seatLag > 0 ? `最近披露 ${seatDate} · 非当日（滞后 ${seatLag} 天）` : '今日被高声誉席位净买入'}
              {' · '}与池正交 ·「谁在买」，不是「还没动」
            </span>
            <ChevronDown className={cn('ml-auto h-3 w-3 shrink-0 text-muted/60 transition-transform', seatsOpen && 'rotate-180')} />
          </button>
          {seatsOpen && (
            <div className="grid grid-cols-1 gap-1.5 px-2.5 pb-2.5 sm:grid-cols-2 lg:grid-cols-3">
              {focus.seat_backing.map(s => (
                <button
                  key={s.symbol}
                  type="button"
                  onClick={() => onOpenStock(s.symbol)}
                  className="rounded-btn border border-amber-500/20 bg-amber-500/[0.03] px-2 py-1.5 text-left transition-colors hover:border-amber-500/40"
                >
                  <div className="flex items-baseline justify-between gap-1">
                    <span className="truncate text-[11px] font-medium text-foreground">{s.name ?? s.symbol}</span>
                    <span className="shrink-0 font-mono text-[9px] text-muted/60">{s.symbol.split('.')[0]}</span>
                  </div>
                  <div className="mt-0.5 truncate text-[9px] text-muted/75" title={s.seat_name}>{s.seat_name}</div>
                  <div className="mt-0.5 flex items-baseline gap-2 font-mono text-[9px] tabular-nums text-secondary">
                    <span title="该席位过去 20 次上榜买入的平均未来 3 日收益；是席位的历史，不是本票的预期">
                      席位近20次 +{(s.rep20 * 100).toFixed(1)}%
                    </span>
                    <span className="text-muted/70">净买 {fmtBigNum(s.net ?? 0)}</span>
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-emerald-500/10 px-4 py-1.5 text-[9.5px] leading-relaxed text-muted/70">
        <span>热度分 = 振幅 / 换手 / 市值反向 / 成交额 四项分位等权，只排序，不是上涨概率。</span>
        <button type="button" onClick={onOpenMethod}
          className="text-accent underline underline-offset-2 hover:text-foreground">
          完整口径与实测
        </button>
      </div>
    </section>
  )
}

/** 前向读数的符号格式：统一用 Unicode 负号，避免和连字符混读。 */
const sbp = (v: number) => `${v > 0 ? '+' : '−'}${Math.abs(v).toFixed(1)}bp`
const st = (v: number) => `${v > 0 ? '+' : '−'}${Math.abs(v).toFixed(2)}`

/**
 * 跨年妖候选池 —— 观察池之外的另一块，独立口径，实时计算（数据来自后端 focus.cross_year）。
 *
 * 把跨年妖股的 8 条历史画像翻译成 4 个硬门槛 + 1 个排序 + 1 个徽标，实时筛出候选，分两梯队：
 *   第一梯队 = 位置干净（ret60 < 30%，前期没怎么涨、最近才启动）
 *   第二梯队 = 位置偏高（ret60 ≥ 30%，已涨过一轮）
 *
 * 措辞纪律：标题是「候选池」，不是「买入名单」。项目 20 年全样本（docs/yaogu-census.md）：
 * 首板后开盘买入 20 日超额中位 −1.85%（t −15.94），见顶确认时买入 −26.9%/20 日。
 * 这是情绪温度计，不是选股筛子。
 */
function CrossYearCandidates({
  watch, hotCount, onOpenStock, onOpenMethod,
}: {
  watch: CrossYearWatch | undefined
  /** 全市场「正在炒」（20 日内有涨停）的票数：情绪温度计读数，与候选池合并构成完整温度。 */
  hotCount: number
  onOpenStock: (s: string) => void
  onOpenMethod: () => void
}) {
  const [showTier2, setShowTier2] = useState(false)
  const [showAllTier1, setShowAllTier1] = useState(false)
  if (!watch) return null
  const { tier1, tier2, budget, criteria } = watch
  const TIER1_PREVIEW = 12
  const shownTier1 = showAllTier1 ? tier1 : tier1.slice(0, TIER1_PREVIEW)

  // 情绪温度：候选数 / 全市场正在炒数 = 跨年妖候选在当前热度里的密度。
  const density = hotCount > 0 ? budget.candidates / hotCount : 0

  return (
    <section className="overflow-hidden rounded-card border border-purple-500/25 bg-purple-500/[0.02]">
      {/* 头部：标题 + 情绪温度计 */}
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 px-4 pb-1.5 pt-2.5">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h2 className="text-[13px] font-medium text-foreground">跨年妖温度计</h2>
          <span className="text-[10px] text-muted">
            8 画像 → 4 门槛 + 1 排序 + 1 徽标 · 实时计算
          </span>
        </div>
        <span className="flex items-center gap-1.5 text-[9.5px] tabular-nums text-muted/80">
          <span title="全市场 20 日内有涨停的票数：情绪温度（密度是退潮温度计，候选是热度样本）">
            市场温度 <b className="text-foreground/80">{hotCount.toLocaleString()}</b> 只正在炒
          </span>
          <span className="text-muted/40">·</span>
          <span title={`形态命中占正在炒的比例 ${(density * 100).toFixed(1)}%`}>
            命中 <b className="text-foreground/80">{budget.candidates}</b> 只
          </span>
        </span>
      </div>

      {/* 口径：六个可量化条件（阈值来自后端 criteria） */}
      <div className="mx-4 mb-1.5 flex flex-wrap gap-1">
        {CROSS_YEAR_SCREEN.map(s => (
          <span
            key={s.label}
            className="rounded-btn border border-purple-500/20 bg-purple-500/[0.04] px-1.5 py-0.5 text-[9px] text-muted/80"
            title={`${s.label}：${s.hint}`}
          >
            <span className="text-muted/45">{s.kind === 'gate' ? '门槛' : s.kind === 'rank' ? '排序' : '徽标'} · </span>
            <span className="text-foreground/70">{s.label}</span>
            <span className="ml-1 text-muted/60">{s.hint}</span>
          </span>
        ))}
      </div>

      {/* 前向检验：候选池整体为负，同门槛未启动为正 */}
      <div className="mx-4 mb-1.5 rounded-btn border border-red-500/25 bg-red-500/[0.05] px-2.5 py-1.5 text-[10px] leading-relaxed text-red-700/90 dark:text-red-400/90">
        历史读数（{CROSS_YEAR_STATS.window}，{CROSS_YEAR_STATS.months} 月）：
        全池 <b>{sbp(CROSS_YEAR_STATS.all.ex20)}</b>/20日（t {st(CROSS_YEAR_STATS.all.t20)}）·
        同门槛「未启动」对照 <b>{sbp(CROSS_YEAR_STATS.ctrl.ex20)}</b>（t {st(CROSS_YEAR_STATS.ctrl.t20)}）。
        <b>「已启动」跑输「未启动」；这是温度计，不是候选名单。</b>
      </div>

      {/* 第一梯队：位置干净 */}
      <div className="border-t border-purple-500/10">
        <div className="flex items-baseline gap-2 px-4 pb-1 pt-2">
          <span className="text-[11px] font-medium text-foreground">第一梯队 · 位置干净</span>
          <span className="text-[9.5px] text-muted/70">
            60 日涨幅 &lt; {Math.round(criteria.ret60_clean * 100)}%，最近才启动 · {tier1.length} 只 ·
            历史 {sbp(CROSS_YEAR_STATS.tier1.ex20)}/20日
          </span>
        </div>
        <div className="grid grid-cols-2 gap-1.5 px-2.5 pb-2 sm:grid-cols-3 lg:grid-cols-4">
          {shownTier1.map(c => <CandidateCard key={c.symbol} c={c} onOpen={onOpenStock} />)}
        </div>
        {tier1.length > TIER1_PREVIEW && (
          <div className="px-2.5 pb-2">
            <button type="button" onClick={() => setShowAllTier1(v => !v)}
              className="w-full rounded-btn border border-purple-500/20 bg-purple-500/[0.03] px-2 py-1 text-[10px] text-muted hover:text-foreground">
              {showAllTier1 ? `收起，只看前 ${TIER1_PREVIEW} 只` : `展开全部 ${tier1.length} 只（按 net_gain 排序，只用于浏览）`}
            </button>
          </div>
        )}
      </div>

      {/* 第二梯队：位置偏高，默认收起 */}
      <div className="border-t border-purple-500/10">
        <button
          type="button"
          aria-expanded={showTier2}
          onClick={() => setShowTier2(v => !v)}
          className="flex w-full items-center gap-2 px-4 py-1.5 text-left transition-colors hover:bg-purple-500/[0.03]"
        >
          <span className="text-[11px] font-medium text-foreground">第二梯队 · 位置偏高</span>
          <span className="text-[9.5px] text-muted/70">
            默认收起 · 60 日已涨过一轮 · {tier2.length} 只 ·
            历史 {sbp(CROSS_YEAR_STATS.tier2.ex20)}/20日（t {st(CROSS_YEAR_STATS.tier2.t20)}）
          </span>
          <ChevronDown className={cn('ml-auto h-3 w-3 shrink-0 text-muted/60 transition-transform', showTier2 && 'rotate-180')} />
        </button>
        {showTier2 && (
          <div className="grid grid-cols-2 gap-1.5 px-2.5 pb-2 sm:grid-cols-3 lg:grid-cols-4">
            {tier2.map(c => <CandidateCard key={c.symbol} c={c} onOpen={onOpenStock} />)}
          </div>
        )}
      </div>

      {/* 底部：诚实说明 */}
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-t border-purple-500/10 px-4 py-1.5 text-[9px] leading-relaxed text-muted/70">
        <span>
          温度计，不是候选名单：net_gain top15 更差 {sbp(CROSS_YEAR_STATS.top15.ex20)}/20日
          （t {st(CROSS_YEAR_STATS.top15.t20)}）；项目 20 年全样本里，
          首板后开盘买入 20 日超额中位 −1.85%（t −15.94）。
        </span>
        <button type="button" onClick={onOpenMethod}
          className="text-accent underline underline-offset-2 hover:text-foreground">
          完整口径
        </button>
      </div>
    </section>
  )
}

/** 单只候选卡片：点击弹日 K。涨幅涨红跌绿。 */
function CandidateCard({ c, onOpen }: { c: CrossYearCandidate; onOpen: (s: string) => void }) {
  const chg = (v: number | null) => v == null ? '—' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(0)}%`
  const chgTone = (v: number | null) => v == null ? 'text-muted' : v > 0 ? 'text-red-600' : v < 0 ? 'text-emerald-600' : 'text-muted'
  return (
    <button
      type="button"
      onClick={() => onOpen(c.symbol)}
      className="rounded-btn border border-border/60 bg-surface/70 px-2 py-1.5 text-left transition-colors hover:border-purple-500/40 hover:bg-purple-500/[0.04]"
    >
      <div className="flex items-baseline justify-between gap-1">
        <span className="flex min-w-0 items-baseline gap-1">
          <span className="truncate text-[11.5px] font-medium text-foreground">{c.name}</span>
          {c.mem && (
            <span className="shrink-0 rounded border border-purple-500/25 bg-purple-500/[0.08] px-1 text-[8.5px] leading-[1.4] text-purple-600">
              记忆点
            </span>
          )}
        </span>
        <span className="shrink-0 font-mono text-[9px] text-muted/60">{c.code}</span>
      </div>

      <div className="mt-0.5 truncate text-[9px] text-muted/70">{c.industry ?? '未分类'}</div>

      <div className="mt-1 flex items-baseline justify-between font-mono text-[10px] tabular-nums">
        <span className="text-foreground">¥{c.price.toFixed(2)}</span>
        <span className="text-muted/70">流通 {fmtBigNum(c.float_cap)}</span>
      </div>

      <div className="mt-0.5 flex items-baseline gap-2 font-mono text-[9.5px] tabular-nums">
        <span title="20 日涨幅" className={chgTone(c.ret20)}>20日 {chg(c.ret20)}</span>
        <span title="60 日涨幅（负/低 = 位置干净）" className={chgTone(c.ret60)}>60日 {chg(c.ret60)}</span>
        <span className="ml-auto text-muted/70" title="近 20 日涨停天数">涨停 {c.lu20} 天</span>
      </div>
    </button>
  )
}

/**
 * 排序体检横幅：放在观察池头，而不是 9px 脚注。
 *
 * 页面自己的体检说现行 heat 排序跑输池内随机 15 只（回填 −140.9bp / t −3.02），
 * 而 15 张卡仍按 heat 排。这两件事必须同屏：卡片回答「谁在动」，横幅回答「按收益它不好」。
 * 判据冻结在 docs/structural-edge/prereg-heat-sort.md，评估日由人执行；这里只报状态，不裁决。
 */
function SortAuditBanner({ audit, onOpen }: { audit: SortAudit | null | undefined; onOpen: () => void }) {
  if (!audit) {
    return (
      <button type="button" onClick={onOpen}
        className="mx-4 mb-2 text-left text-[10px] text-muted/70 underline underline-offset-2 hover:text-foreground">
        排序体检未生成
      </button>
    )
  }
  const hasFwd = (audit.forward?.months ?? 0) > 0
  const w = hasFwd ? audit.forward! : audit.all
  const months = audit.forward?.months ?? 0
  const pending = months < audit.gate.min_months
  const negative = (w.cur_lift_bp ?? 0) < 0
  const bp = (v: number | null | undefined) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}bp`)
  return (
    <button type="button" onClick={onOpen}
      className={cn(
        'mx-4 mb-2 flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-btn border px-3 py-1.5 text-left transition-colors hover:bg-elevated/30',
        negative ? 'border-amber-500/35 bg-amber-500/[0.06]' : 'border-border/60 bg-surface/40',
      )}
      title="现行排序相对同日池内随机 15 只的前向读数；判据见 prereg-heat-sort.md">
      <span className={cn('text-[12px] font-medium', negative ? 'text-amber-700 dark:text-amber-400' : 'text-secondary')}>
        排序体检 · 现行排序{negative ? '跑输' : '跑赢'}池内随机 15 只 {bp(w.cur_lift_bp)}
      </span>
      <span className="font-mono text-[10.5px] tabular-nums text-muted">
        t {w.cur_t?.toFixed(2) ?? '—'} · 候选 {bp(w.shadow_lift_bp)} · 配对 {bp(w.paired_delta_bp)}
      </span>
      <span className="text-[10px] text-muted/70">
        {hasFwd ? `前向 ${months}/${audit.gate.min_months} 月` : `回填 ${audit.backfill_days} 天（样本内）`}
        {pending ? ' · 只看不判' : ''}
      </span>
      <span className="ml-auto text-[10px] text-accent">口径 →</span>
    </button>
  )
}

/** 接口状态的文案。`ready` 是唯一「可以照着挂单」的状态，其余三种是「为什么今天没有清单」。 */
const LU_STATUS_LABEL: Record<DecisionLuStatus, { label: string; tone: string }> = {
  ready: { label: '清单就绪', tone: 'bg-accent/10 text-accent' },
  empty: { label: '当日 0 只', tone: 'bg-elevated text-muted' },
  skipped_not_closed: { label: '数据未收盘', tone: 'bg-amber-500/10 text-amber-700' },
  skipped_no_data: { label: '无行情分区', tone: 'bg-red-500/10 text-red-600' },
}

/** 买点卡里的一格读数。与观察池的 FunnelStep 不同：这里只报「这笔交易的规格」。 */
function Metric({ k, v, hint }: { k: string; v: string; hint?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-[9px] tracking-wide text-muted/70">{k}</div>
      <div className="mt-0.5 font-mono text-[13px] font-medium leading-none tabular-nums text-foreground">{v}</div>
      {hint && <div className="mt-1 text-[9px] leading-[1.4] text-muted/75">{hint}</div>}
    </div>
  )
}

/**
 * 明日买点 —— 全页唯一可执行的买点，形状与「机会雷达」的候选卡同一件事：
 * 明天开盘做什么、按什么价、亏多少、什么时候必须走。
 *
 * 口径唯一产地是 `backend/app/decision/lu_rebound.py`：本组件不重算信号，
 * 只读 `/api/decision/lu-rebound/candidates`，再补上账本面板没有的三件事 ——
 * 代价（p10 盘中回撤）、成本（毛 → 净）、仓位刻度（沿用手册六格，非本腿规则）。
 *
 * 为什么它可以放在这一页：事件在 T+1 收盘就已定稿（T+2 开盘才成交），
 * 不需要盘中盯 5%。旧版「本页不提供触发清单」把「盘中触发」和「收盘后清单」
 * 混为一谈，于是把唯一有正期望的动作挡在了页外。
 */
function LuBuyPoint({
  data, failed, error, onOpenStock, onOpenMethod,
}: {
  data: DecisionLuCandidates | undefined
  failed: boolean
  error?: string
  onOpenStock: (s: string) => void
  onOpenMethod: () => void
}) {
  const rows = data?.rows ?? []
  const ready = data?.status === 'ready' || data?.status === 'empty'
  const n = ready ? rows.length : 0
  const entry = data?.entry_date ?? null
  // 「次一交易日」与「2026-11-23」只差一个空格，但读起来是两件事：前者是未知，后者是已知。
  const entryLabel = entry ? `${entry} 开盘` : '次一交易日开盘'
  // 仓位不是本腿的规则：只有「若沿用手册六格」的换算，输入只有 n 与 p10。
  const slotPct = n > 0 ? 100 / 6 / n : 0
  const worstPct = slotPct * 0.068
  /** 页内负号统一 U+2212；`fmtPct` 给的是 ASCII 连字符。 */
  const signedPct = (v: number) => `${v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(2)}%`

  return (
    <section
      data-testid="lu-buy-point"
      aria-label="明日买点"
      className="overflow-hidden rounded-card border border-accent/25 bg-accent/[0.03]"
    >
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-4 pb-1 pt-2.5">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h2 className="text-[13px] font-medium text-foreground">明日买点</h2>
          <span className="rounded bg-accent/10 px-1.5 py-0.5 text-[9px] font-medium tracking-wide text-accent">唯一</span>
          <span className="text-[10px] text-muted">被砸的换手板 · 不赌形态，赌时点错配</span>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[9.5px] text-muted">
          {data?.as_of && <span className="font-mono">{data.as_of} 收盘定稿</span>}
          {data && (
            <span className={cn('rounded px-1.5 py-0.5', LU_STATUS_LABEL[data.status].tone)}>
              {LU_STATUS_LABEL[data.status].label}
            </span>
          )}
        </div>
      </div>

      {!data ? (
        <p className="px-4 pb-3 pt-1 text-[11px] text-muted">
          {failed
            ? `买点清单读不到${error ? `（${error}）` : ''}；下方观察池不受影响。`
            : '正在读明日挂单…'}
        </p>
      ) : !ready ? (
        <div className="px-4 pb-3 pt-1">
          <div className="text-[13px] text-foreground">这份清单没有成立</div>
          <p className="mt-1 max-w-[60em] text-[11px] leading-relaxed text-muted">
            {data.reason ?? data.warnings[0] ?? '没有记录原因——这本身是缺陷'}
          </p>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-end gap-x-4 gap-y-1 px-4">
            <span className={cn(
              'font-mono text-[40px] font-semibold leading-none tracking-[-0.03em] tabular-nums',
              n > 0 ? 'text-accent' : 'text-muted/40',
            )}>
              {n}
            </span>
            <div className="pb-0.5">
              <div className="text-[13px] font-medium text-foreground">
                {n > 0 ? '只挂单' : '只 · 次一交易日无买入'}
              </div>
              <div className="mt-0.5 text-[10.5px] text-secondary">
                {n > 0
                  ? `${entryLabel} ≤ 挂单限价才成交 · 当日收盘卖 · 不留隔夜`
                  : '全市场没有被砸的换手板 —— 0 只是结论，不是管道没跑'}
                {data.truncated > 0 && ` · 另有 ${data.truncated} 只被容量截断`}
              </div>
            </div>
          </div>

          {n > 0 && (
            <>
              <div className="mx-4 mt-2.5 max-h-[320px] overflow-auto rounded-btn border border-border/60 bg-surface/50">
                <table className="w-full min-w-[330px] text-left text-[11px]">
                  <thead className="sticky top-0 z-[1] bg-surface text-muted">
                    <tr className="border-b border-border/60">
                      <th className="w-7 px-2 py-1.5 font-medium">#</th>
                      <th className="px-2 py-1.5 font-medium">名称 / 代码</th>
                      <th className="w-[80px] px-2 py-1.5 text-right font-medium sm:w-[92px]" title="T+1 日内 open→close，不是隔夜跌幅">T+1 日内</th>
                      <th className="w-[80px] px-2 py-1.5 text-right font-medium sm:w-[92px]" title="涨停价的下一分：开盘 ≤ 它才成交（= 开盘没封涨停）">挂单限价</th>
                      <th className="hidden w-[96px] px-2 py-1.5 text-right font-medium sm:table-cell" title="T+1 成交额；截断到 20/40/60/100 只时逐日超额不变，所以它是容量约束">T+1 成交额</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map(r => (
                      <tr key={r.symbol} className="border-b border-border/40 last:border-0">
                        <td className="px-2 py-1.5 font-mono text-muted">{r.rank}</td>
                        <td className="px-2 py-1.5">
                          <button
                            type="button"
                            onClick={() => onOpenStock(r.symbol)}
                            className="text-left text-foreground underline decoration-dotted underline-offset-2 hover:text-accent"
                          >
                            {r.name ?? r.symbol}
                          </button>
                          <span className="ml-1.5 font-mono text-[9.5px] text-muted">{r.symbol}</span>
                        </td>
                        {/* 跌幅保持中性色：这是入选条件，不是看空信号。染绿会让
                            「跌得越多越该买」读成「跌得越多越危险」。 */}
                        <td className="px-2 py-1.5 text-right font-mono tabular-nums text-secondary">
                          {signedPct(r.drop_pct / 100)}
                        </td>
                        <td className="px-2 py-1.5 text-right font-mono font-medium tabular-nums text-foreground">
                          {r.limit_price.toFixed(2)}
                        </td>
                        <td className="hidden px-2 py-1.5 text-right font-mono tabular-nums text-muted sm:table-cell">
                          {r.day_amount_yi == null ? '—' : `${r.day_amount_yi.toFixed(1)} 亿`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="mx-4 mt-2.5 grid grid-cols-2 gap-x-4 gap-y-2 border-t border-accent/15 pt-2 lg:grid-cols-4">
                <Metric
                  k="日超额（毛）" v={`${LU_STUDY.excessPct}/日`}
                  hint={`t ${LU_STUDY.t} · ${LU_STUDY.window} ${LU_STUDY.years} 年正 · 样本前 ${LU_STUDY.preYears} 年正`}
                />
                <Metric
                  k="扣成本（双边 15bp）" v={`+${LU_STUDY.netBp}bp`}
                  hint="佣金 5 + 印花 5（卖）+ 滑点 5；未计冲击成本"
                />
                <Metric
                  k="代价" v={`p10 ${LU_STUDY.mddP10}`}
                  hint={`T+2 开盘后日内最低点：中位 ${LU_STUDY.mddMedian}、${LU_STUDY.breach5} 盘中破 −5%`}
                />
                <Metric
                  k="成交率" v={LU_STUDY.fillRate}
                  hint="开盘 ≤ 挂单限价才成交；开盘封涨停就买不到，不追"
                />
              </div>

              <p className="mt-1.5 px-4 text-[9.5px] leading-relaxed text-muted">
                仓位（<b className="text-foreground/80">若沿用手册六格</b>）：一格 = 权益/6，均分 {n} 只 ⇒
                单只 {slotPct.toFixed(1)}% 权益；按 p10 {LU_STUDY.mddP10} 计，单只最坏 ≈ {worstPct.toFixed(2)}% 权益。
                本腿未单独验证仓位规则，别把这一行当成本腿的结论。
              </p>
            </>
          )}

          <details className="mt-2 border-t border-accent/15 px-4 py-1.5 text-[10px] leading-relaxed text-secondary">
            <summary className="cursor-pointer select-none text-[10px] text-muted hover:text-foreground">
              为什么只有这一个买点
            </summary>
            <div className="mt-1.5 space-y-1.5">
              <p>
                它赚的不是形态，是时点：T+1 交割 + 次日开盘出货，卖压必须在 T+1 卖完；
                T+2 买盘回来，日内修复。所以它不需要排队、不需要盘感，只需要 T+1 的收盘数据。
                「T 日封板 → T+1 开盘卖」2026 年名义上 +1.45%，但要在涨停板上排队，成交率不可测 ——
                那不叫可执行，所以这里说「唯一」。
              </p>
              <p>
                与筹码页同一个问题 —— <b className="text-foreground/80">谁必须在某个时点卖出</b>。
                筹码页的答案是贴近平仓线的大股东，这里的答案是 T+1 开盘要出货的资金；
                两处赚的都是被迫卖出者的价格让步，不是预测。
              </p>
              <p>
                因此 −5% 是<b className="text-foreground/80">触发条件</b>，不是排序键：这条腿不按「跌得深浅」选股，
                只按成交额取容量。真正的约束只有三条：可成交（开盘没封板）、容量（成交额前 40）、
                制度（T+1、涨跌停、ST/次新）。它也不能推广成「跌 5% 就买」—— 同样的 T+1 日内跌幅，
                没有涨停的票只有 +0.05%（<code className="font-mono text-[9px]">_diag_board_shape.py</code> 的同跌幅对照），溢价来自
                「涨停 + 次日砸盘」这个组合。
              </p>
              <p>
                它是右尾交易：命中中位 52.7%，单笔中位很薄，均值靠分布右尾 ——
                需要足够多的独立交易日，不能单票重仓。
              </p>
              <ul className="list-disc space-y-0.5 pl-4 text-[9.5px] text-muted">
                {LU_EXCLUSIONS.map(x => (
                  <li key={x.action}><span className="text-secondary">{x.action}</span> — {x.read}</li>
                ))}
              </ul>
            </div>
          </details>
        </>
      )}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-accent/15 px-4 py-1.5 text-[9.5px] text-muted">
        <span>口径：换手板（非一字）→ T+1 日内 ≤ −5% → 成交额前 40 → 开盘 ≤ 限价</span>
        <span className="hidden sm:inline">剔除 ST / 次新 / 一字板</span>
        <Link to="/limit-ladder" className="text-accent underline underline-offset-2 hover:text-foreground">
          今日封板 → 连板梯队（T+1 开盘卖）
        </Link>
        <Link to="/decisions" className="text-accent underline underline-offset-2 hover:text-foreground">
          账本与结算
        </Link>
        <button type="button" onClick={onOpenMethod}
          className="ml-auto text-accent underline underline-offset-2 hover:text-foreground">
          完整口径
        </button>
      </div>
      {data?.warnings?.length ? (
        <p className="border-t border-accent/10 px-4 py-1 text-[9px] text-muted/75">
          {data.warnings.slice(0, 2).join(' · ')}
        </p>
      ) : null}
    </section>
  )
}

/**
 * 标签内的主场分布 —— 只给只数与占比，不给 dd20/中位。
 *
 * 后端只提供「标签整体」与「主场整体」两套历史读数，没有 tag×arena 的交叉读数；
 * 在这一格上显示主场的历史左尾，会让人读成「这个标签在这个主场里的风险」。
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
      <div className="flex h-[34px] items-center border-t border-border/40 px-4 text-[10px] text-muted/60">
        该标签今日没有命中的票
      </div>
    )
  }
  // 单格 100% 的条形图不携带信息，退化成一句结论
  if (shown.length === 1) {
    const { arena, count } = shown[0]
    return (
      <div className="flex items-center gap-1.5 border-t border-border/40 px-4 py-2 text-[10px] text-muted">
        <span className={cn('h-1.5 w-1.5 rounded-full', ARENA_META[arena].dot)} />
        今日 {count.toLocaleString()} 只全部落在 <span className="text-secondary">{ARENA_META[arena].label}</span>
        <span className="ml-auto text-muted/50">该标签与主场在这一天重合</span>
      </div>
    )
  }
  return (
    <div className="flex h-[34px] w-full border-t border-border/40" role="group" aria-label="该标签的主场分布">
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
            <span className="hidden truncate text-[10px] text-secondary sm:inline">{meta.short}</span>
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
 * 标签页头 —— 只数、左尾、中位、主场分布。
 *
 * 三条口径必须写清楚，否则会被读成「蓝筹稳、龙头强」：
 *   1. 左尾/中位是 34 期 fwd20 的历史读数，不是预测；
 *   2. 白马没有 PIT 财报序列 ⇒ 没有历史读数，显示「无历史背书」而不是 0；
 *   3. 龙头是动量标签，左尾比大盘更高 —— 强不等于稳。
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
            <span className="text-[10px] text-muted">只 · 占全市场 {(summary.share * 100).toFixed(1)}%</span>
          </div>
          <p className="mt-1 max-w-none text-[10px] leading-relaxed text-muted/75 sm:max-w-[560px]">{summary.hint}</p>
        </div>
        <div className="flex w-full shrink-0 items-start justify-between gap-5 sm:w-auto sm:justify-start">
          <div>
            <div className="text-[9px] tracking-wider text-muted/60">历史左尾 · fwd20 跌 &gt;20%</div>
            <div className="mt-0.5 flex items-baseline gap-1.5">
              {dd === null ? (
                <span className="text-[11px] text-muted/50">无历史背书</span>
              ) : (
                <>
                  <span className="font-mono text-[15px] leading-none tabular-nums text-foreground">{fmtPct(dd)}</span>
                  <Delta value={dd} base={market.dd20} lowerIsBetter />
                </>
              )}
            </div>
          </div>
          <div>
            <div className="text-[9px] tracking-wider text-muted/60">fwd20 中位</div>
            <div className="mt-0.5 flex items-baseline gap-1.5">
              {med === null ? (
                <span className="text-[11px] text-muted/50">—</span>
              ) : (
                <>
                  <span className="font-mono text-[15px] leading-none tabular-nums text-foreground">{fmtPct(med)}</span>
                  <Delta value={med} base={market.med_ret20} lowerIsBetter={false} />
                </>
              )}
            </div>
          </div>
        </div>
      </div>

      <ArenaMix counts={arenaCounts} total={summary.count} active={activeArena} onPick={onPickArena} />

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/50 bg-elevated/20 px-4 py-1.5 text-[9.5px] leading-relaxed text-muted/75">
        <span>
          {!validated
            ? `无历史背书：该标签依赖财报${financialPeriod ? `（${financialPeriod}）` : ''}，只有单期数据，不做历史读数。`
            : riskier
              ? `比大盘更危险：左尾高 ${((dd - market.dd20) * 100).toFixed(2)}pp —— 动量/波动标签，强势会反转。`
              : `比大盘更安全：左尾低 ${((market.dd20 - dd) * 100).toFixed(2)}pp，但仍是历史读数，不是预测。`}
        </span>
        <span className="ml-auto">
          观察池与可执行规则是全市场口径，见
          <button type="button" onClick={onGoAll} className="ml-1 underline underline-offset-2 hover:text-foreground">全市场</button>
        </span>
      </div>
    </section>
  )
}

/** 明细工具条：排序 + 搜索 + 自选 + 当前筛选。结果计数并进来，不再单独一行。 */
function DetailToolbar({
  sortKey, onSort, narrow, onKeyword, onClear, onToggleArena, onToggleTrend, onToggleRisk,
  watchAvailable, watchOnly, onToggleWatch, watchCount, hit, base, activeTag, narrowing,
}: {
  sortKey: SortKey
  onSort: (k: SortKey) => void
  narrow: Narrow
  onKeyword: (v: string) => void
  onClear: () => void
  onToggleArena: () => void
  onToggleTrend: () => void
  onToggleRisk: () => void
  watchAvailable: boolean
  watchOnly: boolean
  onToggleWatch: () => void
  watchCount: number
  hit: number
  base: number
  activeTag: ShortlineRadarTag | null
  narrowing: boolean
}) {
  const chip = (label: string, onX: () => void) => (
    <span className="inline-flex items-center gap-1 rounded border border-accent/35 bg-accent/[0.07] px-1.5 py-px text-[10px] text-foreground">
      {label}
      <button type="button" onClick={onX} aria-label={`取消筛选 ${label}`} className="text-muted hover:text-foreground">
        <X className="h-2.5 w-2.5" />
      </button>
    </span>
  )
  return (
    <div className="rounded-card border border-border bg-surface/40 px-3 py-2">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
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

        {watchAvailable && (
          <TinyToggle active={watchOnly} onClick={onToggleWatch} title="只保留自选股；自选为空时不显示">
            只看自选 {watchCount > 0 ? watchCount.toLocaleString() : ''}
          </TinyToggle>
        )}

        {narrow.arena && chip(ARENA_META[narrow.arena].label, onToggleArena)}
        {narrow.trend && chip(
          narrow.trend === 'up' ? '位次上移' : narrow.trend === 'down' ? '位次下移' : '新进',
          onToggleTrend,
        )}
        {narrow.risk && chip(narrow.risk === 'high' ? 'ST' : '警示', onToggleRisk)}
        {narrow.keyword.trim() && chip(`搜索「${narrow.keyword.trim()}」`, () => onKeyword(''))}
        {narrowing && (
          <button type="button" onClick={onClear}
            className="text-[10px] text-muted/60 underline underline-offset-2 hover:text-foreground">
            清空筛选
          </button>
        )}

        {narrowing && (
          <span data-testid="radar-result" className="text-[10px] text-muted">
            {activeTag ? '本标签命中' : '全市场命中'}
            <span className="ml-1 font-mono text-[13px] tabular-nums text-foreground">{hit.toLocaleString()}</span>
            <span className="text-muted/50"> / {base.toLocaleString()} 只</span>
            {hit === 0 && <span className="ml-1 text-amber-600">· 这一屏没有命中</span>}
          </span>
        )}

        <div className="relative ml-auto">
          <Search className="pointer-events-none absolute left-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-muted/40" />
          <input
            value={narrow.keyword}
            onChange={e => onKeyword(e.target.value)}
            placeholder="代码 / 名称 / 行业"
            aria-label="搜索代码、名称或行业"
            className="w-40 rounded-btn border border-border bg-elevated py-0.5 pl-6 pr-5 text-[10px] text-secondary placeholder:text-muted/40 focus:outline-none focus:ring-1 focus:ring-accent/40"
          />
          {narrow.keyword && (
            <button type="button" onClick={() => onKeyword('')} aria-label="清空搜索"
              className="absolute right-1 top-1/2 -translate-y-1/2 text-muted/50 hover:text-foreground">
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

// ===== 单行 =====
// 只保留 7 列：删掉「风险」（87% 是「—」）与「变化」（90% 是「—」）两列，
// 风险改为名字后的标记，变化并入位次格里的箭头 —— 同一块屏幕少两列空白。
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
  // 标签只在标签页里出现：全市场视图不给逐行合并多标签的列 —— 那会把「视图」读成「分类」，
  // 也正是页面脚注反复在解释的那件事。
  const shownTags = activeTag ? r.tags.filter(t => t !== activeTag) : []
  const open = () => onOpenStock(r.symbol)
  return (
    <tr
      tabIndex={0}
      onClick={open}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open() }
      }}
      className="group cursor-pointer border-b border-border/25 text-[11px] transition-colors last:border-0 hover:bg-accent/[0.04] focus:outline-none focus-visible:bg-accent/[0.06]"
    >
      <td className="py-1.5 pl-3 pr-2">
        <div className="flex items-baseline gap-1.5">
          <span className="truncate font-medium text-foreground">{r.name ?? r.symbol}</span>
          <span className="shrink-0 font-mono text-[9px] text-muted/70">{r.symbol}</span>
          {young && (
            <span className="shrink-0 rounded border border-border/60 px-0.5 text-[9px] leading-tight text-muted/60"
              title={`窗口内仅 ${r.n_days} 个交易日（次新）：主场用已有天数估计`}>次新</span>
          )}
          <span className={cn('shrink-0 rounded border px-1 text-[9px] font-medium', STAGE_META[r.stage].chip)}
            title={r.stage === 'hot'
              ? `近 20 日已涨停 ${r.lu20} 天：预测价值已被兑现，不在观察池里`
              : r.stage === 'cold'
                ? '近 20 日无涨停且涨幅在 ±15% 内：唯一有历史 lift 的一段'
                : r.stage === 'warming' ? '未涨停但 20 日涨幅已超 15%' : '20 日跌超 20%，高波动来自出货'}>
            {STAGE_META[r.stage].label}
          </span>
          {r.risk !== 'none' && (
            <span className={cn('shrink-0 rounded border px-1 text-[9px] font-medium', RISK_META[r.risk].chip)}
              title={r.risk_reason ?? RISK_META[r.risk].hint}>
              {RISK_META[r.risk].label}
            </span>
          )}
        </div>
        <div className="mt-0.5 truncate text-[9px] text-muted/60">{r.industry ?? '未分类'}</div>
      </td>
      {activeTag && (
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
      )}
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
      <td className="px-2 py-1.5 text-right">
        {r.ret20 == null ? (
          <span className="text-[9px] text-muted/30">—</span>
        ) : (
          <span className={cn('font-mono tabular-nums', r.ret20 >= 0 ? 'text-red-600' : 'text-emerald-600')}>
            {r.ret20 > 0 ? '+' : ''}{fmtPct(r.ret20, 1)}
          </span>
        )}
      </td>
      <td className="px-2 py-1.5">
        <div className="flex items-center gap-1">
          {r.transition === 'up' ? (
            <ArrowUp className="h-3 w-3 shrink-0 text-foreground/50" aria-label="位次较前一交易日上升" />
          ) : r.transition === 'down' ? (
            <ArrowDown className="h-3 w-3 shrink-0 text-muted/45" aria-label="位次较前一交易日下降" />
          ) : r.transition === 'new' ? (
            <span className="shrink-0 text-[9px] text-accent" title="前一交易日无此票">新进</span>
          ) : (
            <span className="w-3 shrink-0" />
          )}
          <span className={cn('rounded border px-1 py-px text-[9px] font-medium', LEVEL_META[r.level].chip)}>
            {LEVEL_META[r.level].label}
          </span>
        </div>
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
    <section data-arena={summary.arena} className="overflow-hidden rounded-card border border-border bg-surface/40">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition-colors hover:bg-elevated/30"
      >
        <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', meta.dot)} />
        <span className="text-[12px] font-medium text-foreground">{meta.label}</span>
        <span className="hidden text-[9.5px] text-muted/60 md:inline">
          {summary.hint.split('：')[0]}
        </span>
        <span className="font-mono text-[10px] text-muted/70">
          {(activeTag ? rows.length : summary.count).toLocaleString()}
        </span>
        {titleNote && <span className="hidden text-[10px] text-muted/50 sm:inline">· {titleNote}</span>}
        <span className="ml-auto flex shrink-0 items-center gap-2.5">
          <LevelMix levels={levelCounts} total={rows.length} />
          {!activeTag && (
            <span className="flex items-center gap-1 text-[9px]" title="历史 fwd20 收益中位">
              <span className="text-muted/50">中位</span>
              <span className="font-mono tabular-nums text-secondary">{fmtPct(summary.med_ret20)}</span>
            </span>
          )}
        </span>
        <ChevronDown className={cn('h-3.5 w-3.5 shrink-0 text-muted/60 transition-transform duration-200', !collapsed && 'rotate-180')} />
      </button>
      {!collapsed && (
        // 展开后给表格自己的滚动区：150 行 ≈ 5 屏，全铺开就把「一屏一个标签」变成五屏。
        <div className="max-h-[62vh] overflow-auto border-t border-border/40">
          <table className="w-full min-w-[780px] table-fixed border-collapse text-left" aria-label={`${meta.label}明细`}>
            <colgroup>
              <col style={{ width: '26%' }} />
              {activeTag && <col style={{ width: '10%' }} />}
              <col style={{ width: '12%' }} />
              <col style={{ width: '12%' }} />
              <col style={{ width: '10%' }} />
              <col style={{ width: '11%' }} />
              <col style={{ width: '13%' }} />
            </colgroup>
            <thead className="sticky top-0 z-10 bg-surface">
              <tr className="border-b border-border/40 text-[10px] tracking-wide text-muted/60">
                <th className="py-1.5 pl-3 pr-2 font-medium">股票 / 行业</th>
                {activeTag && <th className="px-2 py-1.5 font-medium">其他标签</th>}
                <th className="px-2 py-1.5 text-right font-medium">流通市值</th>
                <th className="px-2 py-1.5 text-right font-medium">成交额</th>
                <th className="px-2 py-1.5 text-right font-medium">换手</th>
                <th className="px-2 py-1.5 text-right font-medium">20日涨幅</th>
                <th className="px-2 py-1.5 font-medium">位次</th>
              </tr>
            </thead>
            <tbody>
              {visible.map(r => <Row key={r.symbol} r={r} onOpenStock={onOpenStock} activeTag={activeTag} />)}
            </tbody>
          </table>
          {rows.length > visible.length && (
            <div className="flex items-center justify-center gap-2 border-t border-border/40 py-1.5">
              <span className="text-[9px] text-muted/60">已显示 {visible.length} / {rows.length} 只</span>
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

/** 口径抽屉里的一行定义。 */
function DefRow({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div className="flex gap-2">
      <span className="w-[64px] shrink-0 text-muted/70">{term}</span>
      <span className="min-w-0 flex-1 text-secondary/90">{children}</span>
    </div>
  )
}

/** 排序体检完整四格（口径抽屉内）。 */
function SortAuditBlock({ audit }: { audit: SortAudit | null | undefined }) {
  if (!audit) {
    return (
      <p className="text-[10px] text-muted/75">
        排序体检未生成 —— 17:10 定时任务（或 <code>shortline_audit.rebuild</code>）首次运行后出现。
        预注册：<code>docs/structural-edge/prereg-heat-sort.md</code>
      </p>
    )
  }
  const g = audit.gate
  const hasFwd = (audit.forward?.months ?? 0) > 0
  const w = hasFwd ? audit.forward! : audit.all
  const months = audit.forward?.months ?? 0
  const pending = months < g.min_months
  const cell = (label: string, v: number | null, opts?: { t?: number | null; hint?: string }) => (
    <div key={label} className="min-w-[104px]" title={opts?.hint}>
      <div className={cn('font-mono text-[13px] font-medium leading-none tabular-nums',
        v == null ? 'text-muted' : v >= 0 ? 'text-emerald-700' : 'text-red-700')}>
        {v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}bp`}
      </div>
      <div className="mt-1 text-[9.5px] text-muted">
        {label}
        {opts?.t != null && <span className="ml-1 font-mono">t {opts.t}</span>}
      </div>
    </div>
  )
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
        <span className="text-[11px] font-medium text-foreground">排序体检</span>
        <span className="font-mono text-[9px] text-muted/70">{audit.prereg_id}</span>
        <span className="ml-auto text-[9.5px] text-muted">
          {pending
            ? `进行中 · 前向可评估 ${months}/${g.min_months} 月（不到裁决线，只看不判）`
            : `已到裁决线 · 前向 ${months} 个可评估月，按预注册判据人工裁决`}
        </span>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-2">
        {cell('现行 lift（选中−池）', w.cur_lift_bp, { t: w.cur_t, hint: '现行排序键每天 15 只相对同日池子随机 15 只的超额，月均' })}
        {cell('候选 lift', w.shadow_lift_bp, { t: w.shadow_t, hint: `候选 ${audit.rule_shadow} 同口径` })}
        {cell('配对差（候选−现行）', w.paired_delta_bp, { t: w.paired_t, hint: '同一批月份逐月相减' })}
        {cell('候选净超额', w.shadow_net_bp, { hint: '选中−全市场，扣双边 12bp（红线 ≥0）' })}
      </div>
      <p className="text-[9.5px] leading-relaxed text-muted/75">
        {hasFwd ? `前向登记 ${audit.fwd_days} 天` : `回填对照 ${audit.backfill_days} 天（样本内，不参与裁决）`}
        {' · '}共登记 {audit.days_registered} 天 / 可评估 {audit.days_evaluated} 天
        {' · '}门槛：配对差 ≥ {g.lift_delta_bp}bp 且 t &gt; {g.t}，红线 net ≥ {g.net_bp}bp
        {' · '}判据与历史回测见 <code>docs/structural-edge/prereg-heat-sort.md</code>
      </p>
    </div>
  )
}

/**
 * 口径抽屉 —— 研究依据的唯一家。
 *
 * 这些内容每天不变，放第一屏只会把「今天有什么」挤下去。默认收起，
 * 但入口在观察池脚注、可执行动作卡片和页尾各有一处，不用猜。
 */
function MethodDrawer({
  data, heatCut, topN, open, onToggle, anchorRef,
}: {
  data: ShortlineRadarData
  heatCut: number
  topN: number
  open: boolean
  onToggle: () => void
  anchorRef: RefObject<HTMLElement>
}) {
  const s = data.summary
  return (
    <section id="radar-method" ref={anchorRef} className="scroll-mt-12 overflow-hidden rounded-card border border-border bg-surface/40">
      <button type="button" onClick={onToggle} aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2 text-left transition-colors hover:bg-elevated/30">
        <span className="text-[12px] font-medium text-foreground">口径 · 定义 · 研究依据</span>
        <span className="hidden text-[9.5px] text-muted/70 sm:inline">
          主场 = 20 日中位 · 标签 = 视图不是分类 · 历史读数 34 期
        </span>
        <ChevronDown className={cn('ml-auto h-3.5 w-3.5 shrink-0 text-muted/60 transition-transform', open && 'rotate-180')} />
      </button>
      {open && (
        <div className="space-y-4 border-t border-border/40 px-4 py-3 text-[10px] leading-relaxed text-secondary">
          <div className="space-y-3">
            <div>
              <div className="mb-1 text-[10px] font-medium text-foreground">主场（20 日换手中位 × 20 日流通市值中位）</div>
              <div className="space-y-0.5">
                {ARENA_ORDER.map(a => (
                  <DefRow key={a} term={ARENA_META[a].label}>
                    <span className={cn('mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle', ARENA_META[a].dot)} />
                    {s.arenas.find(x => x.arena === a)?.hint ?? ''}
                  </DefRow>
                ))}
              </div>
            </div>

            <div>
              <div className="mb-1 text-[10px] font-medium text-foreground">属性标签（可叠加，不是分类）</div>
              <div className="space-y-0.5">
                {TAG_ORDER.map(t => (
                  <DefRow key={t} term={TAG_META[t].label}>
                    {s.tags.find(x => x.tag === t)?.hint ?? ''}
                  </DefRow>
                ))}
              </div>
              <p className="mt-1 text-[9.5px] text-muted/75">
                实测 {overlapText(data)} 只同时带 2 个以上标签 —— 四个标签页的计数不能相加，
                同一只票会出现在多个视图里，但任何一屏只有一份。
              </p>
            </div>

            <div>
              <div className="mb-1 text-[10px] font-medium text-foreground">阶段与位次</div>
              <div className="space-y-0.5">
                {(Object.keys(STAGE_META) as ShortlineRadarStage[]).map(k => (
                  <DefRow key={k} term={STAGE_META[k].label}>
                    {k === 'cold' && `20 日内无涨停且 |20 日涨幅| < 15%，是唯一有预测价值的一段。`}
                    {k === 'warming' && `20 日内无涨停但已涨超 15%：资金在推，尚未封板。`}
                    {k === 'hot' && `20 日内已有涨停：预测价值已被兑现，是追高榜的主体。`}
                    {k === 'fading' && `20 日跌超 20% 且无涨停：高波动来自出货。`}
                    {' '}今日 {s.stages[k].toLocaleString()} 只。
                  </DefRow>
                ))}
                <DefRow term="位次">
                  核心/活跃/背景/沉默 = 当日成交额与换手的横截面分位，描述「今天有没有人交易」，不是好坏的判断。
                </DefRow>
              </div>
            </div>
          </div>

          <div>
            <div className="mb-1 text-[10px] font-medium text-foreground">观察池为什么不是买入名单</div>
            <ul className="list-disc space-y-1 pl-4 text-[9.5px] text-muted/85">
              <li>heat 最高 1/5 的 20 日超额 <b className="text-foreground/80">−1.50%（t −5.03，1,853 天，8/8 年负）</b>；控制市值后仍为负。观察池的 15 只正好来自这一格。</li>
              <li>去掉已涨停的票之后，前 20 名的未来 5 日打板率是基准的 6.98 倍（23.3bp → 162bp），但<b className="text-foreground/80">打板率不是收益</b>。</li>
              <li>热度分是四项分位等权（振幅 / 换手 / 市值反向 / 当日成交额），门槛 {heatCut}，按热度取前 {topN}；它只排序，没有校准，不能读成上涨概率。</li>
              <li>“超跌修复”救不回来（<code>stock-dip-repair.md</code>）：唯一两段同向的改进是缩量+小市值，Δ +115.6bp，是<b className="text-foreground/80">少亏</b>不是赚。</li>
            </ul>
          </div>

          <div>
            <div className="mb-1 text-[10px] font-medium text-foreground">跨年妖候选池（实时计算，不进观察池）</div>
            <ul className="list-disc space-y-1 pl-4 text-[9.5px] text-muted/85">
              <li>
                把跨年妖股 8 条历史画像翻译成 6 个可量化条件：现价 3~15 元 · 流通市值 10~60 亿 ·
                近 20 日有涨停 · 非 ST/北交所/次新 · 名字记忆点（东方·龙·羊·福·万事·长虹…），
                再加「位置干净度」= 20 日涨幅 − 60 日涨幅（net_gain，降序）。
                梯队：60 日涨幅 &lt; 30% 为第一梯队（刚启动），否则第二梯队（已涨过）。
              </li>
              <li>
                <b className="text-foreground/80">实时计算</b>：由后端 <code>citizenship.py::_cross_year_candidates</code>
                从 <code>kline_daily_enriched</code> 当日横截面现算，每次请求重算，不落盘、无批处理，
                随交易日自动更新（与旧「锁仓 + 上方无抛压」块同源位置）。
              </li>
              <li>
                <b className="text-foreground/80">候选池不是买入名单</b>：项目 20 年全样本（<code>docs/yaogu-census.md</code>）
                里，首板后开盘买入 20 日超额中位 −1.85%（t −15.94）、60 日 −3.56%（t −20.74）；
                见顶确认时买入 20 日超额中位 −26.9%、1 年 −57.4%。「确认它是妖股」这个动作本身就把期望由正转负。
              </li>
              <li>
                <b className="text-foreground/80">它替代了旧的「锁仓 + 上方无抛压」块</b>：旧块依赖获利盘 ≥75% 这一
                无回测背书、方向自相矛盾的指标（详见 <code>docs/structural-edge/stock-dip-repair.md</code> 与
                <code>docs/state-family-converge.md</code> 的筹码族结论），已连同后端死代码一起下线。
              </li>
            </ul>
          </div>

          <div>
            <div className="mb-1 text-[10px] font-medium text-foreground">可执行规则（全市场换手板口径）</div>
            <p className="mb-1.5 text-[9.5px] text-muted/75">
              今日清单、代价与仓位在上方「明日买点」；这张表只做动作之间的对比，不重复清单。
            </p>
            <div className="max-w-[860px] text-[9.5px]">
              <div className="grid grid-cols-[minmax(0,1fr)_64px_minmax(0,1fr)] gap-x-3 border-b border-border/40 pb-1 text-muted/60">
                <span>动作</span>
                <span className="text-right">超额</span>
                <span>结构</span>
              </div>
              {[
                ['T 日封板 → T+1 开盘卖', '+1.45%', '2026 · 17 年唯一稳定为正'],
                ['T 日封板 → 持有 5 日', '+0.78%', '低于 T+1；carry 恒为负'],
                ['T+1 日内跌 >5% → T+2 开盘买、收盘卖', LU_STUDY.excessPct, `t ${LU_STUDY.t} · ${LU_STUDY.window} ${LU_STUDY.years} 年正`],
                ['同上，剔除 T+2 低开 >9%', '+0.764%', 't 9.21 · 近似跌停开盘卖不掉'],
              ].map(r => (
                <div key={r[0]} className="grid grid-cols-[minmax(0,1fr)_64px_minmax(0,1fr)] gap-x-3 border-b border-border/25 py-1 text-secondary last:border-0">
                  <span>{r[0]}</span>
                  <span className="text-right font-mono tabular-nums text-foreground">{r[1]}</span>
                  <span className="text-muted/75">{r[2]}</span>
                </div>
              ))}
            </div>
            <p className="mt-1.5 text-[9.5px] text-muted/85">
              <span className="text-foreground/80">不要做：</span>
              T+1 收盘买 → T+2 开盘卖（隔夜恒负）；追 4 板以上（溢价腰斩）；把缩量涨停当信号（价差 1.74pp → 0.41pp）；
              把观察池当买入名单。指数是给散户看的——这是<b>观点</b>，不是回测结论，页面上不当作数据使用。
            </p>
          </div>

          <div>
            <div className="mb-1 text-[10px] font-medium text-foreground">排序体检</div>
            <SortAuditBlock audit={data.focus.sort_audit} />
          </div>

          <div className="space-y-0.5 text-[9.5px] text-muted/75">
            <div>数据：{data.as_of ?? '—'}{data.prev_as_of ? ` · 对比 ${data.prev_as_of}` : ''} · 主场窗口 {data.window_days} 日 · 标签统计窗口 {data.stats_window} 日 · 财报期 {s.financial_period ?? '无'}。</div>
            <div>左尾与中位取自 2024-01–2026-10 共 34 个基期的 fwd20，是历史读数不是预测；白马缺 PIT 财报序列，故不给历史读数。</div>
            <div>未消除的偏差：行业分类用当前名称映射、ETF 池为在市基金；无逐笔委托，真实排队成交率不可测；未计冲击成本。</div>
          </div>
        </div>
      )}
    </section>
  )
}

/** 标签重叠只在这里算一次（页脚旧版也算了一遍，两处数字可能分叉）。 */
function overlapText(data: ShortlineRadarData): string {
  let two = 0
  let three = 0
  for (const r of data.rows) {
    if (r.tags.length === 2) two += 1
    else if (r.tags.length >= 3) three += 1
  }
  return `${two} 只带 2 个、${three} 只带 3 个`
}

export function ShortlineRadar() {
  const [params, setParams] = useSearchParams()
  const asOf = params.get('as_of') ?? ''
  const rawTag = params.get('tag') ?? 'all'
  const view: ViewKey = (VIEWS.some(v => v.key === rawTag) ? rawTag : 'all') as ViewKey
  const activeTag: ShortlineRadarTag | null = view === 'all' ? null : view
  const watchOnly = params.get('watch') === '1'

  const [narrow, setNarrow] = useState<Narrow>(EMPTY_NARROW)
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [preview, setPreview] = useState<string | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>('amount')
  const [methodOpen, setMethodOpen] = useState(false)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const methodRef = useRef<HTMLElement>(null)

  const q = useQuery<ShortlineRadarData>({
    queryKey: QK.shortlineRadar(asOf || undefined),
    queryFn: () => api.shortlineRadar(asOf || undefined),
    staleTime: 60_000,
  })
  const data = q.data

  // 明日买点：与 /decisions 的「砸盘反转」面板读同一个接口、同一个产地
  // （`app/decision/lu_rebound.py`），这里只是把它放到「今天该做什么」的位置上。
  // 日期用**雷达解析后的** `data.as_of`，不用 URL 上的原始值：用户选周末时雷达会回溯到
  // 上一个交易日，而买点接口对非交易日返回「无行情分区」—— 两边必须同一天。
  // 标签页是浏览模式，不发这个请求。
  const resolvedAsOf = data?.as_of ?? null
  const luQ = useQuery<DecisionLuCandidates>({
    queryKey: ['decision', 'lu-rebound', resolvedAsOf ?? 'pending'],
    queryFn: () => api.decisionLuCandidates(resolvedAsOf ?? undefined),
    staleTime: 60_000,
    retry: false,
    enabled: activeTag === null && resolvedAsOf != null,
  })

  // 自选只用于「只看自选」这一个开关；接口失败就当没有自选，不影响主表。
  const watchQ = useQuery({
    queryKey: QK.watchlist,
    queryFn: api.watchlistList,
    staleTime: 5 * 60_000,
    retry: false,
  })
  const watchSet = useMemo(
    () => new Set((watchQ.data?.symbols ?? []).map(x => x.symbol)),
    [watchQ.data],
  )
  // 自选里大部分是 ETF，而本页只覆盖个股：交集为 0 时这个开关只会筛出空屏，直接不显示。
  const watchInRadar = useMemo(() => {
    if (!data || watchSet.size === 0) return 0
    let n = 0
    for (const r of data.rows) if (watchSet.has(r.symbol)) n += 1
    return n
  }, [data, watchSet])

  const patchParams = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(patch)) {
      if (v == null || v === '') next.delete(k)
      else next.set(k, v)
    }
    setParams(next, { replace: true })
  }

  /** 标签页上的计数：全部取 total，其余取后端 summary（与 rows 实测一致）。 */
  const viewCounts = useMemo(() => {
    const c = { all: 0, bluechip: 0, whitehorse: 0, leader: 0, yaogu: 0 } as Record<ViewKey, number>
    if (!data) return c
    c.all = data.summary.total
    for (const t of data.summary.tags) c[t.tag] = t.count
    return c
  }, [data])

  /** 换标签 = 换一屏：上一屏的主场/变化/风险筛选不带过来，否则会被读成「该标签没有票」。 */
  const switchView = (v: ViewKey) => {
    patchParams({ tag: v === 'all' ? null : v })
    setNarrow(EMPTY_NARROW)
    setOpen({})
  }

  // 在一张 150 行的表里切标签时，新的一屏不该从半截开始；已经在顶部就不动。
  useEffect(() => {
    const el = panelRef.current
    if (el && el.getBoundingClientRect().top < 40) el.scrollIntoView?.({ block: 'start' })
  }, [view])

  const toggleArena = (a: ShortlineRadarArena) =>
    setNarrow(n => ({ ...n, arena: n.arena === a ? null : a }))
  const toggleTrend = (t: Trend) =>
    setNarrow(n => ({ ...n, trend: n.trend === t ? null : t }))
  const toggleRisk = (r: RiskFlag) =>
    setNarrow(n => ({ ...n, risk: n.risk === r ? null : r }))
  const toggleWatch = () => patchParams({ watch: watchOnly ? null : '1' })
  const setKeyword = (v: string) => setNarrow(n => ({ ...n, keyword: v }))
  const clearFilters = () => {
    setNarrow(EMPTY_NARROW)
    if (watchOnly) patchParams({ watch: null })
  }

  const narrowing = Boolean(narrow.arena || narrow.trend || narrow.risk || watchOnly || narrow.keyword.trim())

  const filtered = useMemo(() => {
    const rows = data?.rows ?? []
    const kw = narrow.keyword.trim().toLowerCase()
    return rows.filter(r => {
      if (activeTag && !r.tags.includes(activeTag)) return false
      if (narrow.arena && r.arena !== narrow.arena) return false
      if (narrow.trend && r.transition !== narrow.trend) return false
      if (narrow.risk && r.risk !== narrow.risk) return false
      if (watchOnly && !watchSet.has(r.symbol)) return false
      if (kw) {
        const hay = `${r.symbol} ${r.name ?? ''} ${r.industry ?? ''}`.toLowerCase()
        if (!hay.includes(kw)) return false
      }
      return true
    })
  }, [data, activeTag, narrow, watchOnly, watchSet])

  const buckets = useMemo(() => {
    const map: Record<string, ShortlineRadarRow[]> = {}
    for (const r of filtered) (map[r.arena] ??= []).push(r)
    for (const k of Object.keys(map)) {
      map[k].sort((a, b) => {
        if (sortKey === 'mcap') return b.mcap - a.mcap
        if (sortKey === 'ret20') return (b.ret20 ?? -9) - (a.ret20 ?? -9)
        if (sortKey === 'turn') return b.turn_ratio - a.turn_ratio
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
  /** 状态条上的变化/风险计数必须与「点下去能筛出几行」一致：标签页按该标签重算。 */
  const scopeStats = useMemo(() => {
    const rows = data?.rows ?? []
    const inScope = activeTag ? rows.filter(r => r.tags.includes(activeTag)) : rows
    const tr: Record<Trend, number> = { up: 0, down: 0, new: 0 }
    const rk: Record<RiskFlag, number> = { high: 0, watch: 0 }
    for (const r of inScope) {
      if (r.transition !== 'stay') tr[r.transition] += 1
      if (r.risk !== 'none') rk[r.risk] += 1
    }
    return { tr, rk, total: inScope.length }
  }, [data, activeTag])
  const total = data?.summary.total ?? 0
  const base = activeTag ? tagSummary?.count ?? 0 : total
  const hit = filtered.length

  /**
   * 默认展开哪一格：
   *   全部页无筛选 → 游资主场（短线的主场，也是观察池 15 只里 14 只的来源）；
   *   标签页/筛选后 → 票最多的一格。
   */
  const autoOpenArena = useMemo(() => {
    const list = ARENA_ORDER.filter(a => (buckets[a]?.length ?? 0) > 0)
    if (list.length === 0) return null
    if (!activeTag && !narrowing && list.includes('hotmoney')) return 'hotmoney'
    return list.reduce((best, a) => ((buckets[a]?.length ?? 0) > (buckets[best]?.length ?? 0) ? a : best), list[0])
  }, [buckets, activeTag, narrowing])

  const openMethod = () => {
    setMethodOpen(true)
    requestAnimationFrame(() => methodRef.current?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
  }

  const watchCount = watchInRadar

  return (
    <div className="min-h-full bg-base pb-10">
      <PageHeader
        title={SHORTLINE_RADAR_PAGE_TITLE}
        subtitle={`${SHORTLINE_RADAR_PAGE_SUBTITLE} · 数据截至 ${data?.as_of ?? '—'}`}
        className="flex-wrap pl-12 sm:pl-5"
        right={
          <div className="flex w-full items-center justify-end gap-2 sm:w-auto">
            {asOf && (
              <button
                type="button"
                onClick={() => patchParams({ as_of: null })}
                className="rounded-btn border border-border bg-elevated px-2 py-1 text-[10px] text-muted transition-colors hover:text-foreground"
                title="回到最新交易日"
              >
                回到最新
              </button>
            )}
            <DateInput
              value={asOf}
              onChange={v => patchParams({ as_of: v })}
              ariaLabel="回看日期"
              placeholder="回看某天"
              className="min-w-0 flex-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary sm:flex-none"
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

      <div className="mx-auto max-w-[1400px] px-4 pb-4 sm:px-6">
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
                <span
                  className="hidden shrink-0 pb-2 text-[9px] leading-tight text-muted/50 lg:block"
                  title="同一个标签页里一只票只出现一次；不同标签页会重复计同一只票"
                >
                  标签是视图，不是分类
                </span>
              </div>
            </div>

            <div
              ref={panelRef}
              id="radar-panel"
              role="tabpanel"
              aria-labelledby={`radar-tab-${view}`}
              className="scroll-mt-10 space-y-3 pt-3"
            >
              {q.isError && (
                <div className="flex items-center gap-2 rounded-btn border border-amber-500/25 bg-amber-500/[0.06] px-3 py-1.5 text-[10px] text-amber-700">
                  <AlertTriangle className="h-3 w-3 shrink-0" />
                  刷新失败（{q.error?.message ?? '未知错误'}），当前显示的是上一次成功的数据
                  <button onClick={() => q.refetch()} className="ml-auto underline underline-offset-2">重试</button>
                </div>
              )}

              {/* ② 明日买点：全页唯一可执行的动作。放在状态条之前 ——
                  先回答「明天买不买」，再回答「今天什么状态」。 */}
              {activeTag === null && (
                <LuBuyPoint
                  data={luQ.data}
                  failed={luQ.isError}
                  error={luQ.error?.message}
                  onOpenStock={setPreview}
                  onOpenMethod={openMethod}
                />
              )}

              {/* ③ 状态条：所有视图共用 —— 今天能不能做、变了什么、哪些不能碰 */}
              <StatusBar
                focus={data.focus}
                summary={data.summary}
                scope={scopeStats}
                narrow={narrow}
                onTrend={toggleTrend}
                onRisk={toggleRisk}
              />

              {/* ③ 全部页：观察池 + 可执行动作 + 结构带；标签页只给标签自身的读数 */}
              {activeTag === null ? (
                <>
                  <FocusBand
                    focus={data.focus}
                    asOf={data.as_of}
                    heatCut={data.focus.heat_cut}
                    topN={data.focus.top_n}
                    onOpenStock={setPreview}
                    onOpenMethod={openMethod}
                  />
                  <CrossYearCandidates
                    watch={data.focus.cross_year}
                    hotCount={data.summary.stages.hot}
                    onOpenStock={setPreview}
                    onOpenMethod={openMethod}
                  />
                  <MarketBand
                    arenas={data.summary.arenas}
                    total={total}
                    activeArena={narrow.arena}
                    onPick={toggleArena}
                  />
                </>
              ) : tagSummary ? (
                <TagHeader
                  tag={activeTag}
                  summary={tagSummary}
                  arenaCounts={tagArenaCounts}
                  market={data.summary.market}
                  financialPeriod={data.summary.financial_period}
                  activeArena={narrow.arena}
                  onPickArena={toggleArena}
                  onGoAll={() => switchView('all')}
                />
              ) : null}

              {/* ④ 工具条：排序 + 自选 + 当前筛选 + 搜索；计数并进来，不再单独一行 */}
              <DetailToolbar
                sortKey={sortKey}
                onSort={setSortKey}
                narrow={narrow}
                onKeyword={setKeyword}
                onClear={clearFilters}
                onToggleArena={() => narrow.arena && toggleArena(narrow.arena)}
                onToggleTrend={() => narrow.trend && toggleTrend(narrow.trend)}
                onToggleRisk={() => narrow.risk && toggleRisk(narrow.risk)}
                watchAvailable={watchInRadar > 0}
                watchOnly={watchOnly}
                onToggleWatch={toggleWatch}
                watchCount={watchCount}
                hit={hit}
                base={base}
                activeTag={activeTag}
                narrowing={narrowing}
              />

              {/* ⑤ 明细：标签内部再按主场分组 */}
              {hit === 0 ? (
                <div className="flex flex-col items-center justify-center gap-2 rounded-card border border-border bg-surface/40 px-6 py-14">
                  <div className="text-[13px] font-medium text-foreground">这一屏没有同时满足这些条件的票</div>
                  <p className="text-[11px] text-muted">
                    {activeTag ? `「${TAG_META[activeTag].label}」共 ${base} 只，` : `全市场共 ${base} 只，`}
                    当前筛选把它们排除了。
                  </p>
                  <div className="mt-1 flex items-center gap-2">
                    <button onClick={clearFilters}
                      className="rounded-btn border border-border bg-elevated px-2 py-1 text-[10px] text-secondary hover:text-foreground">
                      清空筛选
                    </button>
                    {activeTag && (
                      <button onClick={() => switchView('all')}
                        className="rounded-btn border border-border bg-elevated px-2 py-1 text-[10px] text-secondary hover:text-foreground">
                        切到全市场
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
                    // 默认只开一格：全部页是游资，标签页是票最多的那格；手动折叠优先。
                    const isOpen = open[key] ?? (narrowing || key === autoOpenArena)
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
                            ? (narrow.keyword.trim()
                              ? `命中 ${rows.length} / 主场 ${s.count}`
                              : (s.count > 0 ? `占该主场 ${Math.round(rows.length / s.count * 100)}%` : undefined))
                            : (narrowing && rows.length !== s.count ? `命中 ${rows.length} / ${s.count}` : undefined)
                        }
                      />
                    )
                  })}
                </div>
              )}

              {/* ⑥ 口径抽屉：研究依据的唯一家 */}
              <MethodDrawer
                data={data}
                heatCut={data.focus.heat_cut}
                topN={data.focus.top_n}
                open={methodOpen}
                onToggle={() => setMethodOpen(v => !v)}
                anchorRef={methodRef}
              />
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
