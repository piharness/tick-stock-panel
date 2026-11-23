"""关键价位计算 —— 独立模块,纯函数,无 IO / 无存储。

输入: 已经包含 OHLCV 的 polars 日 K DataFrame(内存中,通常来自 KlineRepository 缓存)。
输出: 4 类结构化价位点,供:
  - 图表 markLine 渲染(压力位 / 支撑位 / 成交密集区 / 枢轴点 / 前高前低)
  - AI 个股分析提示词(价位上下文)

设计:
  - 纯函数 + polars 向量化,毫秒级,无需落盘。
  - 每个点位带 {value, label, type, side, strength?},前端直接画水平价格线。
  - NaN/Inf 全部过滤,空数据返回空列表,不抛异常。
"""
from __future__ import annotations

import logging
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)


# ================================================================
# 输出结构
# ================================================================

class PriceLevel:
    """单个价位点的数据结构(用 dict 表达,这里只作文档说明)。

    {
      "value": 12.34,          # 价格
      "label": "压力位 R1",     # 显示标签
      "type": "pivot",         # 类型分组(同类型用一个开关按钮控制显隐)
      "side": "resistance",    # 方向:resistance(压力) / support(支撑) / neutral
      "strength": "medium",    # 强度:strong / medium / weak(可选,影响线型)
      "rank": 1,               # 档位(仅 pivot 有):0=P,1=R1/S1,2=R2/S2,3=R3/S3
                               #   前端按"显示到第几档"过滤,非 pivot 点位无此字段
    }
    """


# 价位分组 → 开关 key。前端按这个 type 显隐。
LEVEL_TYPES = {
    "sr": "压力支撑",        # 成交密集区(价量:Volume Profile POC + 高成交密集区)
    "pivot": "枢轴点",        # 经典 Pivot P/R/S
    "extreme": "前高前低",    # 60/250 日极值 + 近期 swing 高低点
    "boll": "布林带",         # MA20 ± 2σ,标准差波动带(参考性,非真实支撑压力)
    "keltner_s": "Keltner短期",  # MA20 ± 2×ATR
    "keltner_m": "Keltner中期",  # MA60 ± 2.5×ATR
    "keltner_l": "Keltner长期",  # MA120 ± 3×ATR(牛熊趋势边界)
    "atr_stop": "ATR波动通道",    # close±nATR 动态波动带(中性命名,非操作指令)
    "gap": "缺口位",          # 未回补跳空缺口
    "fib": "斐波那契",        # 回撤位 0.236~0.786
    "round": "整数关口",      # 心理整数位
}


# ================================================================
# 1. 压力位 / 支撑位 —— 成交量分布 (Volume Profile)
# ================================================================

