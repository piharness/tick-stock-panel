import { ShieldAlert, AlertTriangle, CheckCircle2, CircleDashed, Loader2 } from 'lucide-react'
import type { RiskGate, RiskGateStatus, StockRiskGuard } from '@/lib/api'

/**
 * 退市红线面板 —— 页面上唯一回答「它还活不活得下去」的模块。
 *
 * 其余模块（关键价位、AI 四维分析、简称轨迹）都在描述「它涨得像什么」。
 * 对一只 *ST 股来说，支撑位与目标价都成立在「明天还挂牌」这个前提上，
 * 而这个前提有明确的、可逐日计数的规则：面值、市值、净资产、净利润与营收。
 *
 * 三条纪律：
 *  - unknown 不画成 safe：缺数据的项显示成灰色虚线并写明原因，不给绿勾；
 *  - caveats 不折叠：它说明「本面板不给概率」，这跟数字一样重要；
 *  - 只在 ST 或任一条红线越线时出现：普通股不占版面，但逼近面值的票会出现。
 */

const STATUS_STYLE: Record<RiskGateStatus, { dot: string; text: string; icon: typeof ShieldAlert }> = {
  triggered: { dot: 'text-danger', text: 'text-danger', icon: ShieldAlert },
  counting: { dot: 'text-warning', text: 'text-warning', icon: AlertTriangle },
  near: { dot: 'text-warning', text: 'text-warning', icon: AlertTriangle },
  safe: { dot: 'text-muted', text: 'text-secondary', icon: CheckCircle2 },
  unknown: { dot: 'text-muted/50', text: 'text-muted', icon: CircleDashed },
}

/** 每条红线用各自的量纲：面值是元、市值是亿、财务是亿。 */
function fmtValue(gate: RiskGate): string {
  if (gate.value == null) return '—'
  if (gate.id === 'par') return `${gate.value.toFixed(2)} 元`
  const yi = gate.value / 1e8
  return `${yi >= 0 ? '' : ''}${yi.toFixed(2)} 亿`
}

function shouldShow(data: StockRiskGuard | undefined): boolean {
  if (!data) return false
  if (data.is_st) return true
  return data.gates.some(g => g.status === 'triggered' || g.status === 'counting' || g.status === 'near')
}

export function DelistingGuardPanel({
  data, loading, error,
}: {
  data: StockRiskGuard | undefined
  loading: boolean
  error: boolean
}) {
  if (loading && !data) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted flex items-center gap-2">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        正在算退市红线…
      </div>
    )
  }

  // 补充信息取不到时不能整块消失：消失会让用户以为「没有风险」
  if (error && !data) {
    return (
      <div className="rounded-lg border border-border/40 bg-base/30 px-4 py-2.5 text-[11px] text-muted">
        退市红线数据不可用（接口失败）—— 本面板不显示，不等于没有风险。
      </div>
    )
  }

  if (!shouldShow(data)) return null
  const guard = data!

  return (
    <div className="rounded-lg border border-border/40 bg-base/30 overflow-hidden" data-testid="delisting-guard">
      <div className="px-4 py-2.5 border-b border-border/30 flex items-center gap-2">
        <ShieldAlert className="h-4 w-4 text-warning shrink-0" />
        <span className="text-[13px] font-medium text-foreground">退市红线</span>
        {guard.is_st && (
          <span className="rounded bg-danger/10 px-1.5 py-0.5 text-[10px] text-danger font-medium">
            {guard.st_kind || 'ST'} · {guard.board}
          </span>
        )}
        {guard.counts.verdict && (
          <span className="ml-auto text-[11px] text-secondary">{guard.counts.verdict}</span>
        )}
        {guard.as_of && <span className="text-[10px] font-mono text-muted/70">{guard.as_of}</span>}
      </div>

      <div className="px-4 py-2.5 space-y-2">
        {guard.gates.map(gate => (
          <GateRow key={gate.id} gate={gate} />
        ))}

        {guard.counts.equity_trend && (
          <div className="pt-1.5 border-t border-border/20 text-[11px] text-secondary">
            净资产趋势：{guard.counts.equity_trend}
          </div>
        )}

        {guard.caveats.length > 0 && (
          <ul className="pt-1.5 space-y-0.5">
            {guard.caveats.map(c => (
              <li key={c} className="text-[10px] leading-relaxed text-muted/80">
                · {c}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  )
}

function GateRow({ gate }: { gate: RiskGate }) {
  const style = STATUS_STYLE[gate.status] ?? STATUS_STYLE.unknown
  const Icon = style.icon
  const showBar = (gate.status === 'counting' || gate.status === 'triggered')
    && gate.streak != null && gate.streak_limit != null

  return (
    <div className="flex items-start gap-2">
      <Icon className={`h-3.5 w-3.5 mt-0.5 shrink-0 ${style.dot}`} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="text-[12px] text-foreground shrink-0">{gate.label}</span>
          <span className={`text-[12px] font-mono ${style.text}`}>{fmtValue(gate)}</span>
          {showBar && (
            <span className="ml-auto font-mono text-[10px] text-muted shrink-0">
              {gate.streak}/{gate.streak_limit}
            </span>
          )}
        </div>
        <div className={`text-[10px] leading-relaxed ${style.text} opacity-90`}>{gate.detail}</div>
        {showBar && (
          // 20 个格子的倒计时：比「1/20」更容易看出还剩几天
          <div className="mt-1 flex gap-[2px]">
            {Array.from({ length: gate.streak_limit! }).map((_, i) => (
              <span
                key={i}
                className={`h-[3px] flex-1 rounded-sm ${
                  i < (gate.streak ?? 0) ? 'bg-warning' : 'bg-border/60'
                }`}
              />
            ))}
          </div>
        )}
        <div className="text-[10px] text-muted/60 mt-1">{gate.rule}</div>
      </div>
    </div>
  )
}
