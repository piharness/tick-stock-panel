/**
 * 筹码页（卷三–卷六）的全部数字 —— 唯一来源。
 *
 * 这一版把旧页的「成本分布」扩成「持仓的期限结构」：
 *   - 旧页的冻结数字保留，作为「均值场极限」的退化证据；
 *   - 新页的年龄面板来自 analysis/_build_chip_age.py（2005-2026 日线，95 个月末）；
 *   - 持有人结构来自 analysis/_build_chip_holder.py（top_float 2026Q3）；
 *   - 两张 (价格 × 年龄) 热力图来自 analysis/_build_chip_examples.py。
 *
 * 口径（改数之前先读）：
 *   - 年龄递推：S_t(a)=TO_{t-a}·Π_{u=t-a+1..t}(1−TO_u)，TO=turnover_rate/100，A=500。
 *   - 久期 D=Σ a·S(a)/ΣS(a)；年龄熵 H=−Σ p ln p；young20=Σ_{a<20}S/ΣS；
 *     old_share=1−Σ_{a<500}S。月末截面先取中位，再按年取中位。
 *   - 持有人：top_float 只覆盖前十大流通股东；剔除托管/结算/证金类账户后算
 *     「真实持有人」的网络。比例是占总股本 %，不是占自由流通盘 %。
 *   - 旧页数字（dens/匹配/泛函）保持 2026-11-26 冻结值，不重新估计。
 *
 * 改数唯一路径：重跑脚本 → 改本文件 → 更新 META.asOf；页面上任何读数都必须能在这里找到。
 */

export type Verdict = 'ok' | 'warn' | 'danger' | 'muted'

export const META = {
  asOf: '2026-11-28',
  sample: '5,225 只 · 79 个月末 · 8,559 对路径孪生',
  universe: '活簿 2020-01 ~ 2026-07；质押事件 2019-04 ~ 2026-08',
  window: '存量簿 2020–2026 · 事件 2019–2026',
  convention: '活合约=start≤t 且（unfreeze>t 或未解押）；near=openline/close∈[0.8,1.2]；路径孪生=同日同规模带 60 日路径最近邻',
} as const

/** 页头摘要：先回答「所以呢」，再让人往下读。 */
export const SUMMARY = [
  {
    label: '十六条读数',
    value: '一条候选',
    verdict: 'warn' as Verdict,
    note: '其余十五条是零、半活或不可识别；它们构成排除清单，不构成策略。H1–H9 没有一个稳定收益符号。',
  },
  {
    label: '唯一候选',
    value: '+291bp / 60日',
    verdict: 'warn' as Verdict,
    note: '贴线组相对「同日、同跌幅、同波动/换手/规模」的同特征基准。t 5.28，6/7 年为正；不是 near−far 的 +525。',
  },
  {
    label: '证据等级',
    value: 'B · 准 OOS 过线',
    verdict: 'warn' as Verdict,
    note: '纸面 holdout +293.7bp / t 1.80，组合 10.5% / DD −6.5%；但 0.8% 成本 t 1.62、1.0% t 1.49。真前瞻 2027-06-30 前不部署。',
  },
  {
    label: '可执行动作',
    value: '0',
    verdict: 'muted' as Verdict,
    note: '候选 ≠ 买点。纸面只到 CONDITIONAL_PASS；真前瞻未跑，成本敏感，不接账户。',
  },
]

/**
 * 收敛后的唯一结论。写在摘要之前，因为它是整页的答案；
 * 四个摘要只是它的四个读数。
 */
export const VERDICT = {
  tag: '一个候选 · 其余排除',
  tone: 'warn' as Verdict,
  line: '筹码页不产出买点；它产出一个候选、一张体检表、一份排除清单。',
  body:
    '前四卷在持仓表（资产端）上找到的十六个读数里，十五条没有稳定的收益符号。卷五把唯一有余额的 H11 ' +
    '重读成应付表（负债端），只活下来一条相对值：股价越贴质押平仓线，未来 60 日越强于「同日、同跌幅、同波动/换手/规模」的同特征基准，' +
    '长腿 +291bp（t 5.28），6/7 年为正。纸面 holdout 过线（+293.7bp / t 1.80，组合 10.5% / DD −6.5%），' +
    '但只在 0.5% 成本下；0.8% 即失守（t 1.62）。真前瞻 2026-11-30 → 2027-06-30，未裁决。',
  action: '当前可执行动作：0',
  actionValue: '0',
  actionNote: '真前瞻未跑；成本敏感（0.8% 失守）。评估日 2027-06-30 前，不接账户、不登记信号。',
} as const

export interface EvidenceRow {
  grade: 'A' | 'B' | 'C'
  claim: string
  read: string
  use: string
}

/** 证据分级：A 可复算，B 探索性，C 不可识别。没有 D，因为「没测」不配进表。 */
export const EVIDENCE: EvidenceRow[] = [
  {
    grade: 'A',
    claim: '筹码分布 = 换手率的投影',
    read: 'R²(log dens ~ log TO15) 0.689；11 控后 −23bp / t −0.74',
    use: '解释为什么「形状」没有独立信息；别再调密度参数。',
  },
  {
    grade: 'A',
    claim: '状态变量没有收益符号',
    read: 'H1–H9：零个稳定符号；H1 只活成换手/久期的风险因子',
    use: '不要再用筹码形态、久期、集中度做选股。',
  },
  {
    grade: 'A',
    claim: '活合约存量簿为零',
    read: 'v2 +6.0bp / t 0.34；v1 的弱负号是 G1 复权 artifact',
    use: '不要用「质押存量/解押压力」做多空。',
  },
  {
    grade: 'B',
    claim: '贴线溢价：长腿 +291bp',
    read: '同日同跌幅三次曲线 +229bp（t 3.63）；加波动/换手/规模 +291bp（t 5.28）；纸面 holdout +293.7bp / t 1.80；far −94bp',
    use: '唯一值得冻结的候选；只做多，不做 near−far 的 +525；0.8% 成本即失守。',
  },
  {
    grade: 'B',
    claim: '合约锚比通用锚测得更干净',
    read: 'P1 位移 −314→−127 平滑；对通用锚正交后残差 −147bp / t −3.25',
    use: '测量工具的改进，不是新维度；解释力只有 48.3%。',
  },
  {
    grade: 'C',
    claim: '解除方式决定符号',
    read: 'P2 国资 268 事件 / 43 只 / 11 月；Δ +1,269 不可识别',
    use: '要救助/解押/重组事件数据；现在测不了，不是零。',
  },
]

/** 页面能回答与不能回答的问题。两列都写，避免读者把体检表当信号。 */
export const SCOPE = {
  can: [
    '读当前持仓的年龄结构：D、H、young20、q90',
    '判断一只股票的筹码在老钱还是新钱手里',
    '解释为什么筹码形态、集中度、久期不产生 alpha',
    '做排除：不要把换手/涨停/热度当买点',
  ],
  cannot: [
    '选股',
    '择时',
    '判断质押风险的大小',
    '预测解押、救助或重组的时点',
    '把 +525bp、+1011bp 当作买入信号',
  ],
}

/**
 * H13：贴线溢价的三层归因（2026-11-27）。
 *
 * 旧页只报 near−far +525bp，读者会把它当成「贴线组跑赢」。
 * 但 +525 是多空差；只做多的人能拿到的是长腿相对基准的 +291bp。
 * 而 +291 只有在「同日 + 同跌幅曲线 + 同波动/换手/规模」的对照下才成立——
 * 线性控制 drift 会把系数压到 +47bp/t 0.83，因为真实关系是阈值/凸的，不是线性的。
 */
export const MATCH = {
  nearAbs: 1011.4,
  farAbs: 150.4,
  spread: 525.3,
  spreadT: 7.47,
  nearResidCubic: 229.3,
  nearResidCubicT: 3.63,
  nearResidFull: 290.6,
  nearResidFullT: 5.28,
  farResidFull: -94.3,
  farResidFullT: -4.37,
  diffFull: 384.9,
  linearRatio: 47.2,
  linearRatioT: 0.83,
  linearDrift: -247.0,
  linearDriftT: -3.70,
  years: [
    { year: 2020, bp: 453.9 },
    { year: 2021, bp: -34.7 },
    { year: 2022, bp: 529.7 },
    { year: 2023, bp: 256.2 },
    { year: 2024, bp: 206.6 },
    { year: 2025, bp: 287.3 },
    { year: 2026, bp: 256.7 },
  ],
  note: '对照 = 公告日「同事件池」横截面（2,286 只有质押记录的股票）：f60 ~ ret250 三次 + ret60 + vol20 + turn20 + log(amount20)。同期拟合，含未来信息——它是归因基准，不是可实施基准；未控行业、未剔 ST、未处理退市缺口。',
} as const

export interface DegeneracyRow {
  symptom: string
  old: string
  reading: string
}

/** 三个症状不是三次失败，是同一次投影错误的三个投影。 */
export const DEGENERACY: DegeneracyRow[] = [
  {
    symptom: '密度 = 换手率的影子',
    old: 'R²(log dens ~ log TO15)=0.689；去掉 TO 权重后 −15bp',
    reading: 'a 被积分掉：只剩一个时间尺度',
  },
  {
    symptom: '形状泛函没有独立信息',
    old: 'L 全 11 控 −23bp / t −0.74；CV −2bp / t −0.10',
    reading: '价格形状不是期限形状',
  },
  {
    symptom: '条件效应匹配后剩 −26bp',
    old: 'NW t −0.84；完整控制 −23bp / t −0.74',
    reading: '正负伽马相抵后的平均',
  },
]

export interface Layer {
  tag: string
  title: string
  body: string
  data: string
}

export const LAYERS: Layer[] = [
  {
    tag: '存量层',
    title: 'S(p, a)：成本 × 年龄',
    body: '日频换手递推。输出久期 D、年龄熵 H、年龄分位、新钱占比、500 日以上老钱占比。',
    data: '5,582 只 × 95 个月末',
  },
  {
    tag: '约束层',
    title: 'c：谁持有、什么约束',
    body: '前十大流通股东的类型、集中度、HHI。剔除法人与托管/结算后，个人占 81.9%、基金 7.0%、险资 1.6%。',
    data: '5,650 只 · 2026Q3',
  },
  {
    tag: '网络层',
    title: 'Aᵢⱼ：共同持有人',
    body: '真实持有人的共同持股。中位共同持有人 3 个、p90 7 个；shared_score 中位 39.5、p90 213.4。',
    data: '诺安多策略 236 只 / 中国人寿 136 只',
  },
]

export interface AgeYear {
  year: number
  d: number
  p25: number
  p75: number
  h: number
  young: number
  q90: number
}

/** 年度中位：月末截面先取中位，再按年取中位。 */
export const AGE_YEARS: AgeYear[] = [
  { year: 2019, d: 97.5, p25: 58.5, p75: 143.9, h: 5.29, young: 16.6, q90: 219 },
  { year: 2020, d: 74.3, p25: 43.4, p75: 117.4, h: 5.05, young: 21.4, q90: 167 },
  { year: 2021, d: 77.1, p25: 45.0, p75: 119.2, h: 5.08, young: 20.1, q90: 169 },
  { year: 2022, d: 70.2, p25: 41.8, p75: 111.6, h: 5.00, young: 20.9, q90: 152 },
  { year: 2023, d: 78.0, p25: 45.5, p75: 119.9, h: 5.11, young: 19.3, q90: 171 },
  { year: 2024, d: 61.0, p25: 32.6, p75: 102.3, h: 4.90, young: 28.1, q90: 140 },
  { year: 2025, d: 38.3, p25: 22.7, p75: 66.8, h: 4.47, young: 37.4, q90: 83 },
  { year: 2026, d: 38.5, p25: 23.1, p75: 62.2, h: 4.51, young: 37.9, q90: 86 },
]

export const CROSS = {
  latestD: 43.5,
  p10: 15.6,
  p90: 104.9,
  corrCap: 0.373,
  corrYoung: -0.80,
  corrEntropy: 0.885,
  capQ: [
    { q: 'Q1 小', d: 37.1, h: 4.47, young: 39.0 },
    { q: 'Q2', d: 40.2, h: 4.54, young: 36.5 },
    { q: 'Q3', d: 40.8, h: 4.56, young: 35.3 },
    { q: 'Q4', d: 47.2, h: 4.70, young: 32.5 },
    { q: 'Q5 大', d: 66.6, h: 5.07, young: 22.5 },
  ],
  latest: {
    h: 4.645,
    young: 33.9,
    new5: 9.5,
    q50: 32,
    q90: 96,
  },
}

