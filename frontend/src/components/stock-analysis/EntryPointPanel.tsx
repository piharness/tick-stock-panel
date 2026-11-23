import { useState } from 'react'
import {
  ShieldCheck, ShieldAlert, ShieldQuestion, Loader2, ChevronDown, ChevronRight, History, Check, X, Minus,
} from 'lucide-react'
import type { EntryPoint } from '@/lib/api'

/**
 * 买点体检 —— 这一页唯一回答「现在该不该下注」的模块。
 *
 * ## 它为什么不是「买入信号」
 *
 * 判据在 2016-11~2026-09 的 46.5 万票日上标定过（`data/entry_check/stats.json`）：
 * 低风险档胜率 51.0%、中位 +0.3% —— 只比抛硬币好 3.7pp，谈不上预测上涨。
 * 它真正能做到的是把**暴跌概率**（未来 20 日跌超 10%）从 17.6% 压到 9.7%，
 * 而高危档推到 29.6%。所以界面上：
 *
 *   - 档位用「低风险 / 中性 / 高危」，不用「买入 / 观望 / 卖出」；
 *   - 数字全部并列基准（没有 base rate 的胜率毫无意义）；
 *   - 胜率的分年稳定性必须露出来（10 年里 1 年跑输基准），不藏着；
 *   - 底部给出记录条数 —— 这些数字是样本内的，只有等 journal 攒够结算样本，
 *     才能知道它在样本外还成不成立。
 */

const TIER_STYLE = {
  low: {
    icon: ShieldCheck,
    ring: 'border-accent/45',
    bg: 'bg-accent/[0.06]',
    text: 'text-accent',
    label: '低风险',
    hint: '历史上这一档的暴跌概率约 1/10',
  },
  mid: {
    icon: ShieldQuestion,
    ring: 'border-border/40',
    bg: 'bg-elevated/25',
    text: 'text-secondary',
    label: '中性',
    hint: '与市场平均状态没有显著差别',
  },
  high: {
    icon: ShieldAlert,
    ring: 'border-danger/45',
    bg: 'bg-danger/[0.08]',
    text: 'text-danger',
    label: '高危',
    hint: '历史上这一档近三成标的在 20 日内跌超 10%',
  },
} as const

