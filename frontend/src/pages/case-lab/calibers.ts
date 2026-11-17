/**
 * 操盘档案的口径与文案 —— 单位换算、涨跌配色、枚举展示的唯一来源。
 *
 * 后端只保证枚举值是契约（`Literal` 校验），中文名属于展示层，只在
 * 这里定义一份。组件里不许再写 `status === 'penalized' ? '已处罚' : ...`
 * ——那会让「新增一个状态」变成四处漏改。
 *
 * 单位纪律（本页同时出现四种，禁止「小于 1 就乘 100」）：
 *   · `*_bp`      基点      63.4 → +63.4bp（后端已 ×1e4）
 *   · `*_pct`     百分数    -6.69 → -6.69%
 *   · `*_yi`      亿元      3.83 → 3.83 亿（后端已 ÷1e8）
 *   · `t`/`ratio` 纯数     无单位
 */

export type ActorType = 'hot_money' | 'cap_mgmt' | 'dealer' | 'institution' | 'unknown'
export type CaseStatus = 'penalized' | 'investigated' | 'alleged' | 'trace_only'
export type EvidenceKind = 'penalty' | 'inquiry' | 'filing' | 'leak' | 'media' | 'research' | 'other'
export type Reliability = 'official' | 'media' | 'hearsay' | 'inferred'

export const ACTOR_META: Record<ActorType, { label: string; hint: string }> = {
  hot_money: { label: '游资 / 短线资金', hint: '以席位可追踪的短线资金为主，持仓周期常在数日' },
  cap_mgmt: { label: '市值管理团队', hint: '与上市公司/大股东配合，目标通常是减持或质押价格' },
  dealer: { label: '坐庄 / 控盘方', hint: '长期收集筹码并影响价格，常见对倒、锁仓、画线' },
  institution: { label: '机构', hint: '公募/私募/保险等，受申赎与考核约束' },
  unknown: { label: '未知', hint: '证据不足以归类' },
}

export const STATUS_META: Record<CaseStatus, { label: string; cls: string }> = {
  penalized: { label: '已处罚', cls: 'bg-accent/10 text-accent border-accent/30' },
  investigated: { label: '立案 / 调查中', cls: 'bg-warning/10 text-warning border-warning/30' },
  alleged: { label: '有爆料未证实', cls: 'bg-elevated text-secondary border-border' },
  trace_only: { label: '仅痕迹', cls: 'bg-elevated text-muted border-border' },
}

export const KIND_META: Record<EvidenceKind, { label: string; weight: string }> = {
  penalty: { label: '处罚决定书', weight: '官方定论' },
  inquiry: { label: '问询 / 监管函', weight: '官方关注' },
  filing: { label: '立案告知', weight: '官方程序' },
  leak: { label: '爆料', weight: '不可核' },
  media: { label: '媒体报道', weight: '二手' },
  research: { label: '研报 / 二手分析', weight: '二手' },
  other: { label: '其他', weight: '—' },
}

export const RELIABILITY_META: Record<Reliability, { label: string; cls: string; hint: string }> = {
  official: {
    label: '官方',
    cls: 'bg-accent/10 text-accent border-accent/30',
    hint: '监管文书：事实权威，但通常滞后 1–3 年',
  },
  media: {
    label: '媒体',
    cls: 'bg-elevated text-secondary border-border',
    hint: '有编辑责任，仍可能引用单一信源',
  },
  hearsay: {
    label: '传闻',
    cls: 'bg-warning/10 text-warning border-warning/30',
    hint: '不可核。也可能本身就是出货工具',
  },
  inferred: {
    label: '本人推断',
    cls: 'bg-elevated text-muted border-border',
    hint: '由痕迹反推，不是外部事实',
  },
}

export const FAMILY_META: Record<string, { label: string; color: string }> = {
  distribution: { label: '派发', color: '#d97706' },
  lockup: { label: '锁仓', color: '#7c3aed' },
  wash: { label: '对倒', color: '#0891b2' },
  pump_dump: { label: '拉高出货', color: '#dc2626' },
}

/** 基点：后端已 ×1e4，这里只补符号与单位 */
export function bp(v: number | null | undefined, digits = 1): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}bp`
}

/** 百分数原值（后端已是 6.69 表示 6.69%） */
export function pctSigned(v: number | null | undefined, digits = 2): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${v > 0 ? '+' : ''}${v.toFixed(digits)}%`
}

/** 中国习惯：涨红跌绿。0 与缺失一律中性，不染绿。 */
export function toneCls(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'text-secondary'
  return v > 0 ? 'text-bull' : 'text-bear'
}

/** 比例（0–1）→ 百分数文本 */
export function ratioPct(v: number | null | undefined, digits = 0): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

/** t 值只作参考：样本在时间上重叠，未做 Newey-West 调整 */
export function tText(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v)) return '—'
  return `t=${v.toFixed(2)}`
}

/**
 * 北京日历的今天。
 *
 * 不能用 `new Date().toISOString().slice(0,10)`：UTC 与北京差 8 小时，
 * 北京 00:00–08:00 会返回**昨天**。本项目的「今天」只有一个定义。
 */
export function cnToday(): string {
  const now = new Date()
  const cn = new Date(now.getTime() + 8 * 3600 * 1000)
  return cn.toISOString().slice(0, 10)
}

/**
 * 纯日期加减：用 UTC 分量算，不碰本地时区分量。
 * `new Date('2026-11-16T00:00:00+08:00')` 在 UTC 是 11-15 16:00，
 * 再 `setDate(+1)` 取 ISO 日期会少一天。
 */
export function shiftDate(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

/**
 * 6 位代码补交易所后缀。只做**格式归一**，不做校验：
 * 补错后缀时后端返回 not_in_universe，页面如实说「本地没有这只票」，
 * 而不是猜一个近似标的。
 */
export function normalizeSymbol(raw: string): string {
  const s = raw.trim().toUpperCase()
  if (!s) return ''
  if (s.includes('.')) return s
  if (!/^\d{6}$/.test(s)) return s
  if (/^(60|68|90)/.test(s)) return `${s}.SH`
  if (/^(00|30|20)/.test(s)) return `${s}.SZ`
  if (/^[48]/.test(s)) return `${s}.BJ`
  return s
}

/** 显著性提示 —— 明确写「参考」而不是「显著」 */
export function tHint(v: number | null | undefined): { label: string; cls: string } {
  if (v == null) return { label: '样本不足', cls: 'text-muted' }
  const a = Math.abs(v)
  if (a >= 3) return { label: '参考性强', cls: 'text-accent' }
  if (a >= 2) return { label: '参考', cls: 'text-secondary' }
  return { label: '噪声区内', cls: 'text-muted' }
}
