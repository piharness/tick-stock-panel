/**
 * 「证据」章 —— 证据、验收与组合基准。
 *
 * 这一层回答「凭什么信」，顺序按「先打折，再看图」排：
 *   01 证据强度（它会推翻你对下面所有图的过度解读）
 *   02–03 位置/形态、广度、久期（阈值的来历）
 *   04 仓位择时的验收（原「仓位艺术」的正文：六种实现、净值、五项验收、前视陷阱）
 *   05 组合层（同一条腿放进账户，还剩多少）
 *   06 两个「再等等」（不构成的暂缓理由）
 *
 * 为什么不把 04 放进反证：它不是「已被否掉所以收起来」，而是每天都在被登记的反例 ——
 * 收进反证就没人看了，而这一页的全部价值在于让它每天被看见一次。
 */
import { useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import type { EtfWatchPayload, PositionArtPayload } from '@/lib/api'
import { duration, evidenceNote, grade, portfolio, position, shape, sizing } from './data'
import { CrossLink, TabLayout } from './Shell'
import { breadthReading, v1Reading } from './live'
import {
  Cadence, Callout, Chart, ChartCard, DataTable, type Col, fmtBp, Section, StatGrid, Tag, useAxis,
} from './ui'

const CONFIRM_CHOICES = [0, 3, 5, 10, 15, 20] as const

export function EvidencePanel({
  payload, art, artErr, artFetching, confirmDays, onConfirmDays, onRetry,
}: {
  payload: EtfWatchPayload | null
  art: PositionArtPayload | null
  artErr: string
  artFetching: boolean
  confirmDays: number
  onConfirmDays: (n: number) => void
  onRetry: () => void
}) {
  return (
    <TabLayout
      tab="evidence"
      items={[
        { id: 'grade', no: '01', label: '证据强度' },
        { id: 'position', no: '02', label: '位置与形态' },
        { id: 'breadth', no: '03', label: '广度与久期' },
        { id: 'sizing-audit', no: '04', label: '仓位择时的验收' },
        { id: 'portfolio', no: '05', label: '组合层' },
        { id: 'waits', no: '06', label: '两个「再等等」' },
      ]}
    >
      <GradeBlock payload={payload} />
      <PositionBlock />
      <BreadthBlock payload={payload} />
      <SizingAuditBlock
        art={art}
        artErr={artErr}
        artFetching={artFetching}
        confirmDays={confirmDays}
        onConfirmDays={onConfirmDays}
        onRetry={onRetry}
      />
      <PortfolioBlock />
      <WaitBlock />
    </TabLayout>
  )
}

// ---------------------------------------------------------------- 01 强度

function GradeBlock({ payload }: { payload: EtfWatchPayload | null }) {
  const v1 = v1Reading(payload)
  // V1 进度是每日读数：data.ts 里对应卡片只留口径，值在这里填。
  const cards = grade.cards.map(c => (c.k === 'V1 账本' ? { ...c, v: v1?.progress ?? '—' } : c))
  return (
    <Section
      id="grade"
      no="01"
      title="证据强度：只到「未被证伪」"
      first
      lead="这一节是给下面所有结论打折用的：能算出 t 值，但算不出「下个周期还算数」。所以规则只能以预注册的形式存在，不能以「已验证」的形式存在。"
      source="docs/structural-edge/industry-etf-watch.md · theme-etf-entry.md · holding-and-exit.md"
      actions={<Cadence kind="experiment" />}
    >
      <StatGrid items={cards} />
      <p className="mt-2 max-w-3xl text-[11.5px] leading-relaxed text-secondary">{grade.note}</p>
      <Callout tone="warn" label="给上面所有结论打折">
        {evidenceNote}
      </Callout>
    </Section>
  )
}

// ---------------------------------------------------------------- 02 位置/形态

function PositionBlock() {
  const { axisText, axisLine, barUp, barDown } = useAxis()

  const posOption = useMemo<EChartsOption>(() => ({
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => `${v} bp` },
    grid: { left: 52, right: 16, top: 18, bottom: 46 },
    xAxis: {
      type: 'category', data: position.grad.map(d => d.bin),
      axisLabel: { fontSize: 11, color: axisText, lineHeight: 15 },
      axisLine: { lineStyle: { color: axisLine } },
    },
    yAxis: {
      type: 'value', name: '20 日 EV (bp)', nameTextStyle: { fontSize: 11, color: axisText },
      axisLabel: { color: axisText }, splitLine: { lineStyle: { color: axisLine } },
    },
    series: [{
      type: 'bar', barWidth: '48%',
      data: position.grad.map(d => ({
        value: d.ev,
        itemStyle: { color: d.ev >= 0 ? barUp : barDown },
        // 负值的标签必须落在柱子末端，而不是 0 轴上方（否则读起来像「−103.8 在 0 以上」）。
        label: { position: d.ev >= 0 ? 'top' : 'bottom' },
      })),
      label: { show: true, formatter: '{c}', fontSize: 10.5, color: axisText },
    }],
  }), [axisText, axisLine, barUp, barDown])

  const shapeOption = useMemo<EChartsOption>(() => ({
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => `${v} bp` },
    legend: { data: ['20 日 EV', 'p10（左尾）'], top: 0, textStyle: { fontSize: 11, color: axisText } },
    grid: { left: 56, right: 16, top: 34, bottom: 34 },
    xAxis: {
      type: 'category', data: shape.bins.map(d => d.bin),
      axisLabel: { color: axisText }, axisLine: { lineStyle: { color: axisLine } },
    },
    yAxis: {
      type: 'value', name: 'bp', nameTextStyle: { fontSize: 11, color: axisText },
      axisLabel: { color: axisText }, splitLine: { lineStyle: { color: axisLine } },
    },
    series: [
      { name: '20 日 EV', type: 'bar', data: shape.bins.map(d => d.ev), barWidth: '32%', itemStyle: { color: barUp }, label: { show: true, position: 'top', formatter: '{c}', fontSize: 10, color: axisText } },
      // 三个 p10 标签统一放在柱内底端：ECharts 在柱外没空间时会自动翻进柱内，同一张图里出现两种位置。
      { name: 'p10（左尾）', type: 'bar', data: shape.bins.map(d => d.p10), barWidth: '32%', itemStyle: { color: barDown }, label: { show: true, position: 'insideBottom', formatter: '{c}', fontSize: 10, color: '#fff' } },
    ],
  }), [axisText, axisLine, barUp, barDown])

  const monthCols: Col<(typeof position.monthly)[number]>[] = [
    { head: '月度可执行版（月末调仓）', render: m => <span className="font-medium">{m.name}</span>, w: '32%' },
    { head: '月数', render: m => <span className="font-mono tabular-nums">{m.months}</span>, w: '12%', align: 'right' },
    { head: '相对全行业', render: m => <span className={`font-mono tabular-nums ${m.rel < 0 ? 'text-bear' : 'text-bull'}`}>{fmtBp(m.rel)} bp</span>, w: '20%', align: 'right' },
    { head: 't', render: m => <span className="font-mono tabular-nums">{m.t}</span>, w: '10%', align: 'right' },
    { head: '胜率', render: m => <span className="font-mono tabular-nums">{m.win}%</span>, w: '12%', align: 'right' },
    { head: 'MDD', render: m => <span className="font-mono tabular-nums">{m.mdd}%</span>, w: '14%', align: 'right' },
  ]

  return (
    <Section
      id="position"
      no="02"
      title="位置与形态：方向与生存率"
      lead="同一份数据，切成三个维度看：位置给方向、形态给生存率、时点决定你吃到多少。图不改阈值——它们就是阈值的来历。"
      source={position.sample}
      actions={<Cadence kind="experiment" />}
    >
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <ChartCard
          title="位置：距一年高点的回撤 → 20 日 EV（bp）"
          hint="−50%↓ 样本不足"
          footer={position.note}
        >
          <Chart option={posOption} height={244} />
        </ChartCard>
        <ChartCard
          title="形态：同样的「跌破 35%」，跌的方式决定左尾"
          hint="与位置正交"
          footer={shape.note}
        >
          <Chart option={shapeOption} height={244} />
        </ChartCard>
      </div>

      <Callout tone="danger" label="时点：为什么「等确认再买」等于不买">
        {position.timing}
        差 281bp，只因为晚了两周 —— 这是「配置策略」与「事件策略」的区别，也是本页唯一可执行的时点结论。
      </Callout>

      <DataTable
        cols={monthCols}
        rows={position.monthly}
        rowKey={m => m.name}
        reason="对照组：把同一个信号改成「月末统一调仓」这种可批量执行的形态，收益立刻转负——所以这不是执行难度问题，是信号本身是事件性的。「月数」前三行是行业·月观测（同一个月可有多个行业），全行业等权那行是 113 个日历月，两者不可相加。"
      />
    </Section>
  )
}

