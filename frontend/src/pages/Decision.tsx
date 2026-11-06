/**
 * 决策账本页 — 把「我下过的判断」变成可结算、可计分的样本。
 *
 * 三态门禁是本页的主结构，不是装饰：
 *   PASS        规则已评估，未触发
 *   TRIGGERED   已触发 ⇒ 停
 *   UNKNOWN     无法评估 —— 必须与 PASS 分开显示，否则"读不到"会被读成"没问题"
 *
 * 计分卡只在已结算样本 >= 8 时给结论；样本不足时显示 INSUFFICIENT，
 * 不展示"看起来不错"的数字。
 */
import { useCallback, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  RefreshCw, Loader2, ShieldCheck, ShieldAlert, ShieldQuestion,
  ScrollText, Target, ArrowRight,
} from 'lucide-react'
import {
  api,
  type DecisionCheck,
  type DecisionCheckStatus,
  type DecisionGateDecision,
  type DecisionOverview,
  type DecisionScore,
} from '@/lib/api'
import { toast } from '@/components/Toast'
import { cn } from '@/lib/cn'

const GATE_META: Record<DecisionGateDecision, { label: string; hint: string; tone: string }> = {
  GO: {
    label: 'GO',
    hint: '四条判据均已评估且未触发，可以登记新决策。',
    tone: 'border-accent/40 bg-accent/10 text-accent',
  },
  GO_WITH_UNKNOWNS: {
    label: 'GO · 带未知',
    hint: '有判据无法评估。放行不等于证明没问题，只是没有证据要求停止。',
    tone: 'border-warning/40 bg-warning/10 text-warning',
  },
  STOP_NEW: {
    label: 'STOP_NEW',
    hint: '触发即停：不新开决策，直到触发项被解除。',
    tone: 'border-danger/40 bg-danger/10 text-danger',
  },
  STOP_NEW_AND_REVIEW: {
    label: 'STOP_NEW_AND_REVIEW',
    hint: '停止新决策，并且必须人工复核持仓假设。',
    tone: 'border-danger/50 bg-danger/15 text-danger',
  },
}

const CHECK_META: Record<DecisionCheckStatus, { icon: typeof ShieldCheck; tone: string }> = {
  PASS: { icon: ShieldCheck, tone: 'text-accent' },
  TRIGGERED: { icon: ShieldAlert, tone: 'text-danger' },
  UNKNOWN: { icon: ShieldQuestion, tone: 'text-warning' },
}

const VERDICT_LABEL: Record<string, string> = {
  INSUFFICIENT: '样本不足',
  BEATS_CONSTANT: '跑赢常数基线',
  NO_INFORMATION: '无信息增量',
}

function pct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function num(v: number | null | undefined, digits = 4): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

/** 这个计分结果的结论是不是「不可判定」。
 *
 *  阈值和带宽在后端（`settle.MIN_NEFF`），前端不复制一份——两处各写一个 30
 *  迟早会分叉。这里只读结论：`UNKNOWN` 既不是 PASS，也不是 FAIL。
 *
 *  空账本（n=0）不提示：它连「测过」都谈不上，页头已经说了样本不足。
 */
function weakSample(d: DecisionScore | undefined): boolean {
  if (!d || d.n === 0) return false
  return d.significance === 'UNKNOWN'
}

function GateCheckCard({ check }: { check: DecisionCheck }) {
  const meta = CHECK_META[check.status]
  const Icon = meta.icon
  return (
    <div className="rounded-card border border-border bg-surface p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Icon className={cn('h-3.5 w-3.5', meta.tone)} />
          <span className="font-mono text-[11px] text-muted">{check.id}</span>
        </div>
        <span className={cn('text-[10px] font-medium tracking-wide', meta.tone)}>{check.status}</span>
      </div>
      <div className="mt-1.5 text-xs text-secondary">{check.rule}</div>
      <p className="mt-1 line-clamp-3 text-[11px] leading-relaxed text-muted" title={check.detail}>
        {check.detail}
      </p>
    </div>
  )
}

