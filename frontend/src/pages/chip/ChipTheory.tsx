/**
 * 筹码（/chip）—— 卷五：未付账单。
 *
 * 卷五不是再换一个变量，而是换一张表：
 *   卷一/二/三/四都在**持仓表**（资产端）上找 alpha：谁持有、持有多久、持在什么价格、谁刚发起。
 *   资产按市价计价，市价已经把它定完——十二个状态检验的零不是失败，是默认值。
 *   卷五改读**应付表**（负债端）：谁欠谁多少、多久被重估一次、还剩多少没付。
 *
 * 一条定律：盯市频率。逐日盯市 → gap 每天结清 → 无漂移；事件盯市 → gap 累积到处置日 → 有漂移；
 * 从不盯市 → 根本不是账单 → 只有反转。H9（融资=换手）与 H11（质押贴线 +525bp）由此统一。
 *
 * 三件事必须同时成立，页面才有意义：
 *   1) 十六个读数能被一个恒等式重排成七类（§C）；
 *   2) 新轴要自己先被分离一次：位置 / 能力 / 余额，只有一条会动（§D）；
 *   3) 新预测要能被杀死（§E 五条 / §F 六条），且承认本卷只有一个候选。
 *
 * 卷六（2026-11-28）再拆一层：连「贴线」这个词都没有唯一实现。
 *   四种簿口径给出 +141 / +21 / −42 / −54；水平不可识别，但排序 8/8 一致
 *   （低压力 > 高压力、少合约 > 多合约）。所以卷六预测条件，不预测水平。
 *
 * 所有数字唯一来源 ./data.ts；卷五的探索数字来自 analysis/_diag_chip_ap_probe.py，
 * 卷六来自 analysis/_diag_chip_path_twin.py 与 _diag_chip_largest_contract.py。
 * 排版复用 structural-edge/ui.tsx 的八档字号 / 发丝线 / 语义色，不另建设计系统。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { ChevronRight } from 'lucide-react'
import { Section, DataTable, Callout, Tag, Tiles, Tile, StatGrid, useAxis, type Col } from '../structural-edge/ui'
import { scrollToId } from '../structural-edge/Shell'
import { cn } from '@/lib/cn'
import { useTheme } from '@/lib/theme'
import {
  AGE_YEARS, AP_ATTR, AP_FIX, AP_LEDGER, AP_META, AP_MTM, AP_PRED, CC, CC_PATH, COND, FOUR_DEFS, KILL6, KILL6_NOTE, PRE2019, SHAPE6, STRESS, STRESS_META, P1, P2_TEST,
  AUDIT_BUCKETS, AUDIT_FIX, AUDIT_META, AUDIT_PROOF, AUDIT_READ, AUDIT_SCOPE,
  BOOK_GAPS, BOOK_ROWS, BOOK_V2, BOOK_V3, CONTRAST, COVERAGE, CROSS, DEGENERACY, DIMENSIONS, E_PRED,
  EVENT_READ, G4_ROWS,
  H1, H1_CELLS, H2, H2_COMPS, H3, H3_BUCKETS, H3_CELLS, H3_DOSE, H4, H4_COND, H4_QUINTILES, H5, H567_ROWS, H6, H7,
  H8, H8_CELLS, H9, H11, H11_BUCKETS, HEATMAPS, KILL, KILL4, KILL5, LAYERS, MATCH, MECHANISM, MECH_INTER, META, ONTOLOGY,
  PHASES, PREDICTIONS, REINTERPRET, SCOPE, SECTION_VOLUME, SUMMARY, TRANSFER, TRANSFER_RESULT, TYPE_BIG, TYPE_SPLIT, UNIFY,
  VERSIONS, VERDICT, VOLUMES, EVIDENCE,
  type AgeYear, type APAttrRow, type APLedgerRow, type APPred, type AuditFix, type AuditProof,
  type BookGap, type BookRow,
  type BookV2, type BookV3, type ContrastRow, type CoverRow,
  type DegeneracyRow, type DimensionRow,
  type EPred, type EvidenceRow, type G4Row, type H1Cell, type H2Comp, type H3Bucket, type H3Cell, type H3Dose, type H4Cond,
  type H4Quintile, type H567Row, type H8Cell, type H11Bucket, type Heatmap, type MechInterRow, type MechanismRow,
  type FourDefRow, type ShapeRow, type StressRow,
  type Phase, type Prediction, type ReinterpretRow, type TransferResult, type TransferRow, type TypeBigRow,
  type TypeSplitRow, type UnifyRow, type VolKey, type VolumeDef,
} from './data'

const fmtSigned = (v: number, d = 3) => `${v < 0 ? '−' : '+'}${Math.abs(v).toFixed(d)}`
const fmtPct = (v: number, d = 2) => `${v < 0 ? '−' : '+'}${Math.abs(v).toFixed(d)}%`
const fmtT = (v: number, d = 2) => `${v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}`

const Bp = ({ v, d = 1 }: { v: number; d?: number }) => (
  <span className={cn('font-mono text-[11px] tabular-nums', v < 0 ? 'text-danger' : 'text-foreground')}>
    {fmtSigned(v, d)}
  </span>
)

const fourCols: Col<FourDefRow>[] = [
  {
    head: '口径',
    w: '37%',
    render: r => (
      <span className="text-[12px] leading-[1.6] text-foreground">
        {r.def}
        <span className="ml-2 text-[11px] text-muted">{r.rule}</span>
      </span>
    ),
  },
  {
    head: '水平 · bp / t',
    w: '112px',
    render: r => (
      <span className="whitespace-nowrap">
        <Bp v={r.bp} />
        <span className="ml-1.5 font-mono text-[10.5px] tabular-nums text-muted">/ {fmtT(r.t)}</span>
      </span>
    ),
  },
  { head: '低压力 bp', w: '82px', render: r => <Bp v={r.low} /> },
  { head: '高压力 bp', w: '82px', render: r => <Bp v={r.high} /> },
  { head: '少合约 bp', w: '82px', render: r => <Bp v={r.few} /> },
  { head: '多合约 bp', w: '82px', render: r => <Bp v={r.many} /> },
]

const stressCols: Col<StressRow>[] = [
  { head: '口径', w: '30%', render: r => <span className="text-[12px] text-foreground">{r.def}</span> },
  { head: `低 ${STRESS_META.low}`, w: '86px', render: r => <Bp v={r.low} /> },
  { head: `中 ${STRESS_META.mid}`, w: '86px', render: r => <Bp v={r.mid} /> },
  { head: `高 ${STRESS_META.high}`, w: '86px', render: r => <Bp v={r.high} /> },
  {
    head: '高 − 低 · bp / t',
    w: '124px',
    render: r => (
      <span className="whitespace-nowrap">
        <Bp v={r.delta} />
        <span className="ml-1.5 font-mono text-[10.5px] tabular-nums text-muted">/ {fmtT(r.t)}</span>
      </span>
    ),
  },
]

const h1Cols: Col<H1Cell>[] = [
  { head: 'D 分位', w: '18%', render: r => <span className="text-[13px] text-foreground">{r.d}</span> },
  { head: 'T 低', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.tlo)}</span> },
  { head: 'T 中', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.tmid)}</span> },
  { head: 'T 高', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.thi)}</span> },
  { head: 'mdd（T 高）', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-danger">{fmtPct(r.mdd)}</span> },
]

const ledgerCols: Col<APLedgerRow>[] = [
  { head: '维度', w: '76px', render: r => <span className="text-[13px] font-medium text-foreground">{r.dim}</span> },
  { head: '持仓表（卷一–卷四）', render: r => <span className="text-[12px] leading-[1.6] text-muted">{r.asset}</span> },
  { head: '应付表（卷五）', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.liability}</span> },
]

const attrCols: Col<APAttrRow>[] = [
  { head: '类', w: '92px', render: r => <span className="text-[12px] font-medium text-foreground">{r.cls}</span> },
  { head: '读数', w: '104px', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.results}</span> },
  { head: '旧读法（资产端）', render: r => <span className="text-[12px] leading-[1.6] text-muted">{r.old}</span> },
  { head: '负债端读法', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.neu}</span> },
  { head: '', w: '44px', render: r => <Tag tone={r.verdict}>{r.verdict === 'ok' ? '活' : r.verdict === 'danger' ? '否' : r.verdict === 'warn' ? '半' : '零'}</Tag> },
]

const auditProofCols: Col<AuditProof>[] = [
  { head: '证据', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.test}</span> },
  { head: '读数', w: '268px', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.read}</span> },
]

const auditFixCols: Col<AuditFix>[] = [
  { head: '检验', w: '30%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.test}</span> },
  { head: '旧（前视）', render: r => <span className="font-mono text-[11px] tabular-nums text-muted/80">{r.before}</span> },
  { head: '修正后', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.after}</span> },
  { head: '口径', w: '26%', render: r => <span className="text-[11px] leading-[1.6] text-muted">{r.note}</span> },
]

const ccCols: Col<(typeof CC.rows)[number]>[] = [
  { head: '组', w: '132px', render: r => <span className={cn('text-[12px]', r.group.startsWith('├') || r.group.startsWith('└') ? 'pl-3 text-muted' : 'font-medium text-foreground')}>{r.group.replace(/^[├└]\s*/, '')}</span> },
  { head: 'n', align: 'right', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.n}</span> },
  { head: '事前 60 日', align: 'right', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{fmtSigned(r.pre60, 1)}%</span> },
  { head: 'f20', align: 'right', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{fmtSigned(r.f20, 1)}%</span> },
  { head: 'f40', align: 'right', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{fmtSigned(r.f40, 1)}%</span> },
  { head: 'f60', align: 'right', render: r => <span className={cn('font-mono text-[11px] tabular-nums', r.f60 < -5 ? 'text-danger' : r.f60 > 0 ? 'text-foreground' : 'text-secondary')}>{fmtSigned(r.f60, 1)}%</span> },
  { head: 't', align: 'right', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{fmtT(r.t60, 2)}</span> },
  { head: '胜率', align: 'right', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.win60.toFixed(0)}%</span> },
  { head: 'f120', align: 'right', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{fmtSigned(r.f120, 1)}%</span> },
]

const apPredCols: Col<APPred>[] = [
  { head: '#', w: '44px', render: r => <span className="font-mono text-[12px] font-medium text-foreground">{r.id}</span> },
  { head: '预测', w: '48%', render: r => <span className="text-[13px] leading-[1.6] text-foreground">{r.claim}</span> },
  { head: '判据 / 杀死', render: r => <span className="font-mono text-[12px] leading-[1.6] text-secondary">{r.test}</span> },
]

const evidenceCols: Col<EvidenceRow>[] = [
  { head: '级', w: '40px', render: r => <Tag tone={r.grade === 'A' ? 'ok' : r.grade === 'B' ? 'warn' : 'muted'}>{r.grade}</Tag> },
  { head: '结论', w: '24%', render: r => <span className="text-[13px] leading-[1.6] text-foreground">{r.claim}</span> },
  { head: '读数', render: r => <span className="font-mono text-[11px] leading-[1.6] tabular-nums text-secondary">{r.read}</span> },
  { head: '怎么用', w: '30%', render: r => <span className="text-[11px] leading-[1.6] text-muted">{r.use}</span> },
]

/** 控制权变更：公告前后 180 日的累计超额。国资两线在公告后拐头，民营继续上行。 */
function CCFigure() {
  const A = { x0: 56, x1: 612, y0: 30, y1: 232, min: -8, max: 16 }
  const x = (d: number) => A.x0 + ((d + 60) / 180) * (A.x1 - A.x0)
  const y = (v: number) => A.y1 - ((v - A.min) / (A.max - A.min)) * (A.y1 - A.y0)
  const path = (arr: readonly number[]) =>
    CC_PATH.x.map((d, i) => `${i === 0 ? 'M' : 'L'}${x(d).toFixed(1)},${y(arr[i]).toFixed(1)}`).join(' ')
  return (
    <figure className="mt-4" data-testid="cc-figure">
      <div className="mb-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-[11px] text-muted">
        <span className="flex items-center gap-1.5"><span aria-hidden className="h-[2px] w-4 bg-accent" />民营买方（969）</span>
        <span className="flex items-center gap-1.5"><span aria-hidden className="h-[2px] w-4 bg-danger" />国资 · 现金（45）</span>
        <span className="flex items-center gap-1.5"><span aria-hidden className="h-[2px] w-4 border-t border-dashed border-danger" />国资 · 表决权（58）</span>
      </div>
      <div className="-mx-1 overflow-x-auto">
        <svg viewBox="0 0 680 268" className="w-full min-w-[620px]" role="img" aria-label="控制权变更后国资与民营的累计超额收益">
          <g className="text-muted">
            {[-5, 5, 10, 15].map(v => (
              <line key={v} x1={A.x0} x2={A.x1} y1={y(v)} y2={y(v)} stroke="currentColor" strokeWidth={0.6} opacity={0.35} />
            ))}
          </g>
          {[-5, 0, 5, 10, 15].map(v => (
            <text key={`t${v}`} x={A.x0 - 8} y={y(v) + 3} textAnchor="end" className="fill-current font-mono text-[9px] text-muted">{v}</text>
          ))}
          <line x1={A.x0} x2={A.x1} y1={y(0)} y2={y(0)} stroke="currentColor" strokeDasharray="3 3" className="text-muted" opacity={0.8} />
          <line x1={x(0)} x2={x(0)} y1={A.y0 - 4} y2={A.y1} stroke="currentColor" strokeDasharray="4 4" className="text-muted" opacity={0.5} />
          <text x={x(0) + 4} y={A.y0 + 8} className="fill-current text-[9px] text-muted">公告日</text>

          <path d={path(CC_PATH.priv)} fill="none" stroke="currentColor" strokeWidth={1.9} className="text-accent" />
          <path d={path(CC_PATH.voting)} fill="none" stroke="currentColor" strokeWidth={1.4} strokeDasharray="5 3" className="text-danger" />
          <path d={path(CC_PATH.cash)} fill="none" stroke="currentColor" strokeWidth={1.9} className="text-danger" />

          <text x={x(120) - 4} y={y(11.6) - 9} textAnchor="end" className="fill-current text-[9.5px] text-accent">民营 +11.6</text>
          <text x={x(120) - 2} y={y(-3.7) + 13} textAnchor="end" className="fill-current text-[9.5px] text-danger">现金 −3.7</text>

          {[-60, -30, 0, 30, 60, 90, 120].map(d => (
            <text key={d} x={x(d)} y={A.y1 + 16} textAnchor="middle" className="fill-current font-mono text-[9px] text-muted">{d > 0 ? `+${d}` : d}</text>
          ))}
          <text x={A.x0} y={A.y1 + 33} className="fill-current text-[9.5px] text-muted">交易日（相对公告日）</text>
        </svg>
      </div>
      <figcaption className="mt-1.5 max-w-[48em] text-[11px] leading-[1.7] text-muted">
        三条线都从 −60 日归一为 0。公告前都涨（民营 +5.1、现金 +7.3、表决权 +12.3），
        公告后分叉：民营继续走到 +11.6，国资两条同时拐头，现金组到 −3.7。
        累计超额的口径与下表 f60 略有差异（这里是收盘对收盘，下表是次日开盘入场）。
      </figcaption>
    </figure>
  )
}