def _chip_weights(df: pl.DataFrame, bins: int = 40):
    """筹码分布内核 —— 换手率衰减模型的**唯一**实现点。

    它被两处消费，两处必须看到同一组桶边界与同一组权重，否则
    「POC 线」与「分布图的最高柱」会指向不同价位（同一真相被写两遍）：

      - `_support_resistance`：只取 POC + 前 2 个密集区 → 图上 3 条水平线
      - `compute_chip_distribution`：取全部 40 桶 → 横向分布图 + 获利盘/套牢盘

    ## 分摊方式：按价格区间**重叠长度**，不是全给中心桶

    早先的实现把一根 K 线的成交量整份摊到它覆盖的每个桶（`v / n_buckets`），
    等于假设这根 K 线的成交在 [low, high] 上均匀分布，但桶内位置又被当成矩形。
    当窗口内价格跨度大（除权边界 / 次新 / 仙股）时桶宽被撑到 0.5 元以上，
    现行价附近的几十根 K 线全挤进一两个桶，获利盘比例可以偏 50 个百分点
    （实测 688496.SH：分桶 63.7% vs 精确积分 13.3%，而全样本**均值无偏**
    0.494 vs 0.498 —— 无偏的噪声型错误，只看汇总永远发现不了）。

    现在按每根 K 线与每个桶的价格**重叠长度**加权分摊，桶内不再有位置假设，
    与 `_profit_split` 的精确积分口径一致（差异只剩离散化，< 半个桶宽）。

    返回 (edges, chips)；edges 长度 bins+1，chips 长度 bins。数据不足返回 (None, None)。
    """
    if df.is_empty() or "volume" not in df.columns or df.height < 20:
        return None, None

    hi = float(df["high"].max())
    lo = float(df["low"].min())
    if not (hi > lo > 0):
        return None, None

    n = df.height
    step = (hi - lo) / bins
    edges = [lo + i * step for i in range(bins + 1)]

    highs = df["high"].to_numpy().astype(float)
    lows = df["low"].to_numpy().astype(float)
    vols = df["volume"].to_numpy().astype(float)
    if "turnover_rate" in df.columns:
        turnovers = df["turnover_rate"].to_numpy().astype(float)
        turnovers = [0.0 if (t is None or t != t) else float(t) for t in turnovers]
    else:
        turnovers = [0.0] * n

    # 第 i 根 K 线在"今天"的存活权重 = vol_i × Π_{k=i+1..n-1}(1 - t_k)
    # 反序遍历一次即可；与逐日递推 `chips *= (1-t); chips += vol` 严格等价。
    decay_after = [1.0] * n
    acc = 1.0
    for i in range(n - 1, -1, -1):
        decay_after[i] = acc
        t = max(0.0, min(float(turnovers[i] or 0.0), 100.0)) / 100.0
        acc *= (1.0 - t)

    chips = [0.0] * bins
    for i in range(n):
        w = float(vols[i] or 0.0) * decay_after[i]
        if w <= 0:
            continue
        lo_i, hi_i = float(lows[i]), float(highs[i])
        span = hi_i - lo_i
        if span <= 0:
            continue
        b0 = max(0, min(bins - 1, int((lo_i - lo) / step)))
        b1 = max(0, min(bins - 1, int((hi_i - lo) / step)))
        if b1 < b0:
            b0, b1 = b1, b0
        covered = 0.0
        for b in range(b0, b1 + 1):
            seg = min(hi_i, edges[b + 1]) - max(lo_i, edges[b])
            if seg > 0:
                chips[b] += w * seg / span
                covered += seg
        # 浮点边界残差（< 1e-9 量级）归到中心桶，避免总额对不上权重和
        if covered < span * 0.999:
            chips[min(max(b0, 0), bins - 1)] += w * max(0.0, 1.0 - covered / span)

    return edges, chips


def _profit_split(df: pl.DataFrame, close: float) -> dict | None:
    """获利盘 / 套牢盘 / 近处套牢盘的**精确积分**（不分桶）。

    每根 K 线的成交在 [low, high] 上均匀，落在现价以下的比例就是区间重叠长度占比。
    与 `_chip_weights` 用同一组换手衰减权重，但不经过任何离散化 —— 三个标量
    不受桶宽影响，价格跨度大的票也不会失真。

    分布图（`compute_chip_distribution` 的 prices/weights）仍走 40 桶，那是**画图**
    必须的离散化；标量走这里。两者差异只来自离散化，不会出现"图上数柱子与标称值
    差 30 个百分点"。
    """
    if df.is_empty() or "volume" not in df.columns or df.height < 20:
        return None
    n = df.height
    highs = df["high"].to_numpy().astype(float)
    lows = df["low"].to_numpy().astype(float)
    vols = df["volume"].to_numpy().astype(float)
    if "turnover_rate" in df.columns:
        tr = df["turnover_rate"].to_numpy().astype(float)
        tr = [0.0 if (t is None or t != t) else float(t) for t in tr]
    else:
        tr = [0.0] * n

    decay_after = [1.0] * n
    acc = 1.0
    for i in range(n - 1, -1, -1):
        decay_after[i] = acc
        t = max(0.0, min(float(tr[i] or 0.0), 100.0)) / 100.0
        acc *= (1.0 - t)

    total = below = 0.0
    cost_sum = 0.0
    caps = {0.05: close * 1.05, 0.10: close * 1.10, 0.20: close * 1.20}
    over = {k: 0.0 for k in caps}
    for i in range(n):
        w = float(vols[i] or 0.0) * decay_after[i]
        if w <= 0:
            continue
        lo_i, hi_i = float(lows[i]), float(highs[i])
        span = hi_i - lo_i
        if span <= 0:
            continue
        total += w
        cost_sum += w * (lo_i + hi_i) / 2.0
        below += w * min(1.0, max(0.0, (min(hi_i, close) - lo_i) / span))
        for k, cap in caps.items():
            seg = min(hi_i, cap) - max(lo_i, close)
            if seg > 0:
                over[k] += w * seg / span

    if total <= 0:
        return None
    return {
        "profit_ratio": below / total,
        "trapped_ratio": 1.0 - below / total,
        "overhead": {f"p{int(k * 100)}": over[k] / total for k in sorted(caps)},
        "avg_cost": cost_sum / total,
    }


