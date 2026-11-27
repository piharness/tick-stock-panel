// @vitest-environment jsdom
// 筹码页（卷四）—— 只测对读者有约束的事：
// 1. 首屏四个摘要可读（投影的零 / 状态全零 / 约束的发起 / 存量首测零）；
// 2. 卷四各节都在：A 4 卡 / B 3 行 / C 16 行 / D 9 行 / D2 8+5 行 / D3 图+6 行 / D4 6 行 / D5 3+7 行 / E 3 卡 / F 4+3 行 / F2 4+5 行 / F3 3+5 行 / G 4 条 / H 6 条；
// 3. 关键数字（+266.2 / −0.1 / +6.0 / −13.7 / +154.1 / +763.5 / +126.0 / −97.2 / −169.3 / 0.986）必须在页面上；
// 4. 卷三的记录不能被新页悄悄删掉（0.689 / −26 / +116.0 / −166.8 / −0.976）；
// 5. 版本表必须写到卷四。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it } from 'vitest'
import { ChipTheory } from './ChipTheory'
import {
  AGE_YEARS, AP_ATTR, AP_FIX, AP_LEDGER, AP_MTM, AP_PRED, AUDIT_FIX, AUDIT_PROOF, CC,
  BOOK_GAPS, BOOK_ROWS, BOOK_V2, BOOK_V3,
  CONTRAST, COVERAGE, DEGENERACY, DIMENSIONS, E_PRED,
  EVENT_READ, G4_ROWS,
  H1_CELLS, H2_COMPS, H3_CELLS, H3_DOSE, H4_COND, H4_QUINTILES, H567_ROWS, H8_CELLS, HEATMAPS, KILL4, KILL5, LAYERS,
  MECHANISM, MECH_INTER, ONTOLOGY, PREDICTIONS, REINTERPRET, TRANSFER, TRANSFER_RESULT, TYPE_BIG, TYPE_SPLIT,
  UNIFY, VERSIONS, VOLUMES, SECTION_IDS,
} from './data'

let host: HTMLDivElement
let root: Root

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

// jsdom 不实现 scrollIntoView；导航点击会走到 scrollToId，这里补一个空实现。
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {}
}

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

it('首屏：审计标题与四个摘要', () => {
  const text = host.textContent ?? ''
  expect(text).toContain('未付账单，谁替它付钱')
  expect(text).toContain('投影的零')
  expect(text).toContain('状态全零')
  expect(text).toContain('负债端的活口')
  expect(text).toContain('+525bp 反弹')
  expect(text).toContain('存量簿首测')
  expect(host.querySelectorAll('#ontology').length).toBe(1)
})

it('0 审计：未来函数、作废范围、修正后反转', () => {
  const rows = host.querySelectorAll('#audit tbody tr')
  expect(rows.length).toBe(AUDIT_PROOF.length + AUDIT_FIX.length)
  const text = host.querySelector('#audit')?.textContent ?? ''
  expect(text).toContain('0.269')
  expect(text).toContain('65.65')
  expect(text).toContain('+525.3bp')
  expect(text).toContain('−276.0bp')
  expect(text).toContain('作废范围')
  expect(text).toContain('P1')
})

it('5A 换表：六行，资产端 vs 负债端', () => {
  const rows = host.querySelectorAll('#ledger tbody tr')
  expect(rows.length).toBe(AP_LEDGER.length)
  const text = host.querySelector('#ledger')?.textContent ?? ''
  expect(text).toContain('面值')
  expect(text).toContain('已进入价格')
  expect(text).toContain('资产按市价卖')
})

it('5B 定律：三类盯市，融资与质押被同一条定律收掉', () => {
  const cards = host.querySelectorAll('[data-testid="ap-mtm-card"]')
  expect(cards.length).toBe(AP_MTM.length)
  const text = host.querySelector('#mtm')?.textContent ?? ''
  expect(text).toContain('逐日盯市')
  expect(text).toContain('事件盯市')
  expect(text).toContain('没被追保的那部分杠杆')
})

it('5C 七类：十六个读数被重排，不再新增参数', () => {
  const rows = host.querySelectorAll('#attr tbody tr')
  expect(rows.length).toBe(AP_ATTR.length)
  const text = host.querySelector('#attr')?.textContent ?? ''
  expect(text).toContain('分布类')
  expect(text).toContain('事件类')
  expect(text).toContain('已支付类')
  expect(text).toContain('一次计价错误')
})

