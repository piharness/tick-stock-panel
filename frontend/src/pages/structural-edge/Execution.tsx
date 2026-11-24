/**
 * 机会雷达的可复用卡片：账户指令、现金腿、反例登记、事件账本、触发四步。
 *
 * 2026-11-24 手册拆分后，原「执行」章升级为独立页面 /handbook（Daily.tsx）；
 * 本文件只保留那页真正渲染的内容块。其中 RegisterCard / Ledger / NextSteps
 * 也导出给 Daily 使用 —— 它们的数据只来自 /api/etf-watch 与 /api/position-art，
 * 页面里不写任何阈值字面量（阈值来自 payload.meta.thresholds）。
 */
import type { ReactNode } from 'react'
import type { EtfWatchAccount, EtfWatchCashLeg, EtfWatchPayload, PositionArtPayload } from '@/lib/api'
import { cn } from '@/lib/cn'
import { CrossLink } from './Shell'
import { Callout, DataTable, type Col, Empty, StatGrid, Tag, Tile } from './ui'

const pct = (v: number) => `${(v * 100).toFixed(1)}%`

type Ev = EtfWatchPayload['ledger']['events'][number]


// 以下三个卡片组件被机会雷达（/handbook · Daily.tsx）复用；
// 原来的「执行」章面板已随 2026-11-24 的手册拆分迁往那里。

// ---------------------------------------------------------------- 02 账户

const fmtMoney = (v: number) => Math.round(v).toLocaleString('zh-CN')

export function AccountCard({ account, err }: { account: EtfWatchAccount | null; err: string }) {
  if (!account) {
    return <Empty text={`先跑 python analysis/etf_watch_account.py 生成账户快照${err ? `（${err}）` : ''}。`} />
  }
  const s = account.state
  const stats = [
    { k: '权益', v: fmtMoney(s.equity), d: `纸面 · 初始 ${fmtMoney(account.meta.init)}` },
    { k: '债券 511010', v: fmtMoney(s.defensive.value), d: `占比 ${(s.defensive.weight * 100).toFixed(1)}%` },
    {
      k: '腿 1 持仓',
      v: s.leg1.positions.length ? `${s.leg1.positions.length} 只` : '空仓',
      d: s.leg1.positions.length ? fmtMoney(s.leg1.value) : '等待下一个段首信号',
    },
    { k: '现金', v: fmtMoney(s.cash), d: '货币基金口径计息' },
  ]
  return (
    <div>
      <StatGrid items={stats} />
      {account.instructions.length ? (
        <ol className="mt-7 border-t border-border">
          {account.instructions.map((i: EtfWatchAccount['instructions'][number]) => (
            <li key={i.no} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border/60 py-2.5 text-[13px]">
              <span className={cn(
                'w-8 shrink-0 text-[11px] font-medium tracking-[0.06em]',
                i.action === 'BUY' ? 'text-bull' : i.action === 'SELL' ? 'text-bear' : 'text-muted',
              )}>
                {i.action === 'BUY' ? '买入' : i.action === 'SELL' ? '卖出' : '不动'}
              </span>
              <span className="font-medium text-foreground">{i.what}</span>
              <span className="font-mono tabular-nums text-secondary">≈{fmtMoney(i.est_amount)}</span>
              {i.limit_price != null && (
                <span className="font-mono text-[12px] tabular-nums text-foreground">限价 {i.limit_price.toFixed(3)}</span>
              )}
              <span className="text-[12px] text-muted">{i.why}</span>
              <span className="ml-auto shrink-0 text-[12px] text-muted">{i.when}</span>
            </li>
          ))}
        </ol>
      ) : (
        /* 无指令 ≠ 没有答案。每天要回答的正是「明天什么都不做对不对」，
         * 所以无指令时把三条腿的「不操作」逐条列出来，而不是一句带过。 */
        <div className="mt-7 border-t border-border">
          <TodoRow label="腿 1" action="明日无操作" why={s.leg1.positions.length ? `持仓 ${s.leg1.positions.length} 只，未收到卖出指令` : '空仓 · 无新信号'} />
          <TodoRow label="债券" action="明日无操作" why={`占比 ${(s.defensive.weight * 100).toFixed(1)}%，已对齐目标`} />
          <TodoRow label="现金" action="不动" why="货币基金口径计息" />
        </div>
      )}
      <p className="mt-4 max-w-[46em] text-[12px] leading-[1.7] text-muted">
        {account.meta.entry?.note ?? '买入是次日开盘限价（信号收盘 ×1.03 封顶），未成交即放弃；回测仍按 t+1 收盘（保守口径）。'}
        {' '}{account.asof} 收盘 · 执行层与 V1 判定层独立 · 金额按最近收盘估算，实际按成交时权益计算 ·
        流程见 docs/structural-edge/account-runbook.md
      </p>
    </div>
  )
}

