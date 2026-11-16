/**
 * 博弈链路的口径纯函数 —— 单位与措辞的唯一定义点。
 *
 * 页面结构可以重写，但「同一个数字走错口径差 100 倍」这件事不会变，
 * 所以格式化函数单独放这里，被单测直接引用。当前页面用到三种口径：
 *
 *   scan.chg_pct        百分数   10.08  → +10.08%   （pctRaw）
 *   ledger.median_gap_pct 百分数  -0.9   → -0.9%
 *   账本 bp             基点     41.6   → +41.6bp   （后端已 ×1e4，前端不再换算）
 *   ratio（占比/概率）   百分数   59.8   → 59.8%     （不带 + 号）
 *
 * 分档结论（take / weak / avoid / unbuyable）由后端判，前端只上色。
 * 判据放在一个地方，是因为「高开 3% 该不该接」这种阈值一旦有两个定义点，
 * 页面和回测就会给出不同答案——而两边都不会报错。
 */
import type { GateVerdict, LedgerGap } from '@/lib/api'

// ===== 单位 =====

/** 小数制 → 带符号百分比。用于仍为小数的字段（如 median_gap_pct 之外的比率）。 */
export function pct(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(digits)}%`
}

/** 百分数制 → 带符号百分比。用于 chg_pct / auction_pct 这类「已经是 10.08」的字段。 */
export function pctRaw(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}%`
}

/**
 * 占比 / 概率 → 不带符号的百分比（入参为百分数值，如 59.8）。
 *
 * 单独一个函数是刻意的：样本占比、浮亏比例、胜率都不是「涨跌幅」，
 * 给它们加 + 号会把比例读成涨跌。
 */
export function ratio(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v.toFixed(digits)}%`
}

/** 超额基点（bp）：账本与分档的唯一单位，1bp = 0.01%。 */
export function bp(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)}bp`
}

/** 亿元：榜单净额。后端已除以 1e8，这里只补符号与单位。 */
export function yi(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v >= 0 ? '+' : ''}${v.toFixed(digits)} 亿`
}

/** |t| ≥ 2 才算显著。写成函数是为了让「弱证据」这件事有唯一判据。 */
export function isSignificant(t: number | null | undefined, min = 2): boolean {
  return t != null && Math.abs(t) >= min
}

// ===== 分档结论的措辞 =====

export interface VerdictStyle {
  /** 档位短标签：读者先看这四个字 */
  label: string
  /** 一句话动作。不是预测，是「这个条件下历史上发生过什么」 */
  action: string
  cls: string
  dot: string
}

const VERDICT: Record<GateVerdict, VerdictStyle> = {
  take: {
    label: '可接',
    action: '档内超额为正且显著',
    cls: 'text-bull',
    dot: 'bg-bull',
  },
  weak: {
    label: '方向为正但不显著',
    action: '只能当倾向，不能当规则',
    cls: 'text-warning',
    dot: 'bg-warning',
  },
  avoid: {
    label: '不接',
    action: '档内超额为负',
    cls: 'text-bear',
    dot: 'bg-bear',
  },
  unbuyable: {
    label: '买不进',
    action: '一字板：物理上成交不了，不是策略选择',
    cls: 'text-muted',
    dot: 'bg-muted',
  },
  thin: {
    label: '样本不足',
    action: '只数不够，不给结论',
    cls: 'text-muted',
    dot: 'bg-muted',
  },
}

export function verdictStyle(v: GateVerdict | undefined): VerdictStyle {
  return VERDICT[v ?? 'thin'] ?? VERDICT.thin
}

/**
 * 徽标文字：把「档内显著」和「样本外稳定」两件事合成一个词。
 *
 * 为什么必须合：平开档 overall t=3.46 但前半段只有 1.95，单写「可接」会让读者
 * 把它当规则用；单写「不稳」又丢掉了它确实为正。两个事实一起给才诚实。
 */
export function verdictBadge(v: GateVerdict | undefined, stable?: boolean | null): string {
  const base = verdictStyle(v).label
  if (v === 'take' && stable === false) return `${base}（不稳）`
  return base
}

/** 样本外三态：前后半段都同向显著才算「可当规则用」。 */
export function stability(b: LedgerGap): 'stable' | 'unstable' | 'unknown' {
  if (b.stable === true) return 'stable'
  if (b.stable === false) return 'unstable'
  return 'unknown'
}

export function stabilityText(b: LedgerGap): string {
  switch (stability(b)) {
    case 'stable': return '前后半段都成立'
    case 'unstable': return '后半段不再显著'
    default: return '窗口太短，切不动两段'
  }
}

// ===== 交易时段 =====

export interface PhaseHint {
  label: string
  /** 此刻这个时段「能做什么」——比「现在是几点」有用 */
  todo: string
  /** 0=竞价 1=上午盘 2=下午盘 3=盘后；-1=非交易日 */
  index: number
}

export function phaseHint(phase: string | undefined): PhaseHint {
  switch (phase) {
    case 'preopen':
      return {
        label: '集合竞价 09:15-09:25',
        todo: '09:20 前可撤单，看到的匹配量会被撤走；09:20-09:25 才是真量——下面的分档规则就在这时生效。',
        index: 0,
      }
    case 'morning':
      return {
        label: '上午盘 09:30-11:30',
        todo: '竞价已经定好了你的入场价；此时再追，价差已经被别人赚走。',
        index: 1,
      }
    case 'morning_final':
    case 'pre_afternoon':
      return { label: '午间休市', todo: '上午定版的数字才是可复盘的；下午重开前别用盘中峰值做计划。', index: 1 }
    case 'afternoon':
      return {
        label: '下午盘 13:00-15:00',
        todo: '尾盘 14:30 后的封板/炸板决定次日预期，比上午的高点更有信息。',
        index: 2,
      }
    case 'close_final':
      return {
        label: '已收盘',
        todo: '今天定版了：先看谁上榜、谁在买，再把明早的接盘条件写下来。',
        index: 3,
      }
    default:
      return { label: '非交易时段', todo: '没有实时行情，看到的是上一交易日的定版数据。', index: -1 }
  }
}
