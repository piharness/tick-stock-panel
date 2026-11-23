import { useEffect, useRef, useMemo, useState } from 'react'
import { chartTheme, getTheme, useTheme } from '@/lib/theme'
import * as echarts from 'echarts'
import type { ECharts, EChartsOption } from 'echarts'
import type { KlineRow, LevelSeries } from '@/lib/api'
import { clampToDates } from '@/lib/chartRange'

/**
 * 个股分析专用日 K 图表。
 *
 * 与 StockDailyKChart/EChartsCandlestick 刻意不复用:
 *   - 那套图表面向「行情浏览」,强调全套指标副图(MA/MACD/KDJ/BOLL)、涨停标记等;
 *   - 本图表面向「分析决策」,核心是【关键价位】(压力/支撑/密集区/枢轴/前高前低),
 *     通过开关按钮控制各价位组的显隐,布局更简洁(主图 + 成交量即可)。
 *
 * 预留接口(类型已定义,渲染逻辑留 hook,后续实现):
 *   - markers: 日期标记点(新闻/暴雷/利好 → markPoint)
 *   - ranges:  区间高亮(事件区间 → markArea)
 *   - onDateClick: 点击日期回调(后续接消息面时间轴)
 *   - 指标副图: 后续如需 MACD/KDJ,按 SUB_CHARTS 模式扩展
 */

// ===== 配色(红涨绿跌, 双主题通用); 画布轴/网格主题相关色走 CT() =====
const THEME = {
  bull: '#C74040',
  bear: '#2D9B65',
  volUp: 'rgba(240,68,56,0.5)',
  volDown: 'rgba(18,183,106,0.5)',
}

/** 当前主题的图表调色板 (buildOption 渲染时调用; 切换由组件 effect 触发重建)。 */
const CT = () => chartTheme(getTheme())

// ===== 价位类型(与后端 levels.py 的 LEVEL_TYPES 对齐) =====
export type LevelType = 'sr' | 'pivot' | 'extreme' | 'boll' | 'keltner_s' | 'keltner_m' | 'keltner_l' | 'atr_stop' | 'gap' | 'fib' | 'round'

export interface PriceLevel {
  value: number
  label: string
  type: LevelType
  side: 'resistance' | 'support' | 'neutral'
  strength?: 'strong' | 'medium' | 'weak'
  /** 档位(仅 pivot 有):0=P, 1=R1/S1, 2=R2/S2, 3=R3/S3 */
  rank?: number
}

/**
 * 价位组开关配置: label=按钮文案, color=markLine 颜色, hint=鼠标悬停的解释。
 *
 * hint 的措辞全部来自后端 `app/indicators/levels.py` 的算法注释 —— 页面上每个
 * 能打开的开关都要能回答"这条线是什么、怎么算的"。之前只有数量没有含义，
 * 看不懂的人只能全开，图上 20 条线互相压着，等于没有价位。
 *
 * `kind` 决定这条线在时间轴上的画法，是本节最重要的一列：
 *   - `snapshot`：后端用**截至最新交易日**的数据算出一个数（筹码密集区、
 *     枢轴档位、60/250 日极值、未回补缺口、近期回撤位、当前价附近的整数位）。
 *     它只描述"今天"的位置；画到 2024 年那一段等于把未来的结论贴到过去。
 *     所以这类线只在视窗包含最新一根 K 时出现，且只画最后两根的短横线。
 *   - `series`：逐日递推的带（MA±kσ / MA±k·ATR），第 t 根只用到 t 及之前，
 *     可以整段画，也不存在"提前知道"。
 */
export const LEVEL_GROUPS: {
  key: LevelType
  label: string
  color: string
  hint: string
  kind: 'snapshot' | 'series'
}[] = [
  { key: 'sr',        kind: 'snapshot', label: '压力支撑',     color: '#F97316', hint: '筹码分布（按换手率衰减）：每个价位还剩多少持仓成本。现价上方最密集的是压力，下方最密集的是支撑。按最新交易日计算，只画在今天。' },
  { key: 'pivot',     kind: 'snapshot', label: '枢轴点',       color: '#8B5CF6', hint: '经典枢轴：P=(H+L+C)/3，再由上一根 K 推出 R1/R2/R3 与 S1/S2/S3。档位按钮控制画到第几档。' },
  { key: 'extreme',   kind: 'snapshot', label: '前高前低',     color: '#EAB308', hint: '60 日与 250 日极值，以及近期摆动高低点。极值截至最新交易日。' },
  { key: 'boll',      kind: 'series',   label: '布林带',       color: '#F97316', hint: 'MA20 ± 2σ。它是一条波动带，不等于真实的支撑压力。逐日计算，可整段回溯。' },
  { key: 'keltner_s', kind: 'series',   label: 'Keltner短期',  color: '#06B6D4', hint: 'MA20 ± 2×ATR，约一个月级别的波动带。' },
  { key: 'keltner_m', kind: 'series',   label: 'Keltner中期',  color: '#22D3EE', hint: 'MA60 ± 2.5×ATR，约一个季度。' },
  { key: 'keltner_l', kind: 'series',   label: 'Keltner长期',  color: '#67E8F9', hint: 'MA120 ± 3×ATR，用来划趋势边界。' },
  { key: 'atr_stop',  kind: 'series',   label: 'ATR波动通道',  color: '#EF4444', hint: '收盘 ±2×ATR 的动态带。中性命名，不是买卖指令。' },
  { key: 'gap',       kind: 'snapshot', label: '缺口位',       color: '#EC4899', hint: '未回补的跳空缺口：向上缺口=当日最低>前日最高，向下相反。「未回补」是截至最新交易日的判断。' },
  { key: 'fib',       kind: 'snapshot', label: '斐波那契',     color: '#F59E0B', hint: '最近 120 日高低点之间的回撤位（0.236 ~ 0.786）。区间端点截至最新交易日。' },
  { key: 'round',     kind: 'snapshot', label: '整数关口',     color: '#71717A', hint: '当前价 ±10% 范围内的心理整数位（如 5.00 / 10.00），只作弱参考。' },
]