// ---------------------------------------------------------------- 03 广度与久期

function BreadthBlock({ payload }: { payload: EtfWatchPayload | null }) {
  const live = breadthReading(payload)
  const breadthCols: Col<(typeof grade.breadth)[number]>[] = [
    { head: '当日区间内行业数', render: b => b.bin, w: '40%' },
    { head: '天数', render: b => <span className="font-mono tabular-nums">{b.days}</span>, w: '12%', align: 'right' },
    { head: '20 日相对超额', render: b => <span className={`font-mono tabular-nums ${b.rel > 0 ? 'text-bull' : 'text-bear'}`}>{fmtBp(b.rel)} bp</span>, w: '30%', align: 'right' },
    { head: '胜率', render: b => <span className="font-mono tabular-nums">{b.win}%</span>, w: '18%', align: 'right' },
  ]

  const { axisText, axisLine, barAlt, barDown } = useAxis()

  const durOption = useMemo<EChartsOption>(() => ({
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => `${v}%` },
    legend: { data: ['2017–2025', '2026'], top: 0, textStyle: { fontSize: 11, color: axisText } },
    grid: { left: 48, right: 16, top: 34, bottom: 42 },
    xAxis: {
      type: 'category', data: duration.dur.map(d => d.q),
      axisLabel: { fontSize: 11, color: axisText, lineHeight: 15 },
      axisLine: { lineStyle: { color: axisLine } },
    },
    yAxis: {
      type: 'value', name: 'P(20 日 < −20%) %', max: 22,
      nameTextStyle: { fontSize: 11, color: axisText },
      axisLabel: { color: axisText }, splitLine: { lineStyle: { color: axisLine } },
    },
    series: [
      { name: '2017–2025', type: 'bar', data: duration.dur.map(d => d.hist), barWidth: '28%', itemStyle: { color: barAlt } },
      {
        name: '2026', type: 'bar', data: duration.dur.map(d => d.y26), barWidth: '28%',
        itemStyle: { color: barDown },
        label: { show: true, position: 'top', formatter: '{c}%', fontSize: 11, color: axisText },
      },
    ],
  }), [axisText, axisLine, barAlt, barDown])

  return (
    <Section
      id="breadth"
      no="03"
      title="广度与久期：一个说这是不是机会，一个说能拿多重"
      lead={`${grade.breadthLead} 久期不是第二个信号，而是仓位上限的第二个来源 —— 它决定你能拿多大仓位，不决定你买什么。`}
      source="theme-etf-entry.md · 2026-duration-review.md · analysis/_diag_theme_breadth.py · _diag_market_duration.py"
      actions={<Cadence kind="experiment" />}
      asOf={live?.asof}
    >
      <div className="text-[10.5px] font-medium text-secondary">当日读数（活数据，不写进 data.ts）</div>
      {live ? (
        <div className="mt-1.5 flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded border border-border bg-base px-3 py-2 text-[11.5px]">
          <Tag tone={live.tone}>当日</Tag>
          <span className="font-mono tabular-nums text-foreground">区间内 {live.inBand} 个行业</span>
          <span aria-hidden className="text-muted">·</span>
          <span className="font-mono tabular-nums text-foreground">四条全过 {live.signal} 个</span>
          <span className="text-secondary">
            {live.clustersToday > 0 ? `记 ${live.clustersToday} 个事件簇` : '未成簇'}
          </span>
          <span className="ml-auto font-mono text-[10px] text-secondary">{live.asof}</span>
        </div>
      ) : (
        <p className="mt-1.5 rounded border border-border bg-base px-3 py-2 text-[11px] text-secondary">
          当日读数未就绪：先跑 <code className="font-mono">analysis/etf_watch.py</code>。下表是历史分档，不随当日变化。
        </p>
      )}

      <div className="mt-4 text-[10.5px] font-medium text-secondary">历史分档：当日区间内行业数 → 20 日相对超额</div>
      <DataTable cols={breadthCols} rows={grade.breadth} rowKey={b => b.bin} dense />
      <p className="mt-2 text-[11px] leading-relaxed text-secondary">{grade.breadthNote}</p>

      <div id="duration" className="mt-6 scroll-mt-28 border-t border-border pt-5">
        <div className="text-[10.5px] font-medium text-secondary">久期：仓位上限的第二个来源</div>
        <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-secondary">{sizing.duration}</p>
        <StatGrid items={duration.cards} />
        <div className="mt-3 grid gap-3 md:grid-cols-[1.08fr_0.92fr]">
          <ChartCard
            title="同样的持有窗口，短久期组的大跌概率高一个数量级"
            hint="按 h = 100/换手率 分 5 组"
            footer="纵轴是 20 个交易日内跌超 20% 的概率；2026 年整条曲线右移，短久期端最陡。"
          >
            <Chart option={durOption} height={236} />
          </ChartCard>
          <div className="self-start rounded-lg border border-border bg-surface p-3.5 text-[11.5px] leading-relaxed text-secondary">
            <div className="text-[11.5px] font-medium text-foreground">这是「有底」唯一站得住的形态</div>
            <p className="mt-1.5">
              不是价格有支撑，是<b className="text-foreground">久期长的人不会被迫在同一周里卖</b>。
              所以久期只能决定「同一笔钱拿多大仓位」，不能决定「买什么」。
            </p>
            <p className="mt-2">
              外部叙事说「长钱在定价、中间塌陷、主题有顶」——现象是真的，归因是错的。
              九条可证伪判据与被推翻的三步归因，收在案卷 B。
            </p>
            <CrossLink to="archive" anchor="archive-b" className="mt-3">打开案卷 B：久期复核</CrossLink>
          </div>
        </div>
      </div>
    </Section>
  )
}