export interface Heatmap {
  symbol: string
  name: string
  date: string
  close: number
  d: number
  h: number
  young20: number
  oldShare: number
  priceEdges: number[]
  ageEdges: number[]
  matrix: number[][]
}

export const HEATMAPS: Heatmap[] = [
  {
    symbol: "601398.SH", name: "工商银行",
    date: "2026-11-26", close: 9.24,
    d: 221.8, h: 6.15, young20: 6.5, oldShare: 52.3,
    priceEdges: [0.6, 0.64, 0.68, 0.72, 0.76, 0.8, 0.84, 0.88, 0.92, 0.96, 1.0, 1.04, 1.08, 1.12, 1.16, 1.2, 1.24, 1.28, 1.32, 1.36, 1.4],
    ageEdges: [0.0, 25.0, 50.0, 75.0, 100.0, 125.0, 150.0, 175.0, 200.0, 225.0, 250.0, 275.0, 300.0, 325.0, 350.0, 375.0, 400.0, 425.0, 450.0, 475.0, 500.0],
    matrix: [
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.030366],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.010142, 0.016994, 0.004452],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.006026, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.022055, 0.037405, 0.023332, 0.027178, 0.0],
      [0.0, 0.0, 0.0, 0.018628, 0.04248, 0.019852, 0.02404, 0.043853, 0.010096, 0.0, 0.002175, 0.022541, 0.002457, 0.001533, 0.032296, 0.023449, 0.0, 0.004332, 0.0, 0.0],
      [0.0, 0.0, 0.024713, 0.044166, 0.020263, 0.021835, 0.026011, 0.0, 0.0194, 0.0, 0.019351, 0.033366, 0.041545, 0.029752, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.010263, 0.027457, 0.018859, 0.0, 0.0, 0.0, 0.0, 0.01565, 0.024556, 0.021927, 0.0, 0.005028, 0.009759, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.026576, 0.035417, 0.006856, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.014379, 0.00764, 0.0, 0.0, 0.004472, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.028058, 0.008765, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.028253, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    ],
  },
  {
    symbol: "000002.SZ", name: "万科A",
    date: "2026-11-26", close: 2.72,
    d: 42.4, h: 4.49, young20: 27.2, oldShare: 0.0,
    priceEdges: [0.6, 0.64, 0.68, 0.72, 0.76, 0.8, 0.84, 0.88, 0.92, 0.96, 1.0, 1.04, 1.08, 1.12, 1.16, 1.2, 1.24, 1.28, 1.32, 1.36, 1.4],
    ageEdges: [0.0, 25.0, 50.0, 75.0, 100.0, 125.0, 150.0, 175.0, 200.0, 225.0, 250.0, 275.0, 300.0, 325.0, 350.0, 375.0, 400.0, 425.0, 450.0, 475.0, 500.0],
    matrix: [
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.046027, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.046313, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.055671, 0.007384, 0.0, 0.005115, 0.011295, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.050197, 0.004595, 0.037731, 0.026472, 0.015532, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.152788, 0.015865, 0.039473, 0.011658, 0.00332, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.074589, 0.0, 0.004194, 0.011559, 0.00589, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.05438, 0.041137, 0.0, 0.0, 0.005415, 0.0025, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.071515, 0.0, 0.0, 0.00399, 0.001046, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.085523, 0.0, 0.0, 0.0, 0.002884, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
      [0.0, 0.103853, 0.0, 0.0, 0.0, 0.00168, 0.000409, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    ],
  },
]

export const H1 = {
  n: 375378,
  dates: 93,
  symbols: 5290,
  start: '2019-01-31',
  end: '2026-09-30',
  corrMD: -0.85,
  corrMTurn: 0.799,
  medianT: 28.5,
  medianD: 63.9,
  ratioRho: 0.3796,
  ratioT: 37.04,
  mddQ1: -8.81,
  mddQ5: -14.84,
  mddSpread: -6.02,
  fwdSpread: -0.53,
  fwdT: -1.42,
  bLogT: 0.0743,
  tLogT: 11.35,
  bLogD: -0.2261,
  tLogD: -12.84,
  interaction: 0.029,
  interactionT: 2.30,
  fwdInteraction: -0.46,
  fwdInteractionT: -4.41,
  skewRho: 0.011,
  skewT: 1.58,
  recoveryRho: 0.019,
  recoveryT: 1.47,
  oos1: 0.3513,
  oos2: 0.4097,
  placebo: 100,
  verdict: '部分通过 / 已分解',
} as const

export interface H1Cell {
  d: string
  tlo: number
  tmid: number
  thi: number
  mdd: number
}

/** D 五分位 × T 三分位的 fragility；mdd 是 T 高组。 */
export const H1_CELLS: H1Cell[] = [
  { d: 'Q1 短', tlo: 0.300, tmid: 0.399, thi: 0.563, mdd: -15.82 },
  { d: 'Q2', tlo: -0.001, tmid: 0.075, thi: 0.179, mdd: -12.97 },
  { d: 'Q3', tlo: -0.145, tmid: -0.077, thi: 0.025, mdd: -11.79 },
  { d: 'Q4', tlo: -0.258, tmid: -0.199, thi: -0.072, mdd: -11.04 },
  { d: 'Q5 长', tlo: -0.414, tmid: -0.337, thi: -0.200, mdd: -10.06 },
]

export const H2 = {
  n: 95977,
  dates: 18,
  symbols: 5521,
  start: '2025-04-30',
  end: '2026-09-30',
  fwdInt: 0.6,
  fwdT: 0.09,
  fragInt: 0.0149,
  fragT: 2.47,
  revFrag: 0.0181,
  revFragT: 4.36,
  revFwd: -5.6,
  revFwdT: -0.85,
  placebo: 58,
  revPlacebo: 100,
  oos2025: 15.9,
  oos2026: -14.7,
  densTo: -26.2,
  densToT: -2.16,
  status: '合成失败 / 分项反转（探索）',
} as const

export interface H2Comp {
  comp: string
  cover: number
  fwd: number
  fwdT: number
  frag: number
  fragT: number
  read: string
  tone: Verdict
}

/** 分项是探索性（14 个检验），不把「符号反转」写成结论。 */
export const H2_COMPS: H2Comp[] = [
  { comp: '个人', cover: 0.894, fwd: -2.9, fwdT: -0.44, frag: -0.0145, fragT: -2.46, read: '正伽马候选', tone: 'ok' },
  { comp: '户数集中', cover: 0.557, fwd: 6.5, fwdT: 0.97, frag: -0.0102, fragT: -2.78, read: '正伽马候选', tone: 'ok' },
  { comp: '基金', cover: 0.539, fwd: 12.1, fwdT: 1.06, frag: 0.0067, fragT: 2.03, read: '负伽马候选', tone: 'danger' },
  { comp: '法人', cover: 0.882, fwd: -4.3, fwdT: -0.57, frag: 0.0117, fragT: 2.45, read: '负伽马候选', tone: 'danger' },
  { comp: '险资', cover: 0.098, fwd: 4.8, fwdT: 0.62, frag: 0.004, fragT: 0.75, read: '样本太薄', tone: 'muted' },
  { comp: '社保', cover: 0.135, fwd: 7.9, fwdT: 1.17, frag: -0.0001, fragT: -0.05, read: '样本太薄', tone: 'muted' },
  { comp: 'QFII', cover: 0.024, fwd: 0.8, fwdT: 0.09, frag: 0.0067, fragT: 1.18, read: '样本太薄', tone: 'muted' },
]

export const H3 = {
  n: 191721,
  dates: 2377,
  symbols: 5208,
  start: '2017-01-10',
  end: '2026-10-28',
  kBand: 0.5,
  fast: 5,
  upSlow: 11.7,
  upFast: -69.8,
  dnSlow: 5.5,
  dnFast: 116.0,
  spread: 473.8,
  ud: -89.8,
  udT: -3.24,
  inter: 192.0,
  interT: 7.39,
  regUp: -42.4,
  regUpT: -2.12,
  regFast: 49.6,
  regFastT: 2.31,
  regInt: -129.3,
  regIntT: -4.61,
  placeboUd: -166.8,
  placeboT: -6.27,
  placeboRetention: 1.86,
  placeboDraws: 20,
  verdict: '失败 · 雪崩格反向',
} as const

export interface H3Cell {
  cell: string
  bp: number
  t: number
  pred: string
  actual: string
  tone: Verdict
}

/** 四格；「下穿·快」是预注册的雪崩格，实测 +116bp。 */
export const H3_CELLS: H3Cell[] = [
  { cell: '上穿 · 慢', bp: 11.7, t: 0.18, pred: '吸收 +', actual: '持平', tone: 'muted' },
  { cell: '上穿 · 快', bp: -69.8, t: -1.18, pred: '拒绝 −', actual: '符合', tone: 'ok' },
  { cell: '下穿 · 慢', bp: 5.5, t: 0.08, pred: '支撑 +', actual: '持平', tone: 'muted' },
  { cell: '下穿 · 快', bp: 116.0, t: 1.74, pred: '雪崩 −', actual: '反向：反弹', tone: 'danger' },
]

export interface H3Dose {
  k: string
  n: number
  ud: number
  udT: number
  sf: number
}

/** 带宽剂量反应：若密度在做事，K 越大效应应越弱。实测平坦。 */
export const H3_DOSE: H3Dose[] = [
  { k: '0.25', n: 205059, ud: -102.8, udT: -4.5, sf: -15.9 },
  { k: '0.50', n: 191721, ud: -89.8, udT: -3.2, sf: -14.6 },
  { k: '0.75', n: 169059, ud: -90.1, udT: -2.2, sf: -4.7 },
  { k: '1.00', n: 131566, ud: -87.4, udT: -1.9, sf: -27.3 },
  { k: '1.50', n: 48461, ud: -95.4, udT: -1.2, sf: -35.9 },
]

export interface H3Bucket {
  bucket: string
  dn20: number
  dn60: number
  up20: number
  up60: number
}

/** 事后分桶（探索）：按 |cum5| 分组，fwd 严格单调 = 过反应修正。 */
export const H3_BUCKETS: H3Bucket[] = [
  { bucket: '0–2%', dn20: -33.0, dn60: 6.6, up20: 27.5, up60: 119.1 },
  { bucket: '2–4%', dn20: 25.3, dn60: 75.3, up20: 2.2, up60: 58.2 },
  { bucket: '4–6%', dn20: 68.0, dn60: 112.5, up20: -21.6, up60: 27.0 },
  { bucket: '6–8%', dn20: 129.2, dn60: 237.7, up20: -59.8, up60: -17.6 },
  { bucket: '8–10%', dn20: 180.4, dn60: 195.4, up20: -68.8, up60: -34.3 },
  { bucket: '10–15%', dn20: 83.2, dn60: 166.6, up20: -72.0, up60: -100.1 },
  { bucket: '15–22%', dn20: 132.2, dn60: 142.6, up20: -161.3, up60: -267.8 },
]

export const H4 = {
  n: 100012,
  dates: 1894,
  symbols: 5183,
  start: '2019-01-31',
  end: '2026-11-26',
  mktDays: 53,
  entropyVsDuration: 0.989,
  dispVsDuration: -0.285,
  dispVsTurn: 0.419,
  q1rec20: 68.9,
  q5rec20: 65.8,
  spreadRec20: -3.18,
  spreadT: -3.16,
  q1half: 15.0,
  q5half: 17.7,
  bDisp: 0.0056,
  tDisp: 1.73,
  bExcess: 0.0085,
  tExcess: 2.66,
  mktRec20: -2.64,
  mktT: -2.10,
  speedRatio: 0.851,
  verdict: '失败 · 冲击深度混淆',
} as const

export interface H4Quintile {
  q: string
  disp: number
  drop: number
  vol: number
  rec20: number
  half: number
}

