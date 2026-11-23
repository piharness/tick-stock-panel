/**
 * 个股判据 —— 把「吸筹 / 对敲 / 震仓 / 飞刀 / 派发 / 控盘 / 壳 / 减持 / 分歧」
 * 九条流传的说法，落到**某一只票**上算出结论。
 *
 * 为什么需要这一层：实时锚点（anchors.ts）是**市场级**的——它回答「今天全市场有多少
 * 只票破面值」。但常识真正被用到的地方是「我看的这一只」，那里市场级数字帮不上忙。
 * 这一层把九条判定接到个股上：输入代码，每条给一个状态和（能算的话）一个数。
 *
 * 三条纪律，与后端 table_game._stock_rules 的分工严格对齐：
 *   1. **阈值只在后端存在一份**。这里不写任何数值门槛（0.8、20%、5 日……），
 *      只负责展示后端返回的 state / value / detail。前端一旦自己再判一次，
 *      就出现了同一规则的第二定义点——两处漂移时页面不会报错，只会给出两个答案。
 *   2. **九条永远九条**。缺数据的那条也占一格，标 no_data 并说清缺什么；
 *      不许因为「算不出来」就从页面上消失（那是把「看不见」伪装成「不存在」）。
 *   3. **形状不是证据**。「成立」只说明形态存在，不说明背后是谁、也不说明下一步。
 *      这条必须写在界面上，否则读者会把「成立」读成「看懂了」。
 *
 * 映射关系不在这里复制：哪条常识挂哪个判据，写在 knowledge.ts 的 item.probe 上，
 * 这里用 probeItemId() 反查。单向依赖，不做第二份对照表。
 */
import type { BoardRule, TableGameBoard } from '@/lib/api'
import { KNOWLEDGE } from './knowledge'

export type ProbeId =
  | 'accum'
  | 'wash'
  | 'shakeout'
  | 'knife'
  | 'distribution'
  | 'control'
  | 'shell'
  | 'reduce'
  | 'divergence'

export interface ProbeSpec {
  id: ProbeId
  /**
   * 短名。**只在「拿不到规则」时用作占位**——正常路径一律显示后端返回的 rule.label，
   * 免得同一件事在前后端各有一个名字。占位名与后端 label 的差异由人工保证（仅文字，不影响判定）。
   */
  label: string
  /** 这条判定回答的问题（先给问题，读者才知道数字在答什么） */
  question: string
  /** 需要哪张表 / 哪些列才能回答；no_data 时读者至少知道缺什么 */
  needs: string
}

/**
 * 顺序 = 后端 RULE_IDS 的顺序。
 * 两边由 backend/tests/test_probe_contract.py 双向比对，改动必须同步。
 */
export const PROBES: ProbeSpec[] = [
  {
    id: 'accum',
    label: '吸筹',
    question: '这只票像是在被人慢慢收，还是在被抢？',
    needs: '日线 20 日（close / turnover_rate）',
  },
  {
    id: 'wash',
    label: '对敲 / 换手堆积',
    question: '成交量放大时，价格有没有跟着走？',
    needs: '日线 20 日（turnover_rate / amp）',
  },
  {
    id: 'shakeout',
    label: '震仓',
    question: '这里的放量下跌，后来收回来了吗？',
    needs: '日线 40 日（ret / volume / close）',
  },
  {
    id: 'knife',
    label: '飞刀（连续跌停后接盘）',
    question: '上次连续跌停之后，接刀的人赚了还是亏了？',
    needs: '日线 120 日（consecutive_limit_downs / close）',
  },
  {
    id: 'distribution',
    label: '派发（高位放量长上影）',
    question: '高位放量时，K 线留下的是上影还是实体？',
    needs: '日线 250 日（high / open / close / volume）',
  },
  {
    id: 'control',
    label: '控盘成本',
    question: '拿走两成流通盘，要吃掉几天的全部成交？',
    needs: '流通市值 + 20 日日均成交额（算术，不是观测）',
  },
  {
    id: 'shell',
    label: '壳价值',
    question: '它现在是「生意」还是「上市资格」？',
    needs: '流通市值分位 + 最新年报 ROE',
  },
  {
    id: 'reduce',
    label: '减持压力',
    question: '大股东在卖吗？卖了多少？',
    needs: '十大股东 / 限售解禁 / 大宗交易 / 减持公告（本地均无）',
  },
  {
    id: 'divergence',
    label: '机构 / 游资分歧',
    question: '机构与游资在这只票上，方向一致吗？',
    needs: '龙虎榜机构专用 / 游资席位净额',
  },
]

/** 展示顺序（= 后端 RULE_IDS 顺序，也是契约的一部分）。 */
export const PROBE_ORDER: ProbeId[] = PROBES.map(p => p.id)

const SPEC_BY_ID = new Map(PROBES.map(p => [p.id, p]))

export function probeSpec(id: ProbeId): ProbeSpec {
  const s = SPEC_BY_ID.get(id)
  if (!s) throw new Error(`未知判据 id: ${id}`)
  return s
}

/** 这条判据挂在哪条常识上（反查 knowledge.ts，不做第二份映射）。 */
export function probeItemId(id: ProbeId): string | null {
  return KNOWLEDGE.find(k => k.probe === id)?.id ?? null
}

/**
 * 单条判据的查询结果。三态而不是两态：
 * 拿到规则 / 拿不到（并说明为什么）—— 拿不到时**不能**返回 null 就算了，
 * 否则 UI 上又变成「少一块」。
 */
export type ProbeLookup =
  | { kind: 'rule'; rule: BoardRule }
  | { kind: 'unavailable'; reason: string }

export function probeLookup(board: TableGameBoard | undefined | null, id: ProbeId): ProbeLookup {
  if (!board) return { kind: 'unavailable', reason: '还没取到这只票的数据' }
  if (board.state === 'not_in_universe') {
    return {
      kind: 'unavailable',
      reason: '本地日线里没有这只票——多半是已退市（库里有生存偏差，退市股大多不在），也可能代码写错。',
    }
  }
  if (board.state === 'no_data') {
    return { kind: 'unavailable', reason: '本地日线为空，无法判定' }
  }
  const rule = board.rules?.find(r => r.id === id)
  if (!rule) {
    // 后端本应恒返回九条。这里兜住「少一条」的情况：明说缺失，而不是渲染成空白。
    return { kind: 'unavailable', reason: '后端没有返回这条判定（契约缺一条）' }
  }
  return { kind: 'rule', rule }
}

export interface ProbeTally {
  total: number
  /** 条件成立 */
  hit: number
  /** 条件不成立 */
  clear: number
  /** 算术推算 */
  derived: number
  /** 本仓库看不见 */
  blind: number
  /** 拿不到（缺规则 / 票不在库）——与 roll 的 blind 不同：这是「连判定都没有」 */
  missing: number
}

export function probeTally(board: TableGameBoard | undefined | null): ProbeTally {
  const t: ProbeTally = { total: PROBE_ORDER.length, hit: 0, clear: 0, derived: 0, blind: 0, missing: 0 }
  for (const id of PROBE_ORDER) {
    const r = probeLookup(board, id)
    if (r.kind === 'unavailable') t.missing++
    else if (r.rule.state === 'triggered') t.hit++
    else if (r.rule.state === 'derived') t.derived++
    else if (r.rule.state === 'no_data') t.blind++
    else t.clear++
  }
  return t
}

/** 判定结果的「值」渲染：value 可能是数字、字符串，也可能没有。 */
export function probeValueText(rule: BoardRule): string {
  const v = rule.value
  if (v == null) return '—'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(2)
  return v
}