// ---------------------------------------------------------------- 04 仓位择时

function SizingAuditBlock({
  art, artErr, artFetching, confirmDays, onConfirmDays, onRetry,
}: {
  art: PositionArtPayload | null
  artErr: string
  artFetching: boolean
  confirmDays: number
  onConfirmDays: (n: number) => void
  onRetry: () => void
}) {
  const { dark, axisText, axisLine } = useAxis()
  const baseColor = dark ? '#888780' : '#5f5e5a'

  const equityOption = useMemo<EChartsOption>(() => {
    const pts = art?.equity ?? []
    // 只给「年份变化的那一天」打刻度：默认 interval 会在年中取点，出现 2016 2016 2016 的重复标签。
    const yearStarts = new Set<string>()
    let prevYear = ''
    for (const p of pts) {
      const y = p.date.slice(0, 4)
      if (y !== prevYear) { yearStarts.add(p.date); prevYear = y }
    }
    return {
      tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => Number(v).toFixed(3) },
      legend: { data: ['全程持有', '本规则'], top: 0, textStyle: { fontSize: 11, color: axisText } },
      grid: { left: 52, right: 16, top: 30, bottom: 30 },
      xAxis: {
        type: 'category',
        data: pts.map(x => x.date),
        axisLabel: {
          fontSize: 10.5,
          color: axisText,
          formatter: (v: string) => v.slice(0, 4),
          interval: (_i: number, v: string) => yearStarts.has(v),
        },
        axisLine: { lineStyle: { color: axisLine } },
      },
      yAxis: {
        type: 'value',
        name: '净值',
        scale: true,
        nameTextStyle: { fontSize: 11, color: axisText },
        axisLabel: { color: axisText },
        splitLine: { lineStyle: { color: axisLine } },
      },
      series: [
        {
          name: '全程持有',
          type: 'line',
          data: pts.map(x => x.base),
          showSymbol: false,
          lineStyle: { width: 1.4, color: baseColor, type: 'dashed' },
          itemStyle: { color: baseColor },
        },
        {
          name: '本规则',
          type: 'line',
          data: pts.map(x => x.rule),
          showSymbol: false,
          lineStyle: { width: 1.6, color: dark ? '#85B7EB' : '#185FA5' },
          itemStyle: { color: dark ? '#85B7EB' : '#185FA5' },
        },
      ],
    }
  }, [art, dark, axisText, axisLine, baseColor])

  const chosen = art?.chosen
  const bench = art?.benchmark
  const flatP = art?.flat_stats
  const placebo = art?.placebo
  const passed = (art?.checks ?? []).filter(c => c.pass).length
  const total = (art?.checks ?? []).length

  return (
    <Section
      id="sizing-audit"
      no="04"
      title="仓位择时的验收：六种实现、十年，没有一种跑赢全程持有"
      lead="这是本页最长的一条否定结论。它留在这里不是因为有效，是因为它每天都在被登记 —— 只有把「它不行」写在明面上，某天想「先空仓躲一躲」时才拦得住。"
      source="GET /api/position-art · docs/structural-edge/state-timing-audit.md · backend/app/services/position_art.py"
      actions={<Cadence kind="experiment" note="每日登记一行" />}
    >
      {artErr && !art ? (
        <div className="rounded-lg border border-warning/40 bg-warning/[0.06] px-4 py-3">
          <div className="text-[12.5px] font-medium text-foreground">仓位规则读数加载失败</div>
          <p className="mt-1 text-[11.5px] leading-relaxed text-secondary">
            接口 /api/position-art 未返回（{artErr}）。确认日线分区可读后重试，其余证据不受影响。
          </p>
          <button
            type="button"
            onClick={onRetry}
            className="mt-2 rounded-md border border-border px-2.5 py-1 text-xs text-secondary hover:bg-elevated"
          >
            重试
          </button>
        </div>
      ) : !art ? (
        <div className="rounded-lg border border-dashed border-border bg-surface/40 px-4 py-6 text-center text-[11.5px] text-secondary">
          正在重算十年等权指数与状态序列（首次约 3 秒）…
        </div>
      ) : !art.available ? (
        <div className="rounded-lg border border-dashed border-border bg-surface/40 px-4 py-6 text-center text-[11.5px] text-secondary">
          {art.reason ?? '这条规则需要 2016 年以来的等权指数，至少 260 个交易日。'}
        </div>
      ) : (
        <>
          <div className="rounded-lg border-l-4 border-danger bg-danger/10 px-4 py-3">
            <div className="text-[10px] text-secondary">裁决</div>
            <div className="mt-1 text-[13px] leading-relaxed text-foreground">
              状态择时在规则层被证伪：六种实现、十年、扣 20bp，没有一种跑赢「全程持有」；
              空仓的 {flatP?.flat_days ?? 0} 天日均 {flatP?.flat_mean_bp ?? 0}bp，比在场日（
              {flatP?.in_mean_bp ?? 0}bp）还高 —— 它把好日子空掉了。
            </div>
            <div className="mt-1 text-[10.5px] leading-relaxed text-secondary">
              十年：本规则 {chosen?.cagr_pct}% / 回撤 {chosen?.mdd_pct}%，全程持有 {bench?.cagr_pct}% /{' '}
              {bench?.mdd_pct}%。验收 {passed}/{total} 通过 —— 这条规则保留下来只为「按日登记结算」，
              不作为加减仓依据。
            </div>
          </div>

          <div className="mt-4 flex flex-wrap items-center gap-2">
            <span className="text-[10.5px] text-secondary">出场确认（下行满 N 日才离场）</span>
            {CONFIRM_CHOICES.map(n => (
              <button
                key={n}
                type="button"
                onClick={() => onConfirmDays(n)}
                className={`rounded-md border px-2.5 py-1 text-xs transition-colors ${
                  n === confirmDays
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-border text-secondary hover:bg-elevated'
                }`}
              >
                {n === 0 ? '每日切换' : `${n} 日`}
              </button>
            ))}
            {artFetching && <span className="text-[10.5px] text-secondary">重算中…</span>}
          </div>

          <div className="mt-4 text-[10.5px] font-medium text-secondary">六种实现 vs 全程持有</div>
          <DataTable
            cols={[
              {
                head: '实现',
                w: '30%',
                render: r => (
                  <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
                    <span className="font-medium">{r.confirm_days === 0 ? '每日跟随状态' : `下行满 ${r.confirm_days} 日离场`}</span>
                    {r.is_default && <Tag tone="ok">当前</Tag>}
                  </span>
                ),
              },
              { head: '年化', w: '12%', align: 'right', render: r => <span className="font-mono tabular-nums">{r.cagr_pct}%</span> },
              { head: '最大回撤', w: '14%', align: 'right', render: r => <span className="font-mono tabular-nums">{r.mdd_pct}%</span> },
              { head: 'Sharpe', w: '12%', align: 'right', render: r => <span className="font-mono tabular-nums">{r.sharpe ?? '—'}</span> },
              { head: '暴露', w: '14%', align: 'right', render: r => <span className="font-mono tabular-nums">{r.exposure_pct}%</span> },
              { head: '切换次数', w: '18%', align: 'right', render: r => <span className="font-mono tabular-nums">{r.switches}</span> },
            ]}
            rows={(art.rules ?? []).map(r => ({ ...r, key: String(r.confirm_days) }))}
            rowKey={r => r.key}
            dense
            minWidth={620}
          />
          <p className="mt-2 text-[10.5px] leading-relaxed text-secondary">
            切换成本按 20bp 单边计；40bp 压测下本规则年化 {art.stress?.cagr_pct}%。分半样本：
            {(art.halves ?? []).map(h => `${h.label} ${h.base_cagr_pct}%→${h.rule_cagr_pct}%`).join('；')}。
            安慰剂 p={placebo?.p_value ?? '—'}（{placebo?.blocks ?? 0} 块）。
          </p>

          <div className="mt-3 rounded-lg border border-border bg-surface px-3 py-3">
            <div className="mb-1 text-[10.5px] text-secondary">净值：全程持有 vs 本规则（每 20 个交易日取一点）</div>
            <Chart option={equityOption} height={240} />
          </div>

          <div className="mt-4 text-[10.5px] font-medium text-secondary">验收（证据等级就是这个表）</div>
          <DataTable
            cols={[
              { head: '编号', render: c => <span className="font-mono text-[10px] text-secondary">{c.id}</span>, w: '10%', headCls: 'whitespace-nowrap' },
              { head: '项', render: c => <span className="font-medium">{c.name}</span>, w: '24%' },
              { head: '要求', render: c => <span className="text-[11px] text-secondary">{c.want}</span>, w: '30%' },
              { head: '实测', render: c => <span className="tabular-nums">{c.got}</span>, w: '28%' },
              {
                head: '结论',
                render: c => <Tag tone={c.pass ? 'ok' : 'danger'}>{c.pass ? '通过' : '未通过'}</Tag>,
                w: '8%',
                headCls: 'whitespace-nowrap',
              },
            ]}
            rows={(art.checks ?? []).map(c => ({ ...c, key: c.id }))}
            rowKey={c => c.key}
            dense
            minWidth={640}
          />

          <div className="mt-4 grid gap-2 text-[11px] leading-5 text-secondary md:grid-cols-2">
            <div>指数：{art.definition?.index}</div>
            <div>状态：{art.definition?.state}</div>
            <div>规则：{art.definition?.rule}</div>
            <div>滞后：{art.definition?.lag}</div>
            <div>成本：{art.definition?.cost_one_way_bp}bp 单边，按 |Δ仓位| 计费</div>
            <div>
              当前 cell 的独立观测：{art.neff?.year} 下行 {art.neff?.down_overlap}→
              {art.neff?.down_nonoverlap}、横盘 {art.neff?.flat_overlap}→{art.neff?.flat_nonoverlap}
              （重叠窗口 → 非重叠）
            </div>
          </div>

          <Callout tone="danger" label="为什么这张表上全是红">
            <p>
              第一版这条规则四个验收项过了三个：年化、分半、压测都好看。它是假的 ——
              当时用「整段总长」当「已持续天数」，等于提前知道这段会跌多少天。
              改成段内序号后，年化掉到个位数、安慰剂不再显著（现在 p={placebo?.p_value ?? '—'}）。
            </p>
            <p className="mt-2">
              规则层唯一稳的结论是：这个市场里，退出信号比入场信号更难做对，
              而两者共用同一个状态时，它必然在最差的位置触发。
            </p>
            {art.warning && <p className="mt-2 text-warning">{art.warning}</p>}
          </Callout>
        </>
      )}
    </Section>
  )
}

