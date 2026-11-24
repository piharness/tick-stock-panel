/**
 * 「历史回放」视图 —— 选一个交易日看当时能看见什么，再用轨迹评估首次触发。
 *
 * 三个问题分开回答：
 *   01 单日回放：某日候选/排除的完整快照（复用今日扫描的同一张表）
 *   02 信号轨迹：六只行业从哪天开始满足、连续多久、中途是否退出
 *   03 买点效果：首次触发后 5/10/20 日与 MAE/MFE（事后评价，不参与判定）
 *
 * 数据来自 analysis/etf_watch_replay.py 的离线产出；页面不重算判定。
 * 回放的两个未消除偏差（行业分类用当前名称、ETF 池为当前在市）在口径卡片里明示。
 */
import { useCallback, useEffect, useState } from 'react'
import type {
  EtfWatchOutcome, EtfWatchReplay, EtfWatchReplayMeta, EtfWatchReplaySnapshot, EtfWatchTrack, EtfWatchTrackDay,
} from '@/lib/api'
import { api } from '@/lib/api'
import { ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react'
import { cn } from '@/lib/cn'
import { scrollToId } from './Shell'
import { ScanTable, statusLabel, statusTone } from './ScanTable'
import { Callout, DataTable, type Col, Empty, Fold, Section, Stat, Tag } from './ui'

const PIT_LABEL: Record<string, string> = {
  liquidity: '流动性排名',
  prices: '价格与复权',
  regime: '市场状态',
  stale: '停更检查',
  industry_classification: '行业分类',
  universe: 'ETF 池',
}

const fmtPct = (v: number | null | undefined, digits = 1) => {
  if (v == null) return '—'
  const sign = v > 0 ? '+' : v < 0 ? '−' : ''
  return `${sign}${(Math.abs(v) * 100).toFixed(digits)}%`
}

const retCls = (v: number | null | undefined) =>
  v == null ? 'text-secondary/70' : v > 0 ? 'text-bull' : v < 0 ? 'text-bear' : 'text-secondary'

const shortDate = (d: string) => d.slice(5)
const fullDate = (d: string) => d

function offsetLabel(date: string, sessions: string[]): string {
  const i = sessions.indexOf(date)
  if (i < 0) return ''
  const n = sessions.length - 1 - i
  return n === 0 ? 'T' : `T-${n}`
}

export function ReplayPanel({ active }: { active: boolean }) {
  const [data, setData] = useState<EtfWatchReplay | null>(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(false)
  const [selectedRaw, setSelected] = useState('')
  const [range, setRange] = useState<10 | 20 | 40>(20)

  const load = useCallback(() => {
    setLoading(true)
    setErr('')
    api.etfWatchReplay()
      .then(d => setData(d))
      .catch((e: Error) => setErr(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    if (active && !data && !loading && !err) load()
  }, [active, data, loading, err, load])

  // 数据加载后若目标章节是 #replay-track，滚动发生在数据之前会 no-op，这里补一次。
  useEffect(() => {
    if (!data) return
    const h = window.location.hash.replace(/^#/, '')
    if (!['replay-day', 'replay-track', 'replay-outcome'].includes(h)) return
    const timer = window.setTimeout(() => scrollToId(h), 120)
    return () => window.clearTimeout(timer)
  }, [data])

  if (!data) {
    return (
      <Section
          id="replay-day"
          no="01"
          title="历史买点回放与信号追踪"
          first
          lead="选一个交易日，看当时收盘能看见的候选；再用信号轨迹判断是提前发现，还是跌了几天才识别。"
          source="data/etf_watch/replay.json · analysis/etf_watch_replay.py"
        >
          {loading ? (
            <div className="mt-4 flex items-center gap-2 border-t border-border pt-4 text-[13px] text-muted">
              <RefreshCw className="h-3.5 w-3.5 animate-spin" />
              正在读取历史回放…
            </div>
          ) : err ? (
            <div className="mt-4 border-l-2 border-warning pl-4">
              <div className="text-[13px] font-medium text-foreground">历史回放未生成</div>
              <p className="mt-2 text-[13px] leading-[1.75] text-muted">{err}</p>
              <pre className="mt-3 inline-block rounded border border-border px-3 py-1.5 font-mono text-[12px] text-foreground">
                python analysis/etf_watch_replay.py --lookback 120
              </pre>
              <button
                type="button"
                onClick={load}
                className="mt-3 flex items-center gap-1 text-[12px] font-medium text-accent hover:underline"
              >
                <RefreshCw className="h-3 w-3" /> 重新读取
              </button>
            </div>
          ) : (
            <Empty text="打开本视图时读取回放数据。" />
          )}
        </Section>
    )
  }

  const sessions = data.sessions
  const selected = selectedRaw && data.snapshots[selectedRaw] ? selectedRaw : data.meta.end
  const sessionIndex = Math.max(0, sessions.indexOf(selected))
  const snapshot: EtfWatchReplaySnapshot | undefined = data.snapshots[selected] ?? data.snapshots[data.meta.end]
  const viewDates = sessions.slice(-range)
  const excluded = snapshot ? snapshot.counts.industries - snapshot.counts.candidate : 0

  return (
    <>
      <Section
        id="replay-day"
        no="01"
        title="单日回放：那一天收盘能看见什么"
        first
        lead={
          <>
            选择交易日后，下面就是当日的完整行业快照。判定规则与今日扫描完全相同：
            F1 位置 / F2 形态 / F3 市场状态 / F6 容量，四条全过才是候选。
          </>
        }
        source={`${data.meta.start} → ${data.meta.end} · ${data.meta.sessions} 个交易日 · ${data.meta.rule_version}`}
        actions={
          <span className="text-[12px] text-secondary">
            生成于 {data.meta.generated_at.slice(5, 16)}
          </span>
        }
      >
        <div className="mt-6 flex flex-wrap items-end gap-x-3 gap-y-3 border-y border-border/70 py-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-[11px] font-medium tracking-[0.1em] text-muted">交易日期</span>
            <select
              value={selected}
              onChange={e => setSelected(e.target.value)}
              className="h-8 rounded border border-border bg-base px-2 font-mono text-[12px] text-foreground outline-none focus:border-accent"
            >
              {[...sessions].reverse().map(d => (
                <option key={d} value={d}>
                  {d} · {offsetLabel(d, sessions)}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            disabled={sessionIndex <= 0}
            onClick={() => setSelected(sessions[sessionIndex - 1])}
            className="flex h-8 items-center gap-1 rounded border border-border px-2.5 text-[12px] text-secondary transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ChevronLeft className="h-3 w-3" /> 上一交易日
          </button>
          <button
            type="button"
            disabled={sessionIndex >= sessions.length - 1}
            onClick={() => setSelected(sessions[sessionIndex + 1])}
            className="flex h-8 items-center gap-1 rounded border border-border px-2.5 text-[12px] text-secondary transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
          >
            下一交易日 <ChevronRight className="h-3 w-3" />
          </button>
          <span className="ml-auto pb-1.5 font-mono text-[11px] text-muted">
            回放窗口 {data.meta.lookback} 日 · 末日 {data.meta.end}
          </span>
        </div>

        {snapshot && (
          <>
            <div className="mt-7 grid grid-cols-2 gap-x-7 gap-y-6 md:grid-cols-4">
              <Stat k="候选行业" v={String(snapshot.counts.candidate)} d={`区间内 ${snapshot.counts.in_band} · 高波信号 ${snapshot.counts.signal}`} />
              <Stat k="被排除行业" v={String(excluded)} d={`共 ${snapshot.counts.industries} 个行业`} />
              <Stat k="市场状态" v={snapshot.market.state} d={`沪深300 ${snapshot.market.hs300_close}`} />
              <Stat k="数据日" v={selected} d={offsetLabel(selected, sessions)} />
            </div>
            <div className="mt-3">
              <ScanTable payload={snapshot} reasonHead="排除原因" swipeHint emptyText="该交易日没有行业进入扫描池。" />
            </div>
          </>
        )}
      </Section>

      <Section
        id="replay-track"
        no="02"
        title="信号轨迹：从哪一天开始满足"
        lead={
          <>
            每一格是当日收盘可得的判定；环标记窗口内首次候选。看三件事：
            <b className="text-foreground">首次触发离今天多远、连续多少天、中途是否退出过</b>。
            这比「今天有六个候选」更能回答策略是在提前发现，还是在识别已经跌完的资产。
          </>
        }
        source="track = 最新候选行业 × 每个交易日 · 载体按当日 F6 选取，可能换"
        actions={
          <span className="inline-flex overflow-hidden rounded border border-border">
            {([10, 20, 40] as const).map((n, i) => (
              <button
                key={n}
                type="button"
                aria-pressed={range === n}
                onClick={() => setRange(n)}
                className={cn(
                  'px-2.5 py-1 text-[12px] leading-5 transition-colors',
                  i > 0 && 'border-l border-border',
                  range === n ? 'bg-foreground text-base' : 'text-secondary hover:bg-elevated hover:text-foreground',
                )}
              >
                近 {n} 日
              </button>
            ))}
          </span>
        }
      >
        {data.tracks.length === 0 ? (
          <Empty text="末日没有候选行业，无法生成轨迹。" />
        ) : (
          <>
            <TrackSummary tracks={data.tracks} sessions={sessions} />
            <Legend />
            <TrackMatrix tracks={data.tracks} dates={viewDates} />
            <p className="mt-2 text-[11px] text-muted sm:hidden">左右滑动查看完整轨迹 →</p>
            <Callout tone="warn" label="别把「六只」当成六次机会">
              轨迹按<b className="text-foreground">行业</b>判定；最新载体只是当日 F6 选出的表达工具。军工首次触发的载体是
              航空航天ETF华夏，电新首次触发是电池ETF广发 —— 后来才换成现在这两只。
              如果只盯最新代码回看，会得到「这六只今天同时触发」的错觉。
            </Callout>
          </>
        )}
      </Section>

      <Section
        id="replay-outcome"
        no="03"
        title="买点效果：首次触发之后发生了什么"
        lead="收益按首次触发日选出的载体 ETF 复权收盘价计算；t+1 收盘进场，5/10/20 日收盘出场；MAE/MFE 用持有期日线 low/high 的盘中极值；不计成本。这是事后评价，不参与当时判定。"
        source="close_adj / hi_adj / lo_adj · t+1 收盘进场 · 无成本 · N=6 个行业，不做显著性检验"
      >
        <OutcomeTable outcomes={data.outcomes} sessions={sessions} />
        <MaeGap meta={data.meta} />
        <Callout tone="warn" label="读法">
          如果首次触发离今天很远、而「触发→最新」是负的，说明策略更可能是在<b className="text-foreground">识别已经大幅下跌的资产</b>，
          不是在下跌前发现机会。5/10/20 日为正也不能推翻这一点：那只是触发后的反弹，不是提前量。
          样本只有六个行业，这里只做诊断，不做显著性结论。
        </Callout>
      </Section>

      <Fold id="replay-caveats" title="回放口径与未消除的偏差" hint="读结论前先看这七条">
        <p className="mt-4 inline-block rounded border border-border px-3 py-1.5 font-mono text-[12px] text-muted">
          新增交易日后重跑：python analysis/etf_watch_replay.py --lookback 120
        </p>
        {data.meta.mae_convention && (
          <p className="mt-3 max-w-[46em] text-[12px] leading-[1.7] text-muted">
            <b className="font-medium text-secondary">MAE/MFE 口径：</b>{data.meta.mae_convention}
          </p>
        )}
        <div className="mt-3 max-w-[46em] space-y-2 text-[12px] leading-[1.7] text-muted">
          {Object.entries(data.meta.point_in_time).map(([k, v]) => (
            <p key={k}>
              <b className="text-foreground">{PIT_LABEL[k] ?? k}：</b>{v}
            </p>
          ))}
        </div>
      </Fold>
    </>
  )
}

// ---------------------------------------------------------------- 摘要

function TrackSummary({ tracks, sessions }: { tracks: EtfWatchTrack[]; sessions: string[] }) {
  const cols: Col<EtfWatchTrack>[] = [
    {
      head: '行业 / 最新载体',
      render: t => (
        <div>
          <div className="whitespace-nowrap font-medium">{t.industry}</div>
          <div className="max-w-[170px] truncate text-[11px] text-muted" title={t.name}>{t.name}</div>
        </div>
      ),
    },
    {
      head: '首次触发',
      render: t => t.first_trigger
        ? (
          <div>
            <div className="whitespace-nowrap font-mono text-[12px]">{t.first_trigger}</div>
            <div className="text-[11px] text-muted">{offsetLabel(t.first_trigger, sessions)}</div>
          </div>
        )
        : <span className="text-secondary">窗口内未触发</span>,
    },
    { head: '连续', render: t => <span className="font-mono tabular-nums">{t.max_consecutive} 日</span> },
    { head: '触发次数', render: t => <span className="font-mono tabular-nums">{t.trigger_count}</span> },
    {
      head: '当前',
      render: t => {
        const d = t.days[t.days.length - 1]
        return d ? <Tag tone={statusTone(d.status)}>{statusLabel[d.status]}</Tag> : <span className="text-secondary">—</span>
      },
    },
    {
      head: '备注',
      render: t => {
        const notes: string[] = []
        if (t.left_censored) notes.push('窗口左端已触发')
        if (t.carrier_changed) notes.push(`载体已换（首次 ${t.carrier_at_first}）`)
        if (t.segments.length > 1) notes.push(`中途退出 ${t.segments.length - 1} 次`)
        return <span className="text-[12px] leading-[1.6] text-muted">{notes.join('；') || '—'}</span>
      },
    },
  ]
  return <DataTable cols={cols} rows={tracks} rowKey={t => t.industry} dense minWidth={760} swipeHint />
}

// ---------------------------------------------------------------- 矩阵

type CellKind = 'candidate' | 'low' | 'market' | 'liquidity' | 'position' | 'deep' | 'stale' | 'multi'

/**
 * 轨迹九宫格的色板。红绿在这一页只留给涨跌，所以「排除」用琥珀、「阻断」用墨色：
 * 深跌阻断不是「跌」，是「这道闸门直接不放行」，用最重的墨色比用红色更准。
 */
const CELL: Record<CellKind, { code: string; label: string; cls: string }> = {
  candidate: { code: '候', label: '候选（四条全过）', cls: 'border-accent/40 bg-accent/15 text-accent' },
  low: { code: '低', label: '低波排除', cls: 'border-warning/30 bg-warning/10 text-warning' },
  market: { code: '市', label: '市场状态排除', cls: 'border-warning/30 bg-warning/10 text-warning' },
  liquidity: { code: '流', label: '流动性排除', cls: 'border-warning/30 bg-warning/10 text-warning' },
  position: { code: '位', label: '未到位置', cls: 'border-border bg-elevated text-muted' },
  deep: { code: '深', label: '深跌阻断', cls: 'border-foreground/25 bg-foreground/10 text-foreground' },
  stale: { code: '停', label: '数据停更', cls: 'border-warning/40 bg-warning/15 text-warning' },
  multi: { code: '多', label: '多重排除', cls: 'border-border bg-elevated text-warning' },
}

function cellKind(d: EtfWatchTrackDay): CellKind {
  if (d.status === 'CANDIDATE') return 'candidate'
  if (d.status === 'BLOCKED_F1') return 'deep'
  if (d.reasons.some(r => r.includes('停更'))) return 'stale'
  if (!d.r1) return 'position'
  const missed = [!d.r2, !d.r3, !d.f6].filter(Boolean).length
  if (missed > 1) return 'multi'
  if (!d.r2) return 'low'
  if (!d.r3) return 'market'
  if (!d.f6) return 'liquidity'
  return 'multi'
}

function cellTitle(d: EtfWatchTrackDay, carrier: string | null): string {
  const p = (v: number | null) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
  return [
    `${d.date} · ${statusLabel[d.status]}`,
    `dd ${p(d.dd)} · 波动分位 ${d.vol_pct?.toFixed(2) ?? '—'} · 20 日均额 ${(d.amt20 / 1e4).toFixed(0)} 万`,
    carrier ? `载体 ${carrier}` : '',
    d.reasons.length ? d.reasons.join('；') : '四条全过',
  ].filter(Boolean).join('\n')
}

function TrackMatrix({ tracks, dates }: { tracks: EtfWatchTrack[]; dates: string[] }) {
  return (
    <div className="mt-2 overflow-x-auto overscroll-x-contain">
      <table className="table-fixed border-collapse text-[12px]" style={{ minWidth: 168 + dates.length * 34 }}>
        <colgroup>
          <col style={{ width: 168 }} />
          {dates.map(d => <col key={d} style={{ width: 34 }} />)}
        </colgroup>        <thead>
          <tr>
            <th className="sticky left-0 z-10 border-b border-border bg-base px-2 py-1.5 text-left text-[11px] font-medium text-secondary">
              行业 / 载体
            </th>
            {dates.map((d, i) => (
              <th
                key={d}
                className={cn(
                  'border-b border-border px-0.5 py-1.5 text-center font-mono text-[11px] font-normal',
                  i === dates.length - 1 ? 'text-foreground' : 'text-secondary',
                )}
                title={fullDate(d)}
              >
                {i === dates.length - 1 ? 'T' : shortDate(d)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {tracks.map(t => {
            const byDate = new Map(t.days.map(d => [d.date, d]))
            return (
              <tr key={t.industry}>
                <th className="sticky left-0 z-10 border-b border-border/70 bg-base px-2 py-1 text-left align-middle">
                  <div className="whitespace-nowrap text-[12px] font-medium text-foreground">{t.industry}</div>
                  <div className="max-w-[132px] truncate text-[11px] font-normal text-secondary" title={t.name}>{t.name}</div>
                </th>
                {dates.map(d => {
                  const day = byDate.get(d)
                  if (!day) return <td key={d} className="border-b border-border/70 px-0.5 py-0.5" />
                  const kind = cellKind(day)
                  const meta = CELL[kind]
                  return (
                    <td key={d} className="border-b border-border/70 px-0.5 py-0.5 text-center">
                      <span
                        title={cellTitle(day, day.best_name)}
                        aria-label={`${t.industry} ${cellTitle(day, day.best_name)}`}
                        className={cn(
                          'inline-flex h-[23px] w-[23px] items-center justify-center rounded border text-[11px] font-medium',
                          meta.cls,
                          t.first_trigger === d && 'ring-1 ring-foreground/70 ring-offset-1 ring-offset-base',
                        )}
                      >
                        {meta.code}
                      </span>
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function Legend() {
  const kinds: CellKind[] = ['candidate', 'low', 'market', 'liquidity', 'position', 'deep', 'stale', 'multi']
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-secondary">
      {kinds.map(k => (
        <span key={k} className="inline-flex items-center gap-1">
          <span className={cn('inline-flex h-4 w-4 items-center justify-center rounded border text-[11px] font-medium', CELL[k].cls)}>
            {CELL[k].code}
          </span>
          {CELL[k].label}
        </span>
      ))}
      <span className="text-secondary/80">环 = 窗口内首次触发；悬停看完整原因</span>
    </div>
  )
}

// ---------------------------------------------------------------- 效果

function OutcomeTable({ outcomes, sessions }: { outcomes: EtfWatchOutcome[]; sessions: string[] }) {
  const cols: Col<EtfWatchOutcome>[] = [
    {
      head: '行业 / 最新载体',
      w: '15%',
      render: o => (
        <div>
          <div className="whitespace-nowrap font-medium">{o.industry}</div>
          <div className="truncate text-[12px] text-secondary" title={o.name}>{o.name}</div>
        </div>
      ),
    },
    {
      head: '首次触发',
      w: '12%',
      render: o => o.first_trigger
        ? (
          <div>
            <div className="whitespace-nowrap font-mono text-[12px]">{o.first_trigger}</div>
            <div className="truncate text-[12px] text-secondary" title={o.carrier ?? ''}>{offsetLabel(o.first_trigger, sessions)} · {o.carrier ?? '—'}</div>
          </div>
        )
        : <span className="text-secondary">窗口内未触发</span>,
    },
    {
      head: '触发→最新',
      headCls: 'whitespace-nowrap',
      w: '9%',
      align: 'right',
      render: o => <span className={cn('font-mono tabular-nums', retCls(o.to_end))}>{fmtPct(o.to_end)}</span>,
    },
    { head: '5 日', w: '8%', align: 'right', render: o => <span className={cn('font-mono tabular-nums', retCls(o.ret5))}>{fmtPct(o.ret5)}</span> },
    { head: '10 日', w: '8%', align: 'right', render: o => <span className={cn('font-mono tabular-nums', retCls(o.ret10))}>{fmtPct(o.ret10)}</span> },
    { head: '20 日', w: '8%', align: 'right', render: o => <span className={cn('font-mono tabular-nums', retCls(o.ret20))}>{fmtPct(o.ret20)}</span> },
    {
      head: 'MAE 盘中',
      headCls: 'whitespace-nowrap',
      w: '11%',
      align: 'right',
      render: o => {
        const gap = o.mae != null && o.mae_close != null ? Math.abs(o.mae - o.mae_close) : 0
        return (
          <div>
            <span className={cn('font-mono tabular-nums', retCls(o.mae))}>{fmtPct(o.mae)}</span>
            {gap >= 0.002 && (
              <div className="whitespace-nowrap font-mono text-[12px] text-secondary" title="收盘口径：只看每日收盘，会漏掉盘中跌破">
                收盘 {fmtPct(o.mae_close)}
              </div>
            )}
          </div>
        )
      },
    },
    {
      head: 'MFE 盘中',
      headCls: 'whitespace-nowrap',
      w: '9%',
      align: 'right',
      render: o => <span className={cn('font-mono tabular-nums', retCls(o.mfe))}>{fmtPct(o.mfe)}</span>,
    },
    { head: '备注', w: '20%', render: o => <span className="line-clamp-2 text-[11px] leading-relaxed text-secondary" title={o.note}>{o.note}</span> },
  ]
  return (
    <DataTable
      cols={cols}
      rows={outcomes}
      rowKey={o => o.industry}
      dense
      minWidth={860}
      swipeHint
      reason="载体按首次触发日 F6 选定；t+1 收盘进场；5/10/20 日用收盘价，MAE/MFE 用持有期盘中 low/high（不含进场日盘内，那时还没买）；不计成本。红涨绿跌。"
    />
  )
}

function MaeGap({ meta }: { meta: EtfWatchReplayMeta }) {
  const gap = meta.mae_gap
  if (!gap?.n || gap.median_delta_bp == null) return null
  const w = gap.worst
  return (
    <p className="mt-2 text-[12px] leading-relaxed text-secondary">
      <b className="text-foreground">口径差</b>：盘中 MAE 中位比收盘口径多 {Math.abs(Math.round(gap.median_delta_bp))}bp
      {w && <>，最差 {w.industry} 多 {Math.abs(Math.round(w.delta_bp))}bp（收盘 {fmtPct(w.mae_close)} → 盘中 {fmtPct(w.mae)}）</>}
      。收盘序列看不到「盘中跌破、收盘拉回」，所以收盘口径只留作审计，不用它判断仓位；V1 预注册判据里的 MAE 仍是收盘口径，不改判据。
    </p>
  )
}
