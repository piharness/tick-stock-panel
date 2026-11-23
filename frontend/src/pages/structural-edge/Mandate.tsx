/**
 * 「总则」章 —— 只放已经被一次预注册或一次裁决固定下来的判断。
 *
 * 一本手册的总则回答四个问题，顺序不能换：
 *   01 你在赌什么（优势来自约束集，不来自预测）
 *   02 六条不做（每一条都对应一次「很想做、被数据否掉」）
 *   03 真轴是构成（仓位择时想做的事，数据给的替代方案）
 *   04 代价与边界（它不能做什么；不承认这一节，前面三节就是吹牛）
 *
 * 内容一半来自原「结构优势」，一半来自原「仓位艺术」—— 合并后它们第一次
 * 出现在同一屏：被证伪的规则（03）与未被证伪的规则（规则章）不再是两个页面。
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type EtfWatchAccount, type EtfWatchPayload, type PositionArtTail } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { boundary, ENV_LABEL, envGate, envVerdicts, mandate, refusals, type Refusal } from './data'
import { accountReading, v1Reading } from './live'
import { CrossLink, TabLayout } from './Shell'
import { Cadence, Callout, DataTable, type Col, Section, StatGrid, Tag } from './ui'

export function MandatePanel({ payload, account }: { payload: EtfWatchPayload | null; account: EtfWatchAccount | null }) {
  return (
    <TabLayout
      tab="mandate"
      items={[
        { id: 'mandate', no: '01', label: '你在赌什么' },
        { id: 'refuses', no: '02', label: '六条不做' },
        { id: 'composition', no: '03', label: '真轴是构成' },
        { id: 'boundary', no: '04', label: '代价与边界' },
        { id: 'env', no: '05', label: '制度环境' },
      ]}
    >
      <ThesisBlock />
      <RefusalBlock />
      <CompositionBlock />
      <BoundaryBlock payload={payload} account={account} />
      <EnvBlock />
    </TabLayout>
  )
}

// ---------------------------------------------------------------- 01 赌什么

function ThesisBlock() {
  return (
    <Section
      id="mandate"
      no="01"
      title="你在赌什么：不预测方向，只在深跌里接一手"
      first
      lead={mandate.lead}
      source="docs/structural-edge/README.md §0 · verdict.md · forced-counterparty-ledger.md"
      actions={<Cadence kind="frozen" />}
    >
      <StatGrid items={mandate.cards} />
      <Callout tone={mandate.edgeTone} label="「凭什么是我」的正式回答">
        {mandate.edge}
      </Callout>
    </Section>
  )
}

// ---------------------------------------------------------------- 02 六条不做

function RefusalBlock() {
  const cols: Col<Refusal>[] = [
    {
      head: '不做',
      w: '16%',
      render: r => (
        <span className="whitespace-nowrap">
          <span className="mr-1.5 font-mono text-[10px] text-muted">{r.no}</span>
          <span className="font-medium">{r.act}</span>
        </span>
      ),
    },
    { head: '因为', w: '46%', render: r => <span className="text-secondary">{r.why}</span> },
    {
      head: '证据',
      w: '22%',
      render: r => (
        <CrossLink to={r.to} anchor={r.anchor} className="whitespace-nowrap">
          {r.proof}
        </CrossLink>
      ),
    },
    {
      head: '环境',
      w: '16%',
      render: r => {
        const e = ENV_LABEL[r.env]
        return <span title={e.hint}><Tag tone={e.tone}>{e.text}</Tag></span>
      },
      cls: 'whitespace-nowrap',
      headCls: 'whitespace-nowrap',
    },
  ]

  return (
    <Section
      id="refuses"
      no="02"
      title="六条不做：每一条都花过一次返工"
      lead="这一节不是自我表扬，是给下一次动摇留的账单。六条里有五条是「本来很想做、被数据否掉」，剩下一条（不买个股）连想都不用想 —— 它对左尾的贡献比对右尾大。"
      source="每条的去处在最右列；总则不重复数字，只保留判断"
      actions={<Cadence kind="frozen" />}
    >
      <DataTable cols={cols} rows={refusals} rowKey={r => r.no} minWidth={820} />
      <Callout tone="muted" label="读这一节的方法">
        先看你今天最想破哪一条，再点进证据 —— 这六条里任何一条被推翻，都会先以「重跑实验 + 改 data.ts + 同 commit 改 md」的形式出现，
        不会以「这次不一样」的形式出现。
      </Callout>
      <p className="mt-2 text-[10.5px] leading-relaxed text-secondary">
        「环境」列不是难度，是适用域：<b className="font-medium text-foreground">制度无关</b> 的可以无条件带走；
        <b className="font-medium text-foreground">依赖托底</b> 的收益里含制度租金，托底失效时可能翻转 ——
        定义与三组分类见 <CrossLink to="mandate" anchor="env" className="align-baseline">05 · 制度环境</CrossLink>。
      </p>
    </Section>
  )
}

// ---------------------------------------------------------------- 05 制度环境

/**
 * 这一节不是第七个结论，是给前四节标适用域：哪几条换了制度仍然成立，
 * 哪几条的收益里含制度租金。判断依据只有一条 —— 托底削左尾、不动中位数。
 */
