/**
 * 个股分析页的时间轴口径 —— 区间怎么算、缺口怎么判、区间统计用什么分子分母。
 *
 * 这个文件只放纯函数，因为这里的每一条都曾经（或很容易）被写错：
 *   - 预设「1Y」如果按 365 个自然日算，闰年会少一天；按「回到去年同月同日」才是
 *     人眼期待的那个一年。
 *   - 「本地没数据」和「这只票这段时间没数据」是两件事：前者是覆盖边界（可以
 *     去补），后者是停牌/上市晚（补也没用）。混成一句「无数据」会让人白点一次。
 *   - 区间涨跌幅的分子分母必须同口径（都是收盘），不能用「期末收盘 / 期初开盘」，
 *     那会把一个隔夜跳空算成区间收益。
 */
import { cnToday } from '@/lib/kline'

export type RangePreset = '1M' | '3M' | '6M' | '1Y' | '2Y' | 'ALL' | 'CUSTOM'

export interface DateRange {
  start: string
  end: string
}

/** 预设按钮的展示顺序与文案（单一数据源，页面不另写一份）。 */
export const RANGE_PRESETS: { key: RangePreset; label: string; months?: number }[] = [
  { key: '1M', label: '1月', months: 1 },
  { key: '3M', label: '3月', months: 3 },
  { key: '6M', label: '6月', months: 6 },
  { key: '1Y', label: '1年', months: 12 },
  { key: '2Y', label: '2年', months: 24 },
  { key: 'ALL', label: '全区间' },
]

/** 'YYYY-MM-DD' → 三个整数；格式不对返回 null（不抛，调用方多为渲染路径）。 */
function parts(iso: string): [number, number, number] | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? '')
  if (!m) return null
  const y = Number(m[1]); const mo = Number(m[2]); const d = Number(m[3])
  if (!y || mo < 1 || mo > 12 || d < 1 || d > 31) return null
  return [y, mo, d]
}

/** 按本地日历加减月数：天超出当月长度时钳到月末（1月31日 → 2月28/29日）。 */
export function shiftMonths(iso: string, months: number): string {
  const p = parts(iso)
  if (!p) return iso
  const [y, mo, d] = p
  const base = new Date(Date.UTC(y, mo - 1, 1))
  base.setUTCMonth(base.getUTCMonth() + months)
  const lastDay = new Date(Date.UTC(base.getUTCFullYear(), base.getUTCMonth() + 1, 0)).getUTCDate()
  const day = Math.min(d, lastDay)
  const out = new Date(Date.UTC(base.getUTCFullYear(), base.getUTCMonth(), day))
  return out.toISOString().slice(0, 10)
}

/** 预设 → 区间右端是北京今天，左端按自然月回溯。ALL 返回 null（由调用方决定起点）。 */
export function presetRange(preset: RangePreset, today = cnToday()): DateRange | null {
  const hit = RANGE_PRESETS.find(p => p.key === preset)
  if (!hit || !hit.months) return null
  return { start: shiftMonths(today, -hit.months), end: today }
}

/** 两个 ISO 日之间的自然日差（b - a）。任一格式不对返回 null。 */
export function daysBetween(a: string, b: string): number | null {
  const pa = parts(a); const pb = parts(b)
  if (!pa || !pb) return null
  const ta = Date.UTC(pa[0], pa[1] - 1, pa[2])
  const tb = Date.UTC(pb[0], pb[1] - 1, pb[2])
  return Math.round((tb - ta) / 86_400_000)
}

export function isValidIso(iso: string): boolean {
  const p = parts(iso)
  if (!p) return false
  const [y, mo, d] = p
  return new Date(Date.UTC(y, mo - 1, d)).getUTCDate() === d
}

/** 起止日纠正：起点晚于终点时交换；非法值回落到 fallback。 */
export function normalizeRange(input: DateRange, fallback: DateRange): DateRange {
  const start = isValidIso(input.start) ? input.start : fallback.start
  const end = isValidIso(input.end) ? input.end : fallback.end
  return start <= end ? { start, end } : { start: end, end: start }
}

export type GapKind = 'none' | 'table' | 'symbol'

export interface CoverageGap {
  kind: GapKind
  /** 缺口自然日数（请求起点 → 首根 K），无缺口为 0 */
  days: number
}

/**
 * 请求起点早于第一根 K 时，到底是「本地没铺到那一天」还是「这只票那天没数据」。
 *
 * 判据只有一条：请求起点 < 本地日K表最早日期 → table（可以去补历史）；
 * 否则 → symbol（停牌 / 上市晚 / 退市，补历史也没用）。
 * 不能用「首根 K 比起点晚几天」当阈值：那是个拍出来的魔数，
 * 停牌复牌与覆盖边界的数字长得一模一样。
 */
export function coverageGap(
  requestedStart: string,
  firstRowDate: string | null,
  tableEarliest: string | null | undefined,
): CoverageGap {
  if (!firstRowDate) return { kind: 'none', days: 0 }
  if (firstRowDate <= requestedStart) return { kind: 'none', days: 0 }
  const days = daysBetween(requestedStart, firstRowDate) ?? 0
  if (tableEarliest && requestedStart < tableEarliest) return { kind: 'table', days }
  return { kind: 'symbol', days }
}

