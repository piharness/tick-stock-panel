/**
 * 跨年妖候选池 —— 口径标签。
 *
 * 数据来自后端 `focus.cross_year`（实时计算，非静态快照），这里只放 4 门槛 + 1 排序 + 1 徽标的静态文案
 * 与负向说明。定位：观察名单，不是买入名单 —— 项目 20 年全样本（docs/yaogu-census.md）：
 * 首板后开盘买入 20 日超额中位 −1.85%（t −15.94）；见顶确认时买入 −26.9%/20 日。
 * 这是情绪温度计，不是选股筛子。
 */

/**
 * 8 条画像 → 4 个硬门槛 + 1 个排序键 + 1 个徽标（阈值以后端 criteria 为准）。
 *
 * 这三类不能混着读：门槛决定「能不能进」，排序决定「先看谁」，
 * 徽标只影响「名字有没有记忆点」——它不参与筛选，也不参与排序。
 */
export type CrossYearChipKind = 'gate' | 'rank' | 'badge'

export const CROSS_YEAR_SCREEN: { label: string; hint: string; kind: CrossYearChipKind }[] = [
  { label: '启动价低', hint: '现价 3~15 元', kind: 'gate' },
  { label: '流通市值小', hint: '流通市值 10~60 亿', kind: 'gate' },
  { label: '已启动', hint: '近 20 日有涨停', kind: 'gate' },
  { label: '有效性', hint: '非 ST / 北交所 / 次新', kind: 'gate' },
  { label: '位置干净', hint: '排序键：净增 = 20 日 − 60 日涨幅', kind: 'rank' },
  { label: '名字记忆点', hint: '徽标，不参与筛选', kind: 'badge' },
]

/**
 * 前向检验（2026-11-28，`analysis/_diag_cross_year_pool.py`）：
 * 2016-01 ~ 2026-08，127 个月末重放；t+1 开盘买，t+21/t+61 收盘卖；
 * 超额 = 组均值 − 同日全市场均值；月频 NW(6)。
 *
 * 结论：候选池整体跑输市场，第二梯队强负；同门槛「未启动」对照为正。
 * PIT 未解决（当前股本/名称/上市日），绝对水平有幸存者偏差，相对差更可信。
 */
export const CROSS_YEAR_STATS = {
  window: '2016-01 ~ 2026-08',
  months: 127,
  all: { ex20: -65.7, t20: -2.33, ex60: -140.9 },
  top15: { ex20: -86.5, t20: -1.42, ex60: -226.9 },
  tier1: { ex20: -33.3, t20: -1.07, ex60: -78.1 },
  tier2: { ex20: -320.6, t20: -4.12, ex60: -512.5 },
  ctrl: { ex20: 66.5, t20: 3.39, ex60: 144.4 },
} as const