def _support_resistance(df: pl.DataFrame, bins: int = 40) -> list[dict]:
    """筹码分布(换手率衰减模型) —— 国内主流 A 股支撑/压力位算法。

    算法依据:大智慧/同花顺/通达信"筹码分布"专利模型(CN109711994A),物理含义是
    "当前各价位还剩多少筹码(持仓成本分布)",而非海外 Volume Profile 的"历史成交
    堆积"。两者关键区别在于衰减方式:
      - 海外 VP: 无衰减, 历史成交永远累加(反映"曾经在哪换过手")
      - 国内筹码分布: 按换手率衰减, 反映"现在谁还拿着"(物理正确的持仓成本)

    核心迭代公式(逐日, 从老到新):
        当日收盘后各价位筹码 = 前一日各价位筹码 × (1 - 当日换手率)
                            + 当日新增成交(按 high~low 区间分摊到对应价位桶)

    物理解释: 今天市场换了 turnover_rate 比例的手, 意味着昨天所有价位的筹码中有
    这么比例被换走了(被卖掉), 同时今天新成交的筹码按当天价格区间分布在各价位。
    这样:
      - 低换手股票的老筹码衰减慢(长期横盘低换手 → 历史套牢盘长期保留, 符合真实)
      - 高换手股票的老筹码快速消失(近期高换手 → 老筹码被消化, 近期成本占主导)
    比固定时间衰减更聪明: 停牌期间换手率为 0, 筹码不动; 固定时间衰减会错误地衰减。

    成交量按 high~low 价格区间分摊到桶(而非全归 (high+low)/2 中点), 振幅大的 K 线
    不再污染中点价, 更接近真实成交分布。

    密集区输出:
      - POC(控制点):当前筹码量最大的桶(=最多人持仓的成本区), strong
      - 其他高筹码区:高于均值, medium, 最多 2 个
    """
    edges, chips = _chip_weights(df, bins)
    if edges is None:
        return []

    # 只保留有筹码的桶
    bin_ids = [k for k in range(bins) if chips[k] > 0]
    if not bin_ids:
        return []
    vals = [chips[k] for k in bin_ids]
    mean_val = sum(vals) / len(vals) if vals else 0

    def bin_mid(bin_id: int) -> float:
        return (edges[bin_id] + edges[bin_id + 1]) / 2

    close = float(df.tail(1)["close"][0])

    out: list[dict] = []
    # POC: 当前筹码量最大的桶(最多人持仓的成本区)
    poc_pos = max(range(len(vals)), key=lambda i: vals[i])
    poc_mid = bin_mid(bin_ids[poc_pos])
    out.append({"value": round(poc_mid, 2), "label": "成交密集区(POC)",
                "type": "sr", "side": _side(poc_mid, close), "strength": "strong"})

    # 其他高筹码区(高于均值, 排除 POC), 按筹码量降序取 2 个
    candidates = [(i, v) for i, v in enumerate(vals) if v > mean_val and i != poc_pos]
    candidates.sort(key=lambda x: x[1], reverse=True)
    for i, _v in candidates[:2]:
        mid = bin_mid(bin_ids[i])
        out.append({"value": round(mid, 2), "label": "成交密集区",
                    "type": "sr", "side": _side(mid, close), "strength": "medium"})
    return out


# ================================================================
# 1b. 筹码分布明细 —— 横向分布图 + 获利盘 / 套牢盘 / 集中度
# ================================================================