// ---------------------------------------------------------------- 05 组合层

function PortfolioBlock() {
  const cols: Col<(typeof portfolio.ladder)[number]>[] = [
    { head: '口径', render: r => <span className="font-medium">{r.name}</span>, w: '32%' },
    { head: '窗口', render: r => <span className="whitespace-nowrap font-mono text-[10.5px] text-secondary">{r.win}</span>, w: '20%' },
    { head: '年化', render: r => <span className="font-mono tabular-nums">{r.cagr}</span>, w: '12%', align: 'right' },
    { head: '最大回撤', render: r => <span className="font-mono tabular-nums">{r.mdd}</span>, w: '14%', align: 'right' },
    { head: '状态', render: r => <Tag tone={r.tone}>{r.status}</Tag>, w: '22%' },
  ]

  return (
    <Section
      id="portfolio"
      no="05"
      title="组合层：同一条腿放进账户，还剩多少"
      lead={portfolio.lead}
      source="docs/structural-edge/portfolio-replay.md · leg2-bond.md · account-runbook.md"
      actions={<Cadence kind="experiment" />}
    >
      <StatGrid items={portfolio.cards} />
      <p className="mt-2 max-w-3xl text-[11.5px] leading-relaxed text-secondary">{portfolio.cardsNote}</p>

      <div className="mt-5 text-[10.5px] font-medium text-secondary">基准阶梯（只认最后两行；每行窗口必须一起读）</div>
      <DataTable cols={cols} rows={portfolio.ladder} rowKey={r => r.name} minWidth={700} />

      <div className="mt-6 text-[10.5px] font-medium text-secondary">第二条腿：四条候选，只留一条</div>
      <p className="mt-1 max-w-3xl text-[13px] leading-relaxed text-secondary">{portfolio.leg2Lead}</p>
      <DataTable
        cols={[
          { head: '候选', render: r => <span className="font-medium">{r.name}</span>, w: '22%' },
          { head: '与腿 1 相关', render: r => <span className="font-mono tabular-nums">{r.corr}</span>, w: '10%', align: 'right' },
          { head: '空仓期表现', render: r => <span className="whitespace-nowrap text-secondary">{r.behavior}</span>, w: '16%' },
          { head: '裁决', render: r => <Tag tone={r.tone}>{r.verdict}</Tag>, w: '16%' },
          { head: '为什么', render: r => <span className="text-[11px] text-secondary">{r.why}</span>, w: '36%' },
        ]}
        rows={portfolio.leg2}
        rowKey={r => r.name}
        minWidth={760}
      />

      <Callout tone="warn" label="满仓逆波动比六格更差，这点和直觉相反">
        {portfolio.policy}
      </Callout>
      <p className="mt-3 text-[11.5px] leading-relaxed text-secondary">{portfolio.note}</p>
      <p className="mt-2 text-[10.5px] leading-relaxed text-muted">{portfolio.limits}</p>
    </Section>
  )
}

