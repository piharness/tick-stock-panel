/**
 * 筹码久期（/chip）—— 理论稿，未验证。
 *
 * 这一页不是旧页的「再写一遍」，而是换对象：
 *   旧页：价格上的密度 S(p)，问「密度高是否意味着未来差」；
 *   新页：持仓的期限结构 S(p,a)，问「谁持有、还能拿多久、跌了会不会被迫卖、谁会被一起拖下水」。
 *
 * 三件事必须同时成立，页面才有意义：
 *   1) 旧页的零要被解释，而不是被忽略（§01 退化）；
 *   2) 新对象要被造出来，而不是只画箭头（§02 三层 / §03 久期面板）；
 *   3) 新预测要能被杀死，而不是能吸收一切（§05 七条 / §07 杀死条件）。
 *
 * 所有数字唯一来源 ./data.ts；新数字是描述量，不是判据结果。
 * 排版复用 structural-edge/ui.tsx 的八档字号 / 发丝线 / 语义色，不另建设计系统。
 */
import { useCallback } from 'react'
import { Section, DataTable, Callout, Tag, Tiles, Tile, StatGrid, useAxis, type Col } from '../structural-edge/ui'
import { scrollToId } from '../structural-edge/Shell'
import { cn } from '@/lib/cn'
import { useTheme } from '@/lib/theme'
import {
  AGE_YEARS, CROSS, DEGENERACY, H1, H1_CELLS, H2, H2_COMPS, H3, H3_BUCKETS, H3_CELLS, H3_DOSE,
  H4, H4_COND, H4_QUINTILES, HEATMAPS, KILL, LAYERS, META, PHASES, PREDICTIONS,
  REINTERPRET, SUMMARY, VERSIONS,
  type AgeYear, type DegeneracyRow, type H1Cell, type H2Comp, type H3Bucket, type H3Cell, type H3Dose,
  type H4Cond, type H4Quintile, type Heatmap, type Phase, type Prediction, type ReinterpretRow,
} from './data'

const fmtSigned = (v: number, d = 3) => `${v < 0 ? '−' : '+'}${Math.abs(v).toFixed(d)}`
const fmtPct = (v: number, d = 2) => `${v < 0 ? '−' : '+'}${Math.abs(v).toFixed(d)}%`
const fmtT = (v: number, d = 2) => `${v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}`

const h1Cols: Col<H1Cell>[] = [
  { head: 'D 分位', w: '18%', render: r => <span className="text-[13px] text-foreground">{r.d}</span> },
  { head: 'T 低', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.tlo)}</span> },
  { head: 'T 中', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.tmid)}</span> },
  { head: 'T 高', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.thi)}</span> },
  { head: 'mdd（T 高）', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-danger">{fmtPct(r.mdd)}</span> },
]

const TOC = [
  { id: 'degeneracy', label: '退化' },
  { id: 'layers', label: '三层' },
  { id: 'duration', label: '久期' },
  { id: 'phase', label: '相图' },
  { id: 'predictions', label: '七条' },
  { id: 'h1', label: 'H1' },
  { id: 'h2', label: 'H2' },
  { id: 'h3', label: 'H3' },
  { id: 'h4', label: 'H4' },
  { id: 'reinterpret', label: '新读法' },
  { id: 'kill', label: '杀死条件' },
  { id: 'versions', label: '版本' },
]

