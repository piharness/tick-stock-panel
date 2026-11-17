import type { MinuteKlineRow } from '@/lib/api'

export type DailySummary = { date: string; open: number | null; high: number; low: number; close: number }

/** 仅汇总实际收到的分钟，不将缺失开盘价或成交额伪装为真实值。 */
export function summarizeMinutes(data: MinuteKlineRow[]): MinuteKlineRow | null {
  if (!data.length) return null
  return {
    datetime: data[0].datetime,
    open: data[0].open,
    high: Math.max(...data.map(row => row.high)),
    low: Math.min(...data.map(row => row.low)),
    close: data[data.length - 1].close,
    volume: data.reduce((total, row) => total + row.volume, 0),
    amount: data.every(row => row.amount != null && Number.isFinite(row.amount))
      ? data.reduce((total, row) => total + row.amount!, 0) : null,
  }
}

/** 从 datetime 串取 HH:MM。契约: 分钟K datetime 已在后端入口统一为北京墙钟, 前端不做时区换算。 */
export function formatMinuteTime(datetime: string): string {
  const match = datetime.match(/(\d{2}):(\d{2})/)
  if (!match) return datetime.slice(11, 16)
  return `${match[1]}:${match[2]}`
}

export function computeIntradayAverage(data: MinuteKlineRow[]): (number | null)[] {
  const result: (number | null)[] = []
  let amount = 0
  let volume = 0
  let hasAmount = true
  for (const row of data) {
    if (typeof row.amount === 'number' && Number.isFinite(row.amount)) {
      amount += row.amount
    } else {
      hasAmount = false
    }
    volume += row.volume * 100
    result.push(hasAmount && volume > 0 ? amount / volume : null)
  }
  return result
}

function generateFullDayTimes(): string[] {
  const times: string[] = []
  for (let hour = 9; hour <= 11; hour++) {
    const startMinute = hour === 9 ? 30 : 0
    const endMinute = hour === 11 ? 30 : 59
    for (let minute = startMinute; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  for (let hour = 13; hour <= 15; hour++) {
    const endMinute = hour === 15 ? 0 : 59
    for (let minute = 0; minute <= endMinute; minute++) {
      times.push(`${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`)
    }
  }
  return times
}

export const FULL_DAY_TIMES = generateFullDayTimes()

/** 分钟周期 → 每天 bar 时刻（区间**终点**标注，与 1m 的 09:31..15:00 同约定）。
 *
 * 不要用 FULL_DAY_TIMES 铺 5m/15m/… 的 x 轴：那会给出 240 个槽位、只填 48 个，
 * 图上是一串被拉长的空白。这些网格由实测的通达信数据反推：
 * 5m 每天 48 根（09:35..11:30 + 13:05..15:00）、15m 16 根、30m 8 根、60m 4 根。
 */
const MINUTE_FREQ_STEP: Record<string, number> = { '5m': 5, '15m': 15, '30m': 30, '60m': 60 }

export function minuteTimesForFreq(freq: string): string[] {
  const step = MINUTE_FREQ_STEP[freq]
  if (!step) return FULL_DAY_TIMES
  const out: string[] = []
  const push = (totalMinutes: number) => {
    const h = Math.floor(totalMinutes / 60)
    const m = totalMinutes % 60
    out.push(`${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`)
  }
  for (let m = 9 * 60 + 30 + step; m <= 11 * 60 + 30; m += step) push(m)
  for (let m = 13 * 60 + step; m <= 15 * 60; m += step) push(m)
  return out
}