// ---------------------------------------------------------------- 06 两个再等等

function WaitBlock() {
  const wait = grade.waits[0]

  return (
    <Section
      id="waits"
      no="06"
      title="两个「再等等」：一个条件不存在，一个不可证伪"
      lead="一个等的是不存在的降息条件，一个等的是不可证伪的故事。两件事都不构成「暂缓执行」的理由。"
      source="docs/structural-edge/theme-etf-entry.md · docs/fed-cut-20d.md"
      actions={<Cadence kind="experiment" />}
    >
      <div className="rounded-lg border border-border bg-surface p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[12.5px] font-medium text-foreground">{wait.q}</span>
          <Tag tone={wait.tone}>条件不存在</Tag>
        </div>
        <div className="mt-2 space-y-1.5 text-[11.5px] leading-relaxed text-secondary">
          <p><b className="text-foreground">现在：</b>{wait.now}</p>
          <p><b className="text-foreground">历史：</b>{wait.hist}</p>
          <p><b className="text-foreground">本次：</b>{wait.now2}</p>
          <p className="rounded border-l-2 border-danger bg-danger/[0.07] px-3 py-2 text-foreground">{wait.verdict}</p>
        </div>
      </div>

      <div className="mt-3 rounded-lg border border-border bg-surface p-4">
        <div className="text-[12.5px] font-medium text-foreground">{grade.narrativeLead}</div>
        <DataTable
          cols={[
            { head: '闸门', render: (x: (typeof grade.narrative)[number]) => <span className="whitespace-nowrap font-medium">{x.q}</span> },
            { head: '检验结果', render: (x: (typeof grade.narrative)[number]) => <span className="text-secondary">{x.a}</span> },
          ]}
          rows={grade.narrative}
          rowKey={x => x.q}
          dense
        />
        <p className="mt-2 text-[10.5px] leading-relaxed text-secondary">
          闸门条目本身在案卷 D。这里只说结论：叙事定不了方向，也定不了时点——它只能定「这只票属于哪个故事」，
          而故事不构成买点。
        </p>
      </div>

      <Callout tone="warn" label="如果一定要给一个时点">
        {grade.verdict}
      </Callout>
    </Section>
  )
}