it('5D 首测：三条轴 + P1 四条，深跌反弹必须在页面上', () => {
  const rows = host.querySelectorAll('#probe tbody tr')
  expect(rows.length).toBe(AP_FIX.age.length + AP_FIX.pledge.length + AP_FIX.balance.length + 4)
  expect(host.querySelectorAll('[data-testid="ap-fix-figure"]').length).toBe(1)
  const text = host.querySelector('#probe')?.textContent ?? ''
  expect(text).toContain('+755.5bp')
  expect(text).toContain('−401 → −716bp')
  expect(text).toContain('−492 → −731bp')
  expect(text).toContain('−146.9bp')
  expect(text).toContain('无悬崖')
  expect(text).toContain('困境深度 × 救助概率')
})

it('5E 五条前瞻 + P2 首测：国资/民企两组都在', () => {
  const rows = host.querySelectorAll('#pred5 tbody tr')
  expect(rows.length).toBe(AP_PRED.length + 3)
  const text = host.querySelector('#pred5')?.textContent ?? ''
  expect(text).toContain('锚的残差')
  expect(text).toContain('解除方式')
  expect(text).toContain('支付归零')
  expect(text).toContain('制度切换')
  expect(text).toContain('不可识别')
  expect(text).toContain('+1269.3')
  expect(text).toContain('43 只')
})

it('5G 杀死卷五：六条，且承认只有一条硬证据', () => {
  expect(host.querySelectorAll('#kill5 li').length).toBe(KILL5.length)
  const text = host.querySelector('#kill5')?.textContent ?? ''
  expect(text).toContain('重述不是证据')
  expect(text).toContain('整卷删除')
})

it('5F 对照：控制权转移的符号反转，现金/表决权不显著', () => {
  const rows = host.querySelectorAll('#control tbody tr')
  expect(rows.length).toBe(CC.rows.length)
  expect(host.querySelectorAll('[data-testid="cc-figure"]').length).toBe(1)
  const text = host.querySelector('#control')?.textContent ?? ''
  expect(text).toContain('−10.3%')
  expect(text).toContain('+0.1%')
  expect(text).toContain('−11.1pp')
  expect(text).toContain('−0.87')
  expect(text).toContain('右尾')
  expect(text).toContain('+525bp')
})

it('A 三次换对象：四张卡，S(p) / S(p,a) / Φ(p) 都在', () => {
  const cards = host.querySelectorAll('[data-testid="ontology-card"]')
  expect(cards.length).toBe(ONTOLOGY.length)
  const text = host.querySelector('#ontology')?.textContent ?? ''
  expect(text).toContain('S(p)')
  expect(text).toContain('S(p, a)')
  expect(text).toContain('Φ(p)')
  expect(text).toContain('换的是坐标')
})

it('B 三缺一：符号 / 日期 / 对手方三行', () => {
  const rows = host.querySelectorAll('#selection tbody tr')
  expect(rows.length).toBe(DIMENSIONS.length)
  const text = host.querySelector('#selection')?.textContent ?? ''
  expect(text).toContain('符号')
  expect(text).toContain('日期')
  expect(text).toContain('对手方')
  expect(text).toContain('持仓者选择留下的痕迹')
})

it('C 一张表：十六行，状态与发起分开', () => {
  const rows = host.querySelectorAll('#record tbody tr')
  expect(rows.length).toBe(UNIFY.length)
  expect(UNIFY.filter(r => r.form === '状态').length).toBeGreaterThan(UNIFY.filter(r => r.form === '发起').length)
  const text = host.querySelector('#record')?.textContent ?? ''
  expect(text).toContain('零个稳定的收益符号')
  expect(text).toContain('幅度只能估符号')
})

it('D 存量簿：九行，v1 与 v2 的数字同屏', () => {
  const rows = host.querySelectorAll('#book tbody tr')
  expect(rows.length).toBe(BOOK_ROWS.length)
  const text = host.querySelector('#book')?.textContent ?? ''
  expect(text).toContain('+266.2bp')
  expect(text).toContain('−0.1bp')
  expect(text).toContain('+6.0bp')
  expect(text).toContain('−13.7bp')
  expect(text).toContain('+763.5bp')
  expect(text).toContain('−34.6bp')
  expect(text).toContain('倒 U 变换')
})

it('D2 测量缺口：八个缺口 + 五个变体，G1 是主因', () => {
  const rows = host.querySelectorAll('#book-gap tbody tr')
  expect(rows.length).toBe(BOOK_GAPS.length + BOOK_V2.length)
  const text = host.querySelector('#book-gap')?.textContent ?? ''
  expect(text).toContain('G1')
  expect(text).toContain('平仓线未复权')
  expect(text).toContain('v2b')
  expect(text).toContain('归零')
  expect(text).toContain('事件侧不受影响')
  expect(BOOK_V2.find(v => v.variant === 'v2b')?.verdict).toBe('归零')
})