export const H4_QUINTILES: H4Quintile[] = [
  { q: 'Q1 最集中', disp: 1.97, drop: -5.57, vol: 2.10, rec20: 68.9, half: 15.0 },
  { q: 'Q2', disp: 2.46, drop: -5.65, vol: 2.16, rec20: 68.2, half: 15.4 },
  { q: 'Q3', disp: 2.89, drop: -5.85, vol: 2.25, rec20: 67.4, half: 15.0 },
  { q: 'Q4', disp: 3.48, drop: -6.42, vol: 2.43, rec20: 67.6, half: 15.3 },
  { q: 'Q5 最分散', disp: 4.98, drop: -7.55, vol: 3.10, rec20: 65.8, half: 17.7 },
]

export interface H4Cond {
  tercile: string
  drop: number
  rec20: number
  t: number
  half: number
}

/** 冲击幅度三分位内，效应归零。 */
export const H4_COND: H4Cond[] = [
  { tercile: '浅', drop: -8.59, rec20: -0.71, t: -0.54, half: 0.48 },
  { tercile: '中', drop: -5.66, rec20: 0.89, t: 0.50, half: -1.44 },
  { tercile: '深', drop: -4.10, rec20: 0.02, t: 0.01, half: -1.26 },
]

export const H5 = {
  n: 40859, quarters: 8, symbols: 5111, start: '2024-12-31', end: '2026-09-30',
  corrD: 0.235, corrSize: 0.295, corrTurn: -0.242,
  bTailMarket: -0.0849, tTailMarket: -2.29,
  bTailNet: -0.2098, tTailNet: -11.26,
  bAvgNet: 0.2522,
  placeboPercentile: 0.0,
  verdict: '失败 · 方向相反',
} as const

export const H6 = {
  months: 93, dLo: 33.4, dHi: 150.0,
  bicDiff: -4.5, needBic: 10,
  rhoLow: -0.388, rhoHigh: -0.346,
  bLinear: 0.001, tLinear: 2.69,
  corrDmMkt: -0.015,
  byYear: '2019 −0.341 → 2026 −0.445',
  verdict: '失败 · 无阈值 · 方向反',
} as const

export const H7 = {
  n: 1142209, months: 24, symbols: 5218, start: '2024-11-14', end: '2026-10-22',
  thetaMedian: 0.76, thetaP90: 4.0,
  corrThetaD0: -0.492, corrThetaPI: 0.223, corrThetaCum5: 0.054, corrThetaTo: 0.049,
  bThetaFrag: 0.0018, tThetaFrag: 0.78,
  bPIFrag: 0.0662, tPIFrag: 12.18,
  bD0Frag: -0.0238, tD0Frag: -2.84,
  bThetaFwd: -0.0064, tThetaFwd: -1.41,
  verdict: '失败 · 比值是噪声',
} as const

export interface H567Row {
  id: string
  claim: string
  pred: string
  actual: string
  tone: Verdict
}

/** 三条尾巴：都是失败，共享一条教训。 */
export const H567_ROWS: H567Row[] = [
  { id: 'H5', claim: '网络中心度 → 尾部传染', pred: '高中心度崩盘里跌得更一致', actual: '反向：高中心度崩盘里更稳（−0.085，t −2.3）', tone: 'danger' },
  { id: 'H6', claim: '记忆相变，不是线性衰减', pred: '低 D_m 时结构失去预测力', actual: '无阈值（BIC 差 −4.5）；低 D_m 结构更强', tone: 'danger' },
  { id: 'H7', claim: 'Θ 是滚续成本 → 脆弱性', pred: 'Θ 高 → 更脆', actual: 'Θ 预测力为零；分子 Π（换手）才做事', tone: 'danger' },
]

export const H8 = {
  n: 574828, months: 115, down: 292160, downHi: 61367, downHiShare: 0.21,
  upHi: -140.3, upLo: 42.2, dnHi: 51.1, dnLo: 209.3,
  l1: -158.1, t1: -5.72,
  interaction: 24.4, tInt: 1.17,
  upHiMinusLo: -182.6, tUp: -8.92,
  dnHiF40: 37.8, dnLoF40: 274.8, dnHiF60: 54.5, dnLoF60: 325.0,
  bLvrDown: -63.0, tLvr: -2.02,
  asymCont: -34.8, tAsymCont: -1.39,
  bHiControlled: -56.7, tHiCtl: -1.9,
  verdict: '失败 · 无雪崩',
} as const

export interface H8Cell {
  cell: string
  bp: number
  t: number
  tone: Verdict
}

/** 放量/缩量 × 上涨/下跌 四格。放量让反转更大，但方向对称。 */
export const H8_CELLS: H8Cell[] = [
  { cell: '上涨 · 放量', bp: -140.3, t: -2.18, tone: 'danger' },
  { cell: '上涨 · 缩量', bp: 42.2, t: 0.70, tone: 'ok' },
  { cell: '下跌 · 放量', bp: 51.1, t: 0.72, tone: 'muted' },
  { cell: '下跌 · 缩量', bp: 209.3, t: 3.01, tone: 'ok' },
]

export const H9 = {
  marginCorrD: -0.442, marginCorrTurn: 0.384, marginCorrFrag: 0.264,
  pledgeCorrD: 0.047, pledgeCorrTurn: -0.037, pledgeCorrFrag: -0.024,
  n: 247290, months: 91, pledgePos: 0.38, pledgeQ2: 12.57,
  q0: 165.5, q1: 251.7, q2: 200.1,
  f40: 45.2, tF40: 2.57, f60: 61.3, tF60: 3.3,
  bControlled: -4.5, tControlled: -1.68,
  verdict: '融资代理 · 质押正交但不雪崩',
} as const

export const H11 = {
  n: 20280, months: 80, near: 1478, far: 17396,
  f20: 206.8, tF20: 6.33, f40: 423.3, tF40: 6.58, f60: 525.3, tF60: 7.47,
  bNear: 182.3, tNear: 5.79,
  nearF20: 418.5, nearF40: 783.9, nearF60: 1011.4,
  spearmanF20: 0.976, spearmanF60: 1.0,
  corrRatioAccum: 0.06,
  verdict: '修正后：越贴线越反弹',
} as const

export interface H11Bucket {
  bucket: string
  f20: number
  f40: number
  f60: number
}

/** 股价距平仓线（ratio=平仓线/股价）的剂量反应：修正后是越近反弹越强。 */
export const H11_BUCKETS: H11Bucket[] = [
  { bucket: '≤0.4 最安全', f20: -29.3, f40: -95.6, f60: -132.9 },
  { bucket: '0.4–0.5', f20: 145.3, f40: 195.8, f60: 263.7 },
  { bucket: '0.5–0.6', f20: 149.2, f40: 198.9, f60: 272.6 },
  { bucket: '0.6–0.7', f20: 179.0, f40: 230.7, f60: 310.6 },
  { bucket: '0.7–0.8', f20: 162.8, f40: 282.9, f60: 383.5 },
  { bucket: '0.8–0.9', f20: 214.9, f40: 445.2, f60: 555.4 },
  { bucket: '0.9–1.0', f20: 302.0, f40: 518.5, f60: 580.0 },
  { bucket: '>1.0 最危险', f20: 369.4, f40: 710.4, f60: 977.6 },
]

export interface Phase {
  key: string
  d: string
  gamma: string
  title: string
  body: string
  tone: Verdict
}

export const PHASES: Phase[] = [
  { key: 'drift', d: 'D 高', gamma: 'Γ > 0', title: '漂移', body: '老钱持有，慢速回归；低脆弱。', tone: 'ok' },
  { key: 'trap', d: 'D 高', gamma: 'Γ < 0', title: '价值陷阱', body: '老筹码 + 被迫持有人；慢跌后一次性崩塌。', tone: 'danger' },
  { key: 'pin', d: 'D 低', gamma: 'Γ > 0', title: '钉住', body: '新钱 + 正伽马；密度是支撑，波动被吸收。', tone: 'ok' },
  { key: 'avalanche', d: 'D 低', gamma: 'Γ < 0', title: '雪崩', body: '新钱 + 负伽马；跌破成本即触发止损/赎回。', tone: 'danger' },
]

export interface Prediction {
  id: string
  claim: string
  test: string
  status: string
  tone?: Verdict
}

export const PREDICTIONS: Prediction[] = [
  { id: 'H1', claim: '久期错配 → 脆弱性，不是收益', test: '与下行偏度 |ρ|≥0.15、t≥2；与 fwd20 |ρ|<0.05', status: '部分通过 / 已分解', tone: 'warn' },
  { id: 'H2', claim: '伽马符号决定密度符号', test: 'dens×Γ 交互 ≥100bp，两组符号相反，|t|≥2', status: '合成失败 · 分项反转', tone: 'danger' },
  { id: 'H3', claim: '进入方向决定吸收/崩塌', test: '上/下 × 快/慢四格符号符合 2×2，极差 ≥80bp', status: '失败 · 雪崩格反向', tone: 'danger' },
  { id: 'H4', claim: '年龄熵 → 恢复半衰期', test: '高熵组恢复快 ≥30%，控制 β/市值后仍成立', status: '失败 · 冲击深度混淆', tone: 'danger' },
  { id: 'H5', claim: '网络中心度 → 尾部传染', test: '下行相关每 +1sd ≥+0.10，平均相关不变', status: '失败 · 方向相反', tone: 'danger' },
  { id: 'H6', claim: '记忆相变，不是线性衰减', test: '阈值模型 BIC 优于线性 ≥10，OOS 分段同号', status: '失败 · 无阈值', tone: 'danger' },
  { id: 'H7', claim: 'Θ 是滚续成本 → 脆弱性', test: '与下行偏度负相关、t≥2；与 fwd20 无关', status: '失败 · 比值是噪声', tone: 'danger' },
]

export interface ReinterpretRow {
  old: string
  oldRead: string
  newRead: string
}

export const REINTERPRET: ReinterpretRow[] = [
  { old: '没有势场 −0.125 vs −0.107', oldRead: '筹码不是物理场', newRead: '没有均衡力，但有约束流；负控只测了均值回归' },
  { old: '时钟 135d → 50d', oldRead: '记忆变短', newRead: '久期坍缩：97.5d → 38.5d（本页口径）' },
  { old: 'Θ 中位 0.75', oldRead: '量纲校准', newRead: '滚续成本：预测脆弱性，不预测收益' },
  { old: 'dens = 换手率影子', oldRead: '介质降级', newRead: 'a 被积分掉的必然结果' },
  { old: '形状泛函无信息', oldRead: '形状没用', newRead: '价格形状没用；期限形状（H/尾部）有信息' },
  { old: 'fwd20 归零', oldRead: '信号关闭', newRead: '因变量错了；均值不是脆弱性' },
  { old: 'dens_up/dn +106/+111', oldRead: '不是上方套牢', newRead: '方向被边际掉了；符号应由 Γ 决定' },
  { old: '大市值 +41/t0.84；递推 +74/t4.73', oldRead: '容量不够', newRead: '大市值是长钱，久期长、Γ 正；递推改善说明年龄重要' },
]

export const KILL: string[] = [
  'H1/H2/H3 全灭 → 只是一个更好听的隐喻，不配替代旧页。',
  '伽马代理互不一致 → 符号故事不可识别，Γ 只是拟合参数。',
  '网络中心度只是市值/行业 → 传染故事是旧酒新瓶。',
  '相变只是牛熊日历 → 久期只是市场收益的代理。',
  '2026 反弹是噪声 → 相图过拟合，H6 降级。',
  '年龄递推对核宽/停牌/折算敏感 → 新对象本身不可靠。',
]

/* ───────────── 卷四：约束的发起 ───────────── */

export interface OntologyRow {
  vol: string
  obj: string
  math: string
  why: string
  tone: Verdict
}

