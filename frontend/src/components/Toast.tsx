import { useCallback, useEffect, useState } from 'react'

// ===== 全局 toast 状态 =====
type ToastItem = { id: number; msg: string; kind: 'error' | 'success' }
let _id = 0
const _listeners: Set<(items: ToastItem[]) => void> = new Set()
let _queue: ToastItem[] = []

function _emit() { _listeners.forEach(fn => fn([..._queue])) }

function toast(msg: string, kind: 'error' | 'success' = 'error') {
  // 同一条消息已经在屏幕上：刷新它的存在时间，不再叠一条。
  // 轮询型请求失败时会连续命中同一个错误，去重前 4 秒内能叠出十几条一样的红条，
  // 用户看到的是「页面一直在弹」，而不是「有一个问题一直存在」。
  const dup = _queue.find(t => t.msg === msg && t.kind === kind)
  if (dup) {
    _queue = _queue.filter(t => t.id !== dup.id)
    _queue = [..._queue, { ...dup, id: ++_id }]
    _emit()
    setTimeout(() => {
      _queue = _queue.filter(t => t.msg !== msg || t.kind !== kind)
      _emit()
    }, 4000)
    return
  }
  const item = { id: ++_id, msg, kind }
  _queue = [..._queue, item]
  _emit()
  setTimeout(() => { _queue = _queue.filter(t => t.id !== item.id); _emit() }, 4000)
}

export { toast }

// ===== Toast 容器 — 挂在 Layout 最顶层 =====
export function ToastContainer() {
  const [items, setItems] = useState<ToastItem[]>([])

  const sub = useCallback(() => {
    _listeners.add(setItems)
    return () => { _listeners.delete(setItems) }
  }, [])

  useEffect(sub, [sub])

  if (!items.length) return null

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="false"
      className="fixed bottom-4 right-4 z-[9999] flex flex-col gap-2 pointer-events-none"
    >
      {items.map(t => (
        <div
          key={t.id}
          className={`pointer-events-auto px-4 py-2.5 rounded-lg shadow-lg text-sm font-medium animate-in slide-in-from-bottom-2 fade-in duration-200 ${
            t.kind === 'error'
              ? 'bg-red-500/90 text-white'
              : 'bg-emerald-500/90 text-white'
          }`}
        >
          {t.msg}
        </div>
      ))}
    </div>
  )
}
