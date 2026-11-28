/**
 * 叙事账本（/narrative）—— 大盘主流叙事的收据簿。
 *
 * 结构照 /chip 的论证方式，不照它的研究对象：
 *   卷一 换表：从「谁涨了」（资产端，已定价）换到「谁还欠一个更高的价」（负债端）。
 *   卷二 读数：新跑的剂量反应 + 配对安慰剂（冻结数字，来源 ./data.ts）。
 *   卷三 今日账本：实时读数（温度 / 主线 / 资金四路 / 指数波动 / 词频），全部来自
 *         /api/narrative/ledger，本文件不含任何当日数字，也不在这里做二次计算。
 *   卷四 判据：排序可读、水平不可读；容量；杀死条件。
 *   卷五 能/不能：两列都写，防止读者把收据当账单。
 *
 * 为什么实时块要分块标 as_of：四个源时滞不同（两融 T−6、大宗 T−2、主线 T−0）。
 * 把四块并成一天，等于制造一个不存在的「当日全貌」。
 */
import { useQuery } from '@tanstack/react-query'
import {
  api, MARKET_PHASE_LABELS, REGIME_STATE_LABELS,
  type MarketPhase, type NarrativeLedger, type RegimeState,
} from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import {
  Section, DataTable, Callout, Tag, Tiles, Tile, StatGrid, Empty, Cadence, type Col,
} from '../structural-edge/ui'
import {
  META, VERDICT, SUMMARY, LAW, LEGS, DOSE, PAIR, TIERS, BOUNDS, KILL, SCOPE, LIVE_BLOCKS,
  EVENT_BUCKETS, EVENT_MOMENTUM, EVENT_KIND, SEO_READ, EXCLUSION_CHECK,
  type LawRow, type LegRow, type DoseRow, type PairRow, type TierRow, type KillRow,
  type EventBucketRow, type EventMomentumRow, type EventKindRow,
} from './data'

const sgn = (v: number, d = 1) => `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}`
const tone = (v: number) => (v > 0 ? 'text-bull' : v < 0 ? 'text-bear' : 'text-foreground')
const num = (v: number | null | undefined, d = 0) =>
  v == null || Number.isNaN(v) ? '—' : v.toLocaleString('zh-CN', { maximumFractionDigits: d })

/** 分位条：一条 100% 的轨道 + 一个游标。比再写一个数字更快被读到。 */
/** 解禁类型缩写：原始文本「定向增发机构配售股份」在 64px 列里会折三行。 */
const kindShort = (k: string) =>
  k.includes('定向增发') ? '定增解禁'
    : k.includes('首发原股东') ? '首发原股东'
      : k.includes('首发战略') ? '首发战略'
        : k.includes('首发机构') ? '首发配售'
          : k.includes('股权激励') ? '股权激励'
            : k.includes('追加承诺') ? '追加承诺'
              : '其他'

function RankBar({ pct, label }: { pct: number | null | undefined; label?: string }) {
  if (pct == null || Number.isNaN(pct)) return null
  const w = Math.max(0, Math.min(100, pct))
  return (
    <div className="mt-2">
      <div className="relative h-[3px] w-full rounded-full bg-border">
        <div className="absolute inset-y-0 left-0 rounded-full bg-accent/60" style={{ width: `${w}%` }} />
        <div
          className="absolute top-[-2px] h-[7px] w-[2px] rounded-full bg-foreground"
          style={{ left: `calc(${w}% - 1px)` }}
        />
      </div>
      <div className="mt-1 flex justify-between font-mono text-[10px] tabular-nums text-muted">
        <span>{label ?? '250 日分位'}</span>
        <span>{w.toFixed(0)}%</span>
      </div>
    </div>
  )
}

const lawCols: Col<LawRow>[] = [
  { head: '结算频率', w: '13%', render: r => <span className="text-[12px] font-medium text-foreground">{r.cadence}</span> },
  { head: '谁被迫', w: '20%', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.who}</span> },
  { head: '多久结一次', w: '11%', render: r => <span className="text-[12px] text-secondary">{r.when}</span> },
  { head: '有没有余额', w: '18%', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.balance}</span> },
  { head: '本仓读数', render: r => <span className="text-[12px] leading-[1.6] text-muted">{r.read}</span> },
]

const legCols: Col<LegRow>[] = [
  { head: '腿', w: '9%', render: r => <span className="text-[12px] font-medium text-foreground">{r.leg}</span> },
  { head: '对象', w: '16%', render: r => <span className="text-[12px] leading-[1.6] text-secondary">{r.object}</span> },
  { head: '本仓已完成的检验', w: '26%', render: r => <span className="text-[11.5px] leading-[1.6] text-muted">{r.tests}</span> },
  { head: '读数', w: '26%', render: r => <span className="text-[11.5px] leading-[1.6] text-foreground">{r.read}</span> },
  { head: '用法', render: r => <span className="text-[11.5px] leading-[1.6] text-secondary">{r.use}</span> },
]

const doseCols: Col<DoseRow>[] = [
  { head: '当日涨停家数', w: '96px', render: r => <span className="text-[12px] font-medium text-foreground">{r.bucket}</span> },
  { head: '样本', render: r => <span className="font-mono text-[11.5px] tabular-nums text-muted">{num(r.n)}</span> },
  {
    head: '未来 5 日',
    render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.x5))}>{sgn(r.x5)}<span className="ml-1 text-[10px] text-muted">t {sgn(r.x5t, 2)}</span></span>,
  },
  {
    head: '未来 20 日',
    render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.x20))}>{sgn(r.x20)}<span className="ml-1 text-[10px] text-muted">t {sgn(r.x20t, 2)}</span></span>,
  },
  {
    head: '2025 x20',
    render: r => <span className={cn('font-mono text-[11.5px] tabular-nums', tone(r.y2025 ?? 0))}>{r.y2025 == null ? '—' : sgn(r.y2025)}</span>,
  },
  {
    head: '2026 x20',
    render: r => <span className={cn('font-mono text-[11.5px] tabular-nums', tone(r.y2026 ?? 0))}>{r.y2026 == null ? '—' : sgn(r.y2026)}</span>,
  },
  { head: '波动比', align: 'right', render: r => <span className="font-mono text-[11.5px] tabular-nums text-muted">{r.vr.toFixed(3)}</span> },
]

