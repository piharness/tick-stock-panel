/**
 * 投资手册的六章外壳：章节定义、tab 条、章内目录、跨章跳转。
 *
 * 这一页是「一本手册」，不是一个看板：它按专业交易员写策略文件的方式组织 ——
 *   总则   冻结          为什么做、不做什么、边界在哪（改它等于推翻一次预注册）
 *   证据   按实验        凭什么信：证据强度、验收、组合基准
 *   规则   预注册        什么条件下做：买点、不买、仓位、退出
 *   执行   每日盘后      今天做什么：扫描、账户指令、判定、账本、四步
 *   反证   冻结只追加    什么已经被否掉：四组复盘
 *   工具   按需          用什么做：回放台、数据链路、维护
 *
 * 章节 = 决策链的一环 + 一种更新频率。这两件事在这一版里终于对齐了：
 * 总则只随裁决改、证据只在重跑时变、规则预注册后不动、执行每天变、反证只追加、工具按需。
 * （上一版把「每日」和「预注册」塞进同一层，才需要在一层里再分两个 tab。）
 *
 * 结构契约（nav.test.tsx 守）：
 *   - TABS 的 key 与 TabPanel 的 id 一一对应；
 *   - 所有面板常驻 DOM，非当前视图用 hidden 隐藏（可被检索/测试读到）；
 *   - 默认视图仍是「执行」，因为它是唯一每天需要看的一章。
 */
import {
  createContext, useContext, useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode,
} from 'react'
import {
  Archive as ArchiveIcon, ArrowRight, BarChart3, BookOpen, Clock3, ListChecks, Radar, Wrench, type LucideIcon,
} from 'lucide-react'
import { cn } from '@/lib/cn'

export type TabKey = 'mandate' | 'evidence' | 'rules' | 'today' | 'archive' | 'tools'

export interface TabDef {
  key: TabKey
  label: string
  /** 更新频率。写法固定：每日 / 按实验 / 冻结 / 按需，工具章的表与这里同源。 */
  cadence: string
  /** 频率的具体含义，一句话。 */
  hint: string
  /** 这一章的数据/文档入口。 */
  source: string
  icon: LucideIcon
}

export const TABS: TabDef[] = [
  {
    key: 'mandate',
    label: '总则',
    cadence: '冻结 · 只随裁决改',
    hint: '这一章只放已被数据裁决过的判断；改它等于推翻一次预注册',
    source: 'docs/structural-edge/README.md · verdict.md · cost-vs-return.md',
    icon: BookOpen,
  },
  {
    key: 'evidence',
    label: '证据',
    cadence: '按实验 · 重跑才变',
    hint: '图表就是阈值的来历；阈值不在这里调',
    source: 'docs/structural-edge/*.md · analysis/_diag_* · GET /api/position-art',
    icon: BarChart3,
  },
  {
    key: 'rules',
    label: '规则',
    cadence: '预注册 · 不轻改',
    hint: '改动只发生在 data.ts 与对应 md；平时只读',
    source: 'industry-etf-asymmetry.md · theme-etf-entry.md · holding-and-exit.md',
    icon: ListChecks,
  },
  {
    key: 'today',
    label: '执行',
    cadence: '每日 · 盘后',
    hint: '收盘后由 analysis/etf_watch.py 重写；页面不内联副本',
    source: 'GET /api/etf-watch · GET /api/etf-watch/account · GET /api/position-art',
    icon: Radar,
  },
  {
    key: 'archive',
    label: '反证',
    cadence: '冻结 · 只追加',
    hint: '已有裁决不重写；新证据另开一条，不覆盖旧结论',
    source: 'docs/structural-edge/ 四组案卷（A–D）· analysis/_diag_* 可重跑',
    icon: ArchiveIcon,
  },
  {
    key: 'tools',
    label: '工具',
    cadence: '按需',
    hint: '回放台与维护入口，不参与交易决策',
    source: 'analysis/etf_watch_replay.py · CONTRIBUTING.md §7 / §9 · data.ts',
    icon: Wrench,
  },
]

export const TAB_KEYS = TABS.map(t => t.key)

/**
 * 章节锚点 → 所属章。用于文档与旧链接。
 * 旧名（道 / 法 / 器）与旧 tab（#dao / #fa / #qi）必须继续落到同一块内容上 ——
 * 否则一次改名会把所有书签和文档引用变成「打开了页面但停在顶部」。
 */