it('D3 对比图：存量零 vs 事件正，六行读数', () => {
  expect(host.querySelectorAll('[data-testid="contrast-figure"]').length).toBe(1)
  expect(host.querySelectorAll('#contrast tbody tr').length).toBe(CONTRAST.length)
  const text = host.querySelector('#contrast')?.textContent ?? ''
  expect(text).toContain('存量零')
  expect(text).toContain('事件正')
  expect(text).toContain('零 vs 正')
  expect(text).toContain('+525.3')
})

it('D4 G4 敏感性：六档补寿命，一补就偏正', () => {
  expect(host.querySelectorAll('#g4 tbody tr').length).toBe(G4_ROWS.length)
  const text = host.querySelector('#g4')?.textContent ?? ''
  expect(text).toContain('+126.0')
  expect(text).toContain('一补就偏正')
  expect(text).toContain('补多久不重要')
})

it('D5 公告口径：补 2005–2018 后还是零，但分母只有重要股东', () => {
  expect(host.querySelectorAll('#book-notice tbody tr').length).toBe(BOOK_V3.length + COVERAGE.length)
  const text = host.querySelector('#book-notice')?.textContent ?? ''
  expect(text).toContain('45,349')
  expect(text).toContain('65.7%')
  expect(text).toContain('0.986')
  expect(text).toContain('+15.22')
  expect(text).toContain('重要股东子集上的零')
  expect(text).toContain('重复披露')
  expect(BOOK_V3.find(r => r.tag === 'sn3')?.verdict).toContain('零')
})

it('F2 类型拆解：符号不可识别，大样本仍不过 2', () => {
  expect(host.querySelectorAll('#type-split tbody tr').length).toBe(TYPE_SPLIT.length + TYPE_BIG.length)
  const text = host.querySelector('#type-split')?.textContent ?? ''
  expect(text).toContain('符号不可识别')
  expect(text).toContain('−97.2')
  expect(text).toContain('+41.4')
  expect(text).toContain('−169.3')
  expect(text).toContain('−343.7')
  expect(text).toContain('regime')
  expect(text).toContain('不能升级')
})

it('F 迁移：形式可迁移，符号不迁移；大宗半过', () => {
  const rows = host.querySelectorAll('#transfer tbody tr')
  expect(rows.length).toBe(TRANSFER.length + TRANSFER_RESULT.length)
  const text = host.querySelector('#transfer')?.textContent ?? ''
  expect(text).toContain('形式可迁移，符号不迁移')
  expect(text).toContain('深折价')
  expect(text).toContain('+154.1bp')
  expect(text).toContain('按解除方式重排')
  expect(TRANSFER_RESULT.find(r => r.verdict.includes('已结清'))).toBeTruthy()
  expect(E_PRED.some(p => p.id === 'E4')).toBe(false)
  expect(host.querySelectorAll('#next tbody tr').length).toBe(E_PRED.length)
})

it('F3 机制：质押零重叠，定增共线，负号在 neither', () => {
  expect(host.querySelectorAll('#mechanism tbody tr').length).toBe(MECHANISM.length + MECH_INTER.length)
  const text = host.querySelector('#mechanism')?.textContent ?? ''
  expect(text).toContain('质押不是驱动')
  expect(text).toContain('0 件')
  expect(text).toContain('−350.6')
  expect(text).toContain('+996.3')
  expect(text).toContain('−585.2')
  expect(text).toContain('不是稳定驱动')
})

it('E 事件：三张读法卡，从强平改读为救助', () => {
  const cards = host.querySelectorAll('[data-testid="event-card"]')
  expect(cards.length).toBe(EVENT_READ.length)
  const text = host.querySelector('#event')?.textContent ?? ''
  expect(text).toContain('不是强平，是救助')
  expect(text).toContain('救助期权')
  expect(text).toContain('清算 vs 救助')
  expect(text).toContain('预注册撤回')
})

it('G 四条前瞻预测全部可杀死', () => {
  const rows = host.querySelectorAll('#next tbody tr')
  expect(rows.length).toBe(E_PRED.length)
  const text = host.querySelector('#next')?.textContent ?? ''
  expect(text).toContain('π(发起 | 私有状态)')
  expect(text).toContain('E1 + E2 + E4')
})

it('G 卷四杀死条件六条', () => {
  const items = host.querySelectorAll('#kill4 ol > li')
  expect(items.length).toBe(KILL4.length)
  const text = host.querySelector('#kill4')?.textContent ?? ''
  expect(text).toContain('迁移失败')
  expect(text).toContain('发送者无关')
})

