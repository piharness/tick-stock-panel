/**
 * 兼容入口。
 *
 * 2026-11-24 手册拆成两页：
 *   /handbook  = Daily（机会雷达，每天看）—— 原「执行」章独立成页；
 *   /library   = Library（策略图书馆，低频查）—— 总则/证据/规则/反证/工具。
 * /long-strategy、/structural-edge 与 /position-art 由 router 按 hash 分流：
 * 理论锚点（#buy / #sizing-audit / #archive-b…）→ 图书馆，执行锚点（#today / #register…）→ 雷达。
 *
 * 文件（./structural-edge/）：
 *   index.tsx       图书馆壳 + 五章 tab + 取数（useWatch）
 *   Daily.tsx       机会雷达（判定 hero / 候选卡 / 指令 / 折叠参考区）
 *   Shell.tsx       tab 条 / 章内目录 / 跨章跳转 / 深链解析
 *   useWatch.ts     两页共用的活数据 hook（etf-watch / account / cash-leg / position-art）
 *   data.ts         全部静态数字（唯一来源）
 *   ui.tsx          Section / Stat / DataTable / Callout / Fold / Chart / Cadence / Tile
 *   Mandate.tsx     总则（赌什么 / 六条不做 / 构成轴 / 边界）
 *   Evidence.tsx    证据（强度 / 位置形态 / 广度久期 / 仓位择时验收 / 组合层 / 两个再等等）
 *   Rules.tsx       规则（买点六问 / 不买清单 / 仓位 / 持有与退出）
 *   Execution.tsx   雷达页的可复用卡片（账户 / 现金腿 / 反例登记 / 账本 / 四步）
 *   Archive.tsx     反证（裁决索引 + 四组案卷）
 *   Replay.tsx      工具 · 回放台（单日快照 / 信号轨迹 / 买点效果）
 *   ScanTable.tsx   雷达/回放共用的判定表
 *   Maintenance.tsx 工具 · 维护（更新节奏 / 数据链路 / 文档地图 / 改数规则）
 *
 * router.tsx 与测试仍从这里 import，保留薄转出，不重复实现。
 */
export { Daily } from './structural-edge/Daily'
export { Library, Library as LongStrategy, Library as StructuralEdge } from './structural-edge'