def compute_chip_distribution(df: pl.DataFrame, bins: int = 40) -> dict | None:
    """完整筹码分布(供个股分析页的横向分布图)。

    这里**不产生新算法**，只是把 `_chip_weights` 的中间结果摊开，并算出
    几个可解释的派生量。全部是价格与成交量的确定性函数 —— 它们描述"成本
    在哪里"，不预测"会涨到哪"。页面上任何"抛压低→超额"的说法都必须来自
    独立回测，不能从这张图读出来。

    **不截断样本**：`_support_resistance` 吃的是同一份 df，如果这里再 tail(N)
    截一刀，两个消费者会在同一张图上给出两个 POC（实测差 0.11 元）。样本长度
    由调用方决定（`/levels` 传多少天就算多少天），这里只如实报告 df.height。
    换手率衰减本身就会让高换手票的老筹码自然消失，不需要额外的窗口裁剪。

    返回:
      prices     桶中心价(升序)
      weights    该桶筹码量(与 prices 等长，已归一化到合计 1)
      poc        筹码最重的价位
      avg_cost   筹码加权平均成本
      profit_ratio  现价及以下的筹码占比 —— "获利盘"
      trapped_ratio 现价以上的筹码占比 —— "套牢盘"(即上方供给)
      concentration 90% 筹码所在价格带的宽度 / 中位价(越小越集中)
      overhead     现价 +5% / +10% / +20% 以内的套牢盘占比(近处阻力密度)
    """
    edges, chips = _chip_weights(df, bins)
    if edges is None:
        return None
    total = sum(chips)
    if total <= 0:
        return None

    close = float(df.tail(1)["close"][0])
    mids = [(edges[k] + edges[k + 1]) / 2 for k in range(bins)]

    # 三个标量走精确积分（不分桶）：价格跨度大的票分桶误差可达 50pp。
    # 分布图的 prices/weights 仍是 40 桶 —— 那是画图必须的离散化。
    split = _profit_split(df, close)
    if split is None:
        return None
    below = split["profit_ratio"] * total
    above = total - below
    avg_cost = split["avg_cost"]

    # 90% 筹码带：按筹码量升序累加，找到 5% 与 95% 分位所在价位
    order = sorted(range(bins), key=lambda k: mids[k])
    acc = 0.0
    lo_p = hi_p = mids[order[0]]
    for k in order:
        acc += chips[k]
        if acc <= total * 0.05:
            lo_p = mids[k]
        if acc <= total * 0.95:
            hi_p = mids[k]
    mid_med = (lo_p + hi_p) / 2
    concentration = (hi_p - lo_p) / mid_med if mid_med > 0 else None

    poc_mid = mids[max(range(bins), key=lambda k: chips[k])]
    return {
        "prices": [round(p, 2) for p in mids],
        "weights": [round(w / total, 6) for w in chips],
        "poc": round(poc_mid, 2),
        "avg_cost": round(avg_cost, 2),
        "close": round(close, 2),
        "profit_ratio": round(below / total, 4),
        "trapped_ratio": round(above / total, 4),
        "concentration": round(concentration, 4) if concentration is not None else None,
        "overhead": {
            "p5": round(split["overhead"]["p5"], 4),
            "p10": round(split["overhead"]["p10"], 4),
            "p20": round(split["overhead"]["p20"], 4),
        },
        "span": {"low": round(mids[0], 2), "high": round(mids[-1], 2)},
        "lookback": int(df.height),
    }


# ================================================================
# 2. 枢轴点 (Pivot Point) —— 经典公式,基于最近完整交易日
# ================================================================

def _pivot_points(df: pl.DataFrame) -> list[dict]:
    """经典 Pivot:P = (H+L+C)/3, R1/R2/R3, S1/S2/S3。

    基准:最后 1 根 K(代表"上一交易日")。实务中常用前一日,这里取最后一根。
    """
    if df.is_empty():
        return []
    last = df.tail(1)
    h = last["high"][0]
    l = last["low"][0]
    c = last["close"][0]
    if not _ok(h) or not _ok(l) or not _ok(c):
        return []

    h, l, c = float(h), float(l), float(c)
    p = (h + l + c) / 3
    r1 = 2 * p - l
    s1 = 2 * p - h
    r2 = p + (h - l)
    s2 = p - (h - l)
    r3 = h + 2 * (p - l)
    s3 = l - 2 * (h - p)

    def lv(v: float, label: str, side: str, strength: str, rank: int) -> dict:
        # rank:档位标记,前端据此按"显示到第几档"过滤
        #   0 = 枢轴位 P(始终显示)
        #   1 = R1/S1(第一档压力/支撑)
        #   2 = R2/S2(第二档)
        #   3 = R3/S3(第三档,极端,实际很少触及)
        return {"value": round(v, 2), "label": label, "type": "pivot",
                "side": side, "strength": strength, "rank": rank}

    return [
        lv(p, "枢轴位 P", "neutral", "strong", 0),
        lv(r1, "压力位 R1", "resistance", "medium", 1),
        lv(r2, "压力位 R2", "resistance", "medium", 2),
        lv(r3, "压力位 R3", "resistance", "weak", 3),
        lv(s1, "支撑位 S1", "support", "medium", 1),
        lv(s2, "支撑位 S2", "support", "medium", 2),
        lv(s3, "支撑位 S3", "support", "weak", 3),
    ]


# ================================================================
# 3. 前高 / 前低 —— 60 / 120 / 250 日极值
# ================================================================

