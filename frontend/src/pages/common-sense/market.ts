/**
 * 市场常识 —— 板块与硬线常量的**唯一定义点**。
 *
 * 为什么单独拆出来：板块划分（主板 / 创业板 / 科创板 / 北交所）被三处用到——
 * 退市全景图的市值红线、实时锚点的市值口径、ST 涨跌幅的观察范围。此前这套
 * 判断写在 DelistingLandscape.tsx 里，一旦锚点再抄一份，就会出现「同一个板块
 * 在两处算出两种结论」的分裂（本项目已经吃过一次同一定义写两遍的亏）。
 *
 * 口径提醒（2026-11 实测）：最新交易日的 close 与 raw_close 逐位相等
 * （前复权以最新日为基准），所以用 close 判面值、用 close × total_shares 判市值
 * 在**当天横截面**上是对的；但历史日期上 qfq ≠ raw，历史口径必须回到
 * services/delisting_guard.py（那里用 raw_close + 时点股本）。
 */
import type { MarketSnapshotRow } from '@/lib/api'

/** 板块：决定市值退市红线（5 亿 / 3 亿）与涨跌幅限制（10% / 20% / 30%）。 */
export type Board = 'main' | 'growth' | 'star' | 'beijing'

/** 创业板 300/301/302（302 是 2023 年后启用的创业板号段，旧代码漏了它）。 */
const GROWTH_PREFIX = ['300', '301', '302']
/** 科创板 688/689。 */
const STAR_PREFIX = ['688', '689']

export function boardOf(symbol: string): Board {
  if (symbol.endsWith('.BJ')) return 'beijing'
  const code = symbol.split('.')[0]
  if (GROWTH_PREFIX.some(p => code.startsWith(p))) return 'growth'
  if (STAR_PREFIX.some(p => code.startsWith(p))) return 'star'
  return 'main'
}

/** 面值退市线：收盘价 1 元（连续 20 个交易日）。 */
export const PAR_LINE = 1.0

/** 市值退市线：主板 5 亿，创业板/科创板/北交所 3 亿（2024 退市新规）。 */
export const MCAP_LINE_MAIN = 5e8
export const MCAP_LINE_OTHER = 3e8

export function mcapLineOf(board: Board): number {
  return board === 'main' ? MCAP_LINE_MAIN : MCAP_LINE_OTHER
}

/** 名称里的风险警示标记。ST / *ST 都会命中，退市整理期的「XX退」不命中。 */
export function isSt(name?: string | null): boolean {
  if (!name) return false
  return /^\*?ST/i.test(name)
}

/** 退市整理期名称通常带「退」字（如「华幸退」），与 ST 标记是两回事。 */
export function isDelistingPeriod(name?: string | null): boolean {
  return !!name && name.includes('退')
}

/** 只保留有成交额的行：停牌 / 无数据的行会把占比、中位数算歪。 */
export function tradeableRows(rows: MarketSnapshotRow[]): MarketSnapshotRow[] {
  return rows.filter(r => r.amount != null && r.amount > 0 && Number.isFinite(r.amount))
}

/** 涨跌幅（后端 change_pct 是**小数**，0.05 = 5%），过滤掉空值。 */
export function changeFractions(rows: MarketSnapshotRow[]): number[] {
  return rows
    .map(r => r.change_pct)
    .filter((v): v is number => v != null && Number.isFinite(v))
}

/** 分位数（线性插值），对标 numpy 的 percentile 默认口径。 */
export function quantile(sorted: number[], q: number): number | null {
  const n = sorted.length
  if (n === 0) return null
  if (n === 1) return sorted[0]
  const pos = (n - 1) * q
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  if (lo === hi) return sorted[lo]
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo)
}
