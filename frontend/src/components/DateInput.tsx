import { useEffect, useRef, useState } from 'react'
import { CalendarDays } from 'lucide-react'
import { cn } from '@/lib/cn'
// chartRange 是日期工具的家；这里复用同一个"合法 ISO 日期"判据，避免出现第二份定义
import { isValidIso } from '@/lib/chartRange'

/**
 * 日期输入 —— 打字可靠，日历可选。
 *
 * 为什么不用 `<input type="date">` 直接打字：Chromium 的日期输入是**分段编辑器**，
 * Windows 上小键盘（108 键）的数字键进不去那几段（这不是本仓库的 keydown 拦截，
 * 全局只有 ESC 一个监听）。用户的实际反馈就是「用数字键盘输年份输不进去」。
 * 分段编辑器也不接受 `20240901` 这种连写，年份还得靠左右键切段。
 *
 * 所以这里把"打字"这件事故意做笨：一个普通文本框 + 边打边格式化，
 * 小键盘数字与主键盘数字走的是同一条路（都产生 input 事件）。日历没有取消 ——
 * 右侧小按钮调原生 `showPicker()`，只是它不再是键入的唯一入口。
 *
 * 行为约定（每条都有对应的单测）：
 *   - 只收数字，输入 4/6 位时自动补 `-`，最多 8 位；
 *   - **只有在凑成 8 位且是真的日期时才向上报**（2024-02-30 不会报出去）；
 *   - 清空即上报 ''（调用方据此表示"没选"）；
 *   - 失焦时：合法就规整成 YYYY-MM-DD，不合法就退回上一次的合法值（不留半截脏值）。
 */
export function DateInput({
  value,
  onChange,
  min,
  max,
  className,
  focusClassName = 'focus-within:border-accent',
  ariaLabel,
  disabled,
  allowEmpty = true,
  placeholder = 'YYYY-MM-DD',
}: {
  value: string
  onChange: (next: string) => void
  /**
   * 是否允许清空。允许时清空会上报 ''；不允许（区间起止这种必填场景）时
   * 清空在失焦后退回原值 —— 否则会出现"框里空着、父组件的区间还是老值"，
   * 下一次渲染又把老值填回来，用户以为自己的清空没生效。
   */
  allowEmpty?: boolean
  min?: string
  max?: string
  /** 外层方框的样式（沿用各页原有的 input className，视觉不变） */
  className?: string
  /** 聚焦态样式（原 className 里的 focus: 前缀对 div 无效，所以单独给） */
  focusClassName?: string
  ariaLabel?: string
  disabled?: boolean
  placeholder?: string
}) {
  const [draft, setDraft] = useState(value)
  const [invalid, setInvalid] = useState(false)
  const pickerRef = useRef<HTMLInputElement>(null)

  // 外部改了值（切股票、点预设、恢复到上次区间）→ 草稿跟着走
  useEffect(() => {
    setDraft(value)
    setInvalid(false)
  }, [value])

  const emitIfComplete = (text: string) => {
    if (text === '') {
      if (allowEmpty) {
        setInvalid(false)
        onChange('')
      }
      return
    }
    if (text.length === 10 && isValidIso(text)) {
      setInvalid(false)
      onChange(text)
      return
    }
    setInvalid(text.length > 0)
  }

  const handleInput = (raw: string) => {
    // 先认"带分隔符的粘贴"：2024/9/1、2024.09.01 都要变成 2024-09-01。
    // 只按位数切会把 2024.9.1 读成 2024-91（6 位），那不是日期。
    const parts = /^(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*$/.exec(raw)
    if (parts) {
      const iso = `${parts[1]}-${parts[2].padStart(2, '0')}-${parts[3].padStart(2, '0')}`
      if (isValidIso(iso)) {
        setDraft(iso)
        setInvalid(false)
        onChange(iso)
        return
      }
    }
    const digits = raw.replace(/\D/g, '').slice(0, 8)
    let text = digits
    if (digits.length > 6) text = `${digits.slice(0, 4)}-${digits.slice(4, 6)}-${digits.slice(6)}`
    else if (digits.length > 4) text = `${digits.slice(0, 4)}-${digits.slice(4)}`
    setDraft(text)
    emitIfComplete(text)
  }

  const commitOrRevert = () => {
    if (draft === '') {
      if (!allowEmpty) setDraft(value)
      return
    }
    if (draft.length === 10 && isValidIso(draft)) {
      setDraft(draft)
      setInvalid(false)
      if (draft !== value) onChange(draft)
      return
    }
    setDraft(value)     // 半截/非法：退回上一次的合法值，别把脏值留在框里
    setInvalid(false)
  }

  const openPicker = () => {
    const el = pickerRef.current
    if (!el || disabled) return
    try {
      // Chrome 99+/Edge：直接从隐藏的原生控件拉起日历
      ;(el as HTMLInputElement & { showPicker?: () => void }).showPicker?.()
    } catch {
      el.focus()
    }
  }

  return (
    <div
      className={cn(
        'relative inline-flex min-w-0 items-center gap-1',
        'focus-within:outline-none',
        focusClassName,
        disabled && 'opacity-50',
        invalid ? 'border-danger/60' : undefined,
        className,
      )}
      title={invalid ? '日期不完整或不合法（例：2024-09-01）' : undefined}
    >
      <input
        type="text"
        inputMode="numeric"
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        aria-label={ariaLabel}
        placeholder={placeholder}
        value={draft}
        onChange={e => handleInput(e.target.value)}
        onBlur={commitOrRevert}
        onKeyDown={e => {
          if (e.key === 'Enter') { e.preventDefault(); commitOrRevert(); (e.target as HTMLInputElement).blur() }
          if (e.key === 'Escape') { setDraft(value); setInvalid(false) }
        }}
        className="min-w-0 flex-1 bg-transparent font-mono tabular-nums outline-none placeholder:text-muted/60"
      />
      <button
        type="button"
        tabIndex={-1}
        onMouseDown={e => e.preventDefault()}   // 别把输入框的焦点抢走
        onClick={openPicker}
        disabled={disabled}
        aria-label="打开日历"
        title="打开日历"
        className="shrink-0 text-muted transition-colors hover:text-foreground disabled:opacity-40"
      >
        <CalendarDays className="h-3 w-3" />
      </button>
      {/* 原生控件只负责弹日历：它不可键入，所以永远不会成为输入路径 */}
      <input
        ref={pickerRef}
        type="date"
        tabIndex={-1}
        aria-hidden="true"
        value={value}
        min={min}
        max={max}
        onChange={e => { if (e.target.value) { onChange(e.target.value); setDraft(e.target.value) } }}
        className="pointer-events-none absolute inset-0 h-full w-full opacity-0"
      />
    </div>
  )
}
