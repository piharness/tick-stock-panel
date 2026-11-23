/**
 * 市场常识 —— 把「行业内确定性」做成一条可交互的因果链，而不是一张清单。
 *
 * 定位：散户最大的短板不只是消息慢一拍，而是缺乏行业内的常识、潜规则、法规、
 * 博弈常识、公告套路。这些未必直接改变盘面，但在「谎言 / 欺骗 / 误导」横行的
 * 市场里提供了相对可靠的确定性，是无论是否操盘都该有的基本功。
 *
 * 本页不回答「明天涨不涨」，而是把「可推演的结构」做成六块内容：
 *   ① 认知体系    五层因果链（规则 → 信息 → 动机 → 结构 → 结算 → 回到规则），
 *                 48 条常识挂在链上，每条都带「常见误解 / 实时锚点 / 关联路径」，
 *                 其中 9 条再挂一个「个股判据」（输入代码即出结论）
 *   ② 退市生死全景 全市场「价格×市值」风险地图 + 连续 20 日倒计时 + *ST华幸实时案例
 *   ③ 信息时间差   一条利好从内幕到散户的层级瀑布 + 龙虎榜实证
 *   ④ 博弈动机推演 拖动价格位置，看「谁在这个价位有动机买/卖/做局」
 *   ⑤ 踩踏传导链   量化趋同 → 拥挤 → 流动性枯竭 → 火星 → 踩踏的因果链
 *   ⑥ 规则速查     退市流程时间轴 + 全条目检索（按五层筛选，与体系图同源）
 *
 * 「实时」是这一版的重点：页面顶部的「今日市场事实」用当天全市场横截面算出 9 个
 * 会变的数字，点任意一个数字就能跳到它对应的那条常识（见 common-sense/anchors.ts）。
 * 没有实时数据支撑的条目会如实标注，绝不用静态数字冒充实测。
 *
 * 「个股」是再下一层：9 条行业说法（吸筹 / 对敲 / 震仓 / 飞刀 / 派发 / 控盘 / 壳 / 减持 / 分歧）
 * 由后端 table_game._stock_rules 落到任意一只票上，输入代码即出结论（见 common-sense/probes.ts）。
 * 两层分工明确：市场锚点回答「今天市场怎样」，个股判据回答「我看的这一只怎样」——
 * 前一层的数字再准，也替不了后一层；这也是上一版最大的缺口。
 *
 * 与操盘档案（/case-lab）的分工：操盘档案用「本地痕迹」对账「叙事」，
 * 本页沉淀「不依赖盘面变化」的确定性结构。二者同属「学习」动线。
 *
 * 归属：核心页面，经 lib/navOrder 登记路径 /common-sense（侧栏「市场常识」），
 * 只读 import 核心能力（api/QK/组件/格式化），不承载扩展域语义。
 */
