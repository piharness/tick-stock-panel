/**
 * 「工具」章里的维护四节 —— 这一页自己怎么被维护。
 *
 * 它不是给读者的第五种结论，而是给下一个改这一页的人看的：
 * 什么每天变、什么只在重跑实验时变、什么已经冻结，以及改数时必须同时动哪些文件。
 *
 * 合并成「工具」章之后暴露了一件事：章节是「读什么」，更新频率是另一根轴。
 * 所以频率不再靠 tab 表达，而是靠每一节右上角的 Cadence 与下面这张表。
 */
import { type ReactNode } from 'react'
import {
  Database, FileText, FlaskConical, LayoutDashboard,
} from 'lucide-react'
import { repro } from './data'
import { Cadence, Callout, DataTable, type Col, Section, Tile, Tiles } from './ui'

interface CadenceRow {
  what: string
  when: string
  where: ReactNode
  check: ReactNode
}

const CADENCE: CadenceRow[] = [
  {
    what: '今日扫描',
    when: '每个交易日盘后',
    where: <><Code>analysis/etf_watch.py</Code> 写 <Code>data/etf_watch/latest.json</Code></>,
    check: '页面「数据日」= 最近交易日；--verify 可机械核对',
  },
  {
    what: '反例登记（状态择时）',
    when: '每个交易日盘后',
    where: <><Code>app/services/position_art.py</Code> 十年序列有缓存；接口只读</>,
    check: '与 /api/position-art 同源；结算口径不在这里改',
  },
  {
    what: '账户指令',
    when: '每个交易日盘后',
    where: <><Code>analysis/etf_watch_account.py</Code> 写 <Code>account.json</Code></>,
    check: '指令与 account-runbook 一致（最多三条）；缺快照时显示命令而不是空白',
  },
  {
    what: '结论数字（总则 / 证据 / 规则）',
    when: '仅当重跑实验',
    where: <><Code>data.ts</Code> + <Code>docs/structural-edge/*.md</Code>（同一 commit）</>,
    check: 'pnpm build + 本页结构测试',
  },
  {
    what: '动态读数（机会雷达 / 证据 03）',
    when: '每个交易日盘后（随接口）',
    where: <><Code>live.ts</Code> 把接口字段映射成句子，不落盘、不手抄</>,
    check: <><Code>liveClaims.test.ts</Code> 拦手抄；<Code>python analysis/handbook_audit.py</Code> 按交易日历核对是否覆盖到最近交易日（盘后跑一次）</>,
  },
  {
    what: '仓位择时验收',
    when: '重跑 position_art',
    where: <><Code>backend/app/services/position_art.py</Code></>,
    check: '五项 checks 与接口返回逐项一致；分半、安慰剂、压测同源',
  },
  {
    what: '组合基准阶梯',
    when: '重跑 portfolio / leg2',
    where: <><Code>data.ts</Code> + <Code>portfolio-replay.md</Code> + <Code>leg2-bond.md</Code></>,
    check: '每行必须带窗口；两个窗口的年化不能直接相减',
  },
  {
    what: '规则与阈值',
    when: '预注册后不轻改',
    where: <><Code>data.ts</Code>；扫描器阈值在 <Code>etf_watch.py</Code> 同源</>,
    check: '两处口径必须一致，不得出现第二份副本',
  },
  {
    what: '反证（案卷 A–D）',
    when: '冻结，只追加',
    where: <><Code>Archive.tsx</Code> + <Code>data.ts</Code>（新增一组折叠）</>,
    check: '四组折叠仍在、默认收起，索引卡与折叠 id 对应',
  },
  {
    what: '历史回放',
    when: '新增交易日后按需',
    where: <><Code>analysis/etf_watch_replay.py</Code> → <Code>data/etf_watch/replay.json</Code></>,
    check: '页面回放末日 = 最近交易日；轨迹与效果同步更新',
  },
  {
    what: '页面结构',
    when: '改版时',
    where: <><Code>index.tsx</Code> + <Code>Shell.tsx</Code> + <Code>nav.test.tsx</Code></>,
    check: '六个 tab 与六个 panel 一一对应，所有面板常驻 DOM',
  },
]

const FLOW = [
  { k: '原始数据', v: 'kline_etf_daily · index_daily_full · 511990 万份收益 · 511010 分红还原', Icon: Database },
  { k: '研究 / 引擎', v: 'analysis/_diag_*.py · etf_watch.py · etf_watch_account.py · position_art.py', Icon: FlaskConical },
  { k: '产物', v: '*_result.json · latest.json · account.json · cash_leg.json · replay.json', Icon: FileText },
  { k: '页面', v: 'data.ts（实验结论）· live.ts（接口→句子）· GET /api/etf-watch · /api/position-art', Icon: LayoutDashboard },
]

const RULES = [
  {
    t: '一个数字只有一个定义点',
    d: '正文不写字面量。结论数字只在 data.ts，动态读数只在 live.ts（来自接口）；两处都写，等于制造两个真相。',
  },
  {
    t: '动态读数不抄，缺了就隐藏',
    d: '日期、进度、广度这类每天变的值，手抄一次就会在下一个交易日变成假话。接口没数据时隐藏整句，不回退上次的旧值。',
  },
  {
    t: '改数必须同 commit 改文档',
    d: '注明样本量（N）与口径。否则下一个人会把旧结论当新证据，返工从这里开始。',
  },
  {
    t: '新增内容先归章',
    d: '为什么 → 总则；凭什么信 → 证据；什么条件下做 → 规则；被否掉 → 反证；用什么做 → 工具（以上都在图书馆）；今天做什么 → 机会雷达（/handbook，独立页面）。频率不同就别放同一页 —— 2026-11-24 的拆分就是这条规则的执行。',
  },
  {
    t: '不为了让页面好看而改口径',
    d: '图表跟随 data.ts，阈值不在这里调。视觉可以重排，数字不能顺手对齐。',
  },
]

/** 维护四节。挂在「工具」章的 02–05 位；01 位是回放台。 */
export function MaintSections() {
  const cadenceCols: Col<CadenceRow>[] = [
    { head: '内容', render: r => <span className="font-medium">{r.what}</span>, w: '24%' },
    { head: '多久变一次', render: r => <span className="whitespace-nowrap text-secondary">{r.when}</span>, w: '16%' },
    { head: '从哪里改', render: r => <span className="text-[12px] text-muted">{r.where}</span>, w: '34%' },
    { head: '怎么验收', render: r => <span className="text-[12px] text-muted">{r.check}</span>, w: '26%' },
  ]

  return (
    <>
      <Section
        id="maint"
        no="04"
        title="更新节奏：什么每天变，什么冻结"
        lead="六章按决策顺序排，每一章有自己的更新频率 —— 频率标在每一节的右上角，而不是只标在 tab 上。这张表是那些标签的兑现方式：每一行都能落到一个文件与一次验收。"
        source="CONTRIBUTING.md §7 / §9 · data.ts 维护规则"
        actions={<Cadence kind="ondemand" />}
      >
        <DataTable cols={cadenceCols} rows={CADENCE} rowKey={r => r.what} minWidth={700} />
        <Callout tone="muted" label="这一页不是什么">
          它不是实盘交易系统：不接券商、不下真单、没有实时行情。账户是纸面的，指令是收盘后生成的模拟单 ——
          它只记录「什么条件下买、买多少、什么情况下不卖」，以及这些结论被什么证据支持、被什么实验否掉。
        </Callout>
      </Section>

      <Section
        id="flow"
        no="05"
        title="数据链路：静态结论与活数据走两条路"
        lead="活数据每天被脚本重写，静态结论只在重跑实验时改，回放按需重算并另存 replay.json。三条路在页面里汇合，但不能被合并成同一个数 —— 上一版把位置梯度内联进页面，与研究脚本差了 30bp 而不自知。"
        source="analysis/etf_watch.py · etf_watch_account.py · app/services/position_art.py · data.ts"
        actions={<Cadence kind="ondemand" />}
      >
        <Tiles cols={2} className="mt-6">
          {FLOW.map((f, i) => (
            <div key={f.k} className="border-t border-border pt-3">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5 text-[11px] font-medium text-secondary">
                  <f.Icon className="h-3 w-3 text-muted" />
                  {f.k}
                </div>
                <span className="font-mono text-[11px] text-muted">{String(i + 1).padStart(2, '0')}</span>
              </div>
              <div className="mt-2 break-all font-mono text-[11px] leading-[1.7] text-muted">{f.v}</div>
            </div>
          ))}
        </Tiles>

        <Callout tone="ok" label="活数据只有一条来源">
          「机会雷达」页的四个区块（扫描、账户、现金腿、反例登记）与「证据 03」的当日读数，都来自接口、
          经 <Code>live.ts</Code> 映射成句子 —— 页面不内联任何副本，也不在组件里拼第二套文案。
          证据 04 的验收读数与雷达页的「反例登记」同源同参，不是第二份定义。其余章节是静态结论：读到旧数说明没人重跑实验，不代表市场还在那个状态。
        </Callout>
      </Section>

      <Section
        id="docs"
        no="06"
        title="文档地图：每个结论对应哪篇 md"
        lead="改数之前先读对应文档；文档与页面不一致时，以文档的研究口径为准，页面跟随。"
        source="docs/structural-edge/ · analysis/_diag_*_result.json"
        actions={<Cadence kind="ondemand" />}
      >
        <DataTable
          cols={[
            { head: '文档', render: d => <span className="font-mono text-[11px] text-secondary">docs/structural-edge/{d.f}</span> },
            { head: '支撑本页哪一部分', render: d => d.o },
          ]}
          rows={repro.docs}
          rowKey={d => d.f}
          dense
          minWidth={520}
        />
        <p className="mt-2 text-[11px] leading-relaxed text-secondary">数据来源：{repro.data}</p>
      </Section>

      <Section
        id="discipline"
        no="07"
        title="改数规则：五条，违反过就会出错"
        lead="这个项目返工最多的原因不是算错，是「同一个真相两个定义点」。下面五条是给下一次改版的约束，不是口号。"
        source="data.ts 文件头 · docs/structural-edge/README.md"
        actions={<Cadence kind="ondemand" />}
      >
        <Tiles cols={2} className="mt-6">
          {RULES.map((r, i) => (
            <Tile
              key={r.t}
              lead={String(i + 1).padStart(2, '0')}
              title={r.t}
              body={r.d}
            />
          ))}
        </Tiles>

        <div className="mt-9 border-t border-border pt-4">
          <div className="text-[11px] font-medium tracking-[0.1em] text-muted">改完至少跑这三条</div>
          <pre className="mt-3 overflow-x-auto rounded border border-border bg-elevated/40 px-4 py-3 font-mono text-[12px] leading-[1.8] text-secondary">{`cd frontend
pnpm build
pnpm vitest run src/pages/structural-edge/nav.test.tsx src/pages/structural-edge/liveClaims.test.ts \\
  src/pages/structural-edge/position-sizing.test.tsx
cd ..
python analysis/handbook_audit.py     # 产物是否同日、行情有没有缺口
git diff --check`}</pre>
          <p className="mt-3 text-[12px] leading-[1.7] text-muted">
            只改视觉、不改数字时，也至少跑一次构建；构建通过不等于页面可读，窄屏与暗色主题要各看一遍。
          </p>
        </div>
      </Section>
    </>
  )
}

function Code({ children }: { children: ReactNode }) {
  return <code className="rounded bg-elevated px-1 py-0.5 font-mono text-[11px] text-secondary">{children}</code>
}