export function Decision() {
  const qc = useQueryClient()
  const overview = useQuery({
    queryKey: ['decision', 'overview'],
    queryFn: () => api.decisionOverview(),
    refetchOnWindowFocus: false,
  })
  const score = useQuery({
    queryKey: ['decision', 'score'],
    queryFn: () => api.decisionScore(true),
    refetchOnWindowFocus: false,
  })

  const data: DecisionOverview | undefined = overview.data
  const gate = data?.gates
  const meta = gate ? GATE_META[gate.decision] : null

  const settleNow = useCallback(async () => {
    try {
      const r = await api.decisionSettle()
      toast(`结算完成：新写入 ${r.written} 条，未到期 ${r.pending} 条`, 'success')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['decision', 'overview'] }),
        qc.invalidateQueries({ queryKey: ['decision', 'score'] }),
      ])
    } catch (e) {
      toast(e instanceof Error ? e.message : '结算失败')
    }
  }, [qc])

  const cards = useMemo(() => data?.latest_cards ?? [], [data])
  const s = score.data?.score
  const weak = weakSample(s)
  const sm = data?.score_median
  const prior = score.data?.prior

  if (overview.isLoading) {
    return (
      <div className="flex h-full items-center justify-center bg-base">
        <div className="flex items-center gap-2 text-sm text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 加载决策账本…
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-full bg-base p-4">
      <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ScrollText className="h-4 w-4 text-accent" />
          <h1 className="text-[15px] font-semibold text-foreground">决策账本</h1>
          <span className="font-mono text-[11px] text-muted">
            决策日 {data?.as_of ?? '—'} · {data?.cards ?? 0} 张卡 · {data?.settlements ?? 0} 条结算
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => { void overview.refetch(); void score.refetch() }}
            className="flex items-center gap-1 rounded-btn border border-border bg-surface px-2.5 py-1.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          >
            <RefreshCw className={cn('h-3.5 w-3.5', overview.isFetching && 'animate-spin')} /> 刷新
          </button>
          <button
            onClick={() => { void settleNow() }}
            className="flex items-center gap-1 rounded-btn bg-accent px-2.5 py-1.5 text-xs font-medium text-white transition-opacity hover:opacity-90"
          >
            <Target className="h-3.5 w-3.5" /> 结算到期
          </button>
        </div>
      </header>

      {/* 判决横幅 */}
      {gate && meta && (
        <div className={cn('mb-3 rounded-card border px-3 py-2.5', meta.tone)}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-[13px] font-semibold">{meta.label}</span>
            <span className="text-[11px] opacity-90">{meta.hint}</span>
          </div>
          {gate.notes.length > 0 && (
            <ul className="mt-1 space-y-0.5">
              {gate.notes.map((n, i) => (
                <li key={i} className="text-[11px] opacity-80">· {n}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {(gate?.checks ?? []).map(c => <GateCheckCard key={c.id} check={c} />)}
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
        {/* 决策卡 */}
        <section className="min-w-0 rounded-card border border-border bg-surface">
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <h2 className="text-xs font-medium text-foreground">已封存的决策卡</h2>
            <span className="text-[11px] text-muted">内容哈希 = 承诺，事后不可改</span>
          </div>
          {cards.length === 0 ? (
            <div className="px-3 py-10 text-center">
              <p className="text-xs text-secondary">还没有决策卡。</p>
              <p className="mx-auto mt-1 max-w-md text-[11px] leading-relaxed text-muted">
                空账本不是错误——它表示尚未声称任何决策。第一张卡通过
                <code className="mx-1 rounded bg-elevated px-1 py-0.5 font-mono">POST /api/decision/cards</code>
                登记，封卡后哈希固定；到期后由「结算到期」按真实 bar 撮合并计分。
              </p>
            </div>
          ) : (
            <table className="w-full text-left text-[11px]">
              <thead className="text-muted">
                <tr className="border-b border-border">
                  <th className="px-3 py-1.5 font-medium">card_id</th>
                  <th className="px-2 py-1.5 font-medium">as_of</th>
                  <th className="px-2 py-1.5 text-right font-medium">标的</th>
                  <th className="px-2 py-1.5 text-right font-medium">horizon</th>
                  <th className="px-2 py-1.5 font-medium">封卡时判决</th>
                  <th className="px-3 py-1.5 font-medium">哈希</th>
                </tr>
              </thead>
              <tbody>
                {cards.map(c => (
                  <tr key={c.card_id} className="border-b border-border/60 last:border-0 hover:bg-elevated/60">
                    <td className="px-3 py-1.5 font-mono text-foreground">{c.card_id}</td>
                    <td className="px-2 py-1.5 font-mono text-secondary">{c.as_of}</td>
                    <td className="px-2 py-1.5 text-right font-mono text-secondary">{c.picks}</td>
                    <td className="px-2 py-1.5 text-right font-mono text-secondary">{c.horizon_sessions}</td>
                    <td className="px-2 py-1.5">
                      <span className={cn('font-mono text-[10px]',
                        c.gates_decision?.startsWith('STOP') ? 'text-danger' : 'text-accent')}>
                        {c.gates_decision ?? '—'}
                      </span>
                      {c.override && (
                        <span className="ml-1 text-[10px] text-warning" title={c.override.reason}>越权</span>
                      )}
                    </td>
                    <td className="px-3 py-1.5 font-mono text-muted">{(c.sealed_sha256 ?? '').slice(0, 10)}…</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>

        {/* 计分卡 */}
        <aside className="rounded-card border border-border bg-surface p-3">
          <div className="flex items-center gap-1.5">
            <Target className="h-3.5 w-3.5 text-accent" />
            <h2 className="text-xs font-medium text-foreground">校准</h2>
            <span className={cn('ml-auto rounded px-1.5 py-0.5 text-[10px]',
              s?.verdict === 'BEATS_CONSTANT' ? 'bg-accent/10 text-accent'
                : s?.verdict === 'NO_INFORMATION' ? 'bg-danger/10 text-danger'
                  : 'bg-elevated text-muted')}>
              {VERDICT_LABEL[s?.verdict ?? 'INSUFFICIENT']}
            </span>
          </div>

          <dl className="mt-2.5 space-y-1.5 text-[11px]">
            {([
              ['已结算', String(s?.n ?? 0), s?.days != null ? `${s.days} 天` : undefined],
              // 有效样本 ≠ 已结算数：20 日持有期让相邻决策日重叠 19 天。
              // 它挂在 skill 上，不是账本的属性——同一批数据换个 window 这个数就变，
              // 所以标签必须写「skill 的」，否则会被读成「账本有 1 个样本」。
              ['skill 的有效样本', s?.n_eff == null ? '—' : String(s.n_eff),
                s?.hac_lag != null ? `lag ${s.hac_lag}` : undefined],
              ['命中率', pct(s?.hit_rate)],
              ['Brier', num(s?.brier)],
              ['常数基线 Brier', num(s?.brier_const)],
              ['收益中位', pct(s?.ret_median)],
              ['超额 LCB(90%)', pct(s?.excess_lcb)],
              ['未成交 / 未到期', `${s?.not_filled ?? 0} / ${s?.pending ?? 0}`],
            ] as Array<[string, string, string?]>).map(([k, v, hint]) => (
              <div key={k} className="flex items-center justify-between gap-2">
                <dt className="flex items-baseline gap-1 text-muted">
                  {k}
                  {hint && <span className="text-[9px] opacity-70">{hint}</span>}
                </dt>
                <dd className="font-mono text-secondary">{v}</dd>
              </div>
            ))}
            {/* skill 单独一行：有效样本不足时降级显示，而不是删掉。
                删掉数字会让人去别处找同一个数字，而那里没有「不可判定」的语境。 */}
            <div className="flex items-center justify-between gap-2">
              <dt className="text-muted">Brier skill</dt>
              <dd className={cn('font-mono', weak ? 'text-muted' : 'text-secondary')}>
                {s?.skill == null ? '—' : `${(s.skill * 100).toFixed(1)}%`}
                {weak && <span className="ml-1 text-[9px] text-warning">不可判定</span>}
              </dd>
            </div>
          </dl>

          <p className="mt-2 border-t border-border pt-2 text-[10px] leading-relaxed text-muted">
            {s?.reason ?? '—'}。skill &gt; 0 才算有信息；样本少于 8 不给结论。
          </p>

          {weak && (
            <p className="mt-1 rounded-btn bg-warning/10 px-2 py-1 text-[10px] leading-relaxed text-warning">
              有效样本 {s?.n_eff}
              {s?.n_eff_reliable === false
                ? '（构造值：不重叠窗口数，Newey-West 在此样本量下不可用）' : ''}
              ：这个 skill 可以读，但不能当结论——{s?.days ?? 0} 个决策日只值{' '}
              {s?.n_eff} 个独立样本。
            </p>
          )}

          {/* 两个口径回答不同问题：abs = 赚没赚，median = 选得准不准。
              它们可能给出相反结论，所以并列显示，不合并成一个数字。 */}
          {sm && (
            <div className="mt-2 rounded-btn border border-border/70 bg-elevated/50 px-2 py-1.5">
              <div className="flex items-center justify-between">
                <span className="text-[10px] font-medium text-secondary">选股口径（对中位数）</span>
                {/* 三态，不是一个布尔：✓ = 结论成立，? = 不可判定，空白 = 测过了但没通过。
                    颜色只是辅助，符号本身携带语义。 */}
                <span className={cn('font-mono text-[10px]',
                  sm.significance === 'PASS' ? 'text-accent'
                    : sm.significance === 'UNKNOWN' ? 'text-warning' : 'text-muted')}>
                  {sm.skill == null ? '—' : `${(sm.skill * 100).toFixed(1)}%`}
                  {sm.significance === 'PASS' && ' ✓'}
                  {sm.significance === 'UNKNOWN' && ' ?'}
                </span>
              </div>
              <dl className="mt-1 space-y-0.5 text-[10px]">
                {([
                  ['跑赢中位数', pct(sm.hit_rate)],
                  ['样本 / 决策日', `${sm.n} / ${sm.days ?? 0}`],
                  // 与 abs 口径同源：`n_eff` 由 score() 一并返回，不是另算一套。
                  ['skill 的有效样本', sm.n_eff == null ? '—' : String(sm.n_eff)],
                  ['LCB(90%)', pct(sm.skill_lcb)],
                ] as Array<[string, string]>).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between">
                    <dt className="text-muted">{k}</dt>
                    <dd className="font-mono text-secondary">{v}</dd>
                  </div>
                ))}
              </dl>
              <p className="mt-1 text-[10px] leading-relaxed text-muted">
                基线精确为 50%；同日多个标的近似独立，所以 n 才是可信样本量。
              </p>
            </div>
          )}

          {prior && prior.p0 != null && (
            <div className="mt-2 rounded-btn bg-elevated px-2 py-1.5 text-[10px] leading-relaxed text-muted">
              常数基线：最近 {prior.n_dates} 个采样日、{prior.n_obs} 个观测中
              <span className="mx-1 font-mono text-secondary">{pct(prior.p0)}</span>
              在 {prior.horizon} 个交易日后上涨
              {prior.p0_se != null && (
                <span className="ml-1 text-warning">
                  ±{pct(1.96 * prior.p0_se, 1)}
                </span>
              )}
              。
              {prior.p0_range && (
                <span className="mt-0.5 block">
                  采样日之间从 {pct(prior.p0_range[0], 1)} 到 {pct(prior.p0_range[1], 1)}
                  ——它不是常数，别当固定基线用。
                </span>
              )}
            </div>
          )}

          <div className="mt-2 flex items-center gap-1 text-[10px] text-muted">
            <ArrowRight className="h-3 w-3" />
            结算 → 计分 → 下一张卡，闭环才算接线
          </div>
        </aside>
      </div>
    </div>
  )
}
