/**
 * 内置 (核心) 导航页面的**唯一**清单 —— Layout 的 label/icon 表按本清单展开。
 *
 * 此前 Layout 里另有一份手写 nav 数组, 两处各记一份「有哪些内置页面」:
 * 新增 /decisions 时只改了一处, 于是它在 nav_order 里查不到序号 (Infinity),
 * 既排到 11 个扩展页之后, 也在「已保存排序」用户那里被 push 到末尾 ——
 * 页面能用, 但入口沉底。现在改为 Layout 从本清单派生, 少写一个就是类型错误。
 *
 * 顺序 = 侧栏默认顺序 (与迁移前一致, /battle 与 /decisions 的插入位置是有意决策)。
 */
export const CORE_NAV_PATHS = [
  '/', '/battle', '/watchlist', '/screener', '/factors', '/backtest',
  '/regime', '/indices', '/data', '/decisions', '/case-lab', '/common-sense',
  '/handbook', '/library', '/chip', '/narrative',
] as const

export type CoreNavPath = (typeof CORE_NAV_PATHS)[number]

/**
 * 内置页面的中文名 —— 侧栏与「设置 → 菜单设置」共用这一份。
 *
 * 之前 Layout.nav、MenuSettings.BUILTIN_PAGES、MenuSettings.ARCH_CLASS 各写一份
 * 页面名/归属, 新增页面时漏改不会报错: 侧栏有、菜单设置里没有 (于是用户既不能
 * 隐藏也排不了序, 保存 nav_order 后它被追加到末尾), 归属还被默认成「自定义分析页」。
 * 现在名称只有本表一处, 清单只有 CORE_NAV_PATHS 一处, 缺项 = 类型错误。
 */
export const CORE_NAV_LABELS: Record<CoreNavPath, string> = {
  '/': '看板',
  '/battle': '博弈链路',
  '/watchlist': '自选',
  '/screener': '策略',
  '/factors': '因子',
  '/backtest': '回测',
  '/regime': '市场环境',
  '/indices': '指数',
  '/data': '数据',
  '/decisions': '决策账本',
  '/case-lab': '操盘档案',
  '/common-sense': '市场常识',
  '/handbook': '机会雷达',
  '/library': '策略图书馆',
  '/chip': '筹码久期',
  '/narrative': '叙事账本',
}

/**
 * 侧栏默认排序基准 — 扩展域页面迁移 (custom/) 的老用户升级兼容。
 *
 * 迁移前这 21 个路径按此顺序构成内置侧栏 (Layout 核心导航数组 + 11 个扩展域页面)。
 * 页面迁往 custom/<ns>/extension.tsx 后, 无保存排序的环境 (新装或
 * 从未调整过菜单的用户) 若按「核心在前、扩展追加」拼接, 11 个页面会
 * 整体沉底, 老用户升级即感知错位。因此默认顺序统一按本基准排序,
 * 升级前后侧栏一致; 已保存过 nav_order 的用户按保存值展示, 不受影响。
 *
 * 未知路径 (声明式分析页、二开扩展) 排在已知路径之后, 保持相对顺序。
 * CORE_NAV_PATHS 必须是本清单的子集 (navOrder.test.ts 断言): 漏一个
 * 不会报错, 只会让那个页面在侧栏里静默沉底。
 */
