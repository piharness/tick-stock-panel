/**
 * 机会雷达 —— 每天找机会的那一页（/handbook）。
 *
 * 2026-11-24 手册拆分后的另一半：原来六章里的「执行」章升级成这一页。
 * 它与图书馆（/library）的分工用一句话说清：
 *   雷达回答「今天做什么」，图书馆回答「凭什么这么做」。
 *
 * 设计语言刻意与图书馆不同（这不是不统一，是两种使用频率的两种材质）：
 *   图书馆是文档 —— 发丝线、无卡片、可长读；
 *   雷达是仪表 —— 少量圆角卡片承载「可操作的少数对象」，
 *   默认折叠一切参考性内容，首屏只回答一个问题：今天有没有可下注的。
 *
 * 页面结构（自上而下，按收盘后 3 分钟能否做完决定排）：
 *   01 今天（判定 hero：候选数 + 三项微指标）
 *   02 候选卡（有候选才出现，最多六张）
 *   03 账户指令（明天开盘做什么）
 *   04 反例登记（那条被证伪的规则今天在说什么 —— 每天看见一次）
 *   05 折叠区：判定明细 / 事件账本 / 现金腿 / 触发四步
 *
 * 数据只来自 /api/etf-watch（+ account / cash-leg）与 /api/position-art；
 * 页面不内联任何阈值 —— 口径写在图书馆的「规则」章，判定只在 analysis/etf_watch.py。
 */
import { useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowUpRight, Check, Plus } from 'lucide-react'
import type { EtfWatchIndustry, EtfWatchPayload } from '@/lib/api'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { AccountCard, CashLegCard, Ledger, NextSteps, RegisterCard } from './Execution'
import { ScanTable } from './ScanTable'
import { coverageGap, lagSessions } from './live'
import { Callout, Empty, Fold } from './ui'
import { useWatchData } from './useWatch'

/** 雷达页的卡片材质：浅底一层极淡投影，深底换 4% 白描边（阴影在黑底上不可见）。 */
const CARD =
  'rounded-2xl border border-border/70 bg-surface shadow-[0_1px_3px_rgba(0,0,0,0.04)] dark:shadow-none dark:ring-1 dark:ring-white/[0.05]'

const WEEKDAY = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'] as const

/** "2026-11-24" → "11 月 24 日 · 周一"。解析失败原样返回（接口日期格式是稳定的）。 */
function dateLabel(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso)
  if (!m) return iso
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]))
  if (Number.isNaN(d.getTime())) return iso
  return `${Number(m[2])} 月 ${Number(m[3])} 日 · ${WEEKDAY[d.getDay()]}`
}

const MARKET_STATE: Record<string, string> = { down: '下行', up: '上行', flat: '横盘' }

