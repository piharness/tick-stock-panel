/**
 * 实时锚点 —— 把每条「常识」挂到当天的真实横截面上。
 *
 * 为什么需要这一层：静态知识库的通病是「结论正确但无法验证」，读者只能选择信或不信。
 * 锚点的作用相反——它用**今天这一天的全市场数据**算出一个小数字，让读者自己判断
 * 那条常识是否仍然成立。例如「ST 涨跌幅 5%」这条老经验，只要今天有一只 ST 波动
 * 超过 5%，它就当场被证伪，不需要任何人说服你。
 *
 * 三条纪律（与本项目数据口径同源）：
 *   1. **报数必带口径**：每个锚点都要给出 N、用到的列、以及 as_of 日期。没有口径的数字
 *      不许上屏——它一定会被当成别的口径来读。
 *   2. **单位只认小数**：后端 change_pct 是小数（0.05 = 5%），本文件内部一律用小数，
 *      只在渲染时 ×100。曾出现过「拿 5 当 5%」把阈值放大 100 倍的事故。
 *      ⚠ 同一份快照里 **turnover_rate 是百分数**（1.73 表示 1.73%，见
 *      indicators/pipeline.py 的「换手率(%) = volume*10000/float_shares」），
 *      与 change_pct 的小数口径相反。两个字段挨在一起，混用不会有任何报错，
 *      只会得出一个「看起来合理」的错误数字。
 *   3. **算不出来就返回 null**，不返回 0。0 是一个会被读成「没有风险」的数字，
 *      而「没有数据」和「风险为零」是两件事。
 *
 * 数据来源只有一个：`/api/screener/market-snapshot`（全市场当日横截面，前端聚合，
 * 不改后端）。不新增接口，不落盘，不缓存超过 TanStack Query 的 staleTime。
 */
import type { MarketSnapshotRow } from '@/lib/api'
import { boardOf, isSt, mcapLineOf, quantile } from './market'

export type AnchorId =
  | 'breadth'
  | 'friction'
  | 'st-move'
  | 'par-line'
  | 'mcap-line'
  | 'illiquid'
  | 'turnover-tail'
  | 'limit-up'
  | 'concentration'

export interface AnchorCtx {
  rows: MarketSnapshotRow[]
  /** 快照日期（后端 as_of），用于口径标注。 */
  asOf: string | null
}

export interface AnchorResult {
  id: AnchorId
  /** 短名，如「今日摩擦成本」 */
  label: string
  /** 主数字（已格式化） */
  value: string
  unit?: string
  /** 一句话读法：这个数字说明什么，不许写成「值得关注」这种废话 */
  read: string
  /** 口径三件套：N / 列名 / 日期 */
  sample: string
  tone: 'danger' | 'warn' | 'ok' | 'neutral'
}

/** 摩擦成本假设：佣金万 2.5 双边（0.05%）+ 印花税 0.05% 单边 + 过户费 0.001% 双边 ≈ 0.10%。 */
export const FRICTION_RATE = 0.001
/** 年化用的交易日数（A 股近年约 242–244 天）。 */
const TRADING_DAYS = 244

const pct = (f: number, digits = 2) => `${(f * 100).toFixed(digits)}%`
const yi = (v: number) => (v / 1e8).toFixed(1)
const yiSmall = (v: number) => (v / 1e8).toFixed(2)

function sampleOf(ctx: AnchorCtx, n: number, cols: string, extra?: string): string {
  const date = ctx.asOf ?? '日期未知'
  return `${date} · N=${n.toLocaleString()} · ${cols}${extra ? ` · ${extra}` : ''}`
}

/**
 * 计算单个锚点。数据不足（空表 / 缺列 / 全为空值）时返回 null —— 调用方必须
 * 显示「今日数据不足」而不是 0。
 */
