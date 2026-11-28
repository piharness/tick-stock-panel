/**
 * 叙事账本（/narrative）的冻结部分 —— 理论、读数、判据的唯一来源。
 *
 * 这一页的定位与 /chip 同构：不产出买点，产出一张表 + 一份排除清单。
 *   - 卷一 换表：涨幅榜是资产端（已定价），叙事要读的是「谁还欠一个更高的价」；
 *   - 卷二 读数：新跑的剂量反应与配对安慰剂（analysis/_diag_narrative_heat.py）；
 *   - 卷三 今日账本：实时读数，全部来自 /api/narrative/ledger（本文件不含任何当日数字）；
 *   - 卷四 判据：排序可读、水平不可读；容量；杀死条件。
 *
 * 口径（改数之前先读）：
 *   - 概念日收益 = 成员等权（当日有行情的成员，≥5 只）；fwd = Π(1+r)−1。
 *   - 超额基准 = 同日全体概念的中位；t 按日聚簇 NW(lag=5)。
 *   - 概念成分是当前快照（ext_gn_ths，本地 2026-07 起留存）⇒ 归属漂移，只做 2025-05 起。
 *   - 量级参照：本仓单边成本 20bp（position_art 的 cost_one_way_bp）。
 *
 * 改数唯一路径：重跑 analysis/_diag_narrative_heat.py → 改本文件 → 更新 META.asOf。
 */

export type Verdict = 'ok' | 'warn' | 'danger' | 'muted'

export const META = {
  asOf: '2026-11-28',
  sample: '389 个概念 · 371 个交易日 · 16,928 个解禁事件',
  universe: '叙事 2025-05-08 ~ 2026-11-13；解禁 2019-01 ~ 2026-06',
  convention: '概念等权 · 基准=同日全概念中位 / 同日全市场等权 · t 按日聚簇 NW(5) · 单边成本 20bp',
  source: 'analysis/_diag_narrative_heat.py + _diag_narrative_events.py → data/.scratch/*.json',
} as const

export const VERDICT = {
  tag: '三条腿 · 一条可用的排除',
  tone: 'warn' as Verdict,
  line: '叙事是收据，解禁是账单——但这张账单也在到期前就付完了。',
  body:
    '把 /chip 的定律搬到大盘：一笔约束要产生漂移，需要「未结算的余额」与「结算周期 > 0」。' +
    '叙事腿的结算周期是 0（涨停在收盘撮合完），剂量跨度只有 −17bp，小于成本；' +
    '事件腿终于有了余额与日期：解禁占流通比 ≥10% 的股票，解禁前 20 日比同日等权市场低 ' +
    '276bp（t −12.0），且按比例单调（−37 → −94 → −172 → −276）；解禁后 20 日反而 +51bp。' +
    '抛压是提前支付的，到期日不是卖点，是结算点。这条只够做排除：解禁前 20 日不买，不做空。',
  action: '当前可执行动作：1 条排除',
  actionValue: '1',
  actionNote: '未来 20 日解禁占流通比 ≥10% 的票不进候选池；其余动作仍为 0。',
} as const

export const SUMMARY = [
  {
    label: '三条腿',
    value: '一条可用',
    verdict: 'warn' as Verdict,
    note: '解禁前负漂移可做排除；叙事腿与日结算腿仍然是零。活口仍在 /chip 的质押贴线。',
  },
  {
    label: '解禁前 20 日',
    value: '−276bp',
    verdict: 'warn' as Verdict,
    note: '解禁占流通比 ≥10% 组，相对同日等权市场，t −12.0；四档单调。',
  },
  {
    label: '解禁后 20 日',
    value: '+51bp',
    verdict: 'muted' as Verdict,
    note: '结算完成后的回补，t 2.08，幅度刚过双边成本 —— 不构成买点。',
  },
  {
    label: '证据等级',
    value: 'B · 单市场',
    verdict: 'warn' as Verdict,
    note: '16,928 个事件 / 2019–2026；安慰剂 + 动量控制后仍有 −262bp；但只有一个市场、一个制度周期。',
  },
]

export interface LawRow {
  cadence: string
  who: string
  when: string
  balance: string
  read: string
}

/** 一条定律：漂移 ∝ 未付余额 × 1/结算周期 × 处置方向。周期 → 0，漂移被提前支付。 */
export const LAW: LawRow[] = [
  {
    cadence: '逐日盯市',
    who: 'ETF 申赎套利、两融、做市商',
    when: '每天收盘',
    balance: '无——gap 当天结清',
    read: '涨停溢价 117% 在隔夜；两融 ρ_D −0.442（hotmoney-quant-game / chip-ap H9）',
  },
  {
    cadence: '事件盯市',
    who: '质押、解禁、定增、大宗、指数调样',
    when: '到处置日',
    balance: '有——按账龄摊销',
    read: '质押贴线 +291bp；解禁前 −276bp / 后 +51bp；大宗符号不迁移；调样 −10.5bp',
  },
  {
    cadence: '从不盯市',
    who: '纯题材、想象空间、故事',
    when: '永不',
    balance: '没有对手方——不是账单',
    read: '技术状态族 68 检验：4 个存活且全是控制变量（state-family-screen）',
  },
]

