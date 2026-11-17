/**
 * 市场公民 —— 全市场个股的「多维流动性身份」分级与注意力预算。
 *
 * 设计定位：不是「把 5000 只票排个序」，而是回答一个决策问题：
 *   「今天，我的看盘精力应该落在哪里？」
 *
 * 三个原则：
 *   1. 等级可解释 —— 每只票的等级由规模 / 关注度 / 成交额三根独立轴的
 *      当日分位映射而来（后端 LEVEL_RULES），不是黑盒评分。页面上三轴分位
 *      可视化，点开就知道「它为什么是这个等级」。
 *   2. 等级时变 —— 分位是「当日全市场分位」，每天重算，活跃票升格、沉默票降格，
 *      不是一次性贴标签。
 *   3. 折叠而非展示 —— 低等级（沉默/背景）默认折叠进「沉默区」，一眼看不到、
 *      想看才展开。这才是「不配获得注意力」的正确执行方式：不是删除它，
 *      而是让它不占你的注意力。
 *
 * 诚实边界：等级描述流动性身份，不预测收益。样本外回测（2005–2026，5289 日）
 * 显示等级预测的是「注意力粘性」而非收益；事件研究 + 匹配归因证明「沉默」的
 * 历史超额是小盘+低流动性的投影，不是沉默本身的 alpha。页面顶部固定展示
 * 这条带归因的边界声明。
 *
 * 归属：扩展域官方页面，经 custom/citizenship/extension.tsx 注册路由与导航；
 * 对核心能力均为只读 import（api/QK/组件/格式化），不承载核心域语义。
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronUp, EyeOff, Focus, RefreshCw, TrendingDown, TrendingUp } from 'lucide-react'
import { api, type CitizenshipData, type CitizenshipLevel, type CitizenshipRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { fmtBigNum } from '@/lib/format'
import { PageHeader } from '@/components/PageHeader'
import { DateInput } from '@/components/DateInput'
import { StockPreviewDialog } from '@/components/StockPreviewDialog'
import { AttentionMap } from './AttentionMap'
import { IdentityRadar } from './IdentityRadar'
import { AttentionTrend } from './AttentionTrend'

// ===== 等级视觉映射（单一来源：order 与后端 /levels 对齐） =====
const LEVEL_ORDER: CitizenshipLevel[] = ['core', 'active', 'background', 'silent']

const LEVEL_META: Record<CitizenshipLevel, {
  label: string
  hint: string
  chip: string   // 等级徽章配色
  bar: string    // 数量条配色
}> = {
  core: {
    label: '核心公民',
    hint: '成交额全市场前 10%，流动性的绝对中心',
    chip: 'border-amber-500/30 bg-amber-500/10 text-amber-600',
    bar: 'bg-amber-500',
  },
  active: {
    label: '活跃公民',
    hint: '成交活跃，或小而活跃的异动票',
    chip: 'border-sky-500/30 bg-sky-500/10 text-sky-600',
    bar: 'bg-sky-500',
  },
  background: {
    label: '背景公民',
    hint: '长尾背景板，没有明显活跃信号',
    chip: 'border-border bg-elevated/50 text-muted',
    bar: 'bg-zinc-400',
  },
  silent: {
    label: '沉默公民',
    hint: '既没成交、也没人看',
    chip: 'border-border bg-elevated/30 text-muted/70',
    bar: 'bg-zinc-500',
  },
}

// 折叠阈值：silent 恒折叠；background 默认也折叠（长尾），用户可切换。
const COLLAPSED_LEVELS: CitizenshipLevel[] = ['silent', 'background']

/** 分位 → 视觉条（0~1 → 百分比宽度） */
function pctBar(p: number): string {
  return `${Math.round(Math.min(1, Math.max(0, p)) * 100)}%`
}

