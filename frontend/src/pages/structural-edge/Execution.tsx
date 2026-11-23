/**
 * 「执行」章 —— 全页主要的活数据。
 *
 * 组织顺序刻意按「收盘后 3 分钟能不能做完决定」排：
 *   结论（有没有四条全过）→ 账户指令（明天收盘做什么）→ 判定明细
 *   → 事件账本（V1 怎么记账）→ 触发后的四步 → 反例登记（那条被证伪的规则今天在说什么）
 *
 * 最后一节是合并「仓位艺术」之后新增的：被证伪的规则与可执行的规则第一次同屏。
 * 它放在最后，是为了不打断动手的动线；但每天都会看见一次 —— 这是它存在的全部理由。
 *
 * 数据只来自 /api/etf-watch（+ /api/etf-watch/account、/api/position-art）；
 * 这个文件里不写任何阈值/收益字面量 —— 阈值来自 payload.meta.thresholds，
 * 口径来自 payload.meta.*_convention。上一版页面把位置梯度内联进 JSX，与研究脚本差了 30bp 而不自知。
 */
import type { ReactNode } from 'react'
import type { EtfWatchAccount, EtfWatchCashLeg, EtfWatchPayload, PositionArtPayload } from '@/lib/api'
import { ArrowRight, CircleDot } from 'lucide-react'
import { cn } from '@/lib/cn'
import { CrossLink, TabLayout } from './Shell'
import { ScanTable } from './ScanTable'
import { accountReading, asOfDrift, cashLegReading, lagSessions } from './live'
import { Cadence, Callout, DataTable, type Col, Section, StatGrid, Tag } from './ui'

const pct = (v: number) => `${(v * 100).toFixed(1)}%`

type Ev = EtfWatchPayload['ledger']['events'][number]