/** 三次换对象，三次都还在测状态。 */
export const ONTOLOGY: OntologyRow[] = [
  {
    vol: '卷一 质量',
    obj: '价格上的筹码密度',
    math: 'S(p)',
    why: '把时间边际掉：密度退化成换手率的核密度重构（R² 0.69），形状泛函没有独立信息。',
    tone: 'danger',
  },
  {
    vol: '卷二 改名',
    obj: '密度的局部形状',
    math: 'S(p) 的曲率',
    why: '把持有人边际掉：形状只是均线位置 + 换手的另一种写法；负控否决了「势」。',
    tone: 'danger',
  },
  {
    vol: '卷三 年龄',
    obj: '持仓的期限结构',
    math: 'S(p, a)',
    why: '把约束边际掉：D/T 是两条风险主效应，收益端归零；其余六条全灭。',
    tone: 'warn',
  },
  {
    vol: '卷三·追击 义务',
    obj: '活合约的触发价分布',
    math: 'Φ(p)',
    why: '把「发起」边际掉：活簿存量零、创新不稳（见 D 节）。第三次换对象仍然在测横截面。',
    tone: 'danger',
  },
]

export interface DimensionRow {
  dim: string
  state: string
  event: string
  why: string
}

/** 状态缺的三样东西：符号、日期、对手方。 */
export const DIMENSIONS: DimensionRow[] = [
  {
    dim: '符号',
    state: '幅度：密度 / 久期 / 熵 / 中心度 / 占比',
    event: '动作：押 / 卖 / 披露',
    why: '幅度只能靠估计发现符号，于是 OOS 翻转；动作的符号由约束在测量之前给定。',
  },
  {
    dim: '日期',
    state: '快照：月末 / 季度',
    event: '时点 + 期限：公告日 / 到期日 / 解押日',
    why: '没有日期，分不清「还没发生」与「不会发生」；滚续成本没有到期日就只是比值。',
  },
  {
    dim: '对手方',
    state: '聚合：持有人类型 / 共同持有网络',
    event: '具名：大股东 / 券商 / 基金',
    why: '没有对手方，约束不可执行；共同持有不是共同义务。',
  },
]

export interface UnifyRow {
  id: string
  what: string
  form: '状态' | '发起'
  result: string
  tone: Verdict
}

/** 十六个结果，一个二分：状态没有稳定符号，发起有。 */
export const UNIFY: UnifyRow[] = [
  { id: 'H1', what: '久期 D / T', form: '状态', result: '半活：两条风险主效应，收益端零', tone: 'warn' },
  { id: 'H2', what: '持有人伽马（类型）', form: '状态', result: '合成零、分项反转', tone: 'danger' },
  { id: 'H3', what: '成本带穿越（路径）', form: '状态', result: '雪崩格反向 +116bp', tone: 'danger' },
  { id: 'H4', what: '年龄熵', form: '状态', result: '冲击深度混淆，分层归零', tone: 'danger' },
  { id: 'H5', what: '网络中心度', form: '状态', result: '蓝筹轴；崩盘里更稳', tone: 'danger' },
  { id: 'H6', what: '记忆相变', form: '状态', result: '无阈值（BIC −4.5）', tone: 'danger' },
  { id: 'H7', what: 'Θ 滚续比', form: '状态', result: '比值噪声；分子换手 t 12.2', tone: 'danger' },
  { id: 'H8', what: '放量', form: '状态', result: '对称放大器，无方向', tone: 'danger' },
  { id: 'H9', what: '融资 / 质押水平', form: '状态', result: '融资=代理；质押水平不雪崩', tone: 'danger' },
  { id: 'H11', what: '质押公告贴线（修正）', form: '发起', result: 'f20 +207bp / t 6.33；f60 +525bp', tone: 'ok' },
  { id: 'H12', what: '拆因果（折算 vs 涨幅）', form: '发起', result: '涨幅 t −6.39；折算仍无效（t −0.23）', tone: 'ok' },
  { id: 'C1', what: '活簿 near / wdist', form: '状态', result: 'v1 −0.1 / v2 +6.0；wdist v2 −13.7（t −0.23）', tone: 'danger' },
  { id: 'C2', what: '解押安慰剂', form: '发起', result: '+763bp / t 2.31，但 5 只/月、内生', tone: 'warn' },
  { id: 'C3', what: '贴线 → 波动', form: '状态', result: 'v1 −0.024pp / v2 −0.011pp（t −2.0）；子样本符号互斥', tone: 'danger' },
  { id: 'C4', what: '贴线 × 90 天内到期', form: '状态', result: '−34.6bp / t −1.06', tone: 'danger' },
  { id: 'F1', what: '新合约贴线 vs 有簿但远', form: '发起', result: '+6.9bp / t 0.04；9 只/月', tone: 'danger' },
]

export interface BookRow {
  id: string
  spec: string
  read: string
  verdict: string
  tone: Verdict
}

/** 存量簿首测：三个规格，三个答案，没有一个是稳定的负号。 */
export const BOOK_ROWS: BookRow[] = [
  { id: 'C1', spec: 'near_share 五分位 → f60', read: 'Q1 +294.9 → Q5 +386.3bp；Q5−Q1 +91.4bp / t 1.94', verdict: '原始正向', tone: 'warn' },
  { id: 'C1', spec: '受控 near_share', read: '−0.1bp / t −0.01（控换手/波动/规模/久期/动量）', verdict: '零', tone: 'danger' },
  { id: 'C1b', spec: 'wdist 五分位 → f60', read: 'Q1 +150.7 → Q5 +416.9bp；Q5−Q1 +266.2bp / t 2.34', verdict: '原始反向', tone: 'warn' },
  { id: 'C1b', spec: '受控 wdist', read: '+40.0bp / t 0.77', verdict: '零', tone: 'danger' },
  { id: 'C1c', spec: '下跌子样本 wdist', read: 'ret20 最低 20%：+130.8bp / t 2.83；受控 +14.9 / t 0.80', verdict: '零', tone: 'danger' },
  { id: 'C2', spec: '解押安慰剂', read: 'released − live_near +763.5bp / t 2.31；5 只/月', verdict: '不可识别', tone: 'warn' },
  { id: 'C3', spec: '贴线 → 未来 20 日波动', read: '全样本 near −0.024pp / t −4.4；下跌子样本 near +0.04 / t 3.03、wdist −0.06 / t −3.04', verdict: '符号互斥', tone: 'danger' },
  { id: 'C4', spec: 'near × 90 天内到期', read: '−34.6bp / t −1.06', verdict: '无', tone: 'danger' },
  { id: 'F1', spec: '新合约贴线 vs 有簿但远', read: '+6.9bp / t 0.04；平均 9 只/月', verdict: '样本太薄', tone: 'danger' },
]

export interface BookGap {
  id: string
  gap: string
  impact: string
  fix: string
  status: '已修' | '部分' | '未修'
}

/** 存量簿的八个测量缺口，按影响排序。 */
export const BOOK_GAPS: BookGap[] = [
  {
    id: 'G1',
    gap: '平仓线未复权：raw line / adj close',
    impact: '2019 起始合约因子中位 0.90、2015 约 0.71 → 距离被放大 10–40%，足以把「贴线」推成「已穿」',
    fix: 'K_adj = line × C[start] / raw[start]',
    status: '已修',
  },
  {
    id: 'G2',
    gap: '入场用合约起始日，不是公告日',
    impact: '[start, notice) 区间用了市场还不知道的合约 → 前视',
    fix: 'entry = max(start, notice)；起始早于日线起点按 2019 年初入簿',
    status: '已修',
  },
  {
    id: 'G3',
    gap: '到期日语义混用',
    impact: '未解押的计划到期日被当成实际解押日 → 活簿被提前截断',
    fix: '未解押一直活；已解押按实际日',
    status: '已修',
  },
  {
    id: 'G4',
    gap: '「已解押但无日期」占 32%',
    impact: '整行丢弃 → book 系统性低估',
    fix: 'v2c 按 notice+365d 补寿命（敏感性）',
    status: '部分',
  },
  {
    id: 'G5',
    gap: '分母是总股本，不是流通盘',
    impact: '限售比例高的股票，簿占比被低估；横截面不可比',
    fix: '需历史流通股本；未修',
    status: '未修',
  },
  {
    id: 'G6',
    gap: '生存偏差 + 当前 ST 过滤',
    impact: '退市/后来 ST 的质押股被剔除 → 高杠杆组被美化',
    fix: '需历史名称/退市表；未修',
    status: '未修',
  },
  {
    id: 'G7',
    gap: '月频采样',
    impact: '事件效应可能集中在公告后数日；月末快照会漏掉',
    fix: '事件时间单独测（H11/H12）',
    status: '部分',
  },
  {
    id: 'G8',
    gap: '簿加权稀释单份新合约',
    impact: 'wdist 被老合约拉平；near_share 是占比不是敞口',
    fix: '同报 max ratio / 单合约距离（未做）',
    status: '部分',
  },
]

export interface BookV2 {
  variant: string
  fix: string
  near: string
  wdist: string
  verdict: string
  tone: Verdict
}

/** v1→v2 五变体：修哪个缺口，结果怎么变。 */
export const BOOK_V2: BookV2[] = [
  { variant: 'v1′', fix: '旧口径：起始日入场、不复权、未解押按计划日截断（三者都不修）', near: '−29.7bp / t −1.82', wdist: '+65.0bp / t 1.23', verdict: '旧符号', tone: 'warn' },
  { variant: 'v2a', fix: '修 G2+G3：公告日入场 + 未解押展期（不复权）', near: '−19.6 / t −1.15', wdist: '+60.2 / t 0.99', verdict: '没变', tone: 'muted' },
  { variant: 'v2b', fix: '修 G1+G3：平仓线复权 + 未解押展期（起始日入场）', near: '+6.0 / t 0.34', wdist: '−13.7 / t −0.23', verdict: '归零', tone: 'danger' },
  { variant: 'v2', fix: 'G1+G2+G3 全修', near: '+6.0 / t 0.34', wdist: '−13.7 / t −0.23', verdict: '零', tone: 'danger' },
  { variant: 'v2c', fix: '全修 + 补 G4：缺失解押日 notice+365d', near: '+24.7 / t 1.47', wdist: '+117.5 / t 1.95', verdict: '偏正', tone: 'warn' },
]

export interface BookV3 {
  tag: string
  cover: string
  rule: string
  near: string
  wdist: string
  spread: string
  verdict: string
  tone: Verdict
}

/** 公告口径三变体：补覆盖 / 补覆盖+去重 / 仍是零。 */
export const BOOK_V3: BookV3[] = [
  { tag: 'bw', cover: '2019+', rule: '逐行求和（= v2 口径）', near: '+4.79bp / t 0.20', wdist: '−5.70bp / t −0.10', spread: '+119.6bp / t 0.79', verdict: '零', tone: 'danger' },
  { tag: 'bw3', cover: '2005+', rule: '逐行求和', near: '+1.46 / t 0.09', wdist: '−3.14 / t −0.07', spread: '+192.8 / t 1.53', verdict: '零', tone: 'danger' },
  { tag: 'sn3', cover: '2005+', rule: '每合约只取最近一期未解押余额', near: '−0.03 / t −0.00', wdist: '+15.22 / t 0.33', spread: '+235.4 / t 1.90', verdict: '零（原始偏正）', tone: 'warn' },
]

export interface CoverRow {
  date: string
  raw: string
  dedup: string
  snap: string
  matched: string
  tone: Verdict
}

/** 与中登全口径比：合约簿只覆盖重要股东这一层。 */
export const COVERAGE: CoverRow[] = [
  { date: '2020-07', raw: '0.515', dedup: '0.474', snap: '0.437', matched: '748 / 2,388', tone: 'danger' },
  { date: '2021-07', raw: '0.617', dedup: '0.568', snap: '0.550', matched: '786 / 2,362', tone: 'danger' },
  { date: '2022-07', raw: '0.706', dedup: '0.654', snap: '0.644', matched: '827 / 2,367', tone: 'warn' },
  { date: '2023-06', raw: '0.792', dedup: '0.721', snap: '0.724', matched: '855 / 2,355', tone: 'warn' },
  { date: '2024-06', raw: '0.794', dedup: '0.726', snap: '0.730', matched: '912 / 2,315', tone: 'warn' },
  { date: '2025-06', raw: '0.901', dedup: '0.823', snap: '0.795', matched: '963 / 2,294', tone: 'ok' },
  { date: '2026-06', raw: '1.148', dedup: '1.053', snap: '0.986', matched: '1,171 / 2,218', tone: 'ok' },
]

export interface TransferRow {
  data: string
  state: string
  event: string
  sign: string
  ready: string
  tone: Verdict
}