export interface LegRow {
  leg: string
  object: string
  tests: string
  read: string
  use: string
}

/** 三条腿：把「叙事」拆成可以分别证伪的三类对象，而不是一个笼统的热度。 */
export const LEGS: LegRow[] = [
  {
    leg: '叙事腿',
    object: '概念、题材、涨停梯队',
    tests: '技术状态 68 / surge-all / 席位接力 / 妖股密度 / 本页剂量反应',
    read: '4/68 存活且都是控制变量；本页 −17bp，|t| < 2',
    use: '排除。不要用形态、热度、连板高度选方向。',
  },
  {
    leg: '日结算腿',
    object: '宽基 ETF、两融、涨停封单',
    tests: '打板溢价隔夜/日内分解、两融=换手代理',
    read: '溢价全在隔夜（gap 占 117%）；两融与久期 ρ −0.442',
    use: '只读波动率与拥挤度，不读方向。',
  },
  {
    leg: '事件腿',
    object: '质押、大宗、调样、退市',
    tests: 'H11 贴线、解禁 16,928 件、大宗迁移 225k 条、调样 1,471 事件、退市清仓',
    read: '解禁前 −276bp / 后 +51bp（本次新测）；贴线 +291bp；大宗符号翻转；调样 −10.5bp；退市负 EV',
    use: '只做排除：解禁前 20 日不买。贴线候选归 /chip，不在这页重复。',
  },
]

export interface DoseRow {
  bucket: string
  n: number
  x5: number
  x5t: number
  x20: number
  x20t: number
  vr: number
  y2025: number | null
  y2026: number | null
}

/**
 * 剂量反应：概念当日涨停家数 → 未来 20 日超额。
 *
 * 读法：绝对水平不可比（基准是「同日全概念中位」，而概念截面右偏，均值天然高于中位）。
 * 唯一可读的是**档与档之间的差**：1 档 → ≥10 档 是 −17.3bp；
 * 2026 分段单调（36.6 → 11.3），2025 不单调（40.0 → 28.2，中间 15.7）。
 */
export const DOSE: DoseRow[] = [
  { bucket: '0 个', n: 55128, x5: 9.0, x5t: 2.60, x20: 35.9, x20t: 4.31, vr: 1.039, y2025: 30.6, y2026: 35.1 },
  { bucket: '1 个', n: 31483, x5: 14.0, x5t: 5.59, x20: 39.9, x20t: 5.16, vr: 1.045, y2025: 40.0, y2026: 36.6 },
  { bucket: '2 个', n: 17466, x5: 12.3, x5t: 3.75, x20: 29.6, x20t: 2.82, vr: 1.034, y2025: 24.1, y2026: 35.5 },
  { bucket: '3–4 个', n: 17692, x5: 9.6, x5t: 2.17, x20: 22.9, x20t: 1.68, vr: 1.032, y2025: 15.7, y2026: 34.6 },
  { bucket: '5–9 个', n: 14471, x5: 7.6, x5t: 1.39, x20: 25.1, x20t: 1.52, vr: 1.025, y2025: 24.4, y2026: 23.9 },
  { bucket: '≥10 个', n: 7622, x5: 8.8, x5t: 1.30, x20: 22.6, x20t: 1.13, vr: 1.027, y2025: 28.2, y2026: 11.3 },
]

export interface PairRow {
  key: string
  label: string
  real: number
  placebo: number
  diff: number
  /** 波动比那行的 t 没有意义（差值是四舍五入后的 0，方差极小 ⇒ t 虚高），置 null 显示「—」。 */
  t: number | null
  win: number
  unit: string
}

/** 配对安慰剂：主线榜前 30 − 同日随机 30（每日配对，消掉市场共同因子）。 */
export const PAIR: PairRow[] = [
  { key: 'x5', label: '未来 5 日超额', real: 2.9, placebo: 10.4, diff: -7.5, t: -1.10, win: 50.4, unit: 'bp' },
  { key: 'x20', label: '未来 20 日超额', real: -2.5, placebo: 30.9, diff: -33.3, t: -1.71, win: 57.4, unit: 'bp' },
  { key: 'vr', label: '波动比（未来/过去）', real: 1.10, placebo: 1.10, diff: 0.0, t: null, win: 43.1, unit: '' },
]

