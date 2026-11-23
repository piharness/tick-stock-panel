/**
 * 投资手册的「活读数」适配层：接口字段 → 页面句子。
 *
 * 为什么需要这一层：
 *   data.ts 里的数字其实有两类 ——
 *     1) 实验结论：有 N、有窗口、只在重跑实验时变（位置梯度、退出表、组合阶梯）；
 *     2) 每日读数：扫描快照、账户、现金腿、V1 进度、广度。
 *   第 2 类过去是手抄进 data.ts 与正文的，于是同一个真相有了第二个定义点。已经发生过的漂移：
 *     · 「2026-11-20 广度 6」——当日快照 in_band=8、signal=6；一个「广度」同时冒充两个口径；
 *     · 「2026-11-23 的候选方案」——现金腿的数据日是 2026-11-20，11-23 只是生成时间；
 *     · 「V1 账本 0 / 3」——进度在 payload.ledger.v1 里，手抄两处。
 *   这一层是第 2 类唯一的定义点：只做映射，不重算任何指标（判定口径仍在 analysis/）。
 *
 * 约定：
 *   - 输入缺失时返回 null，由调用方隐藏整句；绝不回退到一个「上次的日期」。
 *   - 数字不在这里格式化成分数/百分比之外的东西，避免出现第三套取整规则。
 */
import type { EtfWatchAccount, EtfWatchCashLeg, EtfWatchPayload } from '@/lib/api'

/** 数据日之后还有几个交易日没被扫描到；0 表示行情已到最近一个交易日。 */
export function lagSessions(payload: EtfWatchPayload | null | undefined): number {
  return payload?.data?.lag_sessions ?? 0
}

export interface CoverageGap {
  /** 本地 ETF 日线覆盖到的最后一天。 */
  storeLast: string
  /** 扫描快照的数据日。 */
  asof: string
  text: string
}

/**
 * 页面是否落后于本地行情。
 *
 * 只回答「链路没跑」，不回答「今天是不是交易日」—— 后者只有交易日历知道
 * （见 analysis/handbook_audit.py）。两者的分工不能混：
 *   - 这里：分区目录已有 storeLast，扫描却停在 asof ⇒ 今天的脚本没跑；
 *   - 体检：日历说今天应收盘 ⇒ 该有而完全没有，连分区都缺。
 * 2026-11-23 19:19 的事故属于前者：分区已到 11-23，扫描停在 11-20，
 * 页面却没有任何地方说得出来 —— 日期控件里自然也没有今天。
 */
export function coverageGap(payload: EtfWatchPayload | null | undefined): CoverageGap | null {
  const storeLast = payload?.meta?.store_last_session
  const asof = payload?.meta?.asof
  if (!storeLast || !asof || storeLast <= asof) return null
  return { storeLast, asof, text: `本地行情已到 ${storeLast}，扫描只到 ${asof}` }
}

/**
 * 两个产出物的数据日不一致时给出可读差异，相同则 null。
 * 存在的理由：「账户指令」与「扫描」由两个脚本产出，一个先跑一个后跑是常态 ——
 * 页面必须说出「这两块不是同一天」，而不是并排摆着让人以为是同一天。
 */
export function asOfDrift(label: string, asof?: string | null, base?: string | null): string | null {
  if (!asof || !base || asof === base) return null
  return `${label} ${asof} ≠ 扫描 ${base}`
}

export interface V1Reading {
  /** 已结算簇 / 需要的簇，形如 '0 / 3'。 */
  progress: string
  settled: number
  required: number
  state: EtfWatchPayload['ledger']['v1']['state']
  asof: string
}

/**
 * V1 预注册判据的进度。
 *
 * 只数「已结算」的簇：signal_clusters 是触发数，不是进度 ——
 * 把触发读成进度，会让「刚开了第一枪」看起来像「快验收了」。
 */
export function v1Reading(payload: EtfWatchPayload | null | undefined): V1Reading | null {
  const v1 = payload?.ledger?.v1
  if (!payload || !v1) return null
  const settled = v1.settled_clusters ?? 0
  const required = v1.required ?? 3
  // progress 是账本自带的展示字段（'0/3'）；只在缺失时按两个计数拼。
  const progress = (v1.progress ?? `${settled}/${required}`).replace(/\s*\/\s*/, ' / ')
  return { progress, settled, required, state: v1.state, asof: payload.meta.asof }
}

export interface BreadthReading {
  asof: string
  /** 当日区间内行业数（位置到了）。 */
  inBand: number
  /** 四条全过数（可下注）。 */
  signal: number
  /** 当日新记的事件簇数。 */
  clustersToday: number
  text: string
  tone: 'ok' | 'muted'
}

/**
 * 当日广度。
 *
 * 两个数必须分开写：区间内 ≠ 四条全过。合成一句「广度 N」时，
 * 写的人取一个口径、读的人以为是另一个 —— 历史注释里那次 6 vs 8 就是这么来的。
 */
export function breadthReading(payload: EtfWatchPayload | null | undefined): BreadthReading | null {
  if (!payload) return null
  const { asof } = payload.meta
  const { in_band, signal, clusters_today } = payload.counts
  const cluster = clusters_today > 0 ? `，记 ${clusters_today} 个事件簇` : '，未成簇'
  return {
    asof,
    inBand: in_band,
    signal,
    clustersToday: clusters_today,
    tone: signal > 0 ? 'ok' : 'muted',
    text: `${asof}：区间内 ${in_band} 个行业，四条全过 ${signal} 个${cluster}`,
  }
}

export interface AccountReading {
  asof: string
  adoption: string
  init: number
  equity: number
  /** 初始资金（万元），用于「60 万」这类正文。 */
  initWan: number
}

export function accountReading(account: EtfWatchAccount | null | undefined): AccountReading | null {
  if (!account) return null
  return {
    asof: account.asof,
    adoption: account.meta.adoption,
    init: account.meta.init,
    equity: account.state.equity,
    initWan: Math.round(account.meta.init / 10_000),
  }
}

export interface CashLegReading {
  /** 数据日 —— 不是生成时间。 */
  asof: string
  generatedAt: string
  policy: string
  bondW: number
}

export function cashLegReading(leg: EtfWatchCashLeg | null | undefined): CashLegReading | null {
  if (!leg) return null
  return {
    asof: leg.meta.asof,
    generatedAt: leg.meta.generated_at,
    policy: leg.meta.policy,
    bondW: leg.bond_w,
  }
}