/** E4 迁移：形式可迁移，符号不迁移。 */
export const TRANSFER: TransferRow[] = [
  { data: '大宗交易', state: '60 日大宗占比 / 平均折价', event: '首次 + 深折价 ≤−5% / 溢价 ≥+2%', sign: '深折价 → 已结清 ≈0；溢价 → 正', ready: '2019–2026 全 · 已跑', tone: 'ok' },
  { data: '龙虎榜', state: '在榜次数 / 席位存量', event: '首次上榜 + 卖方集中', sign: '无合约 → 0', ready: '2025-11 起 · 样本太短', tone: 'warn' },
  { data: '十大流通股东', state: '基金持股占比', event: '新进前十大（首次）', sign: '无余额 → 0 / 不稳', ready: '2024-12~2026-09 · 8 季', tone: 'warn' },
  { data: '回购 / 增持', state: '回购余额', event: '首次公告 / 实施首日', sign: '+（强制买方）', ready: '待确认数据', tone: 'warn' },
]

export interface TransferResult {
  test: string
  read: string
  verdict: string
  tone: Verdict
}

/** 大宗首测：形式过、符号半过。 */
export const TRANSFER_RESULT: TransferResult[] = [
  { test: '存量：60 日大宗占比五分位', read: 'Q5−Q1 −48.1bp / t −0.88；受控 −11.8 / t −1.48', verdict: '零 · 形式过关', tone: 'ok' },
  { test: '发起：溢价首次', read: '+384.8 vs other +230.8bp；差 +154.1bp / t 2.13（控制回归欠功效）', verdict: '正号 · 原始过', tone: 'ok' },
  { test: '发起：深折价首次', read: '+271.4 vs other +230.8bp；差 +40.7bp / t 0.77', verdict: '≈0 · 符合「已结清」', tone: 'ok' },
]

export interface TypeSplitRow {
  cell: string
  events: string
  diff: string
  t: string
  verdict: string
  tone: Verdict
}

/** 买卖方类型拆解：符号随类型翻转，且都不显著。 */
export const TYPE_SPLIT: TypeSplitRow[] = [
  { cell: '深折价 · 营业部卖方', events: '7,617', diff: '+41.4bp', t: '0.82', verdict: '不正不负', tone: 'muted' },
  { cell: '深折价 · 机构专用卖方', events: '138', diff: '−97.2bp', t: '−0.45', verdict: '负但不显著', tone: 'warn' },
  { cell: '溢价 · 营业部买方', events: '986', diff: '+124.2bp', t: '1.71', verdict: '正但不显著', tone: 'warn' },
  { cell: '溢价 · 机构专用买方', events: '31', diff: '+1,045.7bp', t: '0.90', verdict: '样本太薄', tone: 'danger' },
]

export interface TypeBigRow {
  spec: string
  inst: string
  broker: string
  n: string
  verdict: string
  tone: Verdict
}

/** 大样本重跑：任意大宗日 + 20 日冷却，2013–2026；inst 807 件。 */
export const TYPE_BIG: TypeBigRow[] = [
  { spec: '大样本 · 原始', inst: '−169.3bp / t −1.22', broker: '+44.6 / t 1.35', n: '807 / 18,750', verdict: '负但不显著', tone: 'warn' },
  { spec: '大样本 · 受控', inst: '−174.4 / t −1.51', broker: '+4.9 / t 0.17', n: '807 / 18,750', verdict: '负但不显著', tone: 'warn' },
  { spec: '大样本 · 同月匹配', inst: '−142.9 / t −0.50', broker: '—', n: '526 匹配', verdict: '归零', tone: 'danger' },
  { spec: '2012–2018', inst: '−343.7 / t −3.14', broker: '—', n: '469', verdict: '显著负', tone: 'ok' },
  { spec: '2019–2026', inst: '−25.3 / t −0.11', broker: '—', n: '338', verdict: '零', tone: 'danger' },
]

export interface MechanismRow {
  bucket: string
  inst: string
  broker: string
  f60: string
  read: string
  tone: Verdict
}

/** 2012–18 机制拆解：质押 / 定增 / 都不是。 */
export const MECHANISM: MechanismRow[] = [
  { bucket: '质押（P）', inst: '0 / 469', broker: '2,790', f60: '—', read: '机构卖方零重叠；质押强平走营业部，不走机构专用', tone: 'danger' },
  { bucket: '定增（S）', inst: '364 / 469', broker: '0', f60: '−302.8bp', read: '全是机构卖方；2012–15 +996 / 2016–18 −585', tone: 'warn' },
  { bucket: '两者都不是', inst: '105 / 469', broker: '2,020', f60: '−279.6bp', read: '负号仍在 ⇒ 不是质押/定增专属', tone: 'danger' },
]

export interface MechInterRow {
  test: string
  read: string
  verdict: string
  tone: Verdict
}

export const MECH_INTER: MechInterRow[] = [
  { test: 'P 主效应（非机构）', read: '−10.7bp / t −0.57', verdict: '不显著', tone: 'danger' },
  { test: 'P × inst 交互', read: '不可识别：inst 807 件里 P=0', verdict: '零重叠', tone: 'danger' },
  { test: '无质押组的 inst 效应', read: '−350.6bp / t −3.42', verdict: '负号更强', tone: 'warn' },
  { test: 'S × inst 交互', read: '共线：S=1 的 364 件全是 inst', verdict: '不可识别', tone: 'danger' },
  { test: 'S 与非 S 的月度重叠', read: '2014–17 全 S；2019+ 全非 S；同月重叠 1 个月', verdict: '与时代共线', tone: 'danger' },
  { test: 'S 桶 2012–15 / 2016–18', read: '+996.3bp（65）/ −585.2bp（299）', verdict: '符号翻转', tone: 'warn' },
]

export interface G4Row {
  horizon: string
  near: string
  wdist: string
  verdict: string
  tone: Verdict
}

/** G4 敏感性：缺失解押日补多久。 */
export const G4_ROWS: G4Row[] = [
  { horizon: '不补（drop）', near: '+6.0 / t 0.34', wdist: '−13.7 / t −0.23', verdict: '零', tone: 'danger' },
  { horizon: '+90 天', near: '+29.0 / t 1.52', wdist: '+126.0 / t 2.02', verdict: '偏正', tone: 'warn' },
  { horizon: '+180 天', near: '+30.6 / t 1.60', wdist: '+120.6 / t 1.91', verdict: '偏正', tone: 'warn' },
  { horizon: '+365 天', near: '+24.7 / t 1.47', wdist: '+117.5 / t 1.95', verdict: '偏正', tone: 'warn' },
  { horizon: '+540 天', near: '+20.6 / t 1.29', wdist: '+119.0 / t 2.02', verdict: '偏正', tone: 'warn' },
  { horizon: '+730 天', near: '+23.0 / t 1.36', wdist: '+115.1 / t 1.93', verdict: '偏正', tone: 'warn' },
]

export interface ContrastRow {
  label: string
  group: 'state' | 'event'
  beta: number
  t: number
  note: string
}

/** 存量零 vs 事件负：同一张图里的两栏（刻度不同，不可跨栏比大小）。 */
export const CONTRAST: ContrastRow[] = [
  { label: '存量 v1 near', group: 'state', beta: -0.1, t: -0.01, note: '受控 · 冻结' },
  { label: '存量 v1 wdist', group: 'state', beta: 40.0, t: 0.77, note: '受控' },
  { label: '存量 v2 near', group: 'state', beta: 6.0, t: 0.34, note: '全修' },
  { label: '存量 v2 wdist', group: 'state', beta: -13.7, t: -0.23, note: '全修' },
  { label: '事件 H11 f20', group: 'event', beta: 206.8, t: 6.33, note: 'near−far' },
  { label: '事件 H11 f60', group: 'event', beta: 525.3, t: 7.47, note: 'near−far' },
]

export interface EventRead {
  tag: string
  title: string
  body: string
  tone: Verdict
}

/** 唯一候选事件：修正后不是强平，是救助。 */
export const EVENT_READ: EventRead[] = [
  {
    tag: '旧读法',
    title: '强平雪崩',
    body: '贴线 → 券商平仓 → 加速下跌。修正后符号相反：贴线组 f60 绝对 +1011bp，near−far +525bp / t 7.47。',
    tone: 'danger',
  },
  {
    tag: '修正后',
    title: '救助期权',
    body: '越贴线，解除方式越可能是救助（纾困/展期/重组），而不是清算；市场提前给救援定价。H12：折算仍无效（t −0.23），路径 t −6.39。',
    tone: 'ok',
  },
  {
    tag: '判别',
    title: '清算 vs 救助',
    body: '若是清算：高质押、老合约应更差；实测反弹更强（−492→−731、−401→−716）。若是救助：解除后效应应归零（C2 不可识别）、波动不应上升（C3 符号互斥）。',
    tone: 'warn',
  },
]

export interface EPred {
  id: string
  claim: string
  test: string
  tone: Verdict
}

export const E_PRED: EPred[] = [
  { id: 'E1', claim: '漂移随事件时间衰减；存量水平保持零', test: '首月 / 第 4 月 ≥ 3；存量受控 |t| < 2', tone: 'warn' },
  { id: 'E2', claim: '控股股东 / 补充质押 > 财务投资者 / 新增融资', test: '交互 |t| ≥ 2 且同号', tone: 'warn' },
  { id: 'E3', claim: '低注意力处漂移更强（反应不足）', test: '三分位单调；低−高 ≥ 100bp', tone: 'warn' },
  { id: 'E5', claim: '约束发起 / 流通市值 → 市场尾部；2015/2018 高分位', test: '对下行波动有增量，控制过去波动', tone: 'warn' },
]

export const KILL4: string[] = [
  '存量的零是测量偏差：G1–G4 已修（v2 仍零）；2005–2018 公告已补、重复披露已去（sn3 仍零）；若 G5–G8 补齐后出现稳定负号 → 二分降级为「测量问题」。',
  '事件效应被前路径吸收：控制 ret120/ret250 与公告前 20 日反转后消失 → 不是披露，是动量。',
  '干净的解押检验（预定到期，非提前解押）显示效应不消失 → 不是约束，是公司质量。',
  '迁移失败：大宗深折价不为负，拆买卖方后符号翻转（营业部 +41 / 机构 −97）；扩样到 807 件仍 t −1.22、2019 后归零 → 质押是特例。',
  '总量不成立：约束发起对市场尾部无增量 → 微观选择函数不能加总。',
  '发送者无关：控制人/理由/条款都不改变漂移 → 「发起」只是一个时间戳，不是信息。',
]

/* ───────────────────────── 卷五 · 审计（2026-11-27） ─────────────────────────
 *
 * 唯一来源：data/.scratch/leverage/pledge_drift_events.parquet 的字段核对
 *   + analysis/_diag_chip_ap_probe.py 的同一批事件（20,540 / 80 月）。
 * 结论：CLOSE_FORWARD_ADJPRICE_TODAY 不是公告日价，是提取日（2026-11）最新价。
 * 因此 drift = 最新价/质押日价 − 1，ratio = 平仓线/最新价，两者都含公告日之后的行情。
 */

export const AUDIT_META = {
  date: '2026-11-27',
  field: 'CLOSE_FORWARD_ADJPRICE_TODAY',
  events: 20540,
  months: 80,
} as const

export interface AuditProof {
  test: string
  read: string
}

/** 四条证据，前两条是量级，后两条是单票对照。 */
export const AUDIT_PROOF: AuditProof[] = [
  { test: 'drift 与「面板最新价 / 质押日价 − 1」的中位绝对差', read: '0.019' },
  { test: 'drift 与「公告日价 / 质押日价 − 1」的中位绝对差', read: '0.269' },
  { test: '旧 drift 与修正 drift 的相关', read: '0.282' },
  { test: '000661 · 三个不同公告日的 CLOSE_PRICE', read: '65.65 / 65.65 / 65.65（2020、2023、2024）' },
  { test: '000661 · 2020-01-10 的 raw_close / CLOSE_PRICE', read: '496.00 / 65.65' },
]

export interface AuditFix {
  test: string
  before: string
  after: string
  note: string
  tone: Verdict
}

