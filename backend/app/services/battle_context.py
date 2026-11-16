"""把今天的数字放回它自己的分布里 —— 参照系层。

为什么需要这一层：一个没有参照系的数字几乎不携带决策信息。「宽度 15%」本身
说明不了任何事，除非你知道它在近一年里排第几、以及同样窄的日子之后发生过
什么。这正是二手信息（复盘文章、短视频）比裸表面更有用的原因——它们至少给
了一个模糊的「罕见 / 正常」。本层把那句模糊的话算成可复现的分位与条件基数。

口径纪律：
- 分布**只用** `regime_history`（唯一产点，371 天）。不在这里重算宽度/封板率，
  否则就是同一真相的第二个定义点。
- 分位 = `≤ 当日值` 的占比（不插值），窗口默认 250 个交易日并显式返回 n。
- 条件统计的样本**严格早于目标日**：次日收益取 `regime_history` 的下一行
  `avg_pct`（全市场等权），不使用目标日之后才可能知道的信息。
- 样本 < MIN_ANALOG_DAYS 时不报均值，只报 n —— 小样本结论比没有结论更贵。
"""

from __future__ import annotations

import math
from typing import Any

import polars as pl

DEFAULT_WINDOW = 250
MIN_ANALOG_DAYS = 20

# (列名, 显示名, 单位)；单位只决定前端怎么格式化，不参与计算。
# up_ratio 是 **上涨/下跌 家数比**（1072/4225=0.2537），不是「上涨占比」
# （那是 1072/5571=20.2%）。两者相差一个数量级的读法，标签必须写全。
METRICS: tuple[tuple[str, str, str], ...] = (
    ("up_ratio", "涨跌家数比", "x"),
    ("median_pct", "中位涨幅", "pct"),
    ("seal_rate", "封板率", "pct"),
    ("broken_limit", "炸板家数", "int"),
    ("max_consecutive", "最高连板", "int"),
    ("total_amount", "成交额", "amount"),
    ("score", "环境分数", "score"),
)


def pct_rank(values: list[float], value: float) -> float:
    """value 在 values 中的分位（≤ 它的占比 × 100）。空表返回 nan。"""
    if not values:
        return float("nan")
    return 100.0 * sum(1 for v in values if v <= value) / len(values)


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _dist(rets: list[float | None]) -> dict[str, Any]:
    """一组次日收益 → {n, mean_bp, hit, t}。样本不足只返回 n。"""
    xs = [r for r in rets if r is not None]
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean_bp": None, "hit": None, "t": None}
    if n < MIN_ANALOG_DAYS:
        return {"n": n, "mean_bp": None, "hit": None, "t": None}
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return {
        "n": n,
        "mean_bp": round(mean * 1e4, 1),
        "hit": round(sum(1 for x in xs if x > 0) / n, 3),
        "t": round(t, 2),
    }


def build(df: pl.DataFrame, window: int = DEFAULT_WINDOW) -> dict[str, Any]:
    """regime_history → 今日分位 + 历史类比。纯函数，便于单测。"""
    if df is None or df.is_empty():
        return {"as_of": None, "window": window, "n_history": 0, "metrics": [], "analogs": {}}

    df = df.sort("date")
    rows = df.to_dicts()
    last = rows[-1]
    win = rows[-window:] if len(rows) > window else rows

    metrics: list[dict[str, Any]] = []
    for key, label, unit in METRICS:
        vals = [float(r[key]) for r in win if r.get(key) is not None]
        v = last.get(key)
        if v is None or not vals:
            continue
        v = float(v)
        metrics.append({
            "key": key,
            "label": label,
            "unit": unit,
            "value": v,
            "pct_rank": round(pct_rank(vals, v), 1),
            "p05": _quantile(vals, 0.05),
            "p50": _quantile(vals, 0.50),
            "p95": _quantile(vals, 0.95),
            "n": len(vals),
        })

    # 次日收益按「下一行」取：最后一行没有次日，不能进样本。
    fwd = {rows[i]["date"]: rows[i + 1].get("avg_pct") for i in range(len(rows) - 1)}
    hist = rows[:-1]

    analogs: dict[str, Any] = {
        "baseline": _dist([fwd.get(r["date"]) for r in hist]),
    }

    up_vals = [float(r["up_ratio"]) for r in win if r.get("up_ratio") is not None]
    q10 = _quantile(up_vals, 0.10)
    if q10 is not None:
        narrow = [r for r in hist if r.get("up_ratio") is not None and float(r["up_ratio"]) <= q10]
        analogs["narrow"] = {
            "label": f"涨跌家数比 ≤ 近 {len(up_vals)} 日 10 分位（{q10:.2f}）",
            **_dist([fwd.get(r["date"]) for r in narrow]),
        }

    same = [r for r in hist if r.get("state") == last.get("state")]
    analogs["same_state"] = {
        "label": f"同为「{last.get('state')}」",
        **_dist([fwd.get(r["date"]) for r in same]),
    }

    return {
        "as_of": str(last["date"]),
        "window": window,
        "n_history": len(rows),
        "state": last.get("state"),
        "score": last.get("score"),
        "metrics": metrics,
        "analogs": analogs,
    }
