/**
 * 兼容入口。
 *
 * 页面已改名为「投资手册 · 事件低吸」，路由迁到 /handbook；
 * /long-strategy、/structural-edge 与 /position-art 由 router 重定向到这里（保留 hash 锚点）。
 *
 * 章节（./structural-edge/）：
 *   index.tsx       页面壳 + 六章 tab + 唯一一次 /api/etf-watch 取数
 *   Shell.tsx       tab 条 / 章内目录 / 跨章跳转 / 深链解析
 *   data.ts         全部静态数字（唯一来源）
 *   ui.tsx          Section / Stat / DataTable / Callout / Fold / Chart / Cadence
 *   Mandate.tsx     总则（赌什么 / 六条不做 / 构成轴 / 边界）
 *   Evidence.tsx    证据（强度 / 位置形态 / 广度久期 / 仓位择时验收 / 组合层 / 两个再等等）
 *   Rules.tsx       规则（买点六问 / 不买清单 / 仓位 / 持有与退出）
 *   Execution.tsx   执行（扫描 / 账户 / 账本 / 四步 / 反例登记）
 *   Archive.tsx     反证（裁决索引 + 四组案卷）
 *   Replay.tsx      工具 · 回放台（单日快照 / 信号轨迹 / 买点效果）
 *   ScanTable.tsx   执行/回放共用的判定表
 *   Maintenance.tsx 工具 · 维护（更新节奏 / 数据链路 / 文档地图 / 改数规则）
 *
 * router.tsx 与测试仍从这里 import，保留薄转出，不重复实现。
 */
export { Handbook, Handbook as LongStrategy, Handbook as StructuralEdge } from './structural-edge'