/** 修正后：左图余额五桶（深跌反弹），右图 P1 位移曲线（平滑衰减，无悬崖）。 */
function APFixFigure() {
  const A = { x0: 64, x1: 330, y0: 56, y1: 208, min: -150, max: 820 }
  const B = { x0: 432, x1: 690, y0: 56, y1: 208, min: -360, max: -40 }
  const ay = (v: number) => A.y1 - ((v - A.min) / (A.max - A.min)) * (A.y1 - A.y0)
  const by = (v: number) => B.y1 - ((v - B.min) / (B.max - B.min)) * (B.y1 - B.y0)
  const ax = (i: number) => A.x0 + (i / 4) * (A.x1 - A.x0)
  const bx = (i: number) => B.x0 + (i / (P1.shift.length - 1)) * (B.x1 - B.x0)
  return (
    <figure className="mt-4" data-testid="ap-fix-figure">
      <div className="-mx-1 overflow-x-auto">
        <svg viewBox="0 0 720 264" className="w-full min-w-[640px]" role="img" aria-label="余额五桶与 P1 位移曲线">
          <text x={A.x0} y={18} className="fill-current text-[11px] font-medium text-muted">余额：桶内平均 f60（bp）</text>
          <text x={B.x0} y={18} className="fill-current text-[11px] font-medium text-muted">P1 位移：窗口往回挪 k 天（bp/sd）</text>

          <line x1={A.x0} x2={A.x1} y1={ay(0)} y2={ay(0)} stroke="currentColor" strokeDasharray="3 3" className="text-muted" />
          {[-100, 0, 300, 600].map(v => (
            <text key={`ay${v}`} x={A.x0 - 8} y={ay(v) + 3} textAnchor="end" className="fill-current font-mono text-[9px] text-muted">{v}</text>
          ))}
          {AP_FIX.balance.map((r, i) => {
            const y0 = ay(0)
            const y1 = ay(r.mean)
            return (
              <g key={r.bucket}>
                <rect x={ax(i) - 12} y={Math.min(y0, y1)} width={24} height={Math.max(Math.abs(y1 - y0), 1)} fill="currentColor" opacity={r.mean < 0 ? 0.75 : 0.4} className={r.mean < 0 ? 'text-muted' : 'text-danger'} />
                <text x={ax(i)} y={r.mean < 0 ? y1 + 11 : y1 - 6} textAnchor="middle" className="fill-current font-mono text-[9px] text-muted">{r.mean > 0 ? '+' : ''}{r.mean.toFixed(0)}</text>
              </g>
            )
          })}
          {['<−25', '−25~−10', '−10~0', '0~+10', '>+10'].map((t, i) => (
            <text key={t} x={ax(i)} y={A.y1 + 15} textAnchor="middle" className="fill-current text-[9px] text-muted">{t}</text>
          ))}
          <text x={A.x0} y={A.y1 + 32} className="fill-current text-[9.5px] text-muted">亏得越深，反弹越强：+756 → −72</text>

          {[-300, -200, -100].map(v => (
            <g key={`by${v}`} className="text-muted">
              <line x1={B.x0} x2={B.x1} y1={by(v)} y2={by(v)} stroke="currentColor" strokeDasharray="2 4" opacity={0.3} />
              <text x={B.x0 - 8} y={by(v) + 3} textAnchor="end" className="fill-current font-mono text-[9px]">{v}</text>
            </g>
          ))}
          <path
            d={P1.shift.map((r, i) => `${i === 0 ? 'M' : 'L'}${bx(i).toFixed(1)},${by(r.slope).toFixed(1)}`).join(' ')}
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            className="text-accent"
          />
          {P1.shift.map((r, i) => (
            <g key={r.k}>
              <circle cx={bx(i)} cy={by(r.slope)} r={2.8} fill="currentColor" className="text-accent" />
              <text x={bx(i)} y={by(r.slope) - 7} textAnchor="middle" className="fill-current font-mono text-[8.5px] text-muted">{r.slope.toFixed(0)}</text>
            </g>
          ))}
          {P1.shift.map((r, i) => (
            <text key={`k${r.k}`} x={bx(i)} y={B.y1 + 15} textAnchor="middle" className="fill-current font-mono text-[9px] text-muted">{r.k}</text>
          ))}
          <text x={B.x0} y={B.y1 + 32} className="fill-current text-[9.5px] text-muted">平滑衰减，无悬崖；k=120 仍 t −3.3</text>
        </svg>
      </div>
      <figcaption className="mt-1.5 max-w-[48em] text-[11px] leading-[1.7] text-muted">
        左：修正后余额五桶，深跌组 +755.5bp（t 4.42），大涨组 −72.0bp——不是阴跌，是反弹。
        右：把同一段窗口整体往回挪 k 天，系数从 −314 平滑降到 −127，没有悬崖；合约锚是更好的测量点，不是魔法点。
      </figcaption>
    </figure>
  )
}

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

const h11Cols: Col<H11Bucket>[] = [
  { head: '平仓线 / 股价', w: '26%', render: r => <span className="text-[12px] text-foreground">{r.bucket}</span> },
  { head: 'fwd20', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.f20, 1)}bp</span> },
  { head: 'fwd40', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.f40, 1)}bp</span> },
  { head: 'fwd60', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.f60, 1)}bp</span> },
]

const h8Cols: Col<H8Cell>[] = [
  { head: '格', w: '20%', render: r => <span className="text-[13px] text-foreground">{r.cell}</span> },
  { head: 'fwd20', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{fmtSigned(r.bp, 1)}bp</span> },
  { head: 't', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{fmtT(r.t, 2)}</span> },
  { head: '读法', w: '120px', render: r => <Tag tone={r.tone}>{r.tone === 'danger' ? '反转放大' : r.tone === 'ok' ? '延续' : '反弹'}</Tag> },
]

const h567Cols: Col<H567Row>[] = [
  { head: '假设', w: '110px', render: r => <span className="font-mono text-[12px] text-foreground">{r.id}</span> },
  { head: '预注册预测', w: '34%', render: r => <span className="text-[12px] text-foreground">{r.claim}</span> },
  { head: '实测', render: r => <span className="text-[12px] text-secondary">{r.actual}</span> },
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

const dimensionCols: Col<DimensionRow>[] = [
  { head: '维度', w: '76px', render: r => <span className="text-[13px] font-medium text-foreground">{r.dim}</span> },
  { head: '状态量', render: r => <span className="text-[12px] leading-[1.6] text-muted">{r.state}</span> },
  { head: '发起量', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.event}</span> },
  { head: '为什么决定符号', w: '38%', render: r => <span className="text-[12px] leading-[1.6] text-muted">{r.why}</span> },
]

const unifyCols: Col<UnifyRow>[] = [
  { head: '#', w: '52px', render: r => <span className="font-mono text-[12px] font-medium text-foreground">{r.id}</span> },
  { head: '检验', w: '30%', render: r => <span className="text-[13px] text-foreground">{r.what}</span> },
  { head: '形式', w: '76px', render: r => <Tag tone={r.form === '发起' ? 'ok' : 'muted'}>{r.form}</Tag> },
  { head: '结果', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.result}</span> },
]

const bookCols: Col<BookRow>[] = [
  { head: '#', w: '52px', render: r => <span className="font-mono text-[12px] font-medium text-foreground">{r.id}</span> },
  { head: '规格', w: '28%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.spec}</span> },
  { head: '读数', render: r => <span className="font-mono text-[12px] leading-[1.6] tabular-nums text-secondary">{r.read}</span> },
  { head: '裁决', w: '100px', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
]

const gapCols: Col<BookGap>[] = [
  { head: '#', w: '44px', render: r => <span className="font-mono text-[12px] font-medium text-foreground">{r.id}</span> },
  { head: '缺口', w: '26%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.gap}</span> },
  { head: '影响', render: r => <span className="text-[12px] leading-[1.6] text-muted">{r.impact}</span> },
  { head: 'v2 处理', w: '27%', render: r => <span className="font-mono text-[11px] leading-[1.6] text-secondary">{r.fix}</span> },
  { head: '状态', w: '64px', render: r => <Tag tone={r.status === '已修' ? 'ok' : r.status === '部分' ? 'warn' : 'danger'}>{r.status}</Tag> },
]

const v2Cols: Col<BookV2>[] = [
  { head: '变体', w: '58px', render: r => <span className="font-mono text-[12px] font-medium text-foreground">{r.variant}</span> },
  { head: '修了什么', w: '36%', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.fix}</span> },
  { head: 'near_share', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.near}</span> },
  { head: 'wdist', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.wdist}</span> },
  { head: '裁决', w: '72px', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
]

const transferCols: Col<TransferRow>[] = [
  { head: '数据', w: '18%', render: r => <span className="text-[13px] font-medium text-foreground">{r.data}</span> },
  { head: '存量（应≈0）', render: r => <span className="text-[12px] leading-[1.6] text-muted">{r.state}</span> },
  { head: '发起（应有符号）', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.event}</span> },
  { head: '符号', w: '150px', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.sign}</span> },
  { head: '就绪度', w: '132px', render: r => <span className="text-[11px] leading-[1.6] text-muted">{r.ready}</span> },
]

const transferResCols: Col<TransferResult>[] = [
  { head: '首测', w: '28%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.test}</span> },
  { head: '读数', render: r => <span className="font-mono text-[11px] leading-[1.6] tabular-nums text-secondary">{r.read}</span> },
  { head: '裁决', w: '132px', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
]

const g4Cols: Col<G4Row>[] = [
  { head: '补多久', w: '120px', render: r => <span className="font-mono text-[12px] text-foreground">{r.horizon}</span> },
  { head: 'near_share', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.near}</span> },
  { head: 'wdist', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.wdist}</span> },
  { head: '裁决', w: '76px', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
]

const v3Cols: Col<BookV3>[] = [
  { head: '口径', w: '64px', render: r => <span className="font-mono text-[12px] font-medium text-foreground">{r.tag}</span> },
  { head: '公告覆盖', w: '84px', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.cover}</span> },
  { head: '求和规则', w: '25%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.rule}</span> },
  { head: 'near_share', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.near}</span> },
  { head: 'wdist', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.wdist}</span> },
  { head: 'wdist Q5−Q1', w: '132px', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.spread}</span> },
  { head: '裁决', w: '108px', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
]

const coverCols: Col<CoverRow>[] = [
  { head: '日期', w: '84px', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.date}</span> },
  { head: 'raw', w: '74px', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.raw}</span> },
  { head: 'dedup', w: '74px', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.dedup}</span> },
  { head: 'snap', w: '74px', render: r => <span className="font-mono text-[11px] tabular-nums text-foreground">{r.snap}</span> },
  { head: '匹配 / 中登有质押', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.matched}</span> },
  { head: '', w: '56px', render: r => <Tag tone={r.tone}>{r.tone === 'ok' ? '近 1' : r.tone === 'warn' ? '过半' : '不足半'}</Tag> },
]

const typeSplitCols: Col<TypeSplitRow>[] = [  { head: '格', w: '31%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.cell}</span> },
  { head: '事件数', w: '76px', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.events}</span> },
  { head: 'f60 − other', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.diff}</span> },
  { head: 't', w: '62px', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.t}</span> },
  { head: '裁决', w: '104px', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
]

const typeBigCols: Col<TypeBigRow>[] = [
  { head: '规格', w: '28%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.spec}</span> },
  { head: '机构专用卖方', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.inst}</span> },
  { head: '营业部卖方', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.broker}</span> },
  { head: 'N', w: '100px', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.n}</span> },
  { head: '裁决', w: '100px', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
]

const mechanismCols: Col<MechanismRow>[] = [
  { head: '桶', w: '18%', render: r => <span className="text-[12px] font-medium text-foreground">{r.bucket}</span> },
  { head: 'inst 事件', w: '92px', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.inst}</span> },
  { head: 'broker 事件', w: '100px', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.broker}</span> },
  { head: 'inst f60', w: '92px', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.f60}</span> },
  { head: '读法', render: r => <span className="text-[11px] leading-[1.6] text-muted">{r.read}</span> },
]

