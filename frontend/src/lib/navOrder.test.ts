// @vitest-environment node
import { describe, expect, it } from 'vitest'
import {
  CORE_NAV_PATHS,
  DEFAULT_NAV_PATHS,
  EXT_DOMAIN_PAGE_PATHS,
  compareByDefaultNav,
  defaultNavIndex,
  seedHiddenForNewUser,
} from './navOrder'

// 迁移前 Layout 核心导航数组的原序 (2026-10 扩展域迁移快照)。
// 这是老用户升级兼容的锚点数据: 改动任何一个位置都意味着某类未保存
// 排序用户的侧栏顺序在升级时发生变化, 需要明确决策而不是顺手调整。
// 2026-11 明确决策: /battle 插在 / 之后 (入口级页面), 其余路径整体后移一位。
// 2026-11 明确决策: /decisions 追加在 /data 之后 (复盘入口, 但不越过数据维护页)。
// 2026-11 明确决策: /case-lab 追加在 /decisions 之后 (学习入口, 动线 看盘 → 复盘 → 学习)。
describe('navOrder: 迁移前默认排序基准', () => {
  it('覆盖迁移前全部内置路径 (新增 /battle、/decisions、/case-lab 后共 22 个) 且无重复', () => {
    expect(DEFAULT_NAV_PATHS).toHaveLength(22)
    expect(new Set(DEFAULT_NAV_PATHS).size).toBe(22)
  })

  it('关键位置与迁移前核心导航数组一致', () => {
    expect(DEFAULT_NAV_PATHS[0]).toBe('/')
    expect(DEFAULT_NAV_PATHS[1]).toBe('/battle')
    expect(DEFAULT_NAV_PATHS[6]).toBe('/stock-analysis')
    expect(DEFAULT_NAV_PATHS[11]).toBe('/monitor')
    expect(DEFAULT_NAV_PATHS[12]).toBe('/regime')
    expect(DEFAULT_NAV_PATHS[17]).toBe('/review')
    expect(DEFAULT_NAV_PATHS[19]).toBe('/data')
    expect(DEFAULT_NAV_PATHS[20]).toBe('/decisions')
    expect(DEFAULT_NAV_PATHS[21]).toBe('/case-lab')
  })

  it('未知道径返回 Infinity, 排在已知路径之后', () => {
    expect(defaultNavIndex('/data')).toBe(19)
    expect(defaultNavIndex('/company/risk')).toBe(Number.POSITIVE_INFINITY)
    expect(defaultNavIndex('/analysis/custom1')).toBe(Number.POSITIVE_INFINITY)
  })

  it('排序比较器: 已知按默认序, 未知稳定沉底 (不产生 NaN)', () => {
    const paths = ['/monitor', '/company/risk', '/', '/analysis/x', '/watchlist', '/data']
    const sorted = [...paths].sort(compareByDefaultNav)
    // 两个未知道径保持原始相对顺序 (company/risk 在 analysis/x 前)
    expect(sorted).toEqual(['/', '/watchlist', '/monitor', '/data', '/company/risk', '/analysis/x'])
  })
})

// 这条断言是「新增内置页面必须登记默认序」的唯一闸门。
// 漏登记的后果不是报错, 而是该页面在侧栏沉到全部扩展页之后 (/decisions 踩过);
// 上面那条 toHaveLength 不会因此变红 —— 它数的是 DEFAULT_NAV_PATHS 自己。
describe('core 导航与默认基准的绑定 (防止新页面静默沉底)', () => {
  it('CORE_NAV_PATHS 每一项都能在默认基准里查到序号', () => {
    const missing = CORE_NAV_PATHS.filter(p => defaultNavIndex(p) === Number.POSITIVE_INFINITY)
    expect(missing).toEqual([])
  })

  it('CORE_NAV_PATHS 无重复, 且每项都在 DEFAULT_NAV_PATHS 中', () => {
    expect(new Set(CORE_NAV_PATHS).size).toBe(CORE_NAV_PATHS.length)
    const absent = CORE_NAV_PATHS.filter(p => !(DEFAULT_NAV_PATHS as readonly string[]).includes(p))
    expect(absent).toEqual([])
  })

  it('核心页面的默认序互不相同 (合并排序时锚点不打架)', () => {
    const indexes = CORE_NAV_PATHS.map(p => defaultNavIndex(p))
    expect(new Set(indexes).size).toBe(indexes.length)
  })
})

describe('seedHiddenForNewUser: 新装用户默认隐藏种子', () => {
  it('扩展域页面清单与默认基准一致且共 11 项', () => {
    expect(EXT_DOMAIN_PAGE_PATHS).toHaveLength(11)
    for (const p of EXT_DOMAIN_PAGE_PATHS) {
      expect(defaultNavIndex(p)).not.toBe(Number.POSITIVE_INFINITY)
    }
  })

  it('新用户 (未完成 onboarding): 11 个扩展页并入隐藏清单', () => {
    const next = seedHiddenForNewUser(false, [])
    expect(next).not.toBeNull()
    for (const p of EXT_DOMAIN_PAGE_PATHS) expect(next).toContain(p)
  })

  it('新用户已有隐藏项: 取并集不丢失', () => {
    const next = seedHiddenForNewUser(false, ['/indices'])
    expect(next).toContain('/indices')
    expect(next).toContain('/review')
    expect(new Set(next).size).toBe(next!.length)
  })

  it('老用户 (已完成 onboarding) 与未知状态: 不动, 返回 null', () => {
    expect(seedHiddenForNewUser(true, [])).toBeNull()
    expect(seedHiddenForNewUser(undefined, [])).toBeNull()
  })
})