/** 修正前后：同一个检验、同一批事件，只换端点。 */
export const AUDIT_FIX: AuditFix[] = [
  { test: 'H11 · near−far · f60', before: '−474.3bp / t −5.41', after: '+525.3bp / t 7.47', note: '月度聚簇', tone: 'ok' },
  { test: 'H11 · 控制后', before: '−130.8bp/月 / t −4.24', after: '+182.3bp/sd / t 5.79', note: '控换手/波动/规模/久期/动量', tone: 'ok' },
  { test: 'H12 · 涨幅（余额）', before: '+72.7bp / t 5.06', after: '−276.0bp/sd / t −6.39', note: '控后 −213.9 / t −5.25', tone: 'danger' },
  { test: 'H12 · 折算（行权价）', before: '+10.0bp / t 0.62', after: '−9.6bp/sd / t −0.23', note: '仍然无效', tone: 'muted' },
  { test: '卷五 · 账龄轴 Q2−Q0', before: '889 → 305bp（衰减）', after: '−401 → −716bp（增强）', note: '<0.5 年 → >2 年', tone: 'danger' },
  { test: '卷五 · 质押比例 Q1→Q5', before: '584 → 660bp（平）', after: '−492 → −731bp（增强）', note: '高质押反弹更强', tone: 'danger' },
  { test: '卷五 · 余额五桶 f60', before: '−301 → +367bp（负→正）', after: '+755 → −72bp（正→负）', note: '深跌 +755 / 大涨 −72', tone: 'danger' },
]

/** 贴线八桶（修正后，f60，bp）：单调递增，不是递减。 */
export const AUDIT_BUCKETS: { bucket: string; n: number; f60: number }[] = [
  { bucket: '≤0.4', n: 4983, f60: -132.9 },
  { bucket: '0.4–0.5', n: 4499, f60: 263.7 },
  { bucket: '0.5–0.6', n: 5475, f60: 272.6 },
  { bucket: '0.6–0.7', n: 2440, f60: 310.6 },
  { bucket: '0.7–0.8', n: 1405, f60: 383.5 },
  { bucket: '0.8–0.9', n: 625, f60: 555.4 },
  { bucket: '0.9–1.0', n: 336, f60: 580.0 },
  { bucket: '>1.0', n: 384, f60: 977.6 },
]

export const AUDIT_SCOPE: { invalid: string; valid: string } = {
  invalid: 'H11、H12、预注册 pledge-drift（评估日 2027-03-31 作废）、卷五首测、卷四 E 节与 D3 图的「事件负」。',
  valid: 'D/D2/D4/D5 的存量簿（用月末面板价）、H9（用质押比例，不用价格）、H1–H8（用日线面板）、大宗 F 线。',
}

export const AUDIT_READ =
  '路径仍然重要、折算仍然无关——H12 的结构活着；但符号反了：越贴线、跌得越深，未来 60 日反弹越强。' +
  '分年 −108 / −185 / −258 / −300 / −310 / −320 / −541bp，七年同号。A 股的约束不是被强平解决的，' +
  '是被救助/展期/重组解决的；市场提前给救援定价。'


/* ───────────────────── 卷五 · 未付账单（原稿，已暂停） ─────────────────────
 *
 * 唯一来源：analysis/_diag_chip_ap_probe.py → data/.scratch/leverage/ap_probe.json
 * 事件 20,540 / 80 个月 / 2020-01 ~ 2026-08（预注册 pledge-drift 的同一批样本）。
 * 口径：f60 = close[t+61]/open[t+1] − 1；exc60 = f60 − 当月均值；
 *      mean = 桶内月均 exc60；spread = 桶内 drift 三分位 Q2−Q0 的月序列均值。
 * 全部为探索性（H11/H12 已看过全样本），不是样本外证据。
 */

export const AP_META = {
  events: 20540,
  /** 回归样本：dropna(f60, ratio_c, drift_c, ret250, ret60, vol20, turn20, size, D) 后 20,280。 */
  eventsValid: 20280,
  months: 80,
  window: '2020-01 ~ 2026-08',
  rangeMean: { age: 100.4, pledge: 44.8, drift: 668.4 },
  /** 桶内 Q2−Q0 的最高值：年龄轴 889（<0.5 年）、能力轴 708（Q2） */
  peakSpread: { age: 889.0, pledge: 708.1 },
} as const

export interface APAxisRow {
  bucket: string
  n: number
  /** 桶内平均 exc60（bp） */
  mean: number
  /** 该桶月均值的 NW t */
  t: number
  /** 桶内 drift 三分位 Q2−Q0（bp）；余额轴不适用 */
  spread?: number
  /** 该桶 spread 月序列的 NW t */
  spreadT?: number
}

/** 位置轴：合约年龄（公告日 − 质押起始日）。可观测，不外生。 */
export const AP_AGE: APAxisRow[] = [
  { bucket: '<0.5 年', n: 5945, mean: -31.2, t: -0.61, spread: 889.0, spreadT: 4.83 },
  { bucket: '0.5–1 年', n: 4086, mean: -72.0, t: -1.45, spread: 642.0, spreadT: 5.36 },
  { bucket: '1–1.5 年', n: 3764, mean: 11.6, t: 0.34, spread: 497.8, spreadT: 4.19 },
  { bucket: '1.5–2 年', n: 1831, mean: 28.4, t: 0.36, spread: 644.3, spreadT: 4.43 },
  { bucket: '>2 年', n: 4914, mean: -28.4, t: -0.88, spread: 304.9, spreadT: 4.08 },
]

/** 能力轴：累计质押比例（已质押/总股本）五分位。 */
export const AP_PLEDGE: APAxisRow[] = [
  { bucket: 'Q1', n: 3970, mean: -26.9, t: -0.50, spread: 584.0, spreadT: 5.33 },
  { bucket: 'Q2', n: 3922, mean: -2.3, t: -0.07, spread: 708.1, spreadT: 5.74 },
  { bucket: 'Q3', n: 3914, mean: 14.4, t: 0.37, spread: 699.6, spreadT: 5.69 },
  { bucket: 'Q4', n: 3922, mean: 17.9, t: 0.38, spread: 652.3, spreadT: 4.72 },
  { bucket: 'Q5', n: 3948, mean: -1.5, t: -0.03, spread: 659.5, spreadT: 4.02 },
]

/** 余额轴：质押以来涨跌（公告日价 / 质押日价 − 1）。桶本身就是余额。 */
export const AP_DRIFT: APAxisRow[] = [
  { bucket: '<−25%', n: 7242, mean: -301.2, t: -7.38 },
  { bucket: '−25~−10%', n: 4173, mean: 8.3, t: 0.20 },
  { bucket: '−10~0%', n: 2302, mean: 21.5, t: 0.31 },
  { bucket: '0~+10%', n: 1940, mean: 221.7, t: 2.73 },
  { bucket: '>+10%', n: 4878, mean: 367.2, t: 6.55 },
]

/** 修正后三轴（drift_c）：账龄与质押比例都是「越深越强」，余额是「越跌越反弹」。 */
export const AP_FIX = {
  age: [
    { bucket: '<0.5 年', n: 5918, spread: -400.9, t: -3.43 },
    { bucket: '0.5–1 年', n: 4074, spread: -596.7, t: -6.14 },
    { bucket: '1–1.5 年', n: 3746, spread: -518.7, t: -3.00 },
    { bucket: '1.5–2 年', n: 1810, spread: -686.9, t: -4.64 },
    { bucket: '>2 年', n: 4732, spread: -716.3, t: -7.68 },
  ],
  pledge: [
    { bucket: 'Q1', n: 3918, spread: -491.5, t: -4.76 },
    { bucket: 'Q2', n: 3874, spread: -481.0, t: -4.58 },
    { bucket: 'Q3', n: 3873, spread: -454.3, t: -3.46 },
    { bucket: 'Q4', n: 3874, spread: -584.2, t: -4.90 },
    { bucket: 'Q5', n: 3907, spread: -731.5, t: -8.73 },
  ],
  balance: [
    { bucket: '<−25%', n: 2373, mean: 755.5, t: 4.42 },
    { bucket: '−25~−10%', n: 2723, mean: 331.7, t: 2.17 },
    { bucket: '−10~0%', n: 4180, mean: 323.5, t: 1.94 },
    { bucket: '0~+10%', n: 3556, mean: 266.5, t: 1.56 },
    { bucket: '>+10%', n: 7448, mean: -72.0, t: -0.38 },
  ],
} as const

/** P1：锚定安慰剂（修正版），事件 11,596 / 80 月。 */
export const P1 = {
  events: 11596,
  months: 80,
  /** 窗口整体往回挪 k 个交易日；slope = f60 对 z(R_k) 的月均系数（bp/sd） */
  shift: [
    { k: 0, slope: -314.3, t: -6.31, ls: -634.0 },
    { k: 5, slope: -280.7, t: -5.65, ls: -530.8 },
    { k: 10, slope: -260.8, t: -5.59, ls: -510.9 },
    { k: 20, slope: -217.8, t: -4.97, ls: -432.2 },
    { k: 40, slope: -185.2, t: -3.98, ls: -331.5 },
    { k: 80, slope: -138.0, t: -2.81, ls: -260.1 },
    { k: 120, slope: -126.6, t: -3.32, ls: -268.5 },
  ],
  high: { slope: -151.5, t: -3.42, ls: -312.6 },
  race: [
    { name: '合约锚 drift_c', bp: -279.8, t: -5.07 },
    { name: '高点锚 rhigh', bp: -60.9, t: -1.48 },
  ],
  resid: { r2: 0.483, bp: -146.9, t: -3.25, ls: -244.5 },
} as const

export interface APLedgerRow {
  dim: string
  asset: string
  liability: string
}

/** 同一只股票的两张表：前四卷在第一张，价差在第二张。 */
export const AP_LEDGER: APLedgerRow[] = [
  { dim: '对象', asset: '谁持有多少（存量）', liability: '谁欠谁多少（余额）' },
  { dim: '计量', asset: '市价，逐日重估', liability: '面值，合约写死' },
  { dim: '信息', asset: '已进入价格', liability: '未进入价格（gap）' },
  { dim: '可观测', asset: '分布、年龄、密度、网络', liability: '条款、盯市日、余额、处置方向' },
  { dim: '符号', asset: '无（测度非负）', liability: '有（欠方必须处置）' },
  { dim: '读数', asset: '十二个状态检验全零', liability: '一条候选 + 一条衰减' },
]

export interface APMtm {
  kind: string
  freq: string
  who: string
  evidence: string
  tone: Verdict
}

/** 一条定律：盯市频率决定 gap 存不存在。 */
export const AP_MTM: APMtm[] = [
  {
    kind: '逐日盯市',
    freq: '每日',
    who: '两融、期货、ETF 申赎、公募净值',
    evidence: 'gap 每天结清，价格里没有未定价的欠款。融资余额占比 = 换手/久期代理（ρ_D −0.442），不预测雪崩。',
    tone: 'muted',
  },
  {
    kind: '事件盯市',
    freq: '平仓线 / 解禁日 / 成交日',
    who: '质押、定增、员工持股、大宗、减持',
    evidence: 'gap 累积，解除方式决定符号。质押贴线 f60 +525bp（t 7.47）；大宗成交即结清，之后无残余（+40.7 / t 0.77）。',
    tone: 'ok',
  },
  {
    kind: '不盯市',
    freq: '从不',
    who: '成本带、筹码峰、套牢盘、平均成本',
    evidence: '没有对手方、没有金额、没有日期——不是账单。快速下穿成本带 +116bp（反弹），20/20 置换更强。',
    tone: 'danger',
  },
]

export interface APAttrRow {
  cls: string
  results: string
  old: string
  neu: string
  verdict: Verdict
}