export function Daily() {
  const { payload, err, account, accountErr, cashLeg, cashErr, art } = useWatchData({ cashLeg: true })
  const artErr = art.error ? (art.error as Error).message : ''
  const qc = useQueryClient()
  // 预览与加自选：候选卡的两条「不用离开本页就能做」的动线。
  const [preview, setPreview] = useState<{ symbol: string; name: string | null; industry?: EtfWatchIndustry } | null>(null)
  const [added, setAdded] = useState<Record<string, boolean>>({})
  // 「观察池」是加自选的默认分组（规则章的候选本来就该先观察再下单）。
  // 分组不存在时首次点击会自动创建 —— 一次点击完成，不再弹二级菜单；
  // 想换分组的用户用 K 线弹窗里自带的加自选（那里有完整分组选择）。
  const groupsQ = useQuery({ queryKey: QK.watchlistGroups, queryFn: api.watchlistGroups, staleTime: 60_000 })
  const poolGroupId = useMemo(
    () => groupsQ.data?.groups.find(g => g.name === '观察池')?.id ?? null,
    [groupsQ.data],
  )
  const addWatch = useMutation({
    mutationFn: async ({ symbol }: { symbol: string }) => {
      let groupId = poolGroupId
      if (!groupId) {
        const r = await api.watchlistGroupCreate('观察池', 'sky')
        groupId = r.group.id
        qc.invalidateQueries({ queryKey: QK.watchlistGroups })
      }
      return api.watchlistAdd(symbol, '', groupId)
    },
    onSuccess: (_d, v) => {
      qc.invalidateQueries({ queryKey: QK.watchlist })
      setAdded(m => ({ ...m, [v.symbol]: true }))
      window.setTimeout(() => setAdded(m => ({ ...m, [v.symbol]: false })), 2500)
    },
  })

  const lag = lagSessions(payload)
  const gap = coverageGap(payload)
  const candidates = payload
    ? [...payload.industries].filter(i => i.status === 'CANDIDATE').sort((a, b) => a.dd - b.dd)
    : []
  const asof = payload?.meta.asof
  // 六格仓位（规则章预注册）：每笔 = 当时权益 / 6 ≈ 16.7%。候选卡把它带到手边，
  // 金额以「最近收盘估算」，实际按成交时权益计算 —— 与账户指令同一口径。
  const slot = account ? account.state.equity / 6 : null
  // 段首 = 今天首次进入区间（账户只买段首）；其余是延续中的信号。
  // 两组分开排：今天真正可执行的永远是少数，不能让延续的信号淹没它。
  const segToday = useMemo(
    () => new Set((payload?.ledger.events ?? []).filter(e => e.date === payload?.meta.asof).map(e => e.industry)),
    [payload],
  )
  const segFirst = candidates.filter(c => segToday.has(c.industry))
  const ongoing = candidates.filter(c => !segToday.has(c.industry))
  const grouped = segFirst.length > 0 && ongoing.length > 0
  // 弹窗顶栏的判定四条（CANDIDATE 才会出现在这里，所以四条都是过）
  const previewContext = preview?.industry && payload
    ? {
        label: '雷达判定 · 四条全过',
        chips: [
          `位置 ${(preview.industry.dd * 100).toFixed(1)}%`,
          `形态 分位 ${preview.industry.vol_pct != null ? `${Math.round(preview.industry.vol_pct * 100)}%` : '—'}`,
          `市况 ${MARKET_STATE[payload.market.state] ?? payload.market.state}`,
          `容量 ${(preview.industry.amt20 / 1e8).toFixed(1)}亿`,
        ],
      }
    : null

  return (
    <div className="mx-auto max-w-[1080px] px-6 pb-24 pt-10">
      {/* ------------------------------------------------ 00 页头 */}
      <header className="flex flex-col gap-6 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="text-[11px] font-medium tracking-[0.18em] text-muted">
            机会雷达 · 事件低吸 · 收盘后判定
          </div>
          <h1 className="mt-3 text-[34px] font-semibold leading-[1.1] tracking-[-0.02em] text-foreground">
            {asof ? dateLabel(asof) : err ? '等待盘后扫描' : '读取中…'}
          </h1>
          {payload && (
            <p className="mt-2.5 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[13px] text-secondary">
              <span>市场状态 · {MARKET_STATE[payload.market.state] ?? payload.market.state}</span>
              <span aria-hidden className="text-border">·</span>
              <span>沪深300 {payload.market.hs300_close}</span>
              <span aria-hidden className="text-border">·</span>
              <span>V1 已结算 {payload.ledger.v1.progress ?? '—'}</span>
            </p>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-start gap-2.5 sm:items-end">
          <Link
            to="/library"
            className="group inline-flex items-center gap-1.5 rounded-full border border-border px-3.5 py-1.5 text-[12px] text-secondary transition-colors hover:border-foreground/25 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            策略图书馆
            <ArrowUpRight className="h-3 w-3 transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
          </Link>
          <span className={cn(
            'inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px]',
            payload ? 'bg-accent/[0.08] text-accent' : 'bg-elevated text-muted',
          )}>
            <span className={cn('h-[5px] w-[5px] rounded-full', payload ? 'bg-accent' : 'bg-border')} />
            {payload ? `扫描完成 ${payload.meta.asof}` : err ? '扫描未就绪' : '正在读取…'}
          </span>
        </div>
      </header>

      {(lag > 0 || gap) && (
        <div className="mt-6 rounded-xl border border-warning/40 bg-warning/[0.06] px-4 py-3 text-[12.5px] leading-[1.7] text-foreground">
          {lag > 0 && <p>行情落后 {lag} 个交易日：先补数据再重跑扫描，下面的判定不能直接用于下单。</p>}
          {gap && (
            <p className={lag > 0 ? 'mt-1.5' : undefined}>
              {gap.text}：今天的盘后链路没跑。补跑{' '}
              <code className="rounded bg-base px-1 py-0.5 font-mono text-[11px]">analysis/etf_watch.py</code>、
              <code className="rounded bg-base px-1 py-0.5 font-mono text-[11px]">etf_watch_account.py</code>。
            </p>
          )}
        </div>
      )}

      {/* ------------------------------------------------ 01 今天 */}
      <section id="today" className="mt-14 scroll-mt-24">
        {payload ? <Today payload={payload} /> : <Offline err={err} />}
      </section>

      {/* ------------------------------------------------ 02 候选卡 */}
      {payload && candidates.length > 0 && (
        <section className="mt-12">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h2 className="flex items-baseline gap-2.5 text-[17px] font-semibold tracking-[-0.01em] text-foreground">
              候选行业
              <span className="rounded-full bg-accent/[0.08] px-2 py-0.5 font-mono text-[11px] font-medium tabular-nums text-accent">
                {candidates.length}
              </span>
            </h2>
            <span className="text-[11px] text-muted">段首优先 · 组内按回撤深度排 · 点卡片看载体 K 线</span>
          </div>
          {grouped && (
            <div className="mt-5 text-[11px] font-medium tracking-[0.1em] text-accent">段首 · 今天首次进入区间，可执行</div>
          )}
          <div className={cn('grid gap-4 sm:grid-cols-2 lg:grid-cols-3', grouped ? 'mt-3' : 'mt-5')}>
            {segFirst.map(c => (
              <CandidateCard key={c.industry} c={c} payload={payload} slot={slot} onOpen={setPreview} onAdd={() => c.best_symbol && addWatch.mutate({ symbol: c.best_symbol })} added={c.best_symbol ? !!added[c.best_symbol] : false} />
            ))}
          </div>
          {grouped && (
            <div className="mt-7 text-[11px] font-medium tracking-[0.1em] text-muted">延续 · 已在区间内，账户只买段首</div>
          )}
          {ongoing.length > 0 && (
            <div className={cn('grid gap-4 sm:grid-cols-2 lg:grid-cols-3', grouped ? 'mt-3' : 'mt-5')}>
              {ongoing.map(c => (
                <CandidateCard key={c.industry} c={c} payload={payload} slot={slot} onOpen={setPreview} onAdd={() => c.best_symbol && addWatch.mutate({ symbol: c.best_symbol })} added={c.best_symbol ? !!added[c.best_symbol] : false} />
              ))}
            </div>
          )}
        </section>
      )}

      {/* ------------------------------------------------ 03 账户指令 */}
      <DailySection id="account" title="账户指令" note="纸面 · 卖出按收盘，买入按次日开盘限价">
        <AccountCard account={account} err={accountErr} />
      </DailySection>

      {/* ------------------------------------------------ 04 反例登记 */}
      <DailySection id="register" title="反例登记" note="那条被证伪的规则今天在说什么">
        <RegisterCard art={art.data ?? null} err={artErr} />
      </DailySection>

      {/* ------------------------------------------------ 05 参考区（默认收起） */}
      <div className="mt-14 border-t border-border pt-2">
        <Fold id="scan" title="判定明细：四条判据逐行业" hint="按判定优先级排，同档按回撤深度">
          {payload
            ? <ScanTable payload={payload} swipeHint />
            : <Empty text="扫描结果未就绪，判定表暂不可用。先看上方命令。" />}
        </Fold>
        <Fold id="ledger" title="历史信号 · 事件账本" hint="回看历史候选 · 只增不改 · 同月多行业记为 1 簇">
          {payload ? <Ledger payload={payload} /> : <Empty text="还没有账本记录。第一条信号会在触发日写入。" />}
        </Fold>
        <Fold id="cash" title="现金腿：短融打底 + 国债锚（影子方案）" hint="腿 1 空仓时钱放在哪 · 不构成 02 的下单依据">
          <Callout tone="warn" label="政策关系">
            02 的账户曲线在跑 JB0（债券 = 权益 − 腿 1）；本节是影子方案 R1/R2/R3，尚未进入账户曲线。
            同一天两者给出不同金额时，以 02 为准。
          </Callout>
          <div className="mt-5"><CashLegCard leg={cashLeg} err={cashErr} /></div>
        </Fold>
        <Fold id="next" title="如果今天触发：按这四步做完" hint="执行顺序，不产生新判定">
          <NextSteps />
        </Fold>
      </div>

      {/* ------------------------------------------------ 页脚 */}
      <footer className="mt-16 flex flex-wrap items-start justify-between gap-x-8 gap-y-3 border-t border-border pt-4 text-[11px] leading-[1.7] text-muted">
        <span className="max-w-[52em]">
          判定口径只有一份：<code className="font-mono text-secondary">analysis/etf_watch.py</code>。
          为什么是这些条件，读{' '}
          <Link to="/library#rules" className="text-secondary underline-offset-2 hover:text-foreground hover:underline">规则</Link>
          ；反例的完整验收在{' '}
          <Link to="/library#sizing-audit" className="text-secondary underline-offset-2 hover:text-foreground hover:underline">证据 · 仓位择时的验收</Link>
          ；回放某一天在{' '}
          <Link to="/library#replay-day" className="text-secondary underline-offset-2 hover:text-foreground hover:underline">图书馆 · 工具</Link>
          。本页不构成投资建议。
        </span>
        <span className="shrink-0 font-mono tracking-[0.08em]">radar · daily</span>
      </footer>

      <StockPreviewDialog
        symbol={preview?.symbol ?? null}
        name={preview?.name ?? undefined}
        contextBar={previewContext}
        onClose={() => setPreview(null)}
      />
    </div>
  )
}

/** Fold 的收起态在深链/检索时需要 id；DailySection 只给展开节用。 */
function DailySection({ id, title, note, children }: { id: string; title: string; note?: string; children: ReactNode }) {
  return (
    <section id={id} className="mt-14 scroll-mt-24">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="text-[17px] font-semibold tracking-[-0.01em] text-foreground">{title}</h2>
        {note && <span className="text-[11px] text-muted">{note}</span>}
      </div>
      <div className="mt-5">{children}</div>
    </section>
  )
}

/** 01 今天：一个数字回答一个问题。0 也是答案，而且是常见的答案。 */
function Today({ payload }: { payload: EtfWatchPayload }) {
  const { candidate, in_band, industries, signal, clusters_today } = payload.counts
  const has = candidate > 0
  const newSeg = payload.ledger.events.filter(e => e.date === payload.meta.asof).length

  return (
    <div>
      <div className="flex flex-wrap items-end gap-x-6 gap-y-4">
        <span
          className={cn(
            'font-mono text-[72px] font-semibold leading-none tracking-[-0.04em] tabular-nums',
            has ? 'text-accent' : 'text-muted/50',
          )}
        >
          {candidate}
        </span>
        <div className="pb-2">
          <div className="text-[17px] font-medium text-foreground">
            {has ? '个行业四条全过' : '今日无四条全过'}
          </div>
          <div className="mt-1.5 text-[13px] leading-[1.6] text-secondary">
            {has
              ? (newSeg > 0 ? `其中 ${newSeg} 个是段首，可执行` : '均为延续中的信号，等段首再动手')
              : '区间内不等于可下注 —— 形态或容量不过就不做。'}
          </div>
        </div>
      </div>
      <div className="mt-7 grid grid-cols-3 gap-x-7 gap-y-4 sm:max-w-[520px]">
        <Figure k="区间内" v={`${in_band}/${industries}`} />
        <Figure k="高波信号" v={String(signal)} />
        <Figure k="今日事件簇" v={String(clusters_today)} />
      </div>
    </div>
  )
}

function Figure({ k, v, tone, hint }: { k: string; v: string; tone?: 'accent'; hint?: string }) {
  return (
    <div className="border-t border-border/70 pt-2.5">
      <div className="text-[11px] text-muted" title={hint}>{k}</div>
      <div className={cn('mt-1.5 font-mono text-[17px] font-semibold leading-none tabular-nums', tone === 'accent' ? 'text-accent' : 'text-foreground')}>{v}</div>
    </div>
  )
}

/** 02 候选卡：一卡一个行业，可点开载体 K 线、可加自选 —— 看和存都不用离开本页。 */
function CandidateCard({
  c, payload, slot, onOpen, onAdd, added,
}: {
  c: EtfWatchIndustry
  payload: EtfWatchPayload
  slot: number | null
  onOpen: (v: { symbol: string; name: string | null; industry: EtfWatchIndustry }) => void
  onAdd: () => void
  added: boolean
}) {
  const amt = c.amt20 >= 1e8 ? `${(c.amt20 / 1e8).toFixed(1)} 亿` : `${(c.amt20 / 1e4).toFixed(0)} 万`
  const trig = lastTrigger(payload, c.industry)
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => c.best_symbol && onOpen({ symbol: c.best_symbol, name: c.best_name, industry: c })}
      onKeyDown={e => { if (e.key === 'Enter' && c.best_symbol) onOpen({ symbol: c.best_symbol, name: c.best_name, industry: c }) }}
      className={cn(
        CARD,
        'group cursor-pointer p-5 transition-all duration-200 ease-[cubic-bezier(0.16,1,0.3,1)] hover:-translate-y-0.5 hover:shadow-[0_6px_16px_rgba(0,0,0,0.08)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="text-[15px] font-semibold text-foreground">{c.industry}</span>
            {/* 回撤 = 已跌到的位置：A 股语义里「跌」用绿，这一页红绿只表示涨跌 */}
            <span className="font-mono text-[13px] tabular-nums text-bear">{(c.dd * 100).toFixed(1)}%</span>
          </div>
          <div className="mt-1 truncate text-[12px] text-muted" title={c.best_name ?? undefined}>
            {c.best_name ?? '—'}
          </div>
        </div>
        <button
          type="button"
          onClick={e => { e.stopPropagation(); onAdd() }}
          disabled={!c.best_symbol || added}
          title={added ? '已加入自选 · 观察池' : '加入自选 · 观察池（分组不存在时会自动创建）'}
          aria-label={`把 ${c.industry} 的载体加入自选观察池`}
          className={cn(
            'flex h-7 w-7 shrink-0 items-center justify-center rounded-full border transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
            added
              ? 'border-accent/40 bg-accent/[0.08] text-accent'
              : 'border-border text-muted hover:border-accent/40 hover:text-accent',
          )}
        >
          {added ? <Check className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
        </button>
      </div>

      <div className="mt-3 flex flex-wrap items-baseline gap-x-2.5 gap-y-1 text-[11.5px]">
        {trig ? (
          <>
            <span className="text-muted">上次触发 {trig.date}</span>
            {trig.rel != null
              ? <span className={cn('font-mono tabular-nums', trig.rel >= 0 ? 'text-bull' : 'text-bear')}>{fmtSigned(trig.rel)}bp</span>
              : <span className="text-muted">待到期</span>}
          </>
        ) : (
          <span className="text-muted">账本内首次触发</span>
        )}
      </div>

      <div className="mt-4 grid grid-cols-3 gap-x-3 border-t border-border/60 pt-3">
        <Figure
          k="波动分位"
          v={c.vol_pct != null ? `${Math.round(c.vol_pct * 100)}%` : '—'}
          tone={c.vol_pct != null && c.vol_pct > 0.66 ? 'accent' : undefined}
          hint="20 日已实现波动的 500 日分位；蓝色 = 满足形态闸（>66%）"
        />
        <Figure k="20日均额" v={amt} />
        <Figure
          k="一格(权益/6)"
          v={slot != null ? fmtWan(slot) : '—'}
          hint="规则章预注册的六格仓位：每笔 = 当时权益 ÷ 6 ≈ 16.7%；金额按最近收盘估算，实际按成交时权益计算"
        />
      </div>
      {c.reasons.length > 0 && (
        <p className="mt-3 line-clamp-2 text-[11px] leading-[1.6] text-muted" title={c.reasons.join('；')}>
          {c.reasons.join('；')}
        </p>
      )}
    </div>
  )
}

/** 该行业最近一次「四条全过」的事件与其 20 日相对结算。 */
function lastTrigger(payload: EtfWatchPayload, industry: string): { date: string; rel: number | null } | null {
  const ev = [...payload.ledger.events]
    .filter(e => e.industry === industry && e.qualified)
    .sort((a, b) => (a.date < b.date ? 1 : -1))[0]
  if (!ev) return null
  const s = payload.ledger.settles.find(x => x.id === ev.id)
  return { date: ev.date.slice(5), rel: s ? Math.round(s.rel20 * 1e4) : null }
}

/** 数字带符号：正数显式 +，负数用真减号 U+2212（与图书馆 fmtBp 同一视觉约定）。 */
function fmtSigned(v: number): string {
  return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v)}`
}

/** 元 → 万元显示；不足 1 万保持元。 */
function fmtWan(v: number): string {
  return v >= 1e4 ? `${(v / 1e4).toFixed(1)} 万` : `${Math.round(v).toLocaleString('zh-CN')} 元`
}

/** 接口未就绪：给命令，不给空白（与图书馆的空态同一原则）。 */
function Offline({ err }: { err: string }) {
  return (
    <div>
      <div className="flex flex-wrap items-end gap-x-6 gap-y-4">
        <span className="font-mono text-[72px] font-semibold leading-none tracking-[-0.04em] text-muted/30">—</span>
        <div className="pb-2">
          <div className="text-[17px] font-medium text-foreground">还没有盘后扫描结果</div>
          <div className="mt-1.5 text-[13px] text-secondary">
            {err || '正在读取扫描结果…'}
          </div>
        </div>
      </div>
      {err && (
        <>
          <pre className="mt-6 inline-block rounded-lg border border-border px-3.5 py-2 font-mono text-[12.5px] text-foreground">
            python analysis/etf_watch.py
          </pre>
          <p className="mt-4 max-w-[42em] text-[12px] leading-[1.7] text-muted">
            每天记录一次，攒满 3 个事件簇才做 V1 判定 —— 不要回测，回测的 N 已经用到头了。
          </p>
        </>
      )}
      <div className="mt-7 grid grid-cols-3 gap-x-7 gap-y-4 sm:max-w-[520px]">
        <Figure k="区间内" v="—" />
        <Figure k="高波信号" v="—" />
        <Figure k="今日事件簇" v="—" />
      </div>
    </div>
  )
}
