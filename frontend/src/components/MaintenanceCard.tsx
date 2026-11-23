/**
 * 自动维护卡（数据页顶部）—— 行情与投资手册两条链，一个按钮。
 *
 * 交互取舍（按「无感知 > 手动」排）：
 *   1. 进页面先看状态；落后就在后台跑一次，不弹窗、不挡操作；
 *   2. 每「应收盘日」只自动尝试一次：失败不自动重试，避免整页刷新时反复起任务；
 *   3. 任何重任务在跑（不限于本卡触发的）都接管显示，按钮禁用 —— 单飞由后端保证，
 *      前端不重复实现锁，只负责「别让用户以为没反应」；
 *   4. 状态不可判（体检脚本不可用）按「需要更新」显示，不给一句假的「已是最新」。
 *
 * 为什么状态不自己算日期：交易日历只有一个定义点（analysis/trading_calendar.py），
 * 后端把 expected_session 一起返回；前端只比较字符串，不引第二套节假日表。
 */
import { useEffect, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, Loader2, RefreshCw } from 'lucide-react'
import { api, type MaintenanceDomain } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'

/** 自动尝试的幂等键：按「应收盘日」隔离，第二天重新武装；失败后同一天不再自动跑。 */
const autoKey = (expected: string | null) => `tsp:maintenance:auto:${expected ?? 'unknown'}`

const STAGE: Record<string, string> = {
  init: '准备',
  maintenance: '维护',
  handbook: '投资手册',
}
const stageText = (stage: string) =>
  stage.startsWith('pipeline') ? '行情同步' : STAGE[stage] ?? stage

const PHASE: Record<string, string> = {
  closed: '已收盘',
  pre_close: '盘中',
  holiday: '休市',
  unknown: '状态未知',
}

