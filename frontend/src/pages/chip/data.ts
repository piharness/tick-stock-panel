/**
 * 筹码久期页的全部数字 —— 唯一来源。
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
  asOf: '2026-11-26',
  sample: '5,582 只 · 95 个月末 · 2005–2026 日线递推',
  universe: '年龄面板 2019-01 ~ 2026-11；持有人 2026Q3',
  window: '久期 2019–2026 · 持有人 2024-12 ~ 2026-09',
  convention: 'S_t(a)=TO_{t−a}·Π(1−TO)；A=500；久期=年龄加权均值；年龄熵=−Σp ln p',
} as const

/** 页头摘要：先回答「所以呢」，再让人往下读。 */
export const SUMMARY = [
  {
    label: '旧页的零',
    value: '均值场极限',
    verdict: 'muted' as Verdict,
    note: '密度=换手率（R² 0.69）；11 控后 −4bp；匹配后 −26bp（NW −0.84）。',
  },
  {
    label: '新对象',
    value: 'S(p, a)',
    verdict: 'ok' as Verdict,
    note: '对象已建成：5,582 只 × 95 个月末；中位久期 97.5d→38.5d。理论未验证。',
  },
  {
    label: '四条已跑',
    value: '一通过 · 三失败',
    verdict: 'danger' as Verdict,
    note: 'H1 拆成 D/T 两条风险主效应；H2 合成失败；H3 雪崩格反向、密集区不做事；H4 是冲击深度混淆。三条未测。',
  },
]

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
  { id: 'H5', claim: '网络中心度 → 尾部传染', test: '下行相关每 +1sd ≥+0.10，平均相关不变', status: '未测' },
  { id: 'H6', claim: '记忆相变，不是线性衰减', test: '阈值模型 BIC 优于线性 ≥10，OOS 分段同号', status: '未测' },
  { id: 'H7', claim: 'Θ 是滚续成本 → 脆弱性', test: '与下行偏度负相关、t≥2；与 fwd20 无关', status: '未测' },
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
]
