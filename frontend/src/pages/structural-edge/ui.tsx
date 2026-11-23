/**
 * 结构优势页的展示原语。
 *
 * 存在理由：重构前同一个页面里有 4 种卡片写法、3 种表格写法、5 种 callout 写法，
 * 改一个视觉决定要动 20 处。这里把它们收敛成 6 个组件 + 1 个图表壳，
 * 页面正文只描述「内容」，不再描述「长什么样」。
 *
 * 2026-11-22 六视图改版时的两处修订：
 *   - Section 增加序号与右上角 actions；序号只在当前章内有意义，跨章引用一律用锚点/案卷字母。
 *   - Chart 改为「拿到宽度才 init」+ ResizeObserver：非当前 tab 的面板是 hidden 的，
 *     旧实现会在 0 宽时 init，切回来只能看到空白。
 *
 * 只读依赖：@/lib/theme（跟随明暗）、echarts、lucide-react。
 */
import { useEffect, useRef, type ReactNode } from 'react'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { ChevronRight, FileCode2 } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useTheme } from '@/lib/theme'

export type Tone = 'ok' | 'muted' | 'warn' | 'danger'

const TONE_CHIP: Record<Tone, string> = {
  ok: 'bg-accent/15 text-accent',
  warn: 'bg-warning/15 text-warning',
  danger: 'bg-danger/15 text-danger',
  muted: 'bg-elevated text-secondary',
}

/** 判定标签。四档语义固定：ok=成立/可通过，warn=未证伪/待观察，danger=证伪/阻断，muted=不适用。 */
export function Tag({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className={`inline-block whitespace-nowrap rounded-full px-1.5 py-0.5 text-[10.5px] font-medium ${TONE_CHIP[tone]}`}>
      {children}
    </span>
  )
}

/**
 * 按节的更新频率。四档写死，和 Shell.TABS 的 cadence 用词同源。
 *
 * 存在理由：章节是「读什么」，频率是「什么时候读」。同一章里也会混频率
 * （执行每天变，规则只有预注册才变），所以频率必须能标到节上，不能只标到 tab 上。
 */
export type CadenceKind = 'daily' | 'experiment' | 'frozen' | 'ondemand'

const CADENCE_LABEL: Record<CadenceKind, string> = {
  daily: '每日',
  experiment: '按实验',
  frozen: '冻结',
  ondemand: '按需',
}

export function Cadence({ kind, note }: { kind: CadenceKind; note?: string }) {
  const live = kind === 'daily'
  return (
    <span className="inline-flex items-center gap-1.5 text-[10px] leading-none text-secondary">
      <span className={cn('h-1.5 w-1.5 rounded-full', live ? 'bg-accent' : 'bg-border')} />
      {CADENCE_LABEL[kind]}
      {note && <span className="text-secondary/80">· {note}</span>}
    </span>
  )
}

/** 截面数字卡。k=指标名，v=读数，d=口径或对照（必须写清分母/样本，否则就是没有信息量的数字）。 */
export function Stat({ k, v, d }: { k: string; v: string; d?: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface px-3.5 py-3">
      <div className="text-[10.5px] text-secondary">{k}</div>
      <div className="mt-1 font-mono text-[19px] font-semibold leading-none tabular-nums text-foreground">{v}</div>
      {d && <div className="mt-1.5 text-[10.5px] leading-relaxed text-secondary/80">{d}</div>}
    </div>
  )
}

export function StatGrid({ items, cols = 4 }: { items: { k: string; v: string; d?: string }[]; cols?: 2 | 3 | 4 }) {
  const grid = cols === 2 ? 'md:grid-cols-2' : cols === 3 ? 'md:grid-cols-3' : 'md:grid-cols-4'
  return (
    <div className={`mt-3 grid grid-cols-2 gap-2.5 ${grid}`}>
      {items.map(x => <Stat key={x.k} {...x} />)}
    </div>
  )
}

/**
 * 分节容器。
 *
 * no 是「本视图内」的序号（01/02…），不承担跨视图引用职责 —— 跨视图引用请用
 * 「案卷 B」这类带语义的名字或锚点，避免改版一次把所有引用都变成错的。
 */
