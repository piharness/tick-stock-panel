/**
 * 操盘档案 —— 个人学习页：用「痕迹」对账「叙事」，把手法变成可证伪的判据。
 *
 * 四块按学习动作排，不按数据的时间顺序排：
 *   ① 结构异常  机器先找出「最近哪些票同时长出多种不该同时出现的结构」
 *   ② 个股解剖  点进去，看那段区间里价格、结构、席位分别长什么样
 *   ③ 案例库    把人写的外部信息（处罚文书 / 爆料 / 报道）贴到同一条时间轴上对账
 *   ④ 判据检验  这些结构在历史上命中之后到底发生了什么，误报率多少
 *
 * 页面不回答「谁是庄」。它只回答三个能被检验的问题：
 *   这段结构在历史上出现过多少次？命中之后 1/5/20 日相对对照组如何？
 *   我手里的叙事，与本地痕迹对得上吗？
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { BookOpen, FlaskConical, ScanSearch, ShieldAlert } from 'lucide-react'
import { api, type CaseDoc } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { cn } from '@/lib/cn'
import { PageHeader } from '@/components/PageHeader'
import { ScanPanel } from './case-lab/ScanPanel'
import { DissectPanel, type DissectSeed } from './case-lab/DissectPanel'
import { CasesPanel } from './case-lab/CasesPanel'
import { StatsPanel } from './case-lab/StatsPanel'

type Tab = 'scan' | 'dissect' | 'cases' | 'stats'

const TABS: { id: Tab; label: string; icon: typeof ScanSearch; hint: string }[] = [
  { id: 'scan', label: '结构异常', icon: ScanSearch, hint: '最近哪些票同时命中了多种结构' },
  { id: 'dissect', label: '个股解剖', icon: FlaskConical, hint: '一段区间的价格、结构、席位' },
  { id: 'cases', label: '案例库', icon: BookOpen, hint: '外部信息与本地痕迹对账' },
  { id: 'stats', label: '判据检验', icon: ShieldAlert, hint: '每条手法在历史上命中之后' },
]

export function CaseLab() {
  // tab 进 URL：截图、分享、从「解剖」跳「案例库」都只靠一个可回退的地址。
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const tab: Tab = TABS.some(t => t.id === raw) ? (raw as Tab) : 'scan'
  const setTab = (id: Tab) => {
    const next = new URLSearchParams(params)
    next.set('tab', id)
    setParams(next, { replace: true })
  }
  const [seed, setSeed] = useState<DissectSeed | null>(null)
  const [draft, setDraft] = useState<Partial<CaseDoc> | null>(null)

  const cases = useQuery({
    queryKey: QK.caseLabCases(),
    queryFn: () => api.caseLabCases(),
    staleTime: 30_000,
  })

  const caseBrief = useMemo(
    () => (cases.data?.items ?? []).map(c => ({ id: c.id, symbol: c.symbol, title: c.title })),
    [cases.data],
  )

  const pick = (symbol: string, start: string, end: string) => {
    setSeed({ symbol, start, end })
    setTab('dissect')
  }

  const saveCase = (d: Partial<CaseDoc>) => {
    setDraft(d)
    setTab('cases')
  }

  return (
    <div className="min-h-full bg-base pb-8">
      <PageHeader
        title="操盘档案"
        subtitle="用盘后可算的痕迹，去对账事后才知道的叙事。这一页不猜谁是庄，只把「结构 → 后果」和「叙事 → 痕迹」两笔账摊开。"
        right={
          <div className="flex items-center gap-2 text-[10px] text-muted">
            <span className="hidden sm:inline">判据统计：</span>
            <span className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono">1 / 5 / 20 日</span>
            <span className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono">匹配对照</span>
          </div>
        }
      />

      <div className="flex flex-wrap items-center gap-1 border-b border-border px-5 py-2">
        {TABS.map(t => {
          const Icon = t.icon
          const on = tab === t.id
          return (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              title={t.hint}
              className={cn(
                'flex items-center gap-1.5 rounded-btn border px-2.5 py-1 text-[11px] transition-colors',
                on
                  ? 'border-accent/40 bg-accent/10 text-accent'
                  : 'border-transparent text-secondary hover:bg-elevated hover:text-foreground',
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {t.label}
            </button>
          )
        })}
        <span className="ml-2 hidden text-[10px] text-muted sm:inline">
          {TABS.find(t => t.id === tab)?.hint}
        </span>
      </div>

      {tab === 'scan' && <ScanPanel onPick={pick} />}
      {tab === 'dissect' && (
        <DissectPanel seed={seed} onSaveCase={saveCase} cases={caseBrief} />
      )}
      {tab === 'cases' && (
        <CasesPanel draft={draft} onDraftConsumed={() => setDraft(null)} />
      )}
      {tab === 'stats' && <StatsPanel />}

      <footer className="mx-5 mt-4 rounded-card border border-border bg-surface px-3 py-2 text-[10px] leading-relaxed text-muted">
        <b className="text-secondary">边界声明：</b>
        本页只读本地日线（2005 起）、龙虎榜（2025-11 起）、标的维表，加上你自己录入的案例文本。
        公告全文、大宗交易、股东名册、质押与减持数据都不在仓库里，因此「本地未见痕迹」只表示
        <span className="text-secondary">数据看不到</span>，不表示叙事为假。
        页面里的结构命中是统计特征，不构成对任何主体操纵的指控，也不是交易建议。
        所有判据只用 T 日及之前的数据；T+1 收益按「次日开盘买、收盘卖」并剔除开盘即涨停的样本。
      </footer>
    </div>
  )
}
