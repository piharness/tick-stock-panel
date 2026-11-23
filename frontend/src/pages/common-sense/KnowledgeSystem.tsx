/**
 * 认知体系 —— 把知识库画成一条五层因果链，而不是一张平铺清单。
 *
 * 上一版的痛点是「零散」：21 条内容彼此孤立，读者记不住、也接不上。这一版的做法是
 * 先立骨架再挂内容——规则 → 信息 → 动机 → 结构 → 结算，每一层回答一个问题，
 * 层与层之间用一句因果陈述连起来（Layer.toNext），最后从「代价」绕回「规则修订」，
 * 链条闭合。任何一条常识如果不能被放进链上的某一格，它大概不是常识。
 *
 * 每个节点必须能看到三样东西，缺一个就退回成「话术」：
 *   ① 常见误解 —— 大多数人实际相信的错误版本；
 *   ② 实时锚点 —— 用今天全市场数据算出来的一个数（anchors.ts），没有就是 null，不编 0；
 *   ③ 依据 —— 条款 / 数据口径 / 本项目实测。
 *
 * 交互：点节点 → 就地展开详情；点关联 → 跳到目标节点（跨层也会滚过去）。
 * 这样「联系」不是一句「相关阅读」，而是可以走的一条路。
 */
import { useEffect, useMemo } from 'react'
import { ArrowDown, Crosshair, ExternalLink, FlaskConical, ShieldQuestion, TriangleAlert } from 'lucide-react'
import type { MarketSnapshotRow, TableGameBoard } from '@/lib/api'
import { cn } from '@/lib/cn'
import { RULE_STATE } from '@/lib/ruleState'
import { computeAnchor, type AnchorId, type AnchorResult } from './anchors'
import {
  probeLookup, probeSpec, probeValueText, type ProbeLookup,
} from './probes'
import {
  CERTAINTY_CHIP, CERTAINTY_META, KNOWLEDGE, LAYERS, REL_META,
  type KnowledgeItem, type LayerId,
} from './knowledge'

