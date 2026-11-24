"""席位接力的前向检验 —— 换人 vs 不换人，后续收益差多少。

## 口径

- 事件：`seat_relay_panel.parquet` 中的 (symbol, T)，T = 龙虎榜公布日。
- 入场：**T+1 开盘**（T 日盘后才知道席位，这是最早可执行时点）。
- 出场：T+1+n 收盘（n = 1/3/5/10/20）。
- 不可执行剔除：T+1 开盘价已达涨停（主板 >=9.5%、创业/科创 >=19%、北交 >=29%），
  买不到，不计入。
- 组间比较天然消除市场 beta：同一交易日的样本共享同一基准，故直接比组均值即超额。

## 核心检验

relay5（今日买入前5中过去5日未出现过的席位占比）分桶 → 前向收益。
若「席位接力」是有效信号，relay 高组应显著优于 relay 低组。
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / ".scratch"
KLINE = ROOT / "data" / "kline_daily_enriched"

HORIZONS = (1, 3, 5, 10, 20)


def limit_pct(sym: str) -> float:
    c = sym.split(".")[0]
    if c.startswith(("300", "301", "688", "689")):
        return 0.20
    if c.startswith(("8", "4", "920")):
        return 0.30
    return 0.10


def load_kline() -> pd.DataFrame:
    files = sorted(glob.glob(str(KLINE / "date=*/part.parquet")))
    files = [f for f in files if "date=2025-1" in f or "date=2026" in f]
    k = pd.concat(
        [pd.read_parquet(f, columns=["symbol", "date", "open", "close", "raw_close",
                                     "high", "low", "amount", "turnover_rate"]) for f in files],
        ignore_index=True,
    )
    k["date"] = pd.to_datetime(k.date.astype(str))
    return k.sort_values(["symbol", "date"]).reset_index(drop=True)


def main() -> None:
    panel = pd.read_parquet(OUT / "seat_relay_panel.parquet")
    panel["date"] = pd.to_datetime(panel.date)
    k = load_kline()

    # 每票的交易日序列索引
    k["i"] = k.groupby("symbol").cumcount()
    kk = k[["symbol", "date", "i", "open", "close", "raw_close", "high", "low",
            "amount", "turnover_rate"]].copy()
    prev = k.groupby("symbol").close.shift(1)
    kk["prev_close"] = prev.values
    kk["ret_cc"] = kk.close / kk.prev_close - 1

    ev = panel.merge(kk[["symbol", "date", "i"]], on=["symbol", "date"], how="inner")
    print(f"事件 {len(panel):,} → 可对齐日线 {len(ev):,}", flush=True)

    # 前向：T+1 开盘入场
    kmap = {s: g.reset_index(drop=True) for s, g in kk.groupby("symbol")}

    recs = []
    for r in ev.itertuples():
        g = kmap.get(r.symbol)
        if g is None:
            continue
        j = r.i + 1
        if j >= len(g):
            continue
        e = g.iloc[j]
        # 开盘涨停剔除：T+1 开盘相对 T 收盘的跳空，达到涨跌幅上限即买不到
        lp = limit_pct(r.symbol)
        gap = e.open / g.iloc[r.i].close - 1
        buyable = gap < lp * 0.95
        rec = {"symbol": r.symbol, "date": r.date, "streak": r.streak,
               "relay5": r.relay5, "relay10": r.relay10, "net5_ratio": r.net5_ratio,
               "n_seat": r.n_seat, "entry_gap": gap, "buyable": buyable,
               "t_turn": g.iloc[r.i].turnover_rate,
               "t_ret": g.iloc[r.i].close / g.iloc[r.i].prev_close - 1 if pd.notna(g.iloc[r.i].prev_close) else np.nan}
        for n in HORIZONS:
            jj = j + n - 1
            rec[f"f{n}"] = (g.iloc[jj].close / e.open - 1) if jj < len(g) else np.nan
        recs.append(rec)

    fwd = pd.DataFrame(recs)
    fwd.to_parquet(OUT / "seat_relay_fwd.parquet", index=False)
    print(f"前向样本 {len(fwd):,}，可买 {fwd.buyable.sum():,} ({fwd.buyable.mean():.1%})", flush=True)

    d = fwd[fwd.buyable].copy()
    for n in HORIZONS:
        d[f"f{n}_bp"] = d[f"f{n}"] * 1e4

    print("\n" + "=" * 78)
    print("A. relay5 分桶 × 前向收益（bp，T+1 开盘入）  streak>=3")
    print("=" * 78)
    sub = d[d.streak >= 3]
    bins = [-.01, .2, .4, .6, .8, 1.01]
    sub = sub.assign(bucket=pd.cut(sub.relay5, bins))
    agg = sub.groupby("bucket", observed=True).agg(
        n=("f1_bp", "size"),
        **{f"f{n}": (f"f{n}_bp", "mean") for n in HORIZONS},
    )
    print(agg.round(0).to_string())

    print("\n" + "=" * 78)
    print("B. 二分：relay5>=0.8（全换人） vs <=0.4（老面孔为主）")
    print("=" * 78)
    for lab, mask in [("全换人 relay>=0.8", sub.relay5 >= 0.8), ("老面孔 relay<=0.4", sub.relay5 <= 0.4)]:
        g = sub[mask]
        line = f"{lab:<22} n={len(g):>5}"
        for n in HORIZONS:
            line += f"  f{n}={g[f'f{n}_bp'].mean():>7.0f}"
        print(line)

    print("\n" + "=" * 78)
    print("C. 控制 streak ≤5（早期接力才有意义）")
    print("=" * 78)
    sub2 = d[(d.streak >= 3) & (d.streak <= 5)]
    for lab, mask in [("relay>=0.8", sub2.relay5 >= 0.8), ("relay<=0.4", sub2.relay5 <= 0.4)]:
        g = sub2[mask]
        line = f"{lab:<14} n={len(g):>5}"
        for n in HORIZONS:
            line += f"  f{n}={g[f'f{n}_bp'].mean():>7.0f}"
        print(line)

    print("\n" + "=" * 78)
    print("D. 按 streak 看 f5 / f20（全体可买）")
    print("=" * 78)
    d2 = d.assign(sb=pd.cut(d.streak, [0, 1, 2, 3, 5, 8, 100],
                            labels=["1", "2", "3", "4-5", "6-8", "9+"]))
    print(d2.groupby("sb", observed=True).agg(
        n=("f5_bp", "size"), f1=("f1_bp", "mean"), f5=("f5_bp", "mean"),
        f20=("f20_bp", "mean")).round(0).to_string())

    print("\n" + "=" * 78)
    print("E. net5_ratio（前5净买/成交额）分桶 × f5  —— 净买入更猛是否更好")
    print("=" * 78)
    d3 = d[d.streak >= 3].assign(nb=pd.qcut(d[d.streak >= 3].net5_ratio, 5, duplicates="drop"))
    print(d3.groupby("nb", observed=True).agg(
        n=("f5_bp", "size"), f1=("f1_bp", "mean"), f5=("f5_bp", "mean"),
        f20=("f20_bp", "mean"), relay=("relay5", "mean")).round(2).to_string())


if __name__ == "__main__":
    main()
