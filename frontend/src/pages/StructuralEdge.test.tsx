// @vitest-environment jsdom
// 图书馆（/library）的活读数区 —— 只测两件事：
// 1. 读到 API 时，页头快照日与证据章的当日广度如实渲染，且数字只来自 payload；
// 2. 没跑过扫描时降级成「—」＋可执行的下一步（命令行），不空白也不报错。
//
// 2026-11-24 手册拆页后，「盘后扫描」的判定表 / 理由 / V1 进度 / 数簇不数行
// 连同空态命令行一起搬到了机会雷达（Daily.tsx）。那部分断言由 daily.test.tsx 守；
// 本文件只守图书馆仍然承担的那一半 —— 快照。两边都不留空档，也不重复守同一件事。
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { StructuralEdge } from './StructuralEdge'

const fake = vi.hoisted(() => ({
  fail: false,
  payload: {
    meta: {
      asof: '2026-11-20', generated_at: '2026-11-21 23:04:07', mode: 'record-only',
      note: '只记录不下注', dd_convention: '行业净值距 250 日高点', vol_convention: '20 日波动的 500 日分位',
      thresholds: { low: -0.35, deep: -0.5, vol_hi: 0.66, min_amt20: 50000000, cooldown: 60, hold: 20 },
    },
    market: { state: '下行·低波', hs300_close: 3954.57, ma60: 4000, vol_pct: 0.41, trend_up: false, f3_active: false },
    counts: { industries: 20, in_band: 8, signal: 6, candidate: 6, deep: 1, clusters_today: 1, note: '' },
    industries: [
      {
        industry: '电子', dd: -0.488, vol_pct: 0.954, stale_days: 0, n_etf: 30,
        best_symbol: '588170.SH', best_name: '科创半导体ETF华夏', amt20: 7.8e9,
        r1: true, r2: true, r3: true, f6: true, status: 'CANDIDATE', reasons: [],
      },
      {
        industry: '交运', dd: -0.397, vol_pct: 0.63, stale_days: 0, n_etf: 5,
        best_symbol: '159378.SZ', best_name: '通用航空ETF永赢', amt20: 3.555e7,
        r1: true, r2: false, r3: true, f6: false, status: 'BLOCKED',
        reasons: ['F2 低波阴跌（volpct 63%）', 'F6 成交额 3555 万 < 5,000 万'],
      },
    ],
    ledger: {
      events: [
        {
          type: 'event', id: '2026-11-20|电子', date: '2026-11-20', industry: '电子',
          dd: -0.488, vol_pct: 0.954, state: '下行·低波', signal: true, qualified: true,
          best_name: '科创半导体ETF华夏', amt20: 7.8e9, reasons: [],
        },
      ],
      settles: [],
      v1: {
        rule: '', counting: '按自然月成簇', left_tail_mapping: '', required: 3,
        state: 'WAITING', progress: '0/3', signal_events: 6, signal_clusters: 1,
      },
      low_vol_band_entries: 0,
    },
    data: {
      etf: { base: 'etf_daily_full.parquet', base_last: '2026-11-17', part_days_added: [], last: '2026-11-20', rows: 1569064 },
      index: { base: 'index_daily_full.parquet', base_last: '2026-11-17', part_days_added: [], last: '2026-11-20' },
      lag_sessions: 0,
    },
    warnings: [] as string[],
  },
}))

vi.mock('@/lib/api', () => ({
  api: {
    etfWatchAccount: async () => { throw new Error('404 Not Found') },
    etfWatch: async () => {
      if (fake.fail) throw new Error('404 Not Found')
      return fake.payload
    },
    positionArt: async () => ({ available: false, reason: '日线分区不足（测试占位）' }),
    positionArtTail: async () => ({ available: false, reason: '测试占位' }),
  },
}))

// 页面直接引 echarts；jsdom 里没有 canvas，init 会抛。只验证「有数据就不空」。
vi.mock('echarts', () => ({
  init: () => ({ setOption: () => {}, resize: () => {}, dispose: () => {} }),
}))

let host: HTMLDivElement
let root: Root
let client: QueryClient

beforeEach(() => {
  fake.fail = false
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } })
})

afterEach(async () => {
  await act(async () => root.unmount())
  client.clear()
  host.remove()
  vi.unstubAllGlobals()
})

async function render() {
  await act(async () => {
    root.render(
      <QueryClientProvider client={client}>
        <StructuralEdge />
      </QueryClientProvider>,
    )
  })
  await act(async () => { await Promise.resolve() })
  return host.textContent ?? ''
}

