/**
 * 投资手册的展示原语（v2 · 2026-11-24 排版系统重做）。
 *
 * 上一版的问题不是「不好看」，是「没有刻度」：全页 17 种字号，130+ 处落在
 * 8.5–11.5px；40 个圆角描边盒子；每节都挂一行 9px 的文件路径。结果是
 * 视线没有落点 —— 所有东西一样响，等于没有重点。
 *
 * 这一版的四条约束（改样式时请先读这四条，否则会退回上一版）：
 *   1) 字号只有八档：44 / 30 / 22 / 18 / 14 / 13 / 12 / 11。
 *      44 只给页头那个判定数，30 只给页标题，22 只给截面数字，18 只给节标题；
 *      正文 14，密表与表格单元 13，小字 12，元信息与标签 11。
 *      不再出现 9 / 9.5 / 10 / 10.5 / 12.5 / 13.5 这些「差半像素」的档位 ——
 *      它们不构成层级，只构成噪声。
 *   2) 分隔靠发丝线，不靠盒子。描边卡片只保留给「可点击的独立对象」，
 *      正文里的结论块一律用左侧 2px 竖线 + 留白。盒子越少，线越有意义。
 *   3) 颜色是语义，不是装饰：红/绿只表示涨跌（A 股约定），蓝只表示
 *      「当前 / 可达标 / 可交互」，琥珀只表示「数据有问题」，判定用「点 + 墨字」。
 *   4) 元信息（来源、数据日、口径）合并成一行，放在正文之后。读者不需要
 *      在读到第一句话之前先看五个文件路径。
 *
 * 只读依赖：@/lib/theme（跟随明暗）、echarts、lucide-react。
 */
import { useEffect, useRef, type ReactNode } from 'react'
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { ChevronRight } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useTheme } from '@/lib/theme'

export type Tone = 'ok' | 'muted' | 'warn' | 'danger'

/** 判定点的颜色。文字一律用墨色/次级色 —— 5px 的点足够表达语义，色块会抢注意力。 */
const TONE_DOT: Record<Tone, string> = {
  ok: 'bg-accent',
  warn: 'bg-warning',
  danger: 'bg-danger',
  muted: 'bg-muted/45',
}

/**
 * 判定标签。四档语义固定：ok=成立/可通过，warn=未证伪/待观察，danger=证伪/阻断，muted=不适用。
 * 用「点 + 文字」而不是实色药丸：表格里的 10 行判定不该变成一排色块。
 */
export function Tag({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-[12px] leading-[1.5] text-secondary">
      <span aria-hidden className={cn('h-[5px] w-[5px] shrink-0 rounded-full', TONE_DOT[tone])} />
      <span>{children}</span>
    </span>
  )
}

/**
 * 按节的更新频率。四档写死，和 Shell.TABS 的 cadence 用词同源。
 *
 * 存在理由：章节是「读什么」，频率是「什么时候读」。同一章里也会混频率
 * （执行每天变，规则只有预注册才变），所以频率必须能标到节上，不能只标到 tab 上。
 * 只有 daily 带蓝点：它是「这一节今天会变」的唯一外部提示，多了就成了装饰。
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
    <span className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap text-[11px] leading-none text-muted">
      {live && <span aria-hidden className="h-[5px] w-[5px] rounded-full bg-accent" />}
      <span className={live ? 'text-secondary' : undefined}>{CADENCE_LABEL[kind]}</span>
      {note && <span className="text-muted/80">· {note}</span>}
    </span>
  )
}

/**
 * 截面数字。k=指标名，v=读数，d=口径或对照（必须写清分母/样本，否则就是没有信息量的数字）。
 *
 * 视觉上是「测量条」而不是卡片：顶部一条发丝线 + 大号等宽数字。四个并列时，
 * 四条线连成一把尺子，比四个圆角框更容易横向比较。
 */
export function Stat({ k, v, d }: { k: string; v: string; d?: string }) {
  return (
    <div className="border-t border-border pt-3">
      <div className="text-[12px] leading-4 text-muted">{k}</div>
      <div className="mt-2 font-mono text-[22px] font-semibold leading-none tracking-[-0.02em] tabular-nums text-foreground">{v}</div>
      {d && <div className="mt-2 text-[12px] leading-[1.65] text-muted">{d}</div>}
    </div>
  )
}

export function StatGrid({ items, cols = 4 }: { items: { k: string; v: string; d?: string }[]; cols?: 2 | 3 | 4 }) {
  const grid = cols === 2 ? 'sm:grid-cols-2' : cols === 3 ? 'sm:grid-cols-3' : 'sm:grid-cols-2 md:grid-cols-4'
  return (
    <div className={cn('mt-5 grid grid-cols-2 gap-x-7 gap-y-6', grid)}>
      {items.map(x => <Stat key={x.k} {...x} />)}
    </div>
  )
}