function EnvBlock() {
  const cols: Col<(typeof envVerdicts)[number]>[] = [
    { head: '结论', render: r => <span className="font-medium">{r.claim}</span>, w: '26%' },
    {
      head: '环境',
      w: '12%',
      render: r => {
        const e = ENV_LABEL[r.env]
        return <span title={e.hint}><Tag tone={e.tone}>{e.text}</Tag></span>
      },
      cls: 'whitespace-nowrap',
      headCls: 'whitespace-nowrap',
    },
    { head: '托底失效后会怎样', render: r => <span className="text-secondary">{r.flip}</span>, w: '62%' },
  ]

  return (
    <Section
      id="env"
      no="05"
      title="制度环境：结论也来自约束集"
      lead="第 01 节说优势来自约束集（慢、小、丑、容量小）。同一句话还有下半句 —— 结论也来自约束集。这一节不产生新结论，只给前四节标适用域。"
      source="docs/national-team-follow.md §0 §7 §10 · docs/structural-edge/README.md"
      actions={<Cadence kind="frozen" />}
    >
      <StatGrid
        cols={3}
        items={[
          { k: '≤−5% 单日', v: '4.9 → 1.0', d: '次/年 · 1991–2015 vs 2016–2026' },
          { k: '≤−8% 单日', v: '28 → 0', d: '次 · 同上两个窗口' },
          { k: '制度样本', v: 'N=1', d: '16 个独立月全在托底常态内' },
        ]}
      />
      <p className="mt-3 text-[13px] leading-relaxed text-secondary">
        {envGate.lead}{envGate.effect}
      </p>
      <p className="mt-1.5 text-[11px] leading-relaxed text-muted">{envGate.effectNote}</p>

      <DataTable
        cols={cols}
        rows={envVerdicts}
        rowKey={r => r.claim}
        minWidth={720}
        reason="顺序按依赖强度排：依赖托底的四条先列，反向增强与制度无关的放后面作对照。"
      />

      <Callout tone="danger" label="承保的真实空头">
        {envGate.underwrite}
      </Callout>
      <Callout tone="muted" label="为什么「再多跑几年」在这里无效">
        {envGate.ident}
      </Callout>
    </Section>
  )
}

// ---------------------------------------------------------------- 03 构成轴

/** 左尾概率 → 色阶。低=安全(淡)，高=危险(深)。 */
function tailBg(p: number | undefined, max = 23): string {
  if (p == null) return 'transparent'
  const t = Math.min(1, Math.max(0, p / max))
  return `rgba(192, 57, 43, ${(0.08 + t * 0.75).toFixed(3)})`
}

function tailFg(p: number | undefined, max = 23): string {
  if (p == null) return 'inherit'
  return p / max > 0.45 ? '#fff' : 'inherit'
}