const pairCols: Col<PairRow>[] = [
  { head: '读数', w: '24%', render: r => <span className="text-[12px] font-medium text-foreground">{r.label}</span> },
  { head: '主线榜前 30', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.unit === 'bp' ? sgn(r.real) : r.real.toFixed(2)}</span> },
  { head: '同日随机 30', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{r.unit === 'bp' ? sgn(r.placebo) : r.placebo.toFixed(2)}</span> },
  {
    head: '配对差 · t',
    align: 'right',
    render: r => (
      <span className={cn('font-mono text-[12px] tabular-nums', tone(r.diff))}>
        {r.unit === 'bp' ? sgn(r.diff) : r.diff.toFixed(2)}
        <span className="ml-1.5 text-[10.5px] text-muted">/ {r.t == null ? '—' : sgn(r.t, 2)}</span>
      </span>
    ),
  },
  { head: '负向日', align: 'right', render: r => <span className="font-mono text-[11.5px] tabular-nums text-muted">{r.win.toFixed(0)}%</span> },
]

const tierCols: Col<TierRow>[] = [
  { head: '名次', w: '30%', render: r => <span className="text-[12px] text-foreground">{r.tier}</span> },
  { head: '20 日配对差', align: 'right', render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.diff))}>{sgn(r.diff)}bp</span> },
  { head: 't', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{sgn(r.t, 2)}</span> },
  { head: '天数', align: 'right', render: r => <span className="font-mono text-[11.5px] tabular-nums text-muted">{r.days}</span> },
]

const evBucketCols: Col<EventBucketRow>[] = [
  { head: '解禁 / 流通', w: '14%', render: r => <span className="text-[12px] font-medium text-foreground">{r.bucket}</span> },
  { head: '事件', render: r => <span className="font-mono text-[11.5px] tabular-nums text-muted">{num(r.events)}</span> },
  {
    head: '解禁前 20 日',
    align: 'right',
    render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.pre))}>{sgn(r.pre)}<span className="ml-1 text-[10px] text-muted">t {sgn(r.preT, 2)}</span></span>,
  },
  {
    head: 'DiD 前',
    align: 'right',
    render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.didPre))}>{sgn(r.didPre)}</span>,
  },
]

const evMomCols: Col<EventMomentumRow>[] = [
  { head: '前期 120 日动量', w: '20%', render: r => <span className="text-[12px] text-foreground">{r.terc}</span> },
  { head: '≥10% 组', align: 'right', render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.hi))}>{sgn(r.hi)}</span> },
  { head: '<1% 组', align: 'right', render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.lo))}>{sgn(r.lo)}</span> },
  { head: '差（≥10% − <1%）', align: 'right', render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.diff))}>{sgn(r.diff)}</span> },
  {
    head: '前期超额',
    align: 'right',
    render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{sgn(r.m120Hi, 1)}% / {sgn(r.m120Lo, 1)}%</span>,
  },
]

const evKindCols: Col<EventKindRow>[] = [
  { head: '限售类型（≥10% 桶）', w: '26%', render: r => <span className="text-[12px] text-foreground">{r.kind}</span> },
  { head: '事件', align: 'right', render: r => <span className="font-mono text-[11.5px] tabular-nums text-muted">{num(r.events)}</span> },
  {
    head: '解禁前 20 日',
    align: 'right',
    render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.pre))}>{sgn(r.pre)}<span className="ml-1 text-[10px] text-muted">t {sgn(r.preT, 2)}</span></span>,
  },
]

const killCols: Col<KillRow>[] = [
  { head: '#', w: '44px', render: r => <span className="font-mono text-[11px] text-muted">{r.id}</span> },
  { head: '待检验的说法', w: '22%', render: r => <span className="text-[12px] text-foreground">{r.claim}</span> },
  { head: '看到什么就认它死', render: r => <span className="text-[12px] leading-[1.7] text-secondary">{r.kill}</span> },
]