/**
 * 分节容器。
 *
 * no 是「本视图内」的序号（01/02…），不承担跨视图引用职责 —— 跨视图引用请用
 * 「案卷 B」这类带语义的名字或锚点，避免改版一次把所有引用都变成错的。
 *
 * 来源与数据日合成一行元信息（meta），放在导语之后、正文之前：够得着，但不挡路。
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
  const hasMeta = Boolean(source || asOf || stale || drift)
  return (
    <section id={id} className={cn('scroll-mt-28', first ? '' : 'mt-16', className)}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-2 border-b border-border pb-3">
        <h2 className="flex min-w-0 items-baseline gap-3 text-[18px] font-semibold leading-[1.4] tracking-[-0.01em] text-foreground">
          {no && <span className="shrink-0 font-mono text-[11px] font-medium tabular-nums text-muted">{no}</span>}
          <span className="min-w-0">{title}</span>
        </h2>
        {actions}
      </div>
      {lead && <div className="mt-4 max-w-[46em] text-[14px] leading-[1.8] text-secondary">{lead}</div>}
      {hasMeta && (
        <div className="mt-3 flex flex-wrap items-baseline gap-x-2.5 gap-y-1 text-[11px] leading-[1.6] text-muted">
          {stale && (
            <span className="font-medium text-warning">行情落后 {lag} 个交易日（补数据后重跑，不要照旧数下单）</span>
          )}
          {drift && <span className="font-medium text-warning">{drift}</span>}
          {asOf && <span className="font-mono tabular-nums">数据日 {asOf}</span>}
          {asOf && source && <span aria-hidden className="text-border">·</span>}
          {source && <span className="break-all">{source}</span>}
        </div>
      )}
      <div className="mt-6">{children}</div>
    </section>
  )
}

const TONE_RULE: Record<Tone, string> = {
  ok: 'border-l-accent',
  warn: 'border-l-warning',
  danger: 'border-l-danger',
  muted: 'border-l-border',
}

/**
 * 结论条。label 写「这句话是什么性质」（结论/边界/口径），避免读者把它当又一段论述。
 *
 * 视觉上用 2px 左边线而不是四边描边：一页里出现四五次时，色块会把版面切碎。
 * 只有 warn（数据/执行风险）带一层 5% 底色 —— 它是操作性的，需要比结论更早被看到。
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
    <div
      className={cn(
        'mt-6 border-l-2 pl-4 text-[14px] leading-[1.8] text-foreground',
        TONE_RULE[tone],
        className,
      )}
    >
      {label && <div className="mb-1.5 text-[11px] font-medium tracking-[0.14em] text-muted">{label}</div>}
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

/**
 * 唯一表格实现。reason=这张表支撑什么判断，写在表上方，防止表格退化成数据倾倒。
 *
 * 无外框、无斑马线：表头一条线、行间一条 60% 的线、首尾列去掉内边距 —— 表格与
 * 正文左边缘对齐，读者不会觉得「这里嵌了一个控件」。
 */
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
  const cell = dense ? 'px-3 py-2' : 'px-3 py-3'
  const min = minWidth ?? (cols.length >= 7 ? 820 : cols.length >= 5 ? 680 : cols.length >= 3 ? 540 : 0)
  const fixed = cols.some(c => c.w)
  return (
    <div className="mt-4">
      {reason && <div className="max-w-[46em] text-[12px] leading-[1.7] text-muted">{reason}</div>}
      <div className="relative mt-2">
        <div className="overflow-x-auto overscroll-x-contain">
          <table
            className={cn('w-full border-collapse', dense ? 'text-[13px]' : 'text-[13px]', fixed && 'table-fixed')}
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
                  <th
                    key={c.head}
                    className={cn(
                      'border-b border-border text-left align-bottom text-[11px] font-medium leading-[1.4] tracking-[0.06em] text-muted first:pl-0 last:pr-0',
                      cell, c.align === 'right' && 'text-right', c.headCls,
                    )}
                  >
                    {c.head}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={rowKey(r, i)} className="transition-colors hover:bg-elevated/40">
                  {cols.map(c => (
                    <td
                      key={c.head}
                      className={cn(
                        'border-b border-border/60 text-left align-top leading-[1.65] text-foreground first:pl-0 last:pr-0',
                        cell, c.align === 'right' && 'text-right', c.cls,
                      )}
                    >
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
        <div className="mt-1.5 text-[11px] text-muted sm:hidden">左右滑动查看完整表格 →</div>
      )}
    </div>
  )
}