export function KnowledgeSystem({
  rows, asOf, focusId, onFocus, board, symbol,
}: {
  rows: MarketSnapshotRow[]
  asOf: string | null
  focusId: string | null
  onFocus: (id: string | null) => void
  /** 当前选中的个股（null = 只显示市场级锚点） */
  board: TableGameBoard | undefined
  symbol: string | null
}) {
  const byLayer = useMemo(() => {
    const m = new Map<LayerId, KnowledgeItem[]>()
    for (const l of LAYERS) m.set(l.id, [])
    for (const k of KNOWLEDGE) m.get(k.layer)?.push(k)
    return m
  }, [])

  const itemById = useMemo(() => new Map(KNOWLEDGE.map(k => [k.id, k])), [])

  const anchorCache = useMemo(() => {
    const m = new Map<AnchorId, AnchorResult | null>()
    return (id: AnchorId) => {
      if (!m.has(id)) m.set(id, computeAnchor(id, { rows, asOf }))
      return m.get(id) ?? null
    }
  }, [rows, asOf])

  // 被外部（今日事实卡 / 关联跳转）指到时，滚到**详情卡**而不是节点本身：
  // 详情渲染在该层网格的下方，滚到 chip 会让内容停在视口外。
  useEffect(() => {
    if (!focusId) return
    const t = window.setTimeout(() => {
      const el = document.getElementById(`kd-${focusId}`) ?? document.getElementById(`kc-${focusId}`)
      el?.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }, 60)
    return () => window.clearTimeout(t)
  }, [focusId])

  return (
    <div className="space-y-1">
      {/* 一句话主线 —— 先给骨架，再给节点 */}
      <div className="rounded-card border border-border bg-surface/80 px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-secondary">
          <span className="text-[10px] uppercase tracking-wide text-muted/70">一条主线</span>
          {LAYERS.map((l, i) => (
            <span key={l.id} className="flex items-center gap-2">
              <span className="rounded border border-border bg-elevated/60 px-1.5 py-0.5 text-[10px] text-foreground">
                {l.index}. {l.label}
              </span>
              {i < LAYERS.length - 1
                ? <span className="text-muted/50">→</span>
                : <span className="text-muted/50">→ 回到 ①</span>}
            </span>
          ))}
        </div>
        <p className="mt-1.5 text-[10px] leading-relaxed text-muted">
          规则决定「能交易什么、单日能移动多少」；信息决定「谁先定价」；动机决定「价格落在哪」；
          结构决定「出事时谁跑得掉」；结算决定「代价落在谁头上」。代价再倒逼规则修订——这就是闭环。
          下面每一格都可以点开，打开后能看到它的<b className="text-secondary">常见误解</b>、
          <b className="text-secondary">实时锚点</b>（今天的真实数字）和
          <b className="text-secondary">关联路径</b>。
          在上方选了票之后，挂有判据的节点会再多一个
          <b className="text-secondary">个股状态</b>（成立 / 不成立 / 算术 / 无数据）——
          市场数字是背景，个股判定才是你要下的那一单。
        </p>
      </div>

      {LAYERS.map((layer, li) => {
        const items = byLayer.get(layer.id) ?? []
        return (
          <div key={layer.id}>
            {/* 层头 */}
            <div className="mt-2 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-1">
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-sm bg-accent/15 text-[9px] font-semibold text-accent">
                {layer.index}
              </span>
              <h3 className="text-[12px] font-medium text-foreground">{layer.label}</h3>
              <span className="text-[10px] text-muted">{layer.question}</span>
              <span className="ml-auto text-[9px] font-mono text-muted/60">{items.length} 条</span>
            </div>

            {/* 节点网格 */}
            <div className="mt-1 grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
              {items.map(k => {
                const on = focusId === k.id
                const a = k.anchor ? anchorCache(k.anchor) : null
                // 个股判据只在这一条挂了 probe、且已经选了票时才出现——没选票就只谈市场
                const probe = k.probe && symbol ? probeLookup(board, k.probe) : null
                return (
                  <button
                    key={k.id}
                    id={`kc-${k.id}`}
                    onClick={() => onFocus(on ? null : k.id)}
                    className={cn(
                      'group flex flex-col gap-1 rounded border px-2 py-1.5 text-left transition-colors',
                      on
                        ? 'border-accent/50 bg-accent/[0.07]'
                        : 'border-border/60 bg-surface/40 hover:border-border hover:bg-elevated/40',
                    )}
                  >
                    <div className="flex items-start gap-1.5">
                      <span className={cn('mt-[3px] h-1.5 w-1.5 shrink-0 rounded-full', DOT[k.certainty])} />
                      <span className="text-[11px] font-medium leading-snug text-foreground">{k.title}</span>
                    </div>
                    <div className="flex flex-wrap items-center gap-1">
                      <span className={cn('rounded px-1 py-px text-[9px] leading-tight', CERTAINTY_CHIP[k.certainty])}>
                        {CERTAINTY_META[k.certainty].label}
                      </span>
                      {a && (
                        <span className={cn('rounded px-1 py-px font-mono text-[9px] leading-tight', TONE_CHIP[a.tone])}>
                          {a.value}{a.unit ? ` ${a.unit}` : ''}
                        </span>
                      )}
                      {k.anchor && !a && (
                        <span className="rounded border border-border bg-elevated/40 px-1 py-px text-[9px] leading-tight text-muted/60">
                          今日无数据
                        </span>
                      )}
                      {probe && (
                        probe.kind === 'rule' ? (
                          <span
                            className={cn('rounded px-1 py-px text-[9px] leading-tight', RULE_STATE[probe.rule.state].cls)}
                            title={`个股判据：${RULE_STATE[probe.rule.state].hint}`}
                          >
                            个股 {RULE_STATE[probe.rule.state].short}
                          </span>
                        ) : (
                          <span
                            className="rounded border border-border bg-elevated/40 px-1 py-px text-[9px] leading-tight text-muted/60"
                            title={probe.reason}
                          >
                            个股 无法判定
                          </span>
                        )
                      )}
                    </div>
                  </button>
                )
              })}
            </div>

            {/* 展开的详情 */}
            {items.filter(k => k.id === focusId).map(k => (
              <div key={k.id} id={`kd-${k.id}`}>
                <ItemDetail
                  item={k}
                  anchor={k.anchor ? anchorCache(k.anchor) : null}
                  probe={k.probe ? probeLookup(board, k.probe) : null}
                  symbol={symbol}
                  onJump={id => onFocus(id)}
                  exists={id => itemById.has(id)}
                />
              </div>
            ))}

            {/* 层间因果陈述 */}
            {li < LAYERS.length - 1 && (
              <div className="my-1.5 flex items-start gap-1.5 pl-1">
                <ArrowDown className="mt-0.5 h-3 w-3 shrink-0 text-accent/60" />
                <p className="text-[10px] leading-relaxed text-muted">{layer.toNext}</p>
              </div>
            )}
            {li === LAYERS.length - 1 && (
              <div className="my-1.5 flex items-start gap-1.5 pl-1">
                <ArrowDown className="mt-0.5 h-3 w-3 shrink-0 text-accent/60" />
                <p className="text-[10px] leading-relaxed text-muted">
                  {layer.toNext}
                  <button
                    onClick={() => onFocus(KNOWLEDGE.find(k => k.layer === 'rule')?.id ?? null)}
                    className="ml-1 text-accent hover:underline"
                  >
                    回到第 ① 层
                  </button>
                </p>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

const DOT: Record<KnowledgeItem['certainty'], string> = {
  'hard-rule': 'bg-red-500',
  'strong-consensus': 'bg-amber-500',
  'probabilistic': 'bg-sky-500',
  'heuristic': 'bg-zinc-400',
}

const TONE_CHIP: Record<AnchorResult['tone'], string> = {
  danger: 'bg-red-500/10 text-red-600',
  warn: 'bg-amber-500/10 text-amber-600',
  ok: 'bg-emerald-500/10 text-emerald-600',
  neutral: 'bg-elevated text-secondary',
}

/** 条目详情（体系图与规则速查共用同一个渲染器：同一条内容只有一处版式定义）。 */
export function ItemDetail({
  item, anchor, probe = null, symbol = null, onJump, exists, hideHeader = false,
}: {
  item: KnowledgeItem
  anchor: AnchorResult | null
  /** 个股判据的查询结果（没挂判据时传 null） */
  probe?: ProbeLookup | null
  /** 当前选中的个股；null 表示还没选票 */
  symbol?: string | null
  onJump: (id: string | null) => void
  exists: (id: string) => boolean
  /** 列表行已经展示过标题与摘要时置 true，避免同一句话在屏幕上出现两遍。 */
  hideHeader?: boolean
}) {
  return (
    <article className="mt-1.5 rounded-card border border-accent/30 bg-surface/90 p-3">
      {!hideHeader && (
        <header className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h4 className="text-[12px] font-medium text-foreground">{item.title}</h4>
          <span className={cn('rounded px-1 py-px text-[9px]', CERTAINTY_CHIP[item.certainty])}>
            {CERTAINTY_META[item.certainty].label}
          </span>
          <span className="text-[9px] text-muted/60">{CERTAINTY_META[item.certainty].hint}</span>
          <button onClick={() => onJump(null)} className="ml-auto text-[10px] text-muted hover:text-foreground">
            收起
          </button>
        </header>
      )}

      {!hideHeader && (
        <p className="mt-1.5 text-[11px] leading-relaxed text-foreground/90">{item.summary}</p>
      )}
      <p className={cn('text-[11px] leading-relaxed text-secondary', !hideHeader && 'mt-1')}>{item.body}</p>

      {/* 常见误解 —— 这一页最值钱的一行 */}
      <div className="mt-2 flex items-start gap-1.5 rounded border border-amber-500/25 bg-amber-500/[0.06] px-2 py-1.5">
        <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0 text-amber-600" />
        <p className="text-[10px] leading-relaxed text-secondary">
          <b className="text-amber-600">常见误解：</b>{item.misconception}
        </p>
      </div>

      {/* 实时锚点 */}
      {anchor && (
        <div className="mt-1.5 rounded border border-border/70 bg-elevated/25 px-2 py-1.5">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <Crosshair className="h-3 w-3 shrink-0 text-accent" />
            <span className="text-[10px] text-secondary">今日实测 · {anchor.label}</span>
            <span className={cn('font-mono text-[13px] font-semibold', TONE_TEXT[anchor.tone])}>
              {anchor.value}
            </span>
            {anchor.unit && <span className="text-[10px] text-muted">{anchor.unit}</span>}
          </div>
          <p className="mt-1 text-[10px] leading-relaxed text-secondary">{anchor.read}</p>
          <p className="mt-1 font-mono text-[9px] leading-relaxed text-muted/70">口径：{anchor.sample}</p>
        </div>
      )}

      {/* 个股判据 —— 市场级锚点下面的个股级结论 */}
      {probe && item.probe && (
        <div className="mt-1.5 rounded border border-border/70 bg-elevated/25 px-2 py-1.5">
          {probe.kind === 'rule' ? (
            <>
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <Crosshair className="h-3 w-3 shrink-0 text-warning" />
                <span className="text-[10px] text-secondary">
                  个股判据 · <span className="font-mono">{symbol}</span>
                </span>
                <span className={cn('rounded px-1 py-px text-[9px]', RULE_STATE[probe.rule.state].cls)}>
                  {RULE_STATE[probe.rule.state].label}
                </span>
                <span className={cn('font-mono text-[13px] font-semibold', RULE_STATE[probe.rule.state].text)}>
                  {probeValueText(probe.rule)}
                </span>
                {probe.rule.unit && <span className="text-[10px] text-muted">{probe.rule.unit}</span>}
              </div>
              <p className="mt-1 text-[10px] leading-relaxed text-secondary">{RULE_STATE[probe.rule.state].hint}</p>
              {probe.rule.detail && (
                <p className="mt-1 font-mono text-[9px] leading-relaxed text-muted/80">{probe.rule.detail}</p>
              )}
              {/* 判据定义（含阈值）只从后端取：前端不重写一份门槛，否则两处漂移时没人会发现 */}
              <details className="group mt-1">
                <summary className="cursor-pointer list-none text-[9px] text-accent/80 hover:text-accent">
                  判据是怎么判的（阈值由后端给出，前端不重判）
                </summary>
                <p className="mt-1 border-l-2 border-accent/40 pl-2 text-[10px] leading-relaxed text-secondary">
                  {probe.rule.claim}
                </p>
              </details>
            </>
          ) : (
            <div className="flex items-start gap-1.5">
              <Crosshair className="mt-0.5 h-3 w-3 shrink-0 text-muted" />
              <div>
                <p className="text-[10px] leading-relaxed text-secondary">
                  {symbol ? (
                    <>
                      <b className="text-muted">这条判据在这只票上无法判定：</b>{probe.reason}
                    </>
                  ) : (
                    <>
                      <b className="text-muted">这条常识可以落到个股上：</b>
                      {probeSpec(item.probe).question}
                      在上方「个股判据」输入代码，这里会给出这只票的状态与数值。
                    </>
                  )}
                </p>
                <p className="mt-0.5 font-mono text-[9px] text-muted/70">需要：{probeSpec(item.probe).needs}</p>
              </div>
            </div>
          )}
        </div>
      )}

      {/* 自查入口 */}
      <div className="mt-1.5 flex items-start gap-1.5 px-0.5">
        <ShieldQuestion className="mt-0.5 h-3 w-3 shrink-0 text-sky-600" />
        <p className="text-[10px] leading-relaxed text-secondary">
          <b className="text-sky-600">怎么自己核对：</b>{item.check}
        </p>
      </div>

      {/* 依据 */}
      <div className="mt-1 flex items-start gap-1.5 px-0.5">
        <FlaskConical className="mt-0.5 h-3 w-3 shrink-0 text-muted" />
        <p className="text-[9px] leading-relaxed text-muted">
          <span className="text-muted/60">依据：</span>{item.basis}
          {item.effective && <span className="ml-2 font-mono text-muted/60">生效：{item.effective}</span>}
        </p>
      </div>

      {/* 关联路径 */}
      {item.related.length > 0 && (
        <div className="mt-2 border-t border-border/50 pt-1.5">
          <span className="text-[9px] uppercase tracking-wide text-muted/70">关联路径</span>
          <div className="mt-1 space-y-1">
            {item.related.filter(r => exists(r.to)).map(r => (
              <button
                key={`${r.to}-${r.kind}`}
                onClick={() => onJump(r.to)}
                className="flex w-full items-start gap-1.5 rounded px-1 py-0.5 text-left transition-colors hover:bg-elevated/50"
              >
                <span className="mt-px shrink-0 rounded border border-border bg-elevated/60 px-1 text-[9px] text-muted">
                  {REL_META[r.kind].arrow} {REL_META[r.kind].label}
                </span>
                <span className="text-[10px] leading-relaxed text-secondary">
                  <span className="font-mono text-[9px] text-muted/70">{r.to}</span>
                  <span className="mx-1 text-muted/40">·</span>
                  {r.note}
                </span>
                <ExternalLink className="mt-0.5 ml-auto h-2.5 w-2.5 shrink-0 text-muted/40" />
              </button>
            ))}
          </div>
        </div>
      )}
    </article>
  )
}

const TONE_TEXT: Record<AnchorResult['tone'], string> = {
  danger: 'text-red-600',
  warn: 'text-amber-600',
  ok: 'text-emerald-600',
  neutral: 'text-foreground',
}