/** 中位久期 + p25–p75 的年度条。深色=中位，浅带=四分位区间。 */
function DurationBars({ rows }: { rows: AgeYear[] }) {
  const { barFocus } = useAxis()
  const MAX = 160
  return (
    <div className="mt-5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-[11px] text-muted">
        <span>中位久期 D（天）；浅带为 p25–p75。月末截面先取中位，再按年取中位。</span>
        <span className="font-mono">2019 → 2026 · A=500</span>
      </div>
      <div className="mt-3 grid grid-cols-8 gap-x-2 border-b border-border pb-1" style={{ height: 230 }}>
        {rows.map(y => (
          <div key={y.year} className="flex min-w-0 flex-col justify-end" data-testid="age-bar">
            <div className="relative flex-1">
              <div
                className="absolute left-1/2 w-3/5 -translate-x-1/2 rounded-[2px]"
                style={{
                  bottom: `${(y.p25 / MAX) * 100}%`,
                  height: `${Math.max(((y.p75 - y.p25) / MAX) * 100, 1)}%`,
                  backgroundColor: barFocus,
                  opacity: 0.18,
                }}
              />
              <div
                className="absolute bottom-0 left-1/2 w-2/5 -translate-x-1/2 rounded-t-[2px]"
                style={{ height: `${(y.d / MAX) * 100}%`, backgroundColor: barFocus }}
              />
            </div>
            <div className="mt-1.5 text-center font-mono text-[11px] tabular-nums text-foreground">{y.d.toFixed(0)}</div>
            <div className="text-center text-[11px] text-muted">{y.year}</div>
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between font-mono text-[10px] text-muted/80">
        <span>0</span><span>40</span><span>80</span><span>120</span><span>160 天</span>
      </div>
    </div>
  )
}

/** 单只股票的 (价格 × 年龄) 热力图。横轴年龄，纵轴相对现价；虚线=现价。 */
function AgeHeatmap({ h }: { h: Heatmap }) {
  const dark = useTheme() === 'dark'
  const max = Math.max(...h.matrix.flat())
  const rows = [...h.matrix].reverse()
  return (
    <div data-testid="age-heatmap" className="min-w-0">
      <div className="flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <span className="text-[14px] font-medium text-foreground">{h.name}</span>
          <span className="ml-2 font-mono text-[11px] text-muted">{h.symbol}</span>
        </div>
        <span className="shrink-0 font-mono text-[11px] tabular-nums text-muted">D {h.d.toFixed(0)}d</span>
      </div>
      <div className="mt-3 flex items-stretch gap-2">
        <div className="flex flex-col justify-between py-0.5 font-mono text-[10px] leading-none text-muted">
          <span>1.4</span>
          <span>1.0</span>
          <span>0.6</span>
        </div>
        <div className="min-w-0 flex-1">
          <div className="relative">
            <div className="grid gap-px overflow-hidden rounded-[2px] bg-border/60" style={{ gridTemplateColumns: 'repeat(20, minmax(0, 1fr))' }}>
              {rows.map((row, i) =>
                row.map((v, j) => {
                  const a = max > 0 ? Math.pow(v / max, 0.55) : 0
                  const color = dark
                    ? `rgba(133,183,235,${0.05 + 0.95 * a})`
                    : `rgba(24,95,165,${0.05 + 0.95 * a})`
                  return <div key={`${i}-${j}`} style={{ backgroundColor: color, aspectRatio: '1 / 1' }} />
                }),
              )}
            </div>
            <div className="pointer-events-none absolute inset-x-0 top-1/2 border-t border-dashed border-foreground/50" />
            <div className="pointer-events-none absolute inset-y-0 left-0 w-px bg-border" />
          </div>
          <div className="relative mt-1 h-4 font-mono text-[10px] text-muted/80">
            {[0, 100, 200, 300, 400, 500].map((x, i) => (
              <span
                key={x}
                className="absolute"
                style={{
                  left: `${i * 20}%`,
                  transform: i === 0 ? 'none' : i === 5 ? 'translateX(-100%)' : 'translateX(-50%)',
                }}
              >
                {x}
              </span>
            ))}
          </div>
        </div>
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2 border-t border-border pt-2 text-[11px] leading-[1.6] text-muted">
        <span>老钱 &gt;500d <b className="font-mono font-medium text-foreground">{h.oldShare}%</b></span>
        <span>新钱 &lt;20d <b className="font-mono font-medium text-foreground">{h.young20}%</b></span>
        <span>年龄熵 <b className="font-mono font-medium text-foreground">{h.h}</b></span>
      </div>
    </div>
  )
}

const degeneracyCols: Col<DegeneracyRow>[] = [
  { head: '症状', w: '24%', render: r => <span className="text-[13px] text-foreground">{r.symptom}</span> },
  { head: '旧页读数', render: r => <span className="font-mono text-[12px] tabular-nums text-secondary">{r.old}</span> },
  { head: '新读法', render: r => <span className="text-[12px] text-muted">{r.reading}</span> },
]

const predictionCols: Col<Prediction>[] = [
  { head: '#', w: '44px', render: r => <span className="font-mono text-[12px] font-medium text-foreground">{r.id}</span> },
  { head: '预测', w: '30%', render: r => <span className="text-[13px] text-foreground">{r.claim}</span> },
  { head: '判据', render: r => <span className="font-mono text-[12px] text-secondary">{r.test}</span> },
  { head: '状态', w: '184px', render: r => <Tag tone={r.tone ?? 'muted'}>{r.status}</Tag> },
]

const h3Cols: Col<H3Cell>[] = [
  { head: '格', w: '22%', render: r => <span className="text-[13px] text-foreground">{r.cell}</span> },
  { head: 'fwd20', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.bp, 1)}bp</span> },
  { head: 't', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{fmtT(r.t, 2)}</span> },
  { head: '预注册', w: '96px', render: r => <span className="text-[12px] text-muted">{r.pred}</span> },
  { head: '实测', w: '104px', render: r => <Tag tone={r.tone}>{r.actual}</Tag> },
]

const h3doseCols: Col<H3Dose>[] = [
  { head: 'K（带宽）', w: '18%', render: r => <span className="font-mono text-[12px] tabular-nums text-foreground">{r.k}</span> },
  { head: '事件', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{r.n.toLocaleString()}</span> },
  { head: '上穿−下穿', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.ud, 1)}bp</span> },
  { head: 't', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{fmtT(r.udT, 1)}</span> },
  { head: '慢−快', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.sf, 1)}bp</span> },
]