/** 5×5 热力图：行=市值五分位，列=换手五分位。左上/右下是极端格。 */
function TailGrid({ tail }: { tail: PositionArtTail }) {
  const cells = tail.cells ?? []
  const byKey = useMemo(() => {
    const m = new Map<string, (typeof cells)[number]>()
    for (const c of cells) m.set(`${c.mq}-${c.tq}`, c)
    return m
  }, [cells])
  return (
    <div className="overflow-x-auto">
      <table className="border-collapse text-xs">
        <thead>
          <tr>
            <th className="px-2 py-1 text-right font-normal text-muted">市值＼换手</th>
            {[0, 1, 2, 3, 4].map(tq => (
              <th key={tq} className="px-2 py-1 text-center font-normal text-muted">
                Q{tq + 1}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[4, 3, 2, 1, 0].map(mq => (
            <tr key={mq}>
              <td className="px-2 py-1 text-right text-muted">Q{mq + 1}</td>
              {[0, 1, 2, 3, 4].map(tq => {
                const c = byKey.get(`${mq}-${tq}`)
                return (
                  <td
                    key={tq}
                    className="px-2 py-1 text-center tabular-nums"
                    style={{ background: tailBg(c?.p_tail_pct), color: tailFg(c?.p_tail_pct) }}
                    title={c ? `n=${c.n}，中位 ${c.med_pct}%` : '样本不足'}
                  >
                    {c ? `${c.p_tail_pct}%` : '—'}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CompositionBlock() {
  const { data: t } = useQuery({
    queryKey: QK.positionArtTail,
    queryFn: () => api.positionArtTail(),
    staleTime: 5 * 60 * 1000,
    retry: false,
  })

  const stateRatio = t?.by_state && t.by_state.length >= 2
    ? (
        Math.max(...t.by_state.map(x => x.p_tail_pct)) /
        Math.max(0.1, Math.min(...t.by_state.map(x => x.p_tail_pct)))
      ).toFixed(1)
    : '—'

  return (
    <Section
      id="composition"
      no="03"
      title="真正该动的轴：构成，不是仓位"
      lead="仓位择时想做的是「跌之前空仓」。数据给出的替代方案更朴素：不动仓位，动构成 —— 换手率与市值决定你承担多大的左尾概率，而仓位只决定你承担几份。"
      source="GET /api/position-art/tail · docs/structural-edge/2026-duration-review.md · state-timing-audit.md"
      actions={<Cadence kind="experiment" note="读数随接口刷新，结论冻结" />}
    >
      <p className="text-[13px] leading-relaxed text-secondary">
        {t?.year ?? ''} 年 P（20 日内跌超 {t?.cut_pct ?? 20}%）：状态轴最差与最好差{' '}
        {stateRatio} 倍，而换手 × 市值的 25 格差 {t?.ratio ?? '—'} 倍。
        {t?.best && t?.worst
          ? `最安全格（市值 Q${(t.best.mq ?? 0) + 1}/换手 Q${(t.best.tq ?? 0) + 1}）${t.best.p_tail_pct}%，最危险格（市值 Q${(t.worst.mq ?? 0) + 1}/换手 Q${(t.worst.tq ?? 0) + 1}）${t.worst.p_tail_pct}%。`
          : ''}
      </p>

      <div className="mt-3 grid gap-4 md:grid-cols-2">
        <div className="rounded-lg border border-border bg-surface px-3 py-3">
          <div className="mb-2 text-xs text-muted">换手 × 市值（行=市值，列=换手）</div>
          {t?.available ? <TailGrid tail={t} /> : <div className="text-xs text-muted">构成轴数据不可用</div>}
        </div>
        <div className="rounded-lg border border-border bg-surface px-3 py-3">
          <div className="mb-2 text-xs text-muted">按市场状态</div>
          <table className="w-full border-collapse text-xs">
            <thead>
              <tr>
                <th className="py-1 text-left font-normal text-muted">状态</th>
                <th className="py-1 text-right font-normal text-muted">P(&lt;−20%)</th>
                <th className="py-1 text-right font-normal text-muted">20 日中位</th>
              </tr>
            </thead>
            <tbody>
              {(t?.by_state ?? []).map(s => (
                <tr key={s.mkt_state}>
                  <td className="py-1">
                    {s.mkt_state === 'down' ? '下行' : s.mkt_state === 'up' ? '上行' : '横盘'}
                  </td>
                  <td className="py-1 text-right tabular-nums">{s.p_tail_pct}%</td>
                  <td className="py-1 text-right tabular-nums">{s.med_pct}%</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-2 text-xs leading-5 text-muted">
            「横盘」不是安全状态：它是被证伪最彻底的那一格 —— 想等它再进场，等的是全样本里
            左尾最肥的那个状态。
          </div>
        </div>
      </div>

      <Callout tone="danger" label="所以仓位择时错在哪">
        它把「跌」当成一个可以用状态标签提前识别的对象，于是反复在「最危险的那一格」离场、又在修复后回场。
        真正能把左尾概率拉开一个数量级的是持有结构（换手 × 市值），不是仓位开关 —— 完整验收在{' '}
        <CrossLink to="evidence" anchor="sizing-audit" className="align-baseline">证据 · 仓位择时的验收</CrossLink>。
      </Callout>
    </Section>
  )
}

// ---------------------------------------------------------------- 04 边界

function BoundaryBlock({ payload, account }: { payload: EtfWatchPayload | null; account: EtfWatchAccount | null }) {
  const v1 = v1Reading(payload)
  const acct = accountReading(account)
  // 两行是每日读数：data.ts 只留口径，值在这里填。缺数据时给「—」，不回退旧值。
  const rows = boundary.map(r => {
    if (r.k === 'V1 判据') return { ...r, v: v1 ? `${v1.progress} 簇` : '—' }
    if (r.k === '账户性质') {
      return { ...r, d: acct ? `初始 ${acct.initWan} 万，${acct.adoption} 启动。${r.d}` : r.d }
    }
    return r
  })
  const cols: Col<(typeof boundary)[number]>[] = [
    { head: '项', render: r => <span className="whitespace-nowrap font-medium">{r.k}</span>, w: '18%' },
    { head: '读数', render: r => <span className="whitespace-nowrap font-mono tabular-nums">{r.v}</span>, w: '20%' },
    { head: '这意味着什么', render: r => <span className="text-secondary">{r.d}</span>, w: '62%' },
  ]

  return (
    <Section
      id="boundary"
      no="04"
      title="代价与边界：先说清楚它不能做什么"
      lead="这一节是给前面三节打折用的。一个策略的可信度，等于它愿意承认自己不知道什么；不愿承认的部分，最后都会以回撤的形式补回来。"
      source="holding-and-exit.md §6 · portfolio-replay.md §7 · account-runbook.md"
      actions={<Cadence kind="frozen" />}
    >
      <DataTable cols={cols} rows={rows} rowKey={r => r.k} minWidth={680} />
      <Callout tone="warn" label="如果这几条你不接受，这套策略不适合你">
        它会让你大多数交易日什么都不做，可能连续几年没有信号；它的证据只够说「不是噪声」，不够说「可以上规模」；
        账户现在是纸面的；而唯一候选规则还要等样本。接受这些之后，剩下的才是收益。
      </Callout>
    </Section>
  )
}
