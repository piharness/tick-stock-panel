/**
 * 信息时间差瀑布 —— 一条「利好」从诞生到被你看到，经过了几层、价格反映到哪。
 *
 * 这是「你看到消息时它已被定价」这条常识的**可视化**。信息有层级和速度差，
 * 每往下一层，价格已反映的比例递增。当信息流到「散户」这一层时，价格基本已经
 * 定价完毕，剩下的只有噪音和情绪。
 *
 * 诚实边界（重要）：「价格已反映」的**具体百分比无法逐笔测量**，本地也没有公告表
 * 能实测「利好兑现衰减」。所以本图**不画假精确的数字**（不写 100%/95%/85% 这种
 * 编造的刻度），只画一条**单调方向**：越往下、越晚知道，价格越已反映。这个方向
 * 是确定且可验证的（内幕交易必然提前定价，是信息经济学的基本结论）。
 *
 * 下方「实证」补充一个可证伪的侧面：龙虎榜里机构（先动手）与散户（后动手）的
 * 方向同向率约 67% —— 说明「机构买散户也买」占多数，但仍有 1/3 时间相反，
 * 且机构方向对后续收益没有预测力（见博弈推演页的完整判决）。
 */
import { useMemo } from 'react'
import * as echarts from 'echarts'
import { useChartTheme } from '@/lib/theme'
import { useECharts } from '@/pages/backtest/charts/useECharts'

interface Layer {
  name: string
  /** 层级序号（0=最早知道，越大越晚）—— 只用来排序，不承载假精确刻度 */
  order: number
  note: string
}

const LAYERS: Layer[] = [
  { name: '内幕知情人', order: 0, note: '信息源头，提前定价，且交易即违法' },
  { name: '机构 / 卖方研报', order: 1, note: '第一时间解读并建仓' },
  { name: '专业投资者 / 游资', order: 2, note: '跟风但仍有信息优势' },
  { name: '财经媒体', order: 3, note: '开始加工成「新闻」' },
  { name: '社交媒体 / 股吧', order: 4, note: '噪音放大，真假混杂' },
  { name: '散户（你）', order: 5, note: '看到时，利好已基本兑现' },
]

export function InfoCascade() {
  const ct = useChartTheme()

  const option = useMemo(() => {
    // 用「层深」0..5 映射到「价格已反映」的相对位置（只表达单调，不表达精确百分比）
    const maxOrder = LAYERS.length - 1
    return {
      backgroundColor: 'transparent',
      grid: { left: 92, right: 24, top: 20, bottom: 24 },
      tooltip: {
        trigger: 'item',
        backgroundColor: ct.tooltipBg,
        borderColor: ct.tooltipBorder,
        textStyle: { color: ct.tooltipText, fontSize: 11 },
        formatter: (p: any) => {
          const l = LAYERS[p.dataIndex]
          return `${l.name}<br/><span style="color:#999">${l.note}</span><br/><span style="color:#999">越靠下，价格越已反映（相对位置，非精确百分比）</span>`
        },
      },
      xAxis: {
        type: 'value',
        name: '价格已反映（相对，越右越已定价）',
        nameTextStyle: { color: ct.text, fontSize: 10 },
        axisLabel: { color: ct.text, fontSize: 10, show: false },
        axisLine: { lineStyle: { color: ct.border } },
        splitLine: { lineStyle: { color: ct.grid } },
        min: 0,
        max: 6,
      },
      yAxis: {
        type: 'category',
        inverse: true,
        data: LAYERS.map(l => l.name),
        axisLabel: { color: ct.textStrong, fontSize: 11 },
        axisLine: { lineStyle: { color: ct.border } },
        axisTick: { show: false },
      },
      series: [
        {
          type: 'bar',
          data: LAYERS.map(l => (l.order / maxOrder) * 5 + 0.5),
          barWidth: 18,
          showBackground: true,
          backgroundStyle: { color: ct.fillSubtle },
          label: {
            show: true,
            position: 'right',
            formatter: () => '▸ 已反映更多',
            color: ct.text,
            fontSize: 9,
          },
          itemStyle: {
            borderRadius: [0, 3, 3, 0],
            color: (p: any) => {
              // 越靠散户越「危险」（信息越不值钱），红→绿渐变
              const idx = p.dataIndex
              const colors = ['#16A34A', '#4ADE80', '#A3E635', '#FACC15', '#FB923C', '#EF4444']
              return colors[idx]
            },
          },
        },
      ],
    } as echarts.EChartsOption
  }, [ct])

  const ref = useECharts(option, [ct])

  return (
    <div>
      <div ref={ref} style={{ height: 300 }} />
      <p className="mt-1 text-[10px] leading-relaxed text-muted/80">
        这张图只表达<b className="text-secondary">单调方向</b>：越往下、越晚知道，价格越已反映。当它流到散户这一层，
        利好往往只剩情绪、不剩超额收益。<b className="text-secondary">「价格已反映」的具体百分比无法精确测量</b>，
        所以这里不写假精确的数字——方向是确定的，刻度不是。
      </p>
    </div>
  )
}

