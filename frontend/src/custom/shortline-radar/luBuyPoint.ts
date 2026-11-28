/**
 * 明日买点 —— 涨停次日砸盘反转（LU-3）的**静态读数**。
 *
 * 为什么单独一个文件：这些数字同时出现在买点卡与口径抽屉里。
 * 写两遍早晚会分叉（这个仓库最贵的一类 bug），所以只留一处。
 *
 * 唯一产地：`backend/app/decision/lu_rebound.py`
 *   清单：`python -m app.decision.lu_rebound --candidates --as-of YYYY-MM-DD`
 *   读数：`python -m app.decision.lu_rebound --study --data ../data`
 *
 * 2026-11-28 重算（2018-01 ~ 2026-11-27）：15,871 笔成交 / 2,007 个决策日，
 * 日超额 +0.843%（t 12.86），2018–2026 逐年全正。
 * 样本前（同一定义，2010-01 ~ 2017-12）：4,425 笔 / 893 日，+0.887%（t 8.03），8/8 年正。
 * 代价（同事件，`docs/hotmoney-quant-game.md` §6，15,865 样本）：T+2 开盘后日内
 * 最低点中位 −2.69%、p10 −6.80%，22% 的样本盘中跌破 −5%。
 *
 * 改数之前先重跑脚本；页面上的每个读数都必须能在这里找到。
 */
export const LU_STUDY = {
  asOf: '2026-11-27',
  /** 生产口径窗口（账本从 2018 起算）。 */
  window: '2018–2026',
  n: 15871,
  days: 2007,
  excessPct: '+0.84%',
  t: '12.86',
  years: '9/9',
  /** 样本前验证：同一定义回放 2010–2017，不是另一套口径。 */
  preWindow: '2010–2017',
  preExcessPct: '+0.89%',
  preT: '8.03',
  preYears: '8/8',
  /** T+2 开盘 ≤ 限价才成交（= 开盘没封涨停）；一字板已经不在信号里。 */
  fillRate: '99.9%',
  costBp: 15,
  /** 0.843% − 15bp ≈ 69bp。成本口径同 `_diag_board_shape.py`：佣金 5 + 印花 5 + 滑点 5。 */
  netBp: 69,
  mddMedian: '−2.69%',
  mddP10: '−6.80%',
  breach5: '22%',
} as const

/**
 * 排除清单 —— 与买点同一份研究里被否掉的动作。
 *
 * 只列「看起来也能做、实测是负的」；不列「没测过」的，因为没测过不配进排除表。
 * 读数出处：`docs/hotmoney-quant-game.md` §4/§5、`docs/shortline-radar.md`「为什么不能把观察池当买入名单」。
 */
export const LU_EXCLUSIONS: { action: string; read: string }[] = [
  { action: '板上买（2/3 板可成交子集）', read: '17/17 年为负，−1.14% / −2.25%' },
  { action: 'T+1 开盘追高（gap > 3%）', read: 'T+1 日内 −1.85%' },
  { action: 'T+1 收盘买 → T+2 开盘卖', read: '隔夜恒负，2018 后无一年为正' },
  { action: '把「超跌反弹」当同一件事', read: '非涨停 + T+1 跌 >5% 的对照只有 +0.05%' },
  { action: '把观察池 / 跨年池当买入名单', read: 'heat 前 1/5 为 −1.50%/20 日；跨年池 −65.7bp' },
]
