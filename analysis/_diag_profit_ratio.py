"""获利盘比例（筹码分布的核心派生量）到底有没有预测力。

这是"抛压"最贴近直觉的度量：现价以下有多少筹码（获利盘），就有多少潜在止盈卖压。
上一轮验证了换手率与户数，唯独没测它 —— 因为要逐日递推筹码。

计算用**解析积分**而不是 40 桶分摊：
  每根 K 线的筹码均匀落在 [low, high]，落在现价以下的比例是
      frac_j = clip((min(high_j, close) - low_j) / (high_j - low_j), 0, 1)
  权重 w_j = vol_j × Π_{k>j}(1 - turnover_k)     ← 换手率衰减，与 levels.py 同模型
  profit_ratio = Σ w_j·frac_j / Σ w_j

这比分桶更精确（无离散化误差），但与页面上的 40 桶版本会有 1~2% 的差异 ——
本脚本顺带量化这个差异，免得两边数字对不上时无从判断谁错。

窗口 250 日（与页面 levels 的取数区间同量级）。观测日与其余回测脚本一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _diag_entry_point_dist import dist_stats, prep  # noqa: E402

WIN = 250
BINS = 40


def profit_ratio_series(close, high, low, vol, to, win: int = WIN, targets=None):
    """在指定终点索引上计算获利盘比例（其余位置留 NaN）。

    对终点 i：窗口 [i-win+1, i]，权重按"从 i 往前衰减"累积。
        w_j = vol_j * exp(Σ_{k=j+1..i} ln(1-t_k))
    用 cumsum 一次算出，避免 O(win²)。

    **只算 targets**：全序列 2000 天逐日算要 30 分钟以上，而回测只需要 118 个
    观测日。`targets=None` 时退化为全量（仅供单票调试）。
    """
    n = len(close)
    out = np.full(n, np.nan)
    if n < 20:
        return out
    frac = np.clip(
        (np.minimum(high, close) - low) / np.maximum(high - low, 1e-9), 0.0, 1.0
    )
    tt = np.clip(np.asarray(to, dtype=float) / 100.0, 0.0, 1.0)
    logd = np.log1p(-tt)                      # 每日衰减的对数
    cs = np.concatenate([[0.0], np.cumsum(logd)])
    idx_list = range(19, n) if targets is None else [i for i in targets if i >= 19]
    for i in idx_list:
        j0 = max(0, i - win + 1)
        seg = np.arange(j0, i + 1)
        # Σ_{k=j+1..i} logd_k = cs[i+1] - cs[j+1]
        w = vol[seg] * np.exp(cs[i + 1] - cs[seg + 1])
        tot = w.sum()
        out[i] = (w * frac[seg]).sum() / tot if tot > 0 else np.nan
    return out


def binned_profit_ratio(close, high, low, vol, to, win: int = WIN, bins: int = BINS,
                        targets=None):
    """与 levels.py 同口径的 40 桶版本，只为对比差异。"""
    n = len(close)
    out = np.full(n, np.nan)
    idx_list = range(19, n) if targets is None else [i for i in targets if i >= 19]
    for i in idx_list:
        j0 = max(0, i - win + 1)
        lo, hi = low[j0:i + 1].min(), high[j0:i + 1].max()
        if not (hi > lo > 0):
            continue
        step = (hi - lo) / bins
        chips = np.zeros(bins)
        tt = np.clip(to[j0:i + 1] / 100.0, 0.0, 1.0)
        for k in range(len(tt)):
            chips *= (1.0 - tt[k])
            if vol[j0 + k] <= 0:
                continue
            kl = min(int((low[j0 + k] - lo) / step), bins - 1)
            kh = min(int((high[j0 + k] - lo) / step), bins - 1)
            kl, kh = max(0, min(kl, kh)), min(bins - 1, max(kl, kh))
            chips[kl:kh + 1] += vol[j0 + k] / (kh - kl + 1)
        mids = lo + step * (np.arange(bins) + 0.5)
        tot = chips.sum()
        if tot > 0:
            out[i] = chips[mids <= close[i]].sum() / tot
    return out


def main() -> None:
    df = prep()
    # prep() 已过滤到观测日，这里需要完整序列才能滚动 —— 直接从日K重算
    from _diag_chip_pressure import build_panel

    panel = build_panel().sort(["symbol", "rn"])
    obs_dates = set(df["date"].unique().to_list())

    rows = []
    diffs = []
    for sym, g in panel.group_by("symbol", maintain_order=True):
        g = g.sort("rn")
        if g.height < 60:
            continue
        c = g["close"].to_numpy().astype(float)
        h = g["high"].to_numpy().astype(float)
        lo = g["low"].to_numpy().astype(float)
        v = g["volume"].to_numpy().astype(float)
        t = g["turnover_rate"].fill_null(0).to_numpy().astype(float)
        dates = g["date"].to_list()
        symname = g["symbol"][0]
        tgt = [i for i, d in enumerate(dates) if d in obs_dates]
        if not tgt:
            continue
        pr = profit_ratio_series(c, h, lo, v, t, targets=tgt)
        for i in tgt:
            if np.isfinite(pr[i]):
                rows.append({"symbol": symname, "date": dates[i], "profit": float(pr[i])})
        # 每票抽 1 个观测日对比两种口径（40 桶版慢，不宜全跑）
        one = tgt[:1]
        bp = binned_profit_ratio(c, h, lo, v, t, win=WIN, targets=one)
        for i in one:
            if i >= 19 and np.isfinite(bp[i]) and np.isfinite(pr[i]):
                diffs.append(abs(bp[i] - pr[i]))

    chip = pl.DataFrame(rows)
    print(f"获利盘样本 {chip.height:,} 票日 / {chip['symbol'].n_unique()} 只")
    if diffs:
        d = np.array(diffs)
        print(f"口径对比（解析 vs 40 桶）：n={len(d)} 中位差 {np.median(d)*100:.2f}pp "
              f"p90 {np.quantile(d,0.9)*100:.2f}pp max {d.max()*100:.2f}pp")

    merged = df.join(chip, on=["symbol", "date"], how="inner")
    print(f"并入回测样本 {merged.height:,}")
    base = dist_stats(merged["fwd20"].to_numpy())
    print(f"\nbase: 胜率 {base['win']:.2%} 中位 {base['med']*100:+.2f}% "
          f"Q10 {base['q10']*100:+.1f}% 暴跌 {base['crash']:.2%}")
    print(f"\n{'获利盘分位':<16}{'n票日':>9}{'胜率':>8}{'Δ胜率':>8}{'中位':>8}"
          f"{'Q10':>8}{'暴跌率':>8}{'盈亏比':>8}")
    m = merged.with_columns(
        (pl.col("profit").rank("average").over("date")
         / pl.col("profit").count().over("date") * 5).ceil().clip(1, 5).cast(pl.Int32).alias("g")
    )
    for g in range(1, 6):
        s = dist_stats(m.filter(pl.col("g") == g)["fwd20"].to_numpy())
        tag = {1: "G1 获利盘最少", 5: "G5 获利盘最多"}.get(g, f"G{g}")
        print(f"  {tag:<14}{s['n']:>9}{s['win']:>7.1%}{(s['win']-base['win'])*100:>+7.1f}pp"
              f"{s['med']*100:>+7.1f}%{s['q10']*100:>+7.1f}%{s['crash']:>7.1%}{s['pl']:>8.2f}")


if __name__ == "__main__":
    main()
