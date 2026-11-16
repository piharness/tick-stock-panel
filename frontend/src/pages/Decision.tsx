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
import { Fragment, useCallback, useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  RefreshCw, Loader2, ShieldCheck, ShieldAlert, ShieldQuestion,
  ScrollText, Target, ArrowRight, Bot, AlertTriangle, ChevronDown, ChevronRight,
} from 'lucide-react'
import {
  api,
  type DecisionAutoConfig,
  type DecisionCheck,
  type DecisionCheckStatus,
  type DecisionGateDecision,
  type DecisionOverview,
  type DecisionProduceSummary,
  type DecisionScore,
  type DecisionSlate,
  type DecisionSlateCard,
  type DecisionSlateSeries,
} from '@/lib/api'
import { toast } from '@/components/Toast'
import { QK } from '@/lib/queryKeys'
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
  UNKNOWN: '不可判定',
  BEATS_CONSTANT: '跑赢常数基线',
  NO_INFORMATION: '无信息增量',
  NOT_ESTABLISHED: '未成立',
  KILL: '已证伪',
}

/** 判决的配色必须与语义同向：只有 `BEATS_CONSTANT` 是绿，只有 `KILL` 是红。
 *  `NOT_ESTABLISHED` 走黄——它最容易被误读成「有戏」，而它恰恰是「还没测出来」。 */
const VERDICT_TONE: Record<string, string> = {
  BEATS_CONSTANT: 'bg-accent/10 text-accent',
  KILL: 'bg-danger/10 text-danger',
  NOT_ESTABLISHED: 'bg-warning/10 text-warning',
}