/**
 * 现金腿卡：短融打底 + 国债锚的下单计划。
 *
 * 份数、金额、佣金全部来自 cash_leg.json —— 页面不做任何取整。
 * 同一笔单在页面、CLI、账户脚本三处各取整一次，就会得到三个答案（整手、佣金下限、
 * 权益取最近收盘还是成交时），这类分歧不会报错，只会在对账时变成几十元的缺口。
 */
export function CashLegCard({ leg, err }: { leg: EtfWatchCashLeg | null; err: string }) {
  if (!leg) {
    return (
      <Empty
        text={`先跑 python analysis/etf_watch_account.py 生成账户快照（现金腿会顺带产出）${err ? `（${err}）` : ''}。`}
      />
    )
  }
  const stats = [
    { k: '权益', v: fmtMoney(leg.equity), d: `腿 1 ${fmtMoney(leg.leg1)} · 可用 ${fmtMoney(leg.available)}` },
    {
      k: `国债锚 ${(leg.bond_w * 100).toFixed(0)}%`,
      v: fmtMoney(leg.bond_amount),
      d: `目标 ${fmtMoney(leg.bond_target)} · 偏离 ${leg.deviation >= 0 ? '+' : ''}${fmtMoney(leg.deviation)}`,
    },
    { k: '短融底盘', v: fmtMoney(leg.short_amount), d: `留现金 ${fmtMoney(leg.residual)}` },
    { k: '久期暴露', v: `${leg.duration_years} 年`, d: `再平衡阈值 ±${fmtMoney(leg.rebalance_min)}` },
  ]
  return (
    <div>
      <StatGrid items={stats} />
      {leg.orders.length ? (
        <ol className="mt-7 border-t border-border">
          {leg.orders.map(o => (
            <li
              key={o.no}
              className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border/60 py-2.5 text-[13px]"
            >
              <span className={cn(
                'w-8 shrink-0 text-[11px] font-medium tracking-[0.06em]',
                o.action === 'BUY' ? 'text-bull' : 'text-bear',
              )}>
                {o.action === 'BUY' ? '买入' : '卖出'}
              </span>
              <span className="font-medium text-foreground">{o.name}</span>
              <span className="font-mono tabular-nums text-secondary">
                {o.shares.toLocaleString('zh-CN')} 份 × {o.price.toFixed(3)} = ≈{fmtMoney(o.amount)}
              </span>
              <span className="text-[12px] text-muted">佣金 {o.fee.toFixed(2)}</span>
              <span className="text-[12px] text-muted">{o.why}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="mt-7 border-t border-border pt-3 text-[13px] text-secondary">
          无下单指令：两档已在目标内。
        </p>
      )}
      <div className="mt-6 grid gap-x-8 gap-y-2 text-[12px] leading-[1.7] text-muted sm:grid-cols-3">
        <div>
          <b className="font-medium text-foreground">R1 国债锚</b>：{leg.meta.rules.R1}
        </div>
        <div>
          <b className="font-medium text-foreground">R2 底盘吸收</b>：{leg.meta.rules.R2}
        </div>
        <div>
          <b className="font-medium text-foreground">R3 缺钱顺序</b>：{leg.meta.rules.R3}
        </div>
      </div>
      <p className="mt-4 max-w-[46em] text-[12px] leading-[1.7] text-muted">
        {leg.meta.asof} 收盘 · 计算只发生在 <span className="font-mono">analysis/etf_watch_cash_leg.py</span>
        一处，页面与 CLI 都读它的产出 · 换标的改脚本顶部的 BOND / SHORT / BOND_W，流程不动 ·
        QDII 类标的先过溢价闸（溢价 &gt; 0.5% 不买）· 详见 docs/structural-edge/account-runbook.md §7
      </p>
    </div>
  )
}

/**
 * 反例登记卡：那条被证伪的仓位规则今天的读数。
 *
 * 它不做指令，只用陈述句写「这条规则会说什么」，并把验收结果压在同一张卡里。
 * 语气必须与扫描器区分开：扫描器说「可下注 / 不可下注」，这里说「它会这样说」。
 */
export function RegisterCard({ art, err }: { art: PositionArtPayload | null; err: string }) {
  const today = art?.today
  const chosen = art?.chosen
  const bench = art?.benchmark
  const passed = (art?.checks ?? []).filter(c => c.pass).length
  const total = (art?.checks ?? []).length

  if (!today) {
    return (
      <Empty
        text={
          err
            ? `反例读数不可用（${err}）。扫描与账户不受影响。`
            : '正在重算十年等权指数与状态序列（首次约 3 秒）…'
        }
      />
    )
  }

  return (
    <div>
      <div className="grid grid-cols-2 gap-x-7 gap-y-6 md:grid-cols-4">
        <div className="border-t border-border pt-3">
          <div className="text-[12px] leading-4 text-muted">市场状态（t−1 判定）</div>
          <div className="mt-2 text-[18px] font-semibold leading-none text-foreground">
            {today.state === 'down' ? '下行' : today.state === 'up' ? '上行' : '横盘'}
          </div>
          <div className="mt-2 text-[12px] leading-[1.65] text-muted">已持续 {today.state_age} 个交易日</div>
        </div>
        <div className="border-t border-border pt-3">
          <div className="text-[12px] leading-4 text-muted">这条规则会说</div>
          <div className="mt-2 text-[18px] font-semibold leading-none text-foreground">{today.advice}</div>
          <div className="mt-2 text-[12px] leading-[1.65] text-muted">
            自 {today.since} 起，第 {today.days_in_stance} 天
          </div>
        </div>
        <div className="border-t border-border pt-3">
          <div className="text-[12px] leading-4 text-muted">它什么时候改口</div>
          <div className="mt-2 text-[13px] leading-[1.6] text-foreground">{today.trigger}</div>
        </div>
        <div className="border-t border-border pt-3">
          <div className="text-[12px] leading-4 text-muted">60 日涨幅（等权）</div>
          <div
            className={cn(
              'mt-2 font-mono text-[22px] font-semibold leading-none tabular-nums',
              today.ret60_pct == null ? 'text-muted' : today.ret60_pct > 0 ? 'text-bull' : 'text-bear',
            )}
          >
            {today.ret60_pct == null ? '—' : `${today.ret60_pct > 0 ? '+' : ''}${today.ret60_pct}%`}
          </div>
          <div className="mt-2 text-[12px] leading-[1.65] text-muted">as_of {art?.as_of}</div>
        </div>
      </div>

      <Callout tone="danger" label="反例 · 不构成指令">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <Tag tone="danger">未通过验收</Tag>
          <span>
            十年 {chosen?.cagr_pct}% / 回撤 {chosen?.mdd_pct}%，全程持有 {bench?.cagr_pct}% /{' '}
            {bench?.mdd_pct}%；验收 {passed}/{total} 通过。
          </span>
        </div>
        <p className="mt-1.5">
          它今天说「{today.advice}」，理由是一个状态标签。扫描器也可能说「不可下注」，理由是位置 × 形态 × 容量。
          两句话看起来一样，来源不同 —— 不能相加，也不能互相替代。想空仓躲一躲的时候，先看这张卡。
        </p>
      </Callout>
    </div>
  )
}

/** 顶部常驻状态。与 ExecutionPanel 共用同一份 payload，不重复取数。 */


// ---------------------------------------------------------------- 03 账本

export function Ledger({ payload }: { payload: EtfWatchPayload }) {
  const evCols: Col<Ev>[] = [
    { head: '日期', render: e => <span className="font-mono text-[11px] tabular-nums">{e.date}</span> },
    { head: '行业', render: e => e.industry },
    { head: 'dd', render: e => <span className="font-mono tabular-nums">{pct(e.dd)}</span> },
    {
      head: '信号',
      render: e => (
        <Tag tone={e.signal ? (e.qualified ? 'ok' : 'warn') : 'muted'}>
          {e.signal ? (e.qualified ? '高波·可下注' : '高波·被排除') : '低波·已弃'}
        </Tag>
      ),
    },
    {
      head: '20 日相对',
      render: e => {
        const s = payload.ledger.settles.find(x => x.id === e.id)
        return (
          <span className={cn('font-mono tabular-nums', s ? (s.rel20 >= 0 ? 'text-bull' : 'text-bear') : 'text-muted')}>
            {s ? `${(s.rel20 * 1e4).toFixed(0)}bp` : '待到期'}
          </span>
        )
      },
    },
  ]

  const th = payload.meta.thresholds
  const v1 = payload.ledger.v1
  const events = payload.ledger.events

  return (
    <div>
      {events.length === 0 ? (
        <Empty text="账本还是空的 —— 第一条信号会在触发日写入。" />
      ) : (
        <DataTable
          cols={evCols}
          rows={events}
          rowKey={e => e.id}
          dense
          reason={`${events.length} 条 · 只增不改 · 同月多行业记为 1 簇`}
        />
      )}

      <div className="mt-6 grid gap-x-8 gap-y-3 text-[12px] leading-[1.75] text-muted sm:grid-cols-2">
        <div>
          <b className="font-medium text-secondary">① 数簇不数行</b>：同一天多个行业一起跌破，是一波行情里的多个标签，
          不是多次机会。研究段的独立月数远少于事件行数，正是这个坑；按行计数会把 1 次显示成「N/N 达成」。
        </div>
        <div>
          <b className="font-medium text-secondary">② p10 换成 MAE</b>：{v1.left_tail_mapping || '样本太少估不出 10% 分位，V1 改用每簇持有期最差收盘浮亏。'}
        </div>
        <div className="sm:col-span-2">
          <b className="font-medium text-secondary">阈值</b>：dd ∈ ({pct(th.deep)}, {pct(th.low)}]、{payload.meta.vol_convention} &gt;{' '}
          {(th.vol_hi * 100).toFixed(0)}%、市场状态 ≠ 上行·低波、载体 20 日均额 ≥ {(th.min_amt20 / 1e4).toFixed(0)} 万。
          口径与研究段同源，<span className="font-mono">--verify</span> 可机械核对。
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- 04 四步

const NEXT_STEPS: Array<{ n: string; title: string; desc: ReactNode; to: 'rules' | 'evidence'; anchor: string }> = [
  {
    n: '1',
    title: '确认载体',
    desc: <>同行业成交额最大的一只（F6）；容量不够就换标的，不降级买小只。</>,
    to: 'rules',
    anchor: 'buy',
  },
  {
    n: '2',
    title: '复核不买清单',
    desc: <>六条硬条件逐条过，任一条成立就放弃 —— 不设例外，也不因为「这次不一样」开口子。</>,
    to: 'rules',
    anchor: 'stop',
  },
  {
    n: '3',
    title: '按六格定仓位',
    desc: <>每笔 = 当时权益/6（≈16.7%）；不用 1/vol 权重，六只高相关，不当成六注。</>,
    to: 'rules',
    anchor: 'sizing',
  },
  {
    n: '4',
    title: '记录，不追补',
    desc: <>触发日收盘确认 → 次日开盘限价（信号收盘 +3% 封顶），未成交即放弃；窗口过期不补票。</>,
    to: 'rules',
    anchor: 'exits',
  },
]

export function NextSteps() {
  return (
    <ol className="mt-5 grid gap-x-8 gap-y-7 sm:grid-cols-2">
      {NEXT_STEPS.map(s => (
        <li key={s.n}>
          <Tile
            lead={s.n}
            title={s.title}
            body={s.desc}
            foot={<CrossLink to={s.to} anchor={s.anchor}>对应的规则</CrossLink>}
          />
        </li>
      ))}
    </ol>
  )
}

/** 明日待办行：空复选框 = 明天收盘后逐条核对过的「不做」，不是装饰。 */
function TodoRow({ label, action, why }: { label: string; action: string; why: string }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-border/60 py-2.5 text-[13px]">
      <span aria-hidden className="mt-[0.3em] h-3.5 w-3.5 shrink-0 rounded-[3px] border border-border" />
      <span className="w-8 shrink-0 text-[11px] font-medium tracking-[0.06em] text-muted">{label}</span>
      <span className="font-medium text-foreground">{action}</span>
      <span className="text-[12px] text-muted">{why}</span>
    </div>
  )
}
