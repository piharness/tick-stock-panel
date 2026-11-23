import { useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight, Loader2, Users, Lock } from 'lucide-react'
import type { ShareholderProfile } from '@/lib/api'

/**
 * 股东结构 —— 「谁在持有、筹码在集中还是扩散」。
 *
 * 这个面板里只有一条轴被回测证明有预测力，其余都是描述。界面上必须区分开，
 * 否则用户会把有说服力的数字当成有效信号（比不做更贵）：
 *
 *   ✅ 有效：**股东户数的变化率**（同比）。未来 20 日超额在五分位上单调递减，
 *      G5-G1 = -87bp（t=-4.5），11/11 年同向，剥掉过去 60 日收益后仍有 -92bp
 *      （t=-4.1）⇒ 不是反转效应的换皮。
 *   ❌ 无效：**户数的水平**。户均持股市值（"散户多不多"最直接的度量）
 *      G5-G1 = -5.5bp（t=-0.13）；绝对户数 G5-G1 = -31bp（t=-0.95）。
 *      两者与 log(市值) 的秩相关高达 +0.43 / +0.53 —— 它们只是市值的影子。
 *
 * 所以本面板**不给"散户度评分"**。给了就等于把已证伪的东西包装成信号。
 */

const QUAD_ORDER = ['Q1', 'Q2', 'Q3', 'Q4'] as const

/** 象限格子的底色：正超额偏暖、负超额偏冷，与页面红涨绿跌一致 */
function quadTone(bp: number | undefined) {
  if (bp == null) return { bg: 'bg-elevated/30', text: 'text-muted', border: 'border-border/30' }
  if (bp >= 40) return { bg: 'bg-bull/[0.10]', text: 'text-bull', border: 'border-bull/30' }
  if (bp > 0) return { bg: 'bg-bull/[0.05]', text: 'text-bull/80', border: 'border-bull/20' }
  if (bp <= -60) return { bg: 'bg-bear/[0.10]', text: 'text-bear', border: 'border-bear/30' }
  return { bg: 'bg-bear/[0.05]', text: 'text-bear/80', border: 'border-bear/20' }
}