/** produce 结果的人话。门禁 STOP / 无证据都是诚实路径，不是错误。 */
const PRODUCE_STATUS_LABEL: Record<DecisionProduceSummary['status'], string> = {
  created: '已发卡',
  exists: '当日已有卡（幂等）',
  skipped_gate: '门禁 STOP，跳过（定时路径不越权）',
  skipped_no_picks: '策略当日无 picks，未发卡',
  skipped_no_evidence: '无 p_win 证据（冷启动），未发卡',
  disabled: '自动发卡未开启',
  no_strategy: '未绑定策略：只结算，不发卡',
  no_engine: '策略引擎未初始化，未发卡',
  scan_failed: '策略扫描失败',
  // 名单没成立的五种病因：各自可诊断，不许合并成「失败」。
  empty: '名单当日 0 只，未发卡',
  skipped_not_closed: '名单数据未收盘，未发卡',
  skipped_no_data: '无行情分区，未发卡',
  invalid_top_n: '名单口径非法（top_n 不可配），未发卡',
  failed: '名单生成失败，未发卡',
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

  // 自动发卡配置 + 可选策略。配置在偏好里，单独 PUT 部分更新。
  const autoCfg = useQuery({
    queryKey: QK.preferences,
    queryFn: () => api.preferences(),
    refetchOnWindowFocus: false,
    select: (p): DecisionAutoConfig | undefined => p.decision_auto,
  })
  const strategies = useQuery({
    queryKey: ['decision', 'auto-strategies'],
    queryFn: () => api.strategyList('stock', '1d'),
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  })

  // 每日名单：唯一产地的只读视图。`retry:false` —— 名单没生成是正常状态
  // （盘后管道还没跑），重试三次只会把「没有」变成「加载中」。
  //
  // `slateAsOf` 非空 = 正在回看某一天。它进 queryKey，所以回看不会污染
  // 「最新」那一份的缓存——两个问题分开问，答案分开存。
  const [slateAsOf, setSlateAsOf] = useState<string | null>(null)
  const slateQ = useQuery({
    queryKey: ['decision', 'slate', slateAsOf],
    queryFn: () => api.decisionSlate(slateAsOf ?? undefined),
    refetchOnWindowFocus: false,
    retry: false,
    staleTime: 30_000,
  })

  const saveAuto = useMutation({
    mutationFn: (patch: Partial<DecisionAutoConfig>) => api.updateDecisionAutoConfig(patch),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: QK.preferences }) },
    onError: e => toast(e instanceof Error ? e.message : '保存自动发卡配置失败'),
  })

  const produceNow = useCallback(async () => {
    try {
      const r = await api.decisionProduce()
      const label = PRODUCE_STATUS_LABEL[r.status] ?? r.status
      const detail = r.card_id ? `：${r.card_id}` : r.reason ? `：${r.reason}` : ''
      // 只有「真的发了卡」算 success；跳过/未开启走默认（醒目样式），
      // 它们是要用户注意的诚实路径，不是静默成功。
      toast(`${label}${detail}`, r.status === 'created' || r.status === 'exists' ? 'success' : 'error')
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['decision', 'overview'] }),
        qc.invalidateQueries({ queryKey: ['decision', 'score'] }),
      ])
    } catch (e) {
      toast(e instanceof Error ? e.message : '生产失败')
    }
  }, [qc])

  const cfg = autoCfg.data
  const strategyOpts = useMemo(
    () => (strategies.data?.strategies ?? []).map(st => ({ id: st.id, name: st.name })),
    [strategies.data],
  )

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
            onClick={() => { void overview.refetch(); void score.refetch(); void slateQ.refetch() }}
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

      {/* 今日名单：唯一产地的只读视图。卡与它逐位相等是构造性的，
          所以这里看到的就是「今天该挂哪几张单」。 */}
      <SlatePanel
        slate={slateQ.data?.slate}
        card={slateQ.data?.card}
        head={slateQ.data?.head}
        series={slateQ.data?.series}
        browsing={slateAsOf !== null}
        onSelect={setSlateAsOf}
        failed={slateQ.isError}
        error={slateQ.error instanceof Error ? slateQ.error.message : undefined}
      />

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
              VERDICT_TONE[s?.verdict ?? 'INSUFFICIENT'] ?? 'bg-elevated text-muted')}>
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
              // 超额下界走日聚 + HAC（与 skill_lcb 同口径）。`excess_lcb` 是逐 pick 的
              // iid 版本，同日相关与 20 日重叠都没计，只在旧数据没有新字段时兜底。
              ['超额 LCB(90%, HAC)', pct(s?.excess_lcb_hac ?? s?.excess_lcb)],
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
                  // `UNKNOWN` 时不许出现「跑赢」：n_eff=10 的 51.6% 命中率被写成
                  // 「跑赢中位数」，正是六态判决要拦的那种误读——数字可读，
                  // 结论不可判定。这条路只有 `PASS` 才配用「跑赢」这个词。
                  [sm.significance === 'PASS' ? '跑赢中位数' : '高于中位数占比',
                    pct(sm.hit_rate)],
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

      {/* 自动发卡 —— 盘后管道的「策略 → 卡」阶段。开关在这里显式选择：
          自动给不会真实执行的策略发卡会污染校准，所以默认关。 */}
      <section className="mt-3 rounded-card border border-border bg-surface p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Bot className="h-3.5 w-3.5 text-accent" />
          <label className="flex cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              checked={cfg?.enabled ?? false}
              disabled={autoCfg.isLoading || saveAuto.isPending}
              onChange={e => saveAuto.mutate({ enabled: e.target.checked })}
              className="h-3.5 w-3.5 accent-accent cursor-pointer"
            />
            <span className="text-xs font-medium text-foreground">自动发卡（盘后管道）</span>
          </label>
          <span className="text-[10px] text-muted">
            每个交易日收盘后自动：结算到期卡 → 跑策略取 Top-N → 反推 p_win → 门禁 → 封卡
          </span>
          <button
            onClick={() => { void produceNow() }}
            disabled={autoCfg.isLoading}
            className="ml-auto flex items-center gap-1 rounded-btn border border-border bg-surface px-2.5 py-1.5 text-xs text-secondary transition-colors hover:bg-elevated hover:text-foreground"
          >
            <Target className="h-3.5 w-3.5" /> 立即生产
          </button>
        </div>

        {cfg?.enabled && (
          <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1.5 border-t border-border/60 pt-2 text-[11px]">
            <label className="flex items-center gap-1.5">
              <span className="text-muted">绑定策略</span>
              <select
                value={cfg.strategy_id}
                disabled={saveAuto.isPending}
                onChange={e => saveAuto.mutate({ strategy_id: e.target.value })}
                className="rounded-btn border border-border bg-surface px-1.5 py-1 text-[11px] text-foreground"
              >
                <option value="">— 未选择（只结算不发卡）—</option>
                {strategyOpts.map(o => (
                  <option key={o.id} value={o.id}>{o.name || o.id}</option>
                ))}
              </select>
            </label>
            <span className="text-muted">
              Top <code className="font-mono text-secondary">{cfg.top_n}</code>
              {' · '}持有 <code className="font-mono text-secondary">{cfg.horizon}</code> 个交易日
              {' · '}p_win 口径 <code className="font-mono text-secondary">{cfg.basis}</code>
              {cfg.allow_prior && <span className="ml-1 text-warning">冷启动允许先验</span>}
            </span>
            {strategies.isLoading && <Loader2 className="h-3 w-3 animate-spin text-muted" />}
          </div>
        )}
      </section>
    </div>
  )
}

