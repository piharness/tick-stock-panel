// @vitest-environment jsdom
/**
 * DateInput —— 打字这件事必须可靠。
 *
 * 报障原文：「用 108 键键盘数字输入年份时，无法正常输入」。原生 `<input type="date">`
 * 是分段编辑器，Windows 上小键盘数字进不去（全局也没有 keydown 拦截，实测只有 ESC
 * 一个监听），而且它不接受 `20240901` 这种连写。所以这里把判据落在三件事上：
 *
 *   1. **连写数字必须能变成日期**（小键盘按一串 2024 0 9 0 1 就是这个形状）；
 *   2. 没有凑满 8 位**不上报**（不能让父组件拿着 `2024-0` 去查数据）；
 *   3. 非法日期（2024-02-30）既不显示成合法值，也不上报；失焦退回上一次的合法值。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DateInput } from './DateInput'

let host: HTMLDivElement
let root: Root
const onChange = vi.fn()

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  onChange.mockReset()
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

async function render(value = '') {
  await act(async () => {
    root.render(<DateInput value={value} onChange={onChange} ariaLabel="起点" />)
  })
  return host.querySelector('input[type="text"]') as HTMLInputElement
}

/** 受控 input 必须走原生 setter，直接改 .value React 收不到 */
const nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')!.set!

async function type(input: HTMLInputElement, text: string) {
  await act(async () => {
    nativeSet.call(input, text)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

async function blur(input: HTMLInputElement) {
  // React 17+ 的 onBlur 走的是 root 上的 focusout（会冒泡），不是 blur
  await act(async () => {
    input.dispatchEvent(new FocusEvent('focusout', { bubbles: true }))
  })
}

describe('键入：连写数字就是日期', () => {
  it('小键盘连按 2 0 2 4 0 9 0 1 → 2024-09-01 并上报', async () => {
    const input = await render('')
    // 逐位输入：这正是用户按小键盘的过程
    for (const step of ['2', '20', '202', '2024', '20240', '202409', '2024090', '20240901']) {
      await type(input, step)
    }
    expect(input.value).toBe('2024-09-01')
    expect(onChange).toHaveBeenLastCalledWith('2024-09-01')
  })

  it('年份只输到 2024 时：显示 2024，但不上报（父组件不该拿半截去查）', async () => {
    const input = await render('')
    await type(input, '2024')
    expect(input.value).toBe('2024')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('粘贴带分隔符的日期也认：2024/09/01、2024.9.1', async () => {
    const input = await render('')
    await type(input, '2024/09/01')
    expect(onChange).toHaveBeenLastCalledWith('2024-09-01')

    onChange.mockReset()
    await type(input, '2024.9.1')
    expect(onChange).toHaveBeenLastCalledWith('2024-09-01')
  })

  it('非法日期不上报：2024-02-30 会被标红，且不产生 onChange', async () => {
    const input = await render('')
    await type(input, '20240230')
    expect(input.value).toBe('2024-02-30')   // 让人看见自己输的是哪天
    expect(onChange).not.toHaveBeenCalled()
    expect(host.querySelector('.border-danger\\/60')).not.toBeNull()
  })

  it('第 9 位被丢掉（不会变成 2024-09-011）', async () => {
    const input = await render('')
    await type(input, '202409011')
    expect(input.value).toBe('2024-09-01')
  })
})

describe('失焦与清空：不留半截脏值', () => {
  it('半截输入失焦 → 退回上一次的合法值', async () => {
    const input = await render('2026-11-13')
    await type(input, '2024')
    expect(input.value).toBe('2024')
    await blur(input)
    expect(input.value).toBe('2026-11-13')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('清空 → 上报空串（调用方据此表示"没选日期"）', async () => {
    const input = await render('2026-11-13')
    await type(input, '')
    expect(onChange).toHaveBeenLastCalledWith('')
  })

  it('外部换值（切股票/点预设）时草稿跟着走', async () => {
    const input = await render('2024-09-01')
    expect(input.value).toBe('2024-09-01')
    await act(async () => {
      root.render(<DateInput value="2025-01-02" onChange={onChange} ariaLabel="起点" />)
    })
    expect(input.value).toBe('2025-01-02')
  })

  it('Enter 提交并失焦；Escape 放弃当前草稿', async () => {
    const input = await render('2024-09-01')
    await type(input, '20250102')
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    })
    expect(onChange).toHaveBeenLastCalledWith('2025-01-02')

    onChange.mockReset()
    await type(input, '2023')
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(input.value).toBe('2024-09-01')   // Escape 回到外部值
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('日历不是唯一入口，但也没被砍掉', () => {
  it('右侧有一个可点的日历按钮，只负责拉起原生 picker', async () => {
    await render('2024-09-01')
    const btn = host.querySelector('button[aria-label="打开日历"]') as HTMLButtonElement
    expect(btn).not.toBeNull()
    const picker = host.querySelector('input[type="date"]') as HTMLInputElement
    expect(picker).not.toBeNull()
    // 原生控件不可键入（tabIndex=-1 + pointer-events-none）：它不会成为第二条输入路径
    expect(picker.tabIndex).toBe(-1)
    expect(picker.className).toContain('pointer-events-none')
  })
})

describe('必填场景：不许留在空框', () => {
  it('allowEmpty={false} 时清空不上报，失焦退回原值', async () => {
    await act(async () => {
      root.render(<DateInput value="2026-11-13" onChange={onChange} allowEmpty={false} ariaLabel="起点" />)
    })
    const input = host.querySelector('input[type="text"]') as HTMLInputElement
    await type(input, '')
    expect(onChange).not.toHaveBeenCalled()
    expect(input.value).toBe('')
    await blur(input)
    expect(input.value).toBe('2026-11-13')
  })
})
