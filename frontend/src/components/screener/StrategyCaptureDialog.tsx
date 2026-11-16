import { useState, useEffect, useCallback, useRef } from 'react'
import { Loader2, Radar, X } from 'lucide-react'
import { Modal } from '@/components/Modal'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

interface Props { onClose: () => void }

type CaptureStatus = 'idle' | 'running' | 'pass' | 'reject' | 'failed'

export function StrategyCaptureDialog({ onClose }: Props) {
  const [text, setText] = useState('')
  const [status, setStatus] = useState<CaptureStatus>('idle')
  const [reasons, setReasons] = useState<string[]>([])
  const [error, setError] = useState('')
  const [strategyId, setStrategyId] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const pollTimer = useRef<number | undefined>(undefined)

  const stopPoll = useCallback(() => {
    if (pollTimer.current !== undefined) { window.clearInterval(pollTimer.current); pollTimer.current = undefined }
  }, [])

  useEffect(() => stopPoll, [stopPoll])

  const poll = useCallback(async (id: string) => {
    try {
      const res = await api.strategyCaptureGet(id)
      const meta = res?.meta ?? {}
      const status = meta.status as CaptureStatus
      setStatus(status)
      setStrategyId(meta.strategy_id ?? '')
      setReasons(Array.isArray(meta.reasons) ? meta.reasons : [])
      setError(meta.error ?? '')
      if (status === 'pass' || status === 'reject' || status === 'failed') stopPoll()
    } catch { /* 轮询失败静默, 下一轮重试 */ }
  }, [stopPoll])

  const submit = useCallback(async () => {
    if (!text.trim() || submitting) return
    setSubmitting(true)
    setError('')
    setStatus('running')
    try {
      const res = await api.strategyCaptureSubmit(text.trim())
      // 轮询进度
      pollTimer.current = window.setInterval(() => void poll(res.uuid), 3000)
      void poll(res.uuid)
    } catch (e) {
      setStatus('failed')
      setError(e instanceof Error ? e.message : '提交失败，请重试')
    } finally {
      setSubmitting(false)
    }
  }, [text, submitting, poll])

  return (
    <Modal ariaLabel="策略自动捕获" onClose={onClose} closeOnBackdrop
      panelClassName="flex w-[94vw] max-w-[620px] flex-col overflow-hidden rounded-card border border-border bg-surface shadow-2xl">
      <header className="flex shrink-0 items-start justify-between gap-3 border-b border-border px-4 py-3 sm:px-5">
        <div>
          <h2 className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Radar className="h-4 w-4 text-accent" />策略自动捕获
          </h2>
          <p className="mt-1 text-[11px] leading-5 text-muted">
            粘贴淘县/淘股吧等社区战法文本，自动生成策略并跑五道门审计。通过则自动发布入池，未通过则归档。
          </p>
        </div>
        <button type="button" aria-label="关闭" onClick={onClose} className="rounded-btn p-1.5 text-muted hover:bg-elevated hover:text-foreground">
          <X className="h-4 w-4" />
        </button>
      </header>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3 sm:px-5">
        <textarea
          aria-label="社区战法文本"
          placeholder="粘贴社区战法描述，例如：首板接力，昨日首板今日竞价高开买入……"
          value={text}
          onChange={e => setText(e.target.value)}
          disabled={status === 'running'}
          rows={6}
          className="w-full resize-none rounded-input border border-border bg-base p-3 text-xs leading-5 focus:border-accent focus:outline-none disabled:opacity-50"
        />

        {status === 'running' && (
          <div className="flex items-center gap-2 rounded-btn border border-accent/25 bg-accent/5 px-3 py-2 text-xs text-accent">
            <Loader2 className="h-4 w-4 animate-spin" />
            正在处理（生成 → 回测 → 审计）…{strategyId ? ` 已生成策略 ${strategyId}` : ''}
          </div>
        )}

        {status === 'pass' && (
          <div className="rounded-btn border border-teal-500/25 bg-teal-500/5 px-3 py-2 text-xs text-teal-500">
            五道门通过，策略 {strategyId} 已自动发布并加入策略池。
          </div>
        )}

        {status === 'reject' && (
          <div className="rounded-btn border border-amber-500/25 bg-amber-500/5 px-3 py-2 text-xs text-amber-500">
            五道门未通过，策略已归档（不入池）。拒绝理由：
            {reasons.length > 0 ? (
              <ul className="mt-1 list-inside list-disc space-y-0.5">{reasons.map(r => <li key={r}>{r}</li>)}</ul>
            ) : <span className="ml-1">（未返回具体理由）</span>}
          </div>
        )}

        {status === 'failed' && (
          <div className="rounded-btn border border-danger/25 bg-danger/5 px-3 py-2 text-xs text-danger">
            处理失败：{error || '未知错误'}
          </div>
        )}

        <p className="text-[10px] leading-5 text-muted">
          提示：自动捕获的预期产出是「大量未通过 + 极少数通过」，这是正确行为。五道门是拒绝器不是验证器——通过只代表「没被证伪」，不代表有稳定超额。
        </p>
      </div>

      <footer className="flex shrink-0 items-center justify-end gap-2 border-t border-border px-4 py-3 sm:px-5">
        <button type="button" onClick={onClose} className="inline-flex h-8 items-center rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated">关闭</button>
        <button
          type="button"
          disabled={!text.trim() || status === 'running' || submitting}
          onClick={() => void submit()}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-btn border px-3 text-xs font-medium text-white disabled:opacity-40',
            'border-blue-600 bg-blue-600 hover:bg-blue-700',
          )}
        >
          {submitting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          开始捕获
        </button>
      </footer>
    </Modal>
  )
}
