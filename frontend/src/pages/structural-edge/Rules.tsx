/**
 * 「规则」章 —— 下单前要读的那一页。
 *
 * 只放四类内容：入场闸门（六问）、放行闸门（不买清单）、仓位、持有与退出。
 * 证据不在这里展开：每张卡片只给结论与一句依据，完整图表在「证据」章。
 * 这样规则页可以打印/贴在屏幕上，而证据章可以慢慢读。
 *
 * 「持有与退出」是合并仓位艺术时补上的一半：原来只有买点，没有卖出。
 * 它的结论同样是否定式的 —— 三种规则退出全部输给固定 20 日。
 */
import { buyQuestions, exits, sizing, stopRules, thesis } from './data'
import { CrossLink, TabLayout } from './Shell'
import { Cadence, Callout, DataTable, type Col, Section, StatGrid, Tag } from './ui'

export function RulesPanel() {
  return (
    <TabLayout
      tab="rules"
      items={[
        { id: 'buy', no: '01', label: '买点六问' },
        { id: 'stop', no: '02', label: '不买清单' },
        { id: 'sizing', no: '03', label: '买多少' },
        { id: 'exits', no: '04', label: '持有与退出' },
      ]}
    >
      <BuySection />
      <StopSection />
      <SizingSection />
      <ExitSection />
    </TabLayout>
  )
}

// ---------------------------------------------------------------- 01 买点

function BuySection() {
  return (
    <Section
      id="buy"
      no="01"
      title="买点：六个问题，一次答完"
      first
      lead={thesis.one}
      source="docs/structural-edge/industry-etf-asymmetry.md · theme-etf-entry.md"
      actions={<Cadence kind="frozen" note="预注册" />}
    >
      <div className="mt-3 grid gap-2.5 md:grid-cols-2">
        {buyQuestions.map(q => <BuyCard key={q.n} {...q} />)}
      </div>

      <p className="mt-3 text-[10.5px] leading-relaxed text-muted">
        1–4 是入场闸门（缺一不可），5 是仓位约束，6 是放行闸门。
        每条的依据在「证据」视图；第 6 条的完整清单在下一节。
      </p>

      <Callout tone="muted" label="这个视图不回答什么">
        {thesis.two}
      </Callout>
    </Section>
  )
}