export function Section({
  id, no, title, lead, source, actions, asOf, lag, drift, first, className, children,
}: {
  id: string
  no?: string
  title: string
  lead?: ReactNode
  source?: string
  actions?: ReactNode
  /** 这一节数值的数据日。给 daily 节用；冻结节不写，避免制造「它每天在变」的错觉。 */
  asOf?: string | null
  /** 行情落后几个交易日（payload.data.lag_sessions）。>0 时标黄。 */
  lag?: number
  /** 产出物之间的数据日不一致（例如账户 2026-11-20 ≠ 扫描 2026-11-21）。 */
  drift?: string | null
  first?: boolean
  className?: string
  children: ReactNode
}) {
  const stale = (lag ?? 0) > 0
  return (
    <section id={id} className={cn('scroll-mt-28', first ? '' : 'mt-10', className)}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2 border-b border-border pb-2.5">
        <div className="flex min-w-0 items-baseline gap-2.5">
          {no && <span className="shrink-0 font-mono text-[10.5px] text-muted">{no}</span>}
          <h2 className="text-[15px] font-semibold leading-snug text-foreground">{title}</h2>
        </div>
        {actions}
      </div>
      {lead && <div className="mt-2.5 max-w-3xl text-[13px] leading-relaxed text-secondary">{lead}</div>}
      {source && (
        <div className="mt-1.5 flex items-start gap-1.5 text-[10.5px] leading-relaxed text-secondary">
          <FileCode2 className="mt-0.5 h-3 w-3 shrink-0" />
          <span className="break-all font-mono">{source}</span>
        </div>
      )}
      {(asOf || stale || drift) && (
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] leading-relaxed">
          {asOf && <span className="font-mono text-secondary">数据日 {asOf}</span>}
          {stale && (
            <span className="font-medium text-warning">
              行情落后 {lag} 个交易日（补数据后重跑，不要照旧数下单）
            </span>
          )}
          {drift && <span className="font-medium text-warning">{drift}</span>}
        </div>
      )}
      <div className="mt-3">{children}</div>
    </section>
  )
}

const TONE_BAR: Record<Tone, string> = {
  ok: 'border-l-accent bg-accent/[0.06]',
  warn: 'border-l-warning bg-warning/[0.06]',
  danger: 'border-l-danger bg-danger/[0.06]',
  muted: 'border-l-border bg-surface/60',
}

/**
 * 结论条。label 写「这句话是什么性质」（结论/边界/口径），避免读者把它当又一段论述。
 * 视觉上用 3px 左边条而不是四边描边：一页里出现四五次时，四边描边会把版面切碎。
 */
export function Callout({
  tone, label, children, className,
}: {
  tone: Tone
  label?: string
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn('mt-4 rounded-r-lg border border-l-[3px] border-border px-4 py-3 text-[13px] leading-relaxed text-foreground', TONE_BAR[tone], className)}>
      {label && <div className="mb-1 text-[10px] font-medium tracking-wide text-secondary">{label}</div>}
      <div>{children}</div>
    </div>
  )
}

export interface Col<T> {
  head: string
  render: (row: T) => ReactNode
  /** 单元格额外 class，用于右对齐/等宽/着色 */
  cls?: string
  /** 表头额外 class */
  headCls?: string
  /** 数字列右对齐（金额、比率、t 值）。文字列保持左对齐。 */
  align?: 'right'
  /** 列宽（'12%' / '88px'）。给任意一列即整表转 table-fixed，宽度不再随内容抖动。 */
  w?: string
}