it('卷三记录：退化表三行，旧页数字仍在', () => {
  const rows = host.querySelectorAll('#degeneracy tbody tr')
  expect(rows.length).toBe(DEGENERACY.length)
  const text = host.querySelector('#degeneracy')?.textContent ?? ''
  expect(text).toContain('0.689')
  expect(text).toContain('−26')
})

it('三层结构三张卡、久期柱八根、热力图两张', () => {
  expect(host.querySelectorAll('[data-testid="layer-card"]').length).toBe(LAYERS.length)
  const bars = host.querySelectorAll('[data-testid="age-bar"]')
  expect(bars.length).toBe(AGE_YEARS.length)
  expect(bars[0].textContent).toContain('98')
  expect(bars[bars.length - 1].textContent).toContain('39')
  const maps = host.querySelectorAll('[data-testid="age-heatmap"]')
  expect(maps.length).toBe(HEATMAPS.length)
  for (const m of maps) expect(m.textContent).toContain('D ')
})

it('七个预测仍全部跑完', () => {
  const rows = host.querySelectorAll('#predictions tbody tr')
  expect(rows.length).toBe(PREDICTIONS.length)
  const st = (id: string) => PREDICTIONS.find(p => p.id === id)?.status ?? ''
  expect(st('H1')).toContain('部分通过')
  expect(st('H2')).toContain('合成失败')
  expect(st('H3')).toContain('雪崩格反向')
  expect(st('H4')).toContain('冲击深度混淆')
  expect(st('H5')).toContain('方向相反')
  expect(st('H6')).toContain('无阈值')
  expect(st('H7')).toContain('比值是噪声')
  expect(PREDICTIONS.every(p => p.status !== '未测')).toBe(true)
})

it('H3 / H4 / H1 / H2 的冻结读数仍在', () => {
  expect(host.querySelector('#h3')?.textContent).toContain('+116.0bp')
  expect(host.querySelector('#h3')?.textContent).toContain('−166.8bp')
  expect(host.querySelectorAll('#h3 tbody tr').length).toBe(H3_CELLS.length + H3_DOSE.length + 7)
  expect(host.querySelector('#h4')?.textContent).toContain('0.989')
  expect(host.querySelector('#h4')?.textContent).toContain('−3.18pp')
  expect(host.querySelectorAll('#h4 tbody tr').length).toBe(H4_QUINTILES.length + H4_COND.length)
  expect(host.querySelector('#h1')?.textContent).toContain('−0.85')
  expect(host.querySelector('#h1')?.textContent).toContain('−15.82')
  expect(host.querySelectorAll('#h1 tbody tr').length).toBe(H1_CELLS.length)
  expect(host.querySelector('#h2')?.textContent).toContain('−0.0145')
  expect(host.querySelectorAll('#h2 tbody tr').length).toBe(H2_COMPS.length)
})

it('H5–H9 / H11 的记录仍在', () => {
  expect(host.querySelectorAll('#h567 tbody tr').length).toBe(H567_ROWS.length)
  expect(host.querySelector('#h567')?.textContent).toContain('换手/久期是风险因子')
  expect(host.querySelectorAll('#h8 tbody tr').length).toBe(H8_CELLS.length)
  expect(host.querySelector('#h8')?.textContent).toContain('没有雪崩')
  expect(host.querySelector('#h9')?.textContent).toContain('-0.442')
  expect(host.querySelector('#h11')?.textContent).toContain('0.976')
  expect(host.querySelectorAll('#h11 tbody tr').length).toBe(8)
})

it('旧结论新读法八行，版本表写到卷四', () => {
  expect(host.querySelectorAll('#reinterpret tbody tr').length).toBe(REINTERPRET.length)
  const v = host.querySelector('#versions')?.textContent ?? ''
  expect(v).toContain('卷四')
  expect(v).toContain('约束的发起')
  expect(VERSIONS.some(x => x.volume === '卷四')).toBe(true)
})

/* ───────────────────── 结构契约（2026-11-27 加） ─────────────────────
 *
 * 上面 29 条守的是「内容还在」。这一组守的是「读者能不能找到内容」：
 *   1. 结构表 VOLUMES 与渲染必须一一对应——38 节少一节、多一节、id 打错，都红；
 *   2. 折叠只改默认视野，不删证据——收起后 tbody/数字仍在 DOM（测试与脚本可读；Ctrl-F 不会命中 display:none）；
 *   3. 深链 #h3 必须先展开卷三，否则锚点落在一段 0 高度的 DOM 上，滚到页顶。
 *
 * 上一版没有第 1 条：目录是三段手写数组，§0 审计因此长期没有入口，测试全绿。
 */