// 通道曲线元数据(单一数据源):供 buildOption 画线 + 右侧面板取最新值共用。
//   alignedKey: alignedSeries 中的 key(由 series.boll/keltner/atr 对齐而来)
//   group:      属于哪个价位开关组(开关该组即开关这条曲线)
//   endLabel:   右侧端点标签(显示最新值的文字)
const CURVE_DEFS: { alignedKey: string; group: LevelType; endLabel: string; color: string; dashed?: boolean }[] = [
  { alignedKey: 'boll_upper',     group: 'boll',      endLabel: '上轨', color: '#F97316', dashed: true },
  { alignedKey: 'boll_lower',     group: 'boll',      endLabel: '下轨', color: '#F97316', dashed: true },
  { alignedKey: 'boll_mid',       group: 'boll',      endLabel: '中轨', color: '#FB923C', dashed: false },
  { alignedKey: 'keltner_s_upper',group: 'keltner_s', endLabel: 'K短上', color: '#06B6D4', dashed: true },
  { alignedKey: 'keltner_s_lower',group: 'keltner_s', endLabel: 'K短下', color: '#06B6D4', dashed: true },
  { alignedKey: 'keltner_m_upper',group: 'keltner_m', endLabel: 'K中上', color: '#22D3EE', dashed: true },
  { alignedKey: 'keltner_m_lower',group: 'keltner_m', endLabel: 'K中下', color: '#22D3EE', dashed: true },
  { alignedKey: 'keltner_l_upper',group: 'keltner_l', endLabel: 'K长上', color: '#67E8F9', dashed: true },
  { alignedKey: 'keltner_l_lower',group: 'keltner_l', endLabel: 'K长下', color: '#67E8F9', dashed: true },
  { alignedKey: 'atr_stop',       group: 'atr_stop',  endLabel: 'ATR下', color: '#EF4444', dashed: true },
  { alignedKey: 'atr_tp',         group: 'atr_stop',  endLabel: 'ATR上', color: '#F87171', dashed: true },
]

// ===== 预留:标记 / 区间(后续新闻面、事件区间用) =====
export interface ChartMarker {
  date: string
  label?: string
  color?: string
  above?: boolean
}
export interface ChartRange {
  start: string
  end: string
  label?: string
  color?: string
}
/** 竖线事件（简称变更、制度切换这类"某天起"的事实）。date 可落在非交易日：
 *  会吸附到其后第一根 K，标签写明吸附后的日期，不假装事件发生在那一根。 */
export interface ChartVLine {
  date: string
  label: string
  color?: string
  /** 标签放到线的哪一侧（默认 right） */
  position?: 'left' | 'right'
}

interface Props {
  rows: KlineRow[]
  levels?: Record<LevelType, PriceLevel[]>
  /** 带状曲线指标(布林带/Keltner/ATR)的每日序列 —— 画成跟随时间漂移的曲线 */
  series?: LevelSeries
  /** series 数据对应的日期数组(与 series 各数组对齐) */
  seriesDates?: string[]
  /** 默认开启的价位组 */
  defaultLevelTypes?: LevelType[]
  /**
   * 初始视窗（含两端）。给了就按这两根 K 定位初始缩放，不给则回落到
   * 「最近 120 根」。用户拖动缩放条之后，重绘（切主题 / hover 价位高亮 /
   * 开关价位组）不得把视窗打回这里 —— 见 zoomRef。
   */
  initialRange?: { start: string; end: string }
  /** 预留:新闻/暴雷/利好日期标记 */
  markers?: ChartMarker[]
  /** 预留:事件区间高亮 */
  ranges?: ChartRange[]
  /** 竖线事件（简称变更等）：吸附到其后第一根 K */
  vlines?: ChartVLine[]
  /** 鼠标所在那根 K 的日期（null=移出图外）。用于页面顶部读数条 */
  onHoverDate?: (date: string | null) => void
  /** 视窗变化（百分比）。页面据此算"当前可视区间"的统计 */
  onViewportChange?: (v: { start: number; end: number }) => void
  /**
   * 最新交易日收盘价（来自关键价位接口，与 levels 同源）。
   *
   * 不能用 `rows` 的最后一根代替：用户可以把区间拖到 2024 年，那时 rows 的末根
   * 是 2024 年的价，而下方价位是"今天"算的 —— 两者相除会得出一个既不是当期
   * 涨幅、也不是当前距压力的数。缺这个值时退回 rows 末根，并由文案说明口径。
   */
  lastClose?: number

  /** 预留:点击某根 K 线 */
  onDateClick?: (date: string) => void
  height?: number
  className?: string
}

// 成交量副图高度。主图 ≈ 4.4 倍于此：K 线是主角，量柱只需要看得出放缩。
// 原来是 90（主图 328），和弹窗图（纯 K 区约 490）比显得局促 —— 高度是"好看"
// 里最容易被忽略的一项，同样的蜡烛数，纵向空间翻倍看起来就是另一种质感。
const VOL_PANE_H = 80

/**
 * 图表本身不暴露命令式句柄。
 *
 * 曾经有一个 `pan / zoom / reset` 的 ref，专为页面上的 ←/→/+/−/0 键盘快捷键服务。
 * 快捷键下线后它没有调用方了 —— 留着一个只有死代码在用的 ref，会让下一个人
 * 以为某处还在靠它工作。视图复位仍由工具条上的「重置视图」按钮提供。
 */
