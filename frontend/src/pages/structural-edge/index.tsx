/**
 * 投资手册 · 事件低吸 —— 把「结构优势」与「仓位艺术」合成一个策略的六章页。
 *
 * 为什么是一本手册，而不是一个看板：
 *   专业交易员写的策略文件有固定的骨架 —— 总则（授权与边界）、证据（凭什么信）、
 *   规则（什么条件下做）、执行（今天做什么）、反证（什么已经被否掉）、工具（用什么做）。
 *   这六章的顺序就是决策链的顺序，也是更新频率从慢到快的顺序。
 *
 * 章节 = 决策链的一环 + 一种频率（nav.test.tsx 守）：
 *   总则   冻结          动摇时看：在赌什么、哪些直觉已被推翻、边界在哪
 *   证据   按实验        复盘时看：证据、验收、组合基准、第二条腿
 *   规则   预注册        下单前看：买什么、买多少、何时不买、何时卖
 *   执行   每日盘后      收盘后 3 分钟：扫描、账户指令、反例登记
 *   反证   冻结只追加    遇到旧说法时看：这条路是不是已经测过并被否掉
 *   工具   按需          回放某一天、改这一页时看
 *
 * 两次合并的边界：
 *   1) 可执行部分（规则/执行）与被否掉的部分（反证）仍然分开 —— 这条边界现在落在两章之间；
 *   2) 被证伪的仓位规则（证据·仓位择时验收 + 执行·反例登记）与未被证伪的买点规则同页 ——
 *      这是合并「仓位艺术」真正的收益：两条规则的读数第一次出现在同一屏，
 *      「今天不买」有两个来源，而它们不能互相替代。
 *
 * 结构契约（nav.test.tsx 守）：六个 tab 与六个 panel 一一对应；所有面板常驻 DOM；
 * 非当前视图用 hidden 隐藏；默认视图是「执行」。
 *
 * 归属：核心页面，经 lib/navOrder 登记路径 /handbook（侧栏「投资手册」），
 * /long-strategy、/structural-edge 与 /position-art 由 router 重定向到这里。
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type EtfWatchAccount, type EtfWatchCashLeg, type EtfWatchPayload } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { ArchivePanel, openArchiveFold } from './Archive'
import { EvidencePanel } from './Evidence'
import { ExecutionPanel, ExecutionStatus } from './Execution'
import { MandatePanel } from './Mandate'
import { RulesPanel } from './Rules'
import { PageNavProvider, PageTabs, TAB_KEYS, scrollToId, tabFromHash, usePageNav, type TabKey } from './Shell'
import { ToolsPanel } from './Tools'
import { coverageGap } from './live'

export function Handbook() {
  const [active, setActive] = useState<TabKey>(() => tabFromHash())
  const [payload, setPayload] = useState<EtfWatchPayload | null>(null)
  const [err, setErr] = useState('')
  const [account, setAccount] = useState<EtfWatchAccount | null>(null)
  const [accountErr, setAccountErr] = useState('')
  const [cashLeg, setCashLeg] = useState<EtfWatchCashLeg | null>(null)
  const [cashErr, setCashErr] = useState('')
  const [confirmDays, setConfirmDays] = useState(10)
  const rootRef = useRef<HTMLDivElement>(null)
  // 首屏 hash 只在挂载时读一次：之后切 tab 会重写 hash，不能再拿它当深链。
  const firstHash = useRef(window.location.hash.replace(/^#/, ''))

  useEffect(() => {
    let alive = true
    api.etfWatch()
      .then(d => { if (alive) { setPayload(d); setErr('') } })
      .catch((e: Error) => { if (alive) setErr(e.message) })
    api.etfWatchAccount?.()
      .then(d => { if (alive) { setAccount(d); setAccountErr('') } })
      .catch((e: Error) => { if (alive) setAccountErr(e.message) })
    // 现金腿与账户指令同源（同一次 analysis 运行产出），任何一边失败都不影响另一边渲染。
    const legReq = api.etfWatchCashLeg
    if (legReq) {
      legReq()
        .then(d => { if (alive) { setCashLeg(d); setCashErr('') } })
        .catch((e: Error) => { if (alive) setCashErr(e.message) })
    }
    return () => { alive = false }
  }, [])

  // 仓位规则的验收与「反例登记」共用这一份 —— 参数只在这里有一处，两章不会各取一个值。
  const art = useQuery({
    queryKey: QK.positionArt(confirmDays),
    queryFn: () => api.positionArt(confirmDays),
    staleTime: 5 * 60 * 1000,
    retry: false,
  })

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
  // 行情缺口：>0 时全页的「执行」读数都不可直接用于下单，必须在页头说出来。
  const lag = payload?.data.lag_sessions ?? 0
  // 链路缺口：分区已有今天、扫描还停在上一天 —— 这是「脚本没跑」，不是「行情没到」。
  const gap = coverageGap(payload)

  return (
    <PageNavProvider value={nav}>
      <div ref={rootRef} className="mx-auto max-w-6xl px-6 pb-16 pt-6">
        <header className="flex flex-col gap-4 border-b border-border/70 pb-5 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[10px] font-medium tracking-[0.16em] text-secondary">
              <span className="h-1 w-1 rounded-full bg-accent" />
              中长期策略 · 研究与实践 · 纸面账户
            </div>
            <h1 className="mt-1.5 text-[22px] font-semibold tracking-tight text-foreground">
              投资手册 <span className="text-muted/50">/</span> 事件低吸
            </h1>
            <ChapterMap />
            <p className="mt-1 text-[11.5px] leading-relaxed text-secondary sm:hidden">
              六章按决策顺序排；每天只看「执行」。
            </p>
            <p className="mt-2 hidden max-w-2xl text-[11px] leading-relaxed text-muted sm:block">
              每天会被重写的是「执行」：扫描、账户指令、现金腿、反例登记。其余五章是结论 ——
              读到旧数说明没人重跑实验，不代表市场还在那个状态。反例读数在执行 07 与证据 04 各出现一次，同源同参。
            </p>
            {lag > 0 && (
              <p className="mt-1.5 max-w-2xl text-[11px] font-medium leading-relaxed text-warning">
                行情落后 {lag} 个交易日：先补数据再重跑扫描，这一页的判定不能直接用于下单。
              </p>
            )}
            {gap && (
              <p className="mt-1.5 max-w-2xl text-[11px] font-medium leading-relaxed text-warning">
                {gap.text}：今天的盘后链路没跑，这一页（含「工具 · 交易日期」）停在上一个交易日。
                补跑 <code className="rounded bg-elevated px-1 py-0.5 font-mono text-[10px]">analysis/etf_watch.py</code>、
                <code className="rounded bg-elevated px-1 py-0.5 font-mono text-[10px]">etf_watch_account.py</code>、
                <code className="rounded bg-elevated px-1 py-0.5 font-mono text-[10px]">etf_watch_replay.py</code>。
              </p>
            )}
          </div>
          <ExecutionStatus payload={payload} err={err} onOpen={() => go('today')} />
        </header>

        <PageTabs active={active} onChange={go} badge={payload?.counts.candidate} />

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
        <Panel id="today" active={active}>
          <ExecutionPanel
            payload={payload}
            err={err}
            account={account}
            accountErr={accountErr}
            cashLeg={cashLeg}
            cashErr={cashErr}
            art={art.data ?? null}
            artErr={artErr}
          />
        </Panel>
        <Panel id="archive" active={active}><ArchivePanel /></Panel>
        <Panel id="tools" active={active}><ToolsPanel active={active === 'tools'} /></Panel>

        <footer className="mt-12 flex flex-wrap items-center justify-between gap-2 border-t border-border pt-4 text-[10px] leading-relaxed text-secondary">
          <span>
            本页记录结论与纸面指令，不构成投资建议。静态数字来自 <code className="rounded bg-elevated px-1 py-0.5 font-mono">data.ts</code>，
            活数据来自 <code className="rounded bg-elevated px-1 py-0.5 font-mono">/api/etf-watch</code>（扫描 / 账户 / 现金腿）与{' '}
            <code className="rounded bg-elevated px-1 py-0.5 font-mono">/api/position-art</code>（反例读数）。
          </span>
          <span className="font-mono">handbook · 事件低吸 · six chapters</span>
        </footer>
      </div>
    </PageNavProvider>
  )
}

/** 页头的六章地图：可点，不再是六行死文字。 */
function ChapterMap() {
  const { go } = usePageNav()
  const items: { key: TabKey; label: string; text: string }[] = [
    { key: 'mandate', label: '总则', text: '为什么值得做' },
    { key: 'evidence', label: '证据', text: '凭什么信' },
    { key: 'rules', label: '规则', text: '什么条件下做' },
    { key: 'today', label: '执行', text: '今天做什么' },
    { key: 'archive', label: '反证', text: '什么已经被否掉' },
    { key: 'tools', label: '工具', text: '用什么做' },
  ]
  return (
    <p className="mt-1.5 hidden max-w-2xl text-[12px] leading-relaxed text-secondary sm:block">
      {items.map((it, i) => (
        <span key={it.key}>
          {i > 0 && <span aria-hidden className="mx-1 text-muted">·</span>}
          <button
            type="button"
            onClick={() => go(it.key)}
            className="group inline-flex items-baseline gap-1 rounded-sm underline-offset-2 hover:text-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            <b className="font-medium text-foreground group-hover:text-accent">{it.label}</b>
            <span>{it.text}</span>
          </button>
        </span>
      ))}
    </p>
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

/** 兼容旧命名：router 旧入口与测试仍按 StructuralEdge / LongStrategy 导入。 */
export { Handbook as LongStrategy, Handbook as StructuralEdge }