/** 十六个读数，七种状态，一个恒等式。H9 / H12 与大宗各拆两半。 */
export const AP_ATTR: APAttrRow[] = [
  {
    cls: '分布类',
    results: 'H3 · H4 · H6',
    old: '成本带穿越、年龄熵、记忆相变：三个「记忆」变量，三个零',
    neu: '记忆没有对手方、没有金额、没有日期，不产生余额',
    verdict: 'muted',
  },
  {
    cls: '周转类',
    results: 'H1 · H7 · H8 · H9融资',
    old: '久期、滚续比、放量、融资余额：四个名字，一条换手轴',
    neu: '资产周转速度，且逐日盯市——gap 每天结清',
    verdict: 'muted',
  },
  {
    cls: '身份类',
    results: 'H2 · H5',
    old: '机构/个人伽马、网络中心度：标签与拓扑都不成立',
    neu: '标签不是条款；凸性要从合约读，不从身份读',
    verdict: 'muted',
  },
  {
    cls: '位置类',
    results: 'C1 · C3 · C4 · H9质押 · H12折算',
    old: '活簿距离、贴线波动、90 天内到期、质押水平、折算：五个零',
    neu: '位置可协商（展期/补质押/下调线），所以不进入价格',
    verdict: 'danger',
  },
  {
    cls: '事件类',
    results: 'H11 · H12涨幅 · F1',
    old: '修正后：贴线 f60 多空 +525bp；长腿对同特征基准 +291bp；涨幅 t −6.39；折算仍无效',
    neu: '唯一一笔有余额的账单；符号为正，但只是候选（未 OOS / 成本 / 容量）',
    verdict: 'warn',
  },
  {
    cls: '已支付类',
    results: '大宗 F · C2',
    old: '深折价 +40.7bp / t 0.77，无残余；C2 解押 +763bp，但 5 只/月、内生',
    neu: '成交即结清，价格里已支付；解押是否结清——没测出来，不是零',
    verdict: 'warn',
  },
  {
    cls: '制度类',
    results: '大宗 F2 · F3',
    old: '2012–18 机构专用卖方 −343.7bp；2019+ −25.3bp',
    neu: '清算技术缺失时为负；救助技术普及后归零——符号由解除方式给',
    verdict: 'warn',
  },
]

export interface APPred {
  id: string
  claim: string
  test: string
}

/** 五条前瞻：前四条杀理论，第五条杀「只是一个数据的巧合」。 */
export const AP_PRED: APPred[] = [
  {
    id: 'P1',
    claim: '锚的残差：把 drift_c 对 ret20/60/120/250 + 高点锚正交（已解释 48%），残差应在新样本、新合约类型上继续 |t| ≥ 2。',
    test: '残差 |t| < 2 → 「合约锚定」降级为「通用跌幅的一个测量点」。',
  },
  {
    id: 'P2',
    claim: '解除方式：有救助能力的（国资、纾困可达、重组中、行业龙头）反弹更强；无救助能力的（小市值民企、退市边缘）应更弱甚至为负。',
    test: '交互 |t| ≥ 2 且同号；两组无差异 → 符号不是解除方式给的。',
  },
  {
    id: 'P3',
    claim: '支付归零：解押、强平、大宗成交、解禁之后 20 日，效应应归零或反转；支付前同一批股票应仍为正。',
    test: '支付后仍反弹 → 不是账单，是超跌/公司质量。',
  },
  {
    id: 'P4',
    claim: '制度切换：2018 前（救助技术缺失）应为负或零，2019 后为正；2012–18 大宗负号是旁证。',
    test: '2018 前也是正号 → 与「救助技术」矛盾，需另找机制。',
  },
  {
    id: 'P5',
    claim: '加总：全市场未付账单 / 流通市值 × 救助可达性，应预测市场级反转结构切换。',
    test: '对已实现波动无增量 → 微观不能加总。',
  },
]

/** P2 首测（2026-11-27）：国资 vs 自然人控制。事件层与存量簿层的结论不一致。 */
export const P2_TEST = {
  event: [
    { group: '国资', n: 268, symbols: 43, months: 11, spread: 588.1, t: 2.78 },
    { group: '民企', n: 6308, symbols: 697, months: 80, spread: -633.0, t: -5.81 },
  ],
  delta: { bp: 1269.3, t: 5.27 },
  book: [
    { group: '国资', n: 1882, symbols: 46, q5q1: 447.2, t: 2.10 },
    { group: '民企', n: 34915, symbols: 731, q5q1: 176.1, t: 1.37 },
  ],
  bookDelta: { bp: 271.2, t: 1.05 },
  market: { rows: 102419, zi: -23.2, t: -0.57 },
} as const

/** 控制权变更对照（2026-11-27）：同一批公告，国资 vs 民营；现金 vs 表决权。 */
export const CC = {
  events: 129,
  privateN: 969,
  horizon: '公告次日开盘入场，超额 = 个股 − 全市场等权，月聚簇 NW',
  rows: [
    { group: '国资 · 全部', n: 129, pre60: 8.2, f20: -4.8, t20: -3.47, f40: -8.5, f60: -10.3, t60: -4.88, win60: 18.4, f120: -9.6 },
    { group: '├ 现金受让', n: 45, pre60: 6.3, f20: -5.0, t20: -2.63, f40: -9.2, f60: -12.3, t60: -4.74, win60: 11.6, f120: -11.5 },
    { group: '├ 表决权', n: 58, pre60: 15.5, f20: -6.2, t20: -2.16, f40: -9.0, f60: -8.7, t60: -1.57, win60: 22.8, f120: -6.0 },
    { group: '└ 未标注', n: 26, pre60: 3.6, f20: -1.2, t20: 0.19, f40: -6.2, f60: -10.4, t60: -5.16, win60: 20.0, f120: -9.4 },
    { group: '民营买方', n: 969, pre60: 9.4, f20: 2.1, t20: 1.39, f40: 2.1, f60: 0.1, t60: 0.05, win60: 38.7, f120: 0.7 },
  ],
  diff: {
    cashVoting: { label: '现金 − 表决权', v: -5.0, t: -0.87 },
    cashPrivate: { label: '现金 − 民营', v: -11.1, t: -2.22 },
    votingPrivate: { label: '表决权 − 民营', v: -6.2, t: -1.28 },
  },
  dist: {
    state: { p10: -34.2, med: -12.8, p90: 21.0 },
    private: { p10: -30.7, med: -6.2, p90: 47.0 },
  },
  years: [
    { y: 2018, s: -13.0, p: -5.1 }, { y: 2019, s: -15.2, p: -7.2 }, { y: 2020, s: -13.1, p: -5.6 },
    { y: 2021, s: 7.0, p: -1.1 }, { y: 2022, s: -9.7, p: -0.1 }, { y: 2023, s: -7.3, p: 3.4 },
    { y: 2024, s: -5.0, p: 1.2 }, { y: 2025, s: -16.0, p: 8.6 }, { y: 2026, s: -13.9, p: -2.0 },
  ],
} as const

/** 累计超额路径（−60 至 +120 日，每 6 日采样，起点归零）。 */
export const CC_PATH = {
  x: [-60, -54, -48, -42, -36, -30, -24, -18, -12, -6, 0, 6, 12, 18, 24, 30, 36, 42, 48, 54, 60, 66, 72, 78, 84, 90, 96, 102, 108, 114, 120],
  cash: [0.0, 1.0, -1.2, 0.1, 0.4, 2.2, 1.1, 3.3, 4.5, 4.1, 7.3, 6.8, 5.8, 5.4, 2.0, 0.9, -1.0, 0.8, -1.1, -1.9, -3.8, -2.3, -0.1, 1.7, 2.5, 2.5, 2.9, 2.0, -1.6, -3.0, -3.7],
  voting: [0.0, -0.6, -1.0, -1.2, -0.9, -0.6, -0.8, -0.2, 0.9, 3.8, 12.3, 8.1, 9.7, 6.1, 6.5, 3.1, 3.7, 3.1, 5.5, 3.2, 4.7, 5.7, 4.5, 4.2, 5.6, 6.3, 7.6, 11.2, 4.1, 10.2, 11.1],
  priv: [0.0, 0.4, 0.4, 0.6, 0.9, 1.1, 1.6, 1.9, 2.7, 3.8, 5.1, 9.8, 11.8, 11.5, 11.1, 11.7, 11.6, 11.8, 11.4, 11.5, 9.8, 11.0, 11.5, 11.5, 13.0, 12.4, 12.3, 12.2, 10.6, 10.8, 11.6],
} as const

export const KILL5: string[] = [
  '残差死：正交后残差 |t| < 2 → 合约锚只是通用跌幅的测量点，卷五降级为「超跌反弹」，不能叫筹码理论。',
  '救助不调节：国资/民企、纾困可达/不可达无差异 → 符号不是解除方式给的，正的反弹只是超跌。',
  '支付不归零：解押/成交/解禁后仍反弹 → 不是「未付账单」，是公司质量或超跌反转。',
  '2018 前也是正号：与「救助技术」解释矛盾；若找不到替代机制，制度解释作废。',
  '悬崖但残差死：位移曲线在 k=5 出现锐利落差、残差却不显著 → 是微观结构噪声，不是参照点。',
  '加总失败：全市场未付账单对市场级反转结构无增量 → 本卷只配留在个股层。',
]

export const VERSIONS = [
  {
    date: '2026-11-26',
    volume: '卷一',
    title: '筹码势：把熵换成一个有方向的函数',
    body: '提出势场 S(p)、dens、Θ；一个 OOS 存活的 dens（+132.5bp / t 4.60）。',
  },
  {
    date: '2026-11-26',
    volume: '卷二',
    title: '没有势，只有介质',
    body: '负控否决势场；把 dens 解释为介质。§2 的 +434bp 后来被证明不可复现。',
  },
  {
    date: '2026-11-26',
    volume: '审计',
    title: '位置死了，形状是换手率的影子',
    body: '同一管线复现卷一 t 4.60；11 控联合残差 −4bp；R²(log dens ~ log TO)=0.69。',
  },
  {
    date: '2026-11-26',
    volume: '卷三',
    title: '筹码久期：从成本分布到期限结构',
    body: '把持有人和年龄放回去；建成 5,582 只 × 95 个月末的年龄面板；七条预测未测。',
  },
  {
    date: '2026-11-26',
    volume: '卷三·记录',
    title: '十二条跑完：状态全灭，一个事件活下来',
    body: 'H1 部分通过 / 已分解；H2–H9 全灭；H11/H12 找到质押公告贴线 → 漂移（非券商定价）。',
  },
  {
    date: '2026-11-27',
    volume: '卷四',
    title: '约束的发起：从状态到事件',
    body: '活合约簿 v1 受控 −0.1bp；v2 修复权/入场/展期后 +6.0 / t 0.34，仍零——v1 的弱负号是 G1 复权 artifact。理论收窄为「发起事件」：有符号、有日期、有对手方。',
  },
  {
    date: '2026-11-27',
    volume: '卷五',
    title: '未付账单：从资产端到负债端',
    body: '前四卷都在持仓表上找 alpha，资产按市价计价，零是计价的必然。改读应付表：定价的是「面值−市价」的未付余额，以及它多久被重估一次。首测：位置轴极差 100bp、能力轴 45bp、余额轴 668bp；账龄一年减半。',
  },
  {
    date: '2026-11-27',
    volume: '卷五',
    title: '对照：控制权转移之后，符号反了',
    body: '换一个样本：控制权从民营转给国资，事前 60 日 +8.2%、事后 f60 −10.3%（t −4.88，胜率 18%）；民营接盘 +0.1%。现金 vs 表决权 −5.0pp / t −0.87，不成立。差别在右尾：p90 国资 +21% vs 民营 +47%。',
  },
  {
    date: '2026-11-27',
    volume: '审计',
    title: '唯一活口是未来函数：修正后反转',
    body: 'CLOSE_FORWARD_ADJPRICE_TODAY 是提取日价格，drift/ratio 含公告日之后的行情。H11 −474→+525bp、H12 +72.7→−276.0、账龄 889→305 作废（修正后 −401→−716）。存量簿与大宗 F 线不受影响；预注册 pledge-drift 撤回。',
  },
  {
    date: '2026-11-27',
    volume: '收敛',
    title: '一个候选，其余排除',
    body: 'H13：贴线 near−far +525bp 是多空差；只做多的长腿相对「同日同跌幅三次曲线 + 同波动/换手/规模」基准 +291bp（t 5.28），6/7 年为正，2021 −35bp。线性控制 drift 会把 ratio 压到 +47bp（t 0.83），因为关系是阈值不是线性。证据等级 B：未 OOS、未计成本、未定容量。页面收敛为「一个候选 + 一张体检表 + 一份排除清单」，38 节档案保留；卷六加入后 43 节。',
  },
  {
    date: '2026-11-28',
    volume: '卷六',
    title: '条件，不是水平',
    body: '同一个「贴线」四种实现：簿加权 +141bp / 数量占比 +21bp / 最大合约 −42bp / 最贴合约 −54bp，水平差 195bp。但四种实现的排序全部一致：低压力 > 高压力、少合约 > 多合约（8/8）。结论不是「贴线有效」，是「主效应不可识别，条件可识别」。路径孪生 +141bp / t 2.71，路径只解释 5.9%；配对差中位 +7bp、偏度 3.54——均值是右尾，不是常态。',
  },
]