/** 名单状态的文案与配色。`ready` 是唯一「可以直接照着挂单」的状态，
 *  其余四种都是「为什么没有名单」——它们不该被同一个灰色 badge 盖住。 */
const SLATE_STATUS_LABEL: Record<DecisionSlate['status'], { label: string; tone: string }> = {
  ready: { label: '名单就绪', tone: 'bg-accent/10 text-accent' },
  empty: { label: '当日 0 只', tone: 'bg-warning/10 text-warning' },
  skipped_not_closed: { label: '数据未收盘', tone: 'bg-warning/10 text-warning' },
  skipped_no_data: { label: '无行情分区', tone: 'bg-danger/10 text-danger' },
  invalid_top_n: { label: '口径非法', tone: 'bg-danger/10 text-danger' },
  failed: { label: '生成失败', tone: 'bg-danger/10 text-danger' },
}

function fix(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

/**
 * 今日名单 —— 「今天买哪几只」的唯一可见入口。
 *
 * 三件事在这一块里同时成立，缺一条这个面板就会骗人：
 *  1. 名单与决策卡逐位相等（后端构造性保证，不是对拍出来的）；
 *  2. 落后就红：`stale` 由 API 在读取时刻重算，不听文件里的旧结论；
 *  3. 没有名单要说清是哪一种「没有」——未收盘、无分区、口径非法、生成失败
 *     是四种不同的病因，压成一个空表就等于让它们都无法诊断。
 */
function SlatePanel({ slate, card, head, series, browsing, onSelect, failed, error }: {
  slate?: DecisionSlate
  card?: DecisionSlateCard | null
  head?: DecisionSlate | null
  series?: DecisionSlateSeries
  /** 正在回看某一天，而不是「最新可执行的那一份」。 */
  browsing: boolean
  onSelect: (asOf: string | null) => void
  failed: boolean
  error?: string
}) {
  const [open, setOpen] = useState<string | null>(null)
  const navigate = useNavigate()
  const meta = slate ? SLATE_STATUS_LABEL[slate.status] : undefined
  const ready = slate?.status === 'ready' || slate?.status === 'empty'
  // 「最近一次运行没成立」与「名单落后」是两件事：前者要知道原因，
  // 后者要知道行情走到哪了。同时出现时只说前者——它更具体，
  // 而且它的结论（下面是最近一份仍可执行的名单）已经把后者包含进去了。
  const headMissed = !browsing && !!head && !!slate && head.as_of !== slate.as_of

  return (
    <section className="mt-3 rounded-card border border-border bg-surface">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-xs font-medium text-foreground">每日名单</h2>
          {slate?.as_of && (
            <span className="font-mono text-[11px] text-muted">
              {slate.as_of}
              {slate.entry_date
                ? ` → ${slate.entry_date} 开盘挂单`
                : ' → 次日挂单（次日分区未落盘）'}
            </span>
          )}
          {slate && ready && (
            <span className="font-mono text-[11px] text-secondary">
              {slate.n_rows} / {slate.top_n} 只
            </span>
          )}
          {card && (
            <span className="font-mono text-[10px] text-muted">
              卡 {card.card_id.slice(0, 14)}… · 在卡 {card.in_card}/{card.picks}
              {card.verified ? ' · 哈希校验通过' : ' · 哈希校验未通过'}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          {slate && slate.stale && (
            <span className="flex items-center gap-1 rounded bg-danger/10 px-1.5 py-0.5 text-[10px] text-danger">
              <AlertTriangle className="h-3 w-3" /> 落后 {slate.lag_sessions} 个交易日
            </span>
          )}
          {meta && (
            <span className={cn('rounded px-1.5 py-0.5 text-[10px]', meta.tone)}>
              {meta.label}
            </span>
          )}
        </div>
      </div>

      {/* 覆盖条：把「每日」这个词变成可数的东西。
          缺一天在单份文件里看不出来——目录里永远只有「有」和「没有」，
          而读者看的是最近一份，它看起来总是正常的。 */}
      {series && series.sessions > 0 && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-border px-3 py-1.5">
          <span className="text-[10px] text-muted">近 {series.sessions} 个交易日</span>
          <div className="flex items-end gap-[2px]">
            {series.days.map(d => {
              const tone = !d.present || d.status === null
                ? 'bg-danger/60'
                : d.status === 'ready'
                  ? 'bg-accent/70'
                  : d.status === 'empty'
                    ? 'bg-accent/25'
                    : 'bg-warning/70'
              const label = d.present
                ? (d.status ? (SLATE_STATUS_LABEL[d.status]?.label ?? d.status) : '不可读')
                : '缺失'
              return (
                <button
                  key={d.as_of}
                  type="button"
                  title={`${d.as_of}  ${label}${d.reason ? `：${d.reason}` : ''}`}
                  // 缺失的那天不可点：点开只会得到 404，而 404 不能告诉你
                  // 「那天为什么没算」——原因在 series 里，不在那一天的文件里。
                  onClick={() => d.present && onSelect(d.as_of)}
                  disabled={!d.present}
                  className={cn(
                    'h-3 w-2 rounded-[1px]',
                    tone,
                    d.present && 'hover:opacity-80',
                    slate?.as_of === d.as_of && 'ring-1 ring-foreground',
                  )}
                />
              )
            })}
          </div>
          <span className="font-mono text-[10px] text-muted">
            {series.ready + series.empty} 成立 · {series.holes.length} 缺口
          </span>
          {browsing && (
            <button
              type="button"
              onClick={() => onSelect(null)}
              className="rounded border border-border px-1.5 py-0.5 text-[10px] text-secondary hover:bg-elevated"
            >
              回到最新
            </button>
          )}
          {series.holes.length > 0 && (
            <span className="font-mono text-[10px] text-danger">
              缺口 {series.holes.slice(0, 5).join(' ')}
              {series.holes.length > 5 ? ` 等 ${series.holes.length} 天` : ''}
            </span>
          )}
        </div>
      )}

      {headMissed && head && (
        <div className="mx-3 mt-2 rounded border border-warning/40 bg-warning/10 px-2.5 py-2 text-[11px] leading-relaxed text-warning">
          最近一次运行（<code className="font-mono">{head.as_of}</code>）没有成立：
          {head.reason ?? (head.status ? SLATE_STATUS_LABEL[head.status].label : '未知原因')}。
          下面是最近一份成立的名单（<code className="font-mono">{slate?.as_of ?? '—'}</code>）——
          它仍是可照挂的那一份，但不是今天新算的。
        </div>
      )}

      {slate && slate.stale && !headMissed && (
        <div className="mx-3 mt-2 rounded border border-danger/40 bg-danger/10 px-2.5 py-2 text-[11px] leading-relaxed text-danger">
          名单停在 <code className="font-mono">{slate.as_of ?? '—'}</code>，行情已到{' '}
          <code className="font-mono">{slate.expected_as_of}</code>。
          {card
            ? '这张卡仍是有效的承诺，但它承诺的不是最新一天。'
            : '这段时间没有对应的决策卡。'}
          {' '}先查盘后管道 Step 2.9（决策账本）有没有成功。
        </div>
      )}

      {!slate ? (
        <div className="px-3 py-6 text-center text-[11px] text-muted">
          {failed
            ? `名单还没生成${error ? `（${error}）` : ''}。盘后管道 Step 2.9 跑完后，这里会出现当天名单。`
            : '加载名单…'}
        </div>
      ) : !ready ? (
        <div className="px-3 py-6 text-center">
          <p className="text-xs text-secondary">这份名单没有成立。</p>
          <p className="mx-auto mt-1 max-w-lg text-[11px] leading-relaxed text-muted">
            {slate.reason ?? slate.warnings[0] ?? '没有记录原因——这本身是缺陷'}
          </p>
        </div>
      ) : slate.rows.length === 0 ? (
        <div className="px-3 py-6 text-center">
          <p className="text-xs text-secondary">当日 0 只满足条件。</p>
          <p className="mx-auto mt-1 max-w-lg text-[11px] leading-relaxed text-muted">
            「0 只」是名单成立的结果，不是管道没跑：全市场 mom20 排名里没有可挂单的标的。
          </p>
        </div>
      ) : (
        <table className="w-full text-left text-[11px]">
          <thead className="text-muted">
            <tr className="border-b border-border">
              <th className="w-8 px-3 py-1.5 font-medium">#</th>
              <th className="px-2 py-1.5 font-medium">代码</th>
              <th className="px-2 py-1.5 text-right font-medium">昨收</th>
              <th className="px-2 py-1.5 text-right font-medium">挂单限价</th>
              <th className="px-2 py-1.5 text-right font-medium">mom20</th>
              <th className="px-2 py-1.5 text-right font-medium">成交额</th>
              <th className="px-2 py-1.5 text-center font-medium">在卡内</th>
              <th className="w-6 px-3 py-1.5" />
            </tr>
          </thead>
          <tbody>
            {slate.rows.map(row => {
              const expanded = open === row.symbol
              return (
                <Fragment key={row.symbol}>
                  <tr
                    onClick={() => setOpen(expanded ? null : row.symbol)}
                    className="cursor-pointer border-b border-border/60 hover:bg-elevated/60"
                  >
                    <td className="px-3 py-1.5 font-mono text-muted">{row.rank}</td>
                    <td className="px-2 py-1.5 font-mono text-foreground">
                      {/* 下钻的第二步：行内展开是「这张单的细节」，
                          点代码是「这只票长什么样」。两者都要，不能只留一个。 */}
                      <button
                        type="button"
                        onClick={e => {
                          e.stopPropagation()
                          navigate(`/stock-analysis?symbol=${encodeURIComponent(row.symbol)}`)
                        }}
                        className="underline decoration-dotted underline-offset-2 hover:text-accent"
                      >
                        {row.symbol}
                      </button>
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono text-secondary">{fix(row.prev_close_raw)}</td>
                    <td className="px-2 py-1.5 text-right font-mono font-medium text-foreground">{fix(row.limit_price)}</td>
                    <td className={cn('px-2 py-1.5 text-right font-mono',
                      (row.mom20_pct ?? 0) >= 0 ? 'text-danger' : 'text-accent')}>
                      {row.mom20_pct == null ? '—' : `${fix(row.mom20_pct, 1)}%`}
                    </td>
                    <td className="px-2 py-1.5 text-right font-mono text-muted">
                      {row.day_amount_yi == null ? '—' : `${fix(row.day_amount_yi)} 亿`}
                    </td>
                    <td className="px-2 py-1.5 text-center">
                      {/* 不用 ✓：本页把 ✓ 保留给「门禁通过」（见 weakSample
                          那条文案闸的断言 `not.toContain('✓')`）。同一符号
                          两种语义，那条闸就会静默失效。 */}
                      {row.in_card
                        ? <span className="text-accent">卡内</span>
                        : <span className="text-muted">—</span>}
                    </td>
                    <td className="px-3 py-1.5 text-muted">
                      {expanded ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                    </td>
                  </tr>
                  {expanded && (
                    <tr className="border-b border-border/60 bg-elevated/40">
                      <td colSpan={8} className="px-3 py-2">
                        <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[11px] sm:grid-cols-4">
                          <div>
                            <dt className="text-muted">限价口径</dt>
                            <dd className="font-mono text-secondary">
                              floor({fix(row.prev_close_raw)} × 0.99, 0.01) = {fix(row.limit_price)}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-muted">成交条件</dt>
                            <dd className="text-secondary">
                              次日开盘 ≤ {fix(row.limit_price)}（低开 ≥ {fix(Math.abs(row.gap_threshold_pct), 1)}%）
                            </dd>
                          </div>
                          <div>
                            <dt className="text-muted">当日可成交</dt>
                            <dd className={row.tradable ? 'text-accent' : 'text-warning'}>
                              {row.tradable ? '有有效开盘价' : '无有效开盘（停牌或缺数据）'}
                            </dd>
                          </div>
                          <div>
                            <dt className="text-muted">卡内 p_win</dt>
                            <dd className="font-mono text-secondary">
                              {row.p_win == null ? '—' : `${fix(row.p_win * 100, 2)}%`}
                            </dd>
                          </div>
                        </dl>
                        {row.limit_mismatch && (
                          <p className="mt-1.5 text-[11px] text-danger">
                            卡的限价 {fix(row.card_limit_price)} 与名单 {fix(row.limit_price)} 不一致：
                            名单在封卡之后被重算过，这份承诺与当前名单已经不是同一件事。
                          </p>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
          </tbody>
        </table>
      )}

      {slate && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border px-3 py-2 text-[10px] text-muted">
          <span>截断：{slate.truncation.rule ?? '—'}</span>
          <span>面板起点 {slate.panel_since ?? '—'}</span>
          <span>结构 v{slate.schema_revision}</span>
          <span className="font-mono">生成于 {slate.built_at?.slice(0, 19).replace('T', ' ') ?? '—'}</span>
          {slate.origin && (
            <span className={slate.origin === 'backfill' ? 'text-warning' : undefined}>
              {slate.origin === 'backfill' ? '补算' : '管道自动'}
            </span>
          )}
          {slate.warnings.map((w, i) => (
            <span key={i} className="text-warning">· {w}</span>
          ))}
        </div>
      )}
    </section>
  )
}
