/**
 * 退市流程时间轴 —— 一只 ST 股「从戴帽到退市」的全流程，倒计时可视化。
 *
 * 这是「退市三条红线」常识的**流程化升级**：散户常把「ST」「*ST」「退市」混成一团，
 * 其实它们是**不同阶段、不同触发、不同结局**的递进链条。本图把它们摆成一条时间轴，
 * 标出每一站的触发条件与「还能不能回头」。
 *
 * 关键区分（散户最易混的三件事）：
 *   - ST：其它风险警示（如持续经营存疑），不等于退市，也不涨跌幅受限 5%（2026-07 已改 10%）
 *   - *ST：退市风险警示，是「正在走向退市」的信号，但仍有摘帽可能
 *   - 终止上市：硬红线连续触发（面值/市值 20 日、财务连续 2 年），这是终点，不可逆
 */
import { cn } from '@/lib/cn'

interface Stage {
  key: string
  name: string
  trigger: string
  reversible: boolean
  reversibleNote: string
  tone: 'neutral' | 'warn' | 'danger'
}

const STAGES: Stage[] = [
  {
    key: 'normal',
    name: '正常上市',
    trigger: '无风险警示',
    reversible: true,
    reversibleNote: '—',
    tone: 'neutral',
  },
  {
    key: 'st',
    name: 'ST（其他风险警示）',
    trigger: '持续经营存疑 / 违规担保 / 资金占用等',
    reversible: true,
    reversibleNote: '可摘帽，不必然退市',
    tone: 'neutral',
  },
  {
    key: 'xst',
    name: '*ST（退市风险警示）',
    trigger: '触及财务类 / 交易类退市风险',
    reversible: true,
    reversibleNote: '可摘帽，但已进入「倒数」',
    tone: 'warn',
  },
  {
    key: 'delisting',
    name: '终止上市',
    trigger: '面值/市值连续 20 日，或财务连续 2 年',
    reversible: false,
    reversibleNote: '不可逆，进入退市整理期',
    tone: 'danger',
  },
]

const TONE_STYLE: Record<Stage['tone'], string> = {
  neutral: 'border-border bg-elevated/40',
  warn: 'border-amber-500/30 bg-amber-500/[0.05]',
  danger: 'border-red-500/30 bg-red-500/[0.06]',
}

const TONE_DOT: Record<Stage['tone'], string> = {
  neutral: 'bg-zinc-400',
  warn: 'bg-amber-500',
  danger: 'bg-red-500',
}

export function DelistingTimeline() {
  return (
    <div>
      <div className="flex flex-col gap-2">
        {STAGES.map((s, i) => (
          <div key={s.key} className="relative flex gap-3">
            {/* 时间轴竖线 */}
            <div className="flex flex-col items-center">
              <span className={cn('mt-1 h-3 w-3 rounded-full ring-4 ring-base', TONE_DOT[s.tone])} />
              {i < STAGES.length - 1 && <span className="w-px flex-1 bg-border/60" />}
            </div>
            {/* 阶段卡片 */}
            <div className={cn('mb-1 flex-1 rounded-card border px-3 py-2', TONE_STYLE[s.tone])}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[12px] font-medium text-foreground">{s.name}</span>
                <span
                  className={cn(
                    'rounded px-1.5 py-0.5 text-[9px] font-medium',
                    s.reversible
                      ? 'border border-emerald-500/30 bg-emerald-500/10 text-emerald-600'
                      : 'border border-red-500/30 bg-red-500/10 text-red-600',
                  )}
                >
                  {s.reversible ? '可回头' : '不可逆'}
                </span>
              </div>
              <div className="mt-0.5 text-[10px] text-secondary">触发：{s.trigger}</div>
              <div className="text-[9px] text-muted/70">{s.reversibleNote}</div>
            </div>
          </div>
        ))}
      </div>

      {/* 倒计时说明 */}
      <div className="mt-3 rounded border border-warning/20 bg-warning/[0.04] px-3 py-2 text-[10px] leading-relaxed text-muted">
        <b className="text-secondary">核心洞见：</b>
        ST 不等于退市，*ST 也不等于退市 —— 真正不可逆的是「终止上市」。散户的侥幸心理，
        往往是把「*ST 的倒数」误读成「还有机会翻身」。而规则真正残酷的地方在于：
        <b className="text-foreground">从 *ST 到终止上市之间，每一步都是可逐日计数的硬约束</b>，
        不是概率游戏 —— 连续 20 日低于面值，就是 20 日，一天都少不得，也一天都多不得。
      </div>
    </div>
  )
}