export interface TierRow {
  tier: string
  diff: number
  t: number
  days: number
}

/** 名次分档：榜一没有额外信息，三档的差都在 −33 ~ −41bp。 */
export const TIERS: TierRow[] = [
  { tier: '第 1–3 名', diff: -41.2, t: -1.31, days: 371 },
  { tier: '第 4–10 名', diff: -32.8, t: -1.31, days: 371 },
  { tier: '第 11–30 名', diff: -32.7, t: -1.61, days: 358 },
]

/** 边界：五条会改变结论的东西。缺一条，这页就变成另一个「热度选股」。 */
export const BOUNDS = [
  '概念成分是当前快照（ext_gn_ths，本地 2026-07 起留存），没有历史版本；早年归属漂移，只做 2025-05 之后。',
  '样本是一个上行 regime（2025-05 ~ 2026-11），含 2026-11-27 的单日 428 只涨停；换到下行段，符号可能翻。',
  '超额基准是「同日全体概念中位」，概念数 389 个等权；成员数门槛 5 只，小概念（<5）被剔除。',
  '叙事腿所有 |t| < 2；解禁腿到 t −12.0（16,928 个事件）。但解禁只有一个市场、一个制度周期，注册制后的稳定性要每年重测（K7）。',
  'fwd 用概念等权净值（收盘到收盘），未计涨跌停无法成交、未计冲击成本；可实施性比读数更差。',
]

export interface KillRow {
  id: string
  claim: string
  kill: string
}

/** 杀死条件：写在这里，是为了下一次有人想把它变成信号时，先被这几条拦住。 */
export const KILL: KillRow[] = [
  {
    id: 'K1',
    claim: '热度 → 方向',
    kill: '剂量跨度（1 → ≥10）在任何一年超过 +50bp 或连续两年为正 ⇒ 「热度已支付」死，改回动量。',
  },
  {
    id: 'K2',
    claim: '热度 → 方差',
    kill: '高热度档的波动比中位 ≥ 1.20 且单调 ⇒ 热度可做波动率交易（买方或卖方）。',
  },
  {
    id: 'K3',
    claim: '隔夜腿是结算腿',
    kill: '打板溢价的隔夜占比跌破 60% ⇒ 「日结算」这条腿换地方结账，本页的隔夜读法作废。',
  },
  {
    id: 'K4',
    claim: '词频领先板块',
    kill: '上榜理由高频词对应的概念，未来 5 日超额 > +50bp 且 t > 2 ⇒ 文本层有未定价信息，需重做。',
  },
  {
    id: 'K5',
    claim: 'ETF 占比是拥挤度',
    kill: 'ETF 成交额占比的分位与未来 20 日指数收益 |ρ| > 0.3 ⇒ 它是信号，不只是结算工具。',
  },
  {
    id: 'K6',
    claim: '榜一持续',
    kill: '同一概念连续 3 日 rank=1 之后的 20 日超额 > 0 且 t > 2 ⇒ 主线有延续性，本页的「收据」结论死。',
  },
  {
    id: 'K7',
    claim: '解禁前负漂移',
    kill: '注册制后（2023 起）样本里 ≥10% 组的解禁前超额转正 ⇒ 「提前支付、到期结算」死，改回「供给压力持续」，' +
      '撤掉排除规则。每年重跑一次即判。（解禁后 +51bp 不列为条件：它 t 2.08、刚过成本，太薄，' +
      '翻一次也不足以推翻解禁前 −276bp 的主体证据。）',
  },
]


// ---------------------------------------------------------------- 事件腿（解禁）

export interface EventBucketRow {
  bucket: string
  events: number
  days: number
  pre: number
  preT: number
  post: number
  postT: number
  didPre: number
  didPost: number
}

/**
 * 解禁：按「本次解禁股数 / 流通股数」分档。
 *
 * 读法：解禁前 20 日（open[t-20] → close[t-1]）与解禁后 20 日（open[t+1] → close[t+20]）
 * 相对**同日全市场等权收益**的超额。DiD = 事件 − 同股票 ±120 交易日随机日（安慰剂）。
 * 基准从「中位」换成「等权均值」，是因为收益分布右偏，中位口径会把所有个股超额
 * 系统性抬高约 +400bp/20日 —— 价差仍可读，但水平不可读。
 */