/**
 * 发丝线瓦片：一组并列的短条目（买点六问、触发后四步、案卷索引）。
 * 不用卡片，是为了让并列关系由「对齐 + 等距」表达，而不是由四个框表达。
 */
export function Tiles({ cols = 2, children, className }: { cols?: 2 | 3; children: ReactNode; className?: string }) {
  return (
    <div className={cn('mt-5 grid gap-x-8 gap-y-7', cols === 3 ? 'sm:grid-cols-2 lg:grid-cols-3' : 'sm:grid-cols-2', className)}>
      {children}
    </div>
  )
}

export function Tile({
  lead, aside, title, body, foot, className,
}: {
  lead?: ReactNode
  aside?: ReactNode
  title: ReactNode
  body?: ReactNode
  foot?: ReactNode
  className?: string
}) {
  return (
    <div className={cn('flex flex-col border-t border-border pt-3', className)}>
      {(lead || aside) && (
        <div className="flex items-baseline justify-between gap-3 text-[11px] leading-4 text-muted">
          <span className="font-mono tabular-nums">{lead}</span>
          {aside}
        </div>
      )}
      <div className="mt-1.5 text-[14px] font-medium leading-[1.5] text-foreground">{title}</div>
      {body && <div className="mt-1.5 text-[12px] leading-[1.7] text-secondary">{body}</div>}
      {foot && <div className="mt-auto pt-2.5">{foot}</div>}
    </div>
  )
}

/** 空态：不画虚线框。一句话 + 一条发丝线，读者知道这里没内容就够了。 */
export function Empty({ text }: { text: ReactNode }) {
  return (
    <div className="mt-5 border-t border-border pt-4 text-[13px] leading-[1.7] text-muted">{text}</div>
  )
}

/** 折叠块。反证案卷专用：内容常驻 DOM（可搜索、可被测试读到），只是默认不占视线。 */
export function Fold({ id, title, hint, children }: { id: string; title: string; hint?: string; children: ReactNode }) {
  return (
    <details id={id} className="group scroll-mt-28 border-t border-border">
      <summary className="flex cursor-pointer select-none list-none items-baseline gap-3 py-3.5 text-[14px] font-medium leading-snug text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-3.5 w-3.5 shrink-0 translate-y-0.5 text-muted transition-transform duration-150 group-open:rotate-90" />
        <span className="min-w-0">{title}</span>
        {hint && <span className="ml-auto hidden shrink-0 text-[11px] font-normal text-muted sm:block">{hint}</span>}
      </summary>
      <div className="pb-8 pt-1">{children}</div>
    </details>
  )
}

/** 超额收益统一写法：正数带 +，负数用真减号 U+2212（ASCII '-' 在密集数字里读起来像连字符）。 */
export function fmtBp(v: number): string {
  if (v === 0) return '0'
  return `${v > 0 ? '+' : '−'}${Math.abs(v)}`
}

/**
 * 图表坐标轴配色，跟随主题。
 *
 * 红=正、绿=负，与 A 股约定一致；蓝只留给「当前/本规则」这类被强调的序列，
 * 中性灰留给对照组。上一版用蓝色表示正收益、红色表示负收益 —— 那一页里
 * 红色同时意味着「涨」和「差」，两个意思互相稀释。
 */
export function useAxis() {
  const dark = useTheme() === 'dark'
  return {
    dark,
    axisText: dark ? '#b4b2a9' : '#5f5e5a',
    axisLine: dark ? '#3f3f3c' : '#e3e2dd',
    /** 正收益 */
    barUp: dark ? '#F07167' : '#C0392B',
    /** 负收益 / 左尾 */
    barDown: dark ? '#3DBE7C' : '#15803D',
    /** 对照组 / 历史 */
    barAlt: dark ? '#5C5C57' : '#C3C1BA',
    /** 被强调的当前序列 */
    barFocus: dark ? '#85B7EB' : '#185FA5',
  }
}

/** 图表壳。hidden 面板里 0 宽时不要 init，等 ResizeObserver 拿到宽度再建实例。 */
export function Chart({ option, height = 260, className }: { option: EChartsOption; height?: number; className?: string }) {
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

/** 图表卡片：标题 + 口径 + 读法，避免图表裸奔成一堵墙。发丝线顶边，不加底色。 */
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
    <div className={cn('border-t border-border pt-3', className)}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="text-[13px] font-medium text-foreground">{title}</div>
        {hint && <div className="font-mono text-[11px] text-muted">{hint}</div>}
      </div>
      <div className="mt-2">{children}</div>
      {footer && <div className="mt-2 text-[12px] leading-[1.7] text-muted">{footer}</div>}
    </div>
  )
}
