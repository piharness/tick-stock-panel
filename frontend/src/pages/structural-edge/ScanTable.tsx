/**
 * 行业 ETF 判定表 —— 今日扫描与历史回放共用同一张表。
 *
 * 为什么要抽出来：单日回放如果另写一份表格/判定文案，就会产生「今天说四条全过、
 * 回放说候选」的第二套口径。这里只做展示，判定字段仍来自 analysis/etf_watch.py 的 scan。
 */
import type { EtfWatchIndustry, EtfWatchPayload, EtfWatchStatus } from '@/lib/api'
import { cn } from '@/lib/cn'
import { DataTable, type Col, Empty, Tag, type Tone } from './ui'

export type ScanPayload = {
  meta: Pick<EtfWatchPayload['meta'], 'thresholds' | 'vol_convention'>
  industries: EtfWatchIndustry[]
}

export const statusTone = (s: EtfWatchStatus): Tone =>
  s === 'CANDIDATE' ? 'ok' : s === 'BLOCKED_F1' ? 'danger' : s === 'BLOCKED' ? 'warn' : 'muted'

export const statusLabel: Record<EtfWatchStatus, string> = {
  CANDIDATE: '四条全过',
  BLOCKED: '区间内被排除',
  BLOCKED_F1: '回撤 >50%',
  HIGH: '高位',
  NEUTRAL: '不在区间',
}

const RANK: Record<EtfWatchStatus, number> = {
  CANDIDATE: 0, BLOCKED_F1: 1, BLOCKED: 2, HIGH: 3, NEUTRAL: 4,
}

const fmtAmt = (v: number) => (v >= 1e8 ? `${(v / 1e8).toFixed(1)} 亿` : `${(v / 1e4).toFixed(0)} 万`)
const pct = (v: number) => `${(v * 100).toFixed(1)}%`

export function ScanTable({
  payload, reasonHead = '理由', swipeHint, emptyText = '今天没有行业进入扫描池。',
}: {
  payload: ScanPayload
  reasonHead?: string
  swipeHint?: boolean
  emptyText?: string
}) {
  const rows = [...payload.industries].sort(
    (a, b) => RANK[a.status] - RANK[b.status] || a.dd - b.dd,
  )
  const th = payload.meta.thresholds

  if (rows.length === 0) {
    return <Empty text={emptyText} />
  }

  const cols: Col<EtfWatchIndustry>[] = [
    {
      head: '行业',
      w: '15%',
      render: x => (
        <span className="font-medium">
          {x.industry}
          {x.stale_days != null && x.stale_days > 0 && <span className="ml-1 text-[11px] font-normal text-warning">stale {x.stale_days}d</span>}
        </span>
      ),
    },
    {
      head: '距 250 日高点',
      headCls: 'whitespace-nowrap',
      w: '11%',
      align: 'right',
      render: x => <span className="font-mono tabular-nums">{pct(x.dd)}</span>,
    },
    {
      head: '波动分位',
      headCls: 'whitespace-nowrap',
      w: '9%',
      align: 'right',
      render: x => (
        <span className={cn('font-mono tabular-nums', x.vol_pct != null && x.vol_pct > 0.66 ? 'text-accent' : 'text-secondary')}>
          {x.vol_pct != null ? `${Math.round(x.vol_pct * 100)}%` : '—'}
        </span>
      ),
    },
    {
      head: '同行业最大标的（F6）',
      w: '24%',
      render: x => <span className="text-secondary">{x.best_name ?? '—'}</span>,
    },
    {
      head: '20 日均额',
      headCls: 'whitespace-nowrap',
      w: '10%',
      align: 'right',
      render: x => <span className={cn('font-mono tabular-nums', !x.f6 && 'text-warning')}>{fmtAmt(x.amt20)}</span>,
    },
    { head: '判定', render: x => <Tag tone={statusTone(x.status)}>{statusLabel[x.status]}</Tag>, w: '12%' },
    {
      head: reasonHead,
      w: '19%',
      render: x => <span className="text-[12px] leading-[1.6] text-muted">{x.reasons.length ? x.reasons.join('；') : '—'}</span>,
    },
  ]

  return (
    <DataTable
      cols={cols}
      rows={rows}
      rowKey={x => x.industry}
      swipeHint={swipeHint}
      reason={
        <>
          <b className="text-secondary">F1</b> 位置（深跌 &gt; {Math.abs(th.deep * 100).toFixed(0)}% 直接阻断）·{' '}
          <b className="text-secondary">F2</b> 形态（{payload.meta.vol_convention} &gt; {(th.vol_hi * 100).toFixed(0)}%）·{' '}
          <b className="text-secondary">F3</b> 市场状态（≠ 上行·低波）·{' '}
          <b className="text-secondary">F6</b> 载体容量（同行业最大一只的 20 日均额 ≥ {(th.min_amt20 / 1e4).toFixed(0)} 万）。
          四条全过才是 CANDIDATE；「区间内被排除」= 位置对但形态/容量不过。
        </>
      }
    />
  )
}