const h3bucketCols: Col<H3Bucket>[] = [
  { head: '\|cum5\|', w: '16%', render: r => <span className="font-mono text-[12px] tabular-nums text-foreground">{r.bucket}</span> },
  { head: '下穿 fwd20', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-ok">{fmtSigned(r.dn20, 1)}</span> },
  { head: '下穿 fwd60', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-ok">{fmtSigned(r.dn60, 1)}</span> },
  { head: '上穿 fwd20', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-danger">{fmtSigned(r.up20, 1)}</span> },
  { head: '上穿 fwd60', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-danger">{fmtSigned(r.up60, 1)}</span> },
]

const h4qCols: Col<H4Quintile>[] = [
  { head: 'disp 五分位', w: '20%', render: r => <span className="text-[13px] text-foreground">{r.q}</span> },
  { head: '中位 disp', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.disp.toFixed(2)}</span> },
  { head: '冲击深度', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-danger">{r.drop.toFixed(2)}%</span> },
  { head: 'vol20', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{r.vol.toFixed(2)}%</span> },
  { head: 'rec20', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.rec20.toFixed(1)}%</span> },
  { head: 'half_days', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{r.half.toFixed(1)}</span> },
]

const h4cCols: Col<H4Cond>[] = [
  { head: '冲击深度三分位', w: '24%', render: r => <span className="text-[13px] text-foreground">{r.tercile}</span> },
  { head: '平均冲击', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-danger">{r.drop.toFixed(2)}%</span> },
  { head: 'rec20 Q5−Q1', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.rec20, 2)}pp</span> },
  { head: 't', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{fmtT(r.t, 2)}</span> },
  { head: 'half_days 差', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.half, 2)}</span> },
]

const h2Cols: Col<H2Comp>[] = [
  { head: '分项', w: '14%', render: r => <span className="text-[13px] text-foreground">{r.comp}</span> },
  { head: '非零', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{Math.round(r.cover * 100)}%</span> },
  { head: 'dens×分项→fwd (bp)', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.fwd, 1)}</span> },
  { head: 't (fwd)', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{fmtT(r.fwdT, 2)}</span> },
  { head: 'dens×分项→frag (z)', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.frag, 4)}</span> },
  { head: 't (frag)', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{fmtT(r.fragT, 2)}</span> },
  { head: '读法', w: '104px', render: r => <Tag tone={r.tone}>{r.read}</Tag> },
]

const reinterpretCols: Col<ReinterpretRow>[] = [
  { head: '旧结果', w: '27%', render: r => <span className="text-[13px] text-foreground">{r.old}</span> },
  { head: '旧读法', render: r => <span className="text-[12px] text-muted">{r.oldRead}</span> },
  { head: '新读法', render: r => <span className="text-[12px] text-secondary">{r.newRead}</span> },
]

