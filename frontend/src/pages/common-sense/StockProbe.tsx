/**
 * 个股判据 —— 输入一个代码，把九条「行业常识」落到这只票上。
 *
 * 这一块解决的是上一版最大的缺口：页面上的实时数字全是**市场级**的
 * （今天几只破面值、今天摩擦成本多少亿），而读者真正要判断的是「我手里这一只」。
 * 九条判定本来只活在「博弈链路」页里，现在接到常识体系上：点任意一条，跳到它
 * 对应的那条常识，看它的依据、误解与自查入口。
 *
 * 三条落地纪律：
 *   1. 数值全部来自后端 /api/table-game/board（阈值只存在一份，前端不重判）；
 *   2. 九条永远显示九格——「无数据」也是一格，并写明缺哪张表（不显示 0，那会被读成「没问题」）；
 *   3. 「成立」不等于「看懂了」：形状只说明形态存在。这句话必须出现在界面上。
 */
import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Crosshair, Loader2, Search, X } from 'lucide-react'
import { api, type TableGameBoard } from '@/lib/api'
import { cn } from '@/lib/cn'
import { RULE_STATE } from '@/lib/ruleState'
import {
  PROBES, probeItemId, probeLookup, probeSpec, probeTally, probeValueText, type ProbeId,
} from './probes'

export function StockProbe({
  symbol, onSymbol, board, loading, error, onJump,
}: {
  symbol: string | null
  onSymbol: (s: string | null) => void
  board: TableGameBoard | undefined
  loading: boolean
  error: boolean
  /** 跳到对应常识条目（null 表示这条判据没有挂载点，按钮禁用） */
  onJump: (itemId: string) => void
}) {
  const tally = useMemo(() => probeTally(board), [board])
  const meta = board?.meta

  return (
    <section className="rounded-card border border-border bg-surface/80 p-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <Crosshair className="h-3.5 w-3.5 text-accent" />
        <h2 className="text-[12px] font-medium text-foreground">个股判据</h2>
        <span className="text-[10px] text-muted">
          九条说法落到这一只票上 · 点任意一条看它的依据与误解
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <SymbolSearch onPick={s => onSymbol(s)} />
          {symbol && (
            <button
              onClick={() => onSymbol(null)}
              title="清空，只看市场级事实"
              className="rounded-btn border border-border px-1.5 py-1 text-[10px] text-muted transition-colors hover:bg-elevated hover:text-foreground"
            >
              清空
            </button>
          )}
        </div>
      </div>

      {/* 未选票：不占主屏，只说明这里能做什么 */}
      {!symbol && (
        <p className="mt-2 rounded border border-dashed border-border/70 bg-base/20 px-2 py-2 text-[10px] leading-relaxed text-muted">
          还没有选票。搜索代码或名称（例如 600519 / 茅台），九条判据会立刻落到这一只票上；
          算不出来的那几条会标「无数据」并写明缺哪张表，<b className="text-secondary">不会显示 0</b>。
        </p>
      )}

      {symbol && (
        <>
          {/* 口径行：数字属于哪只票、哪一天 */}
          <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 rounded border border-border/60 bg-base/25 px-2 py-1.5">
            <span className="text-[12px] font-medium text-foreground">
              {meta?.name ?? '—'}
              <span className="ml-1.5 font-mono text-[10px] text-muted">{symbol}</span>
            </span>
            {loading && <Loader2 className="h-3 w-3 animate-spin text-muted" />}
            {error && <span className="text-[10px] text-danger">接口失败：判定没取到，不是「没问题」</span>}
            {meta?.symbol && (
              <span className="flex flex-wrap items-baseline gap-x-2 font-mono text-[10px] text-muted">
                <span>收盘 <b className="text-secondary">{fmt(meta.close)}</b></span>
                <span>流通市值 <b className="text-secondary">{yi(meta.float_cap)}</b></span>
                <span>换手 <b className="text-secondary">{fmt(meta.turnover_rate)}%</b></span>
                <span>ROE <b className="text-secondary">{meta.roe == null ? '—' : `${fmt(meta.roe)}%`}</b></span>
                {board?.as_of && <span className="text-muted/60">as_of {board.as_of}</span>}
              </span>
            )}
            {board?.state === 'ok' && (
              <span className="ml-auto flex flex-wrap items-center gap-1 text-[9px] text-muted">
                <span className="rounded bg-warning/15 px-1 py-px text-warning">成立 {tally.hit}</span>
                <span className="rounded bg-accent/10 px-1 py-px text-accent">算术 {tally.derived}</span>
                <span className="rounded bg-elevated px-1 py-px">不成立 {tally.clear}</span>
                <span className="rounded bg-elevated px-1 py-px">本仓库看不见 {tally.blind}</span>
              </span>
            )}
          </div>

          {/* 九格：缺数据也占一格 */}
          <div className="mt-1.5 grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
            {PROBES.map(p => (
              <ProbeCell
                key={p.id}
                id={p.id}
                board={board}
                onJump={onJump}
              />
            ))}
          </div>

          <p className="mt-2 text-[9px] leading-relaxed text-muted/80">
            <b className="text-secondary">形状不是证据。</b>
            「成立」只说明这只票当前具备该形态，不说明背后是谁，也不说明下一步会怎样；
            「不成立」是结论而不是空白；「无数据」是本仓库缺那张表——三者不能互相代替。
            阈值与判定全部由后端日线算出（<span className="font-mono">_stock_rules</span>），前端不重判一次。
          </p>
        </>
      )}
    </section>
  )
}

