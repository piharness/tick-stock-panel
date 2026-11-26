// @vitest-environment jsdom
// 筹码久期页 —— 只测对读者有约束的事：
// 1. 首屏三个摘要可读（旧页的零、新对象、四条已跑）；
// 2. 退化表三行、与新读法表八行，行数与 data.ts 一致；
// 3. 三层结构三张卡、久期柱八根、热力图两张；
// 4. 预测状态必须与跑了多少条一致——H1 部分通过，H2/H3/H4 失败，其余三条未测；
// 5. 旧页的冻结读数仍在（0.689 / −26），不能被新页悄悄删掉。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { ChipTheory } from './ChipTheory'
import { AGE_YEARS, DEGENERACY, H1_CELLS, H2_COMPS, H3_CELLS, H3_DOSE, H4_COND, H4_QUINTILES, HEATMAPS, LAYERS, PREDICTIONS, REINTERPRET } from './data'

let host: HTMLDivElement
let root: Root

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

beforeEach(() => {
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  act(() => { root.render(<ChipTheory />) })
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

it('首屏三个摘要全部可读', () => {
  const text = host.textContent ?? ''
  expect(text).toContain('均值场极限')
  expect(text).toContain('S(p, a)')
  expect(text).toContain('四条已跑')
  expect(text).toContain('一通过 · 三失败')
  expect(host.querySelectorAll('#degeneracy').length).toBe(1)
})

it('退化表三行，旧页的 −136 仍在', () => {
  const rows = host.querySelectorAll('#degeneracy tbody tr')
  expect(rows.length).toBe(DEGENERACY.length)
  const text = host.querySelector('#degeneracy')?.textContent ?? ''
  expect(text).toContain('0.689')
  expect(text).toContain('−26')
})

it('三层结构三张卡', () => {
  const cards = host.querySelectorAll('[data-testid="layer-card"]')
  expect(cards.length).toBe(LAYERS.length)
  const text = host.querySelector('#layers')?.textContent ?? ''
  expect(text).toContain('S(p, a)')
  expect(text).toContain('5,650')
})

it('久期柱八根，首尾是 98 与 39', () => {
  const bars = host.querySelectorAll('[data-testid="age-bar"]')
  expect(bars.length).toBe(AGE_YEARS.length)
  expect(bars[0].textContent).toContain('98')
  expect(bars[bars.length - 1].textContent).toContain('39')
})

it('两张热力图，且都带久期读数', () => {
  const maps = host.querySelectorAll('[data-testid="age-heatmap"]')
  expect(maps.length).toBe(HEATMAPS.length)
  for (const m of maps) expect(m.textContent).toContain('D ')
})

it('七个预测：H1 部分通过，H2/H3/H4 失败，其余三条未测', () => {
  const rows = host.querySelectorAll('#predictions tbody tr')
  expect(rows.length).toBe(PREDICTIONS.length)
  const text = host.querySelector('#predictions')?.textContent ?? ''
  expect(text).toContain('部分通过 / 已分解')
  expect(text).toContain('未测')
  const st = (id: string) => PREDICTIONS.find(p => p.id === id)?.status ?? ''
  expect(st('H1')).toContain('部分通过')
  expect(st('H2')).toContain('合成失败')
  expect(st('H3')).toContain('雪崩格反向')
  expect(st('H4')).toContain('冲击深度混淆')
  expect(PREDICTIONS.filter(p => !['H1', 'H2', 'H3', 'H4'].includes(p.id)).every(p => p.status === '未测')).toBe(true)
})

it('H3 结果：四格里雪崩格反向，置换与剂量反应都在', () => {
  const text = host.querySelector('#h3')?.textContent ?? ''
  expect(text).toContain('雪崩格反向')
  expect(text).toContain('+116.0bp')
  expect(text).toContain('−166.8bp')
  const tables = host.querySelectorAll('#h3 tbody tr')
  expect(tables.length).toBe(H3_CELLS.length + H3_DOSE.length + 7)
})

it('H4 结果：熵的退化、五分位与三分位', () => {
  const text = host.querySelector('#h4')?.textContent ?? ''
  expect(text).toContain('0.989')
  expect(text).toContain('−3.18pp')
  const q = host.querySelectorAll('#h4 tbody tr')
  expect(q.length).toBe(H4_QUINTILES.length + H4_COND.length)
})

it('H2 结果：七个分项与合成失败', () => {
  const rows = host.querySelectorAll('#h2 tbody tr')
  expect(rows.length).toBe(H2_COMPS.length)
  const text = host.querySelector('#h2')?.textContent ?? ''
  expect(text).toContain('合成失败')
  expect(text).toContain('分项反转')
  expect(text).toContain('−0.0145')
})

it('H1 结果：五档 D×T 表与两条主效应', () => {
  const rows = host.querySelectorAll('#h1 tbody tr')
  expect(rows.length).toBe(H1_CELLS.length)
  const text = host.querySelector('#h1')?.textContent ?? ''
  expect(text).toContain('部分通过 / 已分解')
  expect(text).toContain('−0.85')
  expect(text).toContain('−15.82')
})

it('旧结论新读法八行', () => {
  const rows = host.querySelectorAll('#reinterpret tbody tr')
  expect(rows.length).toBe(REINTERPRET.length)
})