it('结构表与渲染一一对应：38 节全在 DOM，且 id 不重复', () => {
  expect(SECTION_IDS.length).toBe(38)
  for (const id of SECTION_IDS) {
    expect(host.querySelectorAll(`#${id}`).length).toBe(1)
  }
  expect(host.querySelectorAll('section[id]').length).toBe(SECTION_IDS.length)
})

it('导航：四卷各一格，节数写在格上，与结构表同源', () => {
  const nav = host.querySelector('[data-testid="volume-nav"]')
  expect(nav).toBeTruthy()
  const chips = [...(nav?.querySelectorAll('button') ?? [])].filter(b => b.getAttribute('aria-current') !== null || /^\d/.test((b.textContent ?? '').trim()))
  expect(VOLUMES.length).toBe(4)
  for (const v of VOLUMES) {
    const hit = [...(nav?.querySelectorAll('button') ?? [])].find(b => (b.textContent ?? '').includes(v.tab))
    expect(hit, `导航缺少 ${v.tab}`).toBeTruthy()
    expect(hit?.textContent).toContain(String(v.sections.length))
    expect(chips.length).toBeGreaterThan(0)
  }
})

/** 卷组的 DOM。折叠是原生 `<details>`（与 structural-edge 的 Fold 同一套），开合读 .open。 */
const volEl = (k: string) => host.querySelector(`[data-testid="volume-${k}"]`) as HTMLDetailsElement | null

it('默认视野：审计与卷五展开，卷四/卷三收起', () => {
  expect(volEl('audit')?.open).toBe(true)
  expect(volEl('v5')?.open).toBe(true)
  expect(volEl('v4')?.open).toBe(false)
  expect(volEl('v3')?.open).toBe(false)
})

it('折叠用 details，不是 hidden 面板：四卷都是细节元素，DOM 里没有隐藏容器', () => {
  // 这一条守的是机制本身。上一版用 <div hidden>，页内查找搜不到折叠区；
  // 换成 <details> 之后浏览器会为命中项自动展开（Chrome 会）。
  // 谁把它改回 hidden，这里立刻红。
  expect(host.querySelectorAll('details[data-testid^="volume-"]').length).toBe(VOLUMES.length)
  expect(host.querySelectorAll('div[hidden]').length).toBe(0)
})

it('收起不等于删除：卷三收起时 H3 的冻结读数仍在 DOM', () => {
  const v3 = volEl('v3')
  expect(v3?.open).toBe(false)
  expect(v3?.querySelectorAll('tbody tr').length).toBeGreaterThan(0)
  expect(v3?.textContent).toContain('+116.0bp')
  expect(v3?.textContent).toContain('−166.8bp')
})

it('展开卷三：点 summary 就开，再点就关（原生行为，不自己维护 aria）', () => {
  const v3 = volEl('v3')!
  const summary = v3.querySelector('summary')!
  expect(v3.open).toBe(false)
  act(() => { summary.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  expect(v3.open).toBe(true)
  act(() => { summary.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
  expect(v3.open).toBe(false)
})

it('深链 #h3：进来先展开卷三，否则锚点落在 0 高度上', () => {
  window.history.replaceState(null, '', '#h3')
  act(() => { window.dispatchEvent(new HashChangeEvent('hashchange')) })
  expect(volEl('v3')?.open).toBe(true)
  window.history.replaceState(null, '', '#audit')
})

it('点导航里的卷：收起状态下也先展开再跳', () => {
  const nav = host.querySelector('[data-testid="volume-nav"]')
  const chip = [...(nav?.querySelectorAll('button') ?? [])].find(b => (b.textContent ?? '').includes(VOLUMES[3].tab))
  expect(chip).toBeTruthy()
  act(() => { (chip as HTMLButtonElement).click() })
  expect(volEl('v3')?.open).toBe(true)
})

it('重算过的三节挂「口径已改」，点它回到 §0', () => {
  // 误读风险的具体形状：读者记得 H11 是 −474bp，页面现在写 +525bp。
  // §0 说了，但滚到 §13 时不会记得——所以标记要挂在节上。
  for (const id of ['probe', 'contrast', 'h11']) {
    const sec = host.querySelector(`#${id}`)
    expect(sec?.textContent, `${id} 缺口径标记`).toContain('口径已改')
  }
  const tag = host.querySelector('#h11 button[title]') as HTMLButtonElement | null
  expect(tag).toBeTruthy()
  act(() => { tag?.click() })
  expect(window.location.hash).toBe('#audit')
})