export function computeAnchor(id: AnchorId, ctx: AnchorCtx): AnchorResult | null {
  const { rows } = ctx
  if (rows.length === 0) return null

  switch (id) {
    // ── 今日涨跌分布：指数是加权平均，你的账户不是 ──
    case 'breadth': {
      const chg = rows
        .map(r => r.change_pct)
        .filter((v): v is number => v != null && Number.isFinite(v))
      if (chg.length === 0) return null
      let up = 0
      let down = 0
      let flat = 0
      for (const v of chg) {
        if (v > 0) up++
        else if (v < 0) down++
        else flat++
      }
      const sorted = [...chg].sort((a, b) => a - b)
      const median = quantile(sorted, 0.5) ?? 0
      const upRatio = up / chg.length
      return {
        id,
        label: '今日涨跌家数',
        value: `${up.toLocaleString()} / ${down.toLocaleString()}`,
        unit: '涨 / 跌',
        read:
          `中位数涨跌幅 ${pct(median)}，上涨家数占比 ${pct(upRatio, 1)}。` +
          `指数是市值加权平均，你的账户是等权的一两只票——` +
          `这两件事可以同时「指数微跌」和「你亏 3%」。`,
        sample: sampleOf(ctx, chg.length, 'change_pct', `平盘 ${flat}`),
        tone: upRatio >= 0.5 ? 'ok' : down > up * 2 ? 'danger' : 'warn',
      }
    }

    // ── 摩擦成本：负和博弈的当日分母 ──
    case 'friction': {
      const amounts = rows
        .map(r => r.amount)
        .filter((v): v is number => v != null && v > 0 && Number.isFinite(v))
      if (amounts.length === 0) return null
      const total = amounts.reduce((s, v) => s + v, 0)
      const cost = total * FRICTION_RATE
      return {
        id,
        label: '今日摩擦成本',
        value: yi(cost),
        unit: '亿元',
        read:
          `全市场今天为了完成这些换手，按佣金万 2.5 双边 + 印花税 0.05% 单边 + 过户费估算，` +
          `约 ${yi(cost)} 亿元流出市场，年化约 ${(cost * TRADING_DAYS / 1e8).toFixed(0)} 亿。` +
          `这笔钱不来自任何人的判断错误，只来自「参与了」——这就是负和的那部分。`,
        sample: sampleOf(ctx, amounts.length, 'amount', `成交额合计 ${yi(total)} 亿；费率假设 ${pct(FRICTION_RATE, 2)}`),
        tone: 'warn',
      }
    }

    // ── ST 涨跌幅：用「有没有越界」直接证伪 5% 老经验 ──
    case 'st-move': {
      const st = rows.filter(r => isSt(r.name) && r.change_pct != null && Number.isFinite(r.change_pct))
      if (st.length === 0) return null
      let maxAbs = 0
      let extreme: MarketSnapshotRow | null = null
      let over5 = 0
      let over95 = 0
      for (const r of st) {
        const v = r.change_pct as number
        const a = Math.abs(v)
        if (a > maxAbs) {
          maxAbs = a
          extreme = r
        }
        if (a > 0.05) over5++
        if (a > 0.095) over95++
      }
      const who = extreme ? `${extreme.name ?? extreme.symbol} ${pct(extreme.change_pct as number)}` : ''
      return {
        id,
        label: 'ST 今日最大波动',
        value: pct(maxAbs),
        read:
          over5 > 0
            ? `今天有 ${over5} 只 ST 的波动超过 5%（其中 ${over95} 只超过 9.5%），最大的是 ${who}。` +
              `如果 ST 仍限 5%，这些数字不可能出现——「5% 涨跌停指纹」当天即可证伪。`
            : `今天没有 ST 波动超过 5%，但这不能反证规则仍是 5%：平静日给不出证据。`,
        sample: sampleOf(ctx, st.length, 'name(含 ST) / change_pct', `|涨跌幅|>5% 共 ${over5} 只`),
        tone: over5 > 0 ? 'danger' : 'neutral',
      }
    }

    // ── 面值退市：横截面上的「已在倒数」名单 ──
    case 'par-line': {
      const priced = rows.filter(r => r.close != null && r.close > 0)
      if (priced.length === 0) return null
      const below = priced.filter(r => (r.close as number) < 1)
      const sorted = [...below].sort((a, b) => (a.close as number) - (b.close as number))
      const cheapest = sorted[0]
      const names = sorted.slice(0, 3).map(r => `${r.name ?? r.symbol} ${(r.close as number).toFixed(2)}`)
      const near = priced.filter(r => (r.close as number) >= 1 && (r.close as number) < 2).length
      return {
        id,
        label: '今日收盘 < 1 元',
        value: below.length.toLocaleString(),
        unit: '只',
        read:
          below.length > 0
            ? `${below.length} 只已收在面值线下方（最低 ${cheapest?.name ?? ''} ` +
              `${(cheapest?.close as number).toFixed(2)} 元；名单：${names.join('、')}），` +
              `另有 ${near} 只挤在 1–2 元区间。规则要的是「连续 20 个交易日」，` +
              `今天破线只说明倒数开始，不说明一定退市。`
            : `今天没有票收在 1 元下方，但 ${near} 只挤在 1–2 元区间——它们是下一批候选。`,
        sample: sampleOf(ctx, priced.length, 'close<1.00', `1–2 元 ${near} 只`),
        tone: below.length > 0 ? 'danger' : 'ok',
      }
    }

    // ── 市值退市：与面值线同期计数的另一条独立红线 ──
    case 'mcap-line': {
      const valued = rows.filter(r => r.market_cap != null && r.market_cap > 0)
      if (valued.length === 0) return null
      const below = valued.filter(r => (r.market_cap as number) < mcapLineOf(boardOf(r.symbol)))
      const min = below.length > 0 ? [...below].sort((a, b) => (a.market_cap as number) - (b.market_cap as number))[0] : null
      return {
        id,
        label: '今日市值低于红线',
        value: below.length.toLocaleString(),
        unit: '只',
        read:
          below.length > 0
            ? `${below.length} 只市值已低于红线（主板 5 亿 / 双创北 3 亿），最小 ${min?.name ?? ''} ` +
              `${yiSmall(min?.market_cap ?? 0)} 亿。市值和面值是两条独立红线，任何一条连续 20 日触发都会终止上市，不互相抵扣。`
            : `今天没有票低于市值红线（主板 5 亿 / 双创北 3 亿）。注意这条线和面值线是独立计数的，不互相抵扣。`,
        sample: sampleOf(ctx, valued.length, 'market_cap = close × total_shares'),
        tone: below.length > 0 ? 'danger' : 'ok',
      }
    }

    // ── 流动性：账面浮盈和真金白银之间隔着的墙 ──
    case 'illiquid': {
      const amounts = rows
        .map(r => r.amount)
        .filter((v): v is number => v != null && v > 0 && Number.isFinite(v))
      if (amounts.length === 0) return null
      const dead = amounts.filter(v => v < 1e7).length
      const worse = amounts.filter(v => v < 5e6).length
      const sorted = [...amounts].sort((a, b) => a - b)
      const median = quantile(sorted, 0.5) ?? 0
      return {
        id,
        label: '今日成交额 < 1000 万',
        value: dead.toLocaleString(),
        unit: '只',
        read:
          `${dead} 只全天成交不足 1000 万（其中 ${worse} 只不足 500 万），全市场中位成交额 ${yiSmall(median)} 亿。` +
          `流动性不是「平时量大」，而是「你要卖的那天还有没有人接」——这些票的对手盘，可能只有你自己的挂单。`,
        sample: sampleOf(ctx, amounts.length, 'amount', `<1000 万 ${dead} / <500 万 ${worse}`),
        tone: dead > amounts.length * 0.02 ? 'warn' : 'neutral',
      }
    }

    // ── 换手率长尾：拥挤发生在尾部而不是平均 ──
    case 'turnover-tail': {
      const turns = rows
        .map(r => r.turnover_rate)
        .filter((v): v is number => v != null && Number.isFinite(v))
      if (turns.length === 0) return null
      const sorted = [...turns].sort((a, b) => a - b)
      const p50 = quantile(sorted, 0.5) ?? 0
      const p90 = quantile(sorted, 0.9) ?? 0
      const p99 = quantile(sorted, 0.99) ?? 0
      const ratio = p50 > 0 ? p99 / p50 : null
      return {
        id,
        label: '换手率 p50 / p99',
        value: `${p50.toFixed(2)}% / ${p99.toFixed(2)}%`,
        unit: '',
        read:
          `换手率的分布不是「大家都差不多」，而是一条长尾：p90 是 p50 的 ` +
          `${(p50 > 0 ? p90 / p50 : 0).toFixed(1)} 倍，p99 是 ${ratio ? ratio.toFixed(1) : '—'} 倍。` +
          `拥挤从来不是全市场现象——它集中在尾部那一小撮票里，而尾部正是踩踏的起点。`,
        sample: sampleOf(ctx, turns.length, 'turnover_rate'),
        tone: ratio != null && ratio > 8 ? 'warn' : 'neutral',
      }
    }

    // ── 涨停与连板：T+1 是「封板资金敢拉」的结构性原因之一 ──
    case 'limit-up': {
      // 涨停/跌停一律不在这里判定：阈值属于后端 price_limits.py（唯一实现点），
      // 前端只统计「后端已经判好的连板计数」，避免成为同一规则的第二定义点。
      const seq = rows
        .map(r => r.consecutive_limit_ups)
        .filter((v): v is number => v != null && Number.isFinite(v) && v > 0)
      const counted = rows.filter(r => r.consecutive_limit_ups != null).length
      if (counted === 0) return null
      const maxConsec = seq.length > 0 ? Math.max(...seq) : 0
      return {
        id,
        label: '今日涨停 / 最高连板',
        value: `${seq.length} / ${maxConsec}`,
        unit: '只 / 板',
        read:
          `今天 ${seq.length} 只封在涨停（连板计数来自后端 ` +
          `consecutive_limit_ups，不在这里重算阈值），最高 ${maxConsec} 连板。` +
          `封板资金敢拉的结构性原因之一就是 T+1：今天买进的人当天卖不掉，砸不动你——反之亦然。`,
        sample: sampleOf(ctx, counted, 'consecutive_limit_ups>0'),
        tone: seq.length > 0 ? 'neutral' : 'ok',
      }
    }

    // ── 成交额集中度：拥挤的横截面代理 ──
    case 'concentration': {
      const amounts = rows
        .map(r => r.amount)
        .filter((v): v is number => v != null && v > 0 && Number.isFinite(v))
        .sort((a, b) => b - a)
      if (amounts.length < 100) return null
      const total = amounts.reduce((s, v) => s + v, 0)
      if (total <= 0) return null
      const top100 = amounts.slice(0, 100).reduce((s, v) => s + v, 0)
      const share = top100 / total
      return {
        id,
        label: '前 100 只成交额占比',
        value: pct(share, 1),
        unit: '',
        read:
          `前 100 只票吃掉全市场 ${pct(share, 1)} 的成交额（约 ${yi(total)} 亿中的 ${yi(top100)} 亿）。` +
          `集中度只能证明「资金挤在少数票上」，不能证明「马上踩踏」——它是拥挤的必要不充分证据。`,
        sample: sampleOf(ctx, amounts.length, 'amount 降序取前 100'),
        tone: share > 0.25 ? 'warn' : 'neutral',
      }
    }

    default:
      return null
  }
}

/** 全部锚点 id（顺序即页面展示顺序，也是测试遍历顺序）。 */
export const ALL_ANCHORS: AnchorId[] = [
  'breadth',
  'friction',
  'st-move',
  'par-line',
  'mcap-line',
  'illiquid',
  'turnover-tail',
  'limit-up',
  'concentration',
]