export const DEFAULT_NAV_PATHS = [
  // 2026-11 新增 /battle（博弈链路）插在 / 之后：它承担"今天该看什么"的入口
  // 职责，沉底会让新用户找不到。这是有意的顺序决策，其余路径相对次序未动。
  '/', '/battle', '/watchlist', '/screener', '/factors', '/backtest',
  '/stock-analysis', '/limit-ladder', '/concept-analysis', '/industry-analysis', '/financials',
  '/monitor', '/regime', '/abnormal', '/lots', '/paper', '/signals', '/review',
  '/indices', '/data',
  // 2026-11 新增 /decisions（决策账本）排在 /data 之后：它是每日复盘入口,
  // 但排在数据维护页之前会让「配置完数据」这条路径断开。位置与 Layout 声明一致。
  '/decisions',
  // 2026-11 新增 /case-lab（操盘档案）排在最后：它是「学」的一站，不是「看盘」的一站。
  // 与「决策账本」相邻而位于其后，动线是 看盘 → 复盘 → 学习。
  '/case-lab',
  // 2026-11 新增 /common-sense（市场常识）排在末尾，与「操盘档案」相邻：
  // 它是「基本功」的一站，动线是 看盘 → 复盘 → 学习 → 常识沉淀。
  '/common-sense',
  // 2026-11 新增 /structural-edge（结构优势）排在末尾：它是研究结论看板，
  // 不是看盘入口，与「学习」动线收尾相邻。
  // 2026-11-22 明确决策：/structural-edge 与 /position-art 合并为「投资手册」，
  // 路径改为 /long-strategy，沿用前者在默认序里的位置（23）。旧路径只做路由重定向，
  // 不再进默认序 —— 已保存过 nav_order 的用户，由 Layout 的「插回默认前驱之后」
  // 逻辑把它放回 /common-sense 之后，与升级前一致。
  '/handbook',
  // 2026-11-24 明确决策：手册拆成两页 —— 理论沉淀（总则/证据/规则/反证/工具）是
  // 「策略图书馆」，冻结低频，单独成页；每天找机会的动态部分是「机会雷达」(/handbook)。
  // 图书馆排在其后：动线是 每天看机会 → 偶尔翻理论。
  '/library',
  // 2026-11-26 明确决策：/chip（筹码久期）排在图书馆之后。
  // 它是独立研究线，不并进「事件低吸」图书馆；页面从「已关闭的裁决记录」
  // 升级为「理论稿（未验证）」，仍放在理论沉淀之后作为收尾。
  '/chip',
  // 2026-11 新增 /narrative（叙事账本）：与 /chip 同为独立研究线（一个读负债端，一个读收据），
  // 排在筹码之后收尾。它不是看盘入口，是「今天的热度该怎么读」的对照页。
  '/narrative',
] as const

const INDEX = new Map<string, number>(DEFAULT_NAV_PATHS.map((p, i) => [p, i]))

/**
 * 改名遗留路径 → 当前路径。
 *
 * 只用于读取「已保存的 nav_order / nav_hidden」：页面改名后旧路径不再存在，
 * 若不归一化，老用户保存过的排序会丢掉这一项（被追加到末尾），隐藏状态也会失效。
 * 旧书签由路由重定向负责，这里只修内存里的偏好。
 */
export const LEGACY_NAV_PATHS: Record<string, string> = {
  '/citizenship': '/shortline-radar',
}

/** 旧路径归一化；未知路径原样返回（分析页与二开扩展不受影响）。 */
export function canonicalNavPath(path: string): string {
  return LEGACY_NAV_PATHS[path] ?? path
}

/** 路径的默认序号; 未知路径返回 Infinity (排在全部已知路径之后) */
export function defaultNavIndex(path: string): number {
  return INDEX.get(path) ?? Number.POSITIVE_INFINITY
}

/**
 * 按默认基准排序的比较器。两个未知路径相减会产生 NaN 破坏排序,
 * 必须先判等再相减 (相等时依赖 sort 的稳定性保持相对顺序)。
 */
export function compareByDefaultNav(a: string, b: string): number {
  const ia = defaultNavIndex(a)
  const ib = defaultNavIndex(b)
  if (ia === ib) return 0
  return ia - ib
}

/**
 * 扩展域页面路径 (迁移到 custom/ 的 11 个官方页面)。
 * 新装用户的一次性种子会把它们写入 nav_hidden 默认隐藏;
 * 老用户 (升级前已完成 onboarding) 不受影响。用户随时可在
 * 设置 → 菜单设置 里点眼睛图标逐个开启。
 */
export const EXT_DOMAIN_PAGE_PATHS: readonly string[] = [
  '/stock-analysis', '/limit-ladder', '/concept-analysis', '/industry-analysis', '/financials',
  '/monitor', '/abnormal', '/lots', '/paper', '/signals', '/review',
]

/**
 * 新装用户种子: 返回应写入 nav_hidden 的完整列表 (与已有隐藏项取并集);
 * 老用户返回 null 表示不动。判定信号是 onboarding_completed —— 首启
 * 向导强制完成, 升级前用过应用的用户必然为 true。
 */
export function seedHiddenForNewUser(
  onboardingCompleted: boolean | undefined,
  currentHidden: readonly string[] | null | undefined,
): string[] | null {
  if (onboardingCompleted !== false) return null
  const hidden = new Set(currentHidden ?? [])
  for (const p of EXT_DOMAIN_PAGE_PATHS) hidden.add(p)
  return [...hidden]
}