const mechInterCols: Col<MechInterRow>[] = [
  { head: '检验', w: '30%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.test}</span> },
  { head: '读数', render: r => <span className="font-mono text-[11px] leading-[1.6] tabular-nums text-secondary">{r.read}</span> },
  { head: '裁决', w: '94px', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
]

const contrastCols: Col<ContrastRow>[] = [
  { head: '组', w: '62px', render: r => <Tag tone={r.group === 'event' ? 'danger' : 'muted'}>{r.group === 'event' ? '事件' : '存量'}</Tag> },
  { head: '读数', w: '30%', render: r => <span className="text-[12px] leading-[1.6] text-foreground">{r.label}<span className="ml-2 text-[11px] text-muted">{r.note}</span></span> },
  { head: 'β', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{fmtSigned(r.beta, 1)}bp</span> },
  { head: 't', w: '62px', render: r => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.t.toFixed(2)}</span> },
  { head: '95% 近似 CI', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{ciText(r)}</span> },
]

function ciText(r: ContrastRow): string {
  const se = Math.abs(r.t) > 1e-9 ? Math.abs(r.beta / r.t) : 0
  const lo = r.beta - 1.96 * se
  const hi = r.beta + 1.96 * se
  return `[${lo.toFixed(1)}, ${hi.toFixed(1)}]`
}

const epredCols: Col<EPred>[] = [
  { head: '#', w: '44px', render: r => <span className="font-mono text-[12px] font-medium text-foreground">{r.id}</span> },
  { head: '预测', w: '46%', render: r => <span className="text-[13px] leading-[1.6] text-foreground">{r.claim}</span> },
  { head: '判据', render: r => <span className="font-mono text-[12px] leading-[1.6] text-secondary">{r.test}</span> },
]

/**
 * 四口径的压力斜率图。
 *
 * 这张图只做一件事：把「高度不同、斜率同向」画出来。
 * 高度 = 四种实现对同一个词给出的水平（+141 → −54），斜率 = 低压力 → 高压力。
 * 如果读者只记住一个形状，应该是四条线一起向下——而不是任何一条线的水平。
 */
const FOUR_TONES = ['text-foreground', 'text-accent', 'text-warning', 'text-danger'] as const

function FourDefFigure() {
  const A = { y0: 214, y1: 34, min: -130, max: 220 }
  const y = (v: number) => A.y0 - ((v - A.min) / (A.max - A.min)) * (A.y0 - A.y1)
  const x0 = 176
  const x1 = 468
  return (
    <figure className="mt-4" data-testid="four-def-figure">
      <div className="-mx-1 overflow-x-auto">
        <svg viewBox="0 0 720 272" className="min-w-[620px] w-full" role="img" aria-label="四种贴线口径的压力斜率">
          <line x1={64} x2={706} y1={y(0)} y2={y(0)} stroke="currentColor" strokeDasharray="3 3" className="text-muted opacity-40" />
          {[-100, 0, 100, 200].map(v => (
            <text key={v} x={56} y={y(v) + 3} textAnchor="end" className="fill-current text-[9.5px] text-muted">{v}</text>
          ))}
          <text x={x0} y={246} textAnchor="middle" className="fill-current text-[10px] text-muted">低压力 · 6.2%</text>
          <text x={x1} y={246} textAnchor="middle" className="fill-current text-[10px] text-muted">高压力 · 14.3%</text>
          {FOUR_DEFS.map((r, i) => (
            <g key={r.def} className={FOUR_TONES[i]}>
              <line x1={x0} x2={x1} y1={y(r.low)} y2={y(r.high)} stroke="currentColor" strokeWidth={1.8} />
              <circle cx={x0} cy={y(r.low)} r={3} fill="currentColor" />
              <circle cx={x1} cy={y(r.high)} r={3} fill="currentColor" />
            </g>
          ))}
          <text x={540} y={54} className="fill-current text-[10.5px] text-muted">四条线的高度差 195bp</text>
          <text x={540} y={70} className="fill-current text-[10.5px] text-muted">四条线的斜率同向 4/4</text>
        </svg>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
        {FOUR_DEFS.map((r, i) => (
          <span key={r.def} className="inline-flex items-center gap-1.5 text-[10.5px] text-muted">
            <span className={cn('h-0 w-4 border-t-2 border-current', FOUR_TONES[i])} />
            {r.def}
            <span className="font-mono tabular-nums text-muted/70">{fmtSigned(r.bp, 0)}</span>
          </span>
        ))}
      </div>
      <figcaption className="mt-1.5 max-w-[46em] text-[11px] leading-[1.7] text-muted">
        纵轴 = 60 日超额（bp）。四条线是同一个概念的四种实现，对照组、匹配向量、样本月份完全相同，
        只换 near 的定义。图不是新检验，是把 §6A 的表画出来：可识别的是斜率，不是高度。
      </figcaption>
    </figure>
  )
}

function ContrastFigure() {
  const A = { min: -150, max: 150, x0: 160, x1: 380 }
  const B = { min: -100, max: 700, x0: 450, x1: 700 }
  const px = (v: number, p: typeof A) => p.x0 + ((v - p.min) / (p.max - p.min)) * (p.x1 - p.x0)
  const state = CONTRAST.filter(r => r.group === 'state')
  const event = CONTRAST.filter(r => r.group === 'event')
  const yA = 64
  const yB = 92
  const dy = 34
  const bar = (r: ContrastRow, p: typeof A, y: number, tone: string, labelX: number) => {
    const se = Math.abs(r.t) > 1e-9 ? Math.abs(r.beta / r.t) : 0
    const lo = Math.max(p.min, r.beta - 1.96 * se)
    const hi = Math.min(p.max, r.beta + 1.96 * se)
    const x = px(r.beta, p)
    return (
      <g key={r.label} className={tone}>
        <text x={labelX} y={y + 3.5} textAnchor="end" className="fill-current text-[10.5px]">{r.label}</text>
        <line x1={px(lo, p)} x2={px(hi, p)} y1={y} y2={y} stroke="currentColor" strokeWidth={1.2} />
        <line x1={px(lo, p)} x2={px(lo, p)} y1={y - 3} y2={y + 3} stroke="currentColor" strokeWidth={1.2} />
        <line x1={px(hi, p)} x2={px(hi, p)} y1={y - 3} y2={y + 3} stroke="currentColor" strokeWidth={1.2} />
        <circle cx={x} cy={y} r={3} fill="currentColor" />
      </g>
    )
  }
  return (
    <figure className="mt-4" data-testid="contrast-figure">
      <div className="-mx-1 overflow-x-auto">
        <svg viewBox="0 0 720 270" className="min-w-[640px] w-full" role="img" aria-label="存量零与事件负的对比图">
        <text x={A.x0} y={26} className="fill-current text-[11px] font-medium text-muted">存量：受控 β（bp / +1sd）</text>
        <text x={B.x0} y={26} className="fill-current text-[11px] font-medium text-muted">事件：near−far（bp）</text>
        <line x1={px(0, A)} x2={px(0, A)} y1={44} y2={190} stroke="currentColor" strokeDasharray="3 3" className="text-muted" />
        <line x1={px(0, B)} x2={px(0, B)} y1={44} y2={190} stroke="currentColor" strokeDasharray="3 3" className="text-muted" />
        {state.map((r, i) => bar(r, A, yA + i * dy, 'text-muted', 150))}
        {event.map((r, i) => bar(r, B, yB + i * dy, 'text-foreground', 440))}
        {[A.min, 0, A.max].map(v => (
          <g key={`a${v}`} className="text-muted">
            <line x1={px(v, A)} x2={px(v, A)} y1={190} y2={194} stroke="currentColor" />
            <text x={px(v, A)} y={206} textAnchor="middle" className="fill-current text-[9.5px]">{v}</text>
          </g>
        ))}
        {[0, 300, 600].map(v => (
          <g key={`b${v}`} className="text-muted">
            <line x1={px(v, B)} x2={px(v, B)} y1={190} y2={194} stroke="currentColor" />
            <text x={px(v, B)} y={206} textAnchor="middle" className="fill-current text-[9.5px]">{v}</text>
          </g>
        ))}
        <g className="text-muted">
          <circle cx={A.x0 + 4} cy={230} r={3} fill="currentColor" />
          <text x={A.x0 + 13} y={233} className="fill-current text-[10px]">存量：受控 β，应≈0</text>
          <circle cx={A.x0 + 166} cy={230} r={3} className="text-foreground" fill="currentColor" />
          <text x={A.x0 + 175} y={233} className="fill-current text-[10px]">事件：raw near−far，应正</text>
        </g>
        <text x={A.x0} y={258} className="fill-current text-[10px] text-muted">
          误差棒 = |β/t| × 1.96；两栏刻度不同，不可跨栏比大小。
        </text>
      </svg>
      </div>
      <div className="mt-1 text-[11px] text-muted sm:hidden">左右滑动查看完整图 →</div>
      <figcaption className="mt-1.5 max-w-[46em] text-[11px] leading-[1.7] text-muted">
        左：存量四行全部跨 0（零）；右：事件两行全部在 0 右侧（正）。这张图不是新检验，
        只是把 D/D2 与修正后 H11 的读数放在同一张图上；它要说明的是「零 vs 正」，不是「负 vs 正」。
      </figcaption>
    </figure>
  )
}

/**
 * 卷内目录：吸顶两层。上层四卷（带节数，点它进卷首），下层是**当前卷**的节。
 *
 * 上一版是三段手写的按钮数组，问题不是丑，是两份定义：§0 审计没有入口，
 * 而测试查的是渲染，不是目录——所以缺了也绿。现在目录由 VOLUMES 生成。
 * 下层只铺当前卷：38 个节全铺开时，目录本身就成了第二篇正文。
 */
function useActiveSection(ids: string[]) {
  const [active, setActive] = useState(ids[0] ?? '')
  const key = ids.join('|')
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return
    const obs = new IntersectionObserver(
      entries => {
        const visible = entries
          .filter(e => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)
        if (visible[0]?.target.id) setActive(visible[0].target.id)
      },
      { rootMargin: '-96px 0px -62% 0px', threshold: 0 },
    )
    for (const id of key.split('|')) {
      const el = document.getElementById(id)
      if (el) obs.observe(el)
    }
    return () => obs.disconnect()
  }, [key])
  return active
}

function VolumeNav({ activeId, onJump }: { activeId: string; onJump: (id: string) => void }) {
  const activeKey = SECTION_VOLUME[activeId] ?? VOLUMES[0].key
  const cur = VOLUMES.find(v => v.key === activeKey) ?? VOLUMES[0]
  return (
    <nav
      aria-label="章节"
      data-testid="volume-nav"
      className="sticky top-0 z-20 -mx-6 mt-5 border-b border-border bg-base/95 px-6 pb-2 pt-2 backdrop-blur supports-[backdrop-filter]:bg-base/85"
    >
      <div className="flex items-center gap-x-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        <span className="w-[40px] shrink-0 text-[10px] tracking-[0.16em] text-muted">卷</span>
        {VOLUMES.map(v => {
          const on = v.key === activeKey
          return (
            <button
              key={v.key}
              type="button"
              onClick={() => onJump(v.sections[0].id)}
              aria-current={on ? 'true' : undefined}
              className={cn(
                'inline-flex shrink-0 items-baseline gap-1.5 whitespace-nowrap rounded px-2 py-1 text-[12px] transition-colors hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                on ? 'bg-elevated text-foreground' : 'text-muted hover:text-foreground',
              )}
            >
              {v.tab}
              <span className="font-mono text-[10px] tabular-nums text-muted/70">{v.sections.length}</span>
            </button>
          )
        })}
      </div>
      <div className="mt-1 flex items-center gap-x-0.5 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        <span className="w-[40px] shrink-0 text-[10px] tracking-[0.16em] text-muted/60">节</span>
        {cur.sections.map(s => {
          const on = s.id === activeId
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => onJump(s.id)}
              aria-current={on ? 'true' : undefined}
              className={cn(
                'shrink-0 whitespace-nowrap rounded px-1.5 py-0.5 text-[11.5px] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
                on ? 'bg-accent/15 text-accent' : 'text-muted hover:text-foreground',
              )}
            >
              {s.label}
            </button>
          )
        })}
      </div>
    </nav>
  )
}

/**
 * 「口径已改」标记：给那些 2026-11-27 被重算过的节。
 *
 * 起因是一个具体的误读风险：读者记得 H11 是 −474bp / t −5.41（旧稿的结论），
 * 而页面现在写 +525bp / t 7.47。§0 说了这件事，但读者滚到 §13 时不会记得。
 * 标记不做判断，只陈述「这一节的数字变了，原因见 §0」——它是警告色，不是作废色，
 * 因为问题已经修好，剩下的只是「别拿旧记忆读新数」。
 */
function RevisedTag({ onJump }: { onJump: () => void }) {
  return (
    <button
      type="button"
      onClick={onJump}
      title="2026-11-27 重算：旧读数用了提取日价格，端点含公告日之后的行情。旧值 / 新值对照见 §0"
      className="inline-flex items-center gap-1 rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-[10px] font-medium tracking-[0.06em] text-warning transition-colors hover:bg-warning/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
    >
      口径已改 · 见 §0
    </button>
  )
}

/** 卷的折叠锚点 id。导航跳转与深链都靠它找到 <details>。 */
const volDomId = (key: VolKey) => `vol-${key}`

/** 展开某一卷。`<details>` 的 open 由 DOM 自己拥有，这里只是替导航与深链把它推开。 */
function openVolume(key: VolKey) {
  const el = document.getElementById(volDomId(key)) as HTMLDetailsElement | null
  if (el && !el.open) el.open = true
}

/**
 * 一卷 = `<details>` 折叠头 + 节。与 structural-edge 的 Fold 同一套机制，不新造第二种。
 *
 * 为什么是 <details> 而不是 hidden：
 *   1) 折叠区在关闭时仍留在 DOM，测试与脚本可读（和 hidden 一样）；
 *   2) 但浏览器页内查找会展开被折叠的内容（Chrome 会），display:none 的内容一律搜不到——
 *      研究档案页最常用的动作就是 Ctrl-F 找一个指标名；
 *   3) 键盘与读屏是原生行为，不需要自己维护 aria-expanded / aria-controls。
 *
 * open 状态不交给 React：受控的话每次父组件重渲染（滚动高亮每滚一节就 setState 一次）
 * 都会把 open 属性写回初值，把读者刚展开的卷弹回去。这里只在挂载时写一次初值，
 * 之后的开关由浏览器拥有，导航要用时走 openVolume() 直接推。
 */
function VolumeBlock({
  def, first, children,
}: {
  def: VolumeDef
  /** 紧跟摘要区的第一卷：上面已经有一条发丝线，不再叠一条 2px 的卷分隔。 */
  first?: boolean
  children: ReactNode
}) {
  const ref = useRef<HTMLDetailsElement>(null)
  useLayoutEffect(() => {
    if (ref.current) ref.current.open = def.openByDefault
  }, [def.openByDefault])

  return (
    <details
      ref={ref}
      id={volDomId(def.key)}
      data-testid={`volume-${def.key}`}
      className={cn('group scroll-mt-24', first ? 'mt-7' : 'mt-16 border-t-2 border-border pt-4')}
    >
      <summary className="flex cursor-pointer select-none list-none flex-wrap items-start justify-between gap-x-5 gap-y-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 [&::-webkit-details-marker]:hidden">
        <div className="min-w-0">
          <div className="text-[11px] font-medium tracking-[0.18em] text-muted">{def.title}</div>
          <p className="mt-1.5 max-w-[46em] text-[12px] leading-[1.7] text-muted">{def.digest}</p>
        </div>
        {/* 文案由 CSS 切换：DOM 里两份都在，React 不需要知道现在是开还是关。 */}
        <span className="inline-flex shrink-0 items-center gap-1 rounded border border-border px-2 py-1 text-[11px] text-secondary">
          <ChevronRight className="h-3 w-3 transition-transform duration-150 group-open:rotate-90" />
          <span className="group-open:hidden">展开 · {def.sections.length} 节</span>
          <span className="hidden group-open:inline">收起本卷</span>
        </span>
      </summary>
      {children}
    </details>
  )
}