def _extreme_levels(df: pl.DataFrame) -> list[dict]:
    """关键前高 / 前低 —— 历史极值 + 近期 swing 高低点(收敛后)。

    设计:把所有"前高前低"类点位集中在本组,与 sr(通道)区分:
      - 60 日极值:近一季度高低点(短期参照)
      - 250 日极值:年度高低点(牛熊分界参照);跳过 120 日(被 250 日包含,信息冗余)
      - swing 高低点:近期局部转折点,每侧只取距当前价最近的 2 个
    """
    if df.is_empty():
        return []
    close = float(df.tail(1)["close"][0]) if "close" in df.columns else None
    out: list[dict] = []

    # —— 历史极值(只取 60 / 250,避免中间档冗余)——
    for n in (60, 250):
        if df.height < n:
            continue
        sub = df.tail(n)
        hi = float(sub["high"].max())
        lo = float(sub["low"].min())
        if _ok(hi):
            out.append({"value": round(hi, 2), "label": f"{n}日新高",
                        "type": "extreme", "side": "resistance", "strength": "strong"})
        if _ok(lo):
            out.append({"value": round(lo, 2), "label": f"{n}日新低",
                        "type": "extreme", "side": "support", "strength": "strong"})

    # —— 近期 swing 高低点(每侧只取距当前价最近的 2 个,避免点位爆炸)——
    win = 5
    if df.height > win * 2 and close:
        highs = df["high"].to_list()
        lows = df["low"].to_list()
        swing_highs: list[float] = []
        swing_lows: list[float] = []
        for i in range(win, len(highs) - win):
            if highs[i] == max(highs[i - win:i + win + 1]):
                swing_highs.append(float(highs[i]))
            if lows[i] == min(lows[i - win:i + win + 1]):
                swing_lows.append(float(lows[i]))

        # 聚合 ±1% 相近价位,再按距当前价排序取最近 2 个
        agg_h = _aggregate_levels(swing_highs, 0.01)
        agg_h = [v for v in agg_h if v > close * 1.001]
        agg_h.sort(key=lambda v: abs(v - close))
        for v in agg_h[:2]:
            out.append({"value": round(v, 2), "label": "前高",
                        "type": "extreme", "side": "resistance", "strength": "medium"})

        agg_l = _aggregate_levels(swing_lows, 0.01)
        agg_l = [v for v in agg_l if v < close * 0.999]
        agg_l.sort(key=lambda v: abs(v - close))
        for v in agg_l[:2]:
            out.append({"value": round(v, 2), "label": "前低",
                        "type": "extreme", "side": "support", "strength": "medium"})

    return out


# ================================================================
# 4. 波动通道 —— 布林带 + Keltner 三档,各自独立开关
# ================================================================

def _ma_value(df: pl.DataFrame, ma_col: str | None, window: int) -> float | None:
    """取某档均线值:优先用预计算列,缺失则现场 rolling_mean。"""
    last = df.tail(1)
    if ma_col and ma_col in df.columns:
        v = last[ma_col][0]
        return float(v) if _ok(v) else None
    if df.height >= window:
        v = df.select(pl.col("close").rolling_mean(window)).tail(1)["close"][0]
        return float(v) if _ok(v) else None
    return None


def _keltner_band(
    df: pl.DataFrame, ma_col: str | None, window: int, n: float,
    label_short: str, type_key: str,
) -> list[dict]:
    """单档 Keltner 通道:均线 ± n×ATR。

    ATR 自适应波动,通道宽度随行情自动收缩/扩张。type_key 决定归入哪一组
    (keltner_s / keltner_m / keltner_l),前端各自独立开关。
    """
    if df.is_empty() or df.height < 20 or "atr_14" not in df.columns:
        return []
    last = df.tail(1)
    close = float(last["close"][0]) if "close" in df.columns else 0
    atr = float(last["atr_14"][0])
    if not close or not _ok(atr):
        return []

    ma_val = _ma_value(df, ma_col, window)
    if ma_val is None:
        return []
    upper = ma_val + n * atr
    lower = ma_val - n * atr
    return [
        {"value": round(upper, 2), "label": f"{label_short}通道上轨",
         "type": type_key, "side": _side(upper, close), "strength": "medium"},
        {"value": round(lower, 2), "label": f"{label_short}通道下轨",
         "type": type_key, "side": _side(lower, close), "strength": "medium"},
    ]