export const EVENT_BUCKETS: EventBucketRow[] = [
  { bucket: '<1%', events: 6384, days: 1696, pre: -37.4, preT: -1.64, post: 62.9, postT: 2.64, didPre: -29.5, didPost: 65.2 },
  { bucket: '1–3%', events: 2540, days: 1228, pre: -93.8, preT: -2.29, post: 32.4, postT: 0.94, didPre: -69.2, didPost: 60.3 },
  { bucket: '3–10%', events: 2574, days: 1267, pre: -172.2, preT: -6.15, post: 127.7, postT: 3.67, didPre: -156.3, didPost: 141.5 },
  { bucket: '≥10%', events: 5430, days: 1583, pre: -276.5, preT: -12.02, post: 50.8, postT: 2.08, didPre: -261.6, didPost: 56.6 },
]

export interface EventMomentumRow {
  terc: string
  hi: number
  lo: number
  diff: number
  m120Hi: number
  m120Lo: number
}

/** 前期动量控制：同一「过去 120 日超额」三分位内，比较 ≥10% 与 <1%。差仍在 −200 ~ −408bp。 */
export const EVENT_MOMENTUM: EventMomentumRow[] = [
  { terc: '低动量', hi: -694.6, lo: -492.7, diff: -201.9, m120Hi: -26.8, m120Lo: -26.6 },
  { terc: '中动量', hi: -330.8, lo: -135.3, diff: -195.6, m120Hi: -10.3, m120Lo: -7.5 },
  { terc: '高动量', hi: 182.0, lo: 589.7, diff: -407.7, m120Hi: 13.6, m120Lo: 20.6 },
]

export interface EventKindRow {
  kind: string
  events: number
  pre: number
  preT: number
  post: number
  postT: number
}

/** ≥10% 桶按限售类型拆：三类同向，说明不是某一种股份的 artifact。 */
export const EVENT_KIND: EventKindRow[] = [
  { kind: '定增解禁', events: 1408, pre: -304.7, preT: -9.44, post: 81.3, postT: 2.22 },
  { kind: '首发原股东', events: 2828, pre: -318.1, preT: -11.75, post: 34.1, postT: 1.12 },
  { kind: '其他（含股权激励）', events: 1190, pre: -248.6, preT: -4.09, post: 81.9, postT: 1.41 },
]

/** 定增：近 12 个月存量。发行价是合约面值，破发率是「面值 − 市价」的存量读数。 */
export const SEO_READ = {
  deals12m: 318,
  stocks12m: 295,
  raiseYi: 6732,
  breakRate: 49.1,
  vsIssueMed: 0.7,
  nValid: 318,
  note: '近 12 个月上市的定增（ISSUE_LISTING_DATE 口径）。破发率 = 现价 < 发行价的比例；' +
    '发行价是合约面值，但它不产生漂移——锁定期内不能卖，到期才结算（解禁那一段已单独测）。',
} as const

/** 371 日逐日表里「剔除事件股」的读数：剂量跨度不变，说明叙事结论不靠事件股撑着。 */
export const EXCLUSION_CHECK = {
  doseAll: -15.9,
  doseExcluded: -15.9,
  days: 371,
  deltaMed: 0.1,
  note: '剔除规则：某股票在未来 20 个交易日内有解禁，或过去 20 个交易日内有定增上市，' +
    '则从当日的概念等权收益里去掉。371 天里逐日重算，剂量跨度 −15.9bp 未变。',
} as const

export const SCOPE = {
  can: [
    '读今天的温度：涨停/连板/封板率/涨跌家数/成交额，各自的历史分位',
    '读钱从哪条腿来：两融、龙虎榜、大宗、ETF 占成交额比，各自带 as_of',
    '读市场在用哪些词讲今天的事（上榜理由词频 + 净买额）',
    '读主要指数的波动分位与隔夜/日内分解',
    '做排除：不要把热度、连板高度、榜一当买点',
  ],
  cannot: [
    '预测明天哪个板块涨',
    '判断一条叙事是真是假（这需要公告、订单、产业数据，本仓没有）',
    '给出个股买点',
    '把「主线榜」当成持仓建议',
    '替代 /chip 的贴线候选：那条在负债端，不在这页',
  ],
}

/** 今日账本的分块标题 —— 与后端 blocks 的 key 一一对应，测试读它。 */
export const LIVE_BLOCKS = [
  { key: 'temp', title: '温度', hint: '涨停梯队与赚钱效应，各项 250 日分位' },
  { key: 'mainline', title: '主线榜', hint: '当日涨停梯队聚合前 10，附 5/20 日涨幅与成交额占比' },
  { key: 'flows', title: '资金四路', hint: '两融 / 龙虎榜 / 大宗 / ETF 占成交额比，四源时滞不同' },
  { key: 'index', title: '指数波动', hint: '主要宽基的 20 日已实现波动分位与隔夜/日内分解' },
  { key: 'words', title: '词频', hint: '龙虎榜上榜理由的高频词与对应净买额' },
] as const
