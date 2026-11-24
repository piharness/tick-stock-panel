"""接力停止 = 顶部？—— 「同一批席位锁仓 + 股价新高」的后续收益。

## 动机

前面的检验说明 relay（接力度）作为**入场**信号没有 alpha。但反向用法未被检验：
当一只已被炒过的票，接力度归零（每天都是同一批席位在买）、且股价仍在 20 日新高，
这是「主力还没走」还是「已经没有新钱进来接」？

这两个解释给出相反的预测，必须用数据裁决。

## 状态定义（T 日盘后全部已知）

  lock   = relay5 <= 0.2  （今日买入前5中 <=1 个是新面孔：老面孔锁仓）
  relay  = relay5 >= 0.6  （>=3 个新面孔：还在换人接力）
  nearhi = T 日收盘 >= 近 20 日收盘最高 * 0.98

## 输出

2x2 状态 × 前向超额收益（T+1 开盘入）。
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"
KLINE = ROOT / "data" / "kline_daily_enriched"
H = (1, 3, 5, 10, 20)


def main() -> None:
    fwd = pd.read_parquet(OUT / "seat_relay_fwd.parquet")
    fwd["date"] = pd.to_datetime(fwd.date)
    mk = pd.read_parquet(OUT / "market_fwd_equalweight.parquet")

    files = sorted(glob.glob(str(KLINE / "date=*/part.parquet")))
    files = [f for f in files if "date=2025-1" in f or "date=2026" in f]
    k = pd.concat([pd.read_parquet(f, columns=["symbol", "date", "close", "turnover_rate"])
                   for f in files], ignore_index=True)
    k["date"] = pd.to_datetime(k.date.astype(str))
    k = k.sort_values(["symbol", "date"]).reset_index(drop=True)

    k["hi20"] = k.groupby("symbol").close.transform(lambda s: s.rolling(20, min_periods=10).max())
    k["ret20"] = k.groupby("symbol").close.pct_change(20)
    k["ret5"] = k.groupby("symbol").close.pct_change(5)
    kk = k[["symbol", "date", "hi20", "ret20", "ret5"]]

    d = fwd.merge(kk, on=["symbol", "date"], how="left").merge(mk, on="date", how="left")
    d = d[d.buyable].copy()
    for n in H:
        d[f"ex{n}"] = (d[f"f{n}"] - d[f"m{n}"]) * 1e4
    # 用 T 日收盘相对 20 日高点
    kt = k[["symbol", "date", "close", "hi20"]]
    d = d.merge(kt.rename(columns={"close": "t_close", "hi20": "t_hi20"}),
                on=["symbol", "date"], how="left")
    d["dist_hi"] = d.t_close / d.t_hi20 - 1

    base = d[d.streak >= 3].copy()
    base["state"] = np.select(
        [(base.relay5 <= 0.2) & (base.dist_hi >= -0.02),
         (base.relay5 >= 0.6) & (base.dist_hi >= -0.02),
         (base.relay5 <= 0.2) & (base.dist_hi < -0.02),
         (base.relay5 >= 0.6) & (base.dist_hi < -0.02)],
        ["锁仓+新高", "换人+新高", "锁仓+已回落", "换人+已回落"], default="中间")

    print("=" * 92)
    print("A. 2x2 状态 × 前向超额（bp，T+1 开盘入）  streak>=3")
    print("=" * 92)
    agg = base.groupby("state").agg(
        n=("ex1", "size"),
        **{f"ex{n}": (f"ex{n}", "mean") for n in H},
        p_win5=("ex5", lambda s: (s > 0).mean() * 100),
        ret20=("ret20", "mean"))
    agg["ret20"] = (agg.ret20 * 100).round(1)
    print(agg.round(0).to_string())

    print("\n" + "=" * 92)
    print("B. 锁仓+新高 按 streak 分层（越久越危险？）")
    print("=" * 92)
    lk = base[base.state == "锁仓+新高"].copy()
    lk["sb"] = pd.cut(lk.streak, [2, 3, 5, 8, 100], labels=["3", "4-5", "6-8", "9+"])
    print(lk.groupby("sb", observed=True).agg(
        n=("ex5", "size"), ex1=("ex1", "mean"), ex5=("ex5", "mean"),
        ex20=("ex20", "mean"), win5=("ex5", lambda s: (s > 0).mean() * 100)).round(0).to_string())

    print("\n" + "=" * 92)
    print("C. 左尾：锁仓+新高 vs 换人+新高，ex20 分位")
    print("=" * 92)
    for st in ["锁仓+新高", "换人+新高"]:
        g = base[base.state == st].ex20.dropna()
        if len(g) < 10:
            continue
        print(f"  {st:<10} n={len(g):>4} p5={np.percentile(g, 5):>7.0f} p25={np.percentile(g, 25):>7.0f} "
              f"median={np.median(g):>7.0f} mean={g.mean():>7.0f} p95={np.percentile(g, 95):>7.0f}")

    print("\n" + "=" * 92)
    print("D. 三只标的当前状态")
    print("=" * 92)
    for s, name in [("002242.SZ", "九阳股份"), ("000530.SZ", "冰山冷热"), ("002086.SZ", "东方海洋")]:
        g = base[base.symbol == s].sort_values("date").tail(1)
        if g.empty:
            print(f"  {name}: 无 streak>=3 记录")
            continue
        r = g.iloc[0]
        print(f"  {name:<6} {r.date.date()}  relay5={r.relay5:.2f}  streak={int(r.streak)}  "
              f"距20日高 {r.dist_hi * 100:+.1f}%  → {r.state}  "
              f"(实际后续 ex5={r.ex5:.0f}bp ex20={r.ex20 if pd.notna(r.ex20) else float('nan'):.0f}bp)")


if __name__ == "__main__":
    main()