export function ExecutionPanel({
  payload, err, account, accountErr, cashLeg, cashErr, art, artErr,
}: {
  payload: EtfWatchPayload | null
  err: string
  account: EtfWatchAccount | null
  accountErr: string
  cashLeg: EtfWatchCashLeg | null
  cashErr: string
  art: PositionArtPayload | null
  artErr: string
}) {
  const lag = lagSessions(payload)
  const acct = accountReading(account)
  const leg = cashLegReading(cashLeg)
  return (
    <TabLayout
      tab="today"
      items={[
        { id: 'today', no: '01', label: '今日结论' },
        { id: 'account', no: '02', label: '账户指令' },
        { id: 'cash', no: '03', label: '现金腿' },
        { id: 'scan', no: '04', label: '判定明细' },
        { id: 'ledger', no: '05', label: '事件账本' },
        { id: 'next', no: '06', label: '触发后的四步' },
        { id: 'register', no: '07', label: '反例登记' },
      ]}
    >
      <Section
        id="today"
        no="01"
        title="盘后扫描：今天有没有可下注的"
        first
        lead={
          <>
            判定口径只有一份（研究脚本），页面不内联任何副本。这一节每个交易日收盘后由{' '}
            <span className="font-mono text-[11px]">analysis/etf_watch.py</span> 重写。
          </>
        }
        source="GET /api/etf-watch → data/etf_watch/latest.json（判定快照，不含下单）"
        actions={<Cadence kind="daily" />}
        lag={lag}
      >
        <TodayHero payload={payload} err={err} />
      </Section>

      <Section
        id="account"
        no="02"
        title="账户指令：腿 1 + 债券叠加（已启用）"
        lead={`运营账户${acct ? `自 ${acct.adoption} 收盘` : ''}以全现金启动（纸面，初始金额以卡片为准）。引擎与回测同一套：卖出按收盘，买入按次日开盘限价。四项状态是「执行前」的快照，所以债券一栏会先显 0。`}
        source="analysis/etf_watch_account.py → /api/etf-watch/account"
        actions={<Cadence kind="daily" />}
        asOf={acct?.asof}
        drift={asOfDrift('账户', acct?.asof, payload?.meta.asof)}
      >
        <AccountCard account={account} err={accountErr} />
      </Section>

      <Section
        id="cash"
        no="03"
        title="现金腿：短融打底 + 国债锚（影子方案）"
        lead="腿 1 空仓时钱放在哪。国债锚按权益定、短融吸收腿 1 的进出 —— 形状由回撤预算决定，不由对行情的判断决定。"
        source="etf_watch_account.py ② → etf_watch_cash_leg.py::plan → /api/etf-watch/cash-leg"
        actions={<Cadence kind="daily" note="与账户同源" />}
        asOf={leg?.asof}
        drift={asOfDrift('现金腿', leg?.asof, payload?.meta.asof)}
      >
        <Callout tone="warn" label="政策关系：这一节不是 02 的下单依据">
          02 用的是账户曲线在跑的 JB0：<b className="text-foreground">债券 = 权益 − 腿 1</b>。本节是
          {leg ? ` ${leg.asof} 数据日` : '当前数据日'}的候选方案 R1/R2/R3：
          <b className="text-foreground">国债 = 40% × 权益</b>、短融吸收剩余，验收期 12 个月，尚未进入账户曲线。
          同一天两者给出不同金额时，以 02 为准；不要按本节的份数下单。
        </Callout>
        <CashLegCard leg={cashLeg} err={cashErr} />
      </Section>

      <Section
        id="scan"
        no="04"
        title="判定明细：四条判据逐行业"
        lead="按判定优先级排序，同档按回撤深度。CANDIDATE 才是四条全过；「区间内被排除」是位置对、形态或容量不过。"
        source={payload ? `口径：${payload.meta.dd_convention} · ${payload.meta.vol_convention}` : 'analysis/etf_watch.py'}
        actions={<Cadence kind="daily" />}
        asOf={payload?.meta.asof}
        lag={lag}
      >
        {payload
          ? <ScanTable payload={payload} swipeHint />
          : <EmptyBlock text="扫描结果未就绪，判定表暂不可用。先看上方命令。" />}
      </Section>

      <Section
        id="ledger"
        no="05"
        title="事件账本：V1 只数簇，不数行"
        lead="账本只增不改。同一天多个行业一起触发，记为一个事件簇 —— 那是同一波行情的多个标签，不是多次机会。"
        source="ledger.events 只增不改 · 同月多行业记为 1 簇"
        actions={<Cadence kind="daily" note="只追加" />}
        asOf={payload?.meta.asof}
        lag={lag}
      >
        {payload
          ? <Ledger payload={payload} />
          : <EmptyBlock text="还没有账本记录。第一条信号会在触发日写入。" />}
      </Section>

      <Section
        id="next"
        no="06"
        title="如果今天触发：按这四步做完"
        lead="这四步不产生新判定阈值；+3% 是执行上限，不是买入条件。它只是把「规则」章里已经写死的顺序搬到你手边。"
        source="规则来自买点六问与不买清单；这里只给顺序"
        actions={<Cadence kind="frozen" />}
      >
        <NextSteps />
      </Section>

      <Section
        id="register"
        no="07"
        title="反例登记：那条被证伪的规则今天在说什么"
        lead="同一天，另一条规则也在给读数。它十年没有跑赢全程持有，验收只过少数几项 —— 放在这里是为了每天看见它一次，不是让你照它做。"
        source="GET /api/position-art · 完整验收在「证据 · 仓位择时的验收」"
        actions={<Cadence kind="daily" note="不构成指令" />}
        asOf={art?.as_of}
      >
        <RegisterCard art={art} err={artErr} />
      </Section>
    </TabLayout>
  )
}


// ---------------------------------------------------------------- 02 账户

const fmtMoney = (v: number) => Math.round(v).toLocaleString('zh-CN')