import { useCallback, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import {
  Activity, ArrowDownToLine, Brain, Flame, Hourglass, Info, Network, Search, ShieldAlert,
} from 'lucide-react'
import { api, type MarketSnapshotRow, type TableGameBoard } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { RULE_STATE } from '@/lib/ruleState'
import { PageHeader } from '@/components/PageHeader'
import { DelistingGuardPanel } from '@/components/stock-analysis/DelistingGuardPanel'
import { DelistingLandscape, riskCount } from './common-sense/DelistingLandscape'
import { DelistingCountdown } from './common-sense/DelistingCountdown'
import { DragonTigerDirection } from './common-sense/DragonTigerDirection'
import { ConcentrationPanel } from './common-sense/ConcentrationPanel'
import { InfoCascade } from './common-sense/InfoCascade'
import { GameTheoryLab } from './common-sense/GameTheoryLab'
import { CrowdingCascade } from './common-sense/CrowdingCascade'
import { DelistingTimeline } from './common-sense/DelistingTimeline'
import { TodayFacts } from './common-sense/TodayFacts'
import { StockProbe } from './common-sense/StockProbe'
import { KnowledgeSystem, ItemDetail } from './common-sense/KnowledgeSystem'
import { computeAnchor, type AnchorId, type AnchorResult } from './common-sense/anchors'
import { PROBES, probeLookup } from './common-sense/probes'
import {
  CATEGORIES, CERTAINTY_CHIP, CERTAINTY_META, KNOWLEDGE, LAYERS,
  type LayerId,
} from './common-sense/knowledge'

type Tab = 'system' | 'landscape' | 'cascade' | 'game' | 'crowding' | 'rules'

const TABS: { id: Tab; label: string; icon: typeof Activity; hint: string }[] = [
  { id: 'system', label: '认知体系', icon: Network, hint: '五层因果链 · 市场实测 + 个股判据' },
  { id: 'landscape', label: '退市生死全景', icon: ShieldAlert, hint: '全市场谁在逼近死亡线' },
  { id: 'cascade', label: '信息时间差', icon: Info, hint: '利好到你手里还剩多少' },
  { id: 'game', label: '博弈动机推演', icon: Brain, hint: '谁在这个价位犯错' },
  { id: 'crowding', label: '踩踏传导链', icon: Flame, hint: '趋同如何演变成踩踏' },
  { id: 'rules', label: '规则速查', icon: ArrowDownToLine, hint: '退市流程 + 全条目检索' },
]

const LIVE_CASE_SYMBOL = '600340.SH'
const LIVE_CASE_NAME = '*ST华幸'

export function CommonSense() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const tab: Tab = TABS.some(t => t.id === raw) ? (raw as Tab) : 'system'
  const [focus, setFocus] = useState<string | null>(null)

  // 个股判据的标的。三种状态必须分开：
  //   ?symbol 缺失   → 默认页面贯穿的实时案例（打开就有数，而不是一张空表）
  //   ?symbol=       → 显式清空（只看市场级事实）
  //   ?symbol=600519.SH → 指定标的
  // 用 URL 承载而不是 useState：换只票的结论是可以发给别人的，刷新也不该丢。
  const rawSymbol = params.get('symbol')
  const symbol = rawSymbol === null ? LIVE_CASE_SYMBOL : (rawSymbol.trim() || null)

  const setSymbol = useCallback((s: string | null) => {
    const next = new URLSearchParams(params)
    if (s) next.set('symbol', s)
    else next.set('symbol', '')
    setParams(next, { replace: true })
  }, [params, setParams])

  const setTab = useCallback((id: Tab) => {
    const next = new URLSearchParams(params)
    next.set('tab', id)
    setParams(next, { replace: true })
  }, [params, setParams])

  // 全市场快照（退市生死全景 + 今日市场事实的共同数据源）
  const snapshot = useQuery({
    queryKey: QK.marketSnapshot,
    queryFn: () => api.marketSnapshot(),
    staleTime: 120_000,
    retry: false,
  })

  // 退市判死案例：*ST华幸 实时红线
  const guardQ = useQuery({
    queryKey: QK.stockRiskGuard(LIVE_CASE_SYMBOL),
    queryFn: () => api.stockAnalysisRiskGuard(LIVE_CASE_SYMBOL),
    staleTime: 60_000,
    retry: false,
  })

  // 全市场退市「连续 20 日」倒计时
  const countdownQ = useQuery({
    queryKey: QK.delistingCountdown,
    queryFn: () => api.stockAnalysisDelistingCountdown(),
    staleTime: 120_000,
    retry: false,
  })

  // 龙虎榜方向一致性（信息瀑布 + 博弈推演的实证佐证）
  const dragonStatsQ = useQuery({
    queryKey: QK.dragonTigerStats,
    queryFn: () => api.dragonTigerStats(),
    staleTime: 600_000,
    retry: false,
  })

  // 龙虎榜「机构方向 → 后续超额收益」判决（谁对谁错）
  const dragonVerdictQ = useQuery({
    queryKey: QK.dragonTigerVerdict,
    queryFn: () => api.dragonTigerVerdict(),
    staleTime: 600_000,
    retry: false,
  })

  // 换手率拥挤度 → 后续超额收益判决（踩踏链）
  const crowdingVerdictQ = useQuery({
    queryKey: QK.crowdingVerdict,
    queryFn: () => api.stockAnalysisCrowdingVerdict(),
    staleTime: 600_000,
    retry: false,
  })

  // 个股判据：九条「行业说法」落到这一只票上（后端 table_game._stock_rules 的实现）
  // queryKey 与「博弈链路」页完全相同 —— 同一只票只请求一次，两个页面共享同一份缓存。
  // 数据是日线口径，5 分钟内不会变；refetchOnWindowFocus 关掉，避免切窗口就把九条重算一遍。
  const boardQ = useQuery({
    queryKey: ['battle', 'board', symbol],
    queryFn: () => api.tableGameBoard(symbol as string),
    enabled: !!symbol,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
    retry: false,
  })

  const rows: MarketSnapshotRow[] = snapshot.data?.rows ?? []
  const asOf = snapshot.data?.as_of ?? null
  const risk = useMemo(() => riskCount(rows), [rows])

  // 锚点只算一次：9 个锚点都要扫全市场，不能在每个条目上重复算。
  const anchorMemo = useMemo(() => {
    const cache = new Map<AnchorId, AnchorResult | null>()
    return (id: AnchorId): AnchorResult | null => {
      if (!cache.has(id)) cache.set(id, computeAnchor(id, { rows, asOf }))
      return cache.get(id) ?? null
    }
  }, [rows, asOf])

  // 从「今日事实」或「关联路径」跳到体系图里的某一条
  const gotoItem = useCallback((id: string | null) => {
    setTab('system')
    setFocus(id)
  }, [setTab])

  // 点数字 → 找到引用该锚点的第一条常识（锚点与条目是索引关系，不是复制关系）
  const pickAnchor = useCallback((id: AnchorId) => {
    const hit = KNOWLEDGE.find(k => k.anchor === id)
    if (hit) gotoItem(hit.id)
  }, [gotoItem])

  return (
    <div className="min-h-full bg-base pb-8">
      <PageHeader
        title="市场常识"
        subtitle="把行业内常识、潜规则、法规与博弈逻辑做成可交互的因果链——不预测涨跌，只让「确定性」看得见、可核对。"
        right={
          <span className="hidden text-[10px] text-muted sm:inline">
            {KNOWLEDGE.length} 条常识 · {LAYERS.length} 层因果 · 今日实测锚点 · {PROBES.length} 条个股判据
          </span>
        }
      />

      <div className="flex flex-wrap items-center gap-1 border-b border-border px-5 py-2">
        {TABS.map(t => {
          const Icon = t.icon
          const on = tab === t.id
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              title={t.hint}
              className={cn(
                'flex items-center gap-1.5 rounded-btn border px-2.5 py-1 text-[11px] transition-colors',
                on
                  ? 'border-accent/40 bg-accent/10 text-accent'
                  : 'border-transparent text-secondary hover:bg-elevated hover:text-foreground',
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {t.label}
            </button>
          )
        })}
        <span className="ml-2 hidden text-[10px] text-muted lg:inline">
          {TABS.find(t => t.id === tab)?.hint}
        </span>
      </div>

      <div className="mx-auto max-w-[1180px] space-y-4 px-4 py-4 sm:px-6">
        {/* 今日市场事实 + 个股判据：市场级数字在上，个股级结论在下 */}
        {(tab === 'system' || tab === 'rules') && (
          <>
            <TodayFacts rows={rows} asOf={asOf} onPick={pickAnchor} />
            <StockProbe
              symbol={symbol}
              onSymbol={setSymbol}
              board={boardQ.data}
              loading={boardQ.isLoading}
              error={boardQ.isError}
              onJump={gotoItem}
            />
          </>
        )}

        {/* ===== ① 认知体系 ===== */}
        {tab === 'system' && (
          <KnowledgeSystem
            rows={rows}
            asOf={asOf}
            focusId={focus}
            onFocus={setFocus}
            board={boardQ.data}
            symbol={symbol}
          />
        )}

        {/* ===== ② 退市生死全景 ===== */}
        {tab === 'landscape' && (
          <section className="rounded-card border border-border bg-surface/80 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <ShieldAlert className="h-4 w-4 text-danger" />
                <h2 className="text-sm font-medium text-foreground">退市生死全景</h2>
              </div>
              <div className="flex items-center gap-3 text-[10px] font-mono text-muted">
                <span>
                  全市场 <b className="text-foreground">{rows.length.toLocaleString()}</b> 只
                </span>
                <span>
                  ST <b className="text-red-600">{risk.st}</b>
                </span>
                <span>
                  破面值 <b className="text-red-600">{risk.belowPar}</b>
                </span>
                <span>
                  破市值线 <b className="text-red-600">{risk.belowMcap}</b>
                </span>
              </div>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
              散户对退市股心存侥幸，是因为看不到「全市场有多少只票正压在死亡线上」。
              这张图把每一只票按「价格 × 市值」散布开，两条虚线就是硬红线——
              但「今天破线」和「连续 20 日破线」是两回事：规则要的是连续 20 个交易日，
              下面这张倒计时把「破线」升级成「离死线还剩几天」。
            </p>
            <div className="mt-2">
              <DelistingLandscape rows={rows} />
            </div>

            {/* 连续 20 日倒计时：从「破线」到「规则距离计数」 */}
            <div className="mt-4 border-t border-border/50 pt-3">
              <div className="flex items-center gap-2">
                <Hourglass className="h-4 w-4 text-warning" />
                <span className="text-[12px] font-medium text-foreground">连续 20 日倒计时</span>
                <span className="text-[10px] text-muted">规则要的是「连续」，不是「某天」</span>
              </div>
              <div className="mt-2">
                <DelistingCountdown data={countdownQ.data} loading={countdownQ.isLoading} error={countdownQ.isError} />
              </div>
            </div>

            {/* *ST华幸 实时案例 */}
            <div className="mt-4 border-t border-border/50 pt-3">
              <div className="flex items-center gap-2">
                <span className="text-[12px] font-medium text-foreground">实时案例：{LIVE_CASE_NAME} 为什么「早被判死刑」</span>
                <span className="text-[10px] text-muted">退市不是概率，是可逐日计数的规则距离</span>
              </div>
              <div className="mt-2">
                <DelistingGuardPanel data={guardQ.data} loading={guardQ.isLoading} error={guardQ.isError} />
              </div>
            </div>
          </section>
        )}

        {/* ===== ③ 信息时间差 ===== */}
        {tab === 'cascade' && (
          <section className="rounded-card border border-border bg-surface/80 p-4">
            <div className="flex items-center gap-2">
              <Info className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-medium text-foreground">信息时间差</h2>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
              「你看到消息时它已被定价」—— 这不是一句抱怨，而是信息层级的必然结果。
              一条利好从内幕到散户要经过至少五层，每往下一层，价格已反映的比例就高一分，
              确定性就低一分。看懂这张图，你就不会再「看到利好就追」。
            </p>
            <div className="mt-3">
              <InfoCascade />
            </div>

            {/* 实证佐证：龙虎榜方向一致性 */}
            <div className="mt-4 border-t border-border/50 pt-3">
              <div className="flex items-center gap-2">
                <span className="text-[12px] font-medium text-foreground">实证：谁先定价、方向多强</span>
                <span className="text-[10px] text-muted">龙虎榜 · 机构 vs 全榜 / 游资 同向率</span>
              </div>
              <div className="mt-2">
                <DragonTigerDirection data={dragonStatsQ.data} verdict={dragonVerdictQ.data} loading={dragonStatsQ.isLoading} error={dragonStatsQ.isError} />
              </div>
            </div>
          </section>
        )}

        {/* ===== ④ 博弈动机推演 ===== */}
        {tab === 'game' && (
          <section className="rounded-card border border-border bg-surface/80 p-4">
            <div className="flex items-center gap-2">
              <Brain className="h-4 w-4 text-accent" />
              <h2 className="text-sm font-medium text-foreground">博弈动机推演</h2>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
              博弈里每个角色都不会故意犯错。所以「谁会心甘情愿在这个价位卖给我」这个反问，
              能批量排除掉经不起推敲的叙事。下面三个反问是思维框架——它们不预测股票，
              只帮你在下单前把「接盘」这件事想清楚。
            </p>
            <div className="mt-3">
              <GameTheoryLab />
            </div>

            {/* 实证佐证：谁在买 —— 龙虎榜机构 vs 游资方向 */}
            <div className="mt-4 border-t border-border/50 pt-3">
              <div className="flex items-center gap-2">
                <span className="text-[12px] font-medium text-foreground">实证：聪明钱与谁反向</span>
                <span className="text-[10px] text-muted">机构 vs 游资同向率仅约四成，才是「谁在犯错」的真线索</span>
              </div>
              <div className="mt-2">
                <DragonTigerDirection data={dragonStatsQ.data} verdict={dragonVerdictQ.data} loading={dragonStatsQ.isLoading} error={dragonStatsQ.isError} />
              </div>
            </div>
          </section>
        )}

        {/* ===== ⑤ 踩踏传导链 ===== */}
        {tab === 'crowding' && (
          <section className="rounded-card border border-border bg-surface/80 p-4">
            <div className="flex items-center gap-2">
              <Flame className="h-4 w-4 text-warning" />
              <h2 className="text-sm font-medium text-foreground">踩踏传导链</h2>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
              近些年量化越来越趋同，因子拥挤造成流动性逐渐枯竭——平时看不出，一点火星就会引发踩踏。
              这条链的关键在前两步是「隐性」的：因子曲线越平滑，越说明大家挤在一起、互为对手盘。
              平滑不是安全，是脆弱的前奏。
            </p>
            <div className="mt-3">
              <CrowdingCascade verdict={crowdingVerdictQ.data} />
            </div>

            {/* 实证佐证：资金集中度（拥挤的横截面代理） */}
            <div className="mt-4 border-t border-border/50 pt-3">
              <div className="flex items-center gap-2">
                <span className="text-[12px] font-medium text-foreground">实证：资金集中度</span>
                <span className="text-[10px] text-muted">前 100 只票吃掉的成交额占比 —— 拥挤的横截面代理</span>
              </div>
              <div className="mt-2">
                <ConcentrationPanel rows={rows} />
              </div>
            </div>
          </section>
        )}

        {/* ===== ⑥ 规则速查 ===== */}
        {tab === 'rules' && (
          <RulesTab anchorOf={anchorMemo} onJump={gotoItem} board={boardQ.data} symbol={symbol} />
        )}

        {/* 边界声明 */}
        <footer className="rounded-card border border-border bg-surface px-4 py-3 text-[10px] leading-relaxed text-muted">
          <b className="text-secondary">边界声明：</b>
          本页沉淀的是「公开可知的确定性」——规则条款、市场结构、博弈逻辑——不构成投资建议，
          也不预测任何个股涨跌。硬规则（退市红线、涨跌幅、交割）可以逐日核对，但
          <span className="text-secondary">规则本身会过时</span>（例如 ST 涨跌幅已于 2026-07 从 5% 放宽到 10%），
          每条已标注生效时间与依据，请以交易所最新公告为准。「退市生死全景」是横截面快照，
          只表示「已进入倒数」，不等于「必然退市」；博弈推演、信息瀑布、踩踏链是结构化的心智模型，
          数值为示意刻度，方向才是确定的结论。真正的确定性不来自「提前知道消息」（那往往是内幕交易），
          而来自「公开规则会如何执行」这类可验证的事实。
        </footer>
      </div>
    </div>
  )
}

/** 规则速查：退市流程时间轴 + 全条目检索（按五层筛选，与体系图同源同版式） */
function RulesTab({
  anchorOf, onJump, board, symbol,
}: {
  anchorOf: (id: AnchorId) => AnchorResult | null
  onJump: (id: string | null) => void
  board: TableGameBoard | undefined | null
  symbol: string | null
}) {
  const [layer, setLayer] = useState<LayerId | 'all'>('all')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<string | null>(null)

  // 关联跳转只允许跳到真实存在的条目：悬空引用在 UI 上表现为「点了没反应」。
  const ids = useMemo(() => new Set(KNOWLEDGE.map(k => k.id)), [])

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase()
    return KNOWLEDGE.filter(k => {
      if (layer !== 'all' && k.layer !== layer) return false
      if (!kw) return true
      return (
        k.title.toLowerCase().includes(kw)
        || k.summary.toLowerCase().includes(kw)
        || k.body.toLowerCase().includes(kw)
        || k.misconception.toLowerCase().includes(kw)
      )
    })
  }, [layer, q])

  return (
    <div className="space-y-4">
      {/* 退市流程时间轴 */}
      <section className="rounded-card border border-border bg-surface/80 p-4">
        <div className="flex items-center gap-2">
          <ArrowDownToLine className="h-4 w-4 text-accent" />
          <h2 className="text-sm font-medium text-foreground">退市流程：ST ≠ *ST ≠ 退市</h2>
        </div>
        <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
          散户最容易把「ST」「*ST」「退市」混成一团。其实它们是不同阶段、不同触发、不同结局的递进链条。
          看这条时间轴，尤其注意「可回头」和「不可逆」的分界。
        </p>
        <div className="mt-3">
          <DelistingTimeline />
        </div>
      </section>

      {/* 条目检索 */}
      <section className="rounded-card border border-border bg-surface/80 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-medium text-foreground">全条目检索</h2>
          <span className="text-[10px] text-muted">
            共 {KNOWLEDGE.length} 条 · 按五层筛选 · 与「认知体系」同一份内容
          </span>
          <div className="ml-auto flex items-center gap-1 rounded border border-border bg-base/40 px-1.5 py-1">
            <Search className="h-3 w-3 text-muted" />
            <input
              value={q}
              onChange={e => setQ(e.target.value)}
              placeholder="搜标题 / 正文 / 误解"
              className="w-40 bg-transparent text-[10px] text-foreground outline-none placeholder:text-muted/50"
            />
            {q && (
              <button onClick={() => setQ('')} className="text-[9px] text-muted hover:text-foreground">清除</button>
            )}
          </div>
        </div>

        <div className="mt-2 flex flex-wrap items-center gap-1">
          <button
            onClick={() => setLayer('all')}
            className={cn(
              'rounded-btn border px-2 py-0.5 text-[10px] transition-colors',
              layer === 'all' ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-elevated text-secondary hover:text-foreground',
            )}
          >
            全部 {KNOWLEDGE.length}
          </button>
          {CATEGORIES.map(c => {
            const n = KNOWLEDGE.filter(k => k.layer === c.id).length
            return (
              <button
                key={c.id}
                onClick={() => setLayer(c.id)}
                title={c.hint}
                className={cn(
                  'rounded-btn border px-2 py-0.5 text-[10px] transition-colors',
                  layer === c.id ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border bg-elevated text-secondary hover:text-foreground',
                )}
              >
                {c.label} {n}
              </button>
            )
          })}
        </div>

        {layer !== 'all' && (
          <p className="mt-1.5 text-[10px] leading-relaxed text-muted">
            {LAYERS.find(l => l.id === layer)?.role}
          </p>
        )}

        <div className="mt-3 space-y-2">
          {filtered.length === 0 && (
            <p className="rounded border border-border/50 bg-base/30 px-3 py-2 text-[11px] text-muted">
              没有匹配的条目。换个词试试，或点「全部」。
            </p>
          )}
          {filtered.map(k => {
            const isOpen = open === k.id
            const a = k.anchor ? anchorOf(k.anchor) : null
            const probe = k.probe && symbol ? probeLookup(board, k.probe) : null
            return (
              <article key={k.id} id={`kl-${k.id}`} className="overflow-hidden rounded border border-border/60 bg-surface/40">
                <button
                  onClick={() => setOpen(isOpen ? null : k.id)}
                  className="flex w-full items-start gap-2 px-3 py-2 text-left transition-colors hover:bg-elevated/30"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-[12px] font-medium text-foreground">{k.title}</span>
                      <span className={cn('rounded px-1 py-0.5 text-[9px] font-medium', CERTAINTY_CHIP[k.certainty])}>
                        {CERTAINTY_META[k.certainty].label}
                      </span>
                      {a && (
                        <span className="rounded bg-elevated px-1 py-0.5 font-mono text-[9px] text-secondary">
                          {a.label} {a.value}{a.unit ? ` ${a.unit}` : ''}
                        </span>
                      )}
                      {probe && (
                        probe.kind === 'rule' ? (
                          <span className={cn('rounded px-1 py-0.5 text-[9px]', RULE_STATE[probe.rule.state].cls)}>
                            个股 {RULE_STATE[probe.rule.state].label}
                          </span>
                        ) : (
                          <span className="rounded bg-elevated px-1 py-0.5 text-[9px] text-muted">个股 无法判定</span>
                        )
                      )}
                    </div>
                    <p className="mt-0.5 text-[10px] leading-relaxed text-secondary">{k.summary}</p>
                  </div>
                  <span className="mt-0.5 shrink-0 text-[10px] text-muted/50">{isOpen ? '收起' : '展开'}</span>
                </button>
                {isOpen && (
                  <div className="border-t border-border/40">
                    <ItemDetail
                      item={k}
                      anchor={a}
                      probe={probe}
                      symbol={symbol}
                      onJump={onJump}
                      exists={id => ids.has(id)}
                      hideHeader
                    />
                  </div>
                )}
              </article>
            )
          })}
        </div>
      </section>
    </div>
  )
}