export const SECTION_TAB: Record<string, TabKey> = {
  mandate: 'mandate', refuses: 'mandate', composition: 'mandate', boundary: 'mandate',
  dao: 'mandate',
  grade: 'evidence', position: 'evidence', breadth: 'evidence', duration: 'evidence', waits: 'evidence',
  portfolio: 'evidence', 'sizing-audit': 'evidence', evidence: 'evidence', fa: 'evidence',
  buy: 'rules', stop: 'rules', sizing: 'rules', exits: 'rules', rules: 'rules',
  today: 'today', register: 'today', scan: 'today', ledger: 'today', next: 'today',
  archive: 'archive', 'archive-a': 'archive', 'archive-b': 'archive', 'archive-c': 'archive', 'archive-d': 'archive',
  review: 'archive',
  replay: 'tools', 'replay-day': 'tools', 'replay-track': 'tools', 'replay-outcome': 'tools',
  maint: 'tools', flow: 'tools', docs: 'tools', discipline: 'tools', qi: 'tools', tools: 'tools',
}

export function tabFromHash(hash = window.location.hash): TabKey {
  const h = hash.replace(/^#/, '')
  if (!h) return 'today'
  if ((TAB_KEYS as string[]).includes(h)) return h as TabKey
  return SECTION_TAB[h] ?? 'today'
}

export function scrollToId(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  if (window.location.hash !== `#${id}`) window.history.replaceState(null, '', `#${id}`)
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

interface PageNav {
  go: (tab: TabKey, anchor?: string) => void
}

const PageNavContext = createContext<PageNav>({ go: () => {} })

export function usePageNav() {
  return useContext(PageNavContext)
}

export function PageNavProvider({ value, children }: { value: PageNav; children: ReactNode }) {
  return <PageNavContext.Provider value={value}>{children}</PageNavContext.Provider>
}

/** 跨视图跳转。用 button 而不是 a：切换视图后还要控制滚动位置，a 的默认行为会先跳。 */
export function CrossLink({
  to, anchor, children, className,
}: {
  to: TabKey
  anchor?: string
  children: ReactNode
  className?: string
}) {
  const { go } = usePageNav()
  return (
    <button
      type="button"
      onClick={() => go(to, anchor)}
      className={cn('inline-flex items-center gap-1 rounded-sm text-[11px] font-medium text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40', className)}
    >
      {children}
      <ArrowRight className="h-3 w-3" />
    </button>
  )
}

/** 顶层视图切换。固定在滚动容器顶部，长页里也能随时换视图。 */
export function PageTabs({
  active, onChange, badge,
}: {
  active: TabKey
  onChange: (key: TabKey) => void
  badge?: number
}) {
  const btns = useRef<Array<HTMLButtonElement | null>>([])
  const listRef = useRef<HTMLDivElement>(null)

  // 窄屏 tab 条横向滚动：切到靠右的视图（或深链进来）时，把当前 tab 滚到中间。
  useEffect(() => {
    const i = TABS.findIndex(t => t.key === active)
    const btn = btns.current[i]
    const list = listRef.current
    if (!btn || !list) return
    const listRect = list.getBoundingClientRect()
    const btnRect = btn.getBoundingClientRect()
    if (listRect.width === 0) return
    const target = list.scrollLeft + btnRect.left - listRect.left - (listRect.width - btnRect.width) / 2
    if (typeof list.scrollTo === 'function') list.scrollTo({ left: Math.max(0, target), behavior: 'smooth' })
  }, [active])

  const onKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>, i: number) => {
    let next = -1
    if (e.key === 'ArrowRight') next = (i + 1) % TABS.length
    else if (e.key === 'ArrowLeft') next = (i - 1 + TABS.length) % TABS.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = TABS.length - 1
    if (next < 0) return
    e.preventDefault()
    btns.current[next]?.focus()
    onChange(TABS[next].key)
  }

  return (
    <div className="sticky top-0 z-20 -mx-6 mt-5 border-b border-border bg-base/95 backdrop-blur supports-[backdrop-filter]:bg-base/80">
      <div
        ref={listRef}
        role="tablist"
        aria-label="投资手册章节"
        className="flex gap-0.5 overflow-x-auto px-6 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
      >        {TABS.map((t, i) => {
          const Icon = t.icon
          const on = active === t.key
          return (
            <button
              key={t.key}
              ref={el => { btns.current[i] = el }}
              id={`se-tab-${t.key}`}
              type="button"
              role="tab"
              aria-selected={on}
              aria-controls={`se-panel-${t.key}`}
              tabIndex={on ? 0 : -1}
              onClick={() => onChange(t.key)}
              onKeyDown={e => onKeyDown(e, i)}
              className={cn(
                'group relative flex shrink-0 items-center gap-2 border-b-2 px-3 pb-2.5 pt-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/50',
                on ? 'border-accent' : 'border-transparent hover:border-border',
              )}
            >
              <Icon className={cn('h-3.5 w-3.5 shrink-0 transition-colors', on ? 'text-accent' : 'text-muted group-hover:text-secondary')} />
              <span className="flex flex-col">
                <span className={cn('text-[12.5px] font-medium leading-tight transition-colors', on ? 'text-foreground' : 'text-secondary group-hover:text-foreground')}>
                  {t.label}
                  {t.key === 'today' && badge != null && badge > 0 && (
                    <span className="ml-1.5 rounded-full bg-accent/15 px-1.5 py-px font-mono text-[9.5px] tabular-nums text-accent">{badge}</span>
                  )}
                </span>
                <span className="mt-0.5 hidden text-[9.5px] leading-tight text-secondary sm:block">{t.cadence}</span>              </span>
            </button>
          )
        })}
      </div>
      <div aria-hidden className="pointer-events-none absolute inset-y-0 left-0 w-10 bg-gradient-to-r from-base via-base/80 to-transparent lg:hidden" />
      <div aria-hidden className="pointer-events-none absolute inset-y-0 right-0 w-10 bg-gradient-to-l from-base via-base/80 to-transparent lg:hidden" />
    </div>
  )
}

export interface RailItem {
  id: string
  no: string
  label: string
  /** 自定义跳转（案卷折叠需要先 open 再 scroll）。不给则按锚点滚动。 */
  onSelect?: () => void
}

/** 视图内目录。桌面端常驻左侧；窄屏由 TabLayout 的 meta 条替代。 */
export function SectionRail({
  items, cadence, hint, source,
}: {
  items: RailItem[]
  cadence: string
  hint: string
  source: string
}) {
  const [active, setActive] = useState(items[0]?.id ?? '')
  const ids = items.map(i => i.id).join('|')

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return
    const obs = new IntersectionObserver(
      entries => {
        const visible = entries
          .filter(e => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (visible[0]?.target.id) setActive(visible[0].target.id)
      },
      { rootMargin: '-120px 0px -62% 0px', threshold: 0 },
    )
    for (const id of ids.split('|')) {
      const el = document.getElementById(id)
      if (el) obs.observe(el)
    }
    return () => obs.disconnect()
  }, [ids])

  const jump = (item: RailItem) => {
    if (item.onSelect) {
      item.onSelect()
      return
    }
    scrollToId(item.id)
  }

  return (
    <nav aria-label="本页目录" className="sticky top-[74px] hidden self-start xl:block">
      <div className="text-[9.5px] font-medium tracking-[0.14em] text-secondary">本页目录</div>
      <ul className="mt-2 border-l border-border">
        {items.map(item => (
          <li key={item.id}>
            <button
              type="button"
              onClick={() => jump(item)}
              aria-current={active === item.id ? 'true' : undefined}
              className={cn(
                '-ml-px flex w-full items-baseline gap-2 border-l-2 py-1.5 pl-3 pr-2 text-left text-[11.5px] leading-snug transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40',
                active === item.id
                  ? 'border-accent text-foreground'
                  : 'border-transparent text-secondary hover:border-border hover:text-foreground',
              )}
            >
              <span className="shrink-0 font-mono text-[9.5px] text-muted">{item.no}</span>
              <span className="min-w-0">{item.label}</span>
            </button>
          </li>
        ))}
      </ul>

      <div className="mt-4 rounded-lg border border-border bg-surface/60 p-3">
        <div className="flex items-center gap-1.5 text-[9.5px] font-medium tracking-wide text-secondary">
          <Clock3 className="h-3 w-3" />
          更新频率
        </div>
        <div className="mt-1 text-[11px] font-medium text-foreground">{cadence}</div>
        <div className="mt-1 text-[10px] leading-relaxed text-secondary">{hint}</div>
        <div className="mt-2 break-all border-t border-border/70 pt-2 font-mono text-[9px] leading-relaxed text-secondary">{source}</div>
      </div>
    </nav>
  )
}

/** 每个视图的统一版式：左侧目录 + 右侧正文，窄屏退化为顶部一条频率说明。 */
export function TabLayout({ tab, items, children }: { tab: TabKey; items: RailItem[]; children: ReactNode }) {
  const def = TABS.find(t => t.key === tab) ?? TABS[0]
  return (
    <div className="mt-6 grid gap-8 xl:grid-cols-[184px_minmax(0,1fr)]">
      <SectionRail items={items} cadence={def.cadence} hint={def.hint} source={def.source} />
      <div className="min-w-0">
        <div className="mb-6 flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-lg border border-border bg-surface/50 px-3 py-2 xl:hidden">
          <Clock3 className="h-3 w-3 shrink-0 self-center text-muted" />
          <span className="text-[10.5px] font-medium text-secondary">{def.cadence}</span>
          <span className="text-[10px] text-secondary">{def.hint}</span>
        </div>
        {children}
      </div>
    </div>
  )
}
