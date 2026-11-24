"""席位状态差异的按日显著性 + 当前清单。

## 为什么要按日聚合

同一交易日的样本共享市场冲击，事件级 t 会高估显著性（记忆里的老坑）。
正确做法：先在每个交易日内算「状态 A 均值 − 状态 B 均值」，
再对这条日序列做单样本 t 检验。日序列的 N 是交易日数，不是事件数。

## 输出

1. 换人+新高 vs 锁仓+新高 的按日差异与 t
2. 截至最新席位日（2026-11-19）处于各状态的股票清单
"""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"
KLINE = ROOT / "data" / "kline_daily_enriched"


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

    d = fwd.merge(k[["symbol", "date", "close", "hi20", "ret20"]].rename(
        columns={"close": "t_close"}), on=["symbol", "date"], how="left")
    d = d.merge(mk, on="date", how="left")
    d = d[d.buyable].copy()
    for n in (1, 3, 5, 10, 20):
        d[f"ex{n}"] = (d[f"f{n}"] - d[f"m{n}"]) * 1e4
    d["dist_hi"] = d.t_close / d.hi20 - 1

    b = d[d.streak >= 3].copy()
    b["relay_hi"] = b.relay5 >= 0.6
    b["relay_lo"] = b.relay5 <= 0.2
    b["nearhi"] = b.dist_hi >= -0.02

    print("=" * 88)
    print("按日聚合 t 检验：换人+新高  vs  锁仓+新高")
    print("=" * 88)
    for n in (1, 3, 5, 10, 20):
        rows = []
        for dt, g in b[b.nearhi].groupby("date"):
            hi = g[g.relay_hi][f"ex{n}"]
            lo = g[g.relay_lo][f"ex{n}"]
            if len(hi) >= 1 and len(lo) >= 1:
                rows.append(hi.mean() - lo.mean())
        s = pd.Series(rows).dropna()
        if len(s) < 10:
            continue
        t = s.mean() / (s.std() / np.sqrt(len(s)))
        print(f"  ex{n:<3}: 日数={len(s):>3}  日均差={s.mean():>7.0f}bp  "
              f"t={t:>5.2f}  正日占比={(s > 0).mean():.0%}")

    print("\n" + "=" * 88)
    print("同样口径：换人+新高 vs 全体 streak>=3")
    print("=" * 88)
    for n in (1, 5, 20):
        rows = []
        for dt, g in b.groupby("date"):
            hi = g[g.relay_hi & g.nearhi][f"ex{n}"]
            if len(hi) >= 1 and len(g) >= 3:
                rows.append(hi.mean() - g[f"ex{n}"].mean())
        s = pd.Series(rows).dropna()
        t = s.mean() / (s.std() / np.sqrt(len(s)))
        print(f"  ex{n:<3}: 日数={len(s):>3}  日均差={s.mean():>7.0f}bp  t={t:>5.2f}")

    # ---------- 当前清单 ----------
    print("\n" + "=" * 88)
    print("截至 2026-11-19（最新席位日）的状态清单")
    print("=" * 88)
    last = b[b.date == b.date.max()].copy()
    last["state"] = np.select(
        [last.relay_hi & last.nearhi, last.relay_lo & last.nearhi,
         last.relay_hi & ~last.nearhi, last.relay_lo & ~last.nearhi],
        ["换人+新高", "锁仓+新高", "换人+已回落", "锁仓+已回落"], default="中间")
    print(last.state.value_counts().to_string())
    print()
    print(f"{'code':<10}{'state':<10}{'streak':>6}{'relay':>6}{'net5%':>7}{'距高%':>7}{'ret20%':>8}")
    for st in ["换人+新高", "锁仓+新高"]:
        g = last[last.state == st].sort_values("streak", ascending=False)
        print(f"-- {st}  (n={len(g)}) --")
        for r in g.head(15).itertuples():
            print(f"{r.symbol:<10}{st:<10}{int(r.streak):>6}{r.relay5:>6.2f}"
                  f"{r.net5_ratio * 100:>7.1f}{r.dist_hi * 100:>7.1f}{r.ret20 * 100:>8.0f}")

    last[["symbol", "date", "state", "streak", "relay5", "net5_ratio", "dist_hi",
          "ret20", "t_turn"]].to_csv(OUT / "seat_state_latest.csv", index=False, encoding="utf-8-sig")
    print(f"\n完整清单 → {OUT / 'seat_state_latest.csv'}（{len(last)} 只）")


if __name__ == "__main__":
    main()