export function MaintenanceCard() {
  const qc = useQueryClient()
  const status = useQuery({
    queryKey: QK.maintenance,
    queryFn: api.maintenance,
    // 有任务在跑就 2s 一刷；平时 30s —— 与数据页其它轮询同频，不做更密的请求。
    refetchInterval: q => (q.state.data?.job ? 2000 : 30000),
    staleTime: 3000,
  })
  const run = useMutation({
    mutationFn: api.maintenanceRun,
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.maintenance }),
  })
  const cancel = useMutation({
    mutationFn: (id: string) => api.pipelineJobCancel(id),
    onSuccess: () => void qc.invalidateQueries({ queryKey: QK.maintenance }),
  })

  const data = status.data
  const job = data?.job ?? null
  const running = !!job

  // 无感更新：落后且没有任务在跑时，每个应收盘日自动跑一次。
  // 盘中不自动跑：那时段落后通常是「昨天的链没跑」，自动补跑会和实时行情抢数据源；
  // 收盘后/休市才自动，盘中留手动按钮。
  useEffect(() => {
    if (!data || running || run.isPending) return
    if (!data.stale || data.phase === 'unknown' || data.phase === 'pre_close') return
    const key = autoKey(data.expected)
    if (sessionStorage.getItem(key)) return
    sessionStorage.setItem(key, '1')
    run.mutate()
  }, [data, running, run])

  // 任务刚结束：行情与手册都可能变了，广域失效一次（与数据页「立即同步」完成时同策略）。
  const hadJob = useRef(false)
  useEffect(() => {
    if (running) {
      hadJob.current = true
      return
    }
    if (hadJob.current) {
      hadJob.current = false
      void qc.invalidateQueries()
    }
  }, [running, qc])

  if (status.isLoading) {
    return <div className="mt-4 h-[104px] animate-pulse rounded-xl border border-border bg-surface" />
  }

  if (status.isError || !data) {
    return (
      <div className="mt-4 flex items-center justify-between rounded-xl border border-border bg-surface px-4 py-3">
        <div className="text-[11.5px] text-secondary">维护状态读取失败：{String((status.error as Error)?.message ?? '未知原因')}</div>
        <button
          type="button"
          onClick={() => void status.refetch()}
          className="flex items-center gap-1 rounded border border-border px-2 py-1 text-[10.5px] text-secondary hover:text-foreground"
        >
          <RefreshCw className="h-3 w-3" /> 重试
        </button>
      </div>
    )
  }

  const fresh = !data.stale
  const failed = !running ? data.last?.error : null
  // 起任务本身失败（运行槽被占/接口 5xx）与「任务跑失败」是两件事，但都只需一行原因。
  const startError = run.error ? String((run.error as Error).message ?? run.error) : null

  return (
    <section className="mt-4 rounded-xl border border-border bg-surface px-4 py-3.5" aria-label="自动维护">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className={cn('h-1.5 w-1.5 rounded-full', fresh ? 'bg-accent' : 'bg-warning')} />
            <span className="text-[12.5px] font-medium text-foreground">自动维护</span>
            <span className="text-[10px] text-secondary">
              {PHASE[data.phase] ?? data.phase}
              {data.expected ? ` · 应收盘日 ${data.expected}` : ''}
              {data.now ? ` · 检查于 ${data.now.slice(11, 16)}` : ''}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1.5">
            {data.domains.map(d => <DomainLine key={d.id} d={d} />)}
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {running ? (
            <button
              type="button"
              onClick={() => cancel.mutate(job.id)}
              disabled={cancel.isPending}
              className="rounded border border-border px-2 py-1 text-[10.5px] text-secondary hover:text-foreground disabled:opacity-40"
            >
              停止
            </button>
          ) : (
            <button
              type="button"
              onClick={() => void status.refetch()}
              title="重新检查"
              className="rounded p-1.5 text-secondary hover:text-foreground"
            >
              <RefreshCw className={cn('h-3.5 w-3.5', status.isFetching && 'animate-spin')} />
            </button>
          )}
          <button
            type="button"
            disabled={running || run.isPending || fresh}
            onClick={() => run.mutate()}
            className={cn(
              'flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[11.5px] font-medium transition-colors',
              fresh
                ? 'cursor-default border border-border text-secondary'
                : 'bg-accent text-white hover:opacity-90 disabled:opacity-60',
            )}
          >
            {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : fresh ? <Check className="h-3.5 w-3.5" />
                : <RefreshCw className="h-3.5 w-3.5" />}
            {running ? `更新中 ${job.progress}%` : fresh ? '已是最新' : '更新数据'}
          </button>
        </div>
      </div>

      {running && (
        <div className="mt-2.5">
          <div className="h-1 w-full overflow-hidden rounded-full bg-elevated">
            <div className="h-full rounded-full bg-accent transition-[width] duration-500" style={{ width: `${Math.max(2, job.progress)}%` }} />
          </div>
          <div className="mt-1 flex items-center justify-between gap-2 text-[10px] text-secondary">
            <span className="truncate">{stageText(job.stage)}{job.message ? ` · ${job.message}` : ''}</span>
            <span className="shrink-0 font-mono tabular-nums">{job.progress}%</span>
          </div>
        </div>
      )}

      {(failed || startError) && (
        <div className="mt-2 flex items-start gap-1.5 text-[10.5px] leading-relaxed text-danger">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>{startError ? `无法启动更新：${startError}` : `上次更新失败：${failed}`}</span>
          <button type="button" onClick={() => run.mutate()} className="shrink-0 underline underline-offset-2">重试</button>
        </div>
      )}

      <p className="mt-2 text-[10px] leading-relaxed text-secondary/80">
        落后时进入本页会自动补跑一次；行情落后先同步行情，再跑手册链（扫描 → 账户 → 回放）。数据已是最新时不启动任何任务。
      </p>
    </section>
  )
}

function DomainLine({ d }: { d: MaintenanceDomain }) {
  return (
    <div className="flex min-w-0 items-baseline gap-1.5 text-[10.5px]">
      <span className={cn('h-1.5 w-1.5 shrink-0 self-center rounded-full', d.stale ? 'bg-warning' : 'bg-accent/70')} />
      <span className="font-medium text-foreground">{d.label}</span>
      <span className="font-mono tabular-nums text-secondary">{d.asof ?? '未生成'}</span>
      <span className="truncate text-secondary/80">{d.stale ? d.detail : '最新'}</span>
    </div>
  )
}