function pct(v: number | null | undefined, digits = 0) {
  if (v == null) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function signedPct(v: number | null | undefined, digits = 1) {
  if (v == null) return '—'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

export function EntryPointPanel({
  data, loading, error,
}: {
  data: EntryPoint | undefined
  loading: boolean
  error: boolean
}) {
  const [openDetail, setOpenDetail] = useState(false)

  if (loading && !data) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted flex items-center gap-2">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        正在算买点档位…
      </div>
    )
  }
  if (error && !data) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted">
        买点体检不可用（接口失败）—— 不显示不等于当前是买点。
      </div>
    )
  }
  if (!data) return null
  if (!data.available) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted">
        {data.reason || '这只票没有买点体检'}
      </div>
    )
  }

  const v = data.verdict
  const st = TIER_STYLE[v.tier as keyof typeof TIER_STYLE] ?? TIER_STYLE.mid
  const Icon = st.icon
  const tierStat = data.stats.tier
  const base = data.stats.base
  const jr = data.journal

  return (
    <div
      className={`rounded-lg border ${st.ring} ${st.bg} overflow-hidden`}
      data-testid="entry-point"
    >
      {/* 头行：档位是主角 */}
      <div className="px-4 py-2.5 flex items-center gap-2.5">
        <Icon className={`h-5 w-5 shrink-0 ${st.text}`} />
        <span className={`text-[15px] font-semibold ${st.text}`}>{data.tier_label}</span>
        <span className="text-[10px] text-muted">{st.hint}</span>
        <span className="ml-auto text-[10px] font-mono text-muted/70">{data.as_of}</span>
      </div>

      {/* 条件清单：命中的用实色，未命中用虚线 —— "为什么是这个档位"必须可追 */}
      <div className="px-4 pb-2.5 flex flex-wrap items-center gap-1.5">
        {v.checks.map(c => (
          <span
            key={c.id}
            title={c.detail}
            className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-[10px] border ${
              c.hit
                ? 'border-accent/40 bg-accent/[0.10] text-accent'
                : c.value == null
                  ? 'border-dashed border-border/50 text-muted/50'
                  : 'border-border/30 text-muted/70'
            }`}
          >
            <span className="font-mono opacity-60">{c.id}</span>
            {c.label}
            {c.hit ? <Check className="h-3 w-3" strokeWidth={3} />
              : c.value == null ? <Minus className="h-3 w-3 opacity-50" />
              : <X className="h-3 w-3 opacity-40" />}
          </span>
        ))}
        {v.bear_hits > 0 && (
          <span
            title="空头条件命中数；命中 ≥2 才会降到「高危」。命中 1 项时只作提示。"
            className="ml-1 inline-flex items-center gap-1 rounded px-2 py-0.5 text-[10px] border border-danger/35 bg-danger/[0.08] text-danger"
          >
            风险提示 {v.bear_hits}/3
          </span>
        )}
      </div>

      {/* 关键数字：本档 vs 基准，两列并排 —— 没有基准的胜率没有意义 */}
      {data.calibrated && tierStat && base ? (
        <div className="px-4 pb-2.5 grid grid-cols-3 gap-2 text-[10px]">
          <Metric
            label="20日胜率"
            value={pct(tierStat.win, 1)}
            delta={`${tierStat.win_delta_pp >= 0 ? '+' : ''}${tierStat.win_delta_pp.toFixed(1)}pp`}
            base={pct(base.win, 1)}
            good={tierStat.win_delta_pp > 0}
          />
          <Metric
            label="暴跌率"
            value={pct(tierStat.crash, 1)}
            delta={`${tierStat.crash_delta_pp >= 0 ? '+' : ''}${tierStat.crash_delta_pp.toFixed(1)}pp`}
            base={pct(base.crash, 1)}
            good={tierStat.crash_delta_pp < 0}
          />
          <Metric
            label="中位收益"
            value={signedPct(tierStat.med)}
            delta={`n=${tierStat.n.toLocaleString()}`}
            base={signedPct(base.med)}
            good={tierStat.med > 0}
          />
        </div>
      ) : (
        <div className="px-4 pb-2.5 text-[10px] text-warning">
          未校准：`data/entry_check/stats.json` 缺失，本档不给历史数字（不编造）。
        </div>
      )}

      {/* 诚实声明 + 展开细节 */}
      <button
        onClick={() => setOpenDetail(x => !x)}
        className="w-full px-4 py-1.5 border-t border-border/25 flex items-center gap-1 text-left hover:bg-elevated/20 transition-colors"
      >
        {openDetail ? <ChevronDown className="h-3.5 w-3.5 text-muted" /> : <ChevronRight className="h-3.5 w-3.5 text-muted" />}
        <span className="text-[10px] text-muted">
          {openDetail ? '收起' : '这个档位怎么算的、能信到什么程度'}
        </span>
        <span className="ml-auto text-[10px] text-muted/60 flex items-center gap-1">
          <History className="h-3.5 w-3.5" />
          已记录 {jr.total} 次 · 已结算 {jr.settled}
        </span>
      </button>

      {openDetail && (
        <div className="px-4 py-2.5 border-t border-border/25 space-y-2 text-[10px] leading-relaxed">
          <div className="text-muted">
            <span className="text-foreground/80">判据（{v.criteria}）</span>：四个多头条件
            （低换手 / 低波动 / 涨幅居中 / 户数不膨胀）各按同日横截面分位判定，
            命中 ≥3 为低风险；三个空头条件（高换手 / 高波动 / 涨幅过大）命中 ≥2 强制降为高危。
            两套条件是独立的 ——「风险提示」只在降档时起作用，不会挡住升档。
            分位是「相对同期其他票」，不是绝对值：换手率的中枢跨年份差一倍，绝对值不可比。
          </div>
          <div className="text-muted">
            <span className="text-foreground/80">它不能做什么</span>：不预测涨跌。低风险档
            胜率 {pct(tierStat?.win, 1)}、中位 {signedPct(tierStat?.med)}，只比基准略好；
            {data.stats.stability && `10 年里有 ${data.stats.stability.win_below_base} 年胜率跑输基准，`}
            所以这里不叫「买入信号」。
          </div>
          <div className="text-muted">
            <span className="text-foreground/80">它能做什么</span>：压低尾部。低风险档暴跌率
            {pct(tierStat?.crash, 1)}（基准 {pct(base?.crash, 1)}），
            {data.stats.stability && `10 年里 ${data.stats.stability.years - data.stats.stability.crash_worse_than_base} 年优于基准`}
            。买点决策里「不会死」比「会涨」可靠得多。
          </div>
          {data.stats.meta && (
            <div className="text-muted/70">
              样本 {data.stats.meta.sample}，{data.stats.meta.n_dates} 期，
              {data.stats.meta.n_rows?.toLocaleString()} 票日；未来 {data.stats.meta.fwd_days} 日、
              {data.stats.meta.return_basis}。
            </div>
          )}
          <div className="text-muted/70">
            <span className="text-foreground/80">怎么证伪它</span>：上面这些数字都是样本内的。
            每次打开这一页都会记一条判定，{data.stats.meta?.fwd_days ?? 20} 个交易日后自动结算，
            累计样本后与历史期望对照 —— 如果实际暴跌率明显高于历史值，就说明判据失效了。
            {jr.settled > 0 && (
              <span>
                {' '}当前已结算 {jr.settled} 条
                {jr.tiers.low?.n ? `（低风险档 ${jr.tiers.low.n} 条，实际暴跌率 ${pct(jr.tiers.low.crash, 1)}）` : ''}。
              </span>
            )}
          </div>
          {jr.symbol_history.length > 0 && (
            <div>
              <div className="text-foreground/80 mb-0.5">这只票的判定轨迹</div>
              <div className="space-y-0.5">
                {jr.symbol_history.map(h => (
                  <div key={h.as_of} className="flex items-center gap-2 text-muted/80">
                    <span className="font-mono w-20">{h.as_of}</span>
                    <span className="w-12">{TIER_STYLE[h.tier as keyof typeof TIER_STYLE]?.label ?? h.tier}</span>
                    <span className="font-mono">
                      {h.fwd20 == null ? '待结算' : `20日 ${signedPct(h.fwd20, 1)}`}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Metric({
  label, value, delta, base, good,
}: {
  label: string
  value: string
  delta: string
  base: string
  good: boolean
}) {
  return (
    <div className="rounded border border-border/25 bg-base/30 px-2.5 py-1.5">
      <div className="text-muted/80">{label}</div>
      <div className="flex items-baseline gap-1.5">
        <span className="font-mono text-[14px] text-foreground">{value}</span>
        <span className={`font-mono text-[10px] ${good ? 'text-accent' : 'text-danger/80'}`}>{delta}</span>
      </div>
      <div className="text-[9px] text-muted/60 font-mono">基准 {base}</div>
    </div>
  )
}
