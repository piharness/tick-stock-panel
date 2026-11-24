/**
 * 策略图书馆 —— 理论与证据的沉淀页（/library）。
 *
 * 2026-11-24 手册拆成两页，这一页收走「不每天变」的全部内容：
 *   总则   冻结          在赌什么、哪些直觉已被推翻、边界在哪
 *   证据   按实验        凭什么信：阈值来历、验收、组合基准、第二条腿
 *   规则   预注册        买什么、买多少、何时不买、何时卖（下单前读）
 *   反证   冻结只追加    这条路是不是已经测过并被否掉
 *   工具   按需          回放某一天、这一页怎么维护
 *
 * 每天要看的「执行」不再是这里的一章，而是独立页面 /handbook（机会雷达）。
 * 拆分的判据是更新频率与使用频率：图书馆是「查」，雷达是「看」。两页共享
 * useWatch.ts 的同一批接口，但互不依赖对方的渲染 —— 图书馆挂了不影响下单读数。
 *
 * 结构契约（nav.test.tsx 守）：五个 tab 与五个 panel 一一对应；所有面板常驻 DOM；
 * 非当前视图用 hidden 隐藏；默认视图是「总则」（图书馆从第一页翻开）。
 *
 * 归属：核心页面，经 lib/navOrder 登记路径 /library（侧栏「策略图书馆」）。
 * 旧路径 /long-strategy、/structural-edge、/position-art 由 router 按 hash 分流：
 * 理论锚点 → 这里，执行锚点 → 机会雷达。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ArchivePanel, openArchiveFold } from './Archive'
import { EvidencePanel } from './Evidence'
import { MandatePanel } from './Mandate'
import { RulesPanel } from './Rules'
import { PageNavProvider, PageTabs, TAB_KEYS, scrollToId, tabFromHash, type TabKey } from './Shell'
import { ToolsPanel } from './Tools'
import { useWatchData } from './useWatch'

export function Library() {
  const [active, setActive] = useState<TabKey>(() => tabFromHash())
  const [confirmDays, setConfirmDays] = useState(10)
  const rootRef = useRef<HTMLDivElement>(null)
  // 首屏 hash 只在挂载时读一次：之后切 tab 会重写 hash，不能再拿它当深链。
  const firstHash = useRef(window.location.hash.replace(/^#/, ''))

  // 图书馆只用扫描快照（广度读数、V1 判据）与反例读数；现金腿是每日页的职责。
  const { payload, account, art } = useWatchData({ confirmDays })

  useEffect(() => {
    const onHash = () => {
      const h = window.location.hash.replace(/^#/, '')
      setActive(tabFromHash())
      if (h.startsWith('archive-')) {
        window.setTimeout(() => openArchiveFold(h.slice('archive-'.length)), 120)
      } else if (h && !(TAB_KEYS as string[]).includes(h)) {
        window.setTimeout(() => scrollToId(h), 120)
      }
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  // 文档里的 #buy / #grade 这类老锚点：先切到所属章，渲染后再滚。
  useEffect(() => {
    const h = firstHash.current
    if (!h || (TAB_KEYS as string[]).includes(h) || h.startsWith('archive-')) return
    const timer = window.setTimeout(() => scrollToId(h), 100)
    return () => window.clearTimeout(timer)
  }, [])

  const go = useCallback((tab: TabKey, anchor?: string) => {
    setActive(tab)
    const hash = anchor ?? tab
    if (window.location.hash !== `#${hash}`) window.history.replaceState(null, '', `#${hash}`)
    const scroller = rootRef.current?.closest('main')
    if (scroller) scroller.scrollTop = 0
    if (anchor?.startsWith('archive-')) {
      window.setTimeout(() => openArchiveFold(anchor.slice('archive-'.length)), 100)
    } else if (anchor) {
      window.setTimeout(() => scrollToId(anchor), 80)
    }
  }, [])

  const nav = useMemo(() => ({ go }), [go])
  const artErr = art.error ? (art.error as Error).message : ''

  return (
    <PageNavProvider value={nav}>
      <div ref={rootRef} className="mx-auto max-w-[1180px] px-6 pb-24 pt-8">
        <header className="flex flex-col gap-7 border-b border-border pb-7 sm:flex-row sm:items-end sm:justify-between">
          <div className="min-w-0">
            <div className="text-[11px] font-medium tracking-[0.18em] text-muted">
              策略图书馆 · 理论与证据 · 低频更新
            </div>
            <h1 className="mt-3 text-[30px] font-semibold leading-[1.15] tracking-[-0.02em] text-foreground">
              策略图书馆<span className="mx-2.5 font-light text-muted/40">/</span>事件低吸
            </h1>
            <p className="mt-3 max-w-[44em] text-[14px] leading-[1.8] text-secondary">
              这里只放结论：总则与规则冻结，证据只在重跑实验时变，反证只追加。
              读到旧数，说明没人重跑实验，不代表市场还在那个状态。
              每天该看的机会在「机会雷达」。
            </p>
          </div>
          <LibraryMeta payloadAsOf={payload ? payload.meta.asof : null} artAsOf={art.data?.as_of ?? null} />
        </header>

        <PageTabs active={active} onChange={go} />

        <Panel id="mandate" active={active}><MandatePanel payload={payload} account={account} /></Panel>
        <Panel id="evidence" active={active}>
          <EvidencePanel
            payload={payload}
            art={art.data ?? null}
            artErr={artErr}
            artFetching={art.isFetching}
            confirmDays={confirmDays}
            onConfirmDays={setConfirmDays}
            onRetry={() => void art.refetch()}
          />
        </Panel>
        <Panel id="rules" active={active}><RulesPanel /></Panel>
        <Panel id="archive" active={active}><ArchivePanel /></Panel>
        <Panel id="tools" active={active}><ToolsPanel active={active === 'tools'} /></Panel>

        <footer className="mt-20 flex flex-wrap items-start justify-between gap-x-8 gap-y-3 border-t border-border pt-4 text-[11px] leading-[1.7] text-muted">
          <span className="max-w-[52em]">
            本页只记录结论，不构成投资建议。静态数字来自{' '}
            <code className="font-mono text-secondary">data.ts</code>，活读数来自{' '}
            <code className="font-mono text-secondary">/api/etf-watch</code> 与{' '}
            <code className="font-mono text-secondary">/api/position-art</code>。
          </span>
          <span className="shrink-0 font-mono tracking-[0.08em]">library · five chapters</span>
        </footer>
      </div>
    </PageNavProvider>
  )
}

/**
 * 页头右侧的「最近更新」：图书馆不是实时页，它需要回答的是
 * 「这些结论最后一次被数据 touching 是什么时候」，而不是今天的行情。
 */