function pct(v: number | null | undefined, digits = 0) {
  if (v == null) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function signed(v: number | null | undefined, digits = 1) {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

/** 户均持股市值：后端给**万元**。565 万比 0.06 亿好读，所以钱够大才换单位。 */
function fmtWan(wan: number | null | undefined) {
  if (wan == null) return '—'
  if (wan >= 1e4) return `${(wan / 1e4).toFixed(2)} 亿`
  return `${wan.toFixed(0)} 万`
}

export function ShareholderPanel({
  data, loading, error,
}: {
  data: ShareholderProfile | undefined
  loading: boolean
  error: boolean
}) {
  const [openTop, setOpenTop] = useState(false)

  if (loading && !data) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted flex items-center gap-2">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        正在读股东结构…
      </div>
    )
  }
  if (error && !data) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted">
        股东结构不可用（接口失败）—— 本面板不显示，不等于筹码没有在扩散。
      </div>
    )
  }
  if (!data) return null
  if (!data.available) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted">
        {data.reason || '没有这只票的股东数据'}
      </div>
    )
  }

  const { holder, pressure, top_float, lift } = data
  const latest = holder.latest
  const meta = pressure.stats_meta

  return (
    <div className="rounded-lg border border-border/40 bg-base/30 overflow-hidden" data-testid="shareholder-panel">
      {/* 头 */}
      <div className="px-4 py-2.5 border-b border-border/30 flex items-center gap-2">
        <Users className="h-4 w-4 text-sky-300/80 shrink-0" />
        <span className="text-[13px] font-medium text-foreground">股东结构</span>
        {latest && (
          <span className="rounded bg-elevated/60 px-1.5 py-0.5 text-[10px] text-secondary font-mono">
            {latest.period_end}
          </span>
        )}
        <span className="ml-auto text-[10px] text-muted">
          换手率取近 {pressure.turnover_window} 日 · {pressure.as_of}
        </span>
      </div>

      <div className="p-4 space-y-4">
        {/* ===== 抛压象限 ===== */}
        <div>
          <div className="flex items-center gap-1.5 mb-1.5">
            <span className="text-[11px] font-medium text-secondary">抛压象限</span>
            <span className="text-[10px] text-muted">
              换手率 × 户数同比，各自中位数二分
            </span>
          </div>
          <div className="grid grid-cols-2 gap-1.5">
            {QUAD_ORDER.map(k => {
              const s = pressure.quadrant.all?.[k] ?? null
              const isCur = k === pressure.quadrant.key
              const tone = quadTone(s?.bp)
              return (
                <div
                  key={k}
                  className={`rounded-md border px-2.5 py-2 ${isCur ? `${tone.bg} ${tone.border}` : 'bg-elevated/20 border-border/25'}`}
                >
                  <div className="flex items-center gap-1">
                    <span className={`text-[10px] font-mono ${isCur ? tone.text : 'text-muted/70'}`}>{k}</span>
                    <span className={`text-[11px] ${isCur ? 'text-foreground font-medium' : 'text-muted'}`}>
                      {QUAD_LABEL[k]}
                    </span>
                    {isCur && <span className="ml-auto text-[9px] text-foreground/60">当前</span>}
                  </div>
                  <div className="flex items-baseline gap-1.5 mt-1">
                    {/* 非当前格也保留自己的色调（只是变淡）：四格的相对好坏
                        是这一块的主要信息，全部涂成灰色等于把它藏起来。 */}
                    <span className={`font-mono text-[12px] ${isCur ? tone.text : `${tone.text} opacity-60`}`}>
                      {s ? `${s.bp >= 0 ? '+' : ''}${s.bp.toFixed(0)}bp` : '—'}
                    </span>
                    {s && (
                      <span className="text-[9px] font-mono text-muted/70">
                        t={s.t.toFixed(1)} · 胜率 {pct(s.win)}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
          <p className="mt-1.5 text-[10px] leading-relaxed text-muted/80">
            格内为未来 20 日相对全市场等权的超额均值
            {meta?.sample ? `（${meta.sample}，${meta.n_dates} 期，约 ${meta.avg_stocks_per_date} 只/期）` : ''}。
            「低换手 · 筹码集中」是历史最强的格子，「高换手 · 筹码扩散」最弱 ——
            但这是横截面统计，不是单只票的收益承诺。
          </p>
        </div>

        {/* ===== 户数趋势 ===== */}
        <div>
          <div className="flex items-center gap-1.5 mb-1">
            <span className="text-[11px] font-medium text-secondary">股东户数</span>
            <span className="text-[10px] text-muted">近 {holder.trend.length} 期</span>
            {latest?.pct_yoy != null && (
              <span className="ml-auto text-[10px] text-muted">
                同期分位 <span className="font-mono text-foreground">{pct(latest.pct_yoy)}</span>
                <span className="text-muted/60">（越高＝户数增得越多）</span>
              </span>
            )}
          </div>
          <HolderSpark trend={holder.trend} />
          {latest && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-1.5 text-[10px]">
              <span className="text-muted">
                户数 <span className="font-mono text-foreground">{latest.holder_num?.toLocaleString() ?? '—'}</span>
              </span>
              <span className="text-muted">
                环比 <span className={`font-mono ${(latest.chg_q ?? 0) > 0 ? 'text-bear' : 'text-bull'}`}>
                  {signed(latest.chg_q)}
                </span>
              </span>
              <span className="text-muted">
                同比 <span className={`font-mono ${(latest.chg_yoy ?? 0) > 0 ? 'text-bear' : 'text-bull'}`}>
                  {signed(latest.chg_yoy)}
                </span>
              </span>
              {/* 户均市值降级显示：它是最常被当成"散户度"的数字，但实测无效
                  （G5-G1 = -5.5bp，t=-0.13）。留着是因为它描述性有用，
                  但视觉上不与有效信号并列。 */}
              <span className="text-muted/60" title="户均持股市值。描述性指标：实测对未来收益无预测力（t=-0.13），不要当作'散户多/少'的信号">
                户均 <span className="font-mono text-secondary">{fmtWan(latest.avg_mcap)}</span>
              </span>
            </div>
          )}
          <p className="mt-1 text-[10px] leading-relaxed text-muted/70">
            只有<span className="text-foreground/70">变化率</span>经回测有效（同比五分位 G5−G1 = −87bp / 20 日，t=−4.5，11/11 年同向）；
            户均市值与绝对户数实测无效（t=−0.13 / −0.95），故不提供「散户度」评分。
            分位分母为{holder.percentile_basis}。
          </p>
        </div>

        {/* ===== 十大流通股东 ===== */}
        {top_float.rows.length > 0 && (
          <div>
            <button
              onClick={() => setOpenTop(v => !v)}
              className="flex items-center gap-1.5 w-full text-left group"
            >
              {openTop
                ? <ChevronDown className="h-3.5 w-3.5 text-muted" />
                : <ChevronRight className="h-3.5 w-3.5 text-muted" />}
              <span className="text-[11px] font-medium text-secondary group-hover:text-foreground transition-colors">
                十大流通股东
              </span>
              <span className="text-[10px] text-muted">{top_float.period_end}</span>
              {top_float.org_ratio != null && (
                <span className="ml-auto text-[10px] text-muted">
                  前十合计 <span className="font-mono text-foreground">{pct(top_float.org_ratio, 1)}</span>
                </span>
              )}
            </button>
            {openTop && (
              <table className="w-full mt-1.5 text-[10px]">
                <thead>
                  <tr className="text-muted/70">
                    <th className="text-left font-normal w-4">#</th>
                    <th className="text-left font-normal">股东</th>
                    <th className="text-right font-normal w-12">占流通</th>
                    <th className="text-right font-normal w-20">变化</th>
                  </tr>
                </thead>
                <tbody>
                  {top_float.rows.map(r => (
                    <tr key={`${r.rank}-${r.name}`} className="border-t border-border/20">
                      <td className="text-muted/60 font-mono">{r.rank}</td>
                      <td className="truncate max-w-[220px]" title={r.name}>
                        <span className={r.is_org ? 'text-foreground' : 'text-secondary'}>{r.name}</span>
                      </td>
                      <td className="text-right font-mono text-foreground">{pct(r.ratio, 2)}</td>
                      <td className="text-right font-mono">{fmtChange(r.change)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}

        {/* ===== 解禁 ===== */}
        {lift.length > 0 && (
          <div>
            <div className="flex items-center gap-1.5 mb-1">
              <Lock className="h-3.5 w-3.5 text-muted" />
              <span className="text-[11px] font-medium text-secondary">未来 12 个月解禁</span>
              <span className="text-[10px] text-muted">确定的未来供给</span>
            </div>
            <div className="space-y-0.5">
              {lift.map(e => (
                <div key={`${e.free_date}-${e.shares_type}`} className="flex items-center gap-2 text-[10px]">
                  <span className="font-mono text-foreground w-[72px] shrink-0">{e.free_date}</span>
                  <span className="text-muted w-11 shrink-0">{e.days_to} 天</span>
                  {/* 解禁量可以超过现有流通盘（金龙鱼 2027-10：8.99 倍）。
                      超过 1 时写成「倍」，比 "899%" 好读，也不会被误读成一个比例。
                      用「倍」而不是「×」：9px 字号下 × 会被看成减号（实测）。 */}
                  <span className="font-mono text-secondary shrink-0">
                    {e.pct_of_float == null
                      ? '—'
                      : e.pct_of_float > 1
                        ? `${e.pct_of_float.toFixed(1)} 倍流通盘`
                        : `占流通 ${pct(e.pct_of_float, 2)}`}
                  </span>
                  <span className="font-mono text-muted shrink-0">
                    {e.mcap_yi == null ? '—' : `${e.mcap_yi.toFixed(1)} 亿`}
                  </span>
                  <span className="truncate text-muted/70" title={e.shares_type ?? ''}>{e.shares_type}</span>
                </div>
              ))}
            </div>
            <p className="mt-1 text-[10px] text-muted/70">
              解禁日期与数量是公告事实，不是预测。占比按「占流通股」口径 ——
              超过 100% 时说明解禁量大于现有流通盘，流通盘会被成倍放大。
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

const QUAD_LABEL: Record<string, string> = {
  Q1: '低换手 · 筹码集中',
  Q2: '高换手 · 筹码集中',
  Q3: '低换手 · 筹码扩散',
  Q4: '高换手 · 筹码扩散',
}

function fmtChange(v: string | null): ReactNode {
  if (v == null) return '—'
  if (v === '新进') return <span className="text-bull">新进</span>
  if (v === '不变' || v === '0') return <span className="text-muted/50">—</span>
  const n = Number(v)
  if (!Number.isFinite(n)) return <span className="text-muted">{v}</span>
  const wan = n / 1e4
  return (
    <span className={n > 0 ? 'text-bull' : 'text-bear'}>
      {n > 0 ? '+' : ''}{Math.abs(wan) >= 10000 ? `${(wan / 1e4).toFixed(2)} 亿` : `${wan.toFixed(0)} 万`}
    </span>
  )
}

/** 户数迷你折线。y 轴反转是刻意的：户数下降在图上要看起来「向下」——
 *  人的直觉把"向下"读成"变少"，而这正是筹码集中的方向。 */
function HolderSpark({ trend }: { trend: { holder_num: number | null; period_end: string; chg_yoy: number | null }[] }) {
  const pts = trend.filter(t => t.holder_num != null) as { holder_num: number; period_end: string; chg_yoy: number | null }[]
  if (pts.length < 2) return null
  const vals = pts.map(p => p.holder_num)
  const lo = Math.min(...vals)
  const hi = Math.max(...vals)
  const range = hi - lo || 1
  const W = 100
  const H = 34
  const step = W / (pts.length - 1)
  const ys = vals.map(v => H - 3 - ((v - lo) / range) * (H - 6))
  const d = ys.map((y, i) => `${i === 0 ? 'M' : 'L'}${(i * step).toFixed(2)},${y.toFixed(2)}`).join(' ')
  const area = `${d} L${W},${H} L0,${H} Z`
  const lastY = ys[ys.length - 1]

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="w-full h-[34px] overflow-visible">
        <path d={area} fill="rgba(56,189,248,0.08)" />
        <path d={d} fill="none" stroke="rgba(56,189,248,0.85)" strokeWidth="0.8"
              vectorEffect="non-scaling-stroke" />
        <circle cx={W} cy={lastY} r="1.4" fill="rgb(56,189,248)" />
      </svg>
      <div className="flex justify-between text-[8px] font-mono text-muted/60 mt-0.5">
        <span>{pts[0].period_end}</span>
        {/* 绝对幅度必须写出来：折线 y 轴自适应，户数只变了 3% 也会画成陡坡。
            不给刻度就等于让形状替数据说话。 */}
        <span className="text-muted/50">{(lo / 1e4).toFixed(1)}万–{(hi / 1e4).toFixed(1)}万</span>
        <span>{pts[pts.length - 1].period_end}</span>
      </div>
    </div>
  )
}