def _boll_channel(df: pl.DataFrame) -> list[dict]:
    """布林带上下轨(MA20 ± 2σ)。

    基于标准差的波动带,反映价格相对均线的统计偏离;非真实支撑压力,
    仅作波动边界参考。数据直接取预计算列 boll_upper/boll_lower。
    """
    if df.is_empty() or "boll_upper" not in df.columns or "boll_lower" not in df.columns:
        return []
    last = df.tail(1)
    close = float(last["close"][0]) if "close" in df.columns else 0
    if not close:
        return []
    bu = last["boll_upper"][0]
    bl = last["boll_lower"][0]
    if not _ok(bu) or not _ok(bl):
        return []
    bu, bl = float(bu), float(bl)
    out = [
        {"value": round(bu, 2), "label": "布林上轨",
         "type": "boll", "side": _side(bu, close), "strength": "medium"},
        {"value": round(bl, 2), "label": "布林下轨",
         "type": "boll", "side": _side(bl, close), "strength": "medium"},
    ]
    # 布林中轨 = MA20(多空平衡线,价格在其上下分强弱);数据层已预计算 ma20
    if "ma20" in df.columns:
        mid = last["ma20"][0]
        if _ok(mid):
            mid = float(mid)
            out.append({"value": round(mid, 2), "label": "布林中轨",
                        "type": "boll", "side": _side(mid, close), "strength": "medium"})
    return out


def _keltner_short(df: pl.DataFrame) -> list[dict]:
    """Keltner 短期:MA20 ± 2×ATR(近期波动带,约一个月)。"""
    return _keltner_band(df, "ma20", 20, 2.0, "短期", "keltner_s")


def _keltner_mid(df: pl.DataFrame) -> list[dict]:
    """Keltner 中期:MA60 ± 2.5×ATR(季度波动带)。"""
    return _keltner_band(df, "ma60", 60, 2.5, "中期", "keltner_m")


def _keltner_long(df: pl.DataFrame) -> list[dict]:
    """Keltner 长期:MA120 ± 3×ATR(半年波动带,牛熊趋势边界)。"""
    return _keltner_band(df, None, 120, 3.0, "长期", "keltner_l")


# ================================================================
# 5. ATR 波动通道 —— close ± n × ATR,动态波动带
# ================================================================

def _atr_stops(df: pl.DataFrame) -> list[dict]:
    """基于 ATR 的动态波动通道(close ± n×ATR)。

    ATR 衡量平均真实波幅,close ± n×ATR 刻画围绕现价的波动带边界:
      - 上轨:close + 2×ATR  (波动上沿)
      - 下轨:close - 2×ATR  (波动下沿)
      - 近端波动带:close ± 1.5×ATR (中短期波动参考)
    命名采用中性"轨道"措辞,客观描述波动范围,不含操作指令。
    """
    if df.is_empty() or "atr_14" not in df.columns:
        return []
    last = df.tail(1)
    close = float(last["close"][0])
    atr = float(last["atr_14"][0])
    if not _ok(close) or not _ok(atr):
        return []

    def lv(v: float, label: str, side: str, strength: str) -> dict:
        return {"value": round(v, 2), "label": label, "type": "atr_stop",
                "side": side, "strength": strength}

    return [
        lv(close + 2 * atr, "ATR 上轨(+2)", "resistance", "medium"),
        lv(close + 1.5 * atr, "ATR 上轨(+1.5)", "resistance", "weak"),
        lv(close - 1.5 * atr, "ATR 下轨(-1.5)", "support", "weak"),
        lv(close - 2 * atr, "ATR 下轨(-2)", "support", "medium"),
    ]


# ================================================================
# 6. 缺口位 (Gap) —— 未回补的跳空缺口
# ================================================================