function BuyCard({ n, badge, q, a, basis }: (typeof buyQuestions)[number]) {
  return (
    <div className="flex gap-3 rounded-lg border border-border bg-surface p-3.5">
      <div className="flex w-6 shrink-0 flex-col items-center">
        <span className="font-mono text-[10px] text-muted">{n}</span>
        <span className="mt-1 h-px w-3 bg-border" />
      </div>
      <div className="min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="text-[10.5px] text-muted">{q}</span>
          <span className="rounded bg-elevated px-1.5 py-0.5 text-[9.5px] text-muted">{badge}</span>
        </div>
        <div className="mt-0.5 text-[14px] font-semibold leading-snug text-foreground">{a}</div>
        <p className="mt-1 text-[11px] leading-relaxed text-secondary">{basis}</p>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 02 不买

function StopSection() {
  const cols: Col<(typeof stopRules)[number]>[] = [
    { head: '条件（t 日收盘可算）', render: r => <span className="font-medium">{r.cond}</span> },
    { head: '数据依据', render: r => <span className="text-secondary">{r.why}</span> },
    { head: '动作', render: r => <span className="whitespace-nowrap">{r.act}</span> },
    { head: '类型', render: r => <Tag tone={r.tone}>{r.tone === 'danger' ? '硬阻断' : r.tone === 'warn' ? '纪律' : '不设'}</Tag> },
  ]

  return (
    <Section
      id="stop"
      no="02"
      title="不买清单：六条，任一条成立就放弃"
      lead="买点谈的是「什么时候可以买」，这一节谈「什么时候不许买」。后者才是这个项目唯一跨子样本稳定的东西——所以它必须是硬条件，不是参考意见。"
      source="docs/structural-edge/industry-etf-asymmetry.md · analysis/_diag_industry_etf_asym.py"
      actions={<Cadence kind="frozen" note="预注册" />}
    >
      <DataTable cols={cols} rows={stopRules} rowKey={r => r.cond} />
      <Callout tone="warn" label="为什么第 1 条不是规则">
        回撤 &gt;50% 看着最像「跌透了」，但它修复份额折算后只剩 7 个样本 —— 估不出来的东西不能写成规则。
        更好的做法不是给深跌加一条买入条件，而是把它移出可下注区间（扫描器里标 <span className="font-mono text-xs">BLOCKED_F1</span>）。
      </Callout>
    </Section>
  )
}

// ---------------------------------------------------------------- 03 仓位

function SizingSection() {
  const cols: Col<(typeof sizing.rows)[number]>[] = [
    {
      head: '标的',
      w: '19%',
      render: r => (
        <>
          <span className="font-medium leading-snug">{r.name}</span>
          <span className="mt-0.5 block font-mono text-[10px] text-muted">{r.code}</span>
        </>
      ),
    },
    { head: '风险平价权重（已废）', headCls: 'whitespace-nowrap', render: r => <span className="font-mono tabular-nums text-secondary">{(r.w * 100).toFixed(1)}%</span>, w: '11%', align: 'right' },
    { head: '距 250 日高点（单只）', render: r => <span className="font-mono tabular-nums">{(r.dd * 100).toFixed(1)}%</span>, w: '12%', align: 'right' },
    { head: '波动分位', headCls: 'whitespace-nowrap', render: r => <span className="font-mono tabular-nums">{(r.volpct * 100).toFixed(0)}%</span>, w: '9%', align: 'right' },
    { head: '年化波动', headCls: 'whitespace-nowrap', render: r => <span className="font-mono tabular-nums">{r.vol}%</span>, w: '8%', align: 'right' },
    { head: '20 日均额', headCls: 'whitespace-nowrap', render: r => <span className="font-mono tabular-nums">{r.amt} 亿</span>, w: '9%', align: 'right' },
    { head: '与沪深300 相关', render: r => <span className="font-mono tabular-nums">{r.corr.toFixed(2)}</span>, w: '10%', align: 'right' },
    { head: 'beta', render: r => <span className="font-mono tabular-nums">{r.beta.toFixed(2)}</span>, w: '7%', align: 'right' },
    { head: '备注', render: r => <span className="text-[10.5px] text-secondary">{r.note}</span>, w: '15%' },
  ]

  return (
    <Section
      id="sizing"
      no="03"
      title="买多少：每笔一格，最多六格"
      lead={sizing.lead}
      source="docs/structural-edge/theme-etf-entry.md · analysis/_diag_theme_etf_entry.py"
      actions={<Cadence kind="frozen" note="预注册" />}
    >
      <Callout tone="ok" label="现行政策：六格（每笔 = 当时权益/6）">
        {sizing.policy}
      </Callout>
      <StatGrid items={sizing.cards} />
      <DataTable
        cols={cols}
        rows={sizing.rows}
        rowKey={r => r.code}
        dense
        minWidth={880}
        reason="六只所在行业均满足位置与形态条件（行业净值口径）；下表「距 250 日高点」是单只 ETF 口径，只用于衡量同一笔钱承担多少波动，不用于判定买点。「风险平价权重」= 1/年化波动 归一化 —— 这是已被淘汰的「满仓逆波动」政策，现行账户用六格（每笔 = 当时权益/6 ≈ 16.7%），本列只用于复现 A/B 差异，不是下单比例。这张表是仓位表，不是推荐表。"
      />
      <Callout tone="warn" label="第二个仓位约束，来自另一个层">
        仓位上限还受个股久期约束：久期决定你能拿多大仓位，不决定你买什么。
        <CrossLink to="evidence" anchor="duration" className="ml-1 align-baseline">看久期证据</CrossLink>
      </Callout>
    </Section>
  )
}

// ---------------------------------------------------------------- 04 持有与退出

function ExitSection() {
  const cols: Col<(typeof exits.rows)[number]>[] = [
    {
      head: '规则',
      w: '26%',
      render: r => (
        <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
          <span className="font-medium">{r.rule}</span>
          {r.base && <Tag tone="ok">基准</Tag>}
        </span>
      ),
    },
    { head: '独立月', render: r => <span className="font-mono tabular-nums">{r.months}</span>, w: '9%', align: 'right' },
    { head: '月均收益', render: r => <span className={`font-mono tabular-nums ${/^[−-]/.test(r.mean) ? 'text-bear' : 'text-bull'}`}>{r.mean}bp</span>, w: '16%', align: 'right' },
    { head: '日均', render: r => <span className={`font-mono tabular-nums ${/^[−-]/.test(r.daily) ? 'text-bear' : 'text-bull'}`}>{r.daily}bp</span>, w: '13%', align: 'right' },
    { head: 'p10', render: r => <span className="font-mono tabular-nums text-bear">{r.p10}bp</span>, w: '16%', align: 'right' },
    { head: '胜率', render: r => <span className="font-mono tabular-nums">{r.win}</span>, w: '10%', align: 'right' },
    { head: '平均持有', render: r => <span className="font-mono tabular-nums">{r.hold} 日</span>, w: '10%', align: 'right' },
  ]

  const lateCols: Col<(typeof exits.late)[number]>[] = [
    { head: '进/出场方案', render: r => <span className="font-medium">{r.plan}</span>, w: '42%' },
    { head: '月均', render: r => <span className="font-mono tabular-nums text-bull">{r.mean}bp</span>, w: '18%', align: 'right' },
    { head: '相对全行业（t）', render: r => <span className="whitespace-nowrap font-mono tabular-nums text-secondary">{r.rel}</span>, w: '26%', align: 'right' },
    { head: 'p10', render: r => <span className="font-mono tabular-nums text-bear">{r.p10}bp</span>, w: '14%', align: 'right' },
  ]

  return (
    <Section
      id="exits"
      no="04"
      title="持有与退出：固定 20 日是基准，三种规则退出都没赢"
      lead={exits.lead}
      source="docs/structural-edge/holding-and-exit.md · analysis/etf_watch_experiments.py"
      actions={<Cadence kind="frozen" note="预注册" />}
    >
      <StatGrid items={exits.cards} />
      <DataTable cols={cols} rows={exits.rows} rowKey={r => r.rule} reason={exits.pairNote} minWidth={720} />
      <Callout tone="danger" label="裁决">
        {exits.verdict}
      </Callout>

      <div className="mt-6 text-[10.5px] font-medium text-secondary">延迟进场：晚 8 天，alpha 就不显著了</div>
      <DataTable
        cols={lateCols}
        rows={exits.late}
        rowKey={r => r.plan}
        dense
        minWidth={640}
        reason={exits.lateNote}
      />
    </Section>
  )
}