function ProbeCell({
  id, board, onJump,
}: {
  id: ProbeId
  board: TableGameBoard | undefined
  onJump: (itemId: string) => void
}) {
  const itemId = probeItemId(id)
  const look = probeLookup(board, id)
  const spec = probeSpec(id)

  if (look.kind === 'unavailable') {
    return (
      <div className="rounded border border-border/60 bg-base/20 px-2 py-1.5">
        <div className="flex items-center gap-1">
          <span className="rounded bg-elevated px-1 py-px text-[9px] text-muted">无法判定</span>
          <span className="text-[11px] font-medium text-secondary">{spec.label}</span>
        </div>
        <p className="mt-0.5 text-[9px] leading-relaxed text-muted">{look.reason}</p>
      </div>
    )
  }

  const r = look.rule
  const st = RULE_STATE[r.state]
  return (
    <button
      onClick={() => itemId && onJump(itemId)}
      disabled={!itemId}
      title={itemId ? `查看常识：${r.label}` : '这条判据没有挂载常识条目'}
      className={cn(
        'group rounded border px-2 py-1.5 text-left transition-colors',
        r.state === 'triggered'
          ? 'border-warning/40 bg-warning/[0.06] hover:bg-warning/[0.1]'
          : 'border-border/60 bg-base/20 hover:border-border hover:bg-elevated/40',
      )}
    >
      <div className="flex items-baseline gap-1">
        <span className={cn('shrink-0 rounded px-1 py-px text-[9px]', st.cls)} title={st.hint}>
          {st.label}
        </span>
        <span className="truncate text-[11px] font-medium text-foreground">{r.label}</span>
        <span className={cn('ml-auto shrink-0 font-mono text-[11px]', st.text)}>
          {probeValueText(r)}
        </span>
      </div>
      {r.unit && <div className="mt-px text-right text-[8px] text-muted/70">{r.unit}</div>}
      <p className="mt-0.5 line-clamp-1 text-[9px] text-muted/80" title={spec.question}>{spec.question}</p>
      {r.detail && (
        <p className="mt-0.5 line-clamp-2 font-mono text-[8px] leading-relaxed text-muted/70" title={r.detail}>
          {r.detail}
        </p>
      )}
    </button>
  )
}

/** 代码 / 名称搜索。回车或点结果即选中；交易所后缀由后端 instruments 决定，前端不猜。 */
function SymbolSearch({ onPick }: { onPick: (s: string) => void }) {
  const [typing, setTyping] = useState('')
  const [debounced, setDebounced] = useState('')
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const listId = useId()

  useEffect(() => {
    const t = window.setTimeout(() => setDebounced(typing.trim()), 220)
    return () => window.clearTimeout(t)
  }, [typing])

  const q = useQuery({
    queryKey: ['probe', 'instrument-search', debounced],
    queryFn: () => api.instrumentSearch(debounced, 8, 'stock'),
    enabled: debounced.length >= 2,
    staleTime: 60_000,
    retry: false,
  })

  // 点外面关掉下拉：否则它会盖住下面的判据格
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])

  const results = q.data?.results ?? []
  const pick = (s: string) => {
    onPick(s)
    setTyping('')
    setOpen(false)
  }

  const submit = () => {
    const raw = typing.trim()
    if (!raw) return
    // 已经是完整 symbol 就直接用；否则交给后端搜索决定交易所（前端不重写一套前缀映射）
    if (/^\d{6}\.(SH|SZ|BJ)$/i.test(raw)) {
      pick(raw.toUpperCase())
      return
    }
    if (results.length > 0) pick(results[0].symbol)
  }

  return (
    <div ref={boxRef} className="relative">
      <div className="flex items-center gap-1 rounded-btn border border-border bg-base/40 px-1.5 py-1">
        <Search className="h-3 w-3 text-muted" />
        <input
          value={typing}
          onChange={e => {
            setTyping(e.target.value.toUpperCase())
            setOpen(true)
          }}
          onFocus={() => setOpen(true)}
          onKeyDown={e => {
            if (e.key === 'Enter') submit()
            if (e.key === 'Escape') setOpen(false)
          }}
          placeholder="代码 / 名称"
          aria-controls={listId}
          className="w-[92px] bg-transparent text-[10px] text-foreground outline-none placeholder:text-muted/50"
        />
        {typing && (
          <button onClick={() => setTyping('')} className="text-muted hover:text-foreground">
            <X className="h-3 w-3" />
          </button>
        )}
      </div>

      {open && debounced.length >= 2 && (
        <div
          id={listId}
          className="absolute right-0 top-full z-30 mt-1 w-[240px] overflow-hidden rounded-card border border-border bg-surface shadow-lg"
        >
          {q.isLoading && (
            <div className="flex items-center gap-1 px-2 py-1.5 text-[10px] text-muted">
              <Loader2 className="h-3 w-3 animate-spin" />搜索中…
            </div>
          )}
          {!q.isLoading && results.length === 0 && (
            <div className="px-2 py-1.5 text-[10px] text-muted">
              没有匹配的股票。数字代码也可直接输入后回车。
            </div>
          )}
          {results.map(r => (
            <button
              key={r.symbol}
              onClick={() => pick(r.symbol)}
              className="flex w-full items-baseline gap-2 px-2 py-1 text-left transition-colors hover:bg-elevated"
            >
              <span className="font-mono text-[10px] text-muted">{r.symbol}</span>
              <span className="truncate text-[11px] text-foreground">{r.name}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** 无法判定时的标题（这时后端没给 rule.label，只能用本地占位名——正常路径用后端 label）。 */
const fmt = (v: number | null | undefined): string =>
  v == null ? '—' : Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2)

const yi = (v: number | null | undefined): string =>
  v == null ? '—' : `${(v / 1e8).toFixed(v >= 1e10 ? 0 : 1)} 亿`
