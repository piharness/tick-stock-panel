/**
 * 个股解剖 —— 把一段区间摊成三层：价格、结构、席位。
 *
 * 刻意不自动生成「吸筹 / 拉升 / 派发」分段。分段是判断，不是观测；
 * 机器只报三件可复现的事：
 *   ① 判据在哪几天成立（用什么阈值写在判据库里）
 *   ② 龙虎榜的钱从哪个席位来、净额多少
 *   ③ 成交额相对前 20 日放大 ≥2 倍的日期（候选拐点，只提示从哪天开始读）
 *
 * 阶段与手法由人写进案例——那正是学习发生的地方，不能由机器代笔。
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { BookmarkPlus, Loader2, Search } from 'lucide-react'
import { api, type CaseDoc } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { EChartsCandlestick, type ChartMarker, type OHLC } from '@/components/EChartsCandlestick'
import { FAMILY_META, cnToday, normalizeSymbol, pctSigned, shiftDate, toneCls } from './calibers'

const EVIDENCE_COLOR: Record<string, string> = {
  distribution: '#d97706',
  lockup: '#7c3aed',
  wash: '#0891b2',
  pump_dump: '#dc2626',
}

export interface DissectSeed {
  symbol: string
  start: string
  end: string
}

export function DissectPanel({
  seed, onSaveCase, cases,
}: {
  seed: DissectSeed | null
  onSaveCase: (draft: Partial<CaseDoc>) => void
  cases: { id: string; symbol: string; title: string }[]
}) {
  const [symbol, setSymbol] = useState(seed?.symbol ?? '002086.SZ')
  const [start, setStart] = useState(seed?.start ?? shiftDate(cnToday(), -120))
  const [end, setEnd] = useState(seed?.end ?? cnToday())
  const [submitted, setSubmitted] = useState<DissectSeed>({
    symbol: seed?.symbol ?? '002086.SZ',
    start: seed?.start ?? shiftDate(cnToday(), -120),
    end: seed?.end ?? cnToday(),
  })

  useEffect(() => {
    if (!seed) return
    setSymbol(seed.symbol)
    setStart(seed.start)
    setEnd(seed.end)
    setSubmitted(seed)
  }, [seed])

  const q = useQuery({
    queryKey: QK.caseLabDissect(submitted.symbol, submitted.start, submitted.end),
    queryFn: () => api.caseLabDissect(submitted.symbol, submitted.start, submitted.end),
    enabled: !!submitted.symbol && !!submitted.start && !!submitted.end,
    refetchOnWindowFocus: false,
    staleTime: 30 * 60_000,
  })

  const d = q.data

  const ohlc: OHLC[] = useMemo(
    () => (d?.bars ?? []).map(b => ({
      date: b.date, open: b.open, high: b.high, low: b.low, close: b.close,
    })),
    [d],
  )

  // 命中标记：同一日期多条命中只画一个，label 用「结构简称」拼接，
  // 避免 250 根 K 线上出现几十个互相遮挡的角标。
  const markers: ChartMarker[] = useMemo(() => {
    if (!d) return []
    const byDate = new Map<string, string[]>()
    const fam = new Map<string, string>()
    for (const h of d.hits) {
      for (const dt of h.dates) {
        const arr = byDate.get(dt) ?? []
        arr.push(h.label)
        byDate.set(dt, arr)
        if (!fam.has(dt)) fam.set(dt, h.family)
      }
    }
    return [...byDate.entries()].map(([date, labels]) => ({
      date,
      kind: 'neutral' as const,
      label: labels.join(' / '),
      color: EVIDENCE_COLOR[fam.get(date) ?? ''] ?? '#64748b',
      above: true,
    })).sort((a, b) => a.date.localeCompare(b.date))
  }, [d])

  const submit = () => {
    setSubmitted({ symbol: normalizeSymbol(symbol), start, end })
  }

  const saveDraft = () => {
    if (!d || d.state !== 'ok') return
    onSaveCase({
      symbol: d.symbol,
      name: d.name ?? undefined,
      start: d.start,
      end: d.end,
      title: `${d.name ?? d.symbol} ${d.start} → ${d.end}`,
      playbook: d.hits.map(h => h.id),
      thesis: '',
    })
  }

  const existing = cases.filter(c => c.symbol === submitted.symbol)

  return (
    <div className="px-5 py-3">
      <div className="mb-3 flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-0.5 text-[10px] text-muted">
          标的
          <input
            value={symbol}
            onChange={e => setSymbol(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') submit() }}
            placeholder="002086.SZ 或 002086"
            className="w-40 rounded-btn border border-border bg-surface px-2 py-1 font-mono text-[11px] text-foreground outline-none focus:border-accent/50"
          />
        </label>
        <label className="flex flex-col gap-0.5 text-[10px] text-muted">
          起
          <input
            type="date"
            value={start}
            onChange={e => setStart(e.target.value)}
            className="rounded-btn border border-border bg-surface px-2 py-1 font-mono text-[11px] text-foreground outline-none focus:border-accent/50"
          />
        </label>
        <label className="flex flex-col gap-0.5 text-[10px] text-muted">
          止
          <input
            type="date"
            value={end}
            onChange={e => setEnd(e.target.value)}
            className="rounded-btn border border-border bg-surface px-2 py-1 font-mono text-[11px] text-foreground outline-none focus:border-accent/50"
          />
        </label>
        <button
          onClick={submit}
          className="flex items-center gap-1 rounded-btn border border-accent/40 bg-accent/10 px-2.5 py-1.5 text-[11px] text-accent hover:bg-accent/15"
        >
          <Search className="h-3.5 w-3.5" /> 解剖
        </button>
        <div className="flex items-center gap-2 text-[10px] text-muted">
          {[60, 120, 250].map(n => (
            <button
              key={n}
              onClick={() => {
                const e = end
                setStart(shiftDate(e, -Math.round(n * 1.5)))
                setSubmitted({ symbol: normalizeSymbol(symbol), start: shiftDate(e, -Math.round(n * 1.5)), end: e })
              }}
              className="rounded border border-border bg-surface px-1.5 py-0.5 hover:bg-elevated"
            >
              {n}日
            </button>
          ))}
        </div>
        <button
          onClick={saveDraft}
          disabled={!d || d.state !== 'ok'}
          className="ml-auto flex items-center gap-1 rounded-btn border border-border bg-surface px-2.5 py-1.5 text-[11px] text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-40"
        >
          <BookmarkPlus className="h-3.5 w-3.5" /> 存为案例
        </button>
      </div>

      {existing.length > 0 && (
        <div className="mb-2 flex flex-wrap items-center gap-1 text-[10px] text-muted">
          已有 {existing.length} 个案例：
          {existing.slice(0, 4).map(c => (
            <span key={c.id} className="rounded border border-border bg-elevated px-1.5 py-0.5 text-secondary">
              {c.title}
            </span>
          ))}
        </div>
      )}

      {q.isLoading && (
        <div className="flex items-center gap-2 py-12 text-xs text-muted">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在读该票的区间日线与席位…
        </div>
      )}
      {q.isError && (
        <div className="py-12 text-xs text-danger">读取失败：{(q.error as Error).message}</div>
      )}
      {d && d.state !== 'ok' && (
        <div className="py-12 text-xs text-muted">
          {d.state === 'not_in_universe'
            ? `${d.symbol} 不在本地标的表里。6 位代码会自动补后缀；若仍失败，说明本地没有这只票。`
            : '该区间没有可用的日线数据（停牌、未上市，或本地分区未覆盖）。'}
        </div>
      )}

      {d?.state === 'ok' && d.summary && (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-x-6 gap-y-1 rounded-card border border-border bg-surface px-3 py-2 text-[11px]">
            <span className="text-sm font-medium text-foreground">{d.name ?? d.symbol}</span>
            <span className="font-mono text-muted">{d.symbol}</span>
            <Stat label="区间涨幅" value={pctSigned(d.summary.range_ret_pct)} cls={toneCls(d.summary.range_ret_pct)} />
            <Stat label="自高点回撤" value={pctSigned(d.summary.max_drawdown_pct)} cls={toneCls(d.summary.max_drawdown_pct)} />
            <Stat label="换手中位" value={`${d.summary.turn_median_pct.toFixed(2)}%`} />
            <Stat label="累计成交" value={`${d.summary.amount_yi_total.toFixed(1)}亿`} />
            <Stat label="交易日" value={`${d.summary.bars}`} />
            <Stat label="龙虎榜" value={`${d.events.length}次`} />
          </div>

          <div className="mb-3 rounded-card border border-border bg-surface p-2">
            <EChartsCandlestick
              data={ohlc}
              markers={markers}
              height={340}
              visibleBars="all"
              showMA
              showInfoBar
            />
            <div className="mt-1 flex flex-wrap items-center gap-3 px-1 text-[10px] text-muted">
              <span>角标 = 判据命中</span>
              {Object.entries(FAMILY_META).map(([k, v]) => (
                <span key={k} className="flex items-center gap-1">
                  <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: v.color }} />
                  {v.label}
                </span>
              ))}
              <span className="ml-auto">红涨绿跌；判据阈值见「判据检验」页</span>
            </div>
          </div>

          <div className="grid gap-3 lg:grid-cols-3">
            <section className="rounded-card border border-border bg-surface p-3">
              <h3 className="mb-2 text-[11px] font-medium text-foreground">结构命中</h3>
              {d.hits.length === 0 && <div className="text-[10px] text-muted">区间内没有判据成立。</div>}
              <div className="space-y-1.5">
                {d.hits.map(h => (
                  <div key={h.id} className="text-[10px]">
                    <div className="flex items-center justify-between">
                      <span className="flex items-center gap-1 text-secondary">
                        <span
                          className="inline-block h-1.5 w-1.5 rounded-full"
                          style={{ background: FAMILY_META[h.family]?.color ?? '#64748b' }}
                        />
                        {h.label}
                      </span>
                      <span className="font-mono text-muted">{h.n}次</span>
                    </div>
                    <div className="mt-0.5 font-mono text-[9px] leading-relaxed text-muted">
                      {h.dates.slice(0, 8).join(' ')}{h.dates.length > 8 ? ' …' : ''}
                    </div>
                  </div>
                ))}
              </div>
            </section>

            <section className="rounded-card border border-border bg-surface p-3">
              <h3 className="mb-2 text-[11px] font-medium text-foreground">龙虎榜（1 日榜）</h3>
              {d.events.length === 0 && (
                <div className="text-[10px] text-muted">
                  区间内未上榜。本地龙虎榜自 2025-11 起，更早的区间看不到是数据边界。
                </div>
              )}
              <div className="max-h-56 space-y-1 overflow-y-auto">
                {d.events.map(e => (
                  <div key={e.date} className="text-[10px]">
                    <div className="flex items-center justify-between gap-1 font-mono tabular-nums">
                      <span className="shrink-0 text-secondary">{e.date}</span>
                      <span className={cn('shrink-0', toneCls(e.net_yi))}>
                        {e.net_yi > 0 ? '+' : ''}{e.net_yi.toFixed(2)}亿
                      </span>
                      <span className={cn('shrink-0 text-[9px]', toneCls(e.chg_pct))}>
                        {pctSigned(e.chg_pct)}
                      </span>
                    </div>
                    {e.seats.length > 0 && (
                      <div className="pl-1 text-[9px] text-muted">
                        {e.seats.map((s, i) => (
                          <span key={i} className="mr-2">
                            {s.seat ?? '未知席位'} {s.net_yi > 0 ? '+' : ''}{s.net_yi.toFixed(2)}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </section>

            <section className="rounded-card border border-border bg-surface p-3">
              <h3 className="mb-2 text-[11px] font-medium text-foreground">候选拐点</h3>
              <p className="mb-1.5 text-[9px] leading-relaxed text-muted">
                成交额 5 日均值 / 前 20 日均值 ≥ 2 的日期。只提示从哪天开始读，不是分段结论。
              </p>
              {d.pivots.length === 0 && <div className="text-[10px] text-muted">区间内没有量能突变。</div>}
              <div className="max-h-56 space-y-0.5 overflow-y-auto font-mono text-[10px]">
                {d.pivots.map(p => (
                  <div key={p.date} className="flex items-center justify-between">
                    <span className="text-secondary">{p.date}</span>
                    <span className="text-muted">×{p.amount_ratio.toFixed(1)}</span>
                    <span className={toneCls(p.ret_pct)}>{pctSigned(p.ret_pct)}</span>
                  </div>
                ))}
              </div>
            </section>
          </div>

          <p className="mt-3 text-[10px] leading-relaxed text-muted">
            判据命中是结构描述，不是操纵证据。要判断「这是不是一次有组织的介入」，
            需要把处罚文书、爆料、媒体报道与这里的痕迹放在同一条时间轴上对账——
            在「案例库」里做。
          </p>
        </>
      )}
    </div>
  )
}

function Stat({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <span className="flex items-baseline gap-1">
      <span className="text-[10px] text-muted">{label}</span>
      <span className={cn('font-mono text-[11px] text-secondary', cls)}>{value}</span>
    </span>
  )
}
