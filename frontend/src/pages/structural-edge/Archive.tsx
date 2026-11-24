/**
 * 「反证」章 —— 四组「为什么别的路不是买点」的案卷。
 *
 * 这个视图的更新频率与前四个不同：冻结、只追加。已有裁决不重写；
 * 新证据另开一条，并在索引里加一张卡。默认折叠，但内容常驻 DOM（可搜索、可测试）。
 *
 * 索引卡不是为了好看：四组案卷的标题都带裁决，先扫索引就能决定要不要展开，
 * 不必把四块长文都滚一遍。
 */
import { useEffect, useMemo } from 'react'
import type { EChartsOption } from 'echarts'
import { ArrowRight } from 'lucide-react'
import { cost, duration, forcedFlows, gates } from './data'
import { CrossLink, TabLayout } from './Shell'
import { Callout, Chart, DataTable, type Col, Fold, fmtBp, Section, StatGrid, Tag, Tile, Tiles, type Tone, useAxis } from './ui'

interface ArchiveIndexItem {
  key: string
  letter: string
  title: string
  verdict: string
  tone: Tone
  desc: string
}

const ARCHIVE_INDEX: ArchiveIndexItem[] = [
  {
    key: 'a',
    letter: 'A',
    title: '被迫流账本',
    verdict: 'moat 真 · edge 负',
    tone: 'warn',
    desc: `${forcedFlows.rows.length} 类被迫流、${forcedFlows.experiments.length} 个实验跑完：没有人抢，有时是因为抢了会亏。`,
  },
  {
    key: 'b',
    letter: 'B',
    title: '久期复核',
    verdict: '归因错',
    tone: 'warn',
    desc: `${duration.checks.length} 条可证伪判据：久期是约束集的投影，不是新的 alpha 来源。`,
  },
  {
    key: 'c',
    letter: 'C',
    title: '这五个月的账',
    verdict: '不划算',
    tone: 'danger',
    desc: '二手信息四类全部测过；按交易回报算，五个月没换来一条通过验收的规则。',
  },
  {
    key: 'd',
    letter: 'D',
    title: '叙事的七道闸门',
    verdict: '只能杀选项',
    tone: 'ok',
    desc: `${gates.rows.length} 道逻辑闸门：不跑数据，先杀掉必然不可能的选项。`,
  },
]