function LibraryMeta({ payloadAsOf, artAsOf }: { payloadAsOf: string | null; artAsOf: string | null }) {
  return (
    <div className="flex shrink-0 flex-col items-start border-l-2 border-border py-0.5 pl-4 sm:w-[216px]">
      <span className="text-[11px] font-medium tracking-[0.14em] text-muted">最近更新</span>
      <span className="mt-2.5 text-[15px] font-medium leading-none text-foreground">
        {payloadAsOf ?? '—'}
      </span>
      <span className="mt-2 text-[11.5px] leading-[1.6] text-muted">
        扫描快照{artAsOf ? ` · 反例登记 ${artAsOf}` : ''}
      </span>
    </div>
  )
}

/** 所有面板常驻 DOM；hidden 只影响可见性，不影响检索与结构测试。 */
function Panel({ id, active, children }: { id: TabKey; active: TabKey; children: ReactNode }) {
  return (
    <div
      role="tabpanel"
      id={`se-panel-${id}`}
      aria-labelledby={`se-tab-${id}`}
      hidden={active !== id}
      tabIndex={0}
      className="focus-visible:outline-none"
    >
      {children}
    </div>
  )
}

/** 兼容旧命名：router 旧入口与测试仍按 StructuralEdge / LongStrategy / Handbook 导入。 */
export { Library as Handbook, Library as LongStrategy, Library as StructuralEdge }