def _gap_levels(df: pl.DataFrame, lookback: int = 120) -> list[dict]:
    """近期未回补的向上/向下跳空缺口。

    向上缺口:当日 low > 前日 high(开盘跳空高开,全天未回补)
    向下缺口:当日 high < 前日 low(开盘跳空低开,全天未回补)

    缺口是天然的支撑/阻力位。只保留"未回补"的:缺口形成后,后续任何一根 K 线的
    价格(low/high)只要回到缺口区间内,就算已回补(支撑/阻力已被测试消化),过滤掉。
    这与"当前价是否在缺口内"无关 —— 即使价格后来远离,只要曾经回补过就不算有效缺口。
    最后按价格聚合相近缺口(±0.5%),每方向只取距当前价最近的 2~3 个。
    """
    if df.is_empty() or df.height < 5:
        return []
    sub = df.tail(lookback) if df.height > lookback else df
    close = float(df.tail(1)["close"][0])
    highs = sub["high"].to_list()
    lows = sub["low"].to_list()

    # 收集缺口: (形成位置 i, 缺口下沿, 缺口上沿)
    # 向上缺口: 第 i 日 low > 第 i-1 日 high, 缺口区间 = (highs[i-1], lows[i])
    # 向下缺口: 第 i 日 high < 第 i-1 日 low, 缺口区间 = (highs[i], lows[i-1])
    up_gaps: list[tuple[int, float, float]] = []
    dn_gaps: list[tuple[int, float, float]] = []
    for i in range(1, len(highs)):
        if _ok(highs[i]) and _ok(lows[i]) and _ok(highs[i - 1]) and _ok(lows[i - 1]):
            if lows[i] > highs[i - 1]:
                up_gaps.append((i, float(highs[i - 1]), float(lows[i])))
            elif highs[i] < lows[i - 1]:
                dn_gaps.append((i, float(highs[i]), float(lows[i - 1])))

    def _filter_unfilled(gaps: list[tuple[int, float, float]]) -> list[float]:
        """过滤掉已被回补的缺口。

        回补判定: 缺口在位置 i 形成, 向后扫描 i+1..end, 只要任意一根 K 线的价格区间
        完全覆盖缺口真空带(low <= g_hi 且 high >= g_lo), 即价格真正穿越了缺口 → 已回补。
        仅"触及缺口边缘"(如 low 跌到上沿)不算回补, 因为缺口是价格真空带, 必须整个
        被某根 K 线的高低区间覆盖才算填补了真空。
        """
        mids: list[float] = []
        for i, g_lo, g_hi in gaps:
            filled = False
            for j in range(i + 1, len(highs)):
                if lows[j] <= g_hi and highs[j] >= g_lo:
                    filled = True
                    break
            if not filled:
                mids.append((g_lo + g_hi) / 2)
        agg = _aggregate_levels(mids, 0.005)
        agg.sort(key=lambda v: abs(v - close))
        return agg[:3]

    out: list[dict] = []
    for mid in _filter_unfilled(up_gaps):
        out.append({"value": round(mid, 2), "label": "向上缺口",
                    "type": "gap", "side": _side(mid, close), "strength": "medium"})
    for mid in _filter_unfilled(dn_gaps):
        out.append({"value": round(mid, 2), "label": "向下缺口",
                    "type": "gap", "side": _side(mid, close), "strength": "medium"})
    return out


# ================================================================
# 7. 斐波那契回撤 —— 基于近期波段的回撤位
# ================================================================

def _fibonacci_levels(df: pl.DataFrame, window: int = 120) -> list[dict]:
    """基于近期一段明确趋势的斐波那契回撤位。

    取近 window 个交易日的最高/最低点:
      - 若高点出现在低点之后(上涨波段):从低到高,回撤 = high - range × ratio
      - 若低点出现在高点之后(下跌波段):从高到低,回撤 = low + range × ratio
    比率:0.236 / 0.382 / 0.5 / 0.618 / 0.786
    """
    if df.is_empty() or df.height < 10:
        return []
    sub = df.tail(window) if df.height > window else df
    close = float(df.tail(1)["close"][0])

    highs = sub["high"].to_list()
    lows = sub["low"].to_list()
    hi_pos = highs.index(max(highs))
    lo_pos = lows.index(min(lows))
    hi_val = float(highs[hi_pos])
    lo_val = float(lows[lo_pos])
    if not _ok(hi_val) or not _ok(lo_val) or hi_val <= lo_val:
        return []

    ratios = [0.236, 0.382, 0.5, 0.618, 0.786]
    rng = hi_val - lo_val

    out: list[dict] = []
    # 判断波段方向:高点在低点之后 = 上涨波段(从低回撤)
    up_trend = hi_pos > lo_pos
    for r in ratios:
        if up_trend:
            val = hi_val - rng * r          # 从高点向下回撤
        else:
            val = lo_val + rng * r          # 从低点向上回撤
        out.append({"value": round(val, 2), "label": f"Fib {int(r * 1000) / 10:.1f}%",
                    "type": "fib", "side": _side(val, close), "strength": "medium"})
    return out


# ================================================================
# 8. 整数关口 —— 心理支撑/阻力位
# ================================================================