/** 唯一表格实现。reason=这张表支撑什么判断，写在表上方，防止表格退化成数据倾倒。 */
export function DataTable<T>({
  cols, rows, rowKey, reason, dense, minWidth, swipeHint,
}: {
  cols: Col<T>[]
  rows: T[]
  rowKey: (row: T, i: number) => string
  reason?: ReactNode
  dense?: boolean
  /** 窄屏最小宽度：不给则按列数推断，避免 7 列表格在 390px 里被挤成一字一行。 */
  minWidth?: number
  /** 窄屏提示可横向滑动（只给主表用，避免每张表下面都挂一句）。 */
  swipeHint?: boolean
}) {
  const cell = dense ? 'px-2 py-1.5' : 'px-2.5 py-2'
  const min = minWidth ?? (cols.length >= 7 ? 820 : cols.length >= 5 ? 680 : cols.length >= 3 ? 540 : 0)
  const fixed = cols.some(c => c.w)
  return (
    <div className="mt-3">
      {reason && <div className="max-w-3xl text-[11px] leading-relaxed text-secondary">{reason}</div>}
      <div className="relative mt-1.5">
        <div className="overflow-x-auto overscroll-x-contain">
          <table
            className={cn('w-full border-collapse text-[12.5px]', fixed && 'table-fixed')}
            style={min ? { minWidth: min } : undefined}
          >
            {fixed && (
              <colgroup>
                {cols.map(c => <col key={c.head} style={c.w ? { width: c.w } : undefined} />)}
              </colgroup>
            )}
            <thead>
              <tr>
                {cols.map(c => (
                  <th key={c.head} className={cn('border-b border-border text-left align-bottom text-[10.5px] font-medium text-secondary', cell, c.align === 'right' && 'text-right', c.headCls)}>
                    {c.head}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={rowKey(r, i)} className="transition-colors hover:bg-elevated/40">
                  {cols.map(c => (
                    <td key={c.head} className={cn('border-b border-border/70 text-left align-top text-foreground', cell, c.align === 'right' && 'text-right', c.cls)}>
                      {c.render(r)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {min > 0 && (
          <div aria-hidden className="pointer-events-none absolute inset-y-0 right-0 w-6 bg-gradient-to-l from-base to-transparent sm:hidden" />
        )}
      </div>
      {swipeHint && min > 0 && (
        <div className="mt-1 text-[9.5px] text-secondary sm:hidden">左右滑动查看完整表格 →</div>
      )}
    </div>
  )
}

/** 折叠块。反证案卷专用：内容常驻 DOM（可搜索、可被测试读到），只是默认不占视线。 */
export function Fold({ id, title, hint, children }: { id: string; title: string; hint?: string; children: ReactNode }) {
  return (
    <details id={id} className="group mt-3 scroll-mt-28 rounded-lg border border-border bg-surface/50">
      <summary className="flex cursor-pointer select-none list-none items-center gap-2 rounded-lg px-4 py-3 text-[12.5px] font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted transition-transform duration-150 group-open:rotate-90" />
        <span className="min-w-0">{title}</span>
        {hint && <span className="ml-auto hidden shrink-0 text-[10px] font-normal text-secondary sm:block">{hint}</span>}
      </summary>
      <div className="border-t border-border px-4 pb-4">{children}</div>
    </details>
  )
}

/** 超额收益统一写法：正数带 +，负数用真减号 U+2212（ASCII '-' 在密集数字里读起来像连字符）。 */
export function fmtBp(v: number): string {
  if (v === 0) return '0'
  return `${v > 0 ? '+' : '−'}${Math.abs(v)}`
}

/** 图表坐标轴配色，跟随主题。 */
export function useAxis() {
  const dark = useTheme() === 'dark'
  return {
    dark,
    axisText: dark ? '#b4b2a9' : '#5f5e5a',
    axisLine: dark ? '#444441' : '#e3e2dd',
    barUp: dark ? '#85B7EB' : '#185FA5',
    barDown: dark ? '#E24B4A' : '#C0392B',
    barAlt: dark ? '#378ADD' : '#85B7EB',
  }
}

/** 图表壳。hidden 面板里 0 宽时不要 init，等 ResizeObserver 拿到宽度再建实例。 */
export function Chart({ option, height = 280, className }: { option: EChartsOption; height?: number; className?: string }) {
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    let chart: ReturnType<typeof echarts.init> | null = null
    const ensure = () => {
      if (!chart) {
        if (el.clientWidth === 0) return
        chart = echarts.init(el)
        chart.setOption(option)
        return
      }
      chart.resize()
    }
    ensure()
    const onResize = () => chart?.resize()
    window.addEventListener('resize', onResize)
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(ensure) : null
    ro?.observe(el)
    // 案卷折叠里的图：<details> 收起时没有盒子，RO 在部分浏览器里不会补发尺寸；
    // 直接监听 toggle，展开后下一帧再 init。
    const details = el.closest('details')
    const onToggle = () => { if (details?.open) window.setTimeout(ensure, 0) }
    details?.addEventListener('toggle', onToggle)
    return () => {
      window.removeEventListener('resize', onResize)
      ro?.disconnect()
      details?.removeEventListener('toggle', onToggle)
      chart?.dispose()
    }
  }, [option])
  return <div ref={ref} className={className} style={{ height }} />
}

/** 图表卡片：标题 + 口径 + 读法，避免图表裸奔成一堵墙。 */
export function ChartCard({
  title, hint, footer, children, className,
}: {
  title: string
  hint?: string
  footer?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <div className={cn('rounded-lg border border-border bg-surface px-3.5 pb-3 pt-3', className)}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
        <div className="text-[11.5px] font-medium text-secondary">{title}</div>
        {hint && <div className="font-mono text-[9.5px] text-secondary">{hint}</div>}
      </div>
      <div className="mt-1">{children}</div>
      {footer && <div className="mt-1 text-[11px] leading-relaxed text-secondary">{footer}</div>}
    </div>
  )
}