export interface RangeStats {
  /** 区间内交易日（K 线根数） */
  n: number
  /** 收盘→收盘 涨跌幅（%） */
  pct: number | null
  /** 收盘序列最大回撤（%，正数表示回撤幅度） */
  maxDrawdown: number | null
  high: number | null
  highDate: string | null
  low: number | null
  lowDate: string | null
  /** 期末收盘距区间最高收盘（%） */
  fromHigh: number | null
  /** 是否有成交额数据 */
  hasAmount: boolean
  /** 区间成交额合计（元）；无 amount 列为 null */
  amount: number | null
}

/**
 * 可见区间的统计。全部用收盘价序列：区间涨跌幅、回撤、距高点三者必须同源，
 * 否则「涨了 5% 却离高点 -20%」这种自相矛盾的组合会同时出现在一屏里。
 */
export function rangeStats(rows: { date: string; close: number; high?: number; low?: number; amount?: number }[]): RangeStats {
  const empty: RangeStats = {
    n: 0, pct: null, maxDrawdown: null, high: null, highDate: null,
    low: null, lowDate: null, fromHigh: null, hasAmount: false, amount: null,
  }
  const valid = rows.filter(r => Number.isFinite(r.close) && r.close > 0)
  if (valid.length === 0) return empty

  const first = valid[0]
  const last = valid[valid.length - 1]
  const pct = first.close > 0 ? (last.close / first.close - 1) * 100 : null

  let peak = valid[0].close
  let maxDd = 0
  let high = valid[0].high ?? valid[0].close
  let highDate = valid[0].date
  let low = valid[0].low ?? valid[0].close
  let lowDate = valid[0].date
  for (const r of valid) {
    if (r.close > peak) { peak = r.close }
    const dd = peak > 0 ? (1 - r.close / peak) * 100 : 0
    if (dd > maxDd) maxDd = dd
    const h = r.high ?? r.close
    const l = r.low ?? r.close
    if (h > high) { high = h; highDate = r.date }
    if (l < low) { low = l; lowDate = r.date }
  }
  const fromHigh = peak > 0 ? (last.close / peak - 1) * 100 : null

  const hasAmount = valid.every(r => typeof r.amount === 'number' && Number.isFinite(r.amount))
  return {
    n: valid.length,
    pct,
    maxDrawdown: valid.length > 1 ? maxDd : null,
    high,
    highDate,
    low,
    lowDate,
    fromHigh,
    hasAmount,
    amount: hasAmount ? valid.reduce((s, r) => s + (r.amount as number), 0) : null,
  }
}

/**
 * 把区间端点裁到现有 K 线日期上：返回 [不晚于 start 的最近一根, 不早于 end 的最近一根]。
 * 用于在图上标「简称变更」「事件区间」——观测日期可能落在图表数据之外。
 */
export function clampToDates(dates: string[], start: string, end: string): [string, string] | null {
  if (dates.length === 0) return null
  let lo: string | null = null
  let hi: string | null = null
  for (const d of dates) {
    if (d <= start) lo = d
    if (hi === null && d >= end) hi = d
  }
  const a = lo ?? dates[0]
  const b = hi ?? dates[dates.length - 1]
  if (a > b) return null
  return [a, b]
}

/** 区间参数的 localStorage 键（按标的记忆；换股票回到默认区间）。 */
export function rangeStorageKey(symbol: string): string {
  return `stock-analysis:range:${symbol}`
}

export interface StoredRange {
  preset: RangePreset
  start: string
  end: string
}

export function loadStoredRange(symbol: string): StoredRange | null {
  try {
    const raw = localStorage.getItem(rangeStorageKey(symbol))
    if (!raw) return null
    const v = JSON.parse(raw) as StoredRange
    if (v && typeof v.start === 'string' && typeof v.end === 'string' && isValidIso(v.start) && isValidIso(v.end)) {
      return v
    }
  } catch { /* ignore */ }
  return null
}

export function saveStoredRange(symbol: string, value: StoredRange | null): void {
  try {
    const k = rangeStorageKey(symbol)
    if (value) localStorage.setItem(k, JSON.stringify(value))
    else localStorage.removeItem(k)
  } catch { /* ignore */ }
}


/** 图表视窗（百分比）—— 平移与缩放的纯函数，边界都夹在这里，组件只负责调用。 */
export interface ViewWindow { start: number; end: number }

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))

/** 平移：保持窗口宽度，撞到 0/100 就贴边（不循环，免得滑到"未来"）。 */
export function panWindow(w: ViewWindow, deltaPct: number): ViewWindow {
  const width = clamp(w.end - w.start, 1, 100)
  const start = clamp(w.start + deltaPct * width, 0, 100 - width)
  return { start, end: start + width }
}

/** 缩放：以窗口中心为锚，宽度夹在 [minWidth, 100]。 */
export function zoomWindow(w: ViewWindow, factor: number, minWidth = 2): ViewWindow {
  const width = clamp((w.end - w.start) * factor, minWidth, 100)
  const center = (w.start + w.end) / 2
  const start = clamp(center - width / 2, 0, 100 - width)
  return { start, end: start + width }
}

/** 百分比视窗 → 行下标区间（两端各向外取整，保证屏幕上看得到的每一根都算进去）。 */
export function windowToIndex(w: ViewWindow, n: number): [number, number] {
  if (n <= 0) return [0, 0]
  const i0 = clamp(Math.floor((w.start / 100) * (n - 1)), 0, n - 1)
  const i1 = clamp(Math.ceil((w.end / 100) * (n - 1)), 0, n - 1)
  return [Math.min(i0, i1), Math.max(i0, i1)]
}