export function AccountCard({ account, err }: { account: EtfWatchAccount | null; err: string }) {
  if (!account) {
    return <EmptyBlock text={`先跑 python analysis/etf_watch_account.py 生成账户快照${err ? `（${err}）` : ''}。`} />
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
        <ol className="mt-3 space-y-1.5">
          {account.instructions.map((i: EtfWatchAccount['instructions'][number]) => (
            <li key={i.no} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded border border-border bg-base px-3 py-2 text-[11.5px]">
              <span className={cn('font-mono text-[10px] font-medium',
                i.action === 'BUY' ? 'text-accent' : i.action === 'SELL' ? 'text-danger' : 'text-secondary')}>
                {i.action === 'BUY' ? '买入' : i.action === 'SELL' ? '卖出' : '暂不动'}
              </span>
              <span className="font-medium text-foreground">{i.what}</span>
              <span className="font-mono tabular-nums text-secondary">≈{fmtMoney(i.est_amount)}</span>
              {i.limit_price != null && (
                <span className="font-mono text-[10px] font-medium tabular-nums text-accent">限价 {i.limit_price.toFixed(3)}</span>
              )}
              <span className="text-[10.5px] text-secondary">{i.why}</span>
              <span className="ml-auto shrink-0 text-[10px] text-secondary">{i.when}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="mt-3 rounded border border-border bg-base px-3 py-2 text-[11.5px] text-secondary">
          下一交易日无指令：腿 1 无新信号、无到期；债券已对齐。
        </p>
      )}
      <p className="mt-2 text-[10px] leading-relaxed text-secondary">
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
      <EmptyBlock
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
        <ol className="mt-3 space-y-1.5">
          {leg.orders.map(o => (
            <li
              key={o.no}
              className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 rounded border border-border bg-base px-3 py-2 text-[11.5px]"
            >
              <span className="font-mono text-[10px] font-medium text-accent">
                {o.action === 'BUY' ? '买入' : '卖出'}
              </span>
              <span className="font-medium text-foreground">{o.name}</span>
              <span className="font-mono tabular-nums text-secondary">
                {o.shares.toLocaleString('zh-CN')} 份 × {o.price.toFixed(3)} = ≈{fmtMoney(o.amount)}
              </span>
              <span className="text-[10.5px] text-secondary">佣金 {o.fee.toFixed(2)}</span>
              <span className="text-[10.5px] text-secondary">{o.why}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="mt-3 rounded border border-border bg-base px-3 py-2 text-[11.5px] text-secondary">
          无下单指令：两档已在目标内。
        </p>
      )}
      <div className="mt-3 grid gap-x-6 gap-y-1 text-[10.5px] leading-relaxed text-secondary sm:grid-cols-3">
        <div>
          <b className="text-foreground">R1 国债锚</b>：{leg.meta.rules.R1}
        </div>
        <div>
          <b className="text-foreground">R2 底盘吸收</b>：{leg.meta.rules.R2}
        </div>
        <div>
          <b className="text-foreground">R3 缺钱顺序</b>：{leg.meta.rules.R3}
        </div>
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-secondary">
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
function RegisterCard({ art, err }: { art: PositionArtPayload | null; err: string }) {
  const today = art?.today
  const chosen = art?.chosen
  const bench = art?.benchmark
  const passed = (art?.checks ?? []).filter(c => c.pass).length
  const total = (art?.checks ?? []).length

  if (!today) {
    return (
      <EmptyBlock
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
      <div className="grid grid-cols-2 gap-2.5 md:grid-cols-4">
        <div className="rounded-lg border border-border bg-surface px-3.5 py-3">
          <div className="text-[10.5px] text-secondary">市场状态（t−1 判定）</div>
          <div className="mt-1 text-[19px] font-semibold leading-none text-foreground">
            {today.state === 'down' ? '下行' : today.state === 'up' ? '上行' : '横盘'}
          </div>
          <div className="mt-1.5 text-[10.5px] text-secondary">已持续 {today.state_age} 个交易日</div>
        </div>
        <div className="rounded-lg border border-border bg-surface px-3.5 py-3">
          <div className="text-[10.5px] text-secondary">这条规则会说</div>
          <div className="mt-1 text-[19px] font-semibold leading-none text-foreground">{today.advice}</div>
          <div className="mt-1.5 text-[10.5px] text-secondary">
            自 {today.since} 起，第 {today.days_in_stance} 天
          </div>
        </div>
        <div className="rounded-lg border border-border bg-surface px-3.5 py-3">
          <div className="text-[10.5px] text-secondary">它什么时候改口</div>
          <div className="mt-1 text-[12px] leading-5 text-foreground">{today.trigger}</div>
        </div>
        <div className="rounded-lg border border-border bg-surface px-3.5 py-3">
          <div className="text-[10.5px] text-secondary">60 日涨幅（等权）</div>
          <div
            className={cn(
              'mt-1 font-mono text-[19px] font-semibold leading-none tabular-nums',
              today.ret60_pct == null ? 'text-secondary' : today.ret60_pct > 0 ? 'text-bull' : 'text-bear',
            )}
          >
            {today.ret60_pct == null ? '—' : `${today.ret60_pct > 0 ? '+' : ''}${today.ret60_pct}%`}
          </div>
          <div className="mt-1.5 text-[10.5px] text-secondary">as_of {art?.as_of}</div>
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
export function ExecutionStatus({
  payload, err, onOpen,
}: {
  payload: EtfWatchPayload | null
  err: string
  onOpen: () => void
}) {
  const candidate = payload?.counts.candidate
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group flex w-full shrink-0 flex-col rounded-lg border border-border bg-surface px-4 py-3 text-left transition-colors hover:border-accent/40 sm:w-[252px]"
    >
      <span className="flex items-center justify-between gap-2 text-[10px] text-secondary">
        <span className="flex items-center gap-1.5">
          <span className={cn('h-1.5 w-1.5 rounded-full', candidate != null && candidate > 0 ? 'bg-accent' : 'bg-border')} />
          今日状态
        </span>
        <ArrowRight className="h-3 w-3 opacity-0 transition-opacity group-hover:opacity-100" />
      </span>
      <span className="mt-1 text-[13px] font-medium text-foreground">
        {payload
          ? (candidate && candidate > 0 ? `${candidate} 个四条全过` : '今日无四条全过')
          : err ? '扫描未就绪' : '读取中…'}
      </span>
      <span className="mt-0.5 text-[10px] text-secondary">
        {payload
          ? `${payload.meta.asof} · ${payload.market.state} · V1 已结算 ${payload.ledger.v1.progress ?? '—'}`
          : err ? '收盘后跑 analysis/etf_watch.py' : '正在读取扫描结果'}
      </span>
    </button>
  )
}

// ---------------------------------------------------------------- 01 结论

function TodayHero({ payload, err }: { payload: EtfWatchPayload | null; err: string }) {
  if (!payload) return <OfflineCard err={err} />

  const { candidate, in_band, signal, industries, clusters_today } = payload.counts
  const v1 = payload.ledger.v1
  const has = candidate > 0
  // 四条全过 ≠ 新信号：账户只买「段首」（今天首次进入区间），其余是延续中的信号。
  const newSeg = payload.ledger.events.filter(e => e.date === payload.meta.asof).length

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-surface">
      <div className="flex flex-col gap-5 p-5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-[10px] font-medium tracking-wide text-secondary">
            <span className={cn('h-1.5 w-1.5 rounded-full', has ? 'bg-accent' : 'bg-border')} />
            收盘后判定 · 纸面执行
          </div>
          <div className="mt-2 flex items-baseline gap-2">
            <span className={cn('font-mono text-[36px] font-semibold leading-none tabular-nums tracking-tight', has ? 'text-accent' : 'text-foreground')}>
              {candidate}
            </span>
            <span className="text-[13px] text-secondary">个四条全过</span>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1.5 text-[11.5px] leading-relaxed text-secondary">
            <span>{in_band}/{industries} 个行业在区间内 · {signal} 个高波信号 · 今日 {clusters_today} 簇</span>
            {newSeg > 0 && <Tag tone="ok">可执行 {newSeg} 笔（段首）</Tag>}
            {!has && <span>—— 区间内不等于可下注；形态或容量不过就不做。</span>}
          </div>
        </div>

        <dl className="grid shrink-0 grid-cols-2 gap-x-5 gap-y-2.5 text-[10px] sm:min-w-[224px]">
          <Meta k="市场状态" v={payload.market.state} />
          <Meta k="V1 已结算" v={v1.progress ?? '—'} d={`累计 ${v1.signal_clusters ?? 0} 簇 / ${v1.signal_events ?? 0} 个行业信号`} />
          <Meta k="数据日" v={payload.meta.asof} />
          <Meta k="生成于" v={payload.meta.generated_at ? payload.meta.generated_at.slice(5, 16) : '—'} />
        </dl>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border bg-elevated/30 px-5 py-2.5">
        <span className="text-[10px] text-secondary">
          判定口径只有一份：<span className="font-mono">analysis/etf_watch.py</span>
        </span>
        <div className="flex flex-wrap items-center gap-3">
          <CrossLink to="tools" anchor="replay-day">历史回放与信号追踪</CrossLink>
          <CrossLink to="rules">下单前读规则</CrossLink>
          <CrossLink to="today" anchor="register">反例：状态择时</CrossLink>
        </div>
      </div>
    </div>
  )
}

function Meta({ k, v, d }: { k: string; v: string; d?: string }) {
  return (
    <div>
      <dt className="text-secondary">{k}</dt>
      <dd className="mt-0.5 font-mono text-[11px] font-medium tabular-nums text-foreground">{v}</dd>
      {d && <dd className="mt-0.5 text-[9.5px] leading-relaxed text-secondary/80">{d}</dd>}
    </div>
  )
}

function OfflineCard({ err }: { err: string }) {
  return (
    <div className="rounded-xl border border-warning/40 bg-warning/[0.06] p-5">
      <div className="flex items-center gap-2 text-[12.5px] font-medium text-foreground">
        <CircleDot className="h-3.5 w-3.5 text-warning" />
        扫描结果未就绪
      </div>
      <p className="mt-1.5 text-[11.5px] leading-relaxed text-secondary">
        {err ? `还没有盘后扫描结果（${err}）。` : '正在读取扫描结果…'}
      </p>
      {err && (
        <>
          <pre className="mt-2 inline-block rounded border border-border bg-base px-2.5 py-1.5 font-mono text-[11px] text-foreground">
            python analysis/etf_watch.py
          </pre>
          <p className="mt-2 text-[11px] leading-relaxed text-secondary">
            每天记录一次，攒满 3 个事件簇才做 V1 判定 —— 不要回测，回测的 N 已经用到头了。
          </p>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- 03 账本

function Ledger({ payload }: { payload: EtfWatchPayload }) {
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
          <span className={cn('font-mono tabular-nums', s ? (s.rel20 >= 0 ? 'text-accent' : 'text-danger') : 'text-secondary/70')}>
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
      <div className="text-[10.5px] text-secondary">
        事件账本（{events.length} 条 · 只增不改 · 同月多行业记为 1 簇）
      </div>
      {events.length === 0 ? (
        <EmptyBlock text="账本还是空的 —— 第一条信号会在触发日写入。" />
      ) : (
        <DataTable cols={evCols} rows={events} rowKey={e => e.id} dense />
      )}

      <div className="mt-4 grid gap-x-6 gap-y-2 text-[11px] leading-relaxed text-secondary sm:grid-cols-2">
        <div>
          <b className="text-foreground">① 数簇不数行</b>：同一天多个行业一起跌破，是一波行情里的多个标签，
          不是多次机会。研究段的独立月数远少于事件行数，正是这个坑；按行计数会把 1 次显示成「N/N 达成」。
        </div>
        <div>
          <b className="text-foreground">② p10 换成 MAE</b>：{v1.left_tail_mapping || '样本太少估不出 10% 分位，V1 改用每簇持有期最差收盘浮亏。'}
        </div>
        <div className="sm:col-span-2">
          <b className="text-foreground">阈值</b>：dd ∈ ({pct(th.deep)}, {pct(th.low)}]、{payload.meta.vol_convention} &gt;{' '}
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

function NextSteps() {
  return (
    <ol className="grid gap-2.5 sm:grid-cols-2">
      {NEXT_STEPS.map(s => (
        <li key={s.n} className="rounded-lg border border-border bg-surface p-3.5">
          <div className="flex items-baseline gap-2">
            <span className="font-mono text-[10px] text-muted">{s.n}</span>
            <span className="text-[12.5px] font-medium text-foreground">{s.title}</span>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-secondary">{s.desc}</p>
          <CrossLink to={s.to} anchor={s.anchor} className="mt-2">
            对应的规则
          </CrossLink>
        </li>
      ))}
    </ol>
  )
}

function EmptyBlock({ text }: { text: string }) {
  return (
    <div className="mt-3 rounded-lg border border-dashed border-border bg-surface/40 px-4 py-5 text-center text-[11px] text-secondary">
      {text}
    </div>
  )
}
