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
