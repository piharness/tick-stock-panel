// @vitest-environment jsdom
/**
 * Toast 的去重规则：轮询型接口失败时会连续命中同一个错误。
 * 去重前 4 秒内能叠出十几条一模一样的红条——用户看到的是「页面一直在弹」，
 * 而真实信息是「有一个问题一直存在」。这两件事必须长得不一样。
 */
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { toast, ToastContainer } from './Toast'

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  host = document.createElement('div')
  document.body.append(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
})

const count = (needle: string) => (host.textContent ?? '').split(needle).length - 1

it('同一条消息重复触发只保留一条；不同消息各自保留', async () => {
  await act(async () => root.render(<ToastContainer />))

  const msg = '指数日K在 2026-11-13 有未完成修补或重算'
  await act(async () => { toast(msg); toast(msg); toast(msg) })
  expect(count(msg)).toBe(1)

  await act(async () => { toast('另一个错误') })
  expect(count(msg)).toBe(1)
  expect(count('另一个错误')).toBe(1)
})
