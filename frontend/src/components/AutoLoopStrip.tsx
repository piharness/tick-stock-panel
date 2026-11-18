/**
 * 研究闭环状态条 — 策略 / 因子 / 回测三页共用的「这个页面还需不需要人管」。
 *
 * 口径全部来自后端 `/api/backtest/lab/report` 的 `loop` 块 (services/research_loop.py):
 * 阈值、计龄方式、准入判定都不在这里算第二份。本组件只负责:
 * - 三页新鲜度 (fresh / aging / stale / never) 与「几天没产出」;
 * - 主动搜索的战果与 DSR 结论 (「找到 11 个主题，但没有一个过噪声门槛」也是结论);
 * - 准入/联动缺口 (有几个策略卡在未复验、有几个已准入但还没接账本/模拟仓)。
 *
 * 失败必须可见: 状态不可用时显示明确一行, 不能因为「没渲染」而让人以为一切正常。
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Activity, AlertTriangle, ChevronDown, FlaskConical, Play } from 'lucide-react'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export type LoopPage = 'strategy' | 'factor' | 'backtest'

const PAGE_ROUTE: Record<LoopPage, string> = {
  strategy: '/factors?tab=discover&view=mining',
  factor: '/factors?tab=discover',
  backtest: '/backtest?tab=robustness',
}

const STATE_META: Record<string, { label: string; dot: string; text: string }> = {
  fresh: { label: '新鲜', dot: 'bg-emerald-500', text: 'text-emerald-500' },
  aging: { label: '待更新', dot: 'bg-amber-500', text: 'text-amber-500' },
  stale: { label: '已过期', dot: 'bg-red-500', text: 'text-red-500' },
  never: { label: '无产出', dot: 'bg-border', text: 'text-muted' },
}

function fmtAge(entry: { age_days: number | null; state: string }): string {
  if (entry.state === 'never' || entry.age_days == null) return '无产出'
  return entry.age_days === 0 ? '今日' : `${entry.age_days} 天前`
}

function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function AutoLoopStrip({ page }: { page: LoopPage }) {
  const [open, setOpen] = useState(false)
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { data, isLoading, isError } = useQuery({
    queryKey: QK.labReport,
    queryFn: () => api.labReport(),
    staleTime: 60_000,
  })
  // 只在展开时拉：折叠态已经显示「上次搜索的战果」，调度细节不值得三页各发一次请求。
  const alphaStatus = useQuery({
    queryKey: QK.labAlphaStatus,
    queryFn: () => api.labAlphaStatus(),
    enabled: open,
    staleTime: 30_000,
  })
  const triggerAlpha = useMutation({
    mutationFn: () => api.labAlphaRun(),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: QK.labAlphaStatus })
    },
  })

  const shell = 'mb-3 rounded-btn border border-border/60 bg-surface px-3 py-2 text-[11px] leading-5'

  if (isLoading) {
    return (
      <div className={`${shell} flex h-[38px] items-center text-muted`}>
        研究闭环状态加载中…
      </div>
    )
  }

  const loop = data?.loop
  if (isError || !loop || !loop.available) {
    return (
      <div className={`${shell} flex items-center gap-1.5 text-muted`}>
        <AlertTriangle className="h-3.5 w-3.5 text-amber-500" aria-hidden="true" />
        研究闭环状态不可用
        {loop && !loop.available && loop.error ? `：${loop.error}` : ''}
        <span className="text-muted/70">· 页面数据本身不受影响</span>
      </div>
    )
  }

  const entries = loop.freshness ?? []
  const current = entries.find(item => item.page === page)
  const staleCount = entries.filter(item => item.state === 'stale' || item.state === 'never').length
  const alpha = current?.detail?.alpha_run ?? entries.find(i => i.page === 'factor')?.detail?.alpha_run ?? null
  const alphaThemes = alpha?.top_themes ?? []
  const admission = loop.admission
  const nextActions = loop.next_actions ?? []
  const market = loop.market
  const worst = current?.state ?? 'never'
  const worstMeta = STATE_META[worst] ?? STATE_META.never

  return (
    <div className={`${shell} ${worst === 'stale' ? 'border-red-500/30' : ''}`}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
          <Activity className={`h-3.5 w-3.5 ${worstMeta.text}`} aria-hidden="true" />
          研究闭环
        </span>

        <span className="inline-flex items-center gap-1 text-secondary">
          <span className={`inline-block h-1.5 w-1.5 rounded-full ${worstMeta.dot}`} aria-hidden="true" />
          本页{worstMeta.label} · {fmtAge(current ?? { age_days: null, state: 'never' })}
        </span>

        <span className="text-muted">
          {loop.autonomy?.any_enabled ? '研究自动化：部分开启' : '研究自动化：未开启'}
        </span>

        {market && market.state !== 'unknown' && (
          <span
            className={market.state === 'risk_off' ? 'text-amber-500' : 'text-secondary'}
            title={market.note}
          >
            {market.state === 'risk_off' ? 'β 逆风' : 'β 正常'} · 广度{' '}
            {fmtPct(market.breadth_20d, 0)}
          </span>
        )}

        <span className="text-secondary">
          准入 {admission?.admitted ?? 0}/{admission?.total ?? 0}
          {staleCount > 0 ? ` · ${staleCount} 页待更新` : ''}
        </span>

        {alpha && (
          <span className="inline-flex items-center gap-1 text-secondary">
            <FlaskConical className="h-3 w-3" aria-hidden="true" />
            主动搜索 {alpha.generated ?? '—'} 试验 → {alpha.confirmed_themes ?? 0} 主题
            {(alpha.dsr_pass ?? 0) === 0 && (alpha.confirmed_themes ?? 0) > 0 && (
              <span className="text-amber-500">· 无一过 DSR 门槛</span>
            )}
          </span>
        )}

        <button
          type="button"
          onClick={() => setOpen(value => !value)}
          aria-expanded={open}
          className="ml-auto inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-muted hover:bg-elevated hover:text-foreground"
        >
          {open ? '收起' : '详情'}
          <ChevronDown className={`h-3 w-3 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden="true" />
        </button>

        <button
          type="button"
          onClick={() => navigate(PAGE_ROUTE[page])}
          className="rounded-btn border border-border px-1.5 py-0.5 text-muted hover:bg-elevated hover:text-foreground"
        >
          去维护
        </button>
      </div>

      {open && (
        <div className="mt-2 grid gap-2 border-t border-border/60 pt-2 md:grid-cols-2 xl:grid-cols-5">
          <div>
            <div className="mb-1 font-medium text-secondary">三页新鲜度</div>
            <ul className="space-y-0.5">
              {entries.map(entry => {
                const meta = STATE_META[entry.state] ?? STATE_META.never
                return (
                  <li key={entry.page} className="flex items-center gap-1.5">
                    <span className={`inline-block h-1.5 w-1.5 rounded-full ${meta.dot}`} aria-hidden="true" />
                    <span className="text-foreground">{entry.label}</span>
                    <span className={meta.text}>{meta.label}</span>
                    <span className="text-muted">{fmtAge(entry)}</span>
                    <span className="text-muted/70">门槛 {entry.fresh_days}/{entry.stale_days} 天</span>
                  </li>
                )
              })}
            </ul>
          </div>

          <div>
            <div className="mb-1 font-medium text-secondary">市场环境</div>
            {market ? (
              <ul className="space-y-0.5 text-muted">
                <li className={market.state === 'risk_off' ? 'text-amber-500' : 'text-foreground'}>
                  {market.note}
                </li>
                {market.evidence && (
                  <li>
                    该状态历史 20 日后中位 {market.evidence.fwd20_median_pct.toFixed(2)}% · 正收益概率{' '}
                    {market.evidence.fwd20_positive_pct}%（{market.evidence.same_state_days} 日）
                  </li>
                )}
                {/* 这一行是刻意的: 环境层不做阻断, 因为阈值不稳健且无预测力。
                    不写清楚, 「β 逆风」会被读成「要跌了」。 */}
                <li className="text-muted/70">仅描述环境，不否决准入</li>
                {loop.crowding?.available && (
                  <li className="text-muted/70">{loop.crowding.note}</li>
                )}
              </ul>
            ) : (
              <div className="text-muted">环境数据不可用</div>
            )}
          </div>

          <div>
            <div className="mb-1 font-medium text-secondary">准入与联动</div>
            <ul className="space-y-0.5 text-muted">
              <li>
                已准入 {admission?.admitted ?? 0} · 拒绝 {admission?.blocked ?? 0}
                {admission?.total ? `（${admission.total} 个已入护策略）` : '（暂无策略入护）'}
              </li>
              <li>
                账本 {loop.linkage?.ledger?.enabled ? '开启' : '关闭'}
                {loop.linkage?.ledger?.strategy_id ? ` · ${loop.linkage.ledger.strategy_id}` : ' · 未指定策略'}
                {loop.linkage?.ledger?.execute ? ' · 自动下单' : ''}
              </li>
              <li>
                模拟仓 {loop.linkage?.paper?.accounts ?? 0} 个账户 · 跟单规则{' '}
                {loop.linkage?.paper?.enabled_rules ?? 0} 条
              </li>
              {(admission?.items ?? []).some(item => item.regime === 'headwind') && (
                <li className="text-amber-500">
                  准入通过 ≠ 该加仓：β 逆风下中长期多头腿没有顺风
                </li>
              )}
              {(() => {
                const exposures = (admission?.items ?? [])
                  .map(item => item.crowding)
                  .filter((row): row is NonNullable<typeof row> => Boolean(row))
                if (exposures.length === 0) return null
                const worst = exposures.reduce((a, b) => (a.pct >= b.pct ? a : b))
                return (
                  <li className={worst.crowded ? 'text-amber-500' : 'text-muted'}>
                    持仓拥挤度最高 {fmtPct(worst.pct, 0)}
                    {worst.card_as_of ? `（${worst.card_as_of} 的卡）` : ''}
                    {worst.crowded ? ' · 主要在高换手区选股' : ''}
                  </li>
                )
              })()}
            </ul>
          </div>

          <div>
            <div className="mb-1 font-medium text-secondary">下一步</div>
            {nextActions.length === 0 ? (
              <div className="text-muted">没有待办：三页都在门槛内。</div>
            ) : (
              <ul className="space-y-0.5">
                {nextActions.map((action, index) => (
                  <li
                    key={`${action.page}-${index}`}
                    className={action.level === 'warn' ? 'text-amber-500' : 'text-muted'}
                  >
                    {action.text}
                  </li>
                ))}
              </ul>
            )}
            {alpha && alphaThemes.length > 0 && (
              <div className="mt-1.5">
                <div className="mb-0.5 font-medium text-secondary">
                  主动搜索确认主题（最高 DSR {fmtPct(alphaThemes[0]?.dsr, 0)}，门槛{' '}
                  {fmtPct(alpha.min_dsr, 0)}）
                </div>
                <ul className="space-y-0.5">
                  {alphaThemes.slice(0, 3).map(theme => (
                    <li key={theme.candidate_id} className="truncate text-muted" title={theme.formula}>
                      <span className="text-foreground">{theme.direction === 'low' ? '反向' : '正向'}</span>{' '}
                      {theme.formula}
                      <span className="text-muted/70">
                        {' '}· 确认 IC {fmtPct(theme.confirmation_ic, 2)} · DSR {fmtPct(theme.dsr, 2)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {/* 调度本身。上面四块说的是「产物长什么样」，这一块说的是「它还会不会继续产出」——
              一个每周都在更新结论、但触发方式是人工点按钮的实验室，仍然需要人。 */}
          <div>
            <div className="mb-1 font-medium text-secondary">主动搜索调度</div>
            {alphaStatus.isLoading ? (
              <div className="text-muted">读取中…</div>
            ) : !alphaStatus.data ? (
              <div className="text-muted">调度状态不可用</div>
            ) : (
              <>
                {(() => {
                  const cfg = alphaStatus.data.config
                  const sch = alphaStatus.data.schedule
                  const last = sch.latest
                  return (
                    <ul className="space-y-0.5 text-muted">
                      <li className={cfg.enabled ? 'text-foreground' : ''}>
                        {cfg.enabled
                          ? `每月 ${cfg.alpha_search_day} 日 · ${cfg.alpha_search_profile} · ${cfg.alpha_search_max_formulas} 试验`
                          : '已关闭（手工仍可触发）'}
                      </li>
                      <li>
                        上次：{sch.latest_claim ?? '从未触发'}
                        {last?.status ? ` · ${last.status}` : ''}
                        {last?.started_at ? ` · ${String(last.started_at).slice(0, 16).replace('T', ' ')}` : ''}
                      </li>
                      {last?.status === 'failed' && last.error && (
                        <li className="text-red-500" title={last.error}>失败：{last.error}</li>
                      )}
                      {last?.counts && (
                        <li>
                          {last.counts.evaluated ?? 0} 试验 → {last.counts.confirmed_themes ?? 0} 主题
                        </li>
                      )}
                      <li className={sch.running ? 'text-amber-500' : ''}>
                        {sch.running ? `正在跑（${sch.running_claim ?? '—'}）` : '空闲'}
                      </li>
                      {sch.error && <li className="text-red-500">状态文件读不出：{sch.error}</li>}
                    </ul>
                  )
                })()}
                <button
                  type="button"
                  onClick={() => triggerAlpha.mutate()}
                  disabled={triggerAlpha.isPending || (alphaStatus.data?.schedule?.running ?? false)}
                  className="mt-1 inline-flex items-center gap-1 rounded-btn border border-border px-1.5 py-0.5 text-muted hover:bg-elevated hover:text-foreground disabled:opacity-50"
                >
                  <Play className="h-3 w-3" aria-hidden="true" />
                  {triggerAlpha.isPending ? '触发中…' : '现在跑一次'}
                </button>
                {triggerAlpha.data && (
                  <div className="mt-0.5 text-muted/70">
                    {triggerAlpha.data.status === 'started'
                      ? `已触发 ${triggerAlpha.data.claim}，约 5 分钟`
                      : triggerAlpha.data.status === 'already_running'
                        ? `已有任务在跑（${triggerAlpha.data.claim}）`
                        : `未触发：${triggerAlpha.data.status}`}
                  </div>
                )}
                {triggerAlpha.isError && (
                  <div className="mt-0.5 text-red-500">
                    触发失败：{triggerAlpha.error instanceof Error ? triggerAlpha.error.message : '未知错误'}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