/* ───────────────────── 卷六：条件，不是水平（2026-11-28） ─────────────────────
 *
 * 这一卷不新增「另一个贴线指标」，而是检验「贴线」这个词本身有没有唯一实现。
 * 同一批股票、同一个对照组（wdist ≤ 0.6 的有簿股）、同一套路径孪生匹配，
 * 只把 near 的定义换四次：簿加权平均 / 数量占比 / 最大合约 / 最贴合约。
 * 水平从 +141 到 −54；两个排序（低压力>高压力、少合约>多合约）在四个口径里 8/8 一致。
 *
 * 数字来源：
 *   analysis/_diag_chip_path_twin.py  → data/.scratch/leverage/path_twin*.json
 *   analysis/_diag_chip_largest_contract.py → largest_contract_panel.parquet
 * 口径：月末面板 2020-01 ~ 2026-07，79 个月；f60 = close[t+61]/open[t+1] − 1；
 *      路径 = 过去 60 日对数收盘的 6 段增量；匹配向量 = 路径 + 规模 + 60 日均换手 + vol20 + ret250。
 *      压力 = 当月有簿股票中 wdist ≥ 0.9 的占比，三分位；合约数 = ncon 的中位数二分。
 */

export interface FourDefRow {
  def: string
  rule: string
  bp: number
  t: number
  low: number
  high: number
  few: number
  many: number
  n: number
}

export const FOUR_DEFS: FourDefRow[] = [
  {
    def: '簿加权平均',
    rule: 'wdist ≥ 0.9：账单按金额加权后贴线',
    bp: 141.0, t: 2.71, low: 189.5, high: 27.6, few: 194.6, many: 67.2, n: 8559,
  },
  {
    def: '数量占比',
    rule: 'near_share ≥ 0.5：过半合约在 [0.8, 1.2]',
    bp: 20.7, t: 0.54, low: 61.6, high: -74.8, few: 32.9, many: -0.1, n: 8945,
  },
  {
    def: '最大合约',
    rule: 'big_ratio ∈ [0.8, 1.2]：tsr 最大那份贴线',
    bp: -41.9, t: -0.76, low: -35.0, high: -73.1, few: -6.1, many: -83.3, n: 7190,
  },
  {
    def: '最贴合约',
    rule: 'max_ratio ∈ [0.8, 1.2]：簿内最贴的一份贴线',
    bp: -54.1, t: -1.10, low: -57.7, high: -95.3, few: -7.1, many: -103.0, n: 12496,
  },
]

/** 四种口径的公共读数：水平散了，排序没散。 */
export const COND = {
  levelMin: -54.1,
  levelMax: 141.0,
  spread: 195.1,
  pairs: 8559,
  pairMin: 7190,
  pairMax: 12496,
  months: 79,
  randBp: 149.8,
  randT: 2.38,
  pathShare: 0.059,
  pairMedian: 7.0,
  pairHit: 50.2,
  pairSkew: 3.54,
  pairSd: 3140,
  stressDelta: -202.0,
  stressT: -1.92,
  match: '同日 · 同规模带 · 60 日路径最近邻 · 8,559 对 / 79 月',
} as const

export interface StressRow {
  def: string
  low: number
  mid: number
  high: number
  delta: number
  t: number
}

/** 系统压力三分位：有簿股票中 wdist ≥ 0.9 的占比。中位 6.2% / 8.3% / 14.3%。 */
export const STRESS: StressRow[] = [
  { def: '簿加权平均', low: 189.5, mid: 201.2, high: 27.6, delta: -202.0, t: -1.92 },
  { def: '数量占比', low: 61.6, mid: 75.4, high: -74.8, delta: -173.8, t: -1.91 },
  { def: '最大合约', low: -35.0, mid: -17.9, high: -73.1, delta: -80.8, t: -0.85 },
  { def: '最贴合约', low: -57.7, mid: -9.6, high: -95.3, delta: -48.0, t: -0.73 },
]

/** 2013–2018 的事件口径：同一套 drift_c / ratio_c，只换事件表。 */
export const PRE2019 = {
  spread: 113.2,
  t: 0.40,
  months: 68,
  near: 165.3,
  far: 52.2,
  postSpread: 525.3,
  postT: 7.47,
  window: '2013-01 ~ 2018-12',
} as const

export const STRESS_META = {
  low: '6.2%',
  mid: '8.3%',
  high: '14.3%',
  hiYears: '2020 / 2022 / 2024',
} as const

export interface ShapeRow {
  metric: string
  near: string
  twin: string
  read: string
}

export const SHAPE6: ShapeRow[] = [
  { metric: '60 日收益 sd', near: '2,577bp', twin: '2,052bp', read: '贴线组更宽' },
  { metric: '左尾 p10', near: '−2,042bp', twin: '−1,758bp', read: '左尾也更差' },
  { metric: '右尾 p90', near: '+2,210bp', twin: '+1,965bp', read: '右尾更厚' },
  { metric: '偏度', near: '7.96', twin: '5.77', read: '正偏' },
  { metric: '配对差 · 中位', near: '+7bp', twin: '—', read: '均值不是常态' },
  { metric: '配对差 · 胜率', near: '50.2%', twin: '—', read: '抛硬币' },
  { metric: '配对差 · 偏度', near: '3.54', twin: '—', read: '右尾驱动' },
]

export const KILL6: string[] = [
  '四种口径的排序反转：任一「低压力 > 高压力」或「少合约 > 多合约」的格子变号。',
  '集中只是规模：在规模/年龄/流动性内匹配后，少合约与多合约的差归零。',
  '压力差只是对照压缩：用独立的救助/解押事件度量后，高压力月的对照也同等反弹。',
  '分布无增量：near 与 twin 的 sd/偏度差在控制后归零，只剩均值噪声。',
  '2027 前瞻：冻结「低压力 × 少合约」格，60 日超额 ≤ 0。',
]

export const KILL6_NOTE = '如果只能留一个数，留 −202（压力差），不留 +141（水平）。'

/* ───────────────────── 页面结构：卷 → 节（导航、折叠、计数的唯一来源） ─────────────────────
 *
 * 为什么结构表放在 data.ts 而不是 ChipTheory.tsx：
 *   上一版的目录是三段手写的按钮数组，§0 审计因此没有任何入口，而测试查不出来——
 *   目录与渲染是两份定义。这一版让「哪些节属于哪一卷」只有一份：
 *   导航、折叠头、节数计数、chip.test.tsx 的覆盖断言都读 VOLUMES。
 *
 * digest 不是新结论，是把该卷分隔段原有的两句话压成一行——
 * 折叠之后读者先读它，再决定要不要展开档案。
 */
export type VolKey = 'audit' | 'v6' | 'v5' | 'v4' | 'v3'

export interface VolSection {
  /** 与 <Section id> 一一对应；测试按它断言这一节真的渲染了。 */
  id: string
  /** 导航与折叠头里的短名。 */
  label: string
}

export interface VolumeDef {
  key: VolKey
  /** 导航里的一格。 */
  tab: string
  /** 折叠头的标题行。 */
  title: string
  /** 一整卷一句话。折叠状态下读者只读这一行。 */
  digest: string
  /** 默认展开：审计与卷五是当前论证；卷四/卷三是档案。 */
  openByDefault: boolean
  sections: VolSection[]
}

export const VOLUMES: VolumeDef[] = [
  {
    key: 'audit',
    tab: '0 审计',
    title: '审计',
    digest: '唯一活口曾来自未来函数：CLOSE 取的端点错成提取日。H11 / H12 / 预注册 / 卷五首测全部作废，修正后符号反转。',
    openByDefault: true,
    sections: [{ id: 'audit', label: '未来函数' }],
  },
  {
    key: 'v5',
    tab: '5 未付账单',
    title: '卷五 · 记录（未付账单）',
    digest: '换一张表：筹码是杠杆持有人的应付账款。贴线溢价仍是候选（长腿 +291bp / t 5.28），但 +525 是多空差、+1011 是 beta；证据等级 B。',
    openByDefault: false,
    sections: [
      { id: 'ledger', label: '5A 换表' },
      { id: 'mtm', label: '5B 定律' },
      { id: 'attr', label: '5C 七类' },
      { id: 'probe', label: '5D 首测' },
      { id: 'pred5', label: '5E 五条' },
      { id: 'control', label: '5F 对照' },
      { id: 'kill5', label: '5G 杀死' },
    ],
  },
  {
    key: 'v6',
    tab: '6 条件',
    title: '卷六 · 理论（条件，不是水平）',
    digest: '单位不是股票，是索取权；统计不是均值，是边界质量；识别不是水平，是条件。四种「贴线」代理按边界质量排序：+141 / +21 / −42 / −54；条件排序 8/8 一致：低压力 > 高压力、少合约 > 多合约。',
    openByDefault: true,
    sections: [
      { id: 'four', label: '6A 四口径' },
      { id: 'grain', label: '6B 质量' },
      { id: 'capacity', label: '6C 容量' },
      { id: 'shape', label: '6D 分布' },
      { id: 'kill6', label: '6E 杀死' },
    ],
  },
  {
    key: 'v4',
    tab: '4 约束的发起',
    title: '卷四 · 记录（约束的发起）',
    digest: '十四节记录：只找到一个「发起」，存量簿全零，「事件负」在审计里被推翻。冻结数字保留，给负债端读法做对照。',
    openByDefault: false,
    sections: [
      { id: 'ontology', label: 'A 换对象' },
      { id: 'selection', label: 'B 符号' },
      { id: 'record', label: 'C 一张表' },
      { id: 'book', label: 'D 存量零' },
      { id: 'book-gap', label: 'D2 口径' },
      { id: 'contrast', label: 'D3 对比' },
      { id: 'g4', label: 'D4 G4' },
      { id: 'book-notice', label: 'D5 公告口径' },
      { id: 'event', label: 'E 发起' },
      { id: 'transfer', label: 'F 迁移' },
      { id: 'type-split', label: 'F2 类型' },
      { id: 'mechanism', label: 'F3 机制' },
      { id: 'next', label: 'G 四条' },
      { id: 'kill4', label: 'H 杀死' },
    ],
  },
  {
    key: 'v3',
    tab: '3 H1–H12',
    title: '卷三 · 记录（H1–H12）',
    digest: '十六节状态变量的检验：零个稳定的收益符号。在新框架里这是预期，不是意外；冻结数字留作退化证据。',
    openByDefault: false,
    sections: [
      { id: 'degeneracy', label: '退化' },
      { id: 'layers', label: '三层' },
      { id: 'duration', label: '久期' },
      { id: 'phase', label: '相图' },
      { id: 'predictions', label: '七条' },
      { id: 'h1', label: 'H1' },
      { id: 'h2', label: 'H2' },
      { id: 'h3', label: 'H3' },
      { id: 'h4', label: 'H4' },
      { id: 'h567', label: 'H5–H7' },
      { id: 'h8', label: 'H8' },
      { id: 'h9', label: 'H9' },
      { id: 'h11', label: 'H11' },
      { id: 'reinterpret', label: '新读法' },
      { id: 'kill', label: '杀死条件' },
      { id: 'versions', label: '版本' },
    ],
  },
]

/** 节 id → 所属卷。锚点跳转时先展开再滚动，靠它。 */
export const SECTION_VOLUME: Record<string, VolKey> = Object.fromEntries(
  VOLUMES.flatMap(v => v.sections.map(s => [s.id, v.key] as const)),
)

/** 页面上应有 43 节；测试与导航计数都读它。 */
export const SECTION_IDS: string[] = VOLUMES.flatMap(v => v.sections.map(s => s.id))