export function AnalysisKChart({
  rows,
  levels,
  series,
  seriesDates,
  // 默认只开布林带：它是逐日递推的带，整段回溯都成立。
  // 「今日价位」那六组（筹码密集区/枢轴/极值/缺口/斐波/整数）默认全关 ——
  // 一次开六组会把图压成一张横线网，而它们只描述今天这一个时点。
  defaultLevelTypes = ['boll'],
  initialRange,
  markers,
  ranges,
  vlines,
  onDateClick,
  onHoverDate,
  onViewportChange,
  lastClose,
  height = 460,
  className,
}: Props) {
  const chartRef = useRef<HTMLDivElement>(null)
  const chartInstRef = useRef<ECharts | null>(null)
  /** seriesIndex → levelKey 映射, buildOption 填充, ECharts hover 事件反查 */
  const seriesKeyMapRef = useRef<Map<number, string>>(new Map())
  /**
   * 用户当前的缩放窗口（百分比）。ECharts 每次 setOption(option, true) 都会
   * 用 option 里的 start/end 覆盖现场状态 —— 而这张图因为 hover 联动高亮
   * 会频繁整幅重绘。不记住它，用户拉好的视窗会被一次鼠标划过打回默认。
   */
  const zoomRef = useRef<{ start: number; end: number } | null>(null)
  /** 上一次渲染用的数据签名: 换了数据（换股票/换区间）才重置缩放 */
  const rowsRef = useRef<string | null>(null)
  // 回调放进 ref：页面每次渲染都会传新函数，直接进 effect 依赖会让图整幅重建
  const onHoverRef = useRef(onHoverDate)
  const onViewportRef = useRef(onViewportChange)
  onHoverRef.current = onHoverDate
  onViewportRef.current = onViewportChange
  /** 上一次上报的 hover 日期 / 视窗，用来去重（每帧都 setState 会卡） */
  const lastHoverRef = useRef<string | null>(null)
  const viewportRafRef = useRef<number | null>(null)
  /**
   * 视窗是否包含最新一根 K —— 决定「今日价位」画不画（见 buildOption 的说明）。
   *
   * 必须是 state，不能每次在 buildOption 里现算：buildOption 只在 effect 依赖
   * 变化时执行，而拖动缩放条只改 zoomRef（不触发重渲染）。现算的结果就是
   * 「拖到 2024 年，图上仍躺着今天的筹码密集区」—— 正是这次要修的那个未来信息。
   * state 只在跨越 99.5 这条界线时翻转一次，不会每帧重绘。
   */
  const [showToday, setShowToday] = useState(true)
  const showTodayRef = useRef(true)
  // 主题: buildOption 内部用 CT() 动态取色, 这里只负责切换时触发重建
  const theme = useTheme()
  const [activeTypes, setActiveTypes] = useState<Set<LevelType>>(new Set(defaultLevelTypes))
  /** 枢轴点显示到第几档:1=只P+R1/S1, 2=到R2/S2, 3=全档(R3/S3) */
  const [pivotRank, setPivotRank] = useState<1 | 2 | 3>(1)
  /** 双向联动高亮: hover 价位标签 ↔ hover 下方文字行。值为 levelKey, null=无高亮 */
  const [hoveredKey, setHoveredKey] = useState<string | null>(null)
  /** 对数坐标：两年看腰斩/翻倍，线性轴会把低价段压成一条线 */
  const [logScale, setLogScale] = useState(false)

  // 数据预处理 + 带状曲线序列对齐(后端 series 的日期范围可能与 rows 不同,需映射)
  const { dates, candle, vols, dateIndex, zoomStart, alignedSeries } = useMemo(() => {
    const dates = rows.map(r => (typeof r.date === 'string' ? r.date.slice(0, 10) : String(r.date)))
    const candle = rows.map(r => [r.open, r.close, r.low, r.high])
    const vols = rows.map(r => ({
      value: r.volume ?? 0,
      itemStyle: { color: r.close >= r.open ? THEME.volUp : THEME.volDown },
    }))
    const dateIndex = new Map(dates.map((d, i) => [d, i]))
    // 默认显示最近 6 个月 ≈ 120 个交易日;数据不足则全部显示
    const showBars = 120
    const zoomStart = dates.length > showBars ? Math.round((1 - showBars / dates.length) * 100) : 0

    // 把后端 series(按 seriesDates 对齐)映射到前端 rows 的 dates 顺序
    const alignedSeries: Record<string, (number | null)[]> = {}
    if (series && seriesDates && seriesDates.length > 0) {
      // 构建 seriesDates 索引
      const sIdx = new Map(seriesDates.map((d, i) => [d, i]))
      // 通用对齐:给定 series 里某条数组,返回与 rows dates 对齐的版本
      const align = (arr: (number | null)[] | undefined): (number | null)[] => {
        if (!arr) return dates.map(() => null)
        return dates.map(d => {
          const i = sIdx.get(d)
          return i != null ? arr[i] : null
        })
      }
      if (series.boll) {
        alignedSeries['boll_upper'] = align(series.boll.upper)
        alignedSeries['boll_lower'] = align(series.boll.lower)
        if (series.boll.mid) alignedSeries['boll_mid'] = align(series.boll.mid)
      }
      if (series.keltner_s) {
        alignedSeries['keltner_s_upper'] = align(series.keltner_s.upper)
        alignedSeries['keltner_s_lower'] = align(series.keltner_s.lower)
      }
      if (series.keltner_m) {
        alignedSeries['keltner_m_upper'] = align(series.keltner_m.upper)
        alignedSeries['keltner_m_lower'] = align(series.keltner_m.lower)
      }
      if (series.keltner_l) {
        alignedSeries['keltner_l_upper'] = align(series.keltner_l.upper)
        alignedSeries['keltner_l_lower'] = align(series.keltner_l.lower)
      }
      if (series.atr) {
        alignedSeries['atr_stop'] = align(series.atr.stop_loss)
        alignedSeries['atr_tp'] = align(series.atr.take_profit)
      }
    }

    return { dates, candle, vols, dateIndex, zoomStart, alignedSeries }
  }, [rows, series, seriesDates])

  /**
   * 初始视窗（百分比）。三档优先级：
   *   1. initialRange（页面选了 2024-09-01 ~ 2026-11-13）→ 按这两根 K 定位；
   *   2. 否则最近 120 根（原行为）；
   *   3. 数据不足一屏 → 0~100。
   * 用户拖过缩放条之后由 zoomRef 接管，这里只在换股票/换区间时生效。
   */
  const presetZoom = useMemo(() => {
    if (dates.length === 0) return { start: 0, end: 100 }
    if (!initialRange) return { start: zoomStart, end: 100 }
    const span = Math.max(dates.length - 1, 1)
    let i0 = dates.findIndex(d => d >= initialRange.start)
    if (i0 < 0) i0 = 0
    let i1 = dates.length - 1
    for (let i = dates.length - 1; i >= 0; i--) {
      if (dates[i] <= initialRange.end) { i1 = i; break }
    }
    if (i1 < i0) i1 = dates.length - 1
    return {
      start: Math.max(0, Math.min(100, (i0 / span) * 100)),
      end: Math.max(0, Math.min(100, (i1 / span) * 100)),
    }
  }, [dates, initialRange, zoomStart])

  // 换股票 / 换区间时，视窗回到 presetZoom，showToday 也要跟着回到它的值
  useEffect(() => {
    const v = presetZoom.end >= 99.5
    if (v !== showTodayRef.current) {
      showTodayRef.current = v
      setShowToday(v)
    }
  }, [presetZoom])

  // 构建 option
  const buildOption = (): EChartsOption => {
    const priceLines = collectPriceLines(levels, activeTypes, pivotRank)
    // 视窗：用户拖过的优先，其次页面给的区间，最后默认最近 120 根
    const zoom = zoomRef.current ?? presetZoom
    /** 视窗是否包含最新一根 K（由 datazoom 事件维护，见组件级 showToday 的说明） */

    // 三段布局:主图 / 成交量 / 缩放条,从上到下累加,各段之间留间距,互不遮挡
    //   [16 顶部] [mainH 主图] [8 间距] [volH 成交量] [12 间距] [SLIDER_H 缩放条] [8 底部]
    const SLIDER_H = 18
    const PAD_TOP = 16
    const GAP_MAIN_VOL = 8        // 主图 ↔ 成交量
    const GAP_VOL_SLIDER = 12     // 成交量 ↔ 缩放条(留足,避免遮挡)
    const PAD_BOTTOM = 8
    const volH = VOL_PANE_H
    const mainH = height - PAD_TOP - GAP_MAIN_VOL - volH - GAP_VOL_SLIDER - SLIDER_H - PAD_BOTTOM
    const volTop = PAD_TOP + mainH + GAP_MAIN_VOL
    const sliderBottom = PAD_BOTTOM

    // 预留:markPoint(新闻标记)
    const markPointData: any[] = (markers ?? [])
      .filter(m => dateIndex.has(m.date))
      .map(m => ({
        coord: [m.date, rows[dateIndex.get(m.date)!].high],
        symbol: 'pin', symbolSize: 32,
        itemStyle: { color: m.color ?? '#EAB308' },
        label: { show: !!m.label, formatter: m.label ?? '', fontSize: 9, color: '#fff' },
      }))

    // 预留:markArea(事件区间) —— 端点允许越界，钳到首/末根而不是整段丢弃
    // （简称观测的起点常早于图表数据，丢了就等于这段时间"没有名字"）。
    const markAreaData: any[] = (ranges ?? [])
      .map(r => {
        const span = clampToDates(dates, r.start, r.end)
        if (!span) return null
        return [{
          xAxis: span[0], name: r.label ?? '',
          itemStyle: { color: r.color ?? 'rgba(234,179,8,0.08)' },
          label: r.label ? { show: true, position: 'insideTop', distance: 6, color: '#EAB308', fontSize: 10 } : undefined,
        }, { xAxis: span[1] }]
      })
      .filter(Boolean) as any[][]

    // 竖线事件：date 落在非交易日时吸附到其后第一根 K（标签写明吸附日）
    const markLineData: any[] = (vlines ?? [])
      .map(v => {
        const snapped = dates.find(d => d >= v.date) ?? null
        if (!snapped) return null
        return {
          xAxis: snapped,
          lineStyle: { color: v.color ?? '#A1A1AA', type: 'dashed', width: 1, opacity: 0.8 },
          label: {
            show: true, formatter: v.label, fontSize: 9,
            color: v.color ?? '#A1A1AA', position: v.position === 'left' ? 'insideEndTop' : 'insideStartTop',
            distance: 2,
          },
        }
      })
      .filter(Boolean) as any[]

    const series: any[] = [
      {
        name: 'K', type: 'candlestick', data: candle, animation: false,
        // z=2 让蜡烛始终在价位线(z=1)之上, hover 高亮价位线时不会被遮挡/变淡
        z: 2,
        itemStyle: {
          color: THEME.bull, color0: THEME.bear,
          borderColor: THEME.bull, borderColor0: THEME.bear,
          cursor: 'pointer',
        },
        markPoint: markPointData.length ? { data: markPointData, animation: false } : undefined,
        markArea: markAreaData.length ? { silent: true, data: markAreaData } : undefined,
        markLine: markLineData.length
          ? { silent: true, symbol: 'none', data: markLineData, animation: false }
          : undefined,
      },
      {
        name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
        data: vols, animation: false,
        // 量柱留 40% 空隙：满格宽会让密集区间糊成一片色块（弹窗图也是 60%）
        barWidth: '60%',
      },
    ]

    // 价位水平线 —— 用 line series(恒定值)画水平线,endLabel 显示标签文字;
    // 与通道曲线一致,标签落在右侧 grid.right 预留带(外侧),不压蜡烛。
    // hoveredKey 非空时:命中线加粗高亮,其它线淡化(opacity 0.15),形成聚焦效果。
    const dimming = hoveredKey != null
    for (const p of priceLines) {
      // 视窗停在历史段时，今日价位整组不出现（见 showToday 的说明）
      if (!showToday) continue
      const k = levelKey(p.type, p.value)
      const hit = hoveredKey === k
      const opacity = dimming ? (hit ? 1 : 0.12) : 0.85
      const width = hit ? 2 : 1.4
      series.push({
        name: p.label, type: 'line', silent: false, animation: false,
        symbol: 'none',
        data: snapshotLineData(dates, p.value, showToday),
        // 默认 z=1 在蜡烛(z=2)之下; 命中时 zlevel=10 提到独立顶层, 标签不再被遮挡
        z: 1,
        zlevel: hit ? 10 : 0,
        lineStyle: { width, color: p.color, type: 'dashed', opacity },
        itemStyle: { color: p.color },
        // 端点数值只在 hover 命中时出现。
        // 常驻标签要求 grid 右侧留 144px（主图被压窄 1/9，是"不如弹窗好看"的主因）；
        // 而价位名称+数值已经在下方 LevelOverview 里逐条列着，图上再挂一遍是冗余。
        // hover 时才需要的那个数，就只给 hover 那一根线 —— 两边的 hover 是联动的。
        endLabel: hit ? {
          show: true,
          formatter: () => p.value.toFixed(2),
          color: p.color, fontSize: 10, fontFamily: 'JetBrains Mono, monospace',
          fontWeight: 'bold',
          backgroundColor: CT().tooltipBg,
          borderColor: p.color, borderWidth: 1,
          padding: [1, 4], borderRadius: 2,
          distance: 4,
        } : undefined,
      })
    }

    // 带状曲线指标(布林带 / Keltner通道 / ATR波动通道) —— 跟随行情漂移的曲线
    // 单一数据源 CURVE_DEFS 驱动:每条曲线带 endLabel(右侧端点标签),显示最新数值
    for (const def of CURVE_DEFS) {
      if (!activeTypes.has(def.group)) continue
      const data = alignedSeries[def.alignedKey]
      if (!data || !data.some(v => v != null)) continue
      // 取最后一个有效值作为右侧端点显示文字
      let lastVal: number | null = null
      for (let i = data.length - 1; i >= 0; i--) {
        if (data[i] != null) { lastVal = data[i]; break }
      }
      // 曲线 key 用 group(同组上下轨联动),hover 命中时高亮
      const hit = hoveredKey === def.group
      const opacity = dimming ? (hit ? 1 : 0.12) : 0.8
      const width = hit ? 1.8 : 1
      series.push({
        name: def.endLabel, type: 'line', data: data.map(v => v ?? '-'),
        smooth: true, symbol: 'none', silent: false, animation: false,
        z: 1,
        zlevel: hit ? 10 : 0,
        lineStyle: { width, color: def.color, type: def.dashed === false ? 'solid' : 'dashed', opacity },
        itemStyle: { color: def.color },
        // 右侧端点标签:显示该通道的最新数值,距绘图区右缘留 6px 间距。
        // 视窗停在历史段时不显示 —— 那个"最新值"在所选区间里还没发生。
        endLabel: lastVal != null ? {
          show: showToday,
          formatter: () => `${lastVal!.toFixed(2)}`,
          color: def.color, fontSize: hit ? 10 : 9, fontFamily: 'JetBrains Mono, monospace',
          fontWeight: hit ? 'bold' : 'normal',
          backgroundColor: hit ? CT().tooltipBg : CT().infoBarBg,
          borderColor: hit ? def.color : 'transparent',
          borderWidth: hit ? 1 : 0,
          padding: [2, 5], borderRadius: 2,
          distance: 6,
        } : undefined,
      })
    }

    // seriesIndex → levelKey 映射(K/成交量索引 0/1 不参与联动)。
    // 必须与上面 push 的条件完全一致，否则 hover 高亮会错位到另一条线。
    const keyMap = new Map<number, string>()
    let si = 2
    if (showToday) {
      for (const p of priceLines) {
        keyMap.set(si++, levelKey(p.type, p.value))
      }
    }
    for (const def of CURVE_DEFS) {
      if (!activeTypes.has(def.group)) continue
      const data = alignedSeries[def.alignedKey]
      if (!data || !data.some(v => v != null)) continue
      keyMap.set(si++, def.group)
    }
    seriesKeyMapRef.current = keyMap

    return {
      animation: false,
      backgroundColor: 'transparent',
      // grid.right 只留一条窄标签带（67px 够放「K短上」这种 3–4 字 + padding）。
      //
      // 原来是 144px：所有价位线都挂 endLabel 在 grid 外，需要一条 144px 的标签带。
      // 代价是主图被压窄 1/9，右侧一大片空白 —— 这正是「不如弹窗图表好看」的主因。
      // 现在价位文字由下方 LevelOverview 承担（hover 双向高亮还在），图上只留
      // 曲线的端点值（布林上下轨这类必须贴着线看的东西）。
      grid: [
        { left: 56, right: 67, top: 16, height: mainH },
        { left: 56, right: 67, top: volTop, height: volH },
      ],
      xAxis: [
        {
          type: 'category', data: dates, boundaryGap: true,
          // 轴线用 border 而不是 grid：grid 是 6% 透明度的分割线，当年轴线太淡，
          // 图底部像被裁掉一截；弹窗图表一直用 border，这里对齐。
          axisLine: { lineStyle: { color: CT().border } },
          axisLabel: { color: CT().text, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' },
          axisTick: { show: false },
          splitLine: { show: false },
        },
        {
          type: 'category', gridIndex: 1, data: dates, boundaryGap: true,
          axisLabel: { show: false }, axisLine: { show: false }, axisTick: { show: false },
          // 成交量区只要竖线（与主图联动），不要日期标签 —— 日期由主图 x 轴出
          axisPointer: { label: { show: false } },
        },
      ],
      yAxis: [
        {
          type: logScale ? 'log' : 'value',
          // log 轴不能同时给 scale（会与 type 冲突），交给 ECharts 自适应
          scale: !logScale,
          // 上下各留 4%：最高/最低那根蜡烛不贴边，十字线也压不到轴线上。
          // log 轴没有"数据范围外扩"的语义（ECharts 会忽略/告警），只在数值轴上给。
          boundaryGap: logScale ? undefined : [0.04, 0.04],
          // 轴线和刻度都不画：数值已经在 splitLine 与轴端标签里，多两条线只是噪声
          axisLine: { show: false }, axisTick: { show: false },
          splitLine: { lineStyle: { color: CT().grid } },
          axisLabel: { color: CT().text, fontSize: 11, fontFamily: 'JetBrains Mono, monospace' },
        },
        { scale: true, gridIndex: 1, splitNumber: 2,
          // 成交量区不画背景横线，也不要横线指示器
          splitLine: { show: false },
          axisLine: { show: false }, axisTick: { show: false },
          axisPointer: { show: false },
          axisLabel: { color: CT().text, fontSize: 10, fontFamily: 'JetBrains Mono, monospace',
                       formatter: (v: number) => fmtVol(v) } },
      ],
      dataZoom: [
        // 与弹窗图表同一套鼠标语义：直接拖动=平移，滚轮=缩放，不必按 shift
        { type: 'inside', xAxisIndex: [0, 1], start: zoom.start, end: zoom.end,
          moveOnMouseMove: true, zoomOnMouseWheel: true },
        { type: 'slider', xAxisIndex: [0, 1], bottom: sliderBottom, height: SLIDER_H, start: zoom.start, end: zoom.end,
          borderColor: 'transparent', fillerColor: CT().zoomFill,
          // 拖动时不弹「2024-09-02 ~ 2024-11-15」气泡：它会盖住最下面那几根 K，
          // 而拖动的目的本来就是看那一段。区间边界看上方区间条即可。
          showDetail: false,
          handleStyle: { color: '#52525B' }, textStyle: { color: CT().text, fontSize: 10 } },
      ],
      // 十字虚线 + 轴端数值标签 —— 与个股弹窗的 K 线图同一套做法。
      //
      // tooltip 本身是透明的（formatter 返回空串、字号 0），只为激活
      // `tooltip.axisPointer.type='cross'`；真正的视觉是两条虚线 + 轴端的
      // crosshairLabel。ECharts 把 cross 只开放给 tooltip 层，所以"不弹 tooltip
      // 又要 cross"必须走这条透明壳子，而不是关掉 tooltip。
      tooltip: {
        trigger: 'axis',
        axisPointer: {
          type: 'cross',
          crossStyle: { color: CT().crosshair, width: 1, type: 'dashed' },
        },
        backgroundColor: 'transparent',
        borderWidth: 0,
        textStyle: { fontSize: 0 },
        formatter: () => '',
      },
      axisPointer: {
        link: [{ xAxisIndex: 'all' }],
        lineStyle: { color: CT().crosshair, width: 1, type: 'dashed' },
        label: {
          backgroundColor: CT().crosshairLabelBg,
          fontFamily: 'JetBrains Mono, monospace',
          fontSize: 10,
        },
      },
      series,
    }
  }

  // 初始化 + 数据更新
  useEffect(() => {
    if (!chartRef.current) return
    if (!chartInstRef.current) {
      chartInstRef.current = echarts.init(chartRef.current, undefined, { renderer: 'canvas' })
      chartInstRef.current.on('click', (params: any) => {
        // 预留:点击 K 线(非 markPoint/markLine)回调
        if (params.componentType === 'series' && params.seriesType === 'candlestick' && onDateClick) {
          onDateClick(dates[params.dataIndex])
        }
      })
      // hover 价位线/曲线 endLabel → 联动高亮(与下方文字行双向联动)
      chartInstRef.current.on('mouseover', (params: any) => {
        if (params.componentType === 'series') {
          const k = seriesKeyMapRef.current.get(params.seriesIndex as number)
          if (k) setHoveredKey(k)
        }
      })
      chartInstRef.current.on('globalout', () => {
        setHoveredKey(null)
        if (lastHoverRef.current !== null) {
          lastHoverRef.current = null
          onHoverRef.current?.(null)   // 移出图外 → 页面读数条回落到最后一根
        }
      })
      // 十字光标所在的那根 K：ECharts 在 axisPointer 联动时抛 axesInfo[0].value
      // （category 轴就是日期字符串）。只做一次字符串比较，不 setState 到本组件。
      chartInstRef.current.on('updateAxisPointer', (e: any) => {
        const info = e?.axesInfo?.[0]
        const d = typeof info?.value === 'string' ? info.value : null
        if (d === lastHoverRef.current) return
        lastHoverRef.current = d
        onHoverRef.current?.(d)
      })
      // 记住用户拖出来的视窗：本图的每次 setOption 都用 notMerge 重建，
      // 不接管就会被 option 里的默认 start/end 覆盖（hover 一下就跳回 120 根）。
      // 事件名必须是全小写 'datazoom'：ECharts 的 registerAction 把 action 的
      // event 名统一 toLowerCase（源码原话 "Event name is all lowercase"），
      // 而 Eventful.on 按字面量存 handler —— 写成 'dataZoom' 永远不会触发。
      chartInstRef.current.on('datazoom', () => {
        const inst = chartInstRef.current
        if (!inst) return
        const dz = (inst.getOption() as any)?.dataZoom
        const first = Array.isArray(dz) ? dz[0] : null
        if (first && typeof first.start === 'number' && typeof first.end === 'number') {
          zoomRef.current = { start: first.start, end: first.end }
          // 视窗是否还包着最新一根 —— 只在跨越界线时 setState，
          // 否则每帧拖动都会重绘整幅图（这条判断是「今日价位」的唯一开关）
          const nextShowToday = first.end >= 99.5
          if (nextShowToday !== showTodayRef.current) {
            showTodayRef.current = nextShowToday
            setShowToday(nextShowToday)
          }
          // 拖动时每帧都会触发；rAF 合并成一帧一次上报，页面重算统计不会拖慢拖动
          if (viewportRafRef.current == null) {
            viewportRafRef.current = window.requestAnimationFrame(() => {
              viewportRafRef.current = null
              const z = zoomRef.current
              if (z) onViewportRef.current?.({ start: z.start, end: z.end })
            })
          }
        }
      })
    }
    // 换了数据（换股票 / 换区间）才重置视窗；hover 高亮、切主题必须保持
    const sig = dataSignature(rows)
    if (rowsRef.current !== sig) {
      rowsRef.current = sig
      zoomRef.current = null
    }
    chartInstRef.current.setOption(buildOption(), true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, levels, series, seriesDates, activeTypes, pivotRank, markers, ranges, vlines, height, theme, hoveredKey, initialRange, logScale, showToday])

  /** 丢掉用户缩放，回到页面选的区间（工具条「重置视图」按钮调它） */
  const resetView = () => {
    zoomRef.current = null
    const inst = chartInstRef.current
    if (inst) inst.setOption(buildOption(), true)
    onViewportRef.current?.({ start: presetZoom.start, end: presetZoom.end })
  }

  // resize
  useEffect(() => {
    const inst = chartInstRef.current
    if (!inst) return
    const onResize = () => inst.resize()
    window.addEventListener('resize', onResize)
    return () => { window.removeEventListener('resize', onResize); inst.dispose(); chartInstRef.current = null }
  }, [])

  const toggleType = (t: LevelType) => {
    setActiveTypes(prev => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }

  /** 一个价位组开关。两组共用同一套视觉，靠 <span>今日</span> 标签区分语义。 */
  const groupBtn = (g: typeof LEVEL_GROUPS[number]) => {
    const active = activeTypes.has(g.key)
    // 枢轴点数量按当前档位过滤显示;其他组显示原始数量
    const raw = levels?.[g.key] ?? []
    const count = g.key === 'pivot'
      ? raw.filter(p => p.rank === undefined || p.rank <= pivotRank).length
      : raw.length
    return (
      <button
        key={g.key}
        onClick={() => toggleType(g.key)}
        disabled={raw.length === 0}
        title={`${g.label}（${count} 个）\n${g.hint}`}
        className={`inline-flex items-center gap-1 h-6 px-2 rounded-md text-[11px] font-medium border transition-all disabled:opacity-30 disabled:cursor-not-allowed ${
          active
            ? 'text-foreground'
            : 'text-muted bg-base/40 border-border/30 hover:border-border/60'
        }`}
        style={active ? { borderColor: g.color + '66', backgroundColor: g.color + '1a' } : undefined}
      >
        <span className="h-1.5 w-1.5 rounded-full" style={{ backgroundColor: active ? g.color : '#52525B' }} />
        {g.label}
        <span className="opacity-50">{count}</span>
      </button>
    )
  }

  return (
    <div className={className}>
      {/* 图表工具条：左边是价位开关（没有价位数据时不占位），右边是坐标/视窗控制 */}
      <div className="flex flex-wrap items-center gap-1.5 mb-2.5">
        {levels && (
          <>
          <span className="text-[11px] text-muted mr-0.5">价位</span>
          {/* 逐日递推的带：整段回溯都成立，可以一直画 */}
          <span className="text-[10px] text-muted/50" title="逐日递推的波动带：第 t 根只用到 t 及之前的数据，整段回溯都成立">带</span>
          {LEVEL_GROUPS.filter(g => g.kind === 'series').map(groupBtn)}
          {/* 快照价位：只描述最新交易日，视窗回到历史段时图上不出现 */}
          <span
            className="text-[10px] text-muted/50 ml-1 pl-2 border-l border-border/40"
            title="按最新交易日算出的单点价位。视窗停在历史段时这些线不会出现 —— 它们不属于那一段。"
          >今日</span>
          {LEVEL_GROUPS.filter(g => g.kind === 'snapshot').map(groupBtn)}

          {/* 枢轴点档位选择器 —— 仅当枢轴点开启时显示 */}
          {activeTypes.has('pivot') && (levels.pivot?.length ?? 0) > 0 && (
            <div className="inline-flex items-center gap-0.5 ml-1 pl-2 border-l border-border/40">
              <span className="text-[11px] text-muted mr-1">档位</span>
              {([1, 2, 3] as const).map(r => (
                <button
                  key={r}
                  onClick={() => setPivotRank(r)}
                  title={r === 1 ? 'P + R1/S1(3 个)' : r === 2 ? '到 R2/S2(5 个)' : '全档 R3/S3(7 个)'}
                  className={`h-6 px-2 rounded-md text-[11px] font-mono border transition-all ${
                    pivotRank === r
                      ? 'bg-[#8B5CF6]/15 border-[#8B5CF6]/40 text-[#c4b5fd]'
                      : 'text-muted bg-base/40 border-border/30 hover:border-border/60'
                  }`}
                >
                  {r}
                </button>
              ))}
            </div>
          )}
          </>
        )}

        <span className="ml-auto flex items-center gap-1">
          <button
            onClick={() => setLogScale(v => !v)}
            title={'对数坐标：长周期看腰斩/翻倍时，线性轴会把早期波动压成一条直线；' +
              '对数轴把"涨一倍 / 跌一半"画成等距。开关只影响纵轴，不影响任何数字。'}
            className={`h-6 rounded-md border px-2 text-[11px] transition-all ${
              logScale
                ? 'border-sky-400/40 bg-sky-400/10 text-sky-300'
                : 'border-border/30 bg-base/40 text-muted hover:border-border/60'
            }`}
          >
            对数
          </button>
          <button
            onClick={resetView}
            title="把视图复位到上面选的区间（拖动缩放条或按 ←/→ 之后用它回来）"
            className="h-6 rounded-md border border-border/30 bg-base/40 px-2 text-[11px] text-muted transition-all hover:border-border/60"
          >
            重置视图
          </button>
        </span>
      </div>
      {/* 图表:右侧预留带(grid.right 预留)显示价位标签文字,不压蜡烛 */}
      <div ref={chartRef} style={{ width: '100%', height }} />

      {/* 价位统计面板:把当前开启的点位按"压力 / 支撑"结构化列出 */}
      {levels && (
        <LevelOverview
          levels={levels}
          activeTypes={activeTypes}
          pivotRank={pivotRank}
          close={lastClose ?? (rows.length ? rows[rows.length - 1].close : undefined)}
          closeIsLatest={lastClose != null}
          hoveredKey={hoveredKey}
          onHover={setHoveredKey}
        />
      )}
    </div>
  )
}

// ===== 价位统计面板(图表下方,结构化文本展示) =====
function LevelOverview({
  levels, activeTypes, pivotRank, close, closeIsLatest, hoveredKey, onHover,
}: {
  levels: Record<LevelType, PriceLevel[]>
  activeTypes: Set<LevelType>
  pivotRank: 1 | 2 | 3
  close?: number
  /** close 是"最新交易日"还是"所选区间末根" —— 决定标签怎么写 */
  closeIsLatest?: boolean
  hoveredKey: string | null
  onHover: (k: string | null) => void
}) {
  // 收集当前显示的点位(同 collectPriceLines 的过滤逻辑)
  const visible: PriceLevel[] = []
  for (const g of LEVEL_GROUPS) {
    if (!activeTypes.has(g.key)) continue
    for (const p of levels[g.key] ?? []) {
      if (p.type === 'pivot' && p.rank !== undefined && p.rank > pivotRank) continue
      visible.push(p)
    }
  }
  if (visible.length === 0) return null

  // 按方向分两组:压力位(在当前价之上) / 支撑位(之下),各自按距当前价远近排序
  const cur = close ?? visible[0].value
  const resistances = visible
    .filter(p => p.side === 'resistance')
    .sort((a, b) => a.value - b.value)        // 由近及远(低→高)
  const supports = visible
    .filter(p => p.side === 'support')
    .sort((a, b) => b.value - a.value)         // 由近及远(高→低)
  const neutrals = visible.filter(p => p.side === 'neutral')

  const fmtPct = (v: number) => {
    if (!cur) return ''
    const pct = ((v - cur) / cur) * 100
    const sign = pct >= 0 ? '+' : ''
    return `${sign}${pct.toFixed(1)}%`
  }

  const Row = ({ p }: { p: PriceLevel }) => {
    const color = LEVEL_GROUPS.find(g => g.key === p.type)?.color ?? CT().text
    const k = levelKey(p.type, p.value)
    const hit = hoveredKey === k
    const dim = hoveredKey != null && !hit
    return (
      <div
        onMouseEnter={() => onHover(k)}
        onMouseLeave={() => onHover(null)}
        className={`flex items-center gap-2 py-1 px-1.5 -mx-1.5 rounded transition-colors cursor-default ${
          hit ? 'bg-elevated/60' : ''
        }`}
        style={dim ? { opacity: 0.35 } : undefined}
      >
        <span className="h-1.5 w-1.5 rounded-full shrink-0 transition-transform" style={{ backgroundColor: color, transform: hit ? 'scale(1.5)' : 'scale(1)' }} />
        <span className={`text-[12px] w-24 shrink-0 truncate ${hit ? 'text-foreground font-medium' : 'text-secondary'}`}>{p.label}</span>
        <span className={`text-[12px] font-mono ${hit ? 'text-foreground font-bold' : 'text-foreground'}`}>{p.value.toFixed(2)}</span>
        <span className="text-[10px] font-mono text-muted">{fmtPct(p.value)}</span>
      </div>
    )
  }

  return (
    <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 rounded-lg border border-border/40 bg-base/30 px-4 py-2.5">
      {/* 当前价 */}
      <div className="sm:col-span-2 flex items-baseline gap-2 pb-1.5 border-b border-border/30 mb-1">
        <span className="text-[11px] text-muted">{closeIsLatest ? '最新收盘' : '区间末收盘'}</span>
        <span className="text-[13px] font-mono font-medium text-foreground">{cur.toFixed(2)}</span>
        <span className="ml-auto text-[10px] text-muted/60">
          价位按最新交易日计算 · 视窗回到历史段时图上不画这些线
        </span>
      </div>
      {/* 压力位(从近到远,即从低到高)倒序展示:最高的在最上 */}
      {resistances.length > 0 && (
        <div>
          <div className="text-[11px] font-medium text-bear mb-0.5">压力位 ↑</div>
          {[...resistances].reverse().map((p, i) => <Row key={`r-${i}`} p={p} />)}
        </div>
      )}
      {/* 支撑位 + 中性(枢轴位 P) */}
      <div>
        {supports.length > 0 && (
          <>
            <div className="text-[11px] font-medium text-bull mb-0.5">支撑位 ↓</div>
            {supports.map((p, i) => <Row key={`s-${i}`} p={p} />)}
          </>
        )}
        {neutrals.length > 0 && (
          <div className={supports.length > 0 ? 'mt-2' : ''}>
            {supports.length === 0 && <div className="text-[11px] font-medium text-muted mb-0.5">枢轴位</div>}
            {neutrals.map((p, i) => <Row key={`n-${i}`} p={p} />)}
          </div>
        )}
      </div>
    </div>
  )
}

// ===== 工具:收集要画的水平价位线(按开启的组 + 档位 + 强度配色) =====
// 注意:带状指标(布林带/Keltner/ATR)改用曲线渲染,不在此画水平线,避免重复。
function collectPriceLines(
  levels: Record<LevelType, PriceLevel[]> | undefined,
  active: Set<LevelType>,
  pivotRank: 1 | 2 | 3,
): { value: number; label: string; color: string; type: string }[] {
  if (!levels) return []
  const out: { value: number; label: string; color: string; type: string }[] = []
  for (const g of LEVEL_GROUPS) {
    if (!active.has(g.key)) continue
    for (const p of levels[g.key] ?? []) {
      // 枢轴点:按档位过滤(rank>P 的,只显示到选定的档位)
      if (p.type === 'pivot' && p.rank !== undefined && p.rank > pivotRank) continue
      // 波动通道类(boll / keltner三档 / atr_stop)整组走曲线渲染,不画水平线;
      // sr 组现为成交密集区水平点,直接画线即可,无需特判。
      if (p.type === 'boll' || p.type === 'keltner_s' || p.type === 'keltner_m'
          || p.type === 'keltner_l' || p.type === 'atr_stop') continue
      out.push({ value: p.value, label: p.label, color: strengthColor(p.strength, g.color), type: p.type })
    }
  }
  return out
}

function strengthColor(strength: string | undefined, base: string): string {
  // strong 用实色,medium 用 0.85,weak 用 0.55 透明
  if (strength === 'weak') return base + '8C'
  if (strength === 'medium') return base + 'D9'
  return base
}

/** 价位唯一标识: 同类型同价格视为同一点位(用于联动高亮)。 */
function levelKey(type: string, value: number): string {
  return `${type}-${value.toFixed(2)}`
}

/**
 * 快照型价位（筹码密集区 / 枢轴 / 极值 / 未回补缺口 / 斐波 / 整数关口）的折线数据。
 *
 * 这些数是后端用**截至最新交易日**的数据算出来的单点结论。画成贯穿全图的横线，
 * 等于在 2024 年那段 K 上标 2026 年才成立的支撑位 —— 图上没有一句话说明
 * 「这是今天说的」，但结论已经被贴到了过去。
 *
 * 所以：视窗不含最新一根（showToday=false）→ 整条不画（全 null）；
 * 包住最新一根 → 只给最后两根赋值，画出来是贴右边缘的一小段 + 右侧标签。
 *
 * 抽成纯函数是为了能被测试直接钉住：这段逻辑没有 DOM、没有 canvas，
 * 靠 jsdom 断言"图长什么样"是测不到的。
 */
export function snapshotLineData(
  dates: string[], value: number, showToday: boolean,
): (number | null)[] {
  if (!showToday) return dates.map(() => null)
  // 只有一根 K 时，单点画不出线段，但也只有这一根，给它值
  const from = Math.max(0, dates.length - 2)
  return dates.map((_, i) => (i >= from ? value : null))
}

/**
 * 数据签名: 用来判断"是不是换了一批 K 线"。
 *
 * 不能用数组引用 —— 父组件在 data 还没到时会写 `rows ?? []`，每帧都是新引用，
 * 于是每帧都被判成"换股票"，用户的缩放在拖动过程中被反复清零。
 * 长度 + 首末日期足够区分换股票/换区间，又能让同区间的实时补丁保持视窗。
 */
function dataSignature(rows: KlineRow[]): string {
  if (rows.length === 0) return '0'
  return `${rows.length}|${rows[0]?.date}|${rows[rows.length - 1]?.date}`
}

function fmtVol(v: number): string {
  if (!v) return '0'
  if (v >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (v >= 1e4) return (v / 1e4).toFixed(0) + '万'
  return v.toFixed(0)
}