export function ChipTheory() {
  const allIds = useMemo(() => VOLUMES.flatMap(v => v.sections.map(s => s.id)), [])
  const activeId = useActiveSection(allIds)

  /**
   * 跳转：锚点在收起的卷里时，先把它推开再滚。
   *
   * 展开是同步的（直接写 `<details>.open`），但仍留一帧再滚：浏览器要重新布局完，
   * scrollIntoView 才拿得到真实 rect。Shell.tsx 的深链跳转用的是同一个写法。
   */
  const go = useCallback((id: string) => {
    const vol = SECTION_VOLUME[id]
    if (vol) openVolume(vol)
    window.setTimeout(() => scrollToId(id), 80)
  }, [])

  // 深链 #h3 / #book 进来时，对应卷要先展开，否则锚点落在一段 0 高度的 DOM 上。
  useEffect(() => {
    const onHash = () => {
      const id = window.location.hash.replace(/^#/, '')
      if (id && SECTION_VOLUME[id]) openVolume(SECTION_VOLUME[id])
    }
    onHash()
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const vol = (key: VolKey) => VOLUMES.find(v => v.key === key) as VolumeDef

  return (
    <div className="mx-auto max-w-[1180px] px-6 pb-24 pt-8">
      <header className="flex flex-col gap-6 border-b border-border pb-7 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <div className="text-[11px] font-medium tracking-[0.18em] text-muted">
            研究结论 · 筹码 · 收敛版（2026-11-28）
          </div>
          <h1 className="mt-3 text-[30px] font-semibold leading-[1.15] tracking-[-0.02em] text-foreground">
            筹码<span className="mx-2.5 font-light text-muted/40">/</span>一张体检表，不是一个信号
          </h1>
          <p className="mt-3 max-w-[46em] text-[14px] leading-[1.8] text-secondary">
            前四卷在持仓表（资产端）上找到的十六个读数里，十五条没有稳定的收益符号。卷五把唯一有余额的 H11
            重读成应付表（负债端），只活下来一条相对值：贴线组未来 60 日比「同日、同跌幅、同波动/换手/规模」的同特征基准高
            <span className="text-foreground"> +291bp</span>（t 5.28，6/7 年为正）。
            它是候选，不是买点——纸面 holdout 过线（+293.7bp / t 1.80），但只在 0.5% 成本下；0.8% 即失守。
            真前瞻 2026-11-30 → 2027-06-30，未裁决。
            卷六再往下拆一层：连「贴线」这个词都没有唯一实现——四种簿口径的 60 日超额从 +141bp 到 −54bp；
            能留下的是条件（低压力 × 少合约），不是水平。
          </p>
        </div>
        <div className="shrink-0 text-[11px] leading-[1.9] text-muted lg:text-right">
          <div className="font-mono tabular-nums">数据 {META.asOf}</div>
          <div>{META.sample}</div>
          <div className="text-warning">一个候选 · 其余排除 · 0 可执行</div>
        </div>
      </header>

      <VolumeNav activeId={activeId} onJump={go} />

      {/* 结论带：整页只回答一个问题——现在能不能用。答案在左边，代价在右边。 */}
      <div className="mt-6 border-y border-border py-5">
        <div className="flex flex-wrap items-start justify-between gap-x-10 gap-y-5">
          <div className="min-w-0 max-w-[52em]">
            <Tag tone={VERDICT.tone}>{VERDICT.tag}</Tag>
            <p className="mt-2.5 text-[18px] font-medium leading-[1.45] tracking-[-0.01em] text-foreground">
              {VERDICT.line}
            </p>
            <p className="mt-2 max-w-[48em] text-[13px] leading-[1.75] text-secondary">{VERDICT.body}</p>
          </div>
          <div className="shrink-0 border-l border-border pl-4 lg:pl-6">
            <div className="text-[11px] tracking-[0.12em] text-muted">当前可执行动作</div>
            <div className="mt-1 font-mono text-[32px] font-medium leading-none tabular-nums text-foreground">
              {VERDICT.actionValue}
            </div>
            <p className="mt-2 max-w-[15em] text-[11px] leading-[1.7] text-muted">{VERDICT.actionNote}</p>
          </div>
        </div>
      </div>

      <div className="mt-6 grid gap-6 border-b border-border pb-6 sm:grid-cols-2 lg:grid-cols-4">
        {SUMMARY.map(s => (
          <div key={s.label} className="block">
            <div className="text-[11px] leading-4 tracking-[0.12em] text-muted">{s.label}</div>
            <div className="mt-1.5">
              <Tag tone={s.verdict}>{s.value}</Tag>
            </div>
            <p className="mt-1.5 max-w-[24em] text-[12px] leading-[1.7] text-secondary">{s.note}</p>
          </div>
        ))}
      </div>

      {/* 能/不能：把「体检表」与「信号」分开，比任何免责声明都硬。 */}
      <div className="mt-6 grid gap-6 border-b border-border pb-6 sm:grid-cols-2">
        <div>
          <div className="text-[11px] font-medium tracking-[0.14em] text-muted">本页能回答</div>
          <ul className="mt-2 space-y-1.5">
            {SCOPE.can.map(t => (
              <li key={t} className="flex gap-2 text-[12px] leading-[1.6] text-secondary">
                <span aria-hidden className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-accent" />
                <span>{t}</span>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <div className="text-[11px] font-medium tracking-[0.14em] text-muted">本页不能回答</div>
          <ul className="mt-2 space-y-1.5">
            {SCOPE.cannot.map(t => (
              <li key={t} className="flex gap-2 text-[12px] leading-[1.6] text-muted">
                <span aria-hidden className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-danger/70" />
                <span>{t}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>

      {/* 证据分级：只有六级，读一遍就知道哪些能拿去做决定。 */}
      <div className="mt-6" data-testid="evidence">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 className="text-[13px] font-medium text-foreground">证据分级</h2>
          <span className="font-mono text-[10.5px] text-muted">A 可复算 · B 探索性 · C 不可识别</span>
        </div>
        <DataTable
          cols={evidenceCols}
          rows={EVIDENCE}
          rowKey={r => r.claim}
          dense
          minWidth={820}
          reason="分级的标准不是 p 值，而是「换一批数据/换一个对照，结论还在不在」：A 有独立复算，B 只有同批数据，C 连对照都找不到。"
        />
      </div>

      <VolumeBlock def={vol('audit')} first>
      <Section
        id="audit"
        no="0"
        title="审计：唯一活口是未来函数"
        lead={`CLOSE_FORWARD_ADJPRICE_TODAY 被当成公告日价，实际是提取日（${AUDIT_META.date.slice(0, 7)}）的最新价。于是 drift = 最新价 / 质押日价 − 1，ratio = 平仓线 / 最新价——两个变量都包含公告日之后的全部行情。H11 的 −474bp、H12 的 t 5.06、预注册的 +426/+915bp、卷五首测的 889→305，全部作废。`}
        source="data/.scratch/leverage/pledge_drift_events.parquet · 字段核对 · 20,540 事件 / 80 月"
        first
        className="mt-10"
      >
        <DataTable
          cols={auditProofCols}
          rows={AUDIT_PROOF}
          rowKey={r => r.test}
          dense
          minWidth={720}
          reason="前两条是量级：旧 drift 与「最新价/起点」差 0.019，与「公告日/起点」差 0.269——27 个百分点。后三条是单票对照：同一只股票在三个不同公告日的 CLOSE_PRICE 完全相同。"
        />
        <DataTable
          cols={auditFixCols}
          rows={AUDIT_FIX}
          rowKey={r => r.test}
          dense
          minWidth={820}
          reason="同一批事件、同一套月聚簇口径，只把端点从提取日改成公告日。/sd = 每 +1 个横截面标准差；bucket 差 = 桶内均值差。"
          swipeHint
        />
        <div className="mt-3 flex flex-wrap items-baseline gap-x-4 gap-y-1 border-t border-border pt-3 font-mono text-[11px] tabular-nums">
          <span className="text-muted">贴线八桶 · f60（bp）：</span>
          {AUDIT_BUCKETS.map(b => (
            <span key={b.bucket}>
              <span className="text-muted">{b.bucket}</span>{' '}
              <span className={b.f60 < 0 ? 'text-danger' : 'text-foreground'}>{b.f60 > 0 ? '+' : ''}{b.f60.toFixed(0)}</span>
            </span>
          ))}
        </div>
        <Callout tone="danger" label="作废范围">
          {AUDIT_SCOPE.invalid}
          <br />
          <span className="text-muted">不受影响：{AUDIT_SCOPE.valid}</span>
        </Callout>
        <Callout tone="warn" label="修正后的读法">
          {AUDIT_READ}
        </Callout>
        <Callout tone="muted" label="为什么旧 P1 跑不了">
          锚定安慰剂要求信号在事件日可观测。旧 drift 的终点是提取日，它在事件日根本不存在——
          P1 检验的是一个未来函数，跑出来的任何分位数都没有意义。换成 drift_c（公告日 / 质押日 − 1）之后，
          P1 才成立；但它要检验的已经不是「未付账单」，而是「同一段跌幅，锚在合约上为什么比锚在固定窗口上更强」。
          修正版 P1 已跑，见 §5D。
        </Callout>
      </Section>
      </VolumeBlock>

      <VolumeBlock def={vol('v5')}>
      <Section
        id="ledger"
        no="5A"
        title="换一张表：筹码是负债，不是资产"
        lead="同一只股票有两张表。持仓表记录谁在什么价格拿着，按市价重估；应付表记录谁欠谁多少、什么时候必须还，按面值挂着。前四卷一直在第一张表上找信号，而价差只在第二张表上出现——因为面值不随市价动。"
        source="docs/structural-edge/chip-ap.md · 卷五理论稿"
        first
        className="mt-10"
      >
        <Callout tone="warn" label="本卷已按修正版重写（2026-11-27）">
          结构没变：两张表、盯市频率、余额。变的是符号——替这张账单付钱的是<span className="text-foreground">救助</span>，
          不是清算；所以贴线不是跑输，是跑赢（长腿对同特征基准 +291bp；多空 +525bp）。审计证据见 §0。
        </Callout>
        <DataTable
          cols={ledgerCols}
          rows={AP_LEDGER}
          rowKey={r => r.dim}
          minWidth={760}
          reason="这不是类比。持仓是资产（按市价），质押是负债（按面值）；资产端没有未定价的量，负债端有。"
        />
        <Callout tone="danger" label="一句话">
          资产按市价卖，负债按面值还。alpha 在面值与市价的差里；筹码图只画了资产端。
        </Callout>
        <Callout tone="warn" label="为什么前四卷的零是默认值">
          分布、年龄、密度、网络、伽马，都是过去成交在价格轴上的投影。过去成交按当时的市价完成，
          不留下按面值计价的欠款——它可以有信息（知情交易），但没有 gap；而 H1–H9 的零说明，
          这点信息已经被换手/波动/规模吸收。同一只股票，<span className="text-foreground">价格路径可以完全相同，
          而应付表完全不同</span>；价格路径相同、账单不同，才是可定价的差。
        </Callout>
        <Callout tone="muted" label="卷四已经摸到了，但读成了信号">
          H11 的平仓线是负债端的第一个对象。卷四把它读成「谁刚发起」——一个信号，问的是发送者；
          卷五读成「还欠多少」——一笔余额，问的是金额。同一个事件，两种坐标；
          这也解释了卷四的杀死条件为什么围绕「测量、动量、发送者、迁移」，而卷五的第一条围绕「锚定与支付」。
        </Callout>
      </Section>

      <Section
        id="mtm"
        no="5B"
        title="一条定律：盯市频率"
        lead="一个约束是否产生可定价的漂移，只取决于它多久被重估一次。逐日盯市的账户，gap 每天结清；事件盯市的合约，gap 累积到处置日；从不盯市的「成本带」，根本不是账单。"
        source="H9 融资余额 · H11 质押贴线 · H3 成本带穿越 · F 大宗迁移"
      >
        <Tiles cols={3}>
          {AP_MTM.map(m => (
            <div key={m.kind} data-testid="ap-mtm-card">
              <Tile
                lead={`${m.kind} · ${m.freq}`}
                aside={<Tag tone={m.tone}>{m.tone === 'ok' ? '有 gap' : m.tone === 'muted' ? '无 gap' : '无账单'}</Tag>}
                title={m.who}
                body={m.evidence}
              />
            </div>
          ))}
        </Tiles>
        <Callout tone="danger" label="定律">
          <span className="font-mono text-[12px] text-foreground">
            drift ≈ (面值 − 市价) / 流通市值 × 解除概率(账龄, 质押比例) × 解除方向
          </span>
          <br />
          盯市频率决定 gap 存不存在；解除方式决定符号；账龄与质押比例决定解除概率。
          A 股的解除以救助为主，所以符号为正。
        </Callout>
        <Callout tone="danger" label="H9 与 H11 是同一件事">
          融资不预测、质押预测，不是「杠杆有效 / 无效」，是同一笔杠杆被盯市的频率不同：
          两融每天追保，质押只在平仓线才动。<span className="text-foreground">定价的不是杠杆，是没被追保的那部分杠杆。</span>
          这也解释了 H9 的 ρ_D −0.442：融资余额不是杠杆变量，是换手变量的第 9 次复读。
        </Callout>
        <Callout tone="warn" label="对「雪崩」的重新解释">
          如果账单的解除方式是救助，就不会有强制卖压。H3（下穿反弹）、H8（放量对称）、
          H12（修正后是反弹而非阴跌）三处独立读数，指向同一件事：<span className="text-foreground">A 股的约束不是被强平解决的，
          是被救助/展期/重组解决的</span>。所以「雪崩」在价格量里找不到——它被救助替代了。
        </Callout>
      </Section>

      <Section
        id="attr"
        no="5C"
        title="十六个读数，七种状态，一个恒等式"
        lead="把前四卷的十六个结果按「有没有账单」重排：七种状态，零个新参数。压缩率本身就是证据——如果每一种都要单独解释，那就不是理论，是目录。"
        source="chip-duration.md · chip-origination.md · 本页 D–H 节"
      >
        <DataTable
          cols={attrCols}
          rows={AP_ATTR}
          rowKey={r => r.cls}
          dense
          minWidth={860}
          reason="H9（融资/质押）与 H12（折算/涨幅）各拆两半，大宗在「已支付」与「制度」两处各用一次读数；所以七种状态不是十六行的机械分组，是七种「有没有账单、付没付」的位置。"
          swipeHint
        />
        <Callout tone="danger" label="读法">
          前四类（分布、周转、身份、位置）是资产端的四种画法，全部零或半活；第五类是唯一有余额的账单，半活（候选）；
          第六类「支付后是否结清」没测出来——C2 只有 5 只/月且内生，<span className="text-foreground">不是零，是不可识别</span>；
          第七类制度变了：清算技术缺失时为负，救助技术普及后归零——符号由解除方式给，余额还在。
          <span className="text-foreground">这不是十六次失败，是一次计价错误：所有人都在资产端做技术分析，而价差在负债端。</span>
        </Callout>
      </Section>

      <Section
        id="probe"
        no="5D"
        title="首测：只有余额是主效应，账龄/质押只改变斜率"
        lead={`把 H11 的事件样本拆成三组：合约年龄（位置）、累计质押比例（能力）、质押以来涨跌（余额）。注意表里的 Q2−Q0 是「各分组内的跌幅斜率」，不是账龄/质押比例的主效应——它回答的是「跌幅在哪个分组里更陡」，不是「老合约本身是否更强」。总表 ${AP_META.events.toLocaleString()} 个事件；回归样本 ${AP_META.eventsValid.toLocaleString()}（缺失 260），${AP_META.months} 个月，月聚簇口径。`}
        source="analysis/_diag_chip_audit_fix.py · _diag_chip_p1_anchor.py · _diag_chip_h13e_fullresid.py"
        actions={<RevisedTag onJump={() => go('audit')} />}
      >
        <StatGrid
          cols={4}
          items={[
            { k: '余额 · 深跌桶', v: '+755.5bp', d: 't 4.42；>+10% 桶 −72.0 / t −0.38' },
            { k: '同特征基准 · 长腿', v: '+290.6bp', d: 't 5.28；同日同跌幅 + 波动/换手/规模' },
            { k: '分组内跌幅斜率', v: '−401 → −716', d: '账龄 <0.5→>2 年 −401→−716bp；质押 Q1→Q5 −492→−731bp' },
            { k: '锚定残差', v: '−146.9bp', d: 't −3.25 · 通用锚只解释 48.3%' },
          ]}
        />
        <APFixFigure />
        <DataTable
          cols={[
            { head: '分组', w: '60px', render: (r: { axis: string; bucket: string; n: number; read: number; t: number; unit: string }) => <span className="text-[12px] text-foreground">{r.axis}</span> },
            { head: '桶', w: '96px', render: (r: { axis: string; bucket: string; n: number; read: number; t: number; unit: string }) => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.bucket}</span> },
            { head: 'n', align: 'right', render: (r: { axis: string; bucket: string; n: number; read: number; t: number; unit: string }) => <span className="font-mono text-[11px] tabular-nums text-muted">{r.n.toLocaleString()}</span> },
            { head: '分组内跌幅斜率', align: 'right', render: (r: { axis: string; bucket: string; n: number; read: number; t: number; unit: string }) => <span className="font-mono text-[11px] tabular-nums text-secondary">{fmtSigned(r.read, 1)}bp</span> },
            { head: 't', align: 'right', render: (r: { axis: string; bucket: string; n: number; read: number; t: number; unit: string }) => <span className="font-mono text-[11px] tabular-nums text-muted">{fmtT(r.t, 2)}</span> },
            { head: '口径', w: '96px', render: (r: { axis: string; bucket: string; n: number; read: number; t: number; unit: string }) => <span className="text-[11px] text-muted">{r.unit}</span> },
          ]}
          rows={[
            ...AP_FIX.age.map(r => ({ ...r, axis: '账龄', read: r.spread, unit: '分组内 Q2−Q0' })),
            ...AP_FIX.pledge.map(r => ({ ...r, axis: '质押', read: r.spread, unit: '分组内 Q2−Q0' })),
            ...AP_FIX.balance.map(r => ({ ...r, axis: '余额', read: r.mean, unit: '桶内均值' })),
          ]}
          rowKey={(r, i) => `${r.axis}-${r.bucket}-${i}`}
          dense
          minWidth={700}
          reason="账龄与质押：桶内 drift_c 三分位 Q2−Q0（负 = 该分组里跌得深的一组反弹更多）；余额：桶内 f60 均值。月聚簇后 NW t，n=20,280。"
          swipeHint
        />
        {/* H13：把「贴线」放到同事件池的同特征基准上，回答「+525 里有多少是 beta/反转」。 */}
        <DataTable
          cols={[
            { head: '对照', w: '42%', render: (r: { test: string; read: string; t: string; note: string }) => <span className="text-[12px] leading-[1.6] text-foreground">{r.test}</span> },
            { head: '读数', align: 'right', render: (r: { test: string; read: string; t: string; note: string }) => <span className="font-mono text-[12px] tabular-nums text-secondary">{r.read}</span> },
            { head: 't', align: 'right', render: (r: { test: string; read: string; t: string; note: string }) => <span className="font-mono text-[12px] tabular-nums text-muted">{r.t}</span> },
            { head: '读法', w: '28%', render: (r: { test: string; read: string; t: string; note: string }) => <span className="text-[11px] leading-[1.6] text-muted">{r.note}</span> },
          ]}
          rows={[
            { test: 'near 绝对 f60（含 beta）', read: `+${MATCH.nearAbs.toFixed(1)}bp`, t: '—', note: '不能当 alpha' },
            { test: '同日同跌幅三次曲线残差', read: `+${MATCH.nearResidCubic.toFixed(1)}bp`, t: fmtT(MATCH.nearResidCubicT, 2), note: '不是纯反转' },
            { test: '再加波动/换手/规模', read: `+${MATCH.nearResidFull.toFixed(1)}bp`, t: fmtT(MATCH.nearResidFullT, 2), note: '长腿可用读数' },
            { test: 'far 同基准', read: `${fmtSigned(MATCH.farResidFull, 1)}bp`, t: fmtT(MATCH.farResidFullT, 2), note: '空腿也在跑输' },
            { test: '长腿 − 空腿', read: `+${MATCH.diffFull.toFixed(1)}bp`, t: '—', note: '不是 +525' },
            { test: '线性同控 ratio + drift', read: `ratio +${MATCH.linearRatio.toFixed(1)} / drift ${fmtSigned(MATCH.linearDrift, 1)}`, t: `${fmtT(MATCH.linearRatioT, 2)} / ${fmtT(MATCH.linearDriftT, 2)}`, note: '线性设定会把它压没' },
            { test: '年度（near 残差）', read: MATCH.years.map(y => `${y.year} ${y.bp > 0 ? '+' : ''}${y.bp.toFixed(0)}`).join(' · '), t: '6/7 正', note: '2021 为负' },
          ]}
          rowKey={r => r.test}
          dense
          minWidth={760}
          reason={`${MATCH.note} 对照是同日同事件池横截面回归，不是 near−far 的另一种算法。`}
          swipeHint
        />
        <Callout tone="danger" label="这不是纯反转">
          同日同事件池、同一条 ret250 三次曲线之上，near 仍 +229bp（t 3.63）；再加 ret60/波动/换手/规模，
          仍 +291bp（t 5.28）。可见「贴线」不是「跌得多」的线性影子。但也不是干净因果：
          对照是同期横截面拟合、含未来信息；未控行业、未剔 ST、未处理退市缺口，且 2021 年为负。
        </Callout>
        <Callout tone="warn" label="+525 不是你能拿到的">
          +525bp 是 near−far 的多空差；+1011bp 是 near 的绝对收益，大部分是 beta。只做多的人，
          能被记成 alpha 的只有<span className="text-foreground"> +291bp</span>——相对同特征基准，不是相对现金。
          把这三个数混着报，是这一页上一版最大的口径错误。
        </Callout>
        <Callout tone="warn" label="三条轴的正确读法">
          账龄与质押比例的表，读的是「该分组内跌幅斜率有多陡」，不是「老合约/高质押本身更强」。
          老合约组斜率更陡（−401 → −716），只能说明跌幅在这个分组里更区分收益；
          年龄与质押比例的主效应，前面的 H1–H9 已经测过，零或不稳。把交互读成主效应，是第二次误读。
        </Callout>
        <Callout tone="warn" label="P1：没有悬崖，但有残差">
          窗口整体往回挪 5 天，系数只掉 11%；挪 120 天仍 −127（t −3.3）。没有悬崖 → 合约锚不是一个锐利的点。
          但把 drift_c 对通用锚（高点 + ret20/60/120/250）正交后，残差仍有 −147 / t −3.25，通用锚只解释 48.3%。
          所以合约是<span className="text-foreground">更好的测量点，不是魔法点</span>——它把「浮亏」测得更干净，而不是创造了一个新维度。
        </Callout>
        <Callout tone="muted" label="边界">
          1）UNFREEZE_DATE 是结果不是条款（股价恢复会被提前解押），主表不用它。2）能力只有一个粗代理
          （已质押 / 总股本），换成「剩余可质押 / 流通市值」可能更强。3）全部为探索性：H11/H12 已看过全样本，
          P1 是同一批数据上的结构检验，不是样本外。4）H13 的对照未控行业、未剔 ST、未处理退市缺口，
          且是同期拟合（含未来信息），只能当归因基准，不能当可实施基准。
        </Callout>
      </Section>

      <Section
        id="pred5"
        no="5E"
        title="五条前瞻：符号、解除、支付、制度、加总"
        lead="写在下一轮跑数之前。P1 决定合约锚是不是只是通用跌幅；P2 决定符号是不是「解除方式」给的；P3 决定它是不是一张账单；P4 决定制度解释能不能成立。"
        actions={<span className="text-[11px] text-muted">预注册草案 · 未冻结</span>}
      >
        <DataTable
          cols={apPredCols}
          rows={AP_PRED}
          rowKey={r => r.id}
          minWidth={760}
          reason="阈值写在这里不是为了让理论通过，是为了让它在明年之前能被杀死。"
        />
        <DataTable
          cols={[
            { head: '层', w: '30%', render: (r: { layer: string; soe: string; person: string; diff: string }) => <span className="text-[12px] leading-[1.6] text-foreground">{r.layer}</span> },
            { head: '国资', render: (r: { layer: string; soe: string; person: string; diff: string }) => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.soe}</span> },
            { head: '民企', render: (r: { layer: string; soe: string; person: string; diff: string }) => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.person}</span> },
            { head: '差（国资 − 民企）', w: '26%', render: (r: { layer: string; soe: string; person: string; diff: string }) => <span className="font-mono text-[11px] tabular-nums text-muted">{r.diff}</span> },
          ]}
          rows={[
            {
              layer: '质押事件 · f60 spread（Q2−Q0）',
              soe: `${P2_TEST.event[0].spread > 0 ? '+' : ''}${P2_TEST.event[0].spread.toFixed(1)} · ${P2_TEST.event[0].n} 事件 / ${P2_TEST.event[0].symbols} 只 / ${P2_TEST.event[0].months} 月`,
              person: `${P2_TEST.event[1].spread.toFixed(1)} · ${P2_TEST.event[1].n.toLocaleString()} / ${P2_TEST.event[1].symbols} / ${P2_TEST.event[1].months}`,
              diff: `+${P2_TEST.delta.bp.toFixed(1)} / t ${P2_TEST.delta.t.toFixed(2)}`,
            },
            {
              layer: '存量簿 · wdist Q5−Q1',
              soe: `+${P2_TEST.book[0].q5q1.toFixed(1)} · ${P2_TEST.book[0].n.toLocaleString()} 行 / ${P2_TEST.book[0].symbols} 只`,
              person: `+${P2_TEST.book[1].q5q1.toFixed(1)} · ${P2_TEST.book[1].n.toLocaleString()} / ${P2_TEST.book[1].symbols}`,
              diff: `+${P2_TEST.bookDelta.bp.toFixed(1)} / t ${P2_TEST.bookDelta.t.toFixed(2)}`,
            },
            {
              layer: '全市场 · ret60 交互',
              soe: '—',
              person: '—',
              diff: `${P2_TEST.market.zi.toFixed(1)} / t ${P2_TEST.market.t.toFixed(2)}`,
            },
          ]}
          rowKey={r => r.layer}
          dense
          minWidth={760}
          reason="事件层：月内 drift_c 三分位 Q2−Q0；存量簿层：has_book 股票-月，wdist 五分位 Q5−Q1；全市场：f60 ~ z(ret60) × 国资 + 控制。产权 = 第一大流通股东（2024–2026 最新一期）。"
          swipeHint
        />
        <Callout tone="danger" label="P2 首测（2026-11-27）：不可识别，不是杀死">
          预注册判据是 Δ &lt; 0 且 t ≤ −2。事件层 Δ = +1,269（t 5.27）——方向相反；但国资只有 268 个事件、
          43 只、11 个月，每月桶里只有 2–3 个事件，池化后 deep +303.5 / shallow +346.7（差 −43bp），
          所谓「方向相反」是小组噪声，不可读。存量簿层（1,882 行 / 46 只）：
          国资 +447（t 2.10）vs 民企 +176（t 1.37），Δ +271（t 1.05），方向与假设一致但不过阈。
          全市场 ret60 交互 −23（t −0.57）。<span className="text-foreground">不是救助解释被证伪，是质押渠道选不到国资质押样本——
          能救助的公司很少质押。</span>下一步不是再跑分组，是换数据：国资入股 / 纾困 / 重组事件。
        </Callout>
        <Callout tone="muted" label="最低验收">
          P1 + P3。P1 过 = 锚定不是任意长期收益；P3 过 = 支付会结清账单。两条全灭，卷五退回「一个更抽象的说法」，
          不留在页面上。
        </Callout>
      </Section>

      <Section
        id="control"
        no="5F"
        title="对照：控制权转移之后，符号反了"
        lead="卷五的「救助→正」是从质押样本读出来的：那批股票事前已经在跌，反弹是浮亏修复。换一个完全不同的样本——控制权从民营转给国资——事前在涨，事后在跌。同一套「账单」，两种符号，差别不在救助，在谁承担余额。"
        source="analysis/_diag_control_change_event.py · 129 国资 / 969 民营 · 2026-11-27"
      >
        <StatGrid
          cols={4}
          items={[
            { k: '国资 · f60', v: '−10.3%', d: 't −4.88 · 胜率 18% · n 129' },
            { k: '民营 · f60', v: '+0.1%', d: 't 0.05 · 胜率 39% · n 969' },
            { k: '差（现金 − 民营）', v: '−11.1pp', d: 't −2.22 · 显著' },
            { k: '现金 − 表决权', v: '−5.0pp', d: 't −0.87 · 不显著' },
          ]}
        />
        <CCFigure />
        <DataTable
          cols={ccCols}
          rows={[...CC.rows]}
          rowKey={r => r.group}
          dense
          minWidth={780}
          reason="公告次日开盘入场，第 k 个交易日收盘出场；超额 = 个股 − 全市场等权；t 为按月聚簇的 Newey-West。对照组是同一批控制权变更公告里买方为民营的事件。"
          swipeHint
        />
        <Callout tone="danger" label="现金 vs 表决权：不成立">
          这是本节最该记下的否定结果。原假设是「付钱的人承担余额，所以现金组反弹更强」——
          实测现金 −12.3%、表决权 −8.7%，差 −5.0pp，t −0.87。<span className="text-foreground">分不开的不是「付不付钱」，是「谁在接」</span>：
          国资 −10.3% vs 民营 +0.1%，差 −11.1pp，t −2.22。
        </Callout>
        <Callout tone="warn" label="消失的是右尾，不是左尾">
          国资 p10 −34.2 / 中位 −12.8 / p90 +21.0；民营 p10 −30.7 / 中位 −6.2 / p90 +47.0。
          下行两端几乎一样（−34 vs −31），差别全在上方：民营买方有 10% 概率涨 47%，国资只有 21%。
          国资接盘没有把左尾压得更狠，它把右尾削掉了——这正是「12 个月内不注入资产」的制度形状。
        </Callout>
        <Callout tone="muted" label="为什么与质押贴线 +525bp 不矛盾">
          质押贴线的事前 60 日是<span className="text-foreground">跌</span>（drift_c 越深，反弹越强）；
          控制权转移的事前 60 日是<span className="text-foreground">涨</span>（+8.2%，表决权组 +15.5%）。
          前者是浮亏修复，后者是利好出尽。同一个「未付账单」，买单的人不同，符号就不同。
        </Callout>
        <Callout tone="muted" label="「未到兑现时点」已检验：拿不到支持">
          如果右尾消失只是「国资还没来得及注入资产」，那么 12 个月内发生注入的子样本应当为正。
          实测（东财并购库，ZRFS=发行股份购买）：24 个月内只命中 6 个，f60 全为负
          （−0.5 / −6.9 / −16.6 / −16.8 / −24.6 / 缺失）；放宽到「任何股权收购」106 个，
          f60 −10.8%，与无收购组 −10.2% 无差异。<span className="text-foreground">严格口径样本只有 2 个，是数据覆盖不足，
          不是检验通过</span>——但现有证据里没有一个正号，所以这条边界暂时不改。
        </Callout>
      </Section>

      <Section
        id="kill5"
        no="5G"
        title="什么会杀死这套理论（卷五）"
        lead="六条写在下一轮跑数之前。任何一条成立，对应的部分就降级；第 5 条是唯一可能把理论改写而不是杀死的。"
        actions={<span className="text-[11px] text-muted">预注册 · 只追加</span>}
      >
        <ol className="mt-2 space-y-4">
          {KILL5.map((k, i) => (
            <li key={k} className="flex gap-4 border-t border-border pt-3 first:border-t-0 first:pt-0">
              <span className="mt-0.5 w-5 shrink-0 font-mono text-[11px] tabular-nums text-muted">
                {String(i + 1).padStart(2, '0')}
              </span>
              <p className="max-w-[46em] text-[13px] leading-[1.75] text-secondary">{k}</p>
            </li>
          ))}
        </ol>
        <Callout tone="danger" label="自认">
          本卷只有一个候选（H11/H12 的贴线溢价）和一条排除证据（D 的存量零），其余是从同一个恒等式重述出来的推论——
          <span className="text-foreground">重述不是证据</span>。候选本身也只有 B 级：准 OOS 过线（+293.7bp / t 1.80），
          但成本敏感（0.8% 即失守）、真前瞻未跑，且线性控制下会消失（+47bp / t 0.83）。它的价值不在解释了过去，在能被更少的参数杀死：
          卷四要问「哪个变量有符号」，卷五只问「这笔账单多久盯一次市」。若前瞻纸面不过，本卷整卷删除，不留启示。
        </Callout>
      </Section>
      </VolumeBlock>

      <VolumeBlock def={vol('v6')}>
      <Section
        id="four"
        no="6A"
        title="四种贴线，一个排序"
        lead={`同一个词，四种实现：簿加权平均、数量占比、最大合约、最贴合约。同一批股票、同一个对照组（wdist ≤ 0.6 的有簿股）、同一套 60 日路径最近邻，四种口径各 ${COND.pairMin.toLocaleString()}–${COND.pairMax.toLocaleString()} 对 / ${COND.months} 月，60 日超额从 +141bp 到 −54bp。水平差 ${COND.spread}bp——这不是抽样误差，是四种代理对「边界质量」的四种近似。`}
        source="analysis/_diag_chip_path_twin.py · path_twin*.json · 8,559 对 / 79 月"
        first
        className="mt-10"
      >
        <Callout tone="danger" label="三句话">
          单位：不是股票，是<span className="text-foreground">索取权</span>。
          统计：不是均值，是<span className="text-foreground">边界质量</span>。
          识别：不是水平，是<span className="text-foreground">条件</span>。
        </Callout>
        <DataTable
          cols={fourCols}
          rows={FOUR_DEFS}
          rowKey={r => r.def}
          dense
          minWidth={880}
          reason="四种实现都在测「贴线」，但没有一种能自称是「贴线」本身。t 只在第一行过 2；其余三行跨 0 或为负。"
          swipeHint
        />
        <FourDefFigure />
        <div className="mt-3 flex flex-wrap items-baseline gap-x-5 gap-y-1 border-t border-border pt-3 font-mono text-[11px] tabular-nums text-muted">
          <span className="text-foreground">路径孪生 · {COND.pairs.toLocaleString()} 对</span>
          <span>不做匹配 <span className="text-foreground">{fmtSigned(COND.randBp, 1)}bp</span> / t {fmtT(COND.randT)}</span>
          <span>匹配后 <span className="text-foreground">{fmtSigned(FOUR_DEFS[0].bp, 1)}bp</span> / t {fmtT(FOUR_DEFS[0].t)}</span>
          <span>路径抵消 <span className="text-foreground">{Math.round(COND.pathShare * 100)}%</span></span>
          <span>配对中位 <span className="text-foreground">{fmtSigned(COND.pairMedian, 0)}bp</span> · 偏度 {COND.pairSkew}</span>
          <span>水平极差 <span className="text-foreground">{COND.spread.toFixed(1)}bp</span></span>
          <span>压力差 <span className="text-foreground">{fmtSigned(COND.stressDelta, 1)}bp</span> / t {fmtT(COND.stressT)}</span>
        </div>
        <Callout tone="danger" label="所以 +291bp 是什么">
          它是<span className="text-foreground">事件口径</span>的读数（单份公告合约、near−far、同特征基准）。
          把它搬到「簿」上，必须说明用哪个近似：同一批股票、同一套匹配，四种簿口径给出四个答案。
          <span className="text-foreground">没有一个跨口径的「贴线溢价常数」</span>——任何 +Xbp 的 X，都是代理的函数，不是市场的常数。
        </Callout>
        <Callout tone="warn" label="但排序没有散">
          四种口径里，低压力全部大于高压力、少合约全部大于多合约——<span className="text-foreground">8/8</span>。
          而且水平本身与「贴线余额的质量」同序：<span className="text-foreground">+141 &gt; +21 &gt; −42 &gt; −54</span>（见 6B）。
          代理可以换，质量与条件不会换。这一卷要留下的不是任何一个 +X，是这两个不等式和这个排序。
        </Callout>
        <Callout tone="muted" label="方法论">
          在不可识别的对象上，只报交互，不报主效应。报主效应的人，最后报的是自己的口径。
          这也是前四卷十六个零的同一个病：它们全是主效应。
        </Callout>
      </Section>

      <Section
        id="grain"
        no="6B"
        title="质量：贴在触发面上的余额"
        lead="四种口径的水平与「边界质量」同序：按金额加权的贴线程度（+141）> 贴线数量占比（+21）> 最大合约（−42）> 最贴合约（−54）。前两个问「多少余额贴线」，后两个问「有没有一份贴线」。一条合约碰到线是一个点；整本账贴线是一团质量。定价的是质量。"
        source="FOUR_DEFS · path_twin_by_ncon"
      >
        <StatGrid
          cols={4}
          items={[
            { k: '质量 · wdist / share', v: '+141.0 / +20.7', d: '多少余额贴线；金额加权 > 数量占比' },
            { k: '点 · big / max', v: '−41.9 / −54.1', d: '有没有一份贴线；碰到线，不是压在线上' },
            { k: '单合约 · ncon 1', v: '+194.6bp', d: 'n 4,957；100% 余额在一条线上' },
            { k: '多合约 · ncon 8', v: '+67.2bp', d: 'n 3,602；余额摊开' },
          ]}
        />
        <Callout tone="danger" label="边界质量">
          定价强度 ∝ 贴在触发面上的余额质量。碰到线不算，贴在线上才算；一份合约不算，一团余额才算。
          这与四种口径的同序一致，也解释了为什么「最贴合约」是负的：它只测到「有一份碰到了线」，
          没测到「多少余额被压在线上」。
        </Callout>
        <Callout tone="warn" label="集中，不是不可分割">
          ncon=1 +194.6 vs ncon=8 +67.2：单合约簿把 100% 的余额放在一条线上（集中度 1），多合约簿把余额摊开。
          机制是<span className="text-foreground">集中</span>，不是「合约不能拆」——后者是一个更窄、也更难证伪的说法，本检验不支持。
          这也不是「均值 vs 极值」：wdist 本身就是加权均值，它赢在权重压在边界上；max 输在它只取一个点。
        </Callout>
        <Callout tone="muted" label="竞争解释（未排除）">
          单合约簿更小、更年轻、更可能是小盘——+195 里有多少是「集中」而不是「小盘彩票」，本检验分不开。
          规模带匹配只控了规模的一阶，没有控 age / 流动性 / 股东类型。
          所以这一条是<span className="text-foreground">推导，不是识别</span>：它解释排序，但不能单独成立。
        </Callout>
      </Section>

      <Section
        id="capacity"
        no="6C"
        title="容量：执行能力是公共资源"
        lead={`贴线密度 = 有簿股票中 wdist ≥ 0.9 的占比。按月中位数分三档：${STRESS_META.low} / ${STRESS_META.mid} / ${STRESS_META.high}。四种口径在高压力档全部最差。`}
        source="path_twin_by_stress · 79 月 · 高压力 14–16 个月"
      >
        <DataTable
          cols={stressCols}
          rows={STRESS}
          rowKey={r => r.def}
          dense
          minWidth={760}
          reason="同一批配对、同一套匹配，只按当月贴线密度分三档。高−低 = 高压力月均值 − 低压力月均值；t 是两个月组均值的双样本 t，不是 NW。"
          swipeHint
        />
        <Callout tone="danger" label="容量">
          救助不是无限的。全市场同时贴线时，单位救助强度下降——
          <span className="text-foreground">执行能力是公共资源，会被挤兑</span>。
          这也解释了 2022（−5.3bp）与 2026（−66.1bp）：不是信号失效，是容量没了。
        </Callout>
        <Callout tone="warn" label="两种解释未分离">
          (a) 容量挤兑：能救的钱不够；(b) 对照压缩：高压力月里对照组也被救，差值被压小。
          两种都指向<span className="text-foreground">状态依赖</span>，都杀死「常数因子」。
          要分开它们，需要独立的救助/解押事件数据——现在没有，不是零。
        </Callout>
        <Callout tone="muted" label="制度旁证：2018 前是零，不是负">
          同一套事件口径，{PRE2019.window} 的 near−far 是 <span className="text-foreground">+{PRE2019.spread.toFixed(1)}bp / t {PRE2019.t.toFixed(2)}</span>
          （{PRE2019.months} 月）；2019–2026 是 +{PRE2019.postSpread.toFixed(1)}bp / t {PRE2019.postT.toFixed(2)}。
          分界不是数据，是 2018-10 的纾困基金。但注意：2018 前是<span className="text-foreground">零</span>，不是负——
          A 股样本里从未出现过清算型负号。符号不是从负到正的连续谱，是「零 / 正」两态；
          清算型负号只是一个理论可能性，未被观测。
        </Callout>
      </Section>

      <Section
        id="shape"
        no="6D"
        title="分布：均值是右尾，不是常态"
        lead="如果约束是期权，它首先改变二阶矩，不是一阶矩。把贴线组与路径孪生的 60 日收益分布放在一起：宽度、两尾、偏度都不同。"
        source="path_twin.json · shape_near / shape_twin / twin"
      >
        <DataTable
          cols={[
            { head: '读数', w: '34%', render: (r: ShapeRow) => <span className="text-[12px] text-foreground">{r.metric}</span> },
            { head: '贴线组', w: '22%', render: (r: ShapeRow) => <span className="font-mono text-[11px] tabular-nums text-foreground">{r.near}</span> },
            { head: '路径孪生', w: '22%', render: (r: ShapeRow) => <span className="font-mono text-[11px] tabular-nums text-secondary">{r.twin}</span> },
            { head: '读法', render: (r: ShapeRow) => <span className="text-[11px] text-muted">{r.read}</span> },
          ]}
          rows={SHAPE6}
          rowKey={r => r.metric}
          dense
          minWidth={640}
          reason="同月去均值后的原始 f60；配对差 = 贴线 − 孪生，按 8,559 对逐对计算。"
        />
        <Callout tone="danger" label="均值不是常态">
          中位数 +7bp、胜率 50.2%、偏度 3.54：+141 是 50% 的零加少数右尾。
          把它当稳定 alpha，会死在集中度和成本上——这也解释了 12× 换手和 0.8% 成本为什么能杀死它。
        </Callout>
        <Callout tone="warn" label="正确用法">
          宽度（多标的、固定权重），不是深度（重仓贴线股）。这类收益不能进 Sharpe 预算，只能进「彩票预算」；
          上限由尾部亏损定，不由均值定。
        </Callout>
        <Callout tone="muted" label="与账户的关系">
          仍是 0。卷六没有把候选升级成买点，只把它从「均值」改写成「条件 + 分布」。
        </Callout>
      </Section>

      <Section
        id="kill6"
        no="6E"
        title="什么会杀死卷六"
        lead="五条写在下一轮跑数之前。第 1 条若成立，整卷删除——因为它检验的不是一个信号，是「条件可识别」这件事本身。"
        actions={<span className="text-[11px] text-muted">预注册 · 只追加</span>}
      >
        <ol className="mt-2 space-y-4">
          {KILL6.map((k, i) => (
            <li key={k} className="flex gap-4 border-t border-border pt-3 first:border-t-0 first:pt-0">
              <span className="mt-0.5 w-5 shrink-0 font-mono text-[11px] tabular-nums text-muted">
                {String(i + 1).padStart(2, '0')}
              </span>
              <p className="max-w-[46em] text-[13px] leading-[1.75] text-secondary">{k}</p>
            </li>
          ))}
        </ol>
        <Callout tone="danger" label="如果只能留一个数">
          {KILL6_NOTE}
          <br />
          <span className="text-muted">
            理由：+141 是四种口径里唯一过 2 的一个，选它就是把口径当发现；−202 是四种口径里三次同号、两次 |t|≈1.9 的交互，
            它不需要挑口径。
          </span>
        </Callout>
      </Section>
      </VolumeBlock>

      <VolumeBlock def={vol('v4')}>
      <Section
        id="ontology"
        no="A"
        title="三次换对象，三次都还在测状态"
        lead="质量 → 年龄 → 义务。每一次都以为换的是对象，其实换的是坐标；四次都是「给定时刻的横截面」。共同死因不是选错了变量，是「状态变量」这个形式本身。"
        first
        className="mt-10"
      >
        <Tiles cols={2}>
          {ONTOLOGY.map(o => (
            <div key={o.vol} data-testid="ontology-card">
              <Tile
                lead={o.vol}
                aside={<span className="font-mono text-[10px] text-muted">{o.math}</span>}
                title={o.obj}
                body={o.why}
              />
            </div>
          ))}
        </Tiles>
        <Callout tone="danger" label="共同死因">
          三次都在问「这个横截面高不高」，没有一次问「这个数字是什么时候、被谁、为什么交出来的」。
          分布是非负测度：它能告诉你多少，不能告诉你往哪卖。
        </Callout>
      </Section>

      <Section
        id="selection"
        no="B"
        title="状态没有符号，发起有"
        lead="一个分布缺三样东西：符号、日期、对手方。一个有成本、有发送者、有条款的「发起」三样都有——所以它的符号在测量之前就定了，不需要靠 OOS 去猜。"
      >
        <DataTable
          cols={dimensionCols}
          rows={DIMENSIONS}
          rowKey={r => r.dim}
          minWidth={720}
          reason="状态量是幅度的聚合；发起量是动作的记录。资产定价的第一课：只有创新是 news，水平是 state，state 已被定价。"
        />
        <Callout tone="danger" label="一句话">
          筹码不是持仓的痕迹，是持仓者选择留下的痕迹。你看到的不是筹码，是有人愿意让你看到的筹码。
        </Callout>
        <Callout tone="warn" label="定价的是发起">
          「谁持有多少」是存量，已经被公开信息定价；「谁刚刚把自己押上去」是发起的新闻，才产生漂移。
          这解释了为什么十六个检验里，十二个状态变量全零，唯一稳定的是发起——不是运气，是二分。
        </Callout>
      </Section>

      <Section
        id="record"
        no="C"
        title="十六个结果，一个二分"
        lead="把 H1–H12 与 C1–C4/F1 放在同一张表里，只看一件事：它测的是状态，还是发起。结果按形式分成两列，没有交叉。"
        source="docs/structural-edge/chip-duration.md · chip-origination.md"
      >
        <DataTable
          cols={unifyCols}
          rows={UNIFY}
          rowKey={r => r.id}
          dense
          minWidth={720}
          reason="H1 的「半活」指它活成了风险因子（与回撤/下行波动），收益端仍是零；这与「状态无符号」一致。"
          swipeHint
        />
        <Callout tone="danger" label="读法">
          十二个状态检验，零个稳定的收益符号；四个发起检验里，两个是同一个信号的两面（H11/H12），
          两个是诊断（C2 不可识别、F1 样本太薄）。这不是十六次独立试验，是一个二分：
          <span className="text-foreground">幅度只能估符号，动作的符号由约束给定。</span>
        </Callout>
      </Section>

      <Section
        id="book"
        no="D"
        title="存量簿首测：v1 零，v2 修口径后仍是零"
        lead="把 H11 从事件升级成状态：任意月末，活合约簿里有多大比例贴住平仓线。v1 的三个规格没有一个是稳定的负号；v2 把复权/入场/展期修完后，连弱负号也消失了。"
        source="analysis/_diag_chip_constraint_book.py · _diag_chip_constraint_book_diag.py · _diag_chip_constraint_flow.py · _diag_chip_constraint_book_v2.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: 'v1 受控 near_share', v: '−0.1bp', d: 't −0.01 · 控换手/波动/规模/久期/动量' },
            { k: 'v1 wdist Q5−Q1', v: '+266.2bp', d: 't 2.34 · 原始正向；控后 +40.0 / t 0.77' },
            { k: 'v2 全修 near_share', v: '+6.0bp', d: 't 0.34 · 修 G1 复权后归零' },
            { k: 'v2 全修 wdist', v: '−13.7bp', d: 't −0.23 · 不是负号，是零' },
          ]}
        />
        <DataTable
          cols={bookCols}
          rows={BOOK_ROWS}
          rowKey={(r, i) => `${r.id}-${i}`}
          dense
          minWidth={860}
          reason="以下为 v1 口径（平仓线未复权）。面板 2020-01 ~ 2026-07，79 个月末，5,225 只，有效 315,178 行；月末次日开盘入场。v2 对照见 D2。"
          swipeHint
        />
        <Callout tone="warn" label="规格坑">
          near_share 是 ratio 的倒 U 变换：股价下跌 → ratio 上升 → 进入 [0.8,1.2] → 继续跌 → 离开（变 breached）。
          五分位混了「正在靠近」和「已经跌穿后离开」，单调性被破坏。任何「贴线占比」都必须同时报 wdist 与 breach_share。
        </Callout>
        <Callout tone="danger" label="裁决">
          v1 看起来是「存量与事件原始符号相反」：存量越贴线，未来收益越高（+266bp）；事件贴线，未来收益越低（f60 −474bp）。
          但这两半都不可靠：v1 的平仓线是原始价、价格是前复权价，老合约的距离被系统性放大，v2 只修复权（G1）就归零；
          事件那一半用了提取日价（§0），修正后是 +525bp。所以稳定的对比只剩下
          <span className="text-foreground">存量零</span>——事件侧要等修正后的符号（§E）。
          测量口径单独见 D2；E1–E5 不参与这个裁决。
        </Callout>
      </Section>

      <Section
        id="book-gap"
        no="D2"
        title="测量缺口单独一版：v1 → v2"
        lead="存量簿的零有两种解释：理论（状态已定价）或测量（口径错）。这一节把八个缺口逐个过一遍：能修的修，不能修的标出来，看零还在不在。目的不是给存量找活路，是把「测量」这个替代解释量化。"
        source="analysis/_diag_chip_constraint_book_v2.py · data/.scratch/leverage/constraint_book_v2.json"
      >
        <DataTable
          cols={gapCols}
          rows={BOOK_GAPS}
          rowKey={r => r.id}
          dense
          minWidth={900}
          reason="G1–G3 已修；G4 做敏感性；G5–G8 未修。未修的缺口只能让存量更不准，不能把一个零变成稳定的负号——但也不能排除。"
          swipeHint
        />
        <DataTable
          cols={v2Cols}
          rows={BOOK_V2}
          rowKey={r => r.variant}
          dense
          minWidth={860}
          reason="受控系数（f60，控制同 D）；n≈8.5–13.8 万股票-月。v1′ 是同脚本复现（n 8.5 万），与 D 的冻结 v1（−0.1/+40，n 11.6 万）样本构造不同，只比变体间的相对变化。v2 与 v2b 完全相同 → G2 无影响；v2 与 v2a 的差 → G1 是主因；G3 三变体都开，不能单独识别。"
          swipeHint
        />
        <Callout tone="danger" label="裁决">
          1）v1 的负号主要来自 G1，不是来自约束：不复权（v2a）仍是旧符号；复权（v2b/v2）就归零；
          v2 与 v2b 完全相同 → G2 无影响；G3 三变体都开，不能单独识别，但不可能是主因。
          2）修完 G1–G3，存量是零，不是负——这不是「存量被救活」，是「存量连弱负号都没有了」。
          3）G4 让存量偏正（v2c near +24.7 / wdist +117.5），是「老公司/低估值」的 tilt，不是约束。
          4）G5–G8 未修：结论只能写成「<span className="text-foreground">在可修的口径内为零</span>」。
        </Callout>
        <Callout tone="warn" label="事件侧不受影响">
          H11 的 `ratio = openline / CLOSE_PRICE` 是公告日的 raw/raw；H12 用两个前复权价相除。事件研究的尺度自洽，
          所以「事件负」不是同一个 artifact。存量零更硬，事件负仍独立。
        </Callout>
        <Callout tone="muted" label="顺带">
          v2 的波动系数仍是 −0.011pp / t −2.0（贴线股未来波动略低，不是更高）；短伽马读法在修正口径后仍没有证据。
        </Callout>
      </Section>

      <Section
        id="contrast"
        no="D3"
        title="一张图：存量零，事件正"
        lead="把 D/D2 的四个存量系数与修正后 H11 的两个事件读数放在同一张图上。左边四行全部跨 0，右边两行全部在 0 右侧——这是「零 vs 正」，不是「负 vs 正」。"
        source="data.ts CONTRAST · D/D2 受控系数 · H11 near−far"
        actions={<RevisedTag onJump={() => go('audit')} />}
      >
        <ContrastFigure />
        <DataTable
          cols={contrastCols}
          rows={CONTRAST}
          rowKey={r => r.label}
          dense
          minWidth={760}
          reason="95% CI 由 |β/t| × 1.96 近似；存量是受控系数（bp / +1sd），事件是 raw near−far（bp / 20/60 日），两栏刻度不同，不可跨栏比大小。"
          swipeHint
        />
        <Callout tone="danger" label="读法">
          存量四行的点估计在 −13.7 ~ +40.0bp 之间，CI 全部跨 0；事件两行是 +206.8 / +525.3bp，
          CI 全部在 0 右侧。图不是新证据，是把「存量没有稳定符号」和「事件有正号」放在同一个视觉刻度上，
          防止读者把两者混成「筹码整体无效」或「筹码整体有效」。
        </Callout>
      </Section>

      <Section
        id="g4"
        no="D4"
        title="G4 敏感性：补多久？"
        lead="G4 是「已解押但无日期」的 32% 行。不补（drop）是零；补 90 天就翻正，之后 90–730 天几乎平台。补多久不重要，补不补才重要。"
        source="analysis/_diag_chip_constraint_book_v2.py · data/.scratch/leverage/constraint_book_v2.json"
      >
        <DataTable
          cols={g4Cols}
          rows={G4_ROWS}
          rowKey={r => r.horizon}
          dense
          minWidth={760}
          reason="所有变体都开着 G1+G2+G3；只改 G4 的补寿命。受控 f60，控制同 D；n≈9.9–14.3 万股票-月。"
          swipeHint
        />
        <Callout tone="warn" label="读法">
          不补：wdist −13.7（t −0.23）；补 90 天：+126.0（t 2.02）；补 180/365/540/730：
          +120.6 / +117.5 / +119.0 / +115.1（t 1.9–2.0）。<span className="font-medium text-foreground">一补就偏正，补多久不再改变方向。</span>
        </Callout>
        <Callout tone="danger" label="裁决">
          G4 只能把存量推向正，不能推出负。它不是「掩盖了雪崩」，而是「把老合约留久后的老公司/低估值 tilt」。
          所以存量零的结论对「补多久」不敏感，只对「补不补」敏感；而「补不补」没有先验正确答案，
          只能把两种都报出来。E1–E5 不参与这个裁决。
        </Callout>
      </Section>

      <Section
        id="book-notice"
        no="D5"
        title="公告口径：合约本身缺一批"
        lead="D2 修的是同一批合约内部的口径。还有两件更外层的事：合约表从 2019 年才开始有公告；同一份合约又在 2019/2020/2021 被反复披露，把剩余额重复求和。补完 2005–2018 再问一次：零还在不在。"
        source="analysis/_fetch_pledge_pre2019.py · _diag_chip_constraint_book_v3.py · _diag_chip_book_coverage.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: '补入明细', v: '45,349 条', d: '2005-01 ~ 2018-12 · 合并去重后 83,703 行 / 2,886 只' },
            { k: '2020 年在簿', v: '65.7%', d: '来自 2019 前公告 · 快照口径下 87.7%' },
            { k: '三口径 near', v: '|t| < 0.4', d: '+4.79 / +1.46 / −0.03bp' },
            { k: '2026 覆盖率', v: '0.986', d: 'Σ簿 / Σ中登 · 逐股中位 0.971' },
          ]}
        />
        <DataTable
          cols={v3Cols}
          rows={BOOK_V3}
          rowKey={r => r.tag}
          dense
          minWidth={900}
          reason="月频 79 个月末，控制同 D（换手/波动/规模/久期/动量）。bw = 仅 2019+ 公告；bw3 = 2005+ 公告；sn3 = 2005+ 且每份合约只取最近一期未解押余额。"
          swipeHint
        />
        <DataTable
          cols={coverCols}
          rows={COVERAGE}
          rowKey={r => r.date}
          dense
          minWidth={760}
          reason="Σ合约簿 / Σ中登个股质押比例（只在匹配到的股票上求和）。中登含全部质押人，公告明细只含重要股东，所以 1.0 不是「应该到」的目标，只是量级参照。"
          swipeHint
        />
        <Callout tone="danger" label="补完还是零">
          三个口径的受控 near / wdist 全部落在 |t| 0.4 以内，符号在 −5.7 ~ +15.2bp 之间。
          覆盖率缺口和重复披露都不是零的原因——这次动的是合约本身，不是合约内部的尺度。
        </Callout>
        <Callout tone="warn" label="但分母不一样">
          sn3 在 2020 只盖住中登口径的 44%，2026 是 99%。早期年份的零是
          <span className="font-medium text-foreground">「重要股东子集上的零」</span>，不能读成「所有质押都测过了」。
          2020 缺的那 56% 不是 2005 年前没抓到（2005–2011 全量只有 3,515 条），而是公告明细只覆盖重要股东。
        </Callout>
        <Callout tone="warn" label="重复披露有多大">
          2026 年 raw 口径已到 1.148（比中登高 15%），说明重复披露把簿撑大了；snap 0.986、逐股中位 0.971。
          near_share / wdist 受影响较小（bw3 与 bw 都还是零），但 <span className="font-mono text-foreground">book</span> 的水平不能用 raw 报。
        </Callout>
        <Callout tone="muted" label="结论因此收窄">
          不是「质押存量与未来收益无关」，而是「在公告明细能覆盖到的重要股东合约上，
          距平仓线的加权位置没有稳定的收益符号」。这句话比 D 的版本弱，但它是唯一经得起覆盖率检验的版本。
        </Callout>
      </Section>

      <Section
        id="event"
        no="E"
        title="唯一候选事件（修正）：不是强平，是救助"
        lead="H11 修正后：股价距平仓线越近，未来 60 日反弹越强（贴线组 f60 绝对 +1011bp；near−far +525bp / t 7.47）。H12 的结构没变——折算（行权价）无效、路径（余额）有效；变的是符号。这个符号不是噪声，它说的是解除方式：A 股的控制权质押不是被清算的，是被救助/展期/重组解决的。"
        actions={<span className="text-[11px] text-muted">修正版 · 2026-11-27</span>}
      >
        <Callout tone="warn" label="旧读数是未来函数造的（§0）">
          旧 drift 与 ratio 都用了提取日价格，含公告日之后的行情。修正端点后，near−far 从 −474bp 翻到 +525bp，
          涨幅系数从 +72.7 翻到 −276.0；分年七年同号，逐年增强（−108 → −541）。
          <span className="text-foreground">不是效应不稳定，是旧端点把未来塞进了信号。</span>
        </Callout>
        <Tiles cols={3}>
          {EVENT_READ.map(e => (
            <div key={e.tag} data-testid="event-card">
              <Tile
                lead={e.tag}
                aside={<Tag tone={e.tone}>{e.tone === 'ok' ? '活' : e.tone === 'warn' ? '判别' : '被否'}</Tag>}
                title={e.title}
                body={e.body}
              />
            </div>
          ))}
        </Tiles>
        <Callout tone="danger" label="预注册撤回">
          pledge-drift 的冻结规格与评估日 2027-03-31 一并撤回：它的信号值 `drift` 用了提取日价格，
          判据在主判据之前就已经失效。要重新预注册，先把 `drift_c` 写死、再用前向数据裁决。
        </Callout>
        <Callout tone="danger" label="边界：这是不是超跌反弹？">
          一半是。P1 把 drift_c 对通用锚（高点 + ret20/60/120/250）正交，通用锚解释 48.3%，
          残差仍有 −147 / t −3.25。<span className="text-foreground">一半是超跌，一半是合约特有的浮亏。</span>
          要分开，看 P2（救助可达性）：国资/纾困组显著更强 → 救助解释成立；两组无差异 → 只是超跌。
        </Callout>
      </Section>

      <Section
        id="transfer"
        no="F"
        title="迁移（E4）单列：形式可迁移，符号不迁移"
        lead="H11/H12 只证明了一个数据集。E4 不是「再找一个显著」，而是一个带负控的符号排序：形式（发起 > 存量）应该迁移；符号由解除方式决定，所以不应该跨工具无差别迁移——已结清的账单约为 0。"
        source="analysis/_diag_block_trade_transfer.py · data/.scratch/leverage/block_trade_transfer.json"
      >
        <DataTable
          cols={transferCols}
          rows={TRANSFER}
          rowKey={r => r.data}
          minWidth={820}
          reason="三个必过：同一数据集内发起 > 存量；强制卖方为负、强制买方为正；自愿发起 ≈ 0。三类同号 = 只是「事件溢价」。"
          swipeHint
        />
        <DataTable
          cols={transferResCols}
          rows={TRANSFER_RESULT}
          rowKey={r => r.test}
          dense
          minWidth={760}
          reason="大宗首测：2019-01 ~ 2026-11，A 股 225,380 条；首次 = 过去 60 个交易日无大宗；深折价 = min(premium_doc) ≤ −5%；溢价 = max ≥ +2%；入场次日开盘，出场 t+61 收盘。"
          swipeHint
        />
        <Callout tone="warn" label="形式过、符号按解除方式重排">
          存量零（−11.8bp / t −1.48）——形式过关；溢价首次 +154.1bp / t 2.13——正号，但控制回归只有 4 个月
          事件数 ≥20，欠功效；深折价首次 +40.7bp / t 0.77——在新读法下这不是失败：大宗成交即结清，
          预测本来就是 ≈0。拆买卖方后翻转（营业部 +41.4 / 机构 −97.2）仍说明「谁卖」比「折价多深」更重要。
        </Callout>
        <Callout tone="danger" label="现状">
          E4：形式 1/1 过；符号拆类型后翻转（F2），不可识别；迁移 1/4 跑。它是一个<span className="font-medium text-foreground">半活</span>的假设，不是结论。
          它最容易死的地方不是「不显著」，是「三类发起同号」——那说明「发起」只是「事件」，
          符号根本不是义务给的。
        </Callout>
      </Section>

      <Section
        id="type-split"
        no="F2"
        title="买卖方类型：符号不可识别"
        lead="深折价不是同一种交易。小样本（138 件）里营业部 +41bp、机构专用 −97bp；把样本扩到 2013–2026、任意大宗日 + 20 日冷却后，机构专用卖方 807 件：点估计 −169bp（t −1.22），匹配后 −143bp（t −0.50），且 2019 后归零。符号仍不可识别。"
        source="analysis/_diag_block_trade_transfer.py · _diag_block_trade_type_big.py · 2013-01 ~ 2026-08"
      >
        <DataTable
          cols={typeSplitCols}
          rows={TYPE_SPLIT}
          rowKey={r => r.cell}
          dense
          minWidth={760}
          reason="小样本口径：过去 60 日首次 + 当日最小折价那笔的卖方类型；2019-01~2026-11。"
          swipeHint
        />
        <DataTable
          cols={typeBigCols}
          rows={TYPE_BIG}
          rowKey={r => r.spec}
          dense
          minWidth={820}
          reason="大样本口径：任意大宗日 + 同股票 20 日冷却，2013-01~2026-08；inst 807 / broker 18,750。匹配 = 同月 × ret20/size/turn20 五分位 1:3。"
          swipeHint
        />
        <Callout tone="danger" label="符号不可识别">
          小样本：深折价 · 营业部卖方 <span className="font-medium text-foreground">+41.4bp / t 0.82</span>；
          机构专用卖方 <span className="font-medium text-foreground">−97.2bp / t −0.45</span>（138 件）。
          大样本：机构专用卖方 <span className="font-medium text-foreground">−169.3bp / t −1.22</span>（807 件），
          受控 −174.4 / t −1.51，匹配后 −142.9 / t −0.50；营业部卖方 +44.6 / t 1.35。
          方向一致，但没有一个口径把 t 推过 2。
        </Callout>
        <Callout tone="warn" label="分时代">
          2012–2018：机构专用卖方 <span className="font-medium text-foreground">−343.7bp / t −3.14</span>（469 件）；
          2019–2026：<span className="font-medium text-foreground">−25.3bp / t −0.11</span>（338 件）。
          显著负只存在于前一时代（2015 去杠杆、2018 质押危机），近八年完全消失。
          <span className="font-medium text-foreground">这不是稳定规律，是 regime。</span>
        </Callout>
        <Callout tone="warn" label="结论">
          扩样后方向更像「机构专用卖方 = 负」，但总样本 t −1.22、匹配后 t −0.50、2019 后归零，
          仍不能把符号钉住。E4 仍是半活，不能升级。机制拆解见 F3：质押零重叠、定增共线且符号翻转、
          负号在 neither 组仍在——不是质押/定增驱动。
        </Callout>
      </Section>

      <Section
        id="mechanism"
        no="F3"
        title="2012–18 机制拆解：质押 / 定增 / 都不是"
        lead="把 2012–18 的机构专用卖方深折价拆成「活质押 / 定增锁定期 / 两者都不是」。质押零重叠；定增全是机构卖方但不能单独识别；负号在两者都不是的组里仍在。"
        source="analysis/_diag_block_type_mechanism.py · _fetch_pledge_seo_history.py · 41,834 条质押 + 3,084 条定增"
      >
        <DataTable
          cols={mechanismCols}
          rows={MECHANISM}
          rowKey={r => r.bucket}
          dense
          minWidth={820}
          reason="2012–18 的 469 件 inst_deep：P=活质押（start≤t 且 unfreeze>t 或未解押）；S=定增锁定期内（listing_date + LOCKIN_PERIOD）。broker 列为同桶的营业部深折价事件数。当前标的池，退市股未纳入；质押/定增为公告口径。"
          swipeHint
        />
        <DataTable
          cols={mechInterCols}
          rows={MECH_INTER}
          rowKey={r => r.test}
          dense
          minWidth={760}
          reason="交互回归：f60 ~ inst + 暴露 + inst×暴露 + ret20/60 + vol20 + turn20 + size + log(D)；2012–18 月截面，月均系数 + NW t。"
          swipeHint
        />
        <Callout tone="danger" label="质押不是驱动">
          807 件全样本机构专用卖方深折价里，<span className="font-medium text-foreground">0 件</span>有活质押；
          2012–18 的 469 件同样 0。营业部深折价里有 2,790 件有（2012–18）。
          质押强平走营业部席位，不走机构专用。
          无质押组的 inst 效应反而更强（−350.6bp / t −3.42），质押主效应不显著（−10.7 / t −0.57）。
          这条只针对大宗卖方类型，不否定 H11 的质押公告漂移——那是另一条线。
        </Callout>
        <Callout tone="warn" label="定增不能单独识别">
          定增锁定期内的 364 件<span className="font-medium text-foreground">全部</span>是机构专用卖方，S 与 inst 完全共线；
          更麻烦的是时间：<span className="font-medium text-foreground">2014–17 的 inst 事件 100% 是定增，2019+ 0% 是定增，同月重叠只有 1 个月</span>。
          定增与时代完全共线，不能把「定增」和「2016–18」分开。桶均值 −302.8bp，但 2012–15 是
          <span className="font-medium text-foreground">+996.3bp</span>（65 件）、2016–18 是
          <span className="font-medium text-foreground">−585.2bp</span>（299 件）——符号随时代翻转。
          定增是 2016–18 机构退出潮的标签，不是稳定驱动。
        </Callout>
        <Callout tone="warn" label="剩下的负号">
          「两者都不是」的 105 件机构卖方，f60 仍是
          <span className="font-medium text-foreground">−279.6bp</span>；2016–18 子样本 −375.4bp（97 件）。
          所以 2012–18 的 regime 比质押/定增更宽：更像机构退出/流动性折价，而不是某一种合约。
        </Callout>
      </Section>

      <Section
        id="next"
        no="G"
        title="四条前瞻预测：把选择函数变成可测的东西"
        lead="E4（迁移）已单列于 F；这里只剩四条。如果信息在发起里，下一个对象不是另一个横截面指标，是选择函数 π(发起 | 私有状态)。"
      >
        <DataTable
          cols={epredCols}
          rows={E_PRED}
          rowKey={r => r.id}
          minWidth={700}
          reason="阈值写在这里，不是为了证明理论，是为了让它在 2027 年之前可以被杀死。"
        />
        <Callout tone="muted" label="最低验收">
          E1 + E2 + E4（F 节）。E1 证明「事件 vs 状态」不是换个说法；E2 证明「发送者」在做事；
          E4 证明它不是质押特例。三条全灭，本卷退回「一个事件的重新描述」。
        </Callout>
      </Section>

      <Section
        id="kill4"
        no="H"
        title="什么会杀死这套理论（卷四）"
        lead="六条写在下一轮跑数之前，防止跑完再讲故事。任何一条成立，对应的部分就降级。"
        actions={<span className="text-[11px] text-muted">预注册 · 只追加</span>}
      >
        <ol className="mt-2 space-y-4">
          {KILL4.map((k, i) => (
            <li key={k} className="flex gap-4 border-t border-border pt-3 first:border-t-0 first:pt-0">
              <span className="mt-0.5 w-5 shrink-0 font-mono text-[11px] tabular-nums text-muted">
                {String(i + 1).padStart(2, '0')}
              </span>
              <p className="max-w-[46em] text-[13px] leading-[1.75] text-secondary">{k}</p>
            </li>
          ))}
        </ol>
        <Callout tone="warn" label="现状">
          存量簿 v1 + v2 都已跑（D/D2）：零，且 v1 的弱负号是 G1 复权 artifact。事件侧 H11/H12：活。
          E4 大宗首测：形式过、符号半过。E1/E2/E3/E5 未跑；
          本页的「发起」在 E4 迁移完成之前只是一个半活假设，不是一个已成立的结论。
        </Callout>
      </Section>
      </VolumeBlock>

      <VolumeBlock def={vol('v3')}>
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
        lead="七条预注册草案全部跑完：H1 部分通过并被拆成两条主效应；H2–H7 全灭。最低验收组合（H1+H2+H3）未达到。"
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
        id="h567"
        no="10"
        title="H5 / H6 / H7：三条尾巴，共享一条教训"
        lead={`H5 网络中心度不是第二维度——它是一条「大盘蓝筹」轴（与规模 ${H5.corrSize}、与久期 ${H5.corrD}），且尾部方向相反：高中心度在崩盘里更稳，不是更易传染。H6 没有相变——阈值 BIC 差只有 ${H6.bicDiff}，且低久期时结构更强。H7 的 Θ 是比值噪声——系数 ${fmtSigned(H7.bThetaFrag, 4)}（t ${fmtT(H7.tThetaFrag, 2)}），但分子 Π（换手）单独就能预测脆弱性（t ${fmtT(H7.tPIFrag, 2)}）。`}
        source="analysis/_diag_chip_h5.py · _diag_chip_h5_net.py · _diag_chip_h6.py · _diag_chip_h7.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: 'H5 · 尾部共动', v: `${fmtSigned(H5.bTailMarket, 4)}`, d: `市场 CoVaR · t ${fmtT(H5.tTailMarket, 2)}；网络版 ${fmtSigned(H5.bTailNet, 4)}（t ${fmtT(H5.tTailNet, 2)}）` },
            { k: 'H6 · 阈值 BIC 差', v: `${H6.bicDiff.toFixed(1)}`, d: `需 ≥ ${H6.needBic}；低 D_m 段 ρ ${fmtSigned(H6.rhoLow, 3)} vs 高段 ${fmtSigned(H6.rhoHigh, 3)}` },
            { k: 'H7 · Θ 系数', v: `${fmtSigned(H7.bThetaFrag, 4)}`, d: `t ${fmtT(H7.tThetaFrag, 2)}；分子 Π t ${fmtT(H7.tPIFrag, 2)}` },
            { k: 'H7 · corr(Θ, D0)', v: H7.corrThetaD0.toFixed(3), d: `比值被分母主导；H5 真值比 40 次置换都更负` },
          ]}
        />
        <DataTable
          cols={h567Cols}
          rows={H567_ROWS}
          rowKey={r => r.id}
          reason="三条都是「标签被拆掉」：H5 中心度=规模/蓝筹，H6 相变=线性，H7 比值=分子换手。判据在杀死条件第 11–13 条。"
        />
        <Callout tone="danger" label="裁决">
          H5、H6、H7 全部失败。H5 的中心度不是第二个维度，它是规模/蓝筹轴，且尾部方向相反——系统性持有人
          （指数基金、国家队、主权基金）是稳定器，不是传染放大器；真正的被迫卖家（带杠杆的基金、融资散户）
          不在十大流通股东里。H6 无相变，结构随久期线性增强而非塌缩。H7 的比值 Θ 是噪声，分子 Π（换手）才是风险变量。
        </Callout>
        <Callout tone="warn" label="整卷的结论">
          七条预注册跑完：只有 H1 活了半条（D 和 T 是两条风险主效应）。筹码对象最终塌缩成一条轴——
          <span className="text-foreground">换手/久期是风险因子，不是收益因子</span>。所有「筹码结构」的华丽标签
          （伽马、吸收/雪崩、分散度、网络、相变、滚续）拆开后，都是换手、规模、波动、涨跌幅的重新包装。
          唯一反复出现的正向线索是：A 股的边际持有人是抄底的，不是被迫卖的；雪崩需要杠杆数据才能测。
        </Callout>
      </Section>

      <Section
        id="h8"
        no="11"
        title="H8 追击：放量不是被迫卖，是反转放大"
        lead={`「雪崩需要杠杆数据」这句话被数据修正了。个股融资/质押/赎回不在库里，但被迫卖有一个价格量痕迹——放量。实测：放量下跌比缩量下跌差 ${fmtSigned(H8.l1, 1)}bp（t ${fmtT(H8.t1, 2)}），方向对了；但放量对上涨的伤害（${fmtSigned(H8.upHiMinusLo, 1)}bp）和对下跌几乎一样，不对称只有 ${fmtSigned(H8.interaction, 1)}bp（t ${fmtT(H8.tInt, 2)}）。放量下跌到 40/60 日仍反弹为正——没有雪崩。`}
        source="analysis/_diag_chip_h8.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: '放量 vs 缩量（下跌）', v: `${fmtSigned(H8.l1, 1)}bp`, d: `t ${fmtT(H8.t1, 2)} · 放量更差，方向对了` },
            { k: '方向不对称', v: `${fmtSigned(H8.interaction, 1)}bp`, d: `t ${fmtT(H8.tInt, 2)} · 放量对上下伤害一样` },
            { k: '放量下跌 40/60 日', v: `${fmtSigned(H8.dnHiF40, 1)} / ${fmtSigned(H8.dnHiF60, 1)}bp`, d: `仍反弹为正，无持续阴跌` },
            { k: '控制后', v: `${fmtSigned(H8.bHiControlled, 1)}bp`, d: `t ${fmtT(H8.tHiCtl, 2)} · 大半效应是换手轴` },
          ]}
        />
        <DataTable
          cols={h8Cols}
          rows={H8_CELLS}
          rowKey={r => r.cell}
          reason="事件 = 5 日 |cum5| ≥ 5%，放量 = 近 5 日换手 / 基线换手 ≥ 2（相对放量，剥离高换手股）。入场 t+1 开盘、20 日。连续剂量反应：下跌侧 b(log 换手比) −63bp（t −2.02），不对称 −35bp（t −1.39）。"
        />
        <Callout tone="danger" label="裁决">
          H8 失败：没有雪崩。放量下跌仍反弹（40/60 日 +38/+55bp），只是比缩量下跌弹得少。放量对上涨、下跌的伤害
          几乎相等（−183 vs −158bp，交互 t 1.17）——它是「换手放大反转」的对称放大器，不是「被迫卖」的方向性信号。
          控制换手/波动/规模后效应减半（t −1.9），说明大半就是 H1 的换手轴。
        </Callout>
        <Callout tone="warn" label="结论：边际持有人是抄底的，这条活了">
          H8 补上了最后一块：即使在「放量急跌」这个最像被迫卖的极端子集里，股价仍然反弹。总量层面（H3）、
          持有人层面（H2、H5）、价格量层面（H8）三处独立测量一致——A 股边际持有人是抄底的，雪崩在公开的
          价格、量、持有人数据里找不到痕迹。要测真正的杠杆通道（融资强平、质押平仓、基金赎回），只能去接
          个股融资余额、质押比例、基金份额这类**不在本仓库**的外部数据。
        </Callout>
      </Section>

      <Section
        id="h9"
        no="12"
        title="H9 杠杆数据：融资是代理，质押正交但不雪崩"
        lead={`接了两个外部杠杆数据源（东财 datacenter）。融资余额占比是换手/久期的代理（vs D ${H9.marginCorrD}、vs 换手 ${H9.marginCorrTurn}、vs 脆弱性 ${H9.marginCorrFrag}）——第 9 次撞墙。质押比例与换手/波动/规模/久期全部正交（|ρ| 小于 0.1），是全卷第一个正交外部变量；但 H9 实测它不预测雪崩：高质押股下跌后反弹反而更多，控制后翻负不显著。`}
        source="analysis/_fetch_leverage_snapshot.py · _fetch_pledge_full.py · _diag_chip_h9.py"
      >
        <StatGrid
          cols={4}
          items={[
            { k: '融资 vs 久期', v: H9.marginCorrD.toFixed(3), d: `换手 ${H9.marginCorrTurn.toFixed(3)} · 脆弱性 ${H9.marginCorrFrag.toFixed(3)} → 代理` },
            { k: '质押 vs 久期', v: H9.pledgeCorrD.toFixed(3), d: `换手 ${H9.pledgeCorrTurn.toFixed(3)} · 脆弱性 ${H9.pledgeCorrFrag.toFixed(3)} → 正交` },
            { k: '高质押 fwd40/60', v: `${fmtSigned(H9.f40, 1)} / ${fmtSigned(H9.f60, 1)}bp`, d: `t ${fmtT(H9.tF40, 2)} / ${fmtT(H9.tF60, 2)} · 反弹更多，非雪崩` },
            { k: '控制后', v: `${fmtSigned(H9.bControlled, 1)}bp`, d: `t ${fmtT(H9.tControlled, 2)} · 翻负不显著，是短久期效应` },
          ]}
        />
        <Callout tone="danger" label="裁决">
          两条杠杆通道都没有产生雪崩。融资余额占比 = 换手/久期代理（第 9 次撞墙）；质押比例虽正交，
          但高质押股下跌后反弹**更多**（fwd40/60 +45/+61bp），控制后翻负不显著——这是短久期效应
          （高质押组久期 50 天 vs 无质押 67 天，规模/波动/换手无差异），不是质押本身。
        </Callout>
        <Callout tone="warn" label="弱否定，下一步是平仓线触发">
          事件定义排除了封板日（|ret| 小于 9.5%），但质押强平发生在**连续跌停**里（无法成交 → 继续跌停），
          最可能雪崩的样本被排除了。下一步用 `OPENLINE` 构建股票级加权平仓线，测「跌破平仓线后是否加速下跌」，
          且不排除封板——这是「被迫卖」最后的物理检验。
        </Callout>
      </Section>

      <Section
        id="h11"
        no="13"
        title="H11 平仓线（修正）：贴线是唯一的候选"
        lead={`ratio = 平仓线 / 公告日股价（修正后不再用提取日价）。ratio 越高 = 越接近或已跌破平仓线。剂量反应单调（Spearman f20 ${H11.spearmanF20} / f60 ${H11.spearmanF60}）：f60 从 ≤0.4 的 −132.9bp 升到 >1.0 的 +977.6bp。near−far f60 ${fmtSigned(H11.f60, 1)}bp（t ${fmtT(H11.tF60, 2)}）——但这是多空差；只做多的长腿，相对同特征基准是 +${MATCH.nearResidFull.toFixed(1)}bp（t ${fmtT(MATCH.nearResidFullT, 2)}）。`}
        source="analysis/_diag_chip_audit_fix.py · _diag_chip_p1_anchor.py · _diag_chip_h13e_fullresid.py · 预注册已撤回"
        actions={<RevisedTag onJump={() => go('audit')} />}
      >
        <StatGrid
          cols={4}
          items={[
            { k: 'near − far · fwd60', v: `${fmtSigned(H11.f60, 1)}bp`, d: `t ${fmtT(H11.tF60, 2)} · 多空差，不是长腿` },
            { k: '长腿 · 同特征基准', v: `+${MATCH.nearResidFull.toFixed(1)}bp`, d: `t ${fmtT(MATCH.nearResidFullT, 2)} · 同日同跌幅+波动/换手/规模` },
            { k: '空腿 · 同特征基准', v: `${fmtSigned(MATCH.farResidFull, 1)}bp`, d: `t ${fmtT(MATCH.farResidFullT, 2)} · 空腿也在跑输` },
            { k: '线性同控 ratio+drift', v: `${fmtSigned(MATCH.linearRatio, 1)}bp/sd`, d: `t ${fmtT(MATCH.linearRatioT, 2)} · 线性设定会把它压没` },
          ]}
        />
        <DataTable
          cols={h11Cols}
          rows={H11_BUCKETS}
          rowKey={r => r.bucket}
          reason="事件 = 质押公告（折算 ∈ [0.3,1.2]，剔除除权噪声），去重到股票-日；入场公告次日开盘。near 组绝对 f20/f40/f60 = +419/+784/+1011bp，far 组 f60 +150bp。绝对收益含 beta，不能当 alpha。"
        />
        <Callout tone="warn" label="单调是真的；+525 不是你能拿到的">
          八次证伪之后，第一个单调、显著、在多种对照下都活着的候选：股价距平仓线越近，未来 60 日越强。
          但 near−far +525 是多空差，near 绝对 +1011 含 beta；只做多的人，能被记成 alpha 的只有
          <span className="text-foreground"> +291bp</span>（相对同特征基准）。
          把 +525 或 +1011 当买点，是把空腿的贡献和 beta 都算进了自己的账户。
        </Callout>
        <Callout tone="danger" label="线性控制为什么把它压没">
          把 ratio 与 drift 同时放进线性回归，ratio 从 +240bp/sd 掉到 +47bp/sd（t 0.83）——
          这容易被读成「贴线没用」。但 ratio ≈ 折算/(1+drift)，两者是同一枚硬币；
          真实关系是阈值/凸的，不是线性的。换成「同日同事件池 + ret250 三次曲线 + 波动/换手/规模」的对照，
          near 仍有 +291bp（t 5.28）。所以正确结论不是「零」，是
          <span className="text-foreground">「线性模型测不到，阈值模型测得到；纸面过线，但真前瞻未跑」</span>。
        </Callout>
        <Callout tone="warn" label="因果已拆解（H12，修正）">
          折算（平仓线定多高）仍然无效：−9.6bp/sd（t −0.23）。涨幅（质押以来跌了多少）强有效：
          −276.0bp/sd（t −6.39），控后 −213.9（t −5.06）。所以定价的是「余额」，不是「行权价」——
          结构没变，符号反了：跌得越深，反弹越强。
          旧版的预注册（design +426 / holdout +915，评估日 2027-03-31）撤回：它的 drift 用了提取日价，
          判据在主判据之前就已失效。
        </Callout>
        <Callout tone="danger" label="别读成雪崩">
          near 组的基本面是「已经跌了很多」，不是「即将被强平」。C1（存量距离零）、C2（解押安慰剂不可识别）、
          C3（波动符号互斥）都没有给强平读法提供证据；三处合并，读法应为救助/超跌，不是清算。
        </Callout>
      </Section>

      <Section
        id="reinterpret"
        no="14"
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
        no="15"
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
          本页跑完了 H1–H7：H1 部分通过 / 已分解；H2–H7 全部失败。旧页的冻结数字保留，角色从「裁决」变成了「退化证据」。
        </Callout>
      </Section>

      <Section
        id="versions"
        no="16"
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
          <div className="font-mono">docs/structural-edge/chip-ap.md</div>
          <div className="font-mono">docs/structural-edge/chip-origination.md</div>
          <div className="font-mono">docs/structural-edge/chip-duration.md</div>
          <div className="font-mono">analysis/_diag_chip_constraint_book.py · _diag_chip_constraint_book_diag.py · _diag_chip_constraint_flow.py</div>
          <div className="font-mono">analysis/_diag_chip_h13_relabel.py · _diag_chip_h13b_match.py · _diag_chip_h13c_market.py · _diag_chip_h13d_curve.py · _diag_chip_h13e_fullresid.py</div>
          <div className="font-mono">docs/structural-edge/prereg-pledge-line-paper.md · reports/pledge-line-paper-2026-11-27.md · data/paper/pledge_line_forward.json</div>
          <div className="font-mono">analysis/_build_chip_age.py · _build_chip_holder.py · _build_chip_examples.py</div>
          <div className="font-mono">data/.scratch/leverage/constraint_book_panel.parquet · constraint_book.json · constraint_flow.json</div>
        </div>
      </Section>
      </VolumeBlock>

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
