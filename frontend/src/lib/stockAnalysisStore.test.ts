// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 「今天」只有一个定义: 北京日历日。
 *
 * 后端 `json_report_store._now_iso()` 写的是北京墙钟 naive 串
 * ("2026-11-15T14:02:26"), 而 UTC 今天在北京时间 00:00-08:00 还停在前一天。
 * 一旦用 `toISOString().slice(0, 10)` 去比 created_at 的日期前缀:
 *   - 凌晨刚生成 (created_at = 今天) → 认不出来 → 二次确认弹窗又弹一次 → 重复花 AI 钱
 *   - 昨晚 23:xx 生成 (created_at = 昨天) → 被当成今天 → 复用过期结论
 * 两种情况都在 00:00-08:00 这个窗口里, 白天永远测不出来。
 */

const { listMock } = vi.hoisted(() => ({ listMock: vi.fn() }))

vi.mock('./api', () => ({
  api: { stockAnalysisReportsList: listMock },
  friendlyStreamError: (m: string | null | undefined) => m ?? '',
}))

type Store = typeof import('./stockAnalysisStore')

/** 每次重新求值模块: history / historyLoaded 是模块级状态, 不能跨用例复用。 */
async function freshStore(): Promise<Store> {
  vi.resetModules()
  return import('./stockAnalysisStore')
}

function report(symbol: string, createdAt: string) {
  return { id: `r-${createdAt}`, symbol, name: '测试', focus: '', content: 'x', created_at: createdAt }
}

describe('findTodayReport: 「当日」按北京日历判, 不按 UTC', () => {
  beforeEach(() => {
    listMock.mockReset()
    // 北京时间 2026-11-17 01:00 —— UTC 此刻是 2026-11-16T17:00Z
    vi.useFakeTimers({ now: new Date('2026-11-17T01:00:00+08:00') })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('失败窗口成立: 此刻的 UTC 日期确实是昨天 (用例前提, 不是被测逻辑)', () => {
    expect(new Date().toISOString().slice(0, 10)).toBe('2026-11-16')
  })

  it('凌晨 00:30 生成的报告算「今天」, 不能被漏掉', async () => {
    listMock.mockResolvedValue({ reports: [report('600000.SH', '2026-11-17T00:30:00')] })
    const store = await freshStore()
    const hit = await store.findTodayReport('600000.SH')
    expect(hit?.created_at).toBe('2026-11-17T00:30:00')
  })

  it('昨晚 23:50 生成的报告不算「今天」, 不能被复用', async () => {
    listMock.mockResolvedValue({ reports: [report('600000.SH', '2026-11-16T23:50:00')] })
    const store = await freshStore()
    expect(await store.findTodayReport('600000.SH')).toBeNull()
  })

  it('同一份历史里新旧各一条: 取今天那条', async () => {
    listMock.mockResolvedValue({
      reports: [
        report('600000.SH', '2026-11-17T00:05:00'),
        report('600000.SH', '2026-11-16T23:50:00'),
      ],
    })
    const store = await freshStore()
    const hit = await store.findTodayReport('600000.SH')
    expect(hit?.created_at).toBe('2026-11-17T00:05:00')
  })

  it('别的股票的同日报告不算', async () => {
    listMock.mockResolvedValue({ reports: [report('000001.SZ', '2026-11-17T00:30:00')] })
    const store = await freshStore()
    expect(await store.findTodayReport('600000.SH')).toBeNull()
  })
})