export function Narrative() {
  const q = useQuery({
    queryKey: QK.narrativeLedger,
    queryFn: api.narrativeLedger,
    staleTime: 60_000,
  })
  const d: NarrativeLedger | undefined = q.data
  const b = d?.blocks

  const liveRows = LIVE_BLOCKS.map(x => ({ ...x, block: b?.[x.key as keyof NarrativeLedger['blocks']] }))

  return (
    <div className="mx-auto max-w-[1180px] px-6 pb-24 pt-8">
      <header className="flex flex-col gap-6 border-b border-border pb-7 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <div className="text-[11px] font-medium tracking-[0.18em] text-muted">
            研究结论 · 叙事 · 收据簿（{META.asOf}）
          </div>
          <h1 className="mt-3 text-[30px] font-semibold leading-[1.15] tracking-[-0.02em] text-foreground">
            叙事<span className="mx-2.5 font-light text-muted/40">/</span>一张收据，不是一个理由
          </h1>
          <p className="mt-3 max-w-[46em] text-[14px] leading-[1.8] text-secondary">
            主流叙事的常见读法是「谁在涨、为什么涨」。那是资产端：按市价计价，每天定完，没有余额。
            换成 /chip 的读法——一笔约束要产生漂移，需要「未结算的余额」和「结算周期 &gt; 0」——
            叙事的结算周期是 0：涨停在收盘那一刻撮合完，溢价提前支付在隔夜。
            于是热度只剩两件事可读：<span className="text-foreground">谁站在最后一棒</span>，
            以及<span className="text-foreground">钱从哪条腿来</span>。
          </p>
        </div>
        <div className="shrink-0 text-[11px] leading-[1.9] text-muted lg:text-right">
          <div className="font-mono tabular-nums">冻结读数 {META.asOf}</div>
          <div>{META.sample}</div>
          <div>{META.universe}</div>
          <div className="text-warning">三条腿 · 零个信号 · 0 可执行</div>
        </div>
      </header>

      {/* 结论带：左结论、右动作。整页只有这两件事需要先看到。 */}
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
          <div key={s.label}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[11px] tracking-[0.12em] text-muted">{s.label}</span>
              <Tag tone={s.verdict}>{s.value}</Tag>
            </div>
            <p className="mt-2 text-[12px] leading-[1.7] text-secondary">{s.note}</p>
          </div>
        ))}
      </div>

      {/* 目录：13 节的长页，没有它读者只能滚。 */}
      <nav className="mt-6 flex flex-wrap gap-x-5 gap-y-2 text-[12px] text-muted">
        {[
          ['reframe', '一 · 换表'],
          ['law', '一 · 定律'],
          ['legs', '一 · 三条腿'],
          ['dose', '二 · 剂量反应'],
          ['pair', '二 · 配对安慰剂'],
          ['event', '二 · 事件腿'],
          ['bounds', '二 · 边界'],
          ['live', '三 · 今日账本'],
          ['judge', '四 · 判据'],
          ['kill', '四 · 杀死'],
          ['scope', '五 · 能/不能'],
        ].map(([id, label]) => (
          <a key={id} href={`#${id}`} className="transition-colors hover:text-foreground">{label}</a>
        ))}
      </nav>

      {/* ---------------------------------------------------------------- 卷一 */}
      <div className="mt-14">
        <div className="text-[11px] font-medium tracking-[0.18em] text-muted">卷一 · 换表</div>
      </div>

      <Section
        id="reframe"
        no="1A"
        title="涨幅榜是资产端，收据才是负债端"
        first
        source="口径迁移自 docs/structural-edge/chip-ap.md §2"
        lead={
          <>
            同一件事有两张表。涨幅表记「谁涨了多少」——市价每天重估，信息已经在价格里；
            收据记「谁在什么价位接了多少、还要接多久」——面值写死，没到期就还是一笔账。
            前一张表能解释过去，后一张表才有余额。
          </>
        }
      >
        <Callout tone="warn" label="两条命题">
          <p>
            命题 1：叙事的定价不在「故事有多好」，在<span className="text-foreground">「还有多少人必须在这个价位买」</span>。
          </p>
          <p className="mt-1.5">
            命题 2：一个约束是否留下可交易残差，取决于它多久被结算一次。结算周期趋近于 0 时，
            漂移被提前支付，剩下的只有回吐。
          </p>
        </Callout>
        <p className="mt-5 max-w-[46em] text-[13px] leading-[1.8] text-muted">
          这条定律把 /chip 的两个相反结果收成一个：两融（逐日盯市）无漂移，质押贴线（事件盯市）
          +291bp。搬到大盘，问题从「哪条叙事会涨」变成「这条叙事用哪种频率结算」。
        </p>
      </Section>

      <Section
        id="law"
        no="1B"
        title="一条定律：结算频率"
        source="hotmoney-quant-game / chip-ap §6 / state-family-screen"
        lead="三种结算频率，对应三类可观测对象。页面后续所有读数都落在这张表的三行里。"
      >
        <DataTable cols={lawCols} rows={LAW} rowKey={r => r.cadence} minWidth={900} swipeHint />
      </Section>

      <Section
        id="legs"
        no="1C"
        title="三条腿：把「叙事」拆成能分别证伪的对象"
        source="本仓已完成的检验，出处见读数列"
        lead={
          <>
            热度是一个复合词：它同时指涨停梯队、ETF 申赎、事件驱动。不拆开就没法证伪——
            一条腿为零会被另一条腿的噪声掩盖。拆开后三条都能单独杀死。
          </>
        }
      >
        <DataTable cols={legCols} rows={LEGS} rowKey={r => r.leg} minWidth={1000} swipeHint />
        <Callout tone="muted" label="结论">
          三条腿里只有事件腿有符号：解禁前 20 日 −276bp（≥10% 组），而且漂移在到期前就付完了。
          叙事腿仍是零，日结算腿只定价方差。所以这一页产出的是<span className="text-foreground">一条排除规则 + 一份排除清单</span>，
          不是候选。
        </Callout>
      </Section>

      {/* ---------------------------------------------------------------- 卷二 */}
      <div className="mt-16">
        <div className="text-[11px] font-medium tracking-[0.18em] text-muted">卷二 · 读数（本次新跑）</div>
      </div>

      <Section
        id="dose"
        no="2A"
        title="剂量反应：涨停越多，未来越差——差多少"
        source={`${META.source} · ${META.convention}`}
        asOf="2026-11-13"
        lead={
          <>
            如果热度是「未付的账单」，涨停越多的概念未来应该越强；如果是「已付的收据」，应该反过来。
            按当日涨停家数分六档，看未来 5/20 日相对同日全体概念中位的超额。
            绝对水平不可比（概念截面右偏，均值天然高于中位），可读的只有档与档之间的差。
          </>
        }
      >
        <DataTable cols={doseCols} rows={DOSE} rowKey={r => r.bucket} minWidth={820} swipeHint />
        <Callout tone="warn" label="怎么读">
          <p>
            从 1 档到 ≥10 档是 <span className="font-mono tabular-nums text-foreground">−17.3bp</span>；
            2026 分段单调（36.6 → 11.3），2025 不单调（40.0 → 28.2，中间 15.7）。
            方向一致、剂量关系只有一年成立、量级不到单边成本 20bp 的两倍。
          </p>
          <p className="mt-1.5">
            波动比一列几乎不动（1.025–1.045）：热度连方差都不定价。所以「热度高 ⇒ 波动大 ⇒ 可以做波动率」
            这条路也堵上了。
          </p>
        </Callout>
      </Section>

      <Section
        id="pair"
        no="2B"
        title="配对安慰剂：不是「随机选 30 个也一样」"
        source={`${META.source} · 每日配对 371 天 · 随机 50 次/日`}
        asOf="2026-11-13"
        lead={
          <>
            裸 t 值会被市场共同因子冲掉。这里按日配对：同一天，主线榜前 30 与随机抽 30 个概念做差，
            再对 371 个配对差做 NW(5) t。随机抽样只保留「选到哪些概念」这一个自由度。
          </>
        }
      >
        <DataTable cols={pairCols} rows={PAIR} rowKey={r => r.key} minWidth={720} />
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <DataTable
            cols={tierCols}
            rows={TIERS}
            rowKey={r => r.tier}
            reason="名次分档：榜一是否比榜 11–30 更有信息。三档的差都在 −33 ~ −41bp，榜一没有额外信息。"
            minWidth={420}
          />
          <div className="text-[12px] leading-[1.8] text-muted">
            <p>
              20 日配对差 −33.3bp（t −1.71），负向日 57%；5 日只有 −7.5bp（t −1.10）。
              效应随时间变长而变负，形态像回吐而不是反转——与「溢价提前支付」一致。
            </p>
            <p className="mt-2">
              但 t 全部不过 2，样本只有 1.5 年。这条读数的用途是<span className="text-foreground">否定「主线榜可以选方向」</span>，
              不是建立「反向做空主线」。
            </p>
          </div>
        </div>
      </Section>

      <Section
        id="event"
        no="2C"
        title="事件腿：解禁的抛压发生在到期之前"
        asOf="2026-06-30"
        source="events/lockup.parquet（东财 RPT_LIFT_STAGE，16,928 件）· analysis/_diag_narrative_events.py"
        lead={
          <>
            解禁是三条腿里唯一同时具备「余额、日期、方向」的约束：解禁股数 / 流通股数就是余额，
            FREE_DATE 就是结算日，方向只能是卖。按 /chip 的读法，它应该产生漂移——
            问题只是漂移发生在到期前还是到期后。
          </>
        }
      >
        <DataTable cols={evBucketCols} rows={EVENT_BUCKETS} rowKey={r => r.bucket} minWidth={880} swipeHint />
        <div className="mt-3 text-[11px] leading-[1.7] text-muted">
          pre20 = open[t−20] → close[t−1]；超额 = 相对同日全市场等权收益。
          DiD = 事件 − 同股票 ±120 交易日随机日（安慰剂）。事件扎堆，t 按日聚簇 NW(5)。
          解禁之后的窗口已从读数中撤下：刚过成本、方向只出现过一次，样本太薄。
        </div>
        <div className="mt-8 grid gap-x-8 gap-y-6 lg:grid-cols-2">
          <DataTable
            cols={evMomCols}
            rows={EVENT_MOMENTUM}
            rowKey={r => r.terc}
            reason="前期动量控制：高比例解禁的股票往往早就涨过。在同一「过去 120 日超额」分位内比较，差仍在 −196 ~ −408bp——不是均值回归。"
            minWidth={520}
          />
          <DataTable
            cols={evKindCols}
            rows={EVENT_KIND}
            rowKey={r => r.kind}
            reason="按限售类型拆：定增解禁、首发原股东、其他三类同向，说明不是某一种股份的 artifact。"
            minWidth={420}
          />
        </div>
        <Callout tone="warn" label="结论：提前支付，到期结算">
          <p>
            解禁前 20 日的超额随比例单调下降（−37 → −94 → −172 → −276bp）：抛压不是在解禁日释放的，
            而是在解禁日之前就被定价的。解禁之后走成什么样，本页不下结论——那一格刚过成本、方向只出现过一次，
            已经撤下。
          </p>
          <p className="mt-1.5">
            解禁效应是<span className="text-foreground">横截面</span>的，不是择时的：371 个交易日里，当日解禁市值 / 流通市值
            与概念未来 20 日超额的相关是 −0.006（几乎没有）。所以它只能用来筛掉个股，不能用来判断「今天该不该买」。
          </p>
          <p className="mt-1.5">
            可执行的只有排除：<span className="text-foreground">未来 20 日解禁占流通比 ≥10% 的股票，不进候选池。</span>
            也不建议做空——融券成本与借券可得性都不在这个读数里。
          </p>
        </Callout>
        <Callout tone="muted" label="剔除事件股后：结论不变">
          把「未来 20 日内有解禁 / 过去 20 日内有定增上市」的股票从概念等权收益里去掉，371 天逐日重算：
          剂量跨度 {EXCLUSION_CHECK.doseAll}bp → {EXCLUSION_CHECK.doseExcluded}bp，逐日差中位{' '}
          {EXCLUSION_CHECK.deltaMed > 0 ? '+' : ''}{EXCLUSION_CHECK.deltaMed}bp。
          叙事腿的零不是靠事件股撑着的。逐日表见{' '}
          <span className="font-mono text-[11px]">docs/structural-edge/narrative-371d.csv</span>。
        </Callout>
        <Callout tone="muted" label="定增：面值存在，但不产生漂移">
          近 12 个月 318 单 / 295 只 / 募资 {SEO_READ.raiseYi.toLocaleString('zh-CN')} 亿；
          破发率 {SEO_READ.breakRate}%，相对发行价中位 {SEO_READ.vsIssueMed > 0 ? '+' : ''}{SEO_READ.vsIssueMed}%。
          发行价是合约面值，但锁定期内不能卖——它只在解禁那一天变成供给，所以它的符号并入上表，不单独成信号。
        </Callout>
      </Section>

      <Section id="bounds" no="2D" title="边界：五条会改变结论的东西" source="写在这里，是为了下一次不用重新想">
        <ol className="mt-2 space-y-2.5">
          {BOUNDS.map((t, i) => (
            <li key={t} className="flex gap-3 text-[13px] leading-[1.8] text-secondary">
              <span className="mt-[3px] font-mono text-[11px] tabular-nums text-muted">{String(i + 1).padStart(2, '0')}</span>
              <span>{t}</span>
            </li>
          ))}
        </ol>
        <Callout tone="muted" label="证据等级">
          B。可复算、有安慰剂、有剂量反应，但只有一个 regime、成分是快照、|t| &lt; 2。
          够做排除，不够做方向。
        </Callout>
      </Section>

      {/* ---------------------------------------------------------------- 卷三 */}
      <div className="mt-16 flex flex-wrap items-baseline justify-between gap-3">
        <div className="text-[11px] font-medium tracking-[0.18em] text-muted">卷三 · 今日账本（实时）</div>
        <Cadence kind="daily" note={d?.as_of ? `最新数据日 ${d.as_of}` : undefined} />
      </div>

      <Section
        id="live"
        no="3A"
        title="温度：今天有多热"
        asOf={b?.temp?.as_of ?? undefined}
        source="regime_history（唯一实现）· 分位取 250 日"
        lead={
          <>
            温度只回答「现在的赔率在什么位置」，不回答方向。同一档温度在上行段与下行段的后验完全不同，
            所以它只能进仓位，不能进选股。
          </>
        }
      >
        {q.isLoading ? (
          <Empty text="正在读取账本…" />
        ) : !b?.temp?.ok ? (
          <Empty text={`温度块不可用：${b?.temp?.error ?? '接口未返回'}`} />
        ) : (
          <>
            <StatGrid
              cols={4}
              items={[
                {
                  k: '涨停家数',
                  v: num(b.temp.limit_up),
                  d: `250 日分位 ${b.temp.pct?.limit_up ?? '—'}% · 炸板 ${num(b.temp.broken_limit)}`,
                },
                {
                  k: '最高连板',
                  v: num(b.temp.max_consecutive),
                  d: `梯队完整度 ${(b.temp.ladder_completeness ?? 0).toFixed(2)} · 二板以上 ${num(b.temp.ge2_count)}`,
                },
                {
                  k: '封板率',
                  v: b.temp.seal_rate == null ? '—' : `${(b.temp.seal_rate * 100).toFixed(1)}%`,
                  d: `250 日分位 ${b.temp.pct?.seal_rate ?? '—'}%`,
                },
                {
                  k: '涨 / 跌 家数',
                  v: `${num(b.temp.up_count)} / ${num(b.temp.down_count)}`,
                  d: `成交额 ${num(b.temp.total_amount_yi)} 亿 · 分位 ${b.temp.pct?.total_amount ?? '—'}%`,
                },
              ]}
            />
            <div className="mt-6 grid gap-x-8 gap-y-5 sm:grid-cols-3">
              <div className="border-t border-border pt-3">
                <div className="text-[12px] text-muted">情绪周期阶段</div>
                <div className="mt-1.5 text-[15px] font-medium text-foreground">
                  {b.temp.phase ? MARKET_PHASE_LABELS[b.temp.phase as MarketPhase] ?? b.temp.phase : '—'}
                  <span className="ml-2 text-[12px] font-normal text-muted">
                    {b.temp.state ? REGIME_STATE_LABELS[b.temp.state as RegimeState] ?? b.temp.state : '—'}
                  </span>
                </div>
                <RankBar pct={b.temp.pct?.score} label="情绪分位" />
              </div>
              <div className="border-t border-border pt-3">
                <div className="text-[12px] text-muted">涨停分位</div>
                <div className="mt-1.5 font-mono text-[15px] tabular-nums text-foreground">
                  {b.temp.pct?.limit_up ?? '—'}%
                </div>
                <RankBar pct={b.temp.pct?.limit_up} />
              </div>
              <div className="border-t border-border pt-3">
                <div className="text-[12px] text-muted">成交额分位</div>
                <div className="mt-1.5 font-mono text-[15px] tabular-nums text-foreground">
                  {b.temp.pct?.total_amount ?? '—'}%
                </div>
                <RankBar pct={b.temp.pct?.total_amount} />
              </div>
            </div>
            {(b.temp.pct?.limit_up ?? 0) >= 99 && (
              <Callout tone="warn" label="今天是极值日">
                涨停家数在 250 个交易日里排到 {b.temp.pct?.limit_up}% 分位。极值日不能外推：
                它本身会改写后面 250 天的分位，也会让「按温度调仓位」的规则在次日失效。
                这一天的正确用法是记下来，作为下一次的分母。
              </Callout>
            )}
          </>
        )}
      </Section>

      <Section
        id="mainline"
        no="3B"
        title="主线榜：今天钱在哪些概念里排梯队"
        asOf={b?.mainline?.as_of ?? undefined}
        source="mainline_history（唯一实现）+ 概念等权涨幅 / 成交额占比"
        lead={
          <>
            主线分 = 涨停数 0.35 + 最高板 0.25 + 梯队档位 0.25 + 二板宽度 0.15 的截面 rank 加权。
            它是「今天涨停生态长在哪」的记账，不是明天的推荐。5 日 / 20 日涨幅放在这里，
            是为了让读者看到「榜上是已经涨过的，还是刚开始的」。
          </>
        }
      >
        {q.isLoading ? (
          <Empty text="正在读取主线…" />
        ) : !b?.mainline?.ok ? (
          <Empty text={`主线块不可用：${b?.mainline?.error ?? '接口未返回'}`} />
        ) : (
          <DataTable
            cols={[
              { head: '#', w: '36px', render: r => <span className="font-mono text-[11px] text-muted">{r.rank}</span> },
              { head: '概念', w: '20%', render: r => <span className="text-[12.5px] font-medium text-foreground">{r.member}</span> },
              { head: '涨停', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.limit_up_count}</span> },
              { head: '最高板', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.max_boards}</span> },
              { head: '二板+', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{r.ge2_count}</span> },
              { head: '龙头', w: '13%', render: r => <span className="text-[12px] text-secondary">{r.leader_name || r.leader_symbol || '—'}</span> },
              {
                head: '5 日',
                align: 'right',
                render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.r5_pct))}>{sgn(r.r5_pct, 2)}%</span>,
              },
              {
                head: '20 日',
                align: 'right',
                render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.r20_pct))}>{sgn(r.r20_pct, 2)}%</span>,
              },
              {
                head: '额占比',
                align: 'right',
                render: r => <span className="font-mono text-[11.5px] tabular-nums text-muted">{r.amt_share_pct.toFixed(2)}%</span>,
              },
            ]}
            rows={b.mainline.rows ?? []}
            rowKey={r => `${r.rank}-${r.member}`}
            minWidth={860}
            swipeHint
            reason="额占比 = 该概念成员当日成交额 / 全部概念成员成交额（同一批成员，分母不是全市场）。占比 < 1% 的概念装不下大钱——容量在下一节。"
          />
        )}
      </Section>

      <Section
        id="flows"
        no="3C"
        title="资金四路：钱从哪条腿来"
        source="margin / dragon_tiger / block_trade / kline_etf_daily"
        lead={
          <>
            四条腿的结算频率不同，所以它们不该被加成一个「净流入」。两融是逐日盯市（无余额），
            龙虎榜是已成交（已结清），大宗是折价转让（对手方必须转手），ETF 占比是叙事的逐日结算工具。
          </>
        }
      >
        {q.isLoading ? (
          <Empty text="正在读取资金…" />
        ) : !b?.flows?.ok ? (
          <Empty text="资金块不可用" />
        ) : (
          <Tiles cols={2}>
            <Tile
              lead={b.flows.margin?.as_of ?? '—'}
              title="两融余额"
              body={
                <>
                  {num(b.flows.margin?.balance_yi)} 亿 · 占流通市值 {b.flows.margin?.pct_float ?? '—'}%
                  （250 日分位 {b.flows.margin?.pct_float_rank ?? '—'}%）
                </>
              }
              foot={
                <span className={cn('font-mono text-[12px] tabular-nums', tone(b.flows.margin?.net5_yi ?? 0))}>
                  近 5 日净买 {sgn(b.flows.margin?.net5_yi ?? 0)} 亿
                </span>
              }
            />
            <Tile
              lead={b.flows.dragon?.as_of ?? '—'}
              title="龙虎榜净买"
              body={
                <>
                  近 {b.flows.dragon?.days ?? 0} 个交易日 · 上榜 {num(b.flows.dragon?.stocks)} 只次
                </>
              }
              foot={
                <span className={cn('font-mono text-[12px] tabular-nums', tone(b.flows.dragon?.net_yi ?? 0))}>
                  {sgn(b.flows.dragon?.net_yi ?? 0, 2)} 亿
                </span>
              }
            />
            <Tile
              lead={b.flows.block?.as_of ?? '—'}
              title="大宗交易"
              body={
                <>
                  近 5 日 {num(b.flows.block?.deals)} 笔 · {num(b.flows.block?.amt5_yi, 1)} 亿
                </>
              }
              foot={
                <span className="font-mono text-[12px] tabular-nums text-secondary">
                  折价笔 {b.flows.block?.neg_share ?? '—'}% · 折价中位 {b.flows.block?.neg_med == null ? '—' : `${sgn(b.flows.block.neg_med, 2)}%`}
                </span>
              }
            />
            <Tile
              lead={b.flows.etf?.as_of ?? '—'}
              title="ETF 成交额占比"
              body={
                <>
                  ETF 成交 {num(b.flows.etf?.amt_yi)} 亿 / 全市场 {num(b.flows.etf?.market_amt_yi)} 亿
                </>
              }
              foot={
                <span className="font-mono text-[12px] tabular-nums text-secondary">
                  占成交额 {b.flows.etf?.share_pct ?? '—'}%
                </span>
              }
            />
          </Tiles>
        )}
        <Callout tone="muted" label="为什么四路不能相加">
          两融余额的变化里，价格重估与真实买卖混在一起（恒等式闸门）；龙虎榜只覆盖异动上榜的股票，
          分母不是全市场；大宗是转让不是抛压；ETF 占比的分母（个股成交额）会随行情缩放。
          四路各读趋势，不做合计。
        </Callout>
      </Section>

      <Section
        id="index"
        no="3D"
        title="指数波动：结算发生在隔夜还是日内"
        asOf={b?.index?.as_of ?? undefined}
        source="kline_index_daily（近端）+ backfill/index_daily_full（分位）"
        lead={
          <>
            隔夜腿 = 今开 / 昨收 − 1，日内腿 = 今收 / 今开 − 1。若叙事在收盘后结算，溢价应集中在隔夜；
            日内腿是对隔夜的支付。20 日年化波动的分位用全历史算，不是用近一年。
          </>
        }
      >
        {q.isLoading ? (
          <Empty text="正在读取指数…" />
        ) : !b?.index?.ok ? (
          <Empty text={`指数块不可用：${b?.index?.error ?? '接口未返回'}`} />
        ) : (
          <DataTable
            cols={[
              { head: '指数', w: '18%', render: r => <span className="text-[12.5px] font-medium text-foreground">{r.name}</span> },
              { head: '收盘', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.close.toFixed(2)}</span> },
              {
                head: '当日',
                align: 'right',
                render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.ret1_pct))}>{sgn(r.ret1_pct, 2)}%</span>,
              },
              { head: '20 日波动', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.vol20_pct.toFixed(1)}%</span> },
              { head: '波动分位', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{r.vol_rank ?? '—'}%</span> },
              {
                head: '5 日隔夜',
                align: 'right',
                render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.on5_pct))}>{sgn(r.on5_pct, 2)}%</span>,
              },
              {
                head: '5 日日内',
                align: 'right',
                render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.id5_pct))}>{sgn(r.id5_pct, 2)}%</span>,
              },
            ]}
            rows={b.index.rows ?? []}
            rowKey={r => r.symbol}
            minWidth={760}
            swipeHint
            reason="波动分位用该指数全历史（上证 8,772 日、创业板 3,997 日）算；隔夜/日内是 5 个交易日的复利，不是均值。"
          />
        )}

        {b?.index?.ok && (b.index.etf_rows ?? []).length > 0 && (
          <div className="mt-8">
            <div className="text-[12px] font-medium text-foreground">宽基 ETF：逐日结算的那一层</div>
            <p className="mt-1.5 max-w-[46em] text-[12px] leading-[1.7] text-muted">
              宽基 ETF 的份额申赎在收盘完成，折溢价当天被套利抹平，所以它读不出方向。
              可以读的是两件事：波动分位（拥挤度）与 5 日跟踪偏离（ETF 收益 − 标的指数收益）。
              偏离为正，说明二级市场在抢筹、把价格推到净值之上——这是申赎套利还没做完的痕迹。
            </p>
            <DataTable
              cols={[
                { head: 'ETF', w: '18%', render: r => <span className="text-[12.5px] font-medium text-foreground">{r.name}</span> },
                { head: '标的', w: '12%', render: r => <span className="font-mono text-[10.5px] text-muted">{r.index_symbol}</span> },
                { head: '收盘', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.close.toFixed(3)}</span> },
                {
                  head: '当日',
                  align: 'right',
                  render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.ret1_pct))}>{sgn(r.ret1_pct, 2)}%</span>,
                },
                { head: '20 日波动', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums">{r.vol20_pct.toFixed(1)}%</span> },
                { head: '分位', align: 'right', render: r => <span className="font-mono text-[12px] tabular-nums text-muted">{r.vol_rank ?? '—'}%</span> },
                {
                  head: '5 日',
                  align: 'right',
                  render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.r5_pct))}>{sgn(r.r5_pct, 2)}%</span>,
                },
                {
                  head: '5 日跟踪偏离',
                  align: 'right',
                  render: r => <span className={cn('font-mono text-[12px] tabular-nums', tone(r.track5_pp))}>{sgn(r.track5_pp, 2)}pp</span>,
                },
                { head: '成交额', align: 'right', render: r => <span className="font-mono text-[11.5px] tabular-nums text-muted">{num(r.amt_yi, 1)} 亿</span> },
              ]}
              rows={b.index.etf_rows ?? []}
              rowKey={r => r.symbol}
              minWidth={820}
              reason="ETF 日线本地只保留 260 个分区（滚动一年），这里只取近 130 个，所以波动分位是「近 130 日分位」，与上表的全历史分位不同口径。"
            />
          </div>
        )}
      </Section>

      <Section
        id="words"
        no="3E"
        title="词频：市场在用哪些词讲今天的事"
        asOf={b?.words?.as_of ?? undefined}
        source="dragon_tiger 上榜理由（券商/交易所标准化记录）· 近 5 个交易日"
        lead={
          <>
            这是最接近「叙事」本身的可观测量：上榜理由是被反复引用的因果标签。
            它不预测涨跌，但它告诉你今天的钱集中在哪个故事里——以及那个故事已经被多少只票分掉了。
          </>
        }
      >
        {q.isLoading ? (
          <Empty text="正在读取词频…" />
        ) : !b?.words?.ok ? (
          <Empty text={`词频块不可用：${b?.words?.error ?? '接口未返回'}`} />
        ) : (b.words.rows ?? []).length === 0 ? (
          <Empty text="近 5 个交易日没有上榜理由文本。" />
        ) : (
          <div className="mt-2 flex flex-wrap gap-x-3 gap-y-2.5">
            {(b.words.rows ?? []).map(w => (
              <span key={w.word} className="inline-flex items-baseline gap-2 border-b border-border/60 pb-1">
                <span className="text-[13px] text-foreground">{w.word}</span>
                <span className="font-mono text-[10.5px] tabular-nums text-muted">{w.n} 次</span>
                <span className={cn('font-mono text-[10.5px] tabular-nums', tone(w.net_yi))}>{sgn(w.net_yi, 2)} 亿</span>
              </span>
            ))}
          </div>
        )}
        <Callout tone="muted" label="口径">
          词频来自本地缓存的龙虎榜 JSON，只覆盖异动上榜的股票（近期每日 60–80 只）；
          净买额是该词命中的股票当日龙虎榜净买之和，同一只票命中多个词会重复计入，不能加总成全市场。
        </Callout>
      </Section>

      <Section
        id="events"
        no="3F"
        title="解禁日历：未来 45 日谁变成可卖"
        asOf={b?.events?.as_of ?? undefined}
        source="events/lockup.parquet + seo.parquet（analysis/_fetch_events.py 抓取）"
        lead={
          <>
            这是本页唯一的「前瞻」块，也是唯一直接对应排除规则的块：把未来 45 日的解禁按日排开，
            比例 ≥10% 的单独标出来——那些票在未来 20 日不进候选池。它不预测涨跌，只标出谁马上可以卖。
          </>
        }
      >
        {q.isLoading ? (
          <Empty text="正在读取解禁日历…" />
        ) : !b?.events?.ok ? (
          <Empty text={`事件块不可用：${b?.events?.error ?? '接口未返回'}`} />
        ) : (
          <>
            <StatGrid
              cols={3}
              items={[
                {
                  k: '未来 45 日解禁',
                  v: `${num(b.events.next?.n)} 只 / ${num(b.events.next?.mcap_yi, 1)} 亿`,
                  d: `分布在 ${b.events.next?.days ?? 0} 个交易日`,
                },
                {
                  k: '近 30 日已解禁',
                  v: `${num(b.events.recent30?.n)} 只 / ${num(b.events.recent30?.mcap_yi, 1)} 亿`,
                  d: '这部分已经解禁完毕，只作为已发生的供给记账',
                },
                {
                  k: '近 90 日定增上市',
                  v: `${num(b.events.seo90?.n)} 单 / ${num(b.events.seo90?.raise_yi)} 亿`,
                  d: '锁定期 0.5–3 年，到期后并入上面的解禁日历',
                },
              ]}
            />
            <div className="mt-8 grid gap-x-8 gap-y-6 lg:grid-cols-2">
              <div>
                <div className="text-[12px] font-medium text-foreground">按日解禁市值（前 10 天）</div>
                <div className="mt-2 space-y-1.5">
                  {(b.events.next?.by_day ?? [])
                    .slice()
                    .sort((x, y) => y.mcap_yi - x.mcap_yi)
                    .slice(0, 10)
                    .map(x => {
                      const max = Math.max(...(b.events?.next?.by_day ?? []).map(y => y.mcap_yi), 1)
                      return (
                        <div key={x.d} className="flex items-center gap-3">
                          <span className="w-[76px] shrink-0 font-mono text-[11px] tabular-nums text-muted">{x.d.slice(5)}</span>
                          <span className="h-[6px] flex-1 rounded-full bg-elevated">
                            <span className="block h-full rounded-full bg-accent/60" style={{ width: `${(x.mcap_yi / max) * 100}%` }} />
                          </span>
                          <span className="w-[86px] shrink-0 text-right font-mono text-[11px] tabular-nums text-secondary">
                            {x.mcap_yi.toFixed(1)} 亿
                          </span>
                          <span className="w-[42px] shrink-0 text-right font-mono text-[10.5px] tabular-nums text-muted">{x.n} 只</span>
                        </div>
                      )
                    })}
                </div>
              </div>
              <DataTable
                cols={[
                  { head: '日期', w: '82px', render: r => <span className="font-mono text-[11px] tabular-nums text-muted">{r.date.slice(5)}</span> },
                  {
                    head: '股票 / 类型',
                    render: r => (
                      <span>
                        <span className="text-[12px] text-foreground">{r.name}</span>
                        <span className="ml-1.5 text-[10px] text-muted">{kindShort(r.kind)}</span>
                      </span>
                    ),
                  },
                  { head: '解禁市值', align: 'right', render: r => <span className="font-mono text-[11.5px] tabular-nums">{r.mcap_yi.toFixed(1)} 亿</span> },
                  {
                    head: '占流通',
                    align: 'right',
                    render: r => (
                      <span className={cn('font-mono text-[11.5px] tabular-nums', r.ratio_pct >= 10 ? 'text-danger' : 'text-muted')}>
                        {r.ratio_pct.toFixed(1)}%
                      </span>
                    ),
                  },
                ]}
                rows={b.events.top ?? []}
                rowKey={r => `${r.date}-${r.symbol}`}
                minWidth={430}
                reason="未来 45 日解禁市值最大的 8 单。占流通 ≥10% 标红——那是排除规则触发的阈值。"
              />
            </div>
          </>
        )}
      </Section>

      {/* ---------------------------------------------------------------- 卷四 */}
      <div className="mt-16">
        <div className="text-[11px] font-medium tracking-[0.18em] text-muted">卷四 · 判据</div>
      </div>

      <Section
        id="judge"
        no="4A"
        title="排序可读，水平不可读"
        source="/chip 卷六的同一条教训"
        lead={
          <>
            四种「贴线」口径给出 +141 / +21 / −42 / −54，水平全不可识别，但排序 8/8 一致。
            叙事这边是同一个病：温度、额占比、涨停数都没有唯一的水平刻度，
            但「更热 / 更冷」的排序在同年份内是稳定的。所以本页只做两件事——
            比同一时点的相对位置、比同一概念的前后变化；不把任何绝对值当阈值。
          </>
        }
      >
        <div className="mt-2 grid gap-6 sm:grid-cols-2">
          <div className="border-t border-border pt-3">
            <div className="text-[12px] font-medium text-foreground">可以用</div>
            <ul className="mt-2 space-y-1.5 text-[12px] leading-[1.7] text-secondary">
              <li>· 同一时点的横截面分位（今天比过去 250 天热多少）</li>
              <li>· 同一概念的时间序列变化（额占比在升还是在降）</li>
              <li>· 三条腿的相对强弱（哪条腿在动）</li>
            </ul>
          </div>
          <div className="border-t border-border pt-3">
            <div className="text-[12px] font-medium text-foreground">不能用</div>
            <ul className="mt-2 space-y-1.5 text-[12px] leading-[1.7] text-secondary">
              <li>· 「涨停 &gt; N 家就是主线」这类绝对阈值</li>
              <li>· 把额占比、封板率当收益预测</li>
              <li>· 跨年份比较温度（口径与样本都在变）</li>
            </ul>
          </div>
        </div>
        <Callout tone="warn" label="容量">
          概念容量 = 成交额占比 × 全市场成交额。占比 1% 的概念在 2 万亿成交额里是 200 亿，
          扣掉龙头与涨停板上的不可成交量，可执行的部分更小。容量 &lt; 3 亿的题材不进任何组合——
          这条来自温和 ramp 的容量否决（footprint-gentle-ramp）。
        </Callout>
      </Section>

      <Section
        id="kill"
        no="4B"
        title="杀死条件：七条，写在这里就不用再争"
        source="与 /chip 卷五 §5、卷六 §6E 同一格式"
        lead="每一条都给出「看到什么就认它死」。没有杀死条件的结论不是结论，是偏好。"
      >
        <DataTable cols={killCols} rows={KILL} rowKey={r => r.id} minWidth={760} />
      </Section>

      {/* ---------------------------------------------------------------- 卷五 */}
      <Section
        id="scope"
        no="5"
        title="这页能回答与不能回答的"
        lead="两列都写。只有左列时，读者会把它当信号源；只有右列时，它看起来像什么都没说。"
      >
        <div className="grid gap-6 sm:grid-cols-2">
          <div className="border-t border-border pt-3">
            <div className="text-[12px] font-medium text-foreground">能</div>
            <ul className="mt-2 space-y-1.5 text-[12px] leading-[1.7] text-secondary">
              {SCOPE.can.map(x => <li key={x}>· {x}</li>)}
            </ul>
          </div>
          <div className="border-t border-border pt-3">
            <div className="text-[12px] font-medium text-foreground">不能</div>
            <ul className="mt-2 space-y-1.5 text-[12px] leading-[1.7] text-muted">
              {SCOPE.cannot.map(x => <li key={x}>· {x}</li>)}
            </ul>
          </div>
        </div>
        <div className="mt-8 border-t border-border pt-4 text-[11px] leading-[1.8] text-muted">
          <div className="font-mono tabular-nums">冻结读数 {META.asOf} · {META.convention}</div>
          <div>{META.source}</div>
          <div className="mt-1">{d?.disclaimer ?? '四个数据源时滞不同，按块标注 as_of。'}</div>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
            {liveRows.map(x => (
              <span key={x.key} className="font-mono tabular-nums">
                {x.title} {x.block?.as_of ?? '—'}
                {x.block && !x.block.ok ? <span className="ml-1 text-warning">缺</span> : null}
              </span>
            ))}
          </div>
        </div>
      </Section>
    </div>
  )
}