/** 三轴分位迷你条：规模 / 关注度 / 成交额 */
function AxisBars({ row }: { row: CitizenshipRow }) {
  const axes = [
    { label: '规模', p: row.p_scale },
    { label: '关注', p: row.p_turnover },
    { label: '成交', p: row.p_amount },
  ]
  return (
    <div className="flex items-center gap-2" title="规模 / 关注度 / 成交额 的当日全市场分位（1=最高）">
      {axes.map(a => (
        <div key={a.label} className="flex items-center gap-1">
          <span className="w-4 text-[9px] text-muted/70">{a.label}</span>
          <div className="relative h-1.5 w-12 overflow-hidden rounded-full bg-elevated">
            <div
              className="absolute inset-y-0 left-0 rounded-full bg-accent/70"
              style={{ width: pctBar(a.p) }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

/** 单个等级区块 */
function LevelSection({
  level, rows, collapsed, onToggle, onOpenStock,
}: {
  level: CitizenshipLevel
  rows: CitizenshipRow[]
  collapsed: boolean
  onToggle: () => void
  onOpenStock: (s: string) => void
}) {
  const meta = LEVEL_META[level]
  if (rows.length === 0) return null
  return (
    <section className="overflow-hidden rounded-card border border-border bg-surface/60">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2.5 border-b border-border/60 px-4 py-2.5 text-left transition-colors hover:bg-elevated/40"
      >
        <span className={cn('h-2 w-2 shrink-0 rounded-full', meta.bar)} />
        <span className="text-[13px] font-medium text-foreground">{meta.label}</span>
        <span className="font-mono text-[11px] text-muted">{rows.length}</span>
        <span className="hidden text-[10px] text-muted/70 sm:inline">· {meta.hint}</span>
        {collapsed ? (
          <ChevronDown className="ml-auto h-4 w-4 text-muted" />
        ) : (
          <ChevronUp className="ml-auto h-4 w-4 text-muted" />
        )}
      </button>
      {!collapsed && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-left">
            <thead>
              <tr className="border-b border-border/60 text-[10px] uppercase tracking-wider text-muted/70">
                <th className="px-4 py-2 font-medium">股票</th>
                <th className="px-2 py-2 text-right font-medium">流通市值</th>
                <th className="px-2 py-2 text-right font-medium">成交额</th>
                <th className="px-2 py-2 text-right font-medium">换手</th>
                <th className="px-4 py-2 font-medium">流动性身份</th>
                <th className="px-4 py-2 font-medium">状态变化</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr
                  key={r.symbol}
                  onClick={() => onOpenStock(r.symbol)}
                  className="cursor-pointer border-b border-border/30 text-[11px] transition-colors last:border-0 hover:bg-accent/[0.04]"
                >
                  <td className="px-4 py-2">
                    <span className="font-medium text-foreground">{r.name ?? r.symbol}</span>
                    <span className="ml-1.5 font-mono text-[9px] text-muted">{r.symbol}</span>
                  </td>
                  <td className="px-2 py-2 text-right font-mono tabular-nums text-secondary">{fmtBigNum(r.mcap)}</td>
                  <td className="px-2 py-2 text-right font-mono tabular-nums text-secondary">{fmtBigNum(r.amount)}</td>
                  <td className="px-2 py-2 text-right font-mono tabular-nums text-secondary">{r.turnover_rate.toFixed(1)}%</td>
                  <td className="px-4 py-2"><AxisBars row={r} /></td>
                  <td className="px-4 py-2">
                    {r.transition === 'down' ? (
                      <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium text-red-600 bg-red-500/10" title="较前一交易日关注度下降（被抛弃）">
                        <TrendingDown className="h-3 w-3" />降格
                      </span>
                    ) : r.transition === 'up' ? (
                      <span className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 bg-emerald-500/10" title="较前一交易日关注度上升（被重新关注）">
                        <TrendingUp className="h-3 w-3" />升格
                      </span>
                    ) : r.transition === 'new' ? (
                      <span className="text-[10px] text-muted/60" title="前一交易日无此票（新进/复牌）">新进</span>
                    ) : (
                      <span className="text-[10px] text-muted/40">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function Citizenship() {
  const [asOf, setAsOf] = useState('')  // '' = 最新交易日
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({
    silent: true,
    background: true,
  })
  const [preview, setPreview] = useState<string | null>(null)
  const [selectedRow, setSelectedRow] = useState<CitizenshipRow | null>(null)

  const q = useQuery<CitizenshipData>({
    queryKey: QK.citizenship(asOf || undefined),
    queryFn: () => api.citizenship(asOf || undefined),
    staleTime: 60_000,
  })

  const data = q.data

  // 按等级分桶；每个等级内按成交额降序（活跃度最高的靠前）。
  const buckets = useMemo(() => {
    const map: Record<CitizenshipLevel, CitizenshipRow[]> = {
      core: [], active: [], background: [], silent: [],
    }
    for (const r of data?.rows ?? []) {
      map[r.level]?.push(r)
    }
    for (const lvl of LEVEL_ORDER) {
      map[lvl].sort((a, b) => b.amount - a.amount)
    }
    return map
  }, [data])

  const total = data?.summary.total ?? 0
  const attentionTop = data?.summary.attention_top ?? 0

  const toggle = (lvl: CitizenshipLevel) => {
    setCollapsed(c => ({ ...c, [lvl]: !c[lvl] }))
  }

  // 全景图点选：打开个股预览 + 在卡片内展示三轴身份雷达
  const onMapSelect = (symbol: string) => {
    setPreview(symbol)
    const row = data?.rows.find(r => r.symbol === symbol) ?? null
    setSelectedRow(row)
  }

  return (
    <div className="min-h-full bg-base pb-8">
      <PageHeader
        title="市场公民"
        subtitle={`流动性身份分级 · 当日全市场分位 · ${asOf ? `回看 ${asOf}` : `数据截至 ${data?.as_of ?? '—'}`}`}
        right={
          <div className="flex items-center gap-2">
            <DateInput
              value={asOf}
              onChange={setAsOf}
              ariaLabel="回看日期"
              placeholder="回看某天"
              className="rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary"
            />
            <button
              onClick={() => q.refetch()}
              disabled={q.isFetching}
              className="inline-flex items-center gap-1 rounded-btn border border-border bg-elevated px-2 py-1 text-[11px] text-secondary transition-colors hover:text-foreground disabled:opacity-50"
              title="刷新"
            >
              <RefreshCw className={cn('h-3 w-3', q.isFetching && 'animate-spin')} />刷新
            </button>
          </div>
        }
      />

      <div className="mx-auto max-w-[1280px] space-y-4 px-4 py-4 sm:px-6">
        {/* 注意力预算概览 */}
        <div className="rounded-card border border-border bg-surface/80 p-4">
          <div className="flex items-center gap-2">
            <Focus className="h-4 w-4 text-accent" />
            <h2 className="text-sm font-medium text-foreground">注意力预算</h2>
          </div>
          <p className="mt-1 text-[11px] leading-relaxed text-muted">
            全市场 {total.toLocaleString()} 只票里，值得投入注意力的（核心 + 活跃）约{' '}
            <span className="font-mono text-foreground">{attentionTop.toLocaleString()}</span> 只，
            占比 <span className="font-mono text-foreground">{total ? ((attentionTop / total) * 100).toFixed(1) : '—'}%</span>。
            其余为背景与沉默公民，默认折叠，不占用你的注意力。
          </p>
          {/* 等级分布条 */}
          <div className="mt-3 flex h-3 w-full overflow-hidden rounded-full bg-elevated">
            {LEVEL_ORDER.map(lvl => {
              const count = buckets[lvl].length
              if (!count) return null
              return (
                <div
                  key={lvl}
                  className={cn('h-full transition-all', LEVEL_META[lvl].bar)}
                  style={{ width: `${total ? (count / total) * 100 : 0}%` }}
                  title={`${LEVEL_META[lvl].label} ${count} 只`}
                />
              )
            })}
          </div>
          <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1">
            {LEVEL_ORDER.map(lvl => (
              <div key={lvl} className="flex items-center gap-1.5 text-[10px] text-muted">
                <span className={cn('h-2 w-2 rounded-full', LEVEL_META[lvl].bar)} />
                {LEVEL_META[lvl].label}
                <span className="font-mono">{buckets[lvl].length}</span>
              </div>
            ))}
          </div>
          {/* 迁移计数（降格 = 风控提示） */}
          {data?.summary.transitions && (
            <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border/40 pt-2 text-[10px] text-muted">
              <span className="flex items-center gap-1">
                <TrendingUp className="h-3 w-3 text-emerald-500" />
                升格（被重新关注）
                <span className="font-mono text-emerald-600">{data.summary.transitions.up.toLocaleString()}</span>
              </span>
              <span className="flex items-center gap-1">
                <TrendingDown className="h-3 w-3 text-red-500" />
                降格（被抛弃）
                <span className="font-mono text-red-600">{data.summary.transitions.down.toLocaleString()}</span>
              </span>
              <span className="flex items-center gap-1">
                新进
                <span className="font-mono">{data.summary.transitions.new.toLocaleString()}</span>
              </span>
            </div>
          )}
        </div>

        {/* 全市场注意力全景图 */}
        {data && data.rows.length > 0 && (
          <div className="rounded-card border border-border bg-surface/80 p-4">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium text-foreground">注意力全景图</span>
                <span className="text-[11px] text-muted">横轴规模分位 · 纵轴关注度分位 · 颜色=等级 · 大小=成交额</span>
              </div>
              <div className="flex items-center gap-3 text-[10px] text-muted">
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-amber-500" />核心
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-sky-500" />活跃
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-zinc-400" />背景
                </span>
                <span className="flex items-center gap-1">
                  <span className="h-2 w-2 rounded-full bg-zinc-600" />沉默
                </span>
                <span className="ml-2 flex items-center gap-1 border-l border-border/60 pl-3">
                  <span
                    className="text-emerald-600"
                    style={{ animation: 'breath-glow 2.8s ease-in-out infinite' }}
                  >▲</span>升格
                </span>
                <span className="flex items-center gap-1">
                  <span
                    className="text-red-600"
                    style={{ animation: 'breath-glow 2.8s ease-in-out infinite', animationDelay: '0.7s' }}
                  >■</span>降格
                </span>
              </div>
            </div>
            <div className="mt-1 text-[10px] leading-relaxed text-muted/70">
              右上角是流动性的绝对中心，左下角是沉默黑洞；右上缘的<span className="text-emerald-600">▲升格</span>是被重新关注的异动票，可点击任意点查看个股。
            </div>
            <div className="mt-2">
              <AttentionMap rows={data.rows} onOpenStock={onMapSelect} />
            </div>

            {/* 选中个股的三轴流动性身份 */}
            {selectedRow && (
              <div className="mt-3 flex flex-col gap-3 border-t border-border/50 pt-3 sm:flex-row sm:items-center">
                <div className="w-full sm:w-56">
                  <IdentityRadar row={selectedRow} />
                </div>
                <div className="min-w-0 flex-1 space-y-1.5">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-foreground">{selectedRow.name ?? selectedRow.symbol}</span>
                    <span className="font-mono text-[10px] text-muted">{selectedRow.symbol}</span>
                    <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-medium', LEVEL_META[selectedRow.level].chip)}>
                      {LEVEL_META[selectedRow.level].label}
                    </span>
                  </div>
                  <p className="text-[11px] leading-relaxed text-muted">{LEVEL_META[selectedRow.level].hint}</p>
                  <div className="grid grid-cols-3 gap-2 text-[11px]">
                    <div>
                      <div className="text-muted/70">规模分位</div>
                      <div className="font-mono text-secondary">{(selectedRow.p_scale * 100).toFixed(0)}%</div>
                    </div>
                    <div>
                      <div className="text-muted/70">关注分位</div>
                      <div className="font-mono text-secondary">{(selectedRow.p_turnover * 100).toFixed(0)}%</div>
                    </div>
                    <div>
                      <div className="text-muted/70">成交分位</div>
                      <div className="font-mono text-secondary">{(selectedRow.p_amount * 100).toFixed(0)}%</div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* 注意力演变趋势（近 30 个交易日） */}
        <div className="rounded-card border border-border bg-surface/80 p-4">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-foreground">注意力演变</span>
            <span className="text-[11px] text-muted">近 30 个交易日 · 等级占比堆叠 + 每日升格/降格计数</span>
          </div>
          <p className="mt-1 text-[10px] leading-relaxed text-muted/70">
            面积越「暖」（核心+活跃占比高）市场越热；下方绿柱=每日被重新关注的票数（升格），红柱=被抛弃的票数（降格）。
          </p>
          <div className="mt-2">
            <AttentionTrend days={30} />
          </div>
        </div>

        {/* 诚实边界声明 —— 由样本外验证（2005–2026，5289 个交易日）背书 */}
        <div className="flex items-start gap-2 rounded-card border border-warning/20 bg-warning/[0.04] px-4 py-3 text-[11px] leading-relaxed text-muted">
          <EyeOff className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning" />
          <span>
            等级预测的是<span className="text-foreground">注意力粘性</span>（今天值得看、明天还值不值得看），
            <span className="text-foreground">不是收益</span>。样本外回测（2005–2026，5289 日）显示：
            核心公民未来 20 日涨停概率确实更高（23.3% vs 沉默 17.6%），
            但未来 20 日<span className="text-foreground">超额收益恰好相反</span>——沉默票 +606bp、核心票 −11bp。
            这份「沉默超额」经事件研究（匹配归因）拆掉小盘/低流动性后<span className="text-foreground">消失甚至转负</span>
            （事件后 60 日差分累计超额 ≈ −134bp）——它是<span className="text-foreground">小盘 + 低流动性的投影</span>，
            不是「沉默」本身有 alpha。同理，同样的小盘票里，沉默的那只<span className="text-foreground">并不更好</span>。
            等级只回答<span className="text-foreground">值不值得你现在花注意力去盯</span>；若目的是找超额收益，请看收益类工具，别把本页等级当买点。
          </span>
        </div>

        {/* 加载 / 空状态 */}
        {q.isLoading && !data ? (
          <div className="flex h-40 items-center justify-center">
            <div className="flex items-center gap-2 text-sm text-muted">
              <RefreshCw className="h-4 w-4 animate-spin" /> 正在计算全市场流动性身份…
            </div>
          </div>
        ) : !data || data.rows.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-3 rounded-card border border-border bg-surface/60 px-6 py-16">
            <div className="text-sm font-medium text-foreground">暂无数据</div>
            <p className="text-xs text-muted">需要日 K 与标的维表，请先前往「数据」页同步</p>
          </div>
        ) : (
          <div className="space-y-3">
            {LEVEL_ORDER.map(lvl => (
              <LevelSection
                key={lvl}
                level={lvl}
                rows={buckets[lvl]}
                collapsed={collapsed[lvl] ?? COLLAPSED_LEVELS.includes(lvl)}
                onToggle={() => toggle(lvl)}
                onOpenStock={setPreview}
              />
            ))}
          </div>
        )}
      </div>

      <StockPreviewDialog
        symbol={preview}
        triggerInfo={null}
        onClose={() => setPreview(null)}
      />
    </div>
  )
}
