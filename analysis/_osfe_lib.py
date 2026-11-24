"""OS/FE 状态变量内核：向量化快速路径，**必须**与生产内核数值等价。

单一真相在 `backend/app/indicators/levels.py::_profit_split`（换手衰减筹码模型的
唯一实现点）。本模块只是它的向量化重写，用于「5,500 只 × 2,900 天 × 145 个观测日」
的量级；`assert_kernel_equivalence()` 是硬闸门，构建面板前必须通过，否则两个口径
会静默分叉（本仓最贵的复发错误）。

定义（报告必带，避免第二个定义点）：
  设 c=收盘，窗口 win=750 个交易日，
      w_j = volume_j × Π_{k=j+1..t}(1 − turnover_k)      （与内核同式）
  在每根 K 线成交沿 [low, high] 均匀的假设下：
      below(t) = Σ_j w_j · |[l_j,h_j] ∩ (−∞, c]| / (h_j−l_j)
      shelf(t) = Σ_j w_j · |[l_j,h_j] ∩ (c, c(1+s)]| / (h_j−l_j)      s=10%
      near(t)  = Σ_j w_j · |[l_j,h_j] ∩ [c(1−n), c(1+n)]| / (h_j−l_j)  n=10%
      near_old(t) = 同上但只累加 j ≤ t−60

    OS = 1 − shelf/total        「上方筹码真空度」：现价上方 10% 内几乎没有持仓
    FE = near_old/near          「浮筹枯竭度」：现价±10% 内由 60 日前的老筹码占多数
    profit = below/total        控制变量（获利盘比例）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

WIN = 750
SHELF = 0.10
NEAR = 0.10
OLD_DAYS = 60


def _decay_log(turnover_pct: np.ndarray) -> np.ndarray:
    """cumsum(log1p(-t))；t 上限裁到 1−1e-12（内核在 t=1 时 acc 归零，等价）。"""
    t = np.nan_to_num(np.asarray(turnover_pct, dtype=np.float64), nan=0.0)
    t = np.clip(t / 100.0, 0.0, 1.0 - 1e-12)
    return np.cumsum(np.log1p(-t))


def chip_state(close: np.ndarray, high: np.ndarray, low: np.ndarray,
               vol: np.ndarray, turnover_pct: np.ndarray,
               evals: np.ndarray, win: int = WIN,
               shelf: float = SHELF, near: float = NEAR,
               old_days: int = OLD_DAYS) -> dict[str, np.ndarray]:
    close = np.asarray(close, dtype=np.float64)
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    vol = np.nan_to_num(np.asarray(vol, dtype=np.float64), nan=0.0)
    cl = _decay_log(turnover_pct)
    n = len(close)
    k = len(evals)
    os_ = np.full(k, np.nan)
    fe_ = np.full(k, np.nan)
    prof = np.full(k, np.nan)
    near_mass = np.full(k, np.nan)
    cost_gap = np.full(k, np.nan)
    os20 = np.full(k, np.nan)
    fe20 = np.full(k, np.nan)

    for idx, i in enumerate(evals):
        if i < 0 or i >= n:
            continue
        j0 = max(0, int(i) - win + 1)
        seg = np.arange(j0, int(i) + 1)
        span = high[seg] - low[seg]
        keep = span > 0
        if not keep.any():
            continue
        seg = seg[keep]
        span = span[keep]
        lo = low[seg]
        hi = high[seg]
        w = vol[seg] * np.exp(cl[i] - cl[seg])
        tot = w.sum()
        if not np.isfinite(tot) or tot <= 0:
            continue
        c = close[i]

        below = w * np.clip((np.minimum(hi, c) - lo) / span, 0.0, 1.0)
        sh = w * np.maximum(0.0, np.minimum(hi, c * (1 + shelf)) - np.maximum(lo, c)) / span
        sh20 = w * np.maximum(0.0, np.minimum(hi, c * 1.20) - np.maximum(lo, c)) / span
        nr = w * np.maximum(0.0, np.minimum(hi, c * (1 + near))
                            - np.maximum(lo, c * (1 - near))) / span
        nr20 = w * np.maximum(0.0, np.minimum(hi, c * 1.20)
                              - np.maximum(lo, c * 0.80)) / span
        old = seg <= int(i) - old_days

        os_[idx] = 1.0 - sh.sum() / tot
        os20[idx] = 1.0 - sh20.sum() / tot
        prof[idx] = below.sum() / tot
        near_mass[idx] = nr.sum() / tot
        nr_tot = nr.sum()
        if nr_tot > 1e-12:
            fe_[idx] = nr[old].sum() / nr_tot
        nr20_tot = nr20.sum()
        if nr20_tot > 1e-12:
            fe20[idx] = nr20[old].sum() / nr20_tot
        wsum = w.sum()
        cost_gap[idx] = (w * ((lo + hi) / 2.0)).sum() / wsum / c - 1.0

    return {"os": os_, "os20": os20, "fe": fe_, "fe20": fe20,
            "profit": prof, "near_mass": near_mass, "cost_gap": cost_gap}


def reference_state(df: pl.DataFrame, close: float,
                    shelf: float = SHELF, near: float = NEAR,
                    old_days: int = OLD_DAYS) -> dict[str, float]:
    """用生产内核 `_profit_split` 的同一组权重算同一组标量（参照真值）。"""
    from app.indicators.levels import _profit_split  # noqa: E402

    ref = _profit_split(df, close)
    if ref is None:
        return {}
    n = df.height
    highs = df["high"].to_numpy().astype(float)
    lows = df["low"].to_numpy().astype(float)
    vols = df["volume"].to_numpy().astype(float)
    tr = df["turnover_rate"].to_numpy().astype(float)
    tr = [0.0 if (t is None or t != t) else float(t) for t in tr]
    acc, decay = 1.0, [1.0] * n
    for i in range(n - 1, -1, -1):
        decay[i] = acc
        acc *= 1.0 - max(0.0, min(float(tr[i] or 0.0), 100.0)) / 100.0
    tot = sh = nr = nro = 0.0
    for i in range(n):
        w = float(vols[i] or 0.0) * decay[i]
        if w <= 0:
            continue
        lo_i, hi_i = float(lows[i]), float(highs[i])
        span = hi_i - lo_i
        if span <= 0:
            continue
        tot += w
        sh += w * max(0.0, min(hi_i, close * (1 + shelf)) - max(lo_i, close)) / span
        a = w * max(0.0, min(hi_i, close * (1 + near)) - max(lo_i, close * (1 - near))) / span
        nr += a
        if i <= n - 1 - old_days:
            nro += a
    return {"os": 1.0 - sh / tot, "fe": nro / nr if nr > 1e-12 else float("nan"),
            "profit": ref["profit_ratio"]}


def assert_kernel_equivalence(sample: pl.DataFrame, n_cases: int = 40, seed: int = 7,
                              tol: float = 1e-9) -> dict[str, float]:
    """在真实数据上比对向量化路径与生产内核；不通过就抛异常。"""
    rng = np.random.default_rng(seed)
    syms = sample["symbol"].unique().to_list()
    worst = {"os": 0.0, "fe": 0.0, "profit": 0.0}
    checked = 0
    for _ in range(n_cases):
        sym = syms[int(rng.integers(len(syms)))]
        g = sample.filter(pl.col("symbol") == sym).sort("date")
        if g.height < 120:
            continue
        i = int(rng.integers(100, g.height))
        j0 = max(0, i - WIN + 1)
        win = g.slice(j0, i - j0 + 1)
        got = chip_state(
            g["close"].to_numpy(), g["high"].to_numpy(), g["low"].to_numpy(),
            g["volume"].to_numpy(), g["turnover_rate"].to_numpy(),
            np.array([i]),
        )
        c = float(g["close"][i])
        ref = reference_state(win, c)
        if not ref:
            continue
        checked += 1
        for k in worst:
            worst[k] = max(worst[k], abs(float(got[k][0]) - ref[k]))
        # 生产内核自带的 p10/p20 也要能对上（防止本模块与内核各自漂移）
        assert abs(got["profit"][0] - ref["profit"]) < 1e-7, (sym, i, got["profit"], ref["profit"])
    bad = {k: v for k, v in worst.items() if not (v < tol)}
    if bad:
        raise AssertionError(f"筹码内核不等价（最大绝对差）：{bad}，checked={checked}")
    return {"checked": checked, **worst}