it('读到扫描结果时：页头快照日与证据章的当日广度都来自 payload', async () => {
  const text = await render()
  // 页头「最近更新」：数据日 + 快照来源，不是今天
  expect(text).toContain('最近更新')
  expect(text).toContain('扫描快照')
  expect(text).toContain('2026-11-20')
  // 证据章「当日读数」：区间内 8 / 四条全过 6 / 记 1 个事件簇 —— 三个数只能来自 payload
  expect(text).toContain('区间内 8 个行业')
  expect(text).toContain('四条全过 6 个')
  expect(text).toContain('记 1 个事件簇')
  // 历史分档仍在（它不随当日变化，不能因为活读数来了就被顶掉）
  expect(text).toContain('历史分档')
})

it('没跑过扫描时：图书馆降级成「—」与命令行，且不把 payload 的数字写进页面', async () => {
  fake.fail = true
  const text = await render()
  expect(text).toContain('扫描快照')
  expect(text).toContain('当日读数未就绪')
  expect(text).toContain('analysis/etf_watch.py')
  // 负控：接口挂了还能读到 8 / 6 这类数，只可能是内联在页面里 —— 那是这一类 bug 的起点
  expect(text).not.toContain('区间内 8 个行业')
  expect(text).not.toContain('四条全过 6 个')
})

// 成本核算节：三件事必须同时在场——硬账、二手信息五类、以及唯一跨规模稳定的那个维度。
it('渲染成本核算与二手信息检验，且把「传播层级」标为唯一真效应', async () => {
  const text = await render()
  expect(text).toContain('这五个月的账')
  expect(text).toContain('1,378 / 319')
  expect(text).toContain('0 条')
  // 五类二手信息都要出现，不能只留结论好听的那一条
  for (const s of ['游资叙事', '身份叙事', '舆情叙事', '散户叙事', '传播层级']) {
    expect(text).toContain(s)
  }
  // 双排序两个规模桶的数必须都在，否则「跨规模稳定」这个判据无法核对
  expect(text).toContain('+248.4')
  expect(text).toContain('+236.4')
  expect(text).toContain('真效应')
  expect(text).toContain('规模效应')
  // 判据第 0 问与悖论
  expect(text).toContain('可检验性悖论')
  expect(text).toContain('第几次')
})

// 叙事排除节：七道闸门必须齐全，案例三步的判定必须写出「真/误读/方向反了」，
// 六嫌疑人里必须至少有一个「排除」，否则这一节就变成了罗列观点。
it('渲染叙事排除的七道闸门、案例三步与嫌疑人判定', async () => {
  const text = await render()
  expect(text).toContain('叙事的七道闸门')
  for (const g of ['恒等式', '方向', '量级', '分母', '层级', '时序', '证伪']) {
    expect(text).toContain(g)
  }
  expect(text).toContain('误读')
  expect(text).toContain('方向反了')
  expect(text).toContain('公允价值变动损益由 +480.27 亿转为 −643.12 亿')
  // 分母陷阱的四个数和结论都要在，否则「占比上升来自分母萎缩」无法核对
  expect(text).toContain('26.09%')
  expect(text).toContain('腰斩 56%')
  // 嫌疑人表必须区分「主因候选」与「排除」
  expect(text).toContain('融资盘去杠杆')
  expect(text).toContain('主因候选')
  expect(text).toContain('排除')
  // 边界必须写着「只能杀选项，不能选选项」
  expect(text).toContain('只是未被排除，不是已证实')
})

it('六只主题 ETF 一节：位置、一注、降息为空、叙事不过关、广度按簇计数', async () => {
  const text = await render()
  expect(text).toContain('什么时候建仓，要不要等降息')
  // 六只代码都在表里，且科创半导体的回撤是修复折算后的 −51.3%（不是 −83.7%）
  for (const code of ['588170', '512710', '159387', '159869', '159363', '159530']) {
    expect(text).toContain(code)
  }
  expect(text).toContain('-51.3%')          // JSX 里数字是 ASCII 负号
  expect(text).not.toContain('-83.7%')
  // 「六个标签 = 一个机会」
  expect(text).toContain('2 个独立簇')
  expect(text).toContain('2022-04-25')
  // 降息这条必须写成「不存在」而不是「还没到」
  expect(text).toContain('3.75–4.00%')
  expect(text).toContain('−2.81%')
  expect(text).toContain('既不必要，也不充分')
  // 叙事要给出相关性实测，而不是只下判断
  expect(text).toContain('0.77')
  expect(text).toContain('58%')
  // 风险平价权重必须显式给出：高波两只被压到最低，低波两只拿最高
  expect(text).toContain('风险平价权重')
  expect(text).toContain('10.7%')
  expect(text).toContain('20.6%')
})
