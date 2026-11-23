/**
 * 博弈动机推演 —— 把「对手不会故意犯错」变成可操作的反问。
 *
 * 这是「逆向推理排除答案」这条常识的**思维框架**。它不画任何编造的「角色买入
 * 倾向」数字（那些数字没有任何数据依据，画出来只会制造假精确），只保留一件事：
 *
 *   在你考虑买入的每个价位，问自己：谁有动机在这里卖给我？
 *
 * 如果答案是「聪明钱」「主力」「懂行的老股东」，那接盘的只能是更不知情的人。
 * 这不是玄学，是一个可逐格核对的反问。
 *
 * 而「聪明钱到底有没有方向优势」这件事，本页已经用龙虎榜数据判决过了（见下方
 * 龙虎榜实证面板）：机构 vs 游资方向经常相反，但机构方向对后续收益**没有预测力**。
 * 所以「跟着聪明钱走」本身就是一条需要证伪的叙事，而不是可以依赖的前提。
 */
import { cn } from '@/lib/cn'

interface Question {
  title: string
  detail: string
  hint: string
}

const QUESTIONS: Question[] = [
  {
    title: '谁心甘情愿在这个价位卖给我？',
    detail: '如果卖家是比我更懂这只票的人，那我买入的「便宜」，其实是别人的「出货」。',
    hint: '低位也有出货：价值资金发现基本面变了，一样会在低位离场。',
  },
  {
    title: '我买的理由，卖家为什么不知道？',
    detail: '如果这个理由是公开信息（财报、新闻、概念），那它已经被定价了——卖给我的人早就知道。',
    hint: '能带来超额收益的，只能是你知道而市场还不知道的东西，否则就是接盘。',
  },
  {
    title: '如果我看错了，多久、多大代价会知道？',
    detail: '做局者的成本很低（他可以慢慢卖），而散户的成本很高（满仓被套）。双方的下行空间不对称。',
    hint: '这正是散户最吃亏的地方：赢的时候赚小钱，输的时候输大钱。',
  },
]

export function GameTheoryLab() {
  return (
    <div>
      <div className="space-y-2">
        {QUESTIONS.map((q, i) => (
          <div key={q.title} className="rounded border border-border/60 bg-elevated/30 px-3 py-2">
            <div className="flex items-start gap-2">
              <span className={cn(
                'mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full text-[10px] font-medium',
                'bg-accent/15 text-accent',
              )}>
                {i + 1}
              </span>
              <div className="min-w-0">
                <div className="text-[11px] font-medium text-foreground">{q.title}</div>
                <p className="mt-0.5 text-[10px] leading-relaxed text-secondary">{q.detail}</p>
                <p className="mt-0.5 text-[9px] leading-relaxed text-muted/70">{q.hint}</p>
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="mt-3 rounded border border-warning/20 bg-warning/[0.04] px-3 py-2 text-[10px] leading-relaxed text-muted">
        <b className="text-secondary">关键提醒：</b>
        这三个反问能帮你「排除」经不起推敲的叙事，但它<b className="text-foreground">不预测任何具体股票</b>。
        「聪明钱有方向优势」这个前提本身，需要数据验证——本页下方的龙虎榜实证已经判决：
        机构方向对后续收益<b className="text-foreground">没有显著预测力</b>。所以别把「跟着主力走」当成可靠策略，
        它只是另一个需要证伪的叙事。
      </div>
    </div>
  )
}