def _round_numbers(df: pl.DataFrame, pct: float = 0.10, max_count: int = 8) -> list[dict]:
    """当前价附近的心理整数关口。

    整数位(如 10/11/12元,或 60/65/70元)是天然的心理支撑/阻力,
    低价股尤其明显。按价格量级自适应步长:
      - 价格 < 10:   步长 0.5  (如 6.5, 7.0, 7.5)
      - 价格 < 20:   步长 1    (如 11, 12, 13)
      - 价格 < 100:  步长 5    (如 60, 65, 70)
      - 价格 < 500:  步长 10   (如 110, 120, 130)
      - 价格 >= 500: 步长 50   (如 1100, 1150, 1200)
    过滤掉距当前价 <1% 的(太近,无分析价值),最多 max_count 个。
    """
    if df.is_empty():
        return []
    close = float(df.tail(1)["close"][0])
    if not _ok(close):
        return []

    if close < 10:
        step = 0.5
    elif close < 20:
        step = 1.0
    elif close < 100:
        step = 5.0
    elif close < 500:
        step = 10.0
    else:
        step = 50.0

    lo = close * (1 - pct)
    hi = close * (1 + pct)
    # 找区间 [lo, hi] 内所有 step 的整数倍(严格限定在区间内)
    start = (int(lo / step) + (1 if lo % step > 0 else 0)) * step
    candidates: list[float] = []
    v = start
    while v <= hi:
        if v > 0:
            candidates.append(round(v, 2))
        v += step

    # 按距当前价从近到远排序,取前 max_count 个
    candidates.sort(key=lambda x: abs(x - close))
    out: list[dict] = []
    for v in candidates[:max_count]:
        # 过滤距当前价 <1% 的(太近,无分析价值)
        if abs(v - close) / close < 0.01:
            continue
        out.append({"value": round(v, 2), "label": f"整数关口 {v:g}",
                    "type": "round", "side": _side(v, close), "strength": "weak"})
    return out

def compute_levels(df: pl.DataFrame) -> dict[str, list[dict]]:
    """计算 11 类价位点,返回 {分组key: [点位...]}。

    分组 key 与 LEVEL_TYPES 一致(sr / pivot / extreme / boll /
    keltner_s / keltner_m / keltner_l / atr_stop / gap / fib / round),
    前端按 key 渲染开关按钮,逐组显隐。
    """
    if df.is_empty():
        return {k: [] for k in LEVEL_TYPES}

    try:
        return {
            "sr": _support_resistance(df),
            "pivot": _pivot_points(df),
            "extreme": _extreme_levels(df),
            "boll": _boll_channel(df),
            "keltner_s": _keltner_short(df),
            "keltner_m": _keltner_mid(df),
            "keltner_l": _keltner_long(df),
            "atr_stop": _atr_stops(df),
            "gap": _gap_levels(df),
            "fib": _fibonacci_levels(df),
            "round": _round_numbers(df),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("compute_levels failed: %s", e)
        return {k: [] for k in LEVEL_TYPES}


def summarize_levels(levels: dict[str, list[dict]], close: float | None) -> str:
    """生成给 AI 提示词的价位摘要文本(紧凑,供上下文)。"""
    if not close:
        return "无价位数据"
    parts: list[str] = []
    # 当前价
    parts.append(f"当前价 {close:.2f}")
    # 每组取前 2 个最相关的(距当前价近的优先)
    for key, label in LEVEL_TYPES.items():
        pts = levels.get(key, [])
        if not pts:
            continue
        # 按距当前价排序,取前 2
        ranked = sorted(pts, key=lambda p: abs(p["value"] - close))[:2]
        desc = "、".join(
            f"{p['label']}={p['value']}" for p in ranked
        )
        parts.append(f"{label}: {desc}")
    return " · ".join(parts)


# ================================================================
# 内部工具
# ================================================================

def _ok(v: Any) -> bool:
    """数值有效(非空/非 NaN/非 Inf/正数)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    import math
    return math.isfinite(f) and f > 0


def _side(level: float, close: float) -> str:
    """价位相对当前价的方向。"""
    if level > close * 1.001:
        return "resistance"
    if level < close * 0.999:
        return "support"
    return "neutral"


def _aggregate_levels(values: list[float], tol: float) -> list[float]:
    """把相近的价位聚合(±tol),返回去重后的代表值(保留最新)。"""
    if not values:
        return []
    values = sorted(values)
    out: list[float] = [values[0]]
    for v in values[1:]:
        if abs(v - out[-1]) / out[-1] <= tol:
            out[-1] = v  # 聚合到最新(更近期)
        else:
            out.append(v)
    return out