export function ChipTheory() {
  const go = useCallback((id: string) => scrollToId(id), [])

  return (
    <div className="mx-auto max-w-[1180px] px-6 pb-24 pt-8">
      <header className="flex flex-col gap-6 border-b border-border pb-7 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <div className="text-[11px] font-medium tracking-[0.18em] text-muted">
            研究结论 · 筹码久期 · 理论稿（未验证）
          </div>
          <h1 className="mt-3 text-[30px] font-semibold leading-[1.15] tracking-[-0.02em] text-foreground">
            筹码久期<span className="mx-2.5 font-light text-muted/40">/</span>从成本分布到持仓期限结构
          </h1>
          <p className="mt-3 max-w-[46em] text-[14px] leading-[1.8] text-secondary">
            旧页证明「密度不是信号」；本页说明为什么——密度是期限结构在价格上的边际。
            新对象已建成：5,582 只 × 95 个月末的年龄面板；H1–H4 已跑，一条部分通过、三条失败。
          </p>
        </div>
        <div className="shrink-0 text-[11px] leading-[1.9] text-muted lg:text-right">
          <div className="font-mono tabular-nums">数据 {META.asOf}</div>
          <div>{META.sample}</div>
          <div className="text-warning">H1–H4 已跑 · 一条部分通过</div>
        </div>
      </header>

      <nav aria-label="章节" className="-mx-1 mt-5 flex flex-wrap gap-x-1 gap-y-1 border-b border-border pb-3">
        {TOC.map(x => (
          <button
            key={x.id}
            type="button"
            onClick={() => go(x.id)}
            className="rounded px-2 py-1 text-[12px] text-muted transition-colors hover:bg-elevated hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            {x.label}
          </button>
        ))}
      </nav>

      <div className="mt-6 grid gap-6 border-b border-border pb-6 sm:grid-cols-3">
        {SUMMARY.map(s => (
          <div key={s.label} className="flex items-start justify-between gap-4 sm:block">
            <div className="text-[11px] leading-4 tracking-[0.12em] text-muted">{s.label}</div>
            <div className="mt-1.5 sm:mt-2">
              <Tag tone={s.verdict}>{s.value}</Tag>
            </div>
            <p className="mt-1.5 max-w-[24em] text-[12px] leading-[1.7] text-secondary sm:mt-2">{s.note}</p>
          </div>
        ))}
      </div>

      <Section
        id="degeneracy"
        no="01"
        title="旧理论的退化"
        lead="三个症状不是三次失败，是同一次投影错误：把持有人（c）和持有年龄（a）边际掉，只剩价格上的密度 S(p)。"
        actions={<span className="text-[11px] text-muted">旧结论冻结 · 2026-11-26</span>}
        first
        className="mt-10"
      >
        <DataTable
          cols={degeneracyCols}
          rows={DEGENERACY}
          rowKey={r => r.symptom}
          reason="旧页的读数保留原样，不重新估计；它们在这里的角色从「失败记录」变成「退化证据」。"
        />
        <Callout tone="danger" label="一句话">
          边际掉 a 和 c 之后，密度只能是换手率的核密度重构——这不是模型没调好，是投影选错了。
        </Callout>
      </Section>

      <Section
        id="layers"
        no="02"
        title="新对象：三层结构"
        lead="新对象不是 S(p)，是 S(p,a) × c × Aᵢⱼ。三层缺一不可：没有 c，密度的符号不可识别；没有 A，尾部风险只是单股票属性。"
        source="analysis/_build_chip_age.py · _build_chip_holder.py"
      >
        <Tiles cols={3}>
          {LAYERS.map((l, i) => (
            <div key={l.tag} data-testid="layer-card">
              <Tile lead={`0${i + 1}`} aside={<span className="font-mono text-[10px] text-muted">{l.data}</span>} title={l.title} body={l.body} />
            </div>
          ))}
        </Tiles>
        <StatGrid
          cols={4}
          items={[
            { k: '年龄面板', v: '5,582 只', d: '2019-01 ~ 2026-11 · 95 个月末' },
            { k: '年龄上限', v: '500 日', d: 'A=500；更老的进 old_share' },
            { k: '持有人', v: '5,650 只', d: 'top_float 2026Q3；剔除托管/结算' },
            { k: '共同持有人', v: '中位 3 个', d: 'p90 7 个；shared_score 中位 39.5' },
          ]}
        />
        <Callout tone="warn" label="边界">
          top_float 只覆盖前十大流通股东，比例是占总股本而不是自由流通盘；网络是粗代理，不是完整基金持仓。
          年龄面板是当前在市股票回溯（5,582 只），含生存偏差——时间序列的绝对水平会偏，横截面与方向仍可用。
          这些边界不影响「同一中位数藏着两种市场」的描述，但会限制 H2/H5 的强度。
        </Callout>
      </Section>

      <Section
        id="duration"
        no="03"
        title="久期：市场真的变短了"
        lead={`同一个中位数，藏着两种市场。2026 年中位久期 ${CROSS.latestD} 天，但 p10 ${CROSS.p10}、p90 ${CROSS.p90}；工商银行 222 天、万科 42 天。`}
        source="analysis/_build_chip_age.py · _build_chip_examples.py"
      >
        <DurationBars rows={AGE_YEARS} />
        <div className="mt-6 grid grid-cols-2 gap-x-8 gap-y-3 border-t border-border pt-4 sm:grid-cols-4">
          <div>
            <div className="text-[11px] text-muted">2019 → 2026</div>
            <div className="mt-1 font-mono text-[18px] font-semibold tabular-nums text-foreground">97.5 → 38.5</div>
            <div className="text-[11px] text-muted">中位久期，天</div>
          </div>
          <div>
            <div className="text-[11px] text-muted">新钱 &lt;20 日</div>
            <div className="mt-1 font-mono text-[18px] font-semibold tabular-nums text-foreground">16.6% → 37.9%</div>
            <div className="text-[11px] text-muted">中位股票</div>
          </div>
          <div>
            <div className="text-[11px] text-muted">年龄熵</div>
            <div className="mt-1 font-mono text-[18px] font-semibold tabular-nums text-foreground">5.29 → 4.51</div>
            <div className="text-[11px] text-muted">期限多样性下降</div>
          </div>
          <div>
            <div className="text-[11px] text-muted">q90 年龄</div>
            <div className="mt-1 font-mono text-[18px] font-semibold tabular-nums text-foreground">219 → 86</div>
            <div className="text-[11px] text-muted">90% 筹码的年龄上限</div>
          </div>
        </div>

        <div className="mt-10 grid gap-8 border-t border-border pt-5 lg:grid-cols-2">
          {HEATMAPS.map(h => <AgeHeatmap key={h.symbol} h={h} />)}
        </div>
        <div className="mt-4 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 text-[11px] text-muted">
          <span>上图是描述，不是判据：同一张热力图里，横轴是年龄、纵轴是相对现价；虚线=现价。</span>
          <span className="font-mono">2026-11-26 · 20×20 分箱</span>
        </div>

        <Callout tone="warn" label="读法">
          工商银行是「老钱、浮盈、长久期」：52% 的筹码超过 500 天，成本在现价下方；万科是「新钱、浮亏、短久期」：
          0% 超过 500 天，成本在现价上方。旧页的「50 天」是一个平均数，不是市场的状态。
        </Callout>
      </Section>

      <Section
        id="phase"
        no="04"
        title="(D, Γ) 相图"
        lead="同一个成本峰，可能是钉子，也可能是悬崖。Γ 的符号由持有人的凸性决定：杠杆/短线/赎回是负伽马，险资/指数/长钱是正伽马。"
      >
        <Tiles cols={2}>
          {PHASES.map((p: Phase) => (
            <Tile
              key={p.key}
              lead={`${p.d} · ${p.gamma}`}
              aside={<Tag tone={p.tone}>{p.tone === 'ok' ? '稳' : '脆'}</Tag>}
              title={p.title}
              body={p.body}
            />
          ))}
        </Tiles>
        <Callout tone="danger" label="旧页的位置">
          旧页的「密度高 → 差」是这张表的对角线平均：漂移和雪崩符号相反，平均接近零。−26bp 不是「没有效应」，
          是两个相反效应的残差。
        </Callout>
      </Section>

      <Section
        id="predictions"
        no="05"
        title="七个预测"
        lead="七条预注册草案：H1 部分通过并被拆成两条主效应；H2/H3/H4 失败。先写死判据，再看数；最低验收组合仍是 H1+H2+H3。"
        source="docs/structural-edge/chip-duration.md §13"
      >
        <DataTable
          cols={predictionCols}
          rows={PREDICTIONS}
          rowKey={r => r.id}
          dense
          minWidth={880}
          reason="阈值是预注册起点，不是真理；写死之后才能改，改要记录。所有 t 值按日聚合，重叠窗用 Newey-West 校正。"
          swipeHint
        />
        <Callout tone="warn" label="最低验收">
          三条全灭，新理论只是更好听的隐喻；活两条以上，它值得一个页面和一条预注册研究线。
        </Callout>
      </Section>

      <Section
        id="h1"
        no="06"
        title="H1 结果：错配被拆成两条主效应"
        lead={`原始 H1 用 M=T/D 比值；M 与 D 的秩相关 ${fmtSigned(H1.corrMD, 2)}，与 20 日换手 +${H1.corrMTurn}——比值被分母主导。把分子分母拆开后，看到的是两条独立主效应：短久期、慢重估，各自加深回撤；二者的交互只有 +${H1.interaction}。`}
        source="analysis/_diag_chip_h1.py · _diag_chip_h1_decomp.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: 'D 主效应', v: fmtSigned(H1.bLogD), d: `fragility 对 logD 的标准化系数 · t ${fmtT(H1.tLogD, 2)}` },
            { k: 'T 主效应', v: fmtSigned(H1.bLogT), d: `fragility 对 logT 的标准化系数 · t ${fmtT(H1.tLogT, 2)}` },
            { k: '错配交互', v: fmtSigned(H1.interaction), d: `t ${fmtT(H1.interactionT, 2)} · 经济上可忽略` },
            { k: '对 fwd20', v: fmtPct(H1.fwdSpread), d: `Q5−Q1 · t ${fmtT(H1.fwdT, 2)}；两条主效应均不显著` },
          ]}
        />
        <div className="mt-6 grid grid-cols-2 gap-x-8 gap-y-3 border-t border-border pt-4 sm:grid-cols-4">
          <div>
            <div className="text-[11px] text-muted">样本</div>
            <div className="mt-1 font-mono text-[18px] font-semibold tabular-nums text-foreground">{H1.n.toLocaleString()}</div>
            <div className="text-[11px] text-muted">{H1.symbols} 只 × {H1.dates} 个月末</div>
          </div>
          <div>
            <div className="text-[11px] text-muted">回撤极差</div>
            <div className="mt-1 font-mono text-[18px] font-semibold tabular-nums text-foreground">{fmtPct(H1.mddSpread)}</div>
            <div className="text-[11px] text-muted">mdd Q5−Q1（T 高组）</div>
          </div>
          <div>
            <div className="text-[11px] text-muted">OOS ρ</div>
            <div className="mt-1 font-mono text-[18px] font-semibold tabular-nums text-foreground">{H1.oos1.toFixed(2)} <span className="text-muted">/</span> {H1.oos2.toFixed(2)}</div>
            <div className="text-[11px] text-muted">2019-22 / 2023-26</div>
          </div>
          <div>
            <div className="text-[11px] text-muted">偏度 / 恢复</div>
            <div className="mt-1 font-mono text-[18px] font-semibold tabular-nums text-foreground">{fmtSigned(H1.skewRho)} <span className="text-muted">/</span> {fmtSigned(H1.recoveryRho)}</div>
            <div className="text-[11px] text-muted">都不支持：效应在回撤，不在偏度/恢复</div>
          </div>
        </div>

        <DataTable
          cols={h1Cols}
          rows={H1_CELLS}
          rowKey={r => r.d}
          reason="fragility 是月度截面 z 的均值（−mdd、−skew、−worst5、+dvol 的等权）；mdd 是未来 20 日最大回撤。向下读=D 主效应，向右读=T 主效应。"
        />

        <Callout tone="warn" label="裁决">
          H1 部分通过 / 已分解：D 和 T 都是风险指标，不是收益因子；「短钱 + 慢故事」的角最差（mdd −15.8%），
          但原因是两条主效应相加，不是错配交互。按原判据「|ρ| 小于 0.05」不过（rank −0.064），但 Q5−Q1 只有 −0.53%，经济上可忽略。偏度和恢复天数都不支持——效应在回撤深度和下行波动，不在偏度/恢复速度。
        </Callout>
        <Callout tone="danger" label="方法坑">
          比值 M=T/D 的方差被 D 主导（D 的 p10–p90 19.7–194.6，T 15.1–57.8）；M 与 D 的秩相关 −0.85。
          任何「比值」信号，先拆分子分母；否则你测的是分母。
        </Callout>
      </Section>

      <Section
        id="h2"
        no="07"
        title="H2 结果：合成失败，分项反转"
        lead={`合成伽马（机构−个人）的收益交互只有 ${fmtSigned(H2.fwdInt, 1)}bp（t ${fmtT(H2.fwdT, 2)}），安慰剂第 ${H2.placebo} 百分位；脆弱交互 ${fmtSigned(H2.fragInt, 4)}（t ${fmtT(H2.fragT, 2)}），符号与理论相反；2025/2026 OOS 收益交互 ${fmtSigned(H2.oos2025, 1)} / ${fmtSigned(H2.oos2026, 1)}，符号翻转。`}
        source="analysis/_diag_chip_h2.py · _diag_chip_h2_decomp.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: '合成 · 收益交互', v: `${fmtSigned(H2.fwdInt, 1)}bp`, d: `t ${fmtT(H2.fwdT, 2)} · 安慰剂第 ${H2.placebo} 百分位` },
            { k: '合成 · 脆弱交互', v: `${fmtSigned(H2.fragInt, 4)}`, d: `t ${fmtT(H2.fragT, 2)} · 符号与理论相反` },
            { k: '反转合成 · 脆弱', v: `${fmtSigned(H2.revFrag, 4)}`, d: `t ${fmtT(H2.revFragT, 2)} · 探索性，OOS 同号` },
            { k: '反转合成 · 收益', v: `${fmtSigned(H2.revFwd, 1)}bp`, d: `t ${fmtT(H2.revFwdT, 2)} · 只有风险，没有收益` },
          ]}
        />
        <DataTable
          cols={h2Cols}
          rows={H2_COMPS}
          rowKey={r => r.comp}
          dense
          minWidth={920}
          reason="分项是探索性分解：7 分项 × 2 结果 = 14 个检验，Bonferroni |t|≈2.7；不把「符号反转」写成结论。frag 负=降低密度脆弱性（正伽马候选），正=放大（负伽马候选）。"
          swipeHint
        />
        <Callout tone="danger" label="裁决">
          H2 合成伽马失败：收益交互 +0.6bp（t 0.09），安慰剂第 58 百分位；脆弱交互 +0.015（t 2.47），
          符号与理论相反；2025/2026 OOS 收益交互 +15.9 / −14.7，符号翻转。分项拆开后，符号地图反转：
          个人、户数集中=正伽马候选；基金、法人=负伽马候选；险资/社保/QFII 样本太薄。反转合成 +0.018（t 4.36）
          是探索性假设，不是结论。密度效应仍是换手交互（dens×to −26.2bp，t −2.16），不是伽马交互。
        </Callout>
        <Callout tone="warn" label="方法坑">
          伽马不是「机构 vs 个人」。A 股里基金有赎回/排名，法人有质押/杠杆，个人可能是抄底者；
          类型标签是先验，不是凸性测量。先量赎回压力、质押比例、融资余额，再贴正/负伽马。
        </Callout>
      </Section>

      <Section
        id="h3"
        no="08"
        title="H3 结果：雪崩格反向，密集区不做事"
        lead={`四格里三格符合，只有「下穿·快」符号相反：预注册是雪崩（−），实测是反弹 ${fmtSigned(H3.dnFast, 1)}bp。方向主效应 ${fmtSigned(H3.ud, 1)}bp（t ${fmtT(H3.udT, 2)}）说明上方阻力强于下方支撑；把密集区换成随机带后，这个差反而更强（${fmtSigned(H3.placeboUd, 1)}bp），带宽剂量反应也平坦。`}
        source="analysis/_diag_chip_h3.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: '下穿 · 快（雪崩格）', v: `${fmtSigned(H3.dnFast, 1)}bp`, d: `t ${fmtT(1.74, 2)} · 预注册符号是 −` },
            { k: '方向主效应', v: `${fmtSigned(H3.ud, 1)}bp`, d: `上穿−下穿 · t ${fmtT(H3.udT, 2)}` },
            { k: '交互', v: `${fmtSigned(H3.inter, 1)}bp`, d: `t ${fmtT(H3.interT, 2)} · 速度只在下跌侧成立` },
            { k: '置换带', v: `${fmtSigned(H3.placeboUd, 1)}bp`, d: `${H3.placeboDraws} 次随机带，全部比真实值更强` },
          ]}
        />
        <DataTable
          cols={h3Cols}
          rows={H3_CELLS}
          rowKey={r => r.cell}
          reason="密集区 = 换手加权递归成本带 c±0.5sd，用 t−1 的参数；上穿 = 从带下进入，下穿 = 从带上进入；快 = |cum5| ≥ 5%。入场 t+1 开盘、20 日。极差 473.8bp，但极差是恒正量，其 t 不可读。"
        />
        <div className="mt-7 grid gap-6 lg:grid-cols-2">
          <div>
            <div className="text-[12px] text-muted">带宽剂量反应：K 越大（带越不密），效应应越弱。实测方向效应平坦。</div>
            <DataTable cols={h3doseCols} rows={H3_DOSE} rowKey={r => r.k} dense minWidth={520} />
          </div>
          <div>
            <div className="text-[12px] text-muted">事后分桶（探索）：按 |cum5| 分组，fwd 严格单调 = 过反应修正。</div>
            <DataTable cols={h3bucketCols} rows={H3_BUCKETS} rowKey={r => r.bucket} dense minWidth={480} />
          </div>
        </div>
        <Callout tone="danger" label="裁决">
          H3 失败。「下穿快 = 雪崩」符号相反：A 股快速跌进成本区是反弹 +116bp，不是被迫抛售。
          四格结构在控制后仍在（b_up×fast −129bp，t −4.61），但它属于短期反转：fwd 随 |cum5| 严格单调，
          60 日仍在扩大。密集区本身不做事——20/20 次置换的方向效应都比真实值更强，K 从 0.25 到 1.5 效应不变。
        </Callout>
        <Callout tone="warn" label="与 H2 一致">
          H2 说个人是正伽马候选、基金/法人是负伽马候选；H3 说总量层面快速下跌被接住。
          两个独立测量指向同一件事：A 股的边际持有人是抄底的，不是被迫卖的。
          雪崩需要杠杆数据（融资/质押/赎回），价格路径推不出来。
        </Callout>
      </Section>

      <Section
        id="h4"
        no="09"
        title="H4 结果：原始差是冲击深度混淆"
        lead={`先修轴：age_entropy 与 age_mean 的秩相关 ${H4.entropyVsDuration}，它只是久期的另一种写法。换成尺度无关的 q90/q50（与久期 ${fmtSigned(H4.dispVsDuration, 3)}）后，高离散组 20 日恢复率 ${H4.q5rec20}% vs 低离散 ${H4.q1rec20}%——更慢，与预测相反；但控制冲击深度后符号翻正且不显著，冲击幅度三分位内全部归零。`}
        source="analysis/_diag_chip_h4.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: '熵 ↔ 久期', v: H4.entropyVsDuration.toFixed(3), d: '原始轴的秩相关：不是第二个轴' },
            { k: 'rec20 Q5−Q1', v: `${fmtSigned(H4.spreadRec20, 2)}pp`, d: `t ${fmtT(H4.spreadT, 2)} · 高离散更慢` },
            { k: '控制后 b(disp)', v: fmtSigned(H4.bDisp, 4), d: `t ${fmtT(H4.tDisp, 2)} · 符号翻正、不显著` },
            { k: '快 30% 判据', v: H4.speedRatio.toFixed(2), d: 'half_days 比值，需 ≥1.30；方向反了' },
          ]}
        />
        <DataTable
          cols={h4qCols}
          rows={H4_QUINTILES}
          rowKey={r => r.q}
          reason="disp = q90/q50（年龄分位比，几何分布下 ≈3.32）。冲击 = 特质收益 ≤ −2sd 且 ≤ −3%；半恢复位 = (前收+当日收)/2，60 日截尾。"
        />
        <DataTable
          cols={h4cCols}
          rows={H4_COND}
          rowKey={r => r.tercile}
          dense
          minWidth={560}
          reason="冲击幅度三分位内重做 Q5−Q1：三格全部归零（−0.7 / +0.9 / +0.0）。原始差来自「高离散 = 高波动 = 冲击更深」（corr(disp, drop) = −0.213）。"
        />
        <Callout tone="danger" label="裁决">
          H4 失败。「离散 → 恢复快」在原始分位上符号相反（−3.18pp，t −3.16），控制后翻正且不显著
          （+0.0056，t 1.73），冲击幅度分层内归零。次轴 excess_H 的符号又与原始分位相反。
          三种口径三个答案，唯一一致的是「没有稳定效应」；市况冲击（53 天）也是 −2.64pp。
        </Callout>
        <Callout tone="warn" label="方法坑">
          熵不是分散度。几何型年龄分布下熵 ≈ log(均值)+常数，原始 age_entropy 与久期 ρ=0.989、
          与换手 −0.921——用它跑任何「分散」检验都只是 H1 的复读。这是同一个坑的第三次出现：
          先看被检验变量与冲击幅度/波动的相关，再谈恢复。
        </Callout>
      </Section>

      <Section
        id="reinterpret"
        no="10"
        title="旧结论的新读法"
        lead="旧页的三个描述不是被推翻，而是被重新定位：它们仍然是事实，但不再是理论的终点，而是新理论的边界条件。"
      >
        <DataTable
          cols={reinterpretCols}
          rows={REINTERPRET}
          rowKey={r => r.old}
          reason="这张表是新旧理论的接口：每一行都必须能同时解释旧读法和新读法，否则就是换词，不是换对象。"
        />
        <Callout tone="muted" label="最重要的一条">
          「没有势场」不是「筹码没用」，是「均衡力不存在」。新理论不假设均衡，假设约束；负控杀死的不是筹码，
          是「价格会被成本重心拉回」这个假设。
        </Callout>
      </Section>

      <Section
        id="kill"
        no="11"
        title="什么会杀死这套理论"
        lead="六条写在跑数之前，防止跑完再讲故事。任何一条成立，对应的部分就降级。"
        actions={<span className="text-[11px] text-muted">预注册 · 只追加</span>}
      >
        <ol className="mt-2 space-y-4">
          {KILL.map((k, i) => (
            <li key={k} className="flex gap-4 border-t border-border pt-3 first:border-t-0 first:pt-0">
              <span className="mt-0.5 w-5 shrink-0 font-mono text-[11px] tabular-nums text-muted">
                {String(i + 1).padStart(2, '0')}
              </span>
              <p className="max-w-[46em] text-[13px] leading-[1.75] text-secondary">{k}</p>
            </li>
          ))}
        </ol>
        <Callout tone="danger" label="现状">
          本页跑了 H1–H4：H1 部分通过 / 已分解；H2 合成失败、分项反转；H3 雪崩格反向且密集区不做事；H4 是冲击深度混淆。H5–H7 未跑。旧页的冻结数字保留，角色从「裁决」变成了「退化证据」。
        </Callout>
      </Section>

      <Section
        id="versions"
        no="12"
        title="版本与文件"
        lead="同一天走完四个版本。每一版都保留原文，只追加修订，不覆盖旧结论。"
        actions={<span className="text-[11px] text-muted">只追加</span>}
      >
        <ol className="border-l border-border pl-6">
          {VERSIONS.map((v, i) => (
            <li key={v.title} className="relative pb-7 last:pb-0">
              <span
                aria-hidden
                className={cn(
                  'absolute -left-[27px] top-[7px] h-[7px] w-[7px] rounded-full border-2 border-base',
                  i === VERSIONS.length - 1 ? 'bg-accent' : 'bg-muted/60',
                )}
              />
              <div className="text-[11px] leading-4 text-muted">
                <span className="font-mono tabular-nums">{v.date}</span>
                <span className="mx-2 text-border">·</span>
                <span>{v.volume}</span>
              </div>
              <div className="mt-1 text-[14px] font-medium leading-[1.5] text-foreground">{v.title}</div>
              <p className="mt-1 max-w-[46em] text-[12px] leading-[1.7] text-secondary">{v.body}</p>
            </li>
          ))}
        </ol>
        <div className="mt-6 border-t border-border pt-3 text-[11px] leading-[1.8] text-muted">
          <div className="font-mono">docs/structural-edge/chip-duration.md</div>
          <div className="font-mono">analysis/_build_chip_age.py · _build_chip_holder.py · _build_chip_examples.py</div>
          <div className="font-mono">data/.scratch/chip_age/age_panel.parquet · holder_panel.parquet · examples.json</div>
        </div>
      </Section>

      <footer className="mt-20 flex flex-wrap items-start justify-between gap-x-8 gap-y-3 border-t border-border pt-4 text-[11px] leading-[1.7] text-muted">
        <span className="max-w-[52em]">
          本页只记录研究结论，不构成投资建议。旧页数字冻结于 2026-11-26；新对象数字来自{' '}
          <code className="font-mono text-secondary">pages/chip/data.ts</code>；口径：{META.convention}。
        </span>
        <span className="font-mono tabular-nums">{META.window}</span>
      </footer>
    </div>
  )
}
