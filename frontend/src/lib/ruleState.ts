/**
 * 四态判定的**唯一定义点**（label / 配色 / 含义）。
 *
 * 为什么单独抽出来：同一套状态（成立 / 不成立 / 算术 / 无数据）现在有三个渲染位置——
 * 博弈链路的「常识检验台」、市场常识的「个股判据」面板、以及常识条目的详情卡。
 * 三处各写一遍文案，迟早在某一处把「算术」写成「推算」、把「无数据」写成「无」，
 * 而读者无从分辨哪个才是真的没有。这类分叉不会报错，只会让同一件事有两种说法。
 *
 * 四态的分界（不是排版选择，是认识论分界）：
 *   triggered  条件成立。**形状存在，但形状不是证据**——它只说明这只票具备某种形态，
 *              不说明背后是谁、也不说明下一步会怎样。
 *   clear      条件不成立。这是一条**有意义的信息**，不是「没有结论」：
 *              它排除了这只票当前具备该形态的可能。
 *   derived    算术推算，不是观测。数字是真的，但它是「按定义必然如此」的推论。
 *   no_data    本地缺这张表。**「看不见」不等于「没有」**——所以它绝不显示 0。
 */
import type { RuleState } from '@/lib/api'

export interface RuleStateMeta {
  /** 面板上的完整标签 */
  label: string
  /** 节点芯片上的短标签（空间只有两三个字） */
  short: string
  /** 徽标底色（用于 <span class="rounded px-1">） */
  cls: string
  /** 数字颜色（用于 font-mono 数值） */
  text: string
  /** 圆点颜色（用于节点前缀） */
  dot: string
  /** 这个状态该怎么读——写进 tooltip，避免「无数据」被读成「没问题」 */
  hint: string
}

export const RULE_STATE: Record<RuleState, RuleStateMeta> = {
  triggered: {
    label: '成立',
    short: '成立',
    cls: 'bg-warning/15 text-warning',
    text: 'text-warning',
    dot: 'bg-warning',
    hint: '条件成立：形态存在。但形态不是证据——它不说明背后是谁，也不说明下一步。',
  },
  clear: {
    label: '不成立',
    short: '不成立',
    cls: 'bg-elevated text-muted',
    text: 'text-secondary',
    dot: 'bg-border',
    hint: '条件不成立。这是结论，不是「没有结论」：这只票当前不具备该形态。',
  },
  derived: {
    label: '算术',
    short: '算术',
    cls: 'bg-accent/10 text-accent',
    text: 'text-accent',
    dot: 'bg-accent',
    hint: '算术推算，不是观测。数字成立，但它是按定义算出来的必然结果。',
  },
  no_data: {
    label: '无数据',
    short: '无数据',
    cls: 'bg-elevated text-muted line-through decoration-border',
    text: 'text-muted',
    dot: 'bg-muted/40',
    hint: '本地缺这张表。「看不见」不等于「没有」——所以这里不给 0。',
  },
}

/** 展示顺序：先成立、再算术、再不成立、最后无数据（读者先看有信息量的）。 */
export const RULE_STATE_ORDER: RuleState[] = ['triggered', 'derived', 'clear', 'no_data']