/** 打开并定位某组案卷。索引卡、左侧目录、跨视图深链共用这一个入口。 */
export function openArchiveFold(key: string) {
  const el = document.getElementById(`archive-${key}`) as HTMLDetailsElement | null
  if (!el) return
  if (window.location.hash !== `#archive-${key}`) window.history.replaceState(null, '', `#archive-${key}`)
  el.open = true
  el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

export function ArchivePanel() {
  // 首屏 #archive-b 这类深链：先展开，再滚动；否则只会停在一个收起的标题上。
  useEffect(() => {
    const h = window.location.hash.replace(/^#/, '')
    if (!h.startsWith('archive-')) return
    const timer = window.setTimeout(() => openArchiveFold(h.slice('archive-'.length)), 120)
    return () => window.clearTimeout(timer)
  }, [])

  return (
    <TabLayout
      tab="archive"
      items={[
        { id: 'archive', no: '01', label: '裁决索引' },
        ...ARCHIVE_INDEX.map(item => ({
          id: `archive-${item.key}`,
          no: item.letter,
          label: item.title,
          onSelect: () => openArchiveFold(item.key),
        })),
      ]}
    >
      <Section
        id="archive"
        no="01"
        title="裁决索引：四组被否掉的路"
        first
        lead={
          <>
            到这里为止，买点已经讲完了。余下四组是被否掉的路 —— 它们的价值不是「结论」，
            而是<b className="text-foreground">同样的题被问过一次、用同样严格的标准答成了否定</b>。
            下次再遇到「机构在抛售」「久期拉长」「二手信息有信息量」这类说法，先来这里查有没有测过。
          </>
        }
        source="docs/structural-edge/ 四组案卷（A–D）· analysis/_diag_* 全部可重跑"
      >
        <Tiles cols={2}>
          {ARCHIVE_INDEX.map(item => (
            <a
              key={item.key}
              href={`#archive-${item.key}`}
              data-archive-index={item.key}
              onClick={e => { e.preventDefault(); openArchiveFold(item.key) }}
              className="group flex h-full flex-col rounded-sm text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
            >
              <Tile
                lead={item.letter}
                aside={<Tag tone={item.tone}>{item.verdict}</Tag>}
                title={<span className="transition-colors group-hover:text-accent">{item.title}</span>}
                body={item.desc}
                foot={
                  <span className="inline-flex items-center gap-1 text-[12px] font-medium text-accent">
                    展开案卷
                    <ArrowRight className="h-3 w-3 transition-transform group-hover:translate-x-0.5" />
                  </span>
                }
              />
            </a>
          ))}
        </Tiles>

        <ForcedFlowArchive />
        <DurationArchive />
        <CostArchive />
        <GateArchive />

        <div className="mt-10 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
          <p className="text-[12px] leading-[1.7] text-muted">
            这些案卷的文档、脚本与口径入口，统一放在「工具」章。
          </p>
          <CrossLink to="tools" anchor="maint">维护与数据来源</CrossLink>
        </div>
      </Section>
    </TabLayout>
  )
}

// ---------------------------------------------------------------- A 被迫流

function ForcedFlowArchive() {
  const { axisText, axisLine, barUp, barAlt } = useAxis()

  const delistOption = useMemo<EChartsOption>(() => ({
    tooltip: { trigger: 'axis', formatter: (p: unknown) => {
      const a = (p as { name: string; value: number }[])[0]
      return `${a.name}<br/>60 日退市率 <b>${a.value}%</b>`
    } },
    grid: { left: 44, right: 20, top: 20, bottom: 34 },
    xAxis: { type: 'category', data: forcedFlows.delist.map(d => d.b), axisLabel: { fontSize: 11.5, color: axisText }, axisLine: { lineStyle: { color: axisLine } } },
    yAxis: { type: 'value', name: '退市率 %', max: 100, nameTextStyle: { fontSize: 11.5, color: axisText }, axisLabel: { color: axisText }, splitLine: { lineStyle: { color: axisLine } } },
    series: [{ type: 'bar', data: forcedFlows.delist.map(d => d.rate), barWidth: '46%', itemStyle: { color: barUp }, label: { show: true, position: 'top', formatter: '{c}%', fontSize: 11.5, color: axisText } }],
  }), [axisText, axisLine, barUp])

  const rebalanceOption = useMemo<EChartsOption>(() => ({
    tooltip: { trigger: 'axis' },
    legend: { data: ['生效前 20 日', '生效后 20 日'], top: 0, textStyle: { fontSize: 11.5, color: axisText } },
    grid: { left: 44, right: 20, top: 34, bottom: 34 },
    xAxis: { type: 'category', data: ['调入', '调出'], axisLabel: { color: axisText }, axisLine: { lineStyle: { color: axisLine } } },
    yAxis: { type: 'value', name: '超额 bp', nameTextStyle: { fontSize: 11.5, color: axisText }, axisLabel: { color: axisText }, splitLine: { lineStyle: { color: axisLine } } },
    series: [
      { name: '生效前 20 日', type: 'bar', data: [forcedFlows.rebalance.pre_add, forcedFlows.rebalance.pre_remove], barWidth: '30%', itemStyle: { color: barAlt } },
      { name: '生效后 20 日', type: 'bar', data: [forcedFlows.rebalance.fwd_add, forcedFlows.rebalance.fwd_remove_med], barWidth: '30%', itemStyle: { color: barUp }, label: { show: true, position: 'top', formatter: '{c}', fontSize: 11.5, color: axisText } },
    ],
  }), [axisText, axisLine, barAlt, barUp])

  const flowCols: Col<(typeof forcedFlows.rows)[number]>[] = [
    { head: '被迫流', render: r => <span className="font-medium">{r.f}</span> },
    { head: '谁被迫', render: r => r.who },
    { head: '错位段落在哪', render: r => r.moat },
    { head: '裁决', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
    { head: '说明', render: r => r.d },
  ]

  const expCols: Col<(typeof forcedFlows.experiments)[number]>[] = [
    { head: '实验', render: r => <span className="font-medium">{r.name}</span> },
    { head: '脚本', render: r => <span className="font-mono text-[11px] text-muted">{r.script}</span> },
    { head: '问题', render: r => r.q },
    { head: '裁决', render: r => <Tag tone={r.tone}>{r.verdict}</Tag> },
    { head: '结论', render: r => r.detail },
  ]

  return (
    <Fold id="archive-a" title="A · 被迫流账本：六类跑完，moat 真、edge 负" hint="6 类被迫流 · 5 个实验 · 4 证伪 / 1 成立">
      <p className="mt-4 max-w-[46em] text-[14px] leading-[1.8] text-secondary">{forcedFlows.lead}</p>

      <DataTable cols={flowCols} rows={forcedFlows.rows} rowKey={r => r.f} />

      <Callout tone="warn" label="收敛规律">{forcedFlows.convergence}</Callout>

      <DataTable cols={expCols} rows={forcedFlows.experiments} rowKey={r => r.name} />

      <div className="mt-7 grid gap-x-8 gap-y-7 md:grid-cols-2">
        <div>
          <div className="text-[12px] leading-[1.6] text-muted">跌破 1 元后 60 日退市率，按跌破前回撤深度分桶（1.5 元阈值 · post2020）</div>
          <Chart option={delistOption} height={240} />
        </div>
        <div>
          <div className="text-[12px] leading-[1.6] text-muted">指数调样：调入/调出股 生效前后 20 日超额（bp，真实名单）</div>
          <Chart option={rebalanceOption} height={240} />
        </div>
      </div>

      <Callout tone="ok" label="唯一被证实的正价值">{forcedFlows.positive}</Callout>

      <div className="mt-6 text-[13px] leading-[1.75] text-secondary">
        <div className="text-[11px] font-medium tracking-[0.1em] text-muted">下一步（就这三条）</div>
        <ul className="mt-2 list-disc space-y-1.5 pl-5">
          {forcedFlows.next.map(n => <li key={n}>{n}</li>)}
        </ul>
      </div>

      <DataTable
        cols={[
          { head: '脚本 / 文档', render: (s: (typeof forcedFlows.scripts)[number]) => <span className="font-mono text-[11px] text-muted">analysis/{s.s}</span> },
          { head: '产出', render: (s: (typeof forcedFlows.scripts)[number]) => s.o },
        ]}
        rows={forcedFlows.scripts}
        rowKey={s => s.s}
        reason="可复现清单"
      />
    </Fold>
  )
}

// ---------------------------------------------------------------- B 久期

function DurationArchive() {
  const checkCols: Col<(typeof duration.checks)[number]>[] = [
    { head: '判据', render: r => <span className="font-medium">{r.k}</span>, w: '20%' },
    { head: '叙事要求它成立的样子', render: r => <span className="text-secondary">{r.want}</span>, w: '22%' },
    { head: '本地读数', render: r => r.got, w: '42%' },
    { head: '裁决', render: r => <Tag tone={r.tone}>{r.verdict}</Tag>, w: '16%' },
  ]

  return (
    <Fold id="archive-b" title="B · 久期复核：现象是真的，归因是错的" hint="9 条可证伪判据 · 1,092 万行日 K">
      <p className="mt-4 max-w-[46em] text-[14px] leading-[1.8] text-secondary">{duration.lead}</p>
      <p className="mt-2 max-w-[46em] text-[12px] leading-[1.7] text-muted">
        外部叙事：险资只买高股息（有底）、量化只买动量（有顶）、主动公募从 5 万亿缩到 3.4–4.06 万亿，
        无股息无动量的中小盘被两边同时抛弃。用本地日 K（2016-01 → 2026-11）拆成 9 条可证伪判据。
      </p>
      <DataTable cols={checkCols} rows={duration.checks} rowKey={r => r.k} />
      <Callout tone="ok" label="它给我们留下的东西">{duration.verdict}</Callout>
      <CrossLink to="evidence" anchor="duration" className="mt-3">回到「证据」章的久期图</CrossLink>
    </Fold>
  )
}

// ---------------------------------------------------------------- C 成本账

function CostArchive() {
  const secondCols: Col<(typeof cost.secondhand)[number]>[] = [
    { head: '叙事', render: r => <span className="whitespace-nowrap font-medium">{r.src}</span> },
    { head: '检验', render: r => <span className="text-secondary">{r.probe}</span> },
    {
      head: '结果',
      render: r => (
        <span className={r.tone === 'danger' ? 'text-danger' : r.tone === 'ok' ? 'text-accent' : 'text-warning'}>{r.res}</span>
      ),
    },
  ]

  const decayCols: Col<(typeof cost.decay.rows)[number]>[] = [
    { head: '维度', render: r => r.dim },
    { head: '大盘内', render: r => <span className={r.big >= 0 ? 'text-accent' : 'text-danger'}>{fmtBp(r.big)}</span> },
    { head: '小盘内', render: r => <span className={r.small >= 0 ? 'text-accent' : 'text-danger'}>{fmtBp(r.small)}</span> },
    { head: '判读', render: r => <span className={r.tone === 'ok' ? 'text-accent' : 'text-secondary'}>{r.verdict}</span> },
  ]

  return (
    <Fold id="archive-c" title="C · 这五个月的账：通过验收的规则 0 条" hint="1,378 提交 / 319 脚本 · 二手信息四类 + 传播层级">
      <StatGrid items={cost.cards} />
      <Callout tone="danger" label="按交易回报算">{cost.verdict}</Callout>

      <Tiles cols={3} className="mt-6">
        {cost.waste.map(w => (
          <Tile key={w.t} title={<span className="text-[13px]">{w.t}</span>} body={w.d} />
        ))}
      </Tiles>

      <h4 className="mt-9 text-[14px] font-medium text-foreground">
        「二手信息有信息量」是对的 —— 而且这条已经走过了
      </h4>
      <p className="mt-2 max-w-[46em] text-[13px] leading-[1.8] text-secondary">
        四类最典型的二手信息全部进过检验；第五行是唯一跨规模稳定的传播维度。它们不是「假的」，而是
        <b className="text-foreground">信息量在传到你之前已被换手率与成交额吸收干净</b>。
      </p>
      <DataTable cols={secondCols} rows={cost.secondhand} rowKey={r => r.src} />

      <div className="mt-8 border-t border-border pt-4">
        <div className="text-[14px] font-medium text-foreground">叙事质量三个维度 × 规模控制（H20 超额组间差，bp）</div>
        <p className="mt-1.5 text-[12px] leading-[1.7] text-muted">{cost.decay.note}</p>
        <DataTable cols={decayCols} rows={cost.decay.rows} rowKey={r => r.dim} dense />
        <p className="mt-3 text-[12px] leading-[1.7] text-warning">{cost.decay.gate}</p>
      </div>

      <Callout tone="ok" label="可检验性悖论">{cost.paradox}</Callout>

      <DataTable
        cols={[
          { head: '收到一条二手信息，先问', render: (c: (typeof cost.criteria)[number]) => c.q },
          { head: '答不上来的后果', render: (c: (typeof cost.criteria)[number]) => <span className="text-warning">{c.bad}</span> },
        ]}
        rows={cost.criteria}
        rowKey={c => c.q}
        reason="第 4 条最贵：能说出失效形态的信息才有头寸，说不出的只有观点。V1 还活着的唯一原因，是它第 4 条答得出来。"
      />

      <Callout tone="warn" label="现在最贵的动作">{cost.close}</Callout>
    </Fold>
  )
}

// ---------------------------------------------------------------- D 闸门

function GateArchive() {
  const denomCols: Col<(typeof gates.denom.rows)[number]>[] = [
    { head: '月份', render: r => <span className="whitespace-nowrap">{r.m}</span> },
    { head: 'ETF 成交额（亿）', render: r => <span className="font-mono tabular-nums">{r.etf.toLocaleString()}</span> },
    { head: '个股成交额（亿）', render: r => <span className="font-mono tabular-nums">{r.stk.toLocaleString()}</span> },
    { head: '占比', render: r => <span className={`font-mono font-medium tabular-nums ${r.m === '2026-10' ? 'text-danger' : ''}`}>{r.share.toFixed(2)}%</span> },
    { head: '沪深300', render: r => <span className={`font-mono tabular-nums ${r.hs < 0 ? 'text-danger' : 'text-accent'}`}>{r.hs.toFixed(2)}%</span> },
  ]

  const suspectCols: Col<(typeof gates.suspects)[number]>[] = [
    { head: '嫌疑人', render: r => <span className="whitespace-nowrap font-medium">{r.who}</span> },
    { head: '量级证据', render: r => <span className="text-secondary">{r.ev}</span> },
    { head: '方向', render: r => r.dir },
    {
      head: '判定',
      render: r => (
        <span className={`whitespace-nowrap font-medium ${r.tone === 'danger' ? 'text-danger' : r.tone === 'warn' ? 'text-warning' : 'text-secondary'}`}>
          {r.v}
        </span>
      ),
    },
  ]

  return (
    <Fold id="archive-d" title="D · 叙事的七道闸门：不跑数据先杀选项" hint="7 道 · 平安案例 · 分母实测 · 六嫌疑人">
      <p className="mt-4 max-w-[46em] text-[14px] leading-[1.8] text-secondary">{gates.intro}</p>
      <p className="mt-2 text-[12px] leading-[1.6] text-muted">{gates.tool}</p>

      <DataTable
        cols={[
          { head: '#', render: (g: (typeof gates.rows)[number]) => <span className="text-muted">{g.n}</span> },
          { head: '闸门', render: (g: (typeof gates.rows)[number]) => <span className="whitespace-nowrap font-medium">{g.name}</span> },
          { head: '一句话问法', render: (g: (typeof gates.rows)[number]) => g.ask },
          { head: '典型死法', render: (g: (typeof gates.rows)[number]) => <span className="text-secondary">{g.kill}</span> },
        ]}
        rows={gates.rows}
        rowKey={g => g.n}
        reason="七道全过才有资格进实证。大多数流行叙事死在第 1、2 道，连数据都不用跑。"
      />

      <h4 className="mt-9 text-[14px] font-medium text-foreground">{gates.caseTitle}</h4>
      <Tiles cols={3} className="mt-4">
        {gates.caseSteps.map(s => (
          <Tile
            key={s.step}
            lead={s.step}
            aside={<span className={s.tone === 'danger' ? 'text-danger' : 'text-warning'}>{s.verdict}</span>}
            title={<span className="text-[13px]">{s.why}</span>}
          />
        ))}
      </Tiles>
      <p className="mt-3 max-w-[46em] text-[12px] leading-[1.7] text-muted">
        三步里<b className="text-warning">第一步是真的</b>（确实亏了）、<b className="text-danger">第二步是误读</b>（重估≠卖出）、
        <b className="text-danger">第三步方向反了</b>（行业层面在增配）。
      </p>

      <div className="mt-8 border-t border-border pt-4">
        <div className="text-[14px] font-medium text-foreground">{gates.denom.head}</div>
        <DataTable cols={denomCols} rows={gates.denom.rows} rowKey={r => r.m} dense />
        <p className="mt-3 max-w-[46em] text-[12px] leading-[1.7] text-muted">{gates.denom.note}</p>
      </div>

      <h4 className="mt-9 text-[14px] font-medium text-foreground">六个嫌疑人过堂：同一份下跌，谁够格</h4>
      <DataTable cols={suspectCols} rows={gates.suspects} rowKey={r => r.who} />
      <p className="mt-3 max-w-[46em] text-[12px] leading-[1.7] text-muted">{gates.suspectsNote}</p>

      <Callout tone="warn" label="边界">{gates.boundary}</Callout>
    </Fold>
  )
}
