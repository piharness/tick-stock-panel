"""40 桶离散化到底把获利盘算歪了多少 —— 以及怎么修。

实测：`levels.py` 的 40 桶版本与"每根 K 线按 [low,high] 精确积分"相比，
中位差 32.6pp、p90 45.3pp、极值 100pp。这不是舍入误差，是系统偏差。

根因：桶范围取窗口内 `[min(low), max(high)]`。只要 250 日内出现过一次极端价
（除权边界、暴涨暴跌、停牌复牌跳空），桶宽就被撑到整个价格区间，当前价附近的
几十根 K 线全被塞进一两个桶里 —— 现价附近的筹码分布被彻底抹平。

三种候选修法，用同一批票对精确积分做基准：
  A 现状：[min, max] 全区间 40 桶
  B 裁剪：[p1, p99] 40 桶，越界筹码并入边界桶
  C 精确：不分桶，按每根 K 线 [low,high] 与现价的重叠积分（基准）

判据：中位绝对误差 <= 2pp 才算「与精确值一致」（页面标称值与图形不矛盾）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_chip_pressure import build_panel  # noqa: E402

WIN = 250
BINS = 40


def weights(vol, to, i, win=WIN):
    """终点 i 的窗口内各根 K 线的筹码权重（换手衰减后）。"""
    j0 = max(0, i - win + 1)
    seg = np.arange(j0, i + 1)
    tt = np.clip(to[seg] / 100.0, 0.0, 1.0)
    cs = np.concatenate([[0.0], np.cumsum(np.log1p(-tt))])
    return seg, vol[seg] * np.exp(cs[len(seg)] - cs[1:])


def exact(close, high, low, vol, to, i, win=WIN) -> float:
    seg, w = weights(vol, to, i, win)
    frac = np.clip((np.minimum(high[seg], close[i]) - low[seg])
                   / np.maximum(high[seg] - low[seg], 1e-9), 0.0, 1.0)
    tot = w.sum()
    return float((w * frac).sum() / tot) if tot > 0 else float("nan")


def binned(close, high, low, vol, to, i, mode: str, win=WIN, bins=BINS) -> float:
    seg, w = weights(vol, to, i, win)
    lo_raw, hi_raw = low[seg].min(), high[seg].max()
    if mode == "clip":
        lo, hi = np.quantile(low[seg], 0.01), np.quantile(high[seg], 0.99)
        lo, hi = min(lo, close[i]), max(hi, close[i])
    else:
        lo, hi = lo_raw, hi_raw
    if not (hi > lo > 0):
        return float("nan")
    step = (hi - lo) / bins
    chips = np.zeros(bins)
    for k, j in enumerate(seg):
        if vol[j] <= 0:
            continue
        # clip 模式下用原始 [low,high] 定桶，只把中心截到边界内
        kl = int(np.floor((low[j] - lo) / step))
        kh = int(np.floor((high[j] - lo) / step))
        kl, kh = max(0, min(kl, kh)), min(bins - 1, max(kl, kh))
        chips[kl:kh + 1] += w[k] / (kh - kl + 1)
    mids = lo + step * (np.arange(bins) + 0.5)
    tot = chips.sum()
    return float(chips[mids <= close[i]].sum() / tot) if tot > 0 else float("nan")


def main() -> None:
    panel = build_panel().sort(["symbol", "rn"])
    dates_all = sorted(panel["date"].unique().to_list())
    # 每 20 个交易日取一个观测点，取最近 6 个，压低算量
    obs = set(dates_all[i] for i in range(len(dates_all) - 1, 0, -20)[:6])

    errs = {"A": [], "B": []}
    n_sym = 0
    for sym, g in panel.group_by("symbol", maintain_order=True):
        g = g.sort("rn")
        if g.height < 300:
            continue
        c = g["close"].to_numpy().astype(float)
        h = g["high"].to_numpy().astype(float)
        lo = g["low"].to_numpy().astype(float)
        v = g["volume"].to_numpy().astype(float)
        t = g["turnover_rate"].fill_null(0).to_numpy().astype(float)
        dates = g["date"].to_list()
        hit = [i for i, d in enumerate(dates) if d in obs and i >= 60]
        if not hit:
            continue
        n_sym += 1
        for i in hit:
            base = exact(c, h, lo, v, t, i)
            if not np.isfinite(base):
                continue
            for k, mode in (("A", "raw"), ("B", "clip")):
                val = binned(c, h, lo, v, t, i, mode)
                if np.isfinite(val):
                    errs[k].append(abs(val - base))

    print(f"样本 {n_sym} 只票")
    for k, lab in (("A", "A 现状 [min,max]"), ("B", "B 裁剪 [p1,p99]")):
        e = np.array(errs[k])
        if not e.size:
            continue
        print(f"  {lab:<22} n={e.size:>6}  中位绝对误差 {np.median(e)*100:>6.2f}pp  "
              f"p90 {np.quantile(e,0.9)*100:>6.2f}pp  max {e.max()*100:>6.1f}pp  "
              f">5pp 占比 {(e>0.05).mean():>5.1%}")


if __name__ == "__main__":
    main()
