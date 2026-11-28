/**
 * 页面名的**唯一**定义点：导航（extension.tsx）与页内标题（Page.tsx）共用。
 *
 * 此前两处各写一份「市场公民」，改名只改一处会让侧栏与页内标题不一致，
 * 且没有任何类型或测试会报错。这里收成一份，缺项 = 编译错误。
 *
 * 2026-11-22：页面、路由、API 与前端类型统一为 shortline-radar；
 * 仅计算内核 `services/citizenship.py` 保留历史模块名（研究脚本仍按此导入）。
 */
export const SHORTLINE_RADAR_PAGE_TITLE = '短线雷达'

/** 页内副标题的固定部分；日期由页面拼在后面。 */
export const SHORTLINE_RADAR_PAGE_SUBTITLE = '明日买什么 · 今天谁在动'
